# 所有推广账号广告状态复查（2026-10-12）

北京时间 2026-10-12 11:32～11:34，只读检查生产 `vanguard` 数据库、Redis 调度、增长 worker 日志及公网健康状态。数据库事务使用 `SET TRANSACTION READ ONLY` 和 15 秒 statement timeout；没有触发 Telegram 发送或修改生产数据。

当前启用广告的账号为 2、33、34、35，四者均有实际成功发送回执；账号 4 的自动广告、账号运行配置和广告绑定关闭。不能据此认定所有群都可立即投放：新账号的可用群均处于已有频率窗口，账号 2 有部分读预算和日核验阻挡，账号 34 的一个群出现明确删广告事件。

| 账号 | 近 24 小时成功回执 | 最近成功（北京时间） | 合格 / 可用群 | 当前广告状态 |
| --- | ---: | --- | --- | --- |
| 2 | 111 | 10-12 11:32:12 | 21 / 14 | 仍在发送，部分群日核验受读预算阻挡 |
| 4 | 0 | 09-21 09:02:29 | 2 / 2 | 自动广告关闭，资格 rollout 暂停，无启用绑定 |
| 33 | 1 | 10-12 07:41:01 | 1 / 1 | 已实际发送，群 704 等待间隔窗口至 19:41:01 |
| 34 | 4 | 10-12 11:18:05 | 3 / 3 | 可发送；11:18 的群 711 广告随即被删除，该群已停止投放 |
| 35 | 4 | 10-12 09:04:20 | 4 / 4 | 已实际发送，现有群等待每日频次窗口，最早 19:29:10 |

成功回执按 `status=success` 且 `telegram_message_id` 非空计算；包含之后被删的发送。`usable` 包含未来的频率槽位，并不代表检查当刻可以发送。频率窗口结束后仍须完成相应资格与留存检查，不保证在该秒发送。

33 / 34 / 35 的 `ad_refresh` 剩余分别为 121 / 77 / 126 读，均通过 16 读刷新准入。暖机策略 default/minimum 均为 0 天。账号 2 的广告读通道尚有 198 读，但 `ad_refresh` 和审核通道为 0，预算恢复时间为当天 22:44:36；这会影响需要重新取证和日核验的群，已有可用目标仍能发送。33 的 12 群、34 的 11 群、35 的 15 群尚未通过完整广告资格，不应等同于账号完全不可发广告。

账号 34 最新成功日志 1795，群本地 ID 711，Telegram 回执 27162004。发送后 0.216 秒收到 `telegram_delete_update`；日志 `survival_status=deleted`，群频率状态为 `exit_pending / frequency_probe_deleted`。删除事实明确，现有证据没有说明删除者身份。

owner 公平性发布于 10:35:40 后，账号 2 有 3 次成功、7 次 failed；这 7 次集中于 10:36:05～10:36:13，原因为 session lease busy 和 account unavailable。最后一次发送失败之后已经出现 10:37、11:04、11:32 成功回执。33 / 35 最近回执早于这次 owner 发布，发布后暂无新回执；它们的广告阶段现已 `completed`，当前目标的频率等待也已核对，不能把 `completed` 当作新发送成功。

公网 `/health=healthy`，增长和守护 worker 心跳 online、last_error 为空；Redis 广告 eligible=4、waiting=0、outstanding=0，session request 队列=0，owner parallel stages 为 review 和 ads。增长 worker 最近 30 分钟 `session_owner_capacity_wait`、owner loop/stage failure、stage timeout 和 error 级别日志均为 0。

证据保存在 `deploy-artifacts/ad-all-account-check-20261012/readonly-current.json` 和 `receipt-runtime.json`。全部时间说明使用北京时间；证据中的 naive datetime 是业务 UTC，统计窗口显式使用 `now() AT TIME ZONE 'UTC'`，避免数据库连接时区影响结果。
