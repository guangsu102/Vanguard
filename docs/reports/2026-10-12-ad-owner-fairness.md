# 广告 owner 调度公平性修复（2026-10-12）

## 问题

Telegram growth worker 的 session owner 只允许 `review` 阶段并行。两个审核任务同时运行时，广告请求会被直接回复 `session_owner_capacity_wait`；审核任务持续刷新时，新账号即使已经有有效广告授权，也会一直拿不到发送执行槽位。

## 修复

- `review` 和 `ads` 可以在 owner 内并行运行；同账号仍由 owner account lock 串行。
- 广告请求进入有界本地等待队列，owner 槽位释放后优先于新审核请求执行。
- 保留广告发送已有的群级 advisory lock、资格、频率、额度和 Telegram 回执校验。
- 调度快照新增 `session_owner_queued_stages=["ads"]`，可直接观察该保护策略。

## 验证

- `backend/tests/unit/test_session_exec.py`：22 passed。
- 广告/资格相关定向集合：68 passed，2 个隔离 PostgreSQL 用例因本机未提供 `GROWTH_TEST_PG_SOCKET` 跳过。
- 新回归覆盖“两条审核持续运行时广告等待，首个槽位释放后广告先执行”。
- 生产 canonical Compose 发布六个 Vanguard 应用服务，使用 `/opt/shared-postgres/stacks/vanguard/compose.json`，共享 PostgreSQL、Redis 和其他容器未重启；发布健康检查返回 `healthy`。
- 发布后 owner 代码指纹与候选一致；生产调度快照显示 `parallel_stages=["review", "ads"]`、`queued_stages=["ads"]`、session request 队列为 0。账号 33、34、35 的广告阶段均已出现 `completed` owner 运行记录，未再出现 `session_owner_capacity_wait`。

发布证据保存在 [ad-owner-fairness-20261012](../../deploy-artifacts/ad-owner-fairness-20261012/)；服务器备份目录为 `/opt/vanguard/backups/session-owner-ad-fairness/20261012t023312z`。
