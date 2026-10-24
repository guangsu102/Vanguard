# 广告与审核执行修复验收（2026-10-10）

## 修复内容

- 新的 `gap`、`rules`、`membership` 事件会唤醒已处于正常续审周期的审核记录。14 天维护排程不再遮蔽新事件；预算、FloodWait、权限和异常退避仍保留原定时器。
- 本地恢复任务会独立发现待处理事件，不依赖已有广告票或预算等待票。
- owner 执行器给每个阶段增加总期限，阶段超时会取消并释放执行槽。已尝试的 Telegram 写操作保留待对账，未尝试的广告记录才会按明确的取消证据清理。
- 共享连接池下审核允许不同账号最多并行两个阶段；广告、加群、退出和审核等 Telegram 阶段仍由每个账号的 account fence 串行，同一账号不并发操作同一会话。

## 验证

- 本地回归：`189 passed`；补充执行边界和账本回归：`115 passed, 1 skipped`；单独执行 Telegram/回执回归：`115 passed, 1 skipped`。Ruff 关键错误检查通过。
- 隔离 PostgreSQL 18 + Redis 回归：第三次运行通过，`333 passed`，并确认 `22` 项真实 PG/Redis 检查未跳过，包含新增 owner/连接池集成检查；前两次仅修正了测试夹具的缺表和 PostgreSQL 外连接锁范围后重跑。
- 生产发布使用 canonical Compose `/opt/shared-postgres/stacks/vanguard/compose.json`，更新 6 个应用服务。备份：`/opt/vanguard/backups/ad-execution-repair/20261010T025627Z`。共享 PG、Redis 和 35 个无关容器保持不变。

## 线上结果

发布后约 20 分钟的只读采集：

- backend 健康检查 HTTP 200；6 个应用服务运行中；PG 和 Redis 未重启。
- owner 阶段日志：34 次开始、34 次完成，未出现阶段总期限超时或数据库连接池耗尽。
- 账号 33、35、34 各有成功入群记录（新记录分别为 3124、3126、3127）。账号 34 首次因已有 account operation lease 返回 busy，随后自动重试成功。
- 账号 2 新增广告成功记录 `ad_delivery_log.id=1520`，Telegram `message_id=3442318`。
- 账号 33、34、35 当前合格可投放群仍为 0，因此它们没有广告成功记录。它们的审核/权限证据仍需完成，不能把入群成功或排程当成广告成功。
- 未知外部动作 `telegram_event_inbox.id=926242` 仍保留待对账，系统没有自动重发。

证据目录：`deploy-artifacts/ad-execution-repair-20261010/`，其中 `live-after3.json`、`host-after2.json` 和发布状态文件为只读验收结果。
