# P6：验收、灰度发布与上线门禁计划

## 目标

P6 在 P0–P5 代码交付完成后，验证共享 PostgreSQL/Redis、P2 Outbox、P3 读取缓存、P4 广告 pacing 以及 P5 群运营中心在真实运行条件下的可用性，并通过受控 Telegram 灰度完成上线。P6 产出的是可审计的发布决定、监控基线和可执行回滚路径。

P6 不重新实现 P0–P5 的业务能力，不扩大 Telegram 配额，不增加发送并发，不绕过账号 Actor、预算账本、fencing token 或 reconciliation 保护。

## 前置依赖与责任

P5 必须先提供并由 Codex 验收：成员分类、Guardian/owned group 统一入口、成员与审计 API、RBAC/脱敏/分页、前端深链和能力状态。glm5.3 负责 P5 实现及交接说明；Codex 负责 P6 的验收编排、门禁记录、灰度和发布决定。运维负责 test 环境与生产观察，业务负责人负责灰度范围和最终放量批准。

生产 feature flag 在 P6 开始时保持：`P2_OUTBOX_ENABLED=false`、`P2_JOIN_OUTBOX_ENABLED=false`、`P2_SHADOW_MODE=true`、`P3_QUALIFICATION_SNAPSHOT=false`。除非对应门禁通过，否则不得打开下一级开关。

## 分阶段执行

### P6.0：P5 交接与代码冻结

**输入**：P5 提交、迁移、测试报告、变更清单和回滚说明。

**动作**：

1. 核对工作树、提交历史和未提交运行产物；确认 P5 没有改写 P0–P4 的停止语义、Actor 入口或数据库连接配置。
2. 运行 P0–P5 定向测试、Ruff、类型/编译检查和 `git diff --check`。
3. 逐项核对 feature flag 默认值、迁移顺序、权限边界、审计字段和 API 脱敏。
4. 形成 `p6-handoff` 记录，列出已知风险、未覆盖测试和责任人。

**停止条件**：P5 交付物缺失、测试失败、迁移不可逆、出现直连 Telegram 的外部 worker、或发现未解释的 P0–P4 回归时停止，不进入数据库验收。

### P6.1：隔离 PostgreSQL/Redis 验收

**输入**：P5 迁移和完整测试集合。

**动作**：

1. 使用隔离测试 PostgreSQL 和 Redis 执行全量迁移、回滚演练和数据约束测试。
2. 验证 qualification snapshot 不可变、版本匹配、Outbox 幂等唯一键、活动 reconciliation 唯一性、状态迁移条件更新和外键约束。
3. 验证租约过期、进程重启、fencing token、重复提交和历史回填不会产生重复操作或孤儿记录。
4. 使用共享 PostgreSQL canonical Compose 的配置形态进行连接和池上限检查；不得停止、重建或启动旧独立 PostgreSQL。

**输出**：迁移日志、约束测试报告、回滚记录和 schema 版本指纹。

**停止条件**：任何结构性不变量失败、迁移产生重复 Outbox、回滚无法恢复、连接池超过项目上限，或 Redis key/TTL 与 P3 约定不一致。

### P6.2：test 环境部署与 Shadow 观察

**输入**：P6.1 通过的构建产物。

**动作**：

1. 只部署应用服务，使用 canonical Compose；保留 Redis、授权配置和其他工作树变更。
2. 保持 `P2_SHADOW_MODE=true`，先观察资格快照、预算 reservation、Actor 提交、reconciliation 和 P5 成员 API，不产生真实发送副作用。
3. 至少观察一个完整业务高峰和一个低峰窗口，记录 session lease 竞争、Outbox 延迟、读取 lane flood wait、reconciliation 年龄和 API 错误率。

**停止条件**：session lease 竞争大于 0、广告 pending/unknown 增长、加群未知结果增长、reconciliation 存在未解释老化任务、P5 API 出现未授权数据或 5xx 回归。

### P6.3：受控 Telegram 灰度

**范围**：最多 2 个指定账号、最多 2 个 owned/Guardian 群，使用已批准的测试 campaign/run；不扩大账号数、群数或发送并发。

**顺序**：

1. 打开 `P3_ENTITY_CACHE`，确认 entity cache 命中和失效正确。
2. 打开 `P3_GROUP_FACT_CACHE`，确认成员/权限增量核验和强制刷新正确。
3. 打开 `P3_SURVIVAL_CACHE`，确认广告 survival 强制刷新、unknown 保护和 reconciliation 正常。
4. 由业务负责人确认前置资格 producer 已产出 fresh 快照后，才可在灰度范围内打开 `P3_QUALIFICATION_SNAPSHOT`。
5. 先打开广告 P2 Outbox；连续观察通过后，最后才允许打开 `P2_JOIN_OUTBOX_ENABLED`。加群开关仍以真实前置快照为硬条件，不得伪造或复用入群后资格结果。

每次只改变一个开关，观察至少一个稳定窗口；任何未知结果必须进入 reconciliation，禁止自动重发。

**停止条件**：广告 pending/unknown 非零且不能在一个 reconciliation 窗口内解释、加群未知结果持续增加、预算跳过率或 flood wait 显著回归、重复目标率上升、Actor fencing 或 lease contention 非零，立即关闭最近启用的开关并保持 shadow。

### P6.4：放量、观察与验收

灰度通过后，按账号和群逐步扩大范围，每次扩展不超过一倍，保持原有预算和发送并发。连续观察 24–48 小时，完成日报和最终验收记录。

## 发布门禁

### 数据库与 Redis

- schema 版本与迁移指纹匹配；所有 P2/P3/P5 约束测试通过。
- 活动幂等键、Outbox、reconciliation、snapshot 无重复或孤儿记录。
- `resolved`、`failed`、`cancelled` 不保留 `reconciliation_required`。
- Redis key 前缀、TTL、账号隔离和失效事件符合 P3 规格。

### Actor 与 Telegram

- session lease 竞争为 0，fencing token 拒绝旧 worker。
- 所有 Telegram 读取和发送均经账号 Actor；无外部 worker 直连。
- entity、群事实和 survival 缓存命中率达到既定基线，flood wait 时间无回归。
- 加群未知结果不持续增长，广告 `pending/unknown` 为 0。

### 业务与群运营中心

- reconciliation 无未解释的老化任务；超过年龄阈值的任务有告警和暂停记录。
- 35 个父群状态不变量持续成立。
- P5 成员分类、RBAC、脱敏、分页、审计和前端深链验收通过。
- 重复目标率、Outbox 延迟、回执延迟、资格审核调用量和错误率无回归。

## 监控基线

至少持续采集：reservation→send 延迟、Outbox oldest age、receipt 延迟、reconciliation oldest age、join unknown、ad pending/unknown、预算利用率、flood wait、session contention、Actor lease/fencing 拒绝、缓存命中率、P5 API 4xx/5xx、审核队列年龄和 35 群不变量检查结果。所有指标按账号、目标类型、campaign/run 和 feature flag 分组。

## 回滚与暂停

1. 发现门禁失败时，先关闭最近打开的 feature flag；保持 P2 shadow 和 reconciliation 消费，禁止手工重发。
2. 对未知结果目标暂停同目标新任务，等待 reconciliation 核验；保留 attempt、receipt、evidence 和审计记录。
3. 应用回滚只回滚应用镜像和可逆配置；数据库仅执行已验证的向后兼容迁移，不删除已写入操作、回执或快照。
4. 若 P5 前端或 API 回归，单独回滚前端/API 版本，保持 P2/P3 数据和审计链完整。
5. 回滚后重新运行 P6.1 约束检查和 P6.2 shadow 观察，连续稳定后才允许重试灰度。

## 预计工期

- P6.0 交接与代码冻结：1–2 人日。
- P6.1 隔离 PostgreSQL/Redis 验收：1–2 人日。
- P6.2 test 环境 Shadow：1–2 人日，含一个高峰窗口。
- P6.3 Telegram 灰度：2–4 人日，视账号和群的观察窗口而定。
- P6.4 24–48 小时观察与最终验收：2–3 人日。

总计约 7–13 人日；真实 Telegram 灰度受账号登录、群权限和外部 flood wait 影响，不能承诺固定时长。

## 完成定义

只有在 P5 交付确认、隔离数据库门禁通过、test shadow 稳定、受控灰度通过、24–48 小时观察无门禁回归，并完成回滚演练与审计记录后，P6 才标记为完成。代码通过不等于生产放行；生产放行必须保留独立的变更记录和业务负责人批准。
