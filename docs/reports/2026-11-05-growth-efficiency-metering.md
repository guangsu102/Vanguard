# 广告与加群效率计量补充

本记录仅描述本地实现与生产只读核对；不表示已发布或已证明吞吐提升。

## 生产只读采样

2026-11-05 23:18:49（北京时间），`oracle4c24g` 的计量文件大小与实际分配均为 16,261,120 字节，权限 0600；dirty=0，来源容量超限标志=0。进程槽上限为 32，当时使用 5 槽：backend 1、growth 1、celery 3；来源心跳距采样 0.05—26 秒。growth 当时存在约 0.3 秒前登记的待确认 SDK 调用，不能据此判定卡死。

backend、growth、celery 使用同一目录 `/opt/vanguard/data/review-read-metrics-v5`；reconciliation、resource-search、guardian 当时未启用计量、未挂载此目录。原三角色窗口不能宣称覆盖后三个服务的读取。七日环形文件当时有 30 个 gap 槽，此数字不是近 24 小时缺口数，也不能反推出历史缺口原因。

## 发布配置

以 `/opt/shared-postgres/stacks/vanguard/compose.json` 的现有服务为准，保留所有其它环境变量、Redis、鉴权和挂载。六个应用服务统一配置：

```text
REVIEW_READ_METRICS_ENABLED=true
REVIEW_READ_METRICS_PATH=/app/review-read-metrics-v5
REVIEW_READ_METRICS_EXPECTED_ROLES=backend,growth,celery,reconciliation,resource,guardian
```

每个服务均使用已有宿主目录的可写绑定挂载：

```text
/opt/vanguard/data/review-read-metrics-v5:/app/review-read-metrics-v5
```

| 运行时 Compose 服务 | REVIEW_READ_METRICS_ROLE |
| --- | --- |
| backend | backend |
| telegram-growth-worker-survival-continuity（按发布时现有服务名核对） | growth |
| celery-worker | celery |
| reconciliation | reconciliation |
| resource-search-worker | resource |
| telegram-guardian-worker | guardian |

三新增角色沿用现有 32 个进程槽。原角色 ID 1—3、时间戳、计数和文件大小保持不变；新增 ID 4—6 的首次覆盖时间使用原文件保留头部 3280—3303 字节。不得删除文件、清空计数或缩短来源租约来消除缺口。所有写入角色升级并验证后再推进现有语义切换标记；新增角色首次覆盖前的窗口不能视为完整六角色覆盖。

reconciliation 的常驻循环和 resource-search 的空闲管理进程增加显式健康源启动、退出关闭；guardian 复用已有 Telegram worker 生命周期。监控启动并不生成 Telegram 读取或业务成功记录。实际来源数需计入 Celery 管理进程、子进程和任务事件循环，保持小于 32；超限仍保守标记不完整。

## 本地实现边界

启用新效率流程的账号使用最多 64 行、队列上限 256、聚合等待 5 毫秒的批量写入。SDK 前的 pending 围栏和 SDK 后的持久化确认仍被等待。取消调用者或 writer、写入失败、溢出都不能把未知消耗呈现为完整零调用。只有实际磁盘线程结束后才释放 I/O 许可。

固定头部新增缺口原因、来源生命周期和写入耗时分桶；累计诊断覆盖全部账号，不属于当前业务窗口。旧历史 gap 不补算原因。收益核验以采集时真实 `REVIEW_READ_METRICS_EXPECTED_ROLES` 配置为准，要求每个角色有活跃来源，且对照窗口的角色范围一致。

管理端分开呈现有效授权、当前任务、所缺依赖以及所需读取。当前任务列表最多 500 个已加入成员关系，包含尚无最终审核单的初始任务。广告成功要求 `succeeded` 及正消息 ID；加群意图、真实 SDK 阶段、平台响应、成员确认分别统计。历史未记录阶段单列显示，既不推算成功，也不抹成零调用。

## 到期广告义务的自然灰度分母

`ad_opportunities.py` 复用持久 `SystemSetting` 记录计量，不创建发送任务、第二套预算或证据。growth dispatcher 取得自身锁后、账号资格/软件队列/预算分流前，以专用短数据库会话批量读取有效授权、成员、活动 campaign binding 和既有调度。直接广告入口也先采集。此会话不提交或回滚调用方未完成的业务修改；13 个成员的测试限制整批 SELECT 不超过 20 次，未调用 Telegram。

每个到期业务义务的稳定 ID 由账号、campaign、membership incarnation 和上次成功周期确定。预算不足、owner 不可用、软件排队、未知外部结果及调度 backoff 均保留第一次 `due_at` 和原 ID；重复审核和同成员关系重新授权不会增加分母。记录保留 due 时的 grant audit/version/scope/peer。广告日志保存实际使用的 audit/policy/profile/member/peer，真实正消息 ID、成功状态及 due 后的发送时间才能完成同一义务；旧 incarnation 或不同 peer 的回执不能完成它。授权更换后成功会保留实际授权并标明变更。

24 小时分母包括本窗口到期义务，以及窗口开始前仍未完成的旧义务。旧义务在本窗口才完成后仍计入本窗口，不能通过等待跨日让失败义务掉出分母。未完成原因独立显示，历史 pending 保留；completed 记录保留 7 天，每轮最多清理 1,000 条更早记录。每个 tuple 建立本轮内存索引，避免按群反复扫描所有历史行。

覆盖信息持久记录首次采集时间、最后扫描和断档。首次未覆盖完整 24 小时、扫描间隔超过 120 秒、采集或回执关联失败时，`completion_rate` 为 `null`，界面显示“未具备完整窗口”。空分母也不显示 100%。`business.ads` 仅为发送回执数，不作为 95% 到期机会完成率验收依据；应同时要求机会覆盖完整、读取六角色覆盖完整，再读取 `business.ad_opportunities`。

## 冻结运行角色的适配边界

main/celery 应用完整增长任务差异。resource/reconciliation/guardian 清除不完整的 strict 增长覆盖，仅应用配置、SDK 计量边界和进程生命周期适配。计量模块对增长灰度账户独立标记 phase2，严格完整解析 allowlist，不导入新 engine/models。

冻结版 reconciliation 对启用新效率流程的账户只转发到已更新的 session owner；无 owner、旧 self owner 或不可达时保留 pending，30 秒重试并回传明确 owner 等待原因，不执行旧版无新 claim/Work 围栏的本地写入。非灰度账户保留原 fallback。guardian 真实命令为 `guardian_bot_worker`，增长事件监听由已升级的 growth 进程负责。

离线验证覆盖三角色 governor 的成功、异常、取消和预算拒绝共 12 场景；owner-only 覆盖 6 场景和 5 种非法 allowlist。到期义务、API 和真实单槽 session owner 的后台 AI 隔离共 24 用例在 main 和 celery 的真实冻结源码叠加发布差异后分别通过，禁止回退到 checkout 的 app 源码。以上仍不代表生产自然窗口已完成或吞吐已提升。

当前任务的 `WAIT_BUDGET` 复用 `/runtime` 已取得的一次 purpose budget 快照，展示实际可用读取、所属预算窗口的 limit/used/remaining/ttl、观测时刻及预测重试时间。snapshot 不可用仍显示未知，已知零保留为零；不为每条成员任务增加 Redis 或 SQL 请求。40 条任务同时投射的测试仅调用一次预算 snapshot。
