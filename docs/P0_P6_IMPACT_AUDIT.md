# P0–P6 主流程影响审计

审计目的：证明当前 P0–P6 方案仍存在会影响生产加群和广告的结构性耦合，并定义在继续开发前必须满足的保护条件。本文只记录只读生产证据和代码路径；不执行 Telegram 操作，不修改共享 PostgreSQL/Redis。

## 判定

当前不能证明 P0–P6 对主流程无影响。生产必须保持 P2/P3/P4/P5 业务开关关闭，尤其是 `P2_JOIN_OUTBOX_ENABLED=false`。P6.3/P6.4 已废弃，不能用生产灰度结果替代隔离验证。

这不是因为新 Outbox 已经接管了主流程，而是因为旧的加群/广告路径仍经过 P0/P1 的共享预算、读取、容量、租约和 guard。新开关关闭只关闭新路径，不能移除这些共享门禁。

## 事实证据

以下事实来自当前生产检查窗口和仓库审计记录，生产检查保持只读：

| 项目 | 当前事实 | 影响 |
| --- | --- | --- |
| 健康状态 | `/health` healthy；backend、growth worker、reconciliation、Celery 重启次数为 0 | 只能证明进程存活，不能证明业务吞吐安全 |
| 新路径 | `growth_operation` 当前没有有效记录；P2/P3/P4/P5 关闭或未配置 | 生产仍由 legacy 路径执行，P2 幂等状态机没有保护实际发送 |
| 成功窗口 | 发布窗口后加群成功 6 次、广告成功 11 次，广告失败 0 次 | 证明短窗口可用，不足以证明架构无风险 |
| 历史结果 | 最近 24 小时加群成功 10、失败 107、跳过 1；广告成功 24、失败 426、跳过 1 | 存在明显历史回归，不能以短窗口覆盖 |
| 未解释任务 | inbox pending 约 56；`reconciliation_required` 约 3；最老任务约从 10 月 18 日开始 | 顺序阻塞和未知结果积压仍在 |
| 错误类别 | 历史出现 `account unavailable`、`account_not_in_pool`、`join_wait_execution_capacity`、`telegram_read_budget`、FloodWait 和广告 `message_id_unproven` | 任一类别增长都会直接减少可执行加群/广告 |
| 运行版本 | 曾出现 backend `local/vanguard:throughput-20261025-r3` 与 growth/reconciliation/Celery 使用旧 `p1-runtime` 镜像 | 同一 operation 可能使用不同 flags、预算和状态语义 |
| 代理 | 最近检查的代理状态已恢复；此前曾有规则拒绝导致 Telegram 连接失败 | 代理故障时无法把 P0–P6 灰度结果解释为业务结果 |

事实与推断必须分开。健康检查、计数、镜像和环境变量是事实；“某 flag 关闭后不会影响 legacy”是推断，必须由代码门禁和回归测试证明。

## 代码路径证明

1. `rpc_governor.check_read_ready()` 在 Telegram RPC 前可直接抛出 `RpcDeferred("telegram_read_budget")`；`check_dispatch_ready()` 可因 cooldown、预算或 guard 返回跳过。该入口被 legacy 加群/广告路径使用，因此 P0 是主流程门禁，不是旁路指标。
2. `read_work` 对 join work 检查 admission plan，预算 reservation 失败会阻止读取；监听、资格审核、加群对账、广告 survival 和 sync 共享账号读取预算。
3. legacy 广告自动化仍先调用 `check_dispatch_ready()`，再通过旧 account pool 直接执行 Telegram。P2 Outbox 关闭时，实际发送没有统一的原子 reservation、attempt、receipt 和未知结果保护。
4. P3 关闭后仍必须实时读取 Telegram；读取 miss、强制刷新和过期回源继续与其他业务竞争账号预算。P4 关闭后没有公平 pacing；P5 关闭后没有统一发布门禁和自动回滚。
5. reconciliation shadow 模式仍扫描数据库、写 Redis stream 和 durable records。即使不调用 Telegram，也会消耗数据库/Redis 资源，不能把 shadow 等同于零影响。

已有回归测试进一步证明 P5 的关闭语义存在危险：
`backend/tests/unit/modules/acquisition/test_p5_rollout.py::test_mode_off_rolls_back_the_active_release`
验证 `mode=off` 的 `observe_cycle` 会写入 stopped 状态；实现随后会把 stopped cohort 从 legacy 可用账号中删除。`test_new_release_clears_the_stop` 还表明，旧 stopped key 要等新 release 才会清除。因此 P5 off 不能被视为安全旁路：遗留 Redis stop key 仍可能阻断加群和广告。

因此，“所有实验 flag 关闭”只能说明新路径未接管；不能说明 P0/P1 的共享控制不会阻塞 legacy，也不能说明 P2 的幂等和未知结果保护已经存在。

## 必须保持的安全条件

在任何 P2–P6 开关重新启用前，生产只读门禁必须连续观察至少一个完整业务窗口，并同时满足：

- backend、growth、reconciliation、Celery 镜像和 flags 一致；
- 清理或隔离所有 P5 `STOPPED_KEY`，并有回归测试证明 `mode=off` 不会写入 legacy 阻断状态；
- 最近 1 小时和 24 小时的 `account_unavailable`、`account_not_in_pool`、`join_wait_execution_capacity`、`telegram_read_budget`、FloodWait、lease busy、unknown/pending 均不增长；
- 加群成功率、广告成功率和 Outbox/receipt 延迟不低于明确的 legacy 基线；
- inbox 最老事件和 reconciliation 最老任务持续下降；
- 任何未知结果都进入独立对账，未确认前禁止重发；
- 加群具备入群前 fresh qualification snapshot，否则保持 `P2_JOIN_OUTBOX_ENABLED=false`；
- 代理、Telegram 连接和账号 Actor 先通过独立连通性检查，不能把代理故障归因于 P0–P6。

任一条件失败都应自动阻止发布或立即回滚到 legacy 配置。历史 pending/reconciliation 数据不得直接删除或批量重发，必须逐条核验外部事实。

## 结论

当前 P0–P6 方案存在可验证的结构性问题：共享预算和容量门禁会直接影响加群/广告，P2 关闭时 legacy 发送没有统一幂等保护，P3/P4 关闭时读取与发送仍争抢资源，P5/P6 缺少一致的发布安全网。故生产安全结论为：继续使用已恢复的 legacy 主流程，保持 P2–P5 关闭，废弃 P6.3/P6.4，不进行生产灰度；先修复共享门禁、镜像一致性、历史积压和回归门禁，再重新评估。

## 已执行的安全改造

- P5 `mode=off` 会清理遗留 `STOPPED_KEY`，只记录 rollback 审计，不阻断 legacy 加群/广告。
- P2 Outbox 在未启用 P3 immutable qualification snapshot 时被配置校验拒绝。
- P1 关闭时 legacy inbox 使用独立的 batch/lease 配置，避免 rollout 参数改变旧路径。
- legacy 广告预算等待现在包含明确的 `retry_at` 和 `counts_as_business_skip=false`，便于调度重试和指标区分。

上述改造只改变本地代码和测试；生产 flags、Telegram、共享 PostgreSQL/Redis 均未改变。

## 2026-10-25 生产复核与运行时对齐

本次只修改了 reconciliation 监控服务的私有运行时配置，并保留了回滚副本：

- `TELEGRAM_READ_BUDGET_PERCENT=150`；
- `RECONCILIATION_SHADOW_MODE=true`；
- `RECONCILIATION_STREAM_ENABLED=true`；
- `P1_RUNTIME_RELIABILITY_ENABLED=false`；
- 通过 `/opt/shared-postgres/stacks/vanguard/compose.json` 与 monitoring overlay 对 `reconciliation` 执行 `up -d --no-deps`。

backend、Celery、Telegram workers、Redis 和共享 PostgreSQL 未重启。公网 `/health` 返回 `healthy`，reconciliation 容器恢复 `running/healthy`；Redis 认证只读检查 `vanguard:p5:rollout:v1:stopped` 返回不存在。P2/P3/P4/P5 业务开关仍按默认值关闭，`P2_JOIN_OUTBOX_ENABLED` 未启用。

复核窗口仍显示主流程阻塞，不能进入任何 P2–P6 灰度：最近一小时 Celery 出现大量 `session_owner_capacity_wait` 和 `account_operation_lease_busy`；growth worker 出现账号 35 `wrong session ID`、`Static proxy 20 is not loaded`、代理获取失败，以及账号 2 的 join/ad 执行超时。生产数据库中的账号 2、4、33、34、35 仍绑定静态代理 16–20，代理记录本身存在且 active，因此下一步应先修复 account pool 的静态代理加载/失效路径并单独恢复账号 session，再观察容量和租约错误是否下降。

在上述错误连续一个完整业务窗口下降前，保持所有 P2–P5 业务开关关闭；不得通过重发未知任务、手工插入资格快照或直接调用 Telegram 来制造验收结果。

## 2026-10-25 静态代理修复发布结论

本地提交 `b739dd9` 为账号池静态代理 fallback 增加了回归测试（`test_account_pool_persistent.py` 全部 23 项通过）。生产验证时发现运行镜像 `throughput-20261025-r3` 与 `/opt/vanguard` 源目录不是同一版本：只复制 `pool.py` 构建会因 `session_guard` 接口不匹配而启动失败；基于生产旧源构建的兼容镜像虽然能加载账号 35 的 proxy 20，但进一步暴露了 `Settings.p2_outbox_enabled_for` 缺失。两个候选镜像均已回滚，未保留在运行服务上。

当前 growth worker 已恢复 `local/vanguard:throughput-20261025-r3`，`RestartCount=0`，公网健康检查正常；最近复核无新的 `Static proxy`、`proxy_acquisition_error`、`telegram_session_quarantined` 或 session timeout。账号 2、33、34、35 的 session lease 在启动交接期间短暂 busy，随后均出现 `account_connected_by_id`。这证明代理加载修复方向正确，但必须先发布完整匹配的源代码和镜像，再重新验证，不能在版本漂移的生产源目录上单文件热补丁。

因此当前生产保持 legacy 镜像和 P2–P5 关闭；下一步是建立完整 release manifest（源代码 commit、镜像 digest、Compose override 一致）后，在不触碰共享 PostgreSQL/Redis 的前提下重新发布代理 fallback，并继续观察 lease/capacity 指标。
