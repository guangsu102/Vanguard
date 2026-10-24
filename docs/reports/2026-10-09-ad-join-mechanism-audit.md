# 2026-10-09 加群与广告机制核查

结论：当前版本仍有一处会漏派发审核的核心调度缺口，以及库存资格判断、重试截止和加群原因分类三类问题。额度窗口到期只能解除读取额度等待，不能保证这些机制缺口自动消失。

生产采样时间为北京时间 20:58:34 和 21:08:03。核查采用只读数据库快照、运行镜像源码比对与隔离 SQLite 复现。10 个关键源码文件经换行规范化后的 SHA-256 均与生产一致。诊断脚本的 14 个场景断言全部通过，其中包含正常对照及错误行为复现；诊断结果用于确认缺口。

本轮产物为核查报告与复现证据，四类新发现尚未修改应用实现或部署。

## P1：外层派发与审核执行器的到期条件不一致

`growth_dispatch.account_sets()` 仅识别 queued、到期 running、到期 completed unknown/technical_wait。审核执行器 `_run_reviews()` 还接受到期 waiting_ai、completed observe/reject、completed allowed/trial。

隔离数据库中，只保留一个对应审核时，执行器实际 SQL 将其判为到期，外层调度却返回空 review 账号集合。已复现等待 AI、观察/拒绝重试、trial/allowed 续审五种状态。生产已关闭旧的全局每分钟审核任务，因此不能依靠另一个周期任务补救。

身份刷新和缺失审核补建也位于 `_run_reviews()` 内。仅存在账号名单变化时，外层不会创建审核执行机会；即使在隔离数据库中直接调用身份刷新，刷新写出的 completed/trial 到期行仍不被外层识别。若一个账号没有其他可被识别的审核，恢复会依赖无关任务再次出现。缺失审核补建同样存在这个入口依赖。

线上 21:08 的待办包括 23 份启用广告、等待事件复核的 trial 审核，以及 6 份有未来重试时间的 observe 审核。这些类型均在上述漏识别范围内。当前还有一份 queued/reject 审核，重试时间为 23:33:01；它被无条件识别为待办，使账号 2 仍获得 review 执行机会，暂时掩盖了漏调度缺口。广告执行前也可安排部分到期 gap，但它只覆盖部分获准广告关系，不能承担全部审核恢复入口。

代码：

- [外层待办筛选](E:/project/Vanguard/backend/app/core/scheduler/growth_dispatch.py:224)
- [执行器审核到期条件](E:/project/Vanguard/backend/app/modules/acquisition/qualification_service.py:2076)
- [身份刷新调用入口](E:/project/Vanguard/backend/app/modules/acquisition/qualification_service.py:1993)
- [旧审核周期任务被移除](E:/project/Vanguard/backend/app/celery.py:324)

修复应让派发与执行共用审核到期条件，并为身份变化和缺失审核提供可被独立发现的入口。继续保留账号风险、人工暂停、退出范围和真实证据检查。

## P2：库存统计与发送端的系统身份检查不一致

`capacity.inventory_snapshot()` 检查审核状态、有效期、事件、频率和群身份，但没有执行发送端的系统账号身份指纹校验。发送端 `_qualified_ad_context()` 会校验当前系统身份覆盖、指纹及排除身份集合。

隔离复现：删除一个管理账号后，旧证据指纹失配；库存仍返回 qualified=1、usable=1、identity_blocked=0，实际发送上下文返回 qualification_system_identity_unconfirmed。

20:58 生产快照也出现此差异：管理推广账号由上一次的 6 个变为 4 个，当前身份确认数暂时降为 0，但库存仍报 qualified=3、usable=3、identity_blocked=0。21:08 的再次采样显示这 3 份审核已完成本次名单缩减校验，身份确认恢复为 3，其余 23 份仍需事件复核。此恢复说明当前存在审核执行机会，不消除两套资格判断不一致的问题。

发送端仍保留拦截；问题影响库存展示、容量预测和加群补充判断，未发现因此绕过发送授权。

代码：

- [库存增加获准计数](E:/project/Vanguard/backend/app/modules/acquisition/capacity.py:116)
- [库存增加可用计数](E:/project/Vanguard/backend/app/modules/acquisition/capacity.py:143)
- [发送端系统身份校验](E:/project/Vanguard/backend/app/modules/acquisition/automation.py:9144)

修复应共享系统身份检查，并明确计入待修复的资格依赖，避免把待修复授权统计为当前可发送库存。

## P2：已知重试时间没有完整接入派发

外层审核筛选对 queued 不检查 next_retry_at，导致未来任务也持续获得执行机会。隔离复现：queued 审核的重试时间在一小时后，执行器到期 SQL 返回 false，外层仍派发 review。

广告执行已返回 account_ad_resource_pacing 及 next_allowed_at，但 `growth_dispatch.execute()` 仍按 dispatcher_interval_seconds 返回重试间隔，`run_ticket()` 按这个间隔设置下次派发。隔离复现：执行结果明确等待 3600 秒，下一次派发仍为 60 秒。线上最新广告任务也持续以该原因处理 0 条、跳过 1 条。

这种空转占用数据库和账号执行机会，增加 account_operation_lease_busy 退让；本次未证明这些空转额外消耗 Telegram 读取，也未发现永久残留锁的证据。

代码：

- [queued 无条件进入待办](E:/project/Vanguard/backend/app/core/scheduler/growth_dispatch.py:229)
- [广告固定派发间隔](E:/project/Vanguard/backend/app/core/scheduler/growth_dispatch.py:431)
- [派发重试截止写回](E:/project/Vanguard/backend/app/core/scheduler/growth_dispatch.py:494)

修复应遵循已知到期时间，同时保留额度变化、事件变化和过期租约恢复所需的有限唤醒机会。

## P2：加群额度不足被误报为库存待审核

`growth_admission.admission_plan()` 在 read_slots 为 0 时统一返回 join_wait_inventory_review，没有区分原始读取容量不足与已有审核债务占满读取容量。

线上实测与隔离复现均为 available_reads=0、review_reads_due=0、pending_reviews=0、execution.remaining=4，却返回等待库存审核。该分类不能准确解释当前加群停止原因。

代码：[准入原因分类](E:/project/Vanguard/backend/app/modules/acquisition/growth_admission.py:107)。

修复应分别返回读取额度不足、现有审核债务背压和全局执行容量不足，并保存对应恢复截止时间。

## 当前业务与预算边界

21:08:03 快照：

| 项目 | 当前数据 |
| --- | --- |
| 修复上线后 / 最近 30 分钟成功加群 | 均为 0 |
| 修复上线后 / 最近 30 分钟成功广告 | 均为 0 |
| 允许/试投审核 | 28 份，当前身份确认 3 份 |
| 启用广告关系 | 26 份，23 份有待处理资格事件 |
| 加群候选 | 214，其中 213 待预览/身份确认，1 排除，就绪 0 |
| 全局日读取 | 5479/6000，剩 521 |
| 加群、审核、存活检查、广告审核刷新读取 | 当前均为 0 |
| 广告实际分配 / 已用 / 剩余 | 740 / 733 / 7 |
| 广告预留重试后的计划读取 / 单次发送成本 | 5 / 6，因此计划发送槽位为 0 |
| 主账号监听器 | connected=false，state=deferred，reason=telegram_read_budget |
| 当前预计读取窗口恢复 | 22:44:10 左右 |

广告基础份额 2100 中已借出 1360，已花费的借款按既有策略持续记账至窗口到期。全局剩余 521 正好对应广告剩余 7 和同步日份额剩余 514；它不是现在可供加群或续审随意使用的余额。同步用途也受小时上限影响，当前有效可用为 150。监听未连接且存在未完成检查点，当前没有满足同步空闲借用条件的证据。

因此当前广告额度保护按既有规则生效。需要修复的是条件解除后的派发完整性、资格统计一致性、有效退避和原因分类。验收应在有足额读取时，隔离验证只剩续审、等待 AI、缺失审核或名单变化的账号仍能自动推进，并观察真实成功加群、广告 message_id 与监听连接恢复。

## 复现与证据

本轮应用工作区的既有修改保留，生产采集采用只读事务和查询超时，复现使用独立内存 SQLite。诊断不调用 Telegram。

证据目录：[mechanism-audit-20261009](E:/project/Vanguard/deploy-artifacts/mechanism-audit-20261009)。

- reproduce_dispatch.py / dispatch-reproduction.log：14 个复现场景及正常对照。
- runtime.log：20:58:34 业务、事件、预算、监听与源码哈希快照。
- runtime-final.log：21:08:03 再次采样。
- local-source-hashes.json / source-comparison.json：10 个关键源文件与当前生产一致。
- mechanism_snapshot.py：只读采集脚本。

修复优先顺序：先补齐审核派发与恢复入口；再统一库存资格检查；随后接入有效重试截止并校正加群原因。生产临时采集脚本在核查完成后清理。
