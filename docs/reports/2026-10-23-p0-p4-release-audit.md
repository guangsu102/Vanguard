# P0–P4 核查报告（2026-10-23）

## 结论

P0–P4 不能作为已完成版本放行 P5。P0/P1 的预算、inbox、session lease 和 fencing 基础在隔离测试中通过；P2/P3 仍有生产接线缺口，P4 的 pacing 统计也存在口径问题。默认 feature flag 仍保持关闭，因此本次核查没有改变生产 Telegram 行为，也没有连接生产 PostgreSQL/Redis。

## 本轮修复

- P2 对账在获得确定外部证据后，和业务状态更新一起收尾对应 Outbox。发送租约仍有效时，对账先延后，避免 verifier 覆盖 dispatcher；成功收尾为 `succeeded`，确定失败收尾为 `dead`，同时清理租约字段。见 [p2_outbox.py](../../backend/app/modules/acquisition/p2_outbox.py:866)。
- P4 历史清理只删除仍有匹配终态 `GrowthOperation`（operation id 与幂等 key 同时匹配）的 archive。旧版仅剩 archive 的记录继续保留，避免删除唯一的重复发送保护。见 [history_governance.py](../../backend/app/modules/acquisition/history_governance.py:110)。
- 为上述行为增加了单元回归和隔离 PostgreSQL 回归，包括并发对账、回滚原子性、活动租约、终态失败和孤立 archive 场景。

## 已验证内容

- P0/P1 聚焦测试：68 passed，19 skipped。跳过项依赖隔离 Redis Unix socket；本机没有可用 Docker/WSL Redis，未连接生产 Redis 补测。
- P2/P3/P4 聚焦单元测试：77 passed。
- 本轮联合 P0–P4 聚焦回归：126 passed。
- 隔离 PostgreSQL 发布门禁：11 passed；另有前序 P0–P4 门禁 7 passed。测试使用临时 loopback 集群，结束后已停止，不读取应用 `DATABASE_URL`。
- 本轮修改文件的 Ruff 和 `git diff --check` 通过。`automation.py` 的 32 个既有 lint 未纳入本轮修复，也不能据此宣称全仓 lint 通过。

## 阻塞项

### P2 主链路接线状态

P2 的 producer、Outbox consumer 和账号 Actor 接线已经完成，代码路径见
[p2_dispatch.py](../../backend/app/modules/acquisition/p2_dispatch.py:39) 和
[growth_dispatch.py](../../backend/app/core/scheduler/growth_dispatch.py:560)。启用对应
canary flag 后，`growth_dispatch.execute` 会在 session owner 内先生成加群/广告
Outbox，再由 `dispatch_outbox_once` 通过当前 fencing token 执行；普通 worker 没有
owner 时 fail closed，不会回退到 legacy Telegram 直发。加群 producer 只接受带当前
fact watermark 的 fresh、qualified preview，并在同一事务创建独立 join reservation、
qualification snapshot、legacy attempt 和 Outbox；广告 producer 还会拒绝 join scope
快照。P2 flags 仍默认关闭，当前状态是等待 canary，而不是已放量。

### P3 读取优化仍未形成完整端到端链路

- [read_cache.py](../../backend/app/core/account/read_cache.py:52) 的默认适配器通过 `account_pool.acquire_by_id` 取得 client 后执行读取；它没有提交到长期账号 Actor 队列，也没有在读取请求上校验 Actor generation。
- single-flight 只存在于一个 `TelegramReadCache` 实例的进程内，尚未实现跨 worker 的 1 秒合并和同账号 50 目标批处理。
- peer/id entity key 目前主要用于生成和失效；正常审核、搜索链路仍没有统一读写这些 key。
- [get_qualification](../../backend/app/core/account/read_cache.py:800) 没有业务 caller，资格服务只写 snapshot/cache，尚未用缓存命中跳过 AI 审核。
- inbox 事件对事实版本递增的异常仍被吞掉；在迁移缺失时保护了 inbox，但会掩盖版本不一致，需要在启用 P3 前补可观测失败路径。

### P4 统计口径需要修正

[apply_to_dispatch](../../backend/app/modules/acquisition/ad_fairness_pacing.py:478) 在 broker publish、预算 reservation 成功之前就把 passing candidates 记为 `scheduled_count`；shadow 模式也会产生该计数。该字段目前不能作为实际投放吞吐门禁，应在真实 publish 完成后回写，或改名为 evaluated/passing count。

## P0/P1 结论

P0 的共享预算、reservation settle、session lease、wrong-session quarantine 和恢复路径通过本地单测及隔离 PostgreSQL 检查。P1 的 inbox claim、局部事实优先、attempted external write 不重放和 session execution 的账号串行/fencing 通过聚焦测试。Redis 依赖场景未在生产环境验证，所以发布门禁仍需在隔离 Redis 和 canary 环境完成：session contention 为 0、预算 lane 无异常增长、最老 inbox 年龄可控。

## P5 放行条件

在 P2 真实主链路接线、P3 Actor/batch/cache 命中链路和 P4 实际调度统计修正前，P5 保持 blocked。放行前必须重新跑隔离 PostgreSQL/Redis 回归，并在 canary 观察广告 `pending/unknown`、加群未知结果、reconciliation 老化任务、session contention 和 35 个父群状态不变量；本次没有读取生产指标，不能声称这些门禁已满足。

## 2026-10-24 关键阻塞修复

- 新增 `p2_dispatch.py`，由 session owner Actor 消费 durable Outbox；读取真实 session lease generation，校验资格快照和预算 reservation，并把未知结果送入 reconciliation。shadow 或 feature flag 关闭时保持 fail-closed，不产生 Telegram side effect。
- `growth_dispatch.execute` 在 P2 lane 开启且运行于 Actor owner 时切换到 P2 consumer；普通 worker 继续转发到 owner，不能直接调用 Telegram。
- P3 read cache 的 miss/refresh 已统一经 `submit_read` 和 owner batch（1 秒窗口、最多 50 个目标）；session generation 在 provider 调用前后校验。
- P4 pacing 的 `scheduled_count` 仅在 broker publish 成功后由 `finalize_published_decision` 回写，shadow 评估不计入实际调度数。

剩余放行阻塞：P2 代码接线和 scope/快照前置条件已经具备，但尚未完成生产 canary。
启用前仍需在隔离 PostgreSQL/Redis 验证 join reservation、snapshot、legacy read model
和 Outbox 的事务一致性，并确认 session contention、未知结果和 reconciliation 老化
门禁；在此之前 P2 active flag 必须保持关闭。

## 2026-10-24 广告 producer 补接记录

- `p2_dispatch.py` 新增 `produce_ad_outbox`。广告候选只有在 P3 当前不可变资格快照存在、事实版本仍匹配、素材可发送且本地广告策略允许时，才创建 durable `AccountBudgetReservation`、pending `AdDeliveryLog` 和 P2 Outbox；缺少快照、快照过期或 unknown 时直接跳过，不会伪造资格或发送。
- `execute_owner_outbox` 在广告 Actor owner 内先运行 producer，再消费 Outbox。producer 不执行 Telegram 读取或发送，发送仍只能由 session owner Actor 完成；shadow、flag 关闭或 lease 缺失时保持 fail-closed。
- P2 单测 fixture 已修正为每个逻辑操作使用独立 active reservation；复用已结算 reservation 的行为仍被拒绝。P2 Outbox 聚焦测试为 19 passed，相关 Ruff 检查通过。
- `produce_join_outbox` 已补接：它只接受账号 Actor 产生的 fresh candidate facts，并要求当前 Telegram fact watermark、明确 `explicit_allow` 或普通成员广告证据，随后在同一事务内创建不可变 snapshot、`join_budget` reservation、`AutoJoinAttempt` 和 P2 Outbox。普通 metadata preview、过期/unknown/版本不匹配证据均 fail-closed；不会伪造入群前资格。
- 加群 Actor 和 reconciliation 会将确认结果投影回 `GroupAccountMembership` 与 `AutoJoinAttempt`，预算 reservation 按其实际零读单元语义结算。实现已通过本地聚焦回归，但尚未完成生产 canary；`P2_JOIN_OUTBOX_ENABLED` 仍保持关闭，待 canary 前继续验证 scope 隔离、未知结果保护和老化任务门禁。
- 该补接只证明本地 durable producer 和状态边界，未连接生产 PostgreSQL/Redis，也未完成生产 canary；不能据此宣称 P0–P5 已放行。
- 本次本地验证：P2 producer/snapshot/Outbox/join budget 共 `48 passed`；P0–P4 聚焦 release gate 共 `155 passed`；隔离 PostgreSQL release gate 因本机未提供测试数据库而 `11 skipped`。生产 PostgreSQL/Redis 未连接。
