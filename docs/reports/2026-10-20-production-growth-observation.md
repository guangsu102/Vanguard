# 2026-10-20 生产加群与广告持续观察

检查时间：2026-10-20 12:49–13:06 UTC。

本轮在上一版会话隔离/资格修复之后，继续完成了一项小范围状态机修复和一次增量发布。生产发布仍使用 `oracle4c24g` 的 canonical Compose `/opt/shared-postgres/stacks/vanguard/compose.json`，没有停止或重建共享 PostgreSQL、Vanguard Redis、前端或其他业务容器。

## 结果

- 新增镜像：`local/vanguard:reservation-state-invariant-20261020T123200Z`
- 镜像 ID：`sha256:d3061ffc499461834f250d629ca3b6780eb305a753119ddee0e42cd59a2384da`
- 更新服务：backend、celery-worker、celery-beat、resource-search-worker、growth worker、guardian worker。
- 六个服务均运行中，重启次数为 0；`https://vanguard.pipenai.xyz/health` 返回 `healthy`。
- 新服务启动后旧租约在 90 秒 fencing TTL 内过期；启动瞬间出现的租约竞争随后收敛。最近 3 分钟没有新的 `AccountSessionLeaseBusy`、wrong-session 或 Traceback。
- 账号 2、33、34、35 均为 `online`，四个 Redis session lease 与 owner marker 均持续续期。

## 本轮状态修复

`acquisition_auto_join_attempt` 的记录 3389 曾经是“未发起 Telegram 请求、reservation 已释放、状态仍为 pending”。同账号同群的记录 3390 已确认成功，因此 3389 是被后续尝试取代的孤儿 reservation。已用并发条件锁定这两条证据，将 3389 收敛为：

- `status=skipped`
- `request_state=released`
- `reconciliation_status=resolved`
- 清空 `reservation_expires_at` 和 `reconciliation_next_at`

同时在 `backend/app/modules/acquisition/join_budget.py` 统一修复了过期、显式释放和 finalize 释放路径；以后未发起 Telegram 请求的 reservation 不会继续表现为 pending。15 项加群预算回归测试通过，目标文件的编译和 Ruff E/F 检查通过。

## 当前业务读数

- 35 个已修复父群仍全部为 `active`，对应目标 membership 全部是 `joined/approved/active`。
- 最近 24 小时广告：成功 40、失败 2、跳过 1；最近 48 小时 `pending=0`、`unknown=0`、逾期 survival=0；成功记录没有重复 Telegram 回执。
- 最近 7 天加群：成功 107、失败 8、跳过 38、pending 2。剩余两个 pending 都是账号 34 的已发送申请，均未到下一次对账时间；没有新的未发送 reservation pending。
- 79 条历史成功加群记录仍保留 `request_state=sent`、`reconciliation_status=pending`，但它们不在活动请求条件中，也不占用当前加群容量。这个历史字段应作为单独的低风险数据治理任务处理，不能与真正 pending 请求混为一谈。
- 事件 inbox 仍有 45 条旧 `message` pending 和 3 条 `reconciliation_required`，主要集中在账号 2、34，最早从 2026-10-18 开始。两条 `TimeoutError` 和一条 `stale_external_attempt_reconciliation` 需要先完成外部结果核验；后续同一 peer 的消息会按顺序等待，避免未知写入被重放。

13:05 UTC 的最终 15 分钟日志窗口中，growth、guardian、backend、celery 的 wrong-session、Traceback、`AccountSessionLeaseBusy` 均为 0；六个新服务重启次数均为 0，健康接口仍返回 `healthy`。监听事实持续消费，剩余积压只有上述需要安全对账的 message 事件。

## 原因判断

当前主链路已经可用，剩余吞吐问题来自读取预算和事件顺序约束：

1. 账号过去出现过 `contacts.ResolveUsernameRequest` flood wait，读取因子被降到 0.25–0.5；Redis 日读取计数已经达到或超过各账号当前恢复系数下的日上限。广告写入仍有成功，但候选预览、资格审核和加群对账会退避，所以近 7 天的主要跳过原因为 `telegram_read_budget` 和 `join_wait_execution_capacity`。
2. `message` inbox 对同一账号和 peer 保持顺序；最早的未知外部操作进入 `reconciliation_required` 后，后续消息暂停。这是安全的幂等策略，但需要单独的 reconciliation worker 和告警，否则会表现为长期积压。
3. 父群状态漂移和 session 竞争已经在前一版发布中修复；本轮没有再次出现 wrong-session，说明 session owner、租约续期和隔离路径已经生效。

## 架构落地顺序

1. 把 `message` 的未知写入核验做成独立高优先级队列，记录 oldest age、peer blocked count、reconciliation success rate；超过阈值只告警和降级普通消息，不自动重发。
2. 将读取预算改成按账号的 reservation ledger：监听、资格审核、加群对账、广告 survival 分别保留额度，超时自动回收；恢复期采用老化和公平调度，避免低优先级历史消息长期占用配额。
3. 为 79 条历史成功记录增加一次性安全收敛任务，条件只允许 `status=success` 且 membership 已离开或已有后续事实的记录进入 `resolved`，每批写审计事件并可回滚。
4. 将 `Group`、membership、qualification audit、ad profile 的可投放结论收敛到版本化状态机和 reconciliation 视图；把“35 个父群全部 active”变成持续不变量，而不是发布后人工查询。
5. 发布门禁继续使用不可变镜像 ID、单账号 canary、15 分钟日志窗口和业务验收；应用服务重建后必须等待 session lease TTL，再判定会话竞争是否持续。

本轮没有修改广告回执、预算计数、Redis lease 或事件 inbox 内容；生产只收敛了已证明为重复 reservation 的单条记录。

## 14:34 UTC 复核

增量镜像发布约两小时后再次检查，六个应用容器仍使用
`sha256:d3061ffc499461834f250d629ca3b6780eb305a753119ddee0e42cd59a2384da`，均为 running，重启次数为 0；`/health` 返回 `healthy`。
growth、guardian、backend、celery 最近 15 分钟的 wrong-session、Traceback 和
`AccountSessionLeaseBusy` 均为 0。

- 账号 2、33、34、35 仍全部 online；35 个目标父群仍为 `active`。
- 最近 24 小时广告为 success 38、failed 2、skipped 1；最近 48 小时 pending、unknown 和逾期 survival 均为 0。
- 最近 7 天加群为 success 108、failed 9、skipped 38、pending 2。两条真实 pending 仍是账号 34 的已发送请求（attempt 3402、3491），下一次对账已分别排到 2026-10-20 15:35 UTC 和 2026-10-21 01:35 UTC；没有未发送 reservation pending。
- 消息 inbox 的 45 条 pending 和 3 条 `reconciliation_required` 仍集中在账号 2、34，未发生自动重放。
- reconciliation 字段的历史脏数据仍存在：162 条 failed/released、1654 条 skipped/released、79 条 success/sent 的记录保留 `reconciliation_status=pending`。它们都不满足当前活动请求条件，不占用加群容量；后续应按审计证据分批收敛，不能直接批量改写。

这次复核没有执行数据库写入、容器重建或共享 PostgreSQL/Redis 操作。
