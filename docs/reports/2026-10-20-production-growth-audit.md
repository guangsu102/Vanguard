# 2026-10-20 生产加群与广告健康检查

检查时间：2026-10-20 01:44–01:59 UTC（09:44–09:59 北京时间）。本轮数据库查询使用只读事务，未修改生产数据库、Redis 或容器。

结论：生产链路部分工作，但当前不正常。4 个推广账号仍在线，广告仍有成功发送；加群和广告的可用库存被会话错误、账号租约竞争、读取预算以及父群状态阻断。

## 当前状态

| 项目 | 结果 |
| --- | --- |
| 运行容器 | backend、celery、growth、guardian、resource-search、frontend、Redis 均在运行；backend/frontend/Redis 健康检查正常 |
| 账号 | 2、33、34、35 均 `ONLINE`、启用自动加群和自动广告 |
| 加群成功 | 最近 24 小时 9 条（33/34/35 各 3 条）；最近 7 天 97 条 |
| 加群异常 | 最近 7 天 17 条 `telegram_read_budget` 跳过、10 条 `join_wait_execution_capacity` 跳过、6 条 `join_failed`；账号 34 仍有 2 条待确认申请，其中 1 条超过 3 天 |
| 广告成功 | 最近 24 小时 26 条（2:7、33:1、34:11、35:7） |
| 广告未收敛 | 账号 34 有 1 条 `pending`，另有 1 条 `send_abandoned_before_attempt:worker_crash`；当前没有重复投递，48 小时内 63 条成功记录均有消息回执和 outbound 成功记录 |
| 存活核验 | 没有逾期的 survival pending |
| 事件积压 | `listener_fact` pending 7948 条，最早约 2 小时；`message` pending 158 条，最早超过 1 天；另有 3 条 `reconciliation_required` |

## 具体根因

### 1. 审核通过后父群仍是 `pending`

当前生产有 **35 个**候选父群：父群状态为 `pending`，账号成员已 `joined`，`review_status=approved`、`ad_status=active`，最新有效审核为 `completed` 且决策为 `trial`。按账号分布为 2:10、33:4、34:12、35:9；审核依据为普通成员广告验证 21 个、自有广告存活 14 个。完整列表见 [候选 JSON](2026-10-20-production-pending-reactivation.json)。

广告发送门禁仍读取父群状态，`pending` 会把这些目标视为不可用；因此数据库里的成员资格已经通过，调度层却无法把它们当作正常广告目标。生产旧代码在审核通过分支只更新 membership 和 ad profile，没有把父群恢复为 `active`。

### 2. 账号 2 的 Telethon 会话持续出现 wrong session ID

最近 24 小时 growth worker 日志中出现约 **13,468** 次 `Server replied with a wrong session ID`，集中在账号 2。旧的 update 接收路径只记录安全错误并继续接收，RPC 守卫因此看不到这个失败，旧连接和新连接会继续竞争同一个授权会话。当前账号 2 的 ad/read 等普通预算均被暂停至约 03:14 UTC，导致它虽然数据库状态为 `ONLINE`，实际增长工作无法继续。

### 3. 会话租约竞争阻断审核、对账和发送

生产日志反复出现 `Telegram session lease is busy`：账号 34 的 `join_request_reconciliation`、`group_qualification`、`ad_delivery` 被跳过，账号 35 的 `join_candidate_preview` 也被跳过。账号 34 的待确认申请 3491 因 `AccountSessionLeaseBusy` 多次无法核实，审核记录出现 `technical_wait`；这会进一步延迟父群从审核状态进入可发送状态。

### 4. 本地读取预算和执行容量是加群吞吐的硬门槛

`telegram_read_budget` 与 `join_wait_execution_capacity` 都是在发起 Telegram RPC 前的本地门禁，不代表 Telegram 已经拒绝请求。它们解释了近 7 天的 27 条跳过记录，以及账号 2、33 当前的刷新暂停。预算恢复后仍需要先处理会话隔离和审核积压，否则容量会继续被旧任务占用。

### 5. 事件积压会延迟资格刷新

`listener_fact` 和 `message` inbox 的 pending 数量较大，审核队列当前仍有 52 条已到期记录（账号 2:34、33:10、34:7、35:1）。这会让已加入群组的资格和广告策略更新晚于实际 Telegram 状态。

## 本地修复候选

已在工作区完成并通过针对性回归测试：

- [session_guard.py](../../backend/app/core/account/session_guard.py)：识别 update 接收线程的 `wrong session ID`，通过现有 `AccountSessionGuard` 隔离并断开失效连接。
- [pool.py](../../backend/app/core/account/pool.py)：创建 Telethon client 后安装 session failure hook，并在失败路径释放租约。
- [qualification_service.py](../../backend/app/modules/acquisition/qualification_service.py)：审核决策为 `allowed/trial` 且 membership 已加入时，把父群状态回写为 `active`。
- 新增回归覆盖 [test_session_guard.py](../../backend/tests/test_session_guard.py) 和 [test_qualification_service.py](../../backend/tests/unit/test_qualification_service.py)。

验证结果：资格服务与 session guard 76 项测试通过；Telegram worker、RPC governor、listener journal/pause 54 项测试通过；`compileall` 通过；针对本次文件运行 `ruff --select E9,F` 通过。完整 `ruff check` 仍会报告工作区已有的 46 个旧式 Optional/identity 风格问题，本轮没有自动改动这些无关代码。

本报告与候选 JSON 只记录检查结果；尚未部署代码，也尚未把 35 个父群写回 `active`。
