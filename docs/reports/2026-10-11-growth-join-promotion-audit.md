# 2026-10-11 加群与推广全面检查报告

截至北京时间 2026-10-11 17:00:15，生产服务在线，入群与广告发送链路能够执行；近期广告全部来自账号 2。账号 33、34、35 当前可投放群数均为 0，增加账号尚未形成新的推广产能。

产出统计以 2026-10-11 09:00:15 UTC 为统一截止，发布基准为当天 02:00:22 UTC（北京时间 10:00:22）。成功入群按 joined_at 计数，成功广告要求 status=success、sent_at 和正数 Telegram 消息号；排队、申请待批和本地预算跳过均不算成功。运行预算及回执补充核查到 09:11 UTC，频率准入复核到 09:13 UTC，分别保留各自采样时间。

## 真实产出与当前库存

| 账号 | 最近 24 小时确认入群 | 最近 24 小时广告日志确认成功 | 发布后确认入群 | 发布后广告确认成功 | 当前业务群关系 | 当前有效 qualified | 当前 usable |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 0 | 109 | 0 | 38 | 58 | 20 | 14 |
| 33 | 10 | 0 | 1 | 0 | 19 | 0 | 0 |
| 34 | 13 | 0 | 0 | 0 | 16 | 2 | 0 |
| 35 | 11 | 0 | 6 | 0 | 17 | 2 | 0 |

业务群关系使用容量接口口径，包括 joined、pending 和 leave_failed，排除 rejected。账号 2/33/34/35 当前保留的 joined 关系分别为 56/12/15/16；等待管理员批准的有效关系分别为 1/7/1/1。账号 4 仍为 ONLINE，但操作配置及自动加群、推广主动关闭，不计入本轮增长产能。

发布后 7 次入群均有 joined_at 且 reconciliation_status=confirmed，没有新的 Telegram 加群失败。账号 35 另有 5 次 telegram_read_budget 本地跳过，未发出 Telegram 请求。其最近 24 小时的 76 次本地失败全部发生在发布前，不能当作当前持续故障。

发布后 38 条广告全部由账号 2 发送，均有 Telegram 消息号。另有 1 次群级发送等待 479 秒导致失败、1 次账号发送间隔导致跳过。最近 24 小时广告日志为 109 success、1 pending、2 skipped、1 failed，成功发送覆盖 19 个不同群。账号 33、34 的历史最后成功分别为 10 月 10 日 03:59:50、05:05:41 UTC；账号 35 没有成功广告。

推广计划 1 为 active/enabled，四个账号各有一条启用绑定，均指向启用的文本素材 28。计划 2 未启用。四个增长账号均为 ONLINE、risk=normal、SpamBot 保存状态 clear，加群和推广开关已打开。搜索词为 794 个 approved/enabled、1904 个 discarded/disabled。

## 三个新账号为何没有广告

账号 33 原来的群 704 虽仍显示 approved/active，其当前资格审核已经 queued，需续审；历史试投日志 1522 的日核验也已到期，频率准入为 frequency_daily_review_due。因此当前有效 qualified=0、usable=0。该账号还有 7 个待管理员批准申请，不能当作已加入或可投放群。

账号 34 和 35 各有两个有效 qualified 群，但全部尚未满足首次广告暖机，usable=0。以下为北京时间最早暖机结束时间，届时仍须复核资格、群规、权限、读取预算和频率，不能保证到点发送：

| 账号 | 群记录 | 最早暖机结束 |
| ---: | ---: | --- |
| 34 | 738 | 2026-10-12 02:07 |
| 34 | 586 | 2026-10-12 09:18 |
| 35 | 581 | 2026-10-12 02:14 |
| 35 | 635 | 2026-10-12 11:02 |

账号 34 的旧试投群 794 还需要日核验和资格续审，其记录不能替代这两个新群的暖机。账号 35 其余新群主要为 initial_pending/review_2h，包含群规则材料不足、发言/验证限制和收费消息未授权等等待。

历史最新审核结果中，账号 33/34/35 分别有 11/16/10 条 no_ordinary_member_ad_48h 拒绝。账号 34 另有 3 条无权威群规证据拒绝。这些统计包含已退出的群，用于解释库存转化损耗，不能当作当前待审群数量。共享候选池也不等于每个账号各自可投放的库存。

17:00 容量快照的 active_backlog 为 8/9/10（账号 33/34/35），unresolved_reviews 为 14/7/11；当时三者加群准入均为 0。17:11 的读取快照显示，四个账号的 critical 读取可用数均为 0，而账号 2 的广告读取仍可用。这解释了广告继续发送、审核与新增加群暂缓可以同时发生。

| 账号 | critical 读取当前可用 | 当前预算窗口预计复核（北京时间） |
| ---: | ---: | --- |
| 2 | 0 | 10 月 11 日 22:44 |
| 33 | 0 | 10 月 11 日 21:22 |
| 34 | 0 | 10 月 11 日 21:26 |
| 35 | 0 | 10 月 11 日 21:29 |

这些是本地用途预算等待，不是本轮新的 Telegram FloodWait。不同读取用途仍有独立余量；总预算 state=ready 也不能推导 critical 可用。跨用途借出额度会降低当前保底余额，不能仅看表面广告额度 144 或读取总额 6000。到窗口复核后，应先处理账号各自到期的资格、验证和存活任务，再判断能否补群。

## 推广上限与存活口径

max_ads_per_day=30 和计划 max_sends_per_account_per_day=30 目前不是所有成熟广告的实际硬上限。四个账号均开启 dynamic_capacity_enabled 与 adaptive_ads_enabled：

- 账号试投成功预算为 30 条，使用本地日与滚动 24 小时双窗口；成熟广告独立记账。
- 当前成熟/全部广告容量由 600 秒发送间隔推导为 144 条/24 小时；两条通道仍共用账号发送节奏和总外发约束，不能把 30 与 144 相加。
- 自适应模式下，旧计划账号/群日上限检查明确让位于共享群频率和账号账本，不是时区统计错误或生产镜像漂移。北京时间日界线在 UTC 前一天 16:00，和 UTC 日期计数不同。
- 配置 max_messages_per_day=62 也会在该模式重算；当前生效总额度为 176（144 广告 + 30 验证 + 2 诊断），不是 62。
- 群频率跨账号共享；账号 2 当前 14 个 usable 成熟群的频次容量合计 377 条/日，实际产量还受 144 条账号上限、读取容量、群间隔和日核验限制。

关键生产源码与本地一致，既有机制文档及通过的测试也明确区分成熟与试投预算。不能因近 24 小时 109 条推广超过配置中的 30，就认定此次发生了配额绕过。

当前成功广告使用 daily 存活策略，日志 survival_status=not_required 表示没有逐条两分钟/一小时检测，不表示已证明每条广告存活 24 小时。过去 24 小时只有 5 个 daily_survived 事件，最新为 10 月 10 日 23:09:59 UTC。

频率复核中，账号 2 有 5 个群处于日核验 grace、3 个 retry、4 个 due；账号 33、34 各有 1 个旧试投群处于 due。grace 允许在本地读取/租约等待下最多继续原批准频率 24 小时，不会提频。retry 中群 459、636 为 frequency_daily_message_unconfirmed；在核实前保持禁发。频率视图同时包含待审核或非 usable 群，不能把这些数量直接当成当前广告目标数。

## 已确认的回执同步缺陷

广告日志 1647（账号 2、群 287、创建于 10 月 11 日 01:04:34 UTC）持续为 pending，消息号及 sent_at 为空；但对应 AccountOutboundAttempt 580 已为 succeeded，并保存消息号 6183、attempted_at 与 completed_at。账本上还保留 CancelledError 错误标记。

因此统一截止时，发送账本有 110 条 mature 成功，广告日志只有 109 条成功，两者差额就是这条已有精确回执却未回写的广告。它不是外发结果未知，不能重新发送。实际生产 FrequencyService.readiness 返回 frequency_group_inflight，使群 287 无确定恢复时间地禁发。

代码缺口在 send_receipts.py 的 recover_missing_receipts：候选要求未知发送/待回执标记，1647 为无错误的普通 pending，因此未进入恢复。另一条 _close_abandoned_pending 只处理无账本或有明确取消且未写入证明的 pending，同样不能处理已成功账本。

使用隔离 SQLite、模拟与生产相同的 pending + succeeded/message_id 状态复现：维护恢复数 0、关闭数 0、广告日志仍 pending，群准入为 frequency_group_inflight，没有 Telegram 调用。建议按原账号、预留标识、目标身份及精确消息号校验账本证据后补齐该日志与对应调度/日核验状态，并为该无标记场景补回归覆盖。

## 未决入群与转化数据

事件收件箱仍有唯一开放事件 926242：账号 2、kind=join、reconciliation_required、external_attempted=true、TimeoutError，收到于 10 月 9 日 21:46:05 UTC。原入群动作结果仍需真实 Telegram 成员/请求状态证据，不能自动重放或直接关闭。当前没有 pending/processing/executing 积压，也没有被其挡住的待执行业务事件。

Vanguard 的 acquisition_tracking 表总记录数为 0，点击、注册、转化和试用均没有记录；xboard_event 与 xboard_callback 也没有记录。正在使用的素材 28 为“点头像了解详情”，没有正文 URL 或 link_url。当前推广效果缺少可用的归因数据，无法按群或账号评价点击、注册、付费和收益。追踪记录为零不能据此证明实际无人注册或付费；这些下游指标没有在本次数据中得到观测。

## 运行健康、验证与处理顺序

后端、前端、增长 worker、Guardian、资源搜索、Celery、Redis 和共享 PostgreSQL 均运行，所查容器重启计数均为 0；后端 /health 返回 200/healthy。增长和 Guardian 心跳在线，last_error=null。

账号 33/34/35 监听保持 connected_wait，原始 journal pressure=false；账号 2 为 deferred，原因 telegram_read_budget，其长期监听等待恢复会暂时限制实时事件覆盖，不能用短会话发广告成功推导所有监听均已连接。会话请求队列多轮复查均为 0。09:00 数据库连接采样为 1 active + 6 idle，无长事务；该采样不等同于高峰并发压测。

02:05 UTC 至 09:08 UTC 的所查日志没有 telegram_worker_failed、数据库连接池耗尽、growth_listener_start_failed、session_exec_stage_failed 或 stage 超时。增长日志包含 2825 个 stage_started 和 2825 个 stage_completed，任务 completed 仅证明调度返回，不等于完成了入群或推广。

九个关键文件完成 SHA-256 比对，生产增长镜像与本地均一致：automation.py、outbound_budget.py、adaptive_frequency.py、growth_dispatch.py、pool.py、telegram_worker.py、send_receipts.py、capacity.py、ad_warmup.py。它不代表所有本地未提交文件都已发布。

定向测试执行 98 项，全部通过（28.41 秒）：test_adaptive_group_frequency.py、test_dynamic_outbound_capacity.py、test_ad_delivery_v2.py、test_growth_dispatch_fence.py、test_session_recovery.py。测试使用隔离 SQLite 和模拟外部调用，另有上述回执缺口隔离复现；没有把它们称为真实 Telegram 全链路验收。

建议处理顺序：

1. 修复 1647 精确回执回写遗漏，恢复群 287 的状态一致性；完成本地回归后再考虑发布和对已存在数据的修复。
2. 预算恢复后优先完成账号 33 群 704、账号 34 群 794 的日核验和资格续审，同时消化三个新账号的规则/验证审核。已 qualified 的新群等待真实暖机期限，不绕过准入。
3. 单独对账事件 926242；不重放已有结果未知的加群。
4. 如需评估推广效果，建立与当前头像引导路径匹配的点击/注册/付费归因，再比较账号及群的效果。

本轮只读生产检查：没有改数据库业务状态或账号配置，没有调用 Telegram 写接口、清空队列、重启/重建服务、部署或提交 Git。共享 PostgreSQL、Redis、应用授权设置和既有工作树修改均保留。

## 脱敏证据

- [统一产出及运行/库存快照](../../deploy-artifacts/session-recovery-20261011/runtime-current-20261011-0855.json)
- [预算、精确回执、转化与暖机补充快照](../../deploy-artifacts/growth-join-promotion-audit-20261011/supplement.json)
- [实际群频率准入快照](../../deploy-artifacts/growth-join-promotion-audit-20261011/readiness.json)
- [生产/本地代码指纹](../../deploy-artifacts/growth-join-promotion-audit-20261011/source-comparison.json)
- [容器与日志汇总](../../deploy-artifacts/growth-join-promotion-audit-20261011/host.json)
- [回执缺口隔离复现](../../deploy-artifacts/growth-join-promotion-audit-20261011/receipt-gap-reproduction.json)
- [只读补充采集脚本](../../deploy-artifacts/growth-join-promotion-audit-20261011/supplement.py)
- [隔离复现脚本](../../deploy-artifacts/growth-join-promotion-audit-20261011/reproduce_receipt_gap.py)
