# R26：统计写入不再占用广告调度围栏，修正加群漏计

状态：2026-11-06 20:19:51（北京时间）已上线，仅更新 Celery。服务健康；整体业务验收仍未通过，未扩大 33 号灰度。

本轮处理 R25 自然样本与源码核对发现的两个确定问题：

- 加群真实成功后，新流程返回 `joined_pending_qualification`，调度统计只识别 `joined`、`joined_pending_review`，因此出现 JoinLog 已确认成功而 `last_run.joined=0`。已补齐状态白名单；审批中、未知、失败和仅 `succeeded>0` 均不会被当成加群成功。
- `run_ticket` 在释放账号运行围栏、完成阶段票据前，先等待 `note_result` 的第二次数据库初始化、全部 pending 广告机会查询和提交。无等待原因时也会执行该查询。已调整为先释放账号围栏、完成票据、保存原业务摘要，再在确有原因时补充统计原因；等待使用 0.5 秒可取消边界，失败记录固定事件及异常类型。异步取消清理可能超过 0.5 秒，但两道调度围栏已提前释放。

`note_result` 只补 pending 机会的 `last_reason` 和 `last_observed_at`。本次没有改变机会分母、成功回执闭合、真实发送／加群执行、未知结果围栏、授权判断、预算和频率。历史日志不回填或改写。

## 验证及来源

[组合候选验证](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/join-counter-fix/measurement-followup/validation.json)70/70 通过；[独立关键场景](../../tmp/growth-efficiency-20261106-r26-join-counter/release-tools/final-candidate-independent-review.json)13/13 通过。覆盖无原因时零新增统计数据库初始化、有原因时记录、统计阻塞／失败／取消时围栏已释放、原业务异常与取消不被吞掉，以及加群状态矩阵。

部署工具 20 组模拟、7 个范围拒绝检查通过；封包工具 10 场景、transport 10 项本地检查通过。来源仅一个既有文件 `core/scheduler/growth_dispatch.py`。模块其他业务代码 AST 保持，仅增加固定日志初始化。候选 SHA256 为 `d68daaf5a892ba905a6a9e252c0451fb7bf3a5625dc18cac5eb843bf1efccea3`。

首次封包被严格 AST 守卫拒绝，因为已审查的日志 import／初始化位于函数外。修正为只允许这两个确切日志 AST 节点后，重跑封包验证并重新冻结证据，未改变业务候选，失败记录保留在 `pack-attempt1-rejection.json`。

[最终发布清单](../../tmp/growth-efficiency-20261106-r26-join-counter/release-r26-final/release-manifest.json) SHA256：`264d8cf53c47e5bf9f27b8129fa6c7ced8bcc0242aceef315b0f6f19bb75babd`。以实际 R25 Celery `b6e99f4f…` 为父镜像，只叠加该文件。构建与无网络、无生产挂载的 smoke 已通过。

[本地最小安装回执](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/join-counter-fix/measurement-followup/local-install.json)核对了 1923 个文件，仅本地 `growth_dispatch.py` 改变，保留既有注释及无关改动；Git index 不变。没有提交或推送。

## 实际生产状态

[原始生产回执](../../tmp/growth-efficiency-20261106-r26-join-counter/deployment-applied-remote-original.json)状态 `supplement_started`，SHA256 为 `00e2dee91bd7ab7f171248fd0e95ddbd55a5e843d27da7561ba251a2b0c22867`，实际时间 `2026-11-06T12:19:51.809433+00:00`。canonical Compose SHA256 为 `b2ac34204ee808ccb295565dda0fd284e4fad3ae5afe58f93106334ed343f31f`。

实际 Celery 镜像为 `sha256:1342b8a7a6f53ce63968e25b34c547294be473775a9e9a2d99e06c472d19c19c`。backend、growth 仍为 R25 `sha256:7fd943d93d030ee11b0b4b707169e0f89be60e5ff7665fe274e28b5c8018b238`，其他服务身份核验通过。beat 保持原服务；共享 PostgreSQL、Redis、Sub2API 均未操作。

[20:22 健康检查](../../tmp/growth-efficiency-20261106-r26-join-counter/runtime-health-20261106T122206Z.json)：健康和前端 200、未认证请求 401，应用零重启。Celery 有 ERROR 字符串计数，不以该计数直接断定具体故障，亦不宣称零错误。

33 号选择器、R21 原 epoch、每账号 180/min、360/hour、3600/day、每进程池 2+1、业务总连接上限 25、计量超时和所有预算事实保持。旧观察器与混合样本保留，不作为当前单版本验收。

## 已观察到的业务与未解决项

[R25 报告](2026-11-06-growth-efficiency-r25.md)记录了本轮连续修复期间的一条真实自然加群完整链：33 号 JoinLog 4213、群 217，在 19:53 完成 SDK 进入、平台响应和成员确认。不是人工测试请求；该群尚不等于具有广告授权。此次修复不会改写其历史 `last_run.joined=0`，新统计分支适用于后续执行。

[R25 20:07 样本](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/sample-20261106T120757Z.json)仍无广告成功回执；33 号 review 排队 1.707 秒、执行 70.405 秒超时。[数字帧取证](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/timeout-trace-20261106T121602Z.json)显示该次已越过原 forecast／历史查询，最深等待为 `qualification_ai.py:268` 的本地材料幂等缓存读取，尚未进入 provider launch，不能归因模型响应慢，也不能将整个 70 秒算成该查询耗时。

已有材料和采集检查点在进入 AI 缓存核验前已提交。后续须核实超时后复用路径，区分连接领取、SQL、前序累计耗时及取消清理；缓存读取超时不能当作 cache miss，不能移除 attempted 围栏或再次购买 AI。计量仍有不完整窗口，历史缺口不清除。

尚未证明广告完成率 ≥95%、健康可支付工作 95% 在 120 秒内启动、匹配任务读取成本下降 30%，也未完成本版本 24 小时自然灰度。`business_acceptance=false`、`automatic_expansion=false`。

[20:27 首个 R26 自然样本](../../tmp/growth-efficiency-20261106-r26-join-counter/natural-sample/sample-20261106T122754Z.json)已执行，回执及三个镜像前后匹配。R26 窗口内新增加群链及广告成功均为 0，广告机会分母为 0、完成率未知；这不取消此前 R25 已确认的加群 1 条。工作状态为 READY 54、WAIT_PROVIDER 3、WAIT_BUDGET 2、WAIT_EVENT 5，无活跃资格领取。采样时存在接近自然到期的旧 ads 调度运行票据（TTL 5 秒）；未清租约或重放任务。计量仍有 17 条 R26 新失败采样帧、窗口未完整，不能验收成本或重复率改善。

[20:31 后续样本](../../tmp/growth-efficiency-20261106-r26-join-counter/natural-sample/sample-20261106T123148Z.json)已见 33 号新的广告阶段：请求 `7b8767467753b245` 排队 63.524 秒、执行 62.094 秒超时，调度总耗时 129.073 秒，返回 `session_owner_stage_timeout`、processed=0、sent=0。随后账号围栏已由 survival 阶段领取，未因统计写入保留上次 ads 围栏。广告成功仍为 0。该样本不支持“广告执行阻塞已解决”；R26 仅修正统计占用和计数，剩余 owner 业务路径仍需修复。

生产业务模型仍为 `gpt-6.1-sol`，Base URL 为 `https://api.openai.com/v1`，reasoning effort 为 `low`；广告沿用共享配置，本次没有修改模型或输出 API key。

[R27 后续修复](2026-11-06-growth-efficiency-r27.md)于 21:22:28 仅更新 growth，处理嵌套 AI 等待、成熟读取计划与实际失效材料边界；Celery 保持本篇 R26 镜像及统计修复。159 项回归和 46 项独审通过，业务验收仍未通过，未扩大灰度。
