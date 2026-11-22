# R25：减少审核执行中的重复预测与隐式读取

状态：2026-11-06 19:45:35（北京时间）已完成生产增量切换，健康核验通过；33 号自然业务验收尚未通过。`business_acceptance=false`、`automatic_expansion=false`。完整回归及隔离 PG18 验证已通过，不能据此宣布广告或加群产出改善。

本次针对 R24 已定位的执行阻塞做小范围修复，不把提高并发或增加读取预算作为解决方式。原 33 号灰度选择器及 R21 计量 epoch 保留。完整计划的真实业务验收仍需广告机会、成功回执、加群 SDK 进入及成员确认共同证明。

## 问题证据及判断边界

R24 的[真实超时跟踪](../../tmp/growth-efficiency-20261106-r24-owner-timeout-trace/postrelease-owner33-trace-20261106T105648Z.json)中，33 号请求 `4acbebf7c73cfa64` 排队 88.376 秒、执行 32.817 秒完成；请求 `ca88069c39ad0cd4` 排队 51.859 秒、执行 62.977 秒超时。后者取消时最深应用路径为：

`qualification_service.collect → group_qualification full-info → rpc_governor before/snapshot/apply_forecast → qualification_peer_history → db.execute`。

这证明中断时停在 full-info 进入前的需求预测／历史查询等待处，不能证明某条 SQL 单独运行了 62 秒，也不能据此认定数据库锁。

[只读生产数据库探针](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/query-economy/owner33-db-result-v1.json)绑定实际 R24 回执，前后三个镜像一致；仅用一个临时 NullPool 连接，statement timeout 为 1000ms、lock timeout 为 300ms，不执行历史正文查询、COUNT 或 EXPLAIN ANALYZE。采样时现有业务连接为 17 条，其中 idle 13 条、idle in transaction 4 条，最大事务年龄 5.274177 秒，未观察到 Lock 等待。这只是该时点的状态。

目录估计审核表约 1079 行，尚无以 `group_id` 开头的索引；代表性非 ANALYZE 计划预计返回 3 行，总 cost 约 100.07。暂不新增索引，先移除每个 RPC 的重复预测及 ORM 隐式加载；该规模顺扫不是已证实的 62 秒根因。

[实际容量取证](../../tmp/growth-efficiency-20261106-r24-owner-timeout-trace/owner33-diagnostic/capacity-result-v1.json)显示 configured concurrency=2、reserve=2、effective cap=1、priority reserve=0。**effective cap=1 暂不改**，不能把配置值 2 描述为实际并发 2。

## 七文件发布范围

[范围契约](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/scope-contract.json)只允许七个既有应用文件，没有新增应用文件：

| 文件 | 修复内容 |
| --- | --- |
| `core/account/rpc_governor.py` | 已领取、未移交、额度绑定有效、lane 匹配且属于选中引擎的小步骤，不再在每次 RPC 前重跑全量 forecast。每次仍检查新鲜限额、冷却、恢复限制、read-work 身份及 owner／初始加群领取；其他情况保留完整 forecast。 |
| `core/account/work_demand.py` | 加群需求查询禁用无用成员关系加载；可选找到第一个真实可执行候选后早停。全量运营统计默认仍完整有界扫描，早停结果明确 `sample_complete=false`。 |
| `core/account/join_demand_lending.py` | 仅效率引擎使用早停查询；unknown／incomplete 不能被当成闲置额度，原保底与预留保持。 |
| `modules/acquisition/qualification_service.py` | `qualification_peer_history` 增加 `raiseload(Group.account_memberships)`，删除隐式关系加载，保持全历史、完整正文与判定语义。 |
| `modules/acquisition/qualification_sla_runtime.py` | 非 owner 稳态仅写自身 source；owner 聚合。失败 attempt 节流不推进成功水位；history_loss 按实质签名幂等。source CAS 与条件 retirement 保留并发新事实，冲突回滚水位，缺口仍为 unknown。 |
| `modules/acquisition/qualification_sla_lifecycle.py` | 接入纯本地、无 I/O 的自身 source provider，配套生命周期和失败 attempt 处理。 |
| `core/account/session_exec.py` | 继承 R24 的固定数字超时帧诊断，本轮没有扩容执行器或修改超时语义。 |

[查询专项证据](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/query-economy/README.md)使用实际映射模型和 SQLite 捕获 SQL：原查询发出 3 条 SQL（历史、成员／账号、账号配置图），候选只发出 1 条。全部 Group／Audit scalar 列、跨账号／跨 core alias 的全历史、降序、`populate_existing`、大正文、checkpoint、`checked_at` 为空的 queued 行及历史 ban 事实均保留；五个调用者不使用被禁用的关系。该结果是 SQL 条数回归，不等于生产端到端耗时已下降。

## 保留的预算和业务保护

每账号 180 次／分钟、360 次／小时、3600 次／日总读取预算保持；原 `RESERVED_BUDGET_LUA`、token、remaining、总限额、listener floor 和已登记贷款校验保持。没有 schema 变更，没有重置预算计数、窗口 TTL、贷款、租约或未知结果围栏，也没有启用 P2。

33 号选择器及 epoch `r21-ready-origin-canary33-20261106T034500Z`（`2026-11-06T03:45:00+00:00`、accounts `[33]`）保持。账号风控、权限和成员关系检查、历史 ban／未知身份、AI 幂等、警告证据完整性及广告／加群未知结果去重保护保留。数据库池继续每进程 2+1，Vanguard 总连接上限 25。

生产仍以 `/opt/shared-postgres/stacks/vanguard/compose.json` 为唯一运行时 Compose；共享 PG18、Redis 和无关服务属于保护对象，遵守[共享 PostgreSQL 部署边界](../ORACLE_SHARED_POSTGRES.md)。

## 已完成验证

[冻结验证汇总](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/verified-tests.json)的 `required_passed=true`，但业务验收和自动扩灰度仍为 false。

| 验证 | 结果与限制 |
| --- | --- |
| [main 第三轮](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/validation-main-attempt3/validation.json)／[celery 第三轮](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/validation-celery-attempt3/validation.json) | 两角色分别 507/507：冻结 419 项、owner maintenance 16 项、保护／生命周期 58 项、正常导入诊断 14 项；零失败、错误及跳过。 |
| [预算专项](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/budget-tests/validation-attempt3/validation.json) | 29/29；FakeRedis + Lupa 执行原 Lua，不是真实生产 Redis／Telegram 延迟验证。 |
| [查询专项](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/query-economy/pytest.xml) | 54/54，含 52 项准入／跨账号 ban 回归及 2 项实际 SQL／结果等价验证。 |
| [计量专项](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/narrow-tests-final.xml) | 18/18。 |
| [真实 PG18 并发事务](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/pg18-source-transactions/actual-pg18-20261106T113318Z.json) | 3/3；隔离的一次性 PG18 容器，不是共享生产库。覆盖自身 CAS 冲突回滚、owner 保留更新 source、条件 retirement 并发保护；SQL 300ms／writer 0.5s 超时不增加，生产 SQL 操作为 0。 |
| [部署模拟](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/release-tools/deployment-offline-validation.json)／[工具独审](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/release-tools/release-tools-independent-review-v2.json) | 20 组部署场景、13 项非法 scope 拒绝、6 个 wrapper 模拟 plan／write；离线完成，不代表实际发布成功。 |
| [本地最小补丁验证](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/local-patch-preparation/pytest.xml) | 12/12；保留本地无关 P1 逻辑和原换行字节。 |

前两轮失败记录保留：attempt1 两角色各 4 failures，旧 R18 fixture 的 snapshot 使用 synthetic limits，而 fresh `limits_for` 返回另一组默认限额；R25 的每 RPC 新鲜校验暴露了输入不一致。[测试输入调整](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/test-input-adjustment.json)仅令两者使用相同测试限额，未改应用代码或断言，R18 原件保留。attempt2 两角色各 1 setup error，迁移后的测试缺少纯 SQLite `test_db` fixture；另将 scope 校验修正为实际 CRLF 字节的 SHA。[修复记录](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/fixture-scope-repair.json)明确未改应用源码或断言。第三轮全部通过。

## 冻结来源与本地安装

[发布清单](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/release-r25-final/release-manifest.json) SHA256：`1d826cb7ade317a53bb9d15672f8bdd2098d2a78639b5b87df810a569cc4f2b5`。清单生成时间 `2026-11-06T11:40:16.543905+00:00` 不是生产切换时间。

父 R24 manifest SHA256 为 `e8722082f848ab954666cacd1c40c788499348821c5f12fd07d818d043e8a98c`；canonical Compose 原始 SHA256 为 `16e09a67ff826d0c151fc18d9e0a5b7e84231fb72adf08e51c37422081c9a236`。两角色以实际 R23 main `b45949a7…`／celery `10deb8db…` 为构建基底，并显式叠加 R24 session 诊断（源码 SHA256 `5ff83969ce81b59080b561e7e0e6844341ff87d52681d47a1274b319819e4029`）。

实际回滚父须按服务识别：backend 为 R23 `b45949a7…`，growth 为 R24 `4a0319bf8b84095eebbebd6c7ea16d87ae23755fc703906946bb5f45fad5df0c`，celery 为 R23 `10deb8db…`。R24 构建但未部署的 celery 候选不能当运行父。完整身份保留在工具独审与最终发布回执中。

[本地安装回执](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/local-install.json)记录在 `2026-11-06T11:43:05.209352+00:00` 完成最小 delta 安装：七文件 before SHA 全匹配，先 `git apply --check`，再安装 6 文件的 32 处行级改动，七文件 after SHA 与编译校验通过；`session_exec.py` 保持本地原样。没有整文件覆盖、stage 或 commit，Git index SHA 前后均为 `9cbcb265353e3424e8327ec1079ced4f99e1e7a68d78b28abd0fed0f6a830c4b`。

本地 `p1_sync` 及 admitted 分支之后完整 forecast fallback 的 `or p1_sync`、其他 target／借额／pace／metrics 逻辑保留。本地合并结果与冻结生产 overlay 因既有工作树差异而可能具有不同整文件 SHA，二者均有独立哈希回执，不能混用。本地安装时间不是生产切换时间。

## 实际发布及首次自然采样

[原始生产回执](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/deployment-applied-remote-original.json)为 `supplement_started`，SHA256 为 `bf570ae2aa60c1df82d0f394e561db1808bc3dc4640da023eb3b6450cbbdf300`；实际完成时间 `2026-11-06T11:45:35.496225+00:00`。canonical Compose SHA256 变为 `550f3b28a0ff023213d195b612d0b97068a35d45f12c8c5ee2b60c289bb1fba3`。保护服务身份核验通过，beat 已恢复；selector 仍为 `true/33`，未变更租约、预算事实或未知结果。

实际运行镜像：backend 与 growth 均为 `sha256:7fd943d93d030ee11b0b4b707169e0f89be60e5ff7665fe274e28b5c8018b238`；celery 为 `sha256:b6e99f4f516fbbf047c5cc01293f7145f57e16bc54e631a9a4f659ae1aa295d9`。[构建及 smoke 记录](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/built-images.json)与[19:46 健康采样](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/runtime-health-20261106T114643Z.json)中的运行身份一致；健康、前端返回 200，未认证请求返回 401，三应用零重启。日志有 ERROR 字符串计数，需依据固定事件和诊断帧分类，不能称为零错误。

[19:48 首样本](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/sample-20261106T114805Z.json)、[19:51 样本](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/sample-20261106T115118Z.json)及[19:52 样本](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/sample-20261106T115231Z.json)均通过采样前后原始回执及三镜像核验，只读、Telegram/AI 调用与生产写入均为 0。旧 R23 观察器未重启或交接，混合版本历史样本保留。

截至 19:52 的事实与限制：

- 自然窗口内广告成功回执、加群 intent／SDK 进入／成员确认均为 0；到期广告机会分母为 0，完成率未知，不能以零失败通过。
- 存储中有 5 个授权指针，均已失效，未失效且匹配当前成员关系代次的指针为 0；这是指针统计，不是最终发送入口的放行证明。
- 工作为 READY 55、WAIT_PROVIDER 2、WAIT_BUDGET 2、WAIT_EVENT 4，均无活跃领取。19:48 残留审核调度运行票据 TTL 244 秒，19:51 为 50 秒，19:52 已自然到期；未删除租约或重放未知操作。广告与加群随后出现排队票据，尚未见新完整 owner 执行。
- 19:48 总读取小时 73/360、日 1464/3600；加群／审核基础日额度已用 1077/900，基础审核额度为 0。该快照未做借额 forecast，不能据此断言可借额后仍无法执行。
- 计量来源当前可见且成功水位有前进，但历史 gap=1024、`denominator_loss=true`、transaction frontier 尚未核实；19:48 至 19:52 的 R25 新计量失败帧由 10 增至 16。不能声称计量完整或超时已消除。
- 尚未形成 R25 后的封闭完整小时计量窗口，读取成本、重复率和吞吐改善均未知。

## 尚待业务验收

### 19:54 更新：自然加群完整确认链出现

[19:54 样本](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/sample-20261106T115428Z.json)确认 33 号有 **1 条本次上线后的真实加群完整链**：JoinLog `4213`、group `217`，意图 `19:53:17.594633`、SDK 进入 `19:53:25.038865`、平台响应 `19:53:26.512481`、成员确认 `19:53:27.380805`，状态 success 且当前成员关系为 joined。全部阶段属于同一 attempt，位于观察窗口内，不是人工探针。该目标的当前成员关系不等于广告授权。

对应 owner 请求 `744796a1b0d1c406` 排队 17.521 秒，执行 39.765 秒完成。调度 `last_run` 显示总耗时 57.618 秒、succeeded=1、joined=0；后者与完整 JoinLog 链的统计口径尚需核对，不能用它覆盖已证实的成员确认，也不改写原样本。审核 READY 数由 55 变为 56，尚无新增广告成功回执。

这证明自然加群链已能推进，尚不足以证明新增可用广告群、整体吞吐提高或匹配读取成本下降。计量失败仍在出现；[首样本超时帧独审](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/first-sample-timeout-analysis.json)将 10 个新帧定位为：7 个 Celery 首次 SET LOCAL、1 个 growth 来源 CAS 更新、1 个 growth 容量 COUNT、1 个 growth 提交等待。外层为 0.5 秒事务总时限，不能认定最后一条语句独自耗时超限，也不能由无进程身份的采样帧证明或否定实例内 20 秒节流。未增加连接或超时。

后续[20:07 样本](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/sample-20261106T120757Z.json)记录 33 号一次 review 排队 1.707 秒、执行 70.405 秒超时，广告成功仍为 0。[纯数字帧](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/timeout-trace-20261106T121602Z.json)将该次最深等待定位到 `qualification_ai.py:268` 的本地材料幂等缓存读取：尚未发起 provider 调用，不能说是 AI 响应慢；也不能把总耗时当作该查询耗时。此请求已越过原 forecast 阻塞点，但整体审核效率尚未验收。

20:19:51 追加了只更新 Celery 的 [R26 修复](2026-11-06-growth-efficiency-r26.md)，解除纯统计写入对调度围栏的占用并补齐加群状态计数。R25 单版本窗口止于该切换；原样本保留，后续不混称为 R25 单版本验收。

后续继续根据同一版本的真实业务样本补充：

- 从 R25 完成切换后开始、前后回执及镜像均匹配的自然样本窗口。旧 R23／R24 混合窗口保留并标明不完整，不用于 R25 单版本验收。
- 广告到期机会、有效授权目标、成功回执及预算／软件排队漏投；加群 intent → SDK 进入 → 平台响应 → 成员确认／未知各阶段与实际回执。
- 排队和启动耗时、审核超时、无进展执行、重复读取及匹配任务成本；不能仅用缓存命中或低读量宣布改善。
- source 生命周期、成功水位、缺口原因及覆盖完整性；历史 gap 保留，不补零。未形成至少完整小时的新计量或样本不足时，继续保持条件不足。

[自然采样器说明](../../tmp/growth-efficiency-20261106-r25-source-coverage-bounded/natural-sample/README.md)已准备，要求最终 receipt 及三运行镜像前后校验。仍需覆盖计划要求的 33 号 24 小时自然灰度、完整日预算窗口和先例成熟过程；真实加群 SDK 进入和成员确认未出现前，不能以零失败验收。广告完成率目标 ≥95%、健康可支付工作 95% 在 120 秒内启动、匹配读取成本下降 30% 均尚待业务数据验证。

生产业务 AI 配置沿用已核实的 R24：`gpt-6.1-sol`，Base URL `https://api.openai.com/v1`，reasoning effort `low`；广告使用共享配置，本次未修改模型，也未读取或记录 API key。
