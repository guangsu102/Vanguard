# 广告与加群效率重构 r12 实施记录

## 状态

当前生产为 **R21，2026-11-06 北京时间11:29:34上线，仍仅33号使用新引擎**。SQL076、独立授权与工作状态、分步取证、新群自身准入、双保底及兼容界面已部署；R19修复新群首步领取顺序，R20修复计量容量恢复，R21补齐未来READY原点及恢复回执的机会关联。正式本地358项通过，精确生产角色各356项通过，实际隔离PG18四类新增场景全过；另有8项附加业务失败在R20原模块同样复现，完整保留。HTTP健康200、鉴权401、应用重启计数0、受保护服务身份不变。**尚未证明广告吞吐提高、95%广告完成率、严格120秒启动率或匹配任务降读30%。** 历史gap、预算消耗、回执和unknown全部保留，R12真实加群成功不算R21成果。唯一观察器PID2650725已于11:38:34按原身份安全接续，截止延长至11月7日14:02；无真实样本则继续保持灰度。当前详情见[R21记录](2026-11-06-growth-efficiency-r21.md)，后文保留各时点历史。

新引擎默认关闭，由 `GROWTH_EFFICIENCY_ENABLED` 和严格账号白名单 `GROWTH_EFFICIENCY_ACCOUNT_IDS` 唯一选择。旧审核路径保留到全量稳定运行 7 天。

| 交付范围 | 当前状态 | 剩余条件 |
| --- | --- | --- |
| 独立有效授权、唯一工作状态、兼容迁移 | 已部署，33 号灰度 | 真实授权及外发样本继续观察 |
| 8 读小步骤、依赖续采、成熟及 AI 等待复用 | 已部署并离线回归 | 等价任务自然成本尚未完成比较 |
| 新群自身准入、两个新流程位、广告/加群保底 | 已部署，r18 补齐首轮类别 | 原已消费预算不退款，33 号当前保存预算等待 |
| 兼容 API、可解释界面、SDK 与真实加群阶段计量 | 已部署，R20容量恢复、R21原点及恢复回执关联上线 | 原时刻健康/可支付联合证明缺失，历史gap与来源覆盖仍保持未知 |
| 仓库可重复回归 | 新增 87 项全部通过、0 跳过 | 不能替代真实业务验收 |
| 24 小时自然业务、95% 广告完成、120 秒启动、30% 降读 | 未验收 | 自然样本及严格分母/匹配成本证明仍不足 |
| 第二账号、矩阵及七天稳定后清理 | 未执行 | 以前述验收为前提 |

最新补充验证发现，R18 的 `claim_next` 仍按 `ready_at,id` 领取。真实领取函数的隔离回归中，31 条旧 READY 全部先于有确认加群来源的新目标领取；加群保底本身不改变这一顺序。R19 已将领取优先级修复合入本地工作树并发布，实际仓库回归 **142 项全部通过、0 跳过**，另有 20 个真实 PG18 场景全部通过。预算等待不能消耗新群首个有效步骤的优先机会；预算、授权、成员代次和 owner 围栏继续由原函数验证。该回归证明排序缺口，不是生产启动时延的测量。

R16 补充计量首次真实 PG18 隔离六例回执为 `tmp/growth-efficiency-20261106-r16/recovery-v2/pg18/actual-pg18-20261106T003033Z.json`（SHA-256 `077b602723abe1be3c50fd410f6e57459952c38be110dde44ff7d1c7b130a041`）。六例均在测试 schema 的 `metadata.create_all` 失败，实际执行数均为 0，不能视为恢复或容量验证通过。全部临时容器已移除，共享 PG 身份前后一致；原失败证据保留。候选继续 HOLD，正在定位测试结构错误，未部署到生产。

随后单例使用原 ORM 定义及递归外键闭包 7 表，未改模型、约束、默认值或候选三模块。真实 PG18 已成功进入 `Producer.run`，但两个 census 尝试分别在 0.512/0.518 秒取消；容量检查分别占 0.359/0.372 秒，最终未持久化扫描游标或新任务，故仍失败。回执 `pg18/attempt2-schema-diagnostic/actual-pg18-20261106T003911Z.json`（SHA-256 `f2b6b12c8cf2e044a998a57885f092220bc6c027581fae6213d8b4aac992411f`）已保留；临时容器全部清理，共享 PG 身份不变。该结果证明 SQLite 通过不足以验证 PG18 满容量恢复，后续优化必须保留原 0.5 秒写入上限，不以延长超时通过验收。

## 已实现的执行边界

- 新增 `qualification_active_grant` 和 `qualification_work`，最终结论追加到原审计表。后台排队、预算和 AI 等待不再等同于授权失效。
- 本地先计算 `ReadStepPlan`，默认一次最多 8 次读取；成熟、事件、预算等待保存工作状态，保留正文与游标，按缺失依赖续做。
- 人数、在线数、48 小时无广告等经营检查进入独立维护族。普通成员先例的 24 小时成熟、作者身份、访问、权限、警告和内容范围仍受原业务判定约束。
- 加群只支付目标自身必要工作；有效本地预览缓存为 6 读，否则为 8 读。不再用历史审核债封锁账号；新流程首轮采集同时最多两个目标。
- 复用原预算账本落实加群保底及监听控制保底。总读取预算仍为每账号 180/分钟、360/小时、3600/日，不清空历史用量、TTL 或贷款。
- 审核事件按版本确认，旧领取不能清除后到事件；已消费事件不会再次唤醒完成任务。终态 AI 失败进入事件等待，避免周期性占槽。
- 审核 bootstrap 批量筛选变化目标；13 个成员重复运行两轮只需 8 次 SELECT，未重复读取审计历史。
- 加群分别记录 intent、真实 SDK 进入、平台响应及成员确认。历史未记录阶段保持未知。
- 读取计量补齐六个执行角色、来源生命周期、持久化缺口原因和未确认水位。旧 gap 不擦除、不补零；原固定文件及预算计数继续使用。
- 兼容原群审核接口，新增授权、工作、阻塞、业务和效率信息；管理界面分别展示授权与后台进度。

## 已完成验证

下列套件存在覆盖重叠，不合计为独立用例总数。离线回归没有真实 Telegram 或付费 AI 调用。

| 验证范围 | 结果 |
| --- | --- |
| 新引擎本地核心回归 | 41 项通过 |
| 冻结生产 main + 审核 / AI / owner 适配 | 212 项通过，1 项已确认旧缺陷 strict xfail |
| 冻结生产 celery + 审核 / AI / owner 适配 | 212 项通过，1 项已确认旧缺陷 strict xfail |
| 冻结生产读取、存活、接口、加群、机会账本、AI / owner 组合 | 132 项通过 |
| 原 session_exec 队列 / 超时 / 取消回归 | main / celery 各 25 项通过 |
| 加群与不确定结果围栏 | main / celery 各 52 项通过 |
| 前端面板与冻结源码构建 | 7 项通过；build:check 通过 |
| 冻结生产 SDK 计量 success/error/cancel/deny | 12 个场景通过 |
| 发布脚本离线篡改、迁移锁与环境保留 | 6 个场景通过 |
| PostgreSQL 18 结构克隆及 SQL076 重复执行 | 通过；不导入业务数据，不连接测试网络 |

已确认的旧缺陷是原 r11 旧审核路径完成批准后未激活父群状态；在未修改的冻结源码上同样失败。新引擎对应路径已正确激活。该例外不用于掩盖新引擎失败。

隔离迁移验证的 SQL076 原始与规范化 SHA-256 均为 `70cf815697b263d7bc41d41735b5356532e3951356917fffcb1a88c5fcbaae51`。发布迁移使用原迁移器相同的 advisory lock `781246903` 和 LF 规范化 checksum。

## 发布与验收约束

生产角色源码分别冻结，发布包只应用本任务增量。主服务、Celery、资源搜索、reconciliation、guardian 分别从当前角色镜像构建；前端使用冻结 r11 源码并只替换审核 API/面板组件。不会整体覆盖本地工作树。

运行时仍使用 `/opt/shared-postgres/stacks/vanguard/compose.json`，只更新明确应用服务并使用 `--no-deps`。共享 PostgreSQL、网关、Redis 和其他业务不重建；校验其镜像与启动时间保持一致。现有鉴权、网络、会话挂载、数据库池及其他环境变量保留。

首次发布保持新引擎关闭，取得至少一个完整小时的六角色计量后，才按账号所有权和旧在途围栏切换 33 号。随后自然观察至少 24 小时，并要求出现真实入群 SDK 和成员确认；样本不足时维持灰度。广告机会完成率与匹配任务降读必须使用完整窗口，不能以零失败、缓存命中或低读量替代。

## 发布证据与当前观察

冻结包 SHA-256：`996bdd47fdc84deea44850e033929ed25aaa0a663d11e9a041a961c5cdaf49ac`；manifest：`15a303c67a99109dfdd9abac576152b17b6380daaebad9adf2ffc0f1fa50fd62`。254 个负载文件，静态依赖闭包新增缺失模块为 0。仅使用既有角色镜像与选择性增量，无网络构建；未覆盖原镜像标签。

五个 Python 角色及前端镜像均通过无网络、无生产挂载隔离检查。检查夹具为只读 Nginx 添加私有 `/run` 与 `/var/cache/nginx` tmpfs 后通过；应用包和镜像未因此改变。夹具修订另有哈希记录。

实际迁移 SQL076 checksum 与隔离验证一致。运行时 Compose 变为 `be9849c9573508a164ad710bb17674f238f160b34342daff200d75055310c5eb`；七个明确应用服务均 running，backend / reconciliation / frontend 健康，restart_count 为 0。共享 PG、Redis、网关的容器 ID、镜像与启动时间一致，未重建。原身份、会话、网络与非白名单环境设置保留。

严格只读首次验收确认六角色镜像、挂载、来源心跳与配置正确；原计量文件 inode `1703944`、大小 `16,261,120`、uid/gid `1000:1000` 保持。未清零预算、贷款、窗口 TTL、历史 gap 或业务回执；pending 为空、dirty 为 0。应用 PG 当前 7 / 25 连接，无 idle-in-transaction。各进程池 2+1，reconciliation 保留 2+0。

历史六条 `sent / pending / resolved` 加群行仍被目标围栏保护，不能仅凭 resolved 释放；33、34 号各一条历史广告 unknown 均保留。新代码修复跨账号 join_claims 仅凭 resolved 释放围栏的问题，没有改写这些历史行。

北京时间 01:29 的上线初期观测：新引擎关闭、Work 为 0，部署后广告完成和加群各真实阶段为 0。旧版此前 24 小时共成功广告 28 条、加群 0，属于发布前基线，不能归因于本次改动。单小时计量尚未闭合，未报告降读比例或机会完成率。

当前后续门槛是完整六角色闭合小时、33 号自然 24 小时灰度及真实入群 SDK 与成员确认；样本不足则保持灰度。第二账号、矩阵扩容及稳定 7 天后删除旧路径均未执行。

额外验收审查发现：现有 Work 的 ready_at / updated_at 会覆盖，当前读取耗时和账户 stage ticket 也不能还原完整的 READY episode、healthy / payable 起点及真实 owner step 启动。因此“95% 在 120 秒启动”当前为**未具备严格验收证据**，不能由零失败、平均耗时或当前无积压代替。独立有界测量补充正在准备；不用于放宽现有灰度或扩容门槛。

只读核心复核未发现新部署阻断。33 号新引擎独立于旧 34 号读取优化白名单；取证、进度恢复及计量阶段显式支持新选择器。灰度切换必须先确认六类旧执行进程退出，再统一切换选择器，原 owner 租约、预算、事实与 unknown 围栏继续保留。

除正文所述核心边界，最终实现还包括：

- 到期广告机会账本先于软件 / 预算阻塞登记，重试不重复扩大分母；仅精确匹配当前 peer / 成员代次的真实成功回执关闭机会。原 pending 保留，覆盖缺口时完成率为未知。
- AI 一次性尝试标记先持久化，再进入有界后台任务；等待期间释放 owner 执行槽，独立短 DB 会话，不重复购买不变材料。重启后不重发未知 AI 尝试。
- 新引擎审核必须由实际会话 owner 执行；Celery 仅 bootstrap / 唤醒，不使用短生命周期 fallback 抢跑。

证据在本地 `tmp/growth-efficiency-20261105-r12/` 的 release-tests、build / smoke receipts、migration-applied、deployment-applied、meter-cutover-applied、runtime-health-postdeploy 及 acceptance-release-fences 文件中。运行时凭据和完整 Compose 仅留在服务器保护文件中。

本记录会随真实观察更新。代码部署与离线验证不等于广告和加群效率已经提高。

## 有界灰度与后续观察作业

北京时间 02:29 启动首灰度作业，最多等待两小时。它只有在最新完整计量小时、33 号新鲜健康及 owner/session 证明同时满足时才切换；等待期间不改变业务。北京时间 02:43 核对时进程仍在等待，选择器尚未切换，没有激活回执。不得重复启动第二个切换作业。

另于北京时间 02:41:55 启动只读观察进程 PID `906313`，目录为 `/opt/vanguard/ops/canary33-observation-r12-cdc9fa2a`。它先等待相同 manifest 的 33 号成功激活回执；失败或超时则记录未验收。成功后每五分钟保存一次有界、哈希串联的脱敏观察，覆盖真实 24 小时成熟区间及 24 个完整 UTC 小时，最多 330 份。它不调用 Telegram，不扩容、不暂停、不回滚业务；时间已满也只表示需要审查，不能自动验收。原部分小时不会当作完整小时。

该观察脚本分别校验同一加入 attempt 的 intent、SDK 进入、平台响应和成员确认，并保留当前成员状态、未知结果及完整性缺口。原计量文件只读打开、私有映射；SQL 明确 READ ONLY。说明和启动回执分别保存在本地 `OBSERVATION_RUNBOOK.md` 与 `observer-launched.json`。若后续补充发布改变镜像，原观察会明确记录版本差异，不能继续冒称全段来自 r12。

启动时延与 whole-Work 成本关联、可执行库存及独立经营维护列表的补充仍在包外验证，尚未部署。启动时延计量限定实际产生 QualificationWork 的 backend / growth / Celery 子进程；其余读取角色仍由既有六角色固定 SDK 计量覆盖。没有补齐生产者来源及未确认水位时，严格启动率保持未知。


## 2026-11-06 03:40 后的事实更新

上述早期“新引擎关闭”和“等待切换”描述属于历史时点，以下为后续实际状态。

首版切换器在北京时间 03:04:18 因当前 Docker Compose 不支持 `create --no-deps` 而失败；其恢复分支复用了同一不兼容命令，六个应用角色和 beat 一度停止。03:07:27.966 已恢复原容器及原关闭选择器，未重置租约、预算、事实或 unknown。故障及重启计量缺口保留，未改动 PostgreSQL、Redis、网关及同机其他业务。原失败回执、紧急恢复回执和健康验证独立保存。

修订版切换器在执行前核对当前 Compose 2.26.1-4 的实际命令契约，改用 `up --no-start --no-deps --no-build --pull never`，先确认各角色一致且停止后再启动。16 组离线场景通过后，03:26:51.498 成功激活账号 33；六角色选择器一致为 true / 33，owner 与 listener 均为新进程之后的新鲜代次，原 beat 恢复。当前 canonical Compose SHA-256 为 `d5c1cb5196a5ac12792402d17073237cfed7180954dd01b07d6fac565dbd94cc`。未扩到第二账号。

截至 03:35:54 的只读证据：加群 attempt 4201 / group 245 的同次链为 intent 03:33:47.799 → SDK 进入 03:33:48.672 → 平台响应 03:33:49.637 → joined 03:33:49.784，当前成员状态 joined。对应 settled 预算行 1 条、实际 charged_reads 1，新增 unknown 为 0。广告新灰度真实成功回执与 active grant 此时均为 0，Work 为 54（READY 41、WAIT_MATURITY 13）。READY 状态数不等同于已满足健康、预算及依赖条件的可运行数。

PG 连续只读复查总连接为 8–9 / 25；瞬时 idle-in-transaction 的事务年龄 0.249–1.605 秒，重复采样时同 PID 的事务时间重置、SQL hash 变化，未发现持续长事务。网关出口映射无法单独追溯每个连接的源应用，未据此扩大连接池。

成功激活后的只读观察作业 PID 1038848 使用 `observations-v2-first` 独立目录。它从真实激活时刻起采样，计划最早北京时间 **2026-11-07 04:02** 完成 24 小时及 24 个完整 UTC 小时；最多 330 份哈希串联快照。原失败观察目录不覆盖。后续补充发布的镜像差异必须记录，不能把跨版本数据冒称为单一 r12 结果。

本地证据：`canary33-emergency-restore.json`、`canary33-v2-launched.json`、`canary33-v2-initial-handoff.json`、`acceptance-canary33-v2-initial-followup.json`、`canary33-v2-pg-readonly-20261105T193635Z.json`。独立 r13 的界面及可选计量补充仍在发布前验证，实际部署状态另行记载。

## 2026-11-06 04:12 后的补充发布与消费故障

r13 于北京时间 04:12:04.016 选择性更新 backend、growth、Celery 和 frontend，33 号选择器保持 true / 33。可执行库存、独立经营维护列表、有界可选启动和 whole-Work 计量已上线；启动 frontier 尚未证明，因此 strict_rate 和 guaranteed_lower_bound 均明确为 null。同步修复了审核 runner 与原扫描策略不一致的问题：原策略 exit_all_accounts=true 时，不能因旧 account_ids 列表未含 33 而排除该账号；人工暂停、enabled、execute 和 owner 门禁保留。

冻结 manifest 为 `e7626ab607d0a6df96960e3e554b164e536cf5cbd33db6f260a522cd9484fb34`。最初候选构建因源目录权限 600/700 未通过 appuser 无网络 smoke，未切换生产。仅规范化非秘密 build context 目录 755、文件 644 后重建未使用候选，旧镜像与失败证据保留；最终两 Python 镜像及前端隔离检查均通过。部署回执 supplement_started，受保护服务、计量 inode/大小/权限及原 beat 身份保持。canonical Compose 为 `1f0b69f1935052ad55599242b9c9b87a0ddba7b3cb2d900433590ddcc471c9df`。SSH 在成功后断开，已通过服务器回执和实际容器复核成功，没有重复 apply。

04:18 后审核确实进入会话 owner，随后不足一秒失败，Work attempt 仍为 0。早期瞬时队列观察不足以解释持续阻塞。只读 PG 日志查到 9 次 `FOR UPDATE cannot be applied to the nullable side of an outer join`：claim_next 的 TelegramAccount 查询隐式 LEFT JOIN api_config，未限定锁对象。生产日志未记录 SQLSTATE，原字段保持 null。隔离 PostgreSQL 18.6 在两个实际 r13 镜像中使用合成记录复现相同 SQL 结构和 claim_next:766，驱动错误为 0A000。

r14 只将该调用改为 `with_for_update={"of": TelegramAccount}`。隔离实际 claim 成功、attempt=1、重复活 claim 拒绝；相同账号、成员和 Work 的 NOWAIT 仍返回 55P03，关联 API 配置和其他账号可独立锁定；人工暂停保留。隔离环境未调用生产业务 DML、Telegram、AI 或 Redis，临时容器已移除，共享 PG 身份不变。两角色源码相同 SHA `d914ed5dbf9372062332b0bfb9515d07f8169aa7eed1359e91d5d885e48991bf`；生产派生核心及 hook 回归 main 81 通过、Celery 79 通过和 2 个角色不适用跳过，17 组部署保护测试通过。测试注册表排除本地未上线 P2/P4/P5 模块，没有为测试引入这些系统。

04:34:34 的计量来源纠正复核确认：实际 Celery 为 13 masters、18 children，20/20 个 backend/growth/Celery 来源身份均正确；先前单 master 假设已撤销。PG 使用应用权威 DSN 创建并释放单条只读连接，采样 14/25。growth pending=1、census last_scan=null、77 个 gap 均真实保留。来源身份通过不代表覆盖完整，严格 SLA 两项仍为 null。v2 探针 161 项离线测试通过，原失败探针与冻结证据保留。

r13 观察器 PID 1228619 已安全继任 r12 观察器，前序快照未覆盖；它从 r13 实际部署时点另起窗口。r14 修复后需要再从最终成功版本重新覆盖至少 24 小时自然业务及 24 个完整小时。原失败窗口、计量缺口、业务回执和未知结果均保留，不能把跨版本样本当作单版本验收。第二账号及矩阵扩容仍未执行。

r14 于北京时间 04:44:21.382 成功选择性更新 backend、growth、Celery；frontend 等受保护服务不变，beat 恢复。manifest 为 `9de9112c72a219ddd0df3ad5f836e1d8e3bbcda1c22ba768bcaabdee926a275c`，canonical Compose 为 `b51bc6c482f47e8663fa749d57b0bd563b71156d42fb21bebdc5632aec33718c`。两候选镜像均通过无网络构建及 smoke，实际健康检查 200、鉴权拒绝 401，7 个应用服务 running。首次 detached launcher 在启动前误读 prepared 回执的 status 字段，尚未创建启动标记或执行变更即拒绝；修正后独占启动并保存 PID/boot 回执，没有重复 apply。

04:47 的只读证据显示：33 号 owner review 于 04:46:19 开始、04:46:36 完成；两条 review 预算记录 settled、实际 consumed_reads 合计 5，原领取 SQL 错误不再出现。尚无 active grant。Work 1 已 attempt=1，但 reason=RuntimeError 被归为 WAIT_BUDGET；数值诊断定位 qualification_service:3564 的群身份错配，属于目标身份问题，不能推断为预算不足。随后其他数值诊断定位 read_work:370 的真实预算等待，二者需分别呈现。现继续修正已发现的技术等待分类及有限重试缺口，不清空预算或绕过身份验证。

04:47 的原账本快照为日总读取 3419/3600、critical_day=1629/900、critical_review_day=1627；现有贷款及历史超额未重置。05:03 的后续只读样本为 55 个 Work，其中 WAIT_BUDGET 48、WAIT_MATURITY 7，多数预算等待明确 ready_at 为 UTC 2026-11-06 04:26:47–48（北京时间约 12:26）。这是原滚动窗口的到期预测，不是恢复或成功承诺；到期仍需检查新消耗、平台和业务条件。该样本没有完整的 r14 后闭合 SDK 小时，SDK 数值保持 unknown，预算结算不冒充 SDK 进入。当前 grant、新广告成功回执和 r14 窗口内新加群链均为空，先前 r12 的真实加群链不被迁入 r14 分母。

此次观测还发现预算等待的 next_step_reads 被错误投影为 0：本地计划已计算，但连接取得前预算拒绝会重建 snapshot 并丢失计划。r15 正同时修正该持久化缺口和有限技术重试；无需增加读取或重新规划。完成与实际发布结果将另行追加，尚不提前记为通过。

## 2026-11-06 05:12 的计量诊断补充

对 r14 实际 main 镜像和固定源码做隔离 PostgreSQL 18.6 合成测试，确认新增遥测存在规模相关缺陷：17 个 Work、每页 16 个及 20 个来源时，空历史一页已有 319 条 SQL、约 0.35 秒；3840 条成本记录加 3840 条 SLA 记录均未触及 4096 上限时，无总时限的一页约 7.083 秒，其中 SQL 执行约 1.511 秒，其余 CPU/ORM/JSON 等耗时尚未细分。同样输入施加原 0.5 秒写入时限会触发实际 Timeout，取消清理后约 0.886 秒退出并全部回滚，census_pending=true、last_scan=null，未伪造完成。该测试只证明实现缺陷，不能据此称生产 PostgreSQL 慢或增加连接池。

随后 UTC 21:12:09（北京时间 05:12:09）的唯一线上只读计数为 account33 SLA 333、alias 368、cursor 333、unresolved 1、cost task 344 和 slice 97；远低于隔离 3840 规模。此时 source_pending_events 已为 0，last_scan_at 为 UTC 20:52:22.099448；此前 pending=1 的瞬时状态已发生变化。因此“线上一直 census_pending=1”已不成立，尚不能证明隔离超时正是生产缺口的原因。历史缺口和旧探针结果继续保留，strict_rate / guaranteed_lower_bound 未改为有效数值。

遥测改进按独立小补丁处理，重点消除事务内重复扫描和重复解析，并验证在原超时边界内可持久推进；不以加长时限、扩大池、清空历史或放宽完整性冒充修复。该补丁仍在验证，未发布。r15 的重试和计划保存已完成每角色 68 项针对性测试，并完成实际派生 main 81、Celery 79 通过（2 个角色不适用跳过）的现有核心及 hook 回归；已有等待行的兼容投影仍在收尾，发布包尚未冻结。

本地仓库合并单独验证：engine 可以按原 SHA 精确合并；本地 qualification_service 与生产版本不同，只将新 helper、身份 guard、catch 和已有计划保存的行为适配到本地 `_assess`。所有其他顶层函数及 AST 原样，原 CRLF 保留。本地真实 backend 导入另有 68 项通过，不以生产整文件覆盖本地的未发布功能。此证据与生产派生测试分开，不混为同一套源码。

| 交付项 | 当前验证状态 |
| --- | --- |
| SQL076、独立有效授权和唯一工作状态 | 已增量部署；原审计及幂等数据保留 |
| 本地计划、8 次 RPC 小步骤、事件版本、材料与 AI 复用 | 已部署；离线回归通过，完整自然业务成本比较待样本 |
| 新群自身准入、2 个首轮执行位、双保底 | 已部署；灰度已出现一次真实 SDK 至 joined 完整链 |
| 六角色 SDK 计量、广告机会分母、真实加群阶段 | 已部署；历史缺口保留，不能以预算记录代替 SDK |
| 可执行库存、经营维护单列及审核兼容界面 | r13 已部署 |
| PostgreSQL 领取锁错误 | r14 已部署，实际审核 owner 完成并结算 5 读 |
| 有限技术重试、身份错配暂停、预算等待计划 | r15 已最小部署；21 条实际 WAIT_BUDGET 行保存/显示均为 8，严格只读验证通过 |
| 遥测规模性能、严格启动分母与匹配成本验收 | 存在已证实现缺陷及证据不足，保持未验收 |
| 自然 24 小时、第二账号、矩阵及稳定 7 天清理 | 尚未完成；不提前扩容或删除旧路径 |

## 2026-11-06 05:34 后的 r15 发布核验

r15 最终实际派生源码完成 main 125 项通过、Celery 123 项通过及 2 项角色不适用跳过。两项跳过仅为 main/growth 长驻入口 AST 检查，main 已通过；不是删除失败用例。另有每角色 68 项有限重试/身份分类及 18 项只读兼容投影测试。独立复核确认全部绑定哈希一致，claim_next 与 _member_lock 的 AST 与已验证的 r14 相同。测试仅用隔离材料，无 Telegram、付费 AI 或生产业务写入。

冻结 manifest 为 `b5e0a89023470b43c77db56a9cb07d166cdf31554c743cf737ceeb39af11cc29`，归档 SHA-256 为 `0a857c7d34672c5c3501e23fe2b645a14c496c7a99911c6439753fcbfcf66be0`；两角色无网络构建与 smoke 均通过。改动只涉及生产派生的 qualification_engine.py、qualification_service.py：60/300/900 秒有限技术续试、第 4 次失败等待事件；身份错配固定为 group_identity_mismatch 且只失效对应成员授权；连接前预算拒绝仍保存已有计划。旧 WAIT_BUDGET 行仅通过只读兼容投影补出下一步需求，不写入、不唤醒、不修改 ready_at，不强制重审。

第一次 apply 在北京时间 05:34:17 以 supplement_failed_before_stop 拒绝，原因是 prepare 时记录的隔离遥测测试容器已正常清理，使受保护身份集合变化。只读核对确认三项应用服务和 beat 均仍是 r14 原容器/启动时间，没有停止生产服务。失败回执和身份差异证据保留。协调停止临时测试容器创建/删除后，第二次在独立状态目录重新 prepare 并启动；使用同一冻结包，未放宽任何保护校验。实际成功与健康结果以其后回执为准，不将 pending 视作发布完成。

第二次于北京时间 05:40:10.764 成功，回执 supplement_started、protected_unchanged=true、beat_restored=true；canonical Compose 为 `5ce2bcb12f0b27807f033fba7b3605ce7e7b2aeef81527ab5a3a120f0bc56127`。仅 backend/growth/Celery 镜像更新，选择器仍 true/33，schema、固定计量文件、预算、租约、事实和 unknown 未切换或改写。七项服务均 running、restart=0，健康 200、鉴权拒绝 401。成功回执位于本地 r15/deploy-attempt2；根目录同名回执仍为第一次 before-stop 失败，不能混用。

05:41:35 的首次只读消费样本为 Work READY 33、WAIT_BUDGET 17、WAIT_MATURITY 5，生命周期 attempt 合计 125；新发布窗口的广告授权、广告回执、加群链及预算结算均为空，不能把历史 attempt 当成发布后增量。七项镜像符合冻结记录。此时尚无完整 r15 SDK 小时，真实 SDK 和严格 SLA/匹配成本保持未知。健康日志存在 ERROR 标签计数，且共享 PG 有 35 条未分类 ERROR；共享日志没有账号归因证明，正在有界脱敏分类，不宣称“日志无错误”。独立数字诊断在本次发布后为空，也不能据此解释或忽略 PG 错误。

实际只读 runtime 投影探针首次返回 bounded_readonly_projection_failed，原证据已保留，未重审或修改旧 Work。离线两角色各 33 项测试证明代码路径，不能替代 PostgreSQL 实际调用。后续仅添加安全异常类型/位置诊断定位失败，不输出异常参数或原始 SQL，不把采样失败填成 8 读或零读。

05:48 的安全类型/位置诊断将探针失败定位为独立 Python 进程没有完成纯模型注册：InvalidRequestError → KeyError，SQLAlchemy clsregistry._resolve_name / _raise_for_name，尚未进入业务 SQL；没有 SQLSTATE。这是诊断夹具缺失，未据此修改应用。v2 显式导入与生产相同的纯模型清单并 configure_mappers，不导入 main/API、不启动 lifespan。两角色干净子进程在禁止联网、子进程和写文件的审计钩子下完成 mapper 与实际 SELECT 编译。

05:54:02 的 v2 实际只读验证成功：账号 33 的 21 条当前 WAIT_BUDGET 行，raw next_step_reads、保存的 read_plan.required_reads、job 和 blocker 投影全部为 8。READ ONLY 已通过 SHOW 验证，19 次 SELECT、非 SELECT 为 0；完整 Work 持久列和 ready_at 均保持不变，未调用 Telegram/AI/Redis。此样本均已有 r15 保存计划，未发现“缺 read_plan 的旧行”或“明确零读计划”样本；这两项仍以离线覆盖证明，不能把本次样本冒充它们的线上验证。证据保存在 r15/safe-error-diagnostics/runtime-v2-20261105T215402Z.json。

05:56 的日志 v1 分类样本进一步核查发现工具局限：它曾按整块日志（含 STATEMENT 和 JSONB 字面量）匹配错误短语，且没有从 PG 错误头提取 relation，可能误分类。因此其中的 311 个 check_violation、24 个 connection_limit 等是未经修正的诊断输出，不能当作实际数据库故障数或 Vanguard 归因。原结果保留；v2 将仅分类错误头，并以 READ ONLY pg_stat_activity 的当前角色/数据库和进程出生时间验证可关联 PID。应用 unknown 标记也须按确有的静态事件解析，未定位前不作账号故障结论。

严格启动分母还有实现层限制，不能写成“只等待 24 小时就会具备”：当前 qualification_sla_runtime 的 source coverage 显式将 transaction_frontier_verified 设为 false，来源心跳/空队列不能证明业务 READY 事务边界。r16 的计量性能修复不会改变这一字段，也不会伪造健康/可支付起点；严格 95% 启动率仍未具备验收依据，须独立补全可证明的业务提交边界。广告机会完成率和真实加群阶段继续按各自真实账本观察。

06:12 的最终冻结诊断 v2 已实际执行（工具离线 56 项通过）。有界 growth 样本识别账号 33 的 67 次 owner 阶段启动及 67 次完成，包含 join/ads/review/survival 调度，不是 67 次广告或加群成功；全部 owner 日志还包含其他账号及无法归因完成事件，不能用作严格 SLA 分母。共享 PG 的可见错误 PID 均因已退出/不可见或缺出生时间而未取得角色/数据库证明，保持不归因；PG tail 与 Celery tail 截断，绝不宣称覆盖完整或应用零错误。原候选分类和失败探针保留。

06:12:21 的同版本业务只读样本：Work READY 5、WAIT_BUDGET 40、WAIT_MATURITY 10，生命周期 attempt 179；新窗口预算创建/结算、有效 grant、广告回执和完整新加群链均为空。调度阶段完成不等同于业务成功。连接瞬时为 active 1、idle 11、idle-in-transaction 4，单样本无法证明长事务。广告机会当前 due=0 且存在 dispatcher_scan_gap / observation_window_not_covered，完成率仍为 null，不能将零分母当作 100%。r15 的第一个完整 SDK 小时须等 UTC 22:00–23:00 闭合；当前小时不参与完整计量验收。证据在 r15/consumer-observation/consumer-r15-followup-20261105T221221Z.json 和 safe-error-diagnostics/error-logs-v2-final-20261105T221209Z.json。

现有 denominator_loss 也只有置 True 的生产写入，evaluate 将其作为未恢复缺口，不会仅随观察时长自动清除。本次没有额外读取 33 号该标记，不能声称它已经置 True。新的提交边界方案必须保留旧缺口并建立可证明的新观察 epoch，不能直接清标记、改成 True frontier 或放宽分母。该补全另做独立离线原型，不混入 r16 遥测性能包。

## 2026-11-06 06:20 的稳定版本观察与未完成项

生产继续为 r15，r16 未冻结发布包、未构建应用镜像、未部署。独立遥测候选虽在低占用合成数据的单次 writer 测试中改善耗时，但满容量冷启动有超时及提交后取消的不确定边界，真实 Producer.run 的 17/55 Work 完整恢复尚无合格证据。后续隔离 fixture 初始化两次失败、runs=[]，不作为代码通过或业务失败。220 项本地测试、10 次 PG 尝试、清理及失败证据保留，shared PG 身份未变化；候选明确 HOLD。root 的 r16 派生树及发布工具仅为本地待审骨架，不能误称已上线。

为不继续推迟自然业务观察，已按用户原计划采用当前真实 r15 版本启动观察。06:20:11.930 安全继任 r13 观察器：旧 PID 1228619 的脚本、argv、boot/start 身份核对后，用 pidfd 停止并等待整个旧只读 session 退出；原快照和 gap 全保留。新 PID 1623442 为唯一观察器，实际状态目录 `/opt/vanguard/ops/r15-observation-reserved-b5e0a890`，tools 为 `/opt/vanguard/ops/r15-observer-tools-v1`。目录沿用准备期 reserved 名称；是否已运行以实际 handoff.json 为准。它使用 r15 第二次成功部署回执，不使用根目录的第一次失败回执。

观察从本次实际启动起覆盖至少 24 小时及 24 个完整 UTC 小时，当前最早结束点为北京时间 **2026-11-07 07:02**（UTC 11 月 6 日 23:02）；这是收集窗口截止点，不是提效验收或恢复承诺。每五分钟进行顺序有界只读采样，最多 330 份；不调用 Telegram/AI、不发广告或加群、不扩账号、不改预算和事实。样本不足或缺口未解决时必须延长或保持未验收。

06:22 首次只读核对确认新进程在运行，sample-000 已落盘且源文件哈希匹配；七项镜像与 r15 冻结记录全部一致。来源身份正确，来源完整性仍不通过，明确保留 active_source_pending_or_loss、completed_census_not_fresh、pg_coverage_gap_or_pending、retained_source_history_unresolved。首样本真实新加群链为 0，广告机会无完整分母，strict SLA 和匹配成本均 unknown；不将早前 r12 的成功链混入新窗口。原继承 collector 的 acceptance_limits 文案仅表示该 collector 无法证明这些指标，不能代替独立计量补全的实际上线状态。

剩余工作包括：可证明的 READY 事务边界与健康/可支付起点、匹配任务的等价成本基线、完整自然业务产出验收；在这些证据不足时不扩到第二账号，也不删除旧领取/完整重采入口。新授权与工作状态、目标自身准入、预算保底、分步取证和可解释界面已上线，但**整份计划尚未完成最终验收**。实施报告保留全部未通过项，避免把技术上线误记为效率目标达成。

## 2026-11-06 06:30 的加群保底复核

接续检查实际 r15 两角色源码，确认一处直接影响新群推进的预算归属缺口：新加入目标已有持久 `initial_collection_pending`，但 Claim / 草稿未将该来源传递给读取工作；首次取证仍以普通 review 预留，真实 SDK 读取也按 group_qualification / review 扣费。因此普通审核份额用完时，加入本身可能可支付，而同一新群的首个 8 读取证步骤却被挡住，两个首轮执行位仍可能持续等待预算。前文“预算保底已上线”仅代表分配器和加群操作已部署，不能据此宣称必要生命周期已完整覆盖。

同一分类复核还发现待确认加入的成员核对使用 `join_reconciliation` 工作种类，但预留默认按普通审核，而真实 RPC 按 join 扣费。候选修复限定当前真实新加入流程及其首次取证来源，保持业务工作种类、owner 身份和计量关联；预留与实际扣费必须一致。历史初审、迁移进度、成熟和 AI 等待不会因此取得加群保底。候选与测试在隔离 r18 目录中进行，尚未发布，不改变原日窗口、贷款、预算计数或未知结果围栏。

严格启动时延另有 r17 离线协议原型，尚未接入生产。事务提交边界和原时刻健康/可支付证明是两个独立条件；当前真实 stage_ready 调用没有资格证明，部分 ready_at 甚至早于其写入事务开始。因此不能靠增加一个 frontier=true 或等待 24 小时就得到严格 95% 的合格证据。

r17 已完成 54 项离线对抗测试，涵盖真实 SQLAlchemy 根事务、savepoint、提交/回滚、崩溃、乱序、来源替换及未知起点。2223 个应用/r15/r16 输入前后哈希不变，root 独立复核全部 16 个冻结文件哈希匹配。它只是一份隔离协议原型，尚未完成真实 mutator/driver 覆盖、提交前 origin intent、fleet bootstrap、并发容量及原时刻健康/预算证明，`strict_rate` 仍为 null。证据在 `tmp/growth-efficiency-20261106-r17-sla-frontier/`，不混入 r18 业务补丁。

06:38 的现有 r15 观察文件只读核对：唯一 PID 1623442 的 boot/start/argv/hash 均匹配，已保存 4 份样本，最近为 sample-003，源文件哈希匹配，七角色镜像一致。来源仍有历史 pending/gap、旧 census 和 watermark 缺口；新窗口完整加群链仍为 0，广告分母不完整。该核对没有重新采集业务、调用 Telegram/AI 或改动运行中观察器。

## 2026-11-06 07:19 的 r18 首轮保底修复

r18 已从实际生产 r15 选择性派生并上线，未经过 HOLD 的 r16。只修改 read_work、rpc_governor、qualification_engine、qualification_service 和 automation 五个模块。修复的实际反例是：原普通审核份额在 54 读/小时、540 读/日用完后，加群本身能支付 8 读，但同一新群的第一个必要取证步骤仍被普通审核预算拒绝。另有无目标预览遗留的 `12+24+reads` 门槛，以及成员核对预留和实际扣费类别不一致。

确认加入事务现在将原 attempt、成员加入代次和范围来源持久保存在已有 progress JSON。仅持有有效来源、当前领取及原账本证明的首个最多 8 读取证步骤，可按 join 预算预留和实际扣费；业务工作、计量关联及原账本 workload 仍是 review。原账本行先落盘，随后绑定唯一 read token，再进入 SDK。该 token 可完成本步骤，后续分页、新 token、成熟或普通续检恢复普通审核预算。取消、崩溃、缺账本/读数或不确定结算不能重新获得保底。正常零 SDK 续试仅在原 journal、已结算零读及全部当前围栏均可证明时重用资格。

仅旧 `initial_collection_pending` 标签而没有本次确认来源的历史工作，不占两个新流程执行位，也不能领取加群首轮保底。原 r12 成功 attempt 4201 不回填新来源。无目标预览暂用自身 8 读的保守估计，尚不是完整逐目标预测。成员确认保持只读核对与原不确定围栏，不会重新加入。

原 Lua 分配器、账本、journal、预算计数、贷款及窗口 TTL 均未重置；原总限额继续为 180/分钟、360/小时、3600/日。监听和其他分配没有另建扣费器。零 SDK 清理先在事务外进行最多 0.5 秒 Redis 查询，再在短数据库事务内重新核对全部来源、领取与账本围栏。历史日消耗及借额仍可能导致续检等预算等待，r18 不退款或提前释放旧窗口。

最终针对性测试 main、Celery 各 85 项通过且无跳过，使用真实隔离 engine/SQLite 账本、原 Lua、已安装 governor 和最终模拟 SDK，包含原默认预算、已用贷款/TTL、分钟/小时/日硬限制、取消和未知结果。既有组合 main 125 项通过，Celery 123 项通过、2 项角色不适用；17 组发布保护测试通过。旧 slot 夹具仅补入真实 confirmed 来源，断言未移除，原失败证据保留。所有这些是离线证明，不是实际加群或广告成功。

发布 manifest `e94b87a8a56969b584eb4a3bacc7c5fed7e2561799e307470ebfee0db8407e79`，归档 `334a263cd1b501f5ceb7cf766f7c91086cf98c1cbf12ba78e49ab5aaf693e931`。两角色无网络构建及隔离 smoke 通过。北京时间 **07:19:32.016931** 回执为 supplement_started、protected_unchanged=true、beat_restored=true，选择器仍 true/33；canonical Compose 为 `3bcab94433591bdb1d0002c706e75c86342429755a8a0bc11d3303bfcba7bbe3`。仅更新 backend、growth、Celery，schema、固定计量文件、租约、事实与 unknown 均未切换或改写。07:20 七项服务 running、restart=0，健康和前端 200、鉴权 401；共享服务身份不变。这些健康结果不代表提效验收。

服务器原始成功回执 SHA 为 `9bfde0c4b31d483de6324e3a8556032193aab4c8bd80598904606df73e75ef45`。本地 check_deployment 将 JSON 重新缩进并转换为 CRLF，副本 SHA 为 `c21a615c89b1150ea7098f3e1b914a3063f5671daa1570350ca7b7ca0823660a`。观察器外层首次哈希检查因此拒绝，但未停止旧进程。取回原始字节后逐字段相等、逐字节重现本地序列化，并经独审核对；外层绑定服务器原始哈希，冻结观察 bundle 与归档不变，未放宽业务输入检查。

## 2026-11-06 07:34 的 r18 独立观察与本地交付

唯一观察器已于北京时间 **07:34:43.621206** 启动，PID 1859868，状态目录 `/opt/vanguard/ops/r18-join-floor-observation-e94b87a8`。预检同时核对旧 PID 1623442 的完整 argv、boot/start、唯一 FLOCK 所有者及 inode；执行时再次核对，用 pidfd 终止后等待整个旧 session 排空，再启动一个继任者。旧文件清单、快照、失败和 gap 均保留，不将 r15 写成自然完成。旧最后样本到新启动间隔 193.425 秒明确记账。

新窗口至少覆盖实际启动和部署后 24 小时、24 个完整 UTC 小时及 120 秒结算；目前最早收集截止为 **2026-11-07 北京时间 08:02**。它只顺序读取现有业务数据库、计量和来源，不进入 Telegram/AI、不加群、不发广告、不修改业务、不扩容；经过时间并不自动验收。

07:36 读取已落盘的首样本确认实际 PID/boot/start/argv 匹配，source 文件哈希和七角色镜像匹配。r18 窗口内完整真实加群链 0、广告成功回执 0；本 collector 未查询有效授权表，不能由零回执推断授权数量。attempt 4203 于 07:26:25 跳过并 released，SDK/平台响应/成员确认均为空，不能算一次真实入群或 unknown 重试。Work 为 READY 27、WAIT_BUDGET 19、WAIT_MATURITY 9，没有在途 claim。广告机会 due=0，但 dispatcher_scan_gap/observation_window_not_covered 保留，完成率为 null。六角色来源身份正确，历史 pending/loss、旧 census、PG coverage 和保留历史仍使覆盖不完整；严格 SLA 和匹配成本保持 unknown。

07:38 已将五个变更逐项合入本地各自版本，没有用生产整文件覆盖未发布功能。本地真实 `assess → _assess → for_review/apply → engine/原账本/Lua/governor` 正反例包含在最终 **87 项全通过、0 跳过**组合中。414 个本地源码仅五个 overlay，419 个未触及函数的源码原文和 AST 保留；独审后按基线哈希备份并逐文件原子替换，应用后 414 个源码字节与已测试 derived 全部相同。回滚支持未被并发编辑文件的逆序异常恢复，不声称五文件整体事务或进程崩溃自动恢复。验证 SHA `59cae0af3634acd002e876feb251279aa3c2045e694ef6aa355aa671b57ec125`，独审记录 `ee7f0f87ae5a90b41d9a3fd2b5b0257eafb0de024f5ee8ff0eba89a49e225256`。未提交或推送 Git。

07:25 的原有界日志样本包含 growth 6、Celery 8 个未分类 ERROR 文字标记，不能据此称为账号 33 应用错误。07:37 另用严格 logger header 探针复采固定 07:19:32–07:25:10 窗口：growth 45 行中的 6 个标记全部是 JSON warning，含两条账号 33；Celery 本次仅 9 行且无标记，与先前行数不同，尚不能用零标记解释先前 8 条。原分类及差异保留。共享 PG 的 137 个错误头为 134 check violation 和 3 serialization，但没有有效 PID 出生时间/角色/数据库关联，继续不归因 Vanguard，不扩大池。

本次证据位于 `tmp/growth-efficiency-20261106-r18-join-floor/`：release-r18-final、deployment-applied-remote-original、runtime-health、observer-handoff/current、local-merge、independent-postdeploy-review 和 header-probe。r16 遥测满容量性能及提交后取消边界仍 HOLD，r17 提交 frontier 仍为隔离原型。最终自然业务、严格启动率、完整广告机会完成率及匹配任务成本比较尚未验收；第二账号、矩阵扩容和稳定七天后的旧路径清理未执行。

07:45 的独立范围扫描纠正前一探针边界：`--tail 2000` 与固定 until 组合不能仅凭返回少于 2000 行证明历史覆盖。新工具改为 `--tail all`、固定时间窗及 2 MiB/15 秒硬上限，仍将日志保留完整性标为 unknown。它通过原 block 哈希正向找回全部 growth 6 条及 Celery 8 条：growth 六条都是 pool.connect_by_id 的 warning；Celery 八条为五条 warning、两条 info、**一条真实 ERROR**。真实 ERROR 位于北京时间 07:21:14.131867，尚未定位静态模板或账号，不忽略、不称日志全正常。账号 33 另有 listener 连接 warning，继续与新目标跳过原因分别核对。新收据 `warning-header-probe-20261105T234514Z.json` 不覆盖任何旧结果。

07:50 固定 attempt 4203 的最小只读探针及独审已通过：一条 1+0 连接、REPEATABLE READ/READ ONLY 和语句时限均经 SHOW 核实，前后镜像、容器和源码 hash 一致。保存原因为 **telegram_read_budget**，07:26:25.166724 预留目标、07:26:27.750135 释放，没有 sent、SDK、平台响应或 joined 标记。当前历史审核债暂停为 false，日配额、滚动日配额和最小间隔检查可通过，当前目标无活跃成员或请求围栏；配置的 next_join_after 为北京时间 **12:26:47.750090**，尚未到时。这证明该次记录按读预算跳过，不能证明它当时本应可支付。动态完整准入及历史 Redis 可支付性仍未还原，不用当前数值补成历史通过。该预测不是恢复承诺，没有清预算或重放该尝试。证据 `join-attempt4203-probe-20261105T235000Z.json`。

07:54 已将 87 项回归提升为仓库内 `backend/tests/unit/growth_efficiency_join_floor/`，直接对实际合并后的 backend 运行全部通过，0 跳过。独审核对 236 条断言和 42 个参数化测试定义未减少，只适配包内相对 import、实际 app 路径断言和包内 SQLite 纯模型 fixture；原父 fixture 及其未上线 P2/P4/P5 引用均保持原样。实际源码 414 个文件继续逐字节匹配已测试 derived。JUnit SHA `fb42feeff49d3a9457a194950a0dd487834a44e2730c6181ba846b6c4a9ce0a2`；包内 README 提供直接运行命令和依赖说明。

07:56 只读核对已存在的第五份样本：唯一观察器身份、源文件 hash 和七角色镜像仍匹配；r18 完整加群链仍为 0，广告机会分母仍不完整，严格 SLA 与匹配任务成本仍 unknown。未重复采集 Telegram 或 AI，没有从这些零值推断成功率。

08:00 的角色源码绑定分类已完成：growth 403、Celery 402 个应用源码哈希与各自运行镜像一致，并在容器内静态读取已安装依赖源码，不导入业务模块或调用 DB/Redis/Telegram/AI。账号 33 的启动 listener warning 明确带有 `AccountSessionLeaseBusy` 类型；对应 pool 连接错误匹配 session 租约保护路径，未删除或强取租约。Celery 五条账号 33 warning 匹配 pool.acquire_by_id；唯一真实 ERROR 的有界后续行含 `ConnectionError` 类型前缀候选，但静态事件来源和账号仍 unknown，不能据此归因 33 或宣称已消失。分类保留候选歧义，不把通用模板误当唯一调用点；没有追加扫描或凭日志字面词改变业务。证据 `source-category-probe-20261106T000025Z.json`。当前可确认的新目标拒绝原因仍以独立 attempt 4203 的预算记录为准。
