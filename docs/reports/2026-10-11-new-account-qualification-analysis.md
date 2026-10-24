# 账号 33、34 广告资格为零的逐群分析

**结论：当前 qualified=0 的直接原因是投放入口的复合门禁，不能解释为全部群规审核失败。已获得试投授权的群被事件复核挂起，复核又缺少读取预算；其余群分别缺少广告许可证据、AI 未确认、存在发言限制或取证失败。广告暖机已经是 0 天。**

生产最终复查时间：北京时间 **2026-10-11 20:03:38**。逐群证据采集于 19:48:00，AI/取证细节及实际预算准入复现于 19:55:45；最终复查确认 27 个已加入群的审核 ID、decision、reason 均未改变。以下群 ID 为项目数据库 ID。

本轮通过 `ssh oracle4c24g` 读取生产 PostgreSQL、Redis 预算及应用源码。数据库使用只读事务，容量与预算读取使用禁止写入/锁定的 `CapacityReads`；没有连接 Telegram、修改审核或预算、发送广告、部署或重启服务。15 个相关源码文件与本地 SHA-256 一致。

## 当前账号与统计口径

| 指标 | 账号 33 | 账号 34 |
| --- | ---: | ---: |
| 状态 / 风险 | online / normal | online / normal |
| 自动广告开关 | 开启 | 开启 |
| 广告暖机阶段 / 天数 / 剩余天数 | normal / 0 / 0 | normal / 0 / 0 |
| 当前已加入群 | 12 | 15 |
| 另有待管理员批准申请（19:48 采样） | 7 | 1 |
| 投放入口 qualified / usable | 0 / 0 | 0 / 0 |
| 已加入群的统一入口提示 | qualification_review_required × 12 | qualification_review_required × 15 |

qualified 使用可进入当前投放链路的授权口径：除了历史审核决定，还检查 audit 状态、会员审核状态、广告状态、授权作用域、有效性与未处理事件。`quality_status=qualified` 只表示某次群质量/权限检查通过，不代表广告许可也已批准。`joined` 同样不保证已经解除新人验证或禁言。

## 最优先恢复的群：原授权有效，事件复核未完成

| 账号 | 群 ID | 审核 ID | 会员状态 / 广告状态 | 审核状态 / 原决定 | 授权有效、作用域匹配 | 待处理事件 | 复核最低读取 |
| ---: | ---: | ---: | --- | --- | --- | --- | ---: |
| 33 | 704 | 718 | approved / active | queued / trial | 是 | gap、rules | 16 |
| 34 | 738 | 755 | approved / active | queued / trial | 是 | gap | 16 |
| 34 | 586 | 773 | approved / active | queued / trial | 是 | gap | 16 |
| 34 | 794 | 723 | initial_pending / warming | queued / trial | 是 | gap、rules | 16 |

这四条原决定都是 `ordinary_member_ads_verified`，广告证据置信度为 100，审核快照的文字发送权限为 true，且实际轻量复核资格检查 `light_renewal_eligible=true`。其中 794 的会员审核状态尚未恢复，不能把它与另外三个 approved 群等同为已具备当前投放条件。

生产源码中的路径是：

1. 监听客户端换连接实例时，`telegram_worker.py:742` 调用 `account_gap`，产生新的账号连续性代次。这个 gap 标记不等于已证实群规禁止广告。
2. `qualification_events.py:128` 的 `queue_due_gaps` 为受影响的批准群排队，并在 `:184` 将 audit 设为 queued；rules 表示待复核的群规相关材料事件，同样不是禁止广告的最终结论。
3. `qualification_service.py:2394` 要求轻量复核一次性至少预留 16 次读取；有待处理事件时需要实际取证，不能直接沿用本地旧快照结束复核。
4. 预算不足时 `:2404` 附近只保存等待和下一次重试，已经 queued 的记录保持 queued。
5. `automation.py:9174` 附近先检查 audit/会员/广告状态。queued 立即返回 `qualification_review_required`；通过这一层后仍会检查 pending 事件。因此即使原授权有效，也不会进入广告发送。

时间证据也说明两种情况不同：33 的 704 首次预算等待在 **12:41:01**，34 的 794 在 **09:17:24**，均早于本次 0 天暖机发布。34 的 738、586 在 **19:29:19** 开始记录复核预算等待，发生在发布后的监听恢复阶段。不能把全部挂起归因于暖机变更。

## 为什么“总读取还有余额”，复核仍不执行

以下使用 20:03:38 的最终预算快照：

| 读取预算指标 | 账号 33 | 账号 34 |
| --- | ---: | ---: |
| 全日窗口上限 | 6000 | 6000 |
| 全日窗口已使用 | 5818 | 5872 |
| 全局剩余 | 182 | 128 |
| 广告日份额已经借出给其他用途 | 2043 | 1412 |
| 广告用途日读取已用 | 34 | 665 |
| 当前广告读取可用 | 23 | 23 |
| 普通资格审核 critical 可用 | 0 | 0 |
| 广告授权复核 ad_refresh 可用 | **11** | **11** |
| 一次轻量复核最低预留 | **16** | **16** |
| 当前预算窗口预计重新准入 | 21:22:55 | 21:26:26 |

关键关系为 **23 次广告读取余量 − 12 次发送保留 = 11 次复核余量 < 16 次最低复核预留**。全局余额、用途余额和发送保留是不同约束，不能把全局剩余 182/128 全部用于广告复核。已经借出的额度在窗口到期前仍按借用记账。

`rpc_budget_policy.py:9`、`:45` 将 `ad_qualification_refresh` 正确归入广告用途；`critical_fairness.py:67` 使用更细的 ad_refresh 准入。因此这里不是复核被错误地当成普通 critical 读取。`demand_lending.py:58` 保留至少两次发送及缓冲，在当前剩余量下无法同时容纳一次完整复核。

最终直接调用实际只读 `check_read_ready`，两账号的普通审核（最低 1 次读取）和授权复核（最低 16 次读取）均返回 **telegram_read_budget**，对应等待 4756 秒、4967 秒。这是项目本地预算门禁；本次复现没有发出 Telegram RPC，也没有出现新的 Telegram FloodWait。

这形成了本窗口内的产能阻塞：已有授权需要读取才能恢复，但已借出的额度与发送保留使复核读量不足。21:22/21:26 是预算重新评估时间，不是确定广告发送时间；到时仍需事件复核、会员状态、频率及执行会话条件通过。

## 所有已加入群的原因分布

每个群按当前首要原因归类一次，合计 27 个；不包含待批准申请和已退出群。预算阻挡可能同时影响多个类别，不在下面重复计数。

| 当前首要原因 | 账号 33（12 个） | 账号 34（15 个） |
| --- | ---: | ---: |
| 曾获得 trial，复核/会员状态未恢复 | 1 | 3 |
| AI 未确认广告许可或缺少权威群规材料 | 4 | 6 |
| 群规覆盖/取证材料尚不完整 | 5 | 1 |
| 验证、禁言或临时发言限制 | 0 | 5 |
| Telegram 取证技术失败 | 2 | 0 |

### 账号 33 的 12 个群

| 群 ID | 审核 ID | 当前决定 / 原因 | 具体解释 |
| ---: | ---: | --- | --- |
| 704 | 718 | trial / ordinary_member_ads_verified | 原授权有效；gap、rules 复核排队，需 16 读，当前只有 11 |
| 660 | 680 | observe / qualification_evidence_incomplete | 群规覆盖不完整；权限允许文字，广告许可未建立；补证需 22 读 |
| 598 | 683 | observe / qualification_evidence_incomplete | 群规覆盖不完整；补证需 22 读 |
| 44 | 701 | observe / qualification_evidence_incomplete | 群规覆盖不完整；补证需 24 读，另有 3600 秒慢速模式 |
| 610 | 708 | observe / qualification_evidence_incomplete | 群规覆盖不完整；补证需 24 读 |
| 697 | 725 | observe / rules_coverage_incomplete | 群规证据仍不完整；补证需 22 读，另有 3600 秒慢速模式 |
| 638 | 690 | reject / group_rules_ai_unresolved_conflict | AI unknown、置信度 60、conflict=true；没有确认适用的明确禁令 |
| 643 | 721 | reject / group_rules_ai_unresolved_conflict | 同上；记录虽有 1 个近期普通成员广告样本，trial_history_sufficient=false |
| 800 | 747 | reject / group_rules_ai_unresolved_conflict | AI unknown、置信度 60、conflict=true；新证据取证也受预算限制 |
| 658 | 735 | reject / group_rules_ai_consensus_failed | AI unknown、置信度 60，conflict=false；未达到许可判定阈值 |
| 605 | 695 | technical_wait / evidence_read_failed | 群资料、历史、置顶、自身权限读取均为 ChannelPrivateError；需重新确认真实访问能力 |
| 574 | 751 | technical_wait / evidence_read_failed | 采集 TimeoutError；现有技术恢复窗口已耗尽，需新证据触发 |

605、574 均记录 `technical_recovery_exhausted=true`、`review_trigger=new_evidence_required`，当前 `next_retry_at` 为空。它们不会仅因为日预算恢复就自动完成审核。

### 账号 34 的 15 个群

| 群 ID | 审核 ID | 当前决定 / 原因 | 具体解释 |
| ---: | ---: | --- | --- |
| 738 | 755 | trial / ordinary_member_ads_verified | 原授权有效、approved/active；gap 复核排队，需 16 读，当前只有 11 |
| 586 | 773 | trial / ordinary_member_ads_verified | 与 738 同类；先前广告失败是账号暂不可用，当前最先被资格复核挡住 |
| 794 | 723 | trial / ordinary_member_ads_verified | 原授权有效；gap、rules 复核排队，会员仍 initial_pending/warming |
| 670 | 706 | reject / group_rules_ai_unresolved_conflict | AI unknown、置信度 60、conflict=true；未证实明确禁令 |
| 628 | 739 | reject / group_rules_ai_unresolved_conflict | 同上 |
| 603 | 771 | reject / group_rules_ai_unavailable | AI 本次不可用、置信度 0、无成功 review；fail 已落入最终记录 |
| 575 | 720 | reject / group_rules_no_authoritative_evidence | 缺少可作为广告许可依据的权威群规材料 |
| 582 | 733 | reject / group_rules_no_authoritative_evidence | 同上 |
| 571 | 736 | reject / group_rules_no_authoritative_evidence | 同上；旧预算等待记录不表示当前已有自动批准 |
| 567 | 710 | observe / qualification_evidence_incomplete | 群规覆盖不完整；补证需 23 读 |
| 617 | 679 | observe / verification_or_send_restriction | 审核记录 can_send_text=false、verification_pending=true |
| 723 | 700 | observe / verification_or_send_restriction | 同上 |
| 720 | 726 | observe / verification_or_send_restriction | 同上 |
| 691 | 741 | observe / verification_or_send_restriction | 同上 |
| 711 | 716 | wait / temporary_send_restriction | 同样 cannot-send-text/待验证，记录为临时发言限制 |

这些权限来自审核取证记录，本轮没有再次访问 Telegram 测试发言或验证。它们不是广告暖机天数；需要真实验证完成、权限解除后再审核。

## AI 拒绝和取证不足为何容易混淆

生产 AI 策略启用，模型为 `glm-5.3-flash`，最低置信度 **95**。上述 6 个冲突/共识失败群都是 unknown/60；5 个记录 conflict=true，658 的 conflict=false。其 `applicable_prohibition=false`，所以当前证据支持“系统无法明确批准”，不支持“这些群都明确禁止广告”。603 则没有成功模型结果。

`qualification_ai.py:179` 的 `binary_result` 要求明确允许、允许类策略、置信度达到阈值，任何未通过情况都映射为 `ad_allowed=false`。同一语义材料采用一次识别缓存；相关快照已有 `ai_final=true`、`ai_decision=fail`。因此恢复预算不保证这类 reject 自动变为批准，需要新增、补齐或改变实际许可证据，或另行设计失败重试机制。

取证方面，多个群在约 300 条独立消息后因 `identity_read_slice_exhausted` 停止，缺少普通成员身份或广告先例的完整确认。看到推广消息并不自动满足普通成员、至少 24 小时留存、规则适用范围等试投要求。未读完整也不能推断为“48 小时没人发广告”。

另一个实现差异：策略配置 `ad_policy_ai_require_second_pass=true`，但本次核对的当前识别路径 `automation.py:5812` 只保存 `reviews=[first]`。现有 `consensus_failed` 名称不能据此解读成两个模型互相矛盾；658 在当前单次结果中就已未满足阈值和许可模式。这是后续配置/实现一致性检查项，不是四条有效 trial 被预算挂起的直接原因。

## 19:27 的广告尝试与当前阻挡

0 天暖机在 **19:27:31** 生效。34 对 738 的广告日志 1707 在 19:27:53 创建，失败为会话租约占用；对 586 的日志 1708 在 19:27:54 创建，失败为 `transient:account unavailable`。这说明当时确实进入了广告尝试，不能将这两个失败解释成群规审核拒绝。

20:03 的最终复查仍没有账号 33、34 在本次暖机发布后的成功广告记录。账号 35 在 **19:29:10** 对群 581 成功发送，日志 1711，Telegram 消息号 1230844，可作为 0 天暖机和广告执行链路已实际生效的对照；不能据此推导所有新账号和所有群均可用。

## 建议处理顺序

1. 优先解决 704、738、586 的复核预算供给，完成受影响事件的真实轻量复核并确认会员状态；794 同时恢复会员审核。改进方向是保证一次完整复核读量，或按实际分段读量保留进度，不能仅修改 queued/approved 字段绕过事件。
2. 对 704、794 的旧试投日核验重新检查。17:00 审计已发现它们日核验到期；本轮资格复查没有将其改成可用，也没有重新声称日核验已经通过。
3. 对 10 个 AI/权威材料未通过群和 6 个材料不完整群补齐可核验许可证据；单独处理 AI 不可用被缓存为终态的恢复行为。不能通过统一调低置信度或批量批准解决。
4. 对 34 的 5 个限制群完成实际验证/解除权限；对 33 的 605、574 核实访问与采集后用新证据恢复审核。
5. 将投放入口通用的 qualification_review_required 拆成“有效授权待事件复核、取证预算等待、材料不足、AI 未确认、发言受限、技术恢复待新证据”，避免再次把不同原因统称为没有资格。

本报告提供根因及恢复方向，本轮没有更改线上资格、读取预算或广告策略。

## 脱敏采集文件

- [逐群状态和原始有效审核摘要](../../deploy-artifacts/new-account-qualification-analysis-20261011/current.json)
- [AI、取证细节和实际只读准入复现](../../deploy-artifacts/new-account-qualification-analysis-20261011/deep-evidence.json)
- [20:03 最终状态、暖机、预算准入和广告日志](../../deploy-artifacts/new-account-qualification-analysis-20261011/refreshed-summary.json)
- [补充生产源码指纹](../../deploy-artifacts/new-account-qualification-analysis-20261011/additional-source-hashes.json)
- [逐群只读采集脚本](../../deploy-artifacts/new-account-qualification-analysis-20261011/collect.py)
- [深度只读采集脚本](../../deploy-artifacts/new-account-qualification-analysis-20261011/deep_evidence.py)
- [最终只读复查脚本](../../deploy-artifacts/new-account-qualification-analysis-20261011/refresh_summary.py)
