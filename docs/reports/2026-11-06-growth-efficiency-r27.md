# R27：修复嵌套 AI 等待与读取计划不一致

状态：2026-11-06 21:22:28（北京时间）已上线，仅更新 growth；业务验收未通过，仍仅 33 号灰度。

本轮针对 R25 数字帧取证和持久化路径核对发现的问题：已采集材料位于 `pending_collection` 内，取消处理却只在外层记录 `CancelledError`。旧执行器仅识别外层 `ai_pending`，因此可能将等待 AI 的任务留在 `READY`，并保留旧的 8 读计划。实际 service 的 `for_review → step_plan` 也有独立的同类判断，必须与执行器一并修复。该问题证明了重复领取和错误预算预测风险，不能据此直接断言 Telegram SDK 已重复读取。

候选只修改 `qualification_engine.py` 与 `review_read_plan.py`，沿用现有证据、检查点和 AI 材料幂等键。目标行为是：同一作用域、同一事实事件、采集完整的 AI 等待进入有限 `WAIT_PROVIDER`，Telegram 计划为 0；旧 `READY` 工作在领取前完成本地归一；同键 AI 已完成时只唤醒本地消费，最终资格判断仍执行。真实规则、身份、成员关系变化、显式补证或强制刷新不得被旧 AI 等待掩盖。缺少材料键或缓存读取超时不能当作未发出请求，不能释放 attempted 围栏或重新购买 AI。

独立实际入口测试还查出两项必须同时处理的边界：解包内层材料时丢失外层失效标记，以及 24 小时成熟已到期、实际需要定点核验时仍投影 0 读。候选须保留这些标记，并使到期义务回到可支付的小步骤，不能让等待优化阻止成熟核验。早期测试通过记录不替代新增边界后的最终回归。

预测边界：通用工作投影没有完整审核行、checkpoint revision 与当前规则证明，因此超过 24 小时、时间缺失或未来时间的材料不直接预测 0 读，而保守保留最多 8 读。实际 service 若证明原有 720 小时 checkpoint 仍可复用，继续沿用正文与游标并释放未用预留；本次不强制整包重采。因而本轮不能宣称所有长时间 AI 等待都已实现 0 预算预测，也不能将稳定正文直接等同于当前权限或访问证明。

部署仅更新 `telegram-growth-worker-survival-continuity`。源码父镜像为 R25 main，实际运行保护父为 R26；backend、Celery、beat、共享 PostgreSQL、Redis 与无关服务保持原身份。选择器、原测量 epoch、预算计数、TTL、贷款、未知结果围栏和历史缺口均保持。

## 验证与实际上线

[最终完整回归](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/nested-ai-wait-fix/maturity-adapted-attempt4/verified-tests.json)为 159/159，[独立关键场景](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/release-tools/final-candidate-independent-review.json)为 46/46，均无错误或跳过、无真实外部动作。包括实际 `draft → assess`、新规则事件、取消后恢复、AI 同键完成竞态、成熟到期、编辑重计时、成熟后已核验，以及实际 `defer` 四轮持久重试。404 个 Python 应用文件中仅两个文件变化，9+2 个函数外 AST 保持。

原样旧回归和夹具适配分别保留：R25 原样 74 通过／8 失败；初版候选原样 117 通过／7 个相同旧来源夹具失败。最终只补充已确认加群来源及有效材料夹具，115 条原断言全部保留。开发期间发现的快完成、实际失效解包、成熟以及退避回归均保留失败证据，不能把较早通过的候选当作最终版本。

[总验证绑定](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/verified-tests.json)固定两文件：`qualification_engine.py` SHA `10bffcaaebc0f947fe68941c20274bcdc7dbae44565fa82f0917d6be1007f459`；`review_read_plan.py` SHA `a3b29595772a69d2f9bbda885021ebbe0aca612741e3ea83f37c15e5a928084f`。部署 20 组模拟与 8 个范围拒绝、打包／传输 17 场景、自然采样生成器 14 项均通过。真实构建和隔离 smoke 通过。

[最终 manifest](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/release-r27-final/release-manifest.json) SHA `dac391fa30755f2618ff1726ebe4ef92466653bbf127cfa5e2496d39c731a770`。[原始生产回执](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/deployment-applied-remote-original.json) SHA `8a1d096a1c33075d7823a4941b691b1e2feda083c6fa82c5434c6bb9bb303c54`，状态 `supplement_started`，实际时间 `2026-11-06T13:22:28.069377+00:00`。canonical Compose SHA `a5da68ed5316c7c3fa44756686b6efdf92c3e13f92a72a4748b694bc296e577b`。

实际 growth 镜像 `sha256:f5a8187c69a99c3a1dc3bd2dc1c346f10cec03cb7953c7155f9f861aaefa0eeb`；backend 保持 R25 `7fd943d9…`，Celery 保持 R26 `1342b8a7…`。发布回执确认受保护服务身份不变，没有 schema／计量切换，也没有修改租约、预算事实或未知结果。

[本地安装回执](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/local-install.json)检查 1489 个文件，仅上述两源文件变化，Git index 不变，未提交或推送。传输准备首轮因 root 预置健康检查文件与继承工具同名而在本地拒绝；保留该文件并改名后，使用原工具重新生成，未改变冻结包、未触及生产。

## 当前效果边界

此前 R25 自然业务已确认 1 条加群完整成功链（33 号、群 217、JoinLog 4213）。[R26 后续只读样本](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/pre-release-evidence/sample-20261106T124235Z.json)中仍出现广告阶段排队 50.121 秒、执行 61.216 秒超时；[定点数字投影](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/pre-release-evidence/ads-timeout-trace-r26.json)没有该次最深等待帧，不能将它归因 AI 或某一条 SQL。

[21:33 一次专项只读核验](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/join-chain-only/join-chain-20261106T133320Z.json)确认连续修复以来累计 **2 条完整自然加群链**，当前成员确认均有效，样本未截断。新增记录 4214、群 349：20:48:18.555 发起、20:48:19.532 进入 SDK、20:48:21.388 平台响应、20:48:22.289 成员确认。两个成功分别发生于 R25→R26、R26→R27 区间；R27 上线后仍为 0，跨发布边界链为 0。没有仅凭 `last_run.joined=1` 计数，没有把加入状态当作广告授权。

当前不能认定广告执行恢复、广告完成率达到 95%、120 秒启动 SLA 达标或匹配任务读取下降 30%。计量仍有不完整窗口，历史 gap 不清除。本次只读探测没有 Telegram、AI 或生产写入，也没有人工发广告或加群。

[21:23 健康检查](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/runtime-health-20261106T132337Z.json)：健康与前端 HTTP 200、未认证请求 401，各应用重启数 0。growth 有 14 个 ERROR 字符串、Celery 有 1 个；没有仅凭计数认定故障类型或宣称零错误。

[21:25 首个自然样本](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/natural-sample/sample-20261106T132513Z.json)前后运行镜像匹配。R27 窗口内广告成功及新加群完整链均为 0；广告机会分母 0、完成率未知。READY 50、WAIT_PROVIDER 7、WAIT_BUDGET 3、WAIT_EVENT 5，计量仍有 7 个新失败采样帧。没有当前窗口内已配对的 33 号 owner 事件，旧票据／资格执行租约仍可见；此样本尚不能证明新路径完成实际推进。

[21:27 结构快照](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/wait-projection/growth-efficiency-20261106-r27-nested-provider-wait-20261106T132733Z.json)为 READY 46、WAIT_BUDGET 7、WAIT_PROVIDER 7、WAIT_EVENT 5；其中 14 条 READY 含嵌套 AI，8 条正读取预测。该变化仍仅是结构统计，不能证明每条工作的事件、材料版本或成熟义务，也不能作为重复率改善验收。

[21:32 后续自然样本](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/natural-sample/sample-20261106T133246Z.json)已捕获 R27 运行镜像中的 33 号两次审核阶段完成：请求 `41b828d034bcf05c` 排队 10.912 秒、执行 12.706 秒；请求 `c8ddd9a0f13a792c` 排队 0.011 秒、执行 13.275 秒。另有存活阶段排队 88.125 秒、执行 13.888 秒完成。它们证明新运行路径已实际执行，不能据此宣称审核最终通过、SDK 成本下降 30% 或整个账号调度 SLA 达标。当前 READY 46、WAIT_BUDGET 7、WAIT_PROVIDER 7、WAIT_EVENT 5；广告和新加群成功仍为 0、广告机会分母为 0，计量新失败采样帧已为 24，窗口依然不完整。

[一次有界日志诊断](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/release-tools/bounded-log-diagnosis/strict-classification.json)以实际回执和三个应用镜像前后保护确认 R27 的 33 号 review、survival 各有 2 次 started／completed；该有限范围未见它们的 timeout／failed 终态。仍发现后台 `growth_event_persistence_failed`、`growth_inbox_failed`、`private_outbox_loop_failed` 固定 TimeoutError 事件各 1 次。初版提取器将 `error=` 字段误当日志级别，其 10／6 计数不作为 ERROR 总数；原证据、严格分类和 Celery 截断／未分类 DBAPI 候选限制均保留，未再次读取远端。后台超时和计量缺口仍属未完成项，不能因 HTTP 健康或两次阶段完成宣布整体恢复。

[21:03 发布前结构快照](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/wait-projection/growth-efficiency-20261106-r26-join-counter-20261106T130349Z.json)中，33 号共有 65 条工作：READY 51、WAIT_PROVIDER 7、WAIT_BUDGET 2、WAIT_EVENT 5。15 条 READY 含嵌套 AI 等待材料，其中 10 条持久读取预测大于 0。该快照只做结构统计，未逐条核验当前事件、成熟义务与授权，不能把全部 15 条都判为可直接归一的重复审核。

[20:47 再次核实的生产业务模型](../../tmp/growth-efficiency-20261106-r27-nested-provider-wait/pre-release-evidence/production-model.json)：Provider `openai`，模型 `gpt-6.1-sol`，Base URL `https://api.openai.com/v1`，reasoning effort `low`，广告专用配置关闭，沿用共享配置。没有输出 API key。
