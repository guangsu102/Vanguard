# r9：晚间新增缺陷修复与上线核验

2026 年 11 月 4 日，北京时间。本轮执行用户“开始修复”的授权，修复晚间复核已确认的恢复与缓存缺陷。r9 三应用于 **20:01:34** 启动，20:02:35 完成代码、维护与保护检查，并记录新代码版本边界。实际降读收益仍未通过验收，不能把修复上线或低读取当成额外减少 30%。

## 修复内容

1. **监听事件统一进入有界技术恢复入口。** 耗尽任务收到新 membership/rules 事件时，listener 只保留持久事件提示，由统一入口核对当前加入作用域、事件版本、运行等待，再打开新窗口。兼容旧 r8 已经排成 queued 的耗尽任务；同一事件重放不重新获得窗口。保留每个被接纳事件最多三次技术失败及 24 小时恢复期限，不清空账号配额，不确认消费尚未处理的事件。
2. **预算前完成可安全判定的本地收尾。** 到期 running/reviewing_ai/waiting_ai 的 AI 最终拒绝，不再不断经过预算、增加 attempts 和续 claim 后从旧 AI 分支早退；已耗尽的技术恢复也能在读取预算检查前终结。保留有效 claim、运行退避、人工状态、真实成熟与续采等待。当前事实或资料变化时转入重采，不把旧拒绝当成新材料的结论。
3. **防止错作用域材料关单或反复卡住。** 独立审阅进一步发现外层/续采快照可能属于旧账号、群或加入范围。现在核对真实 Group、Membership 与快照作用域；仅材料失配时明确重排并强制取新证据，采集内存中排除旧材料，持久旧材料保留到正常结果写回。真实审核作用域已变更时保持保守，不伪造新 scope、不授予广告权限。
4. **普通缓存清理使用本轮新计数。** 清理部分过期记录后，先同步工作索引再判断容量压力，并仍在同一原子提交中写回。清理后低于阈值时不误淘汰新鲜缓存；确实超额时继续按原策略淘汰。保护槽及移交协议没有因此改变。

本地收尾不发送广告、不退出群、不刷新 checked_at 伪造新结论。配置级群保护继续由既有发送/退出门控执行；本轮没有把该配置扩展为冻结所有纯本地审核状态收尾。

## 验证及限制

| 验证对象 | 本轮结果 |
| --- | --- |
| 冻结 r9 45 模块组合：计量、调度、恢复、存储四组 | **348 passed**，0 failed/error/skip/deselected；实际导入及输入 SHA 不变 |
| 实际工作树：正式 174 + 新增恢复 69 | **243 passed**，0 failed/error，保留未发布改动 |
| 精确 r8 + 本轮恢复增量的扩大集 | **177 passed，1 个原基线失败**；不称全绿 |
| 旧 r8 恢复缺陷负对照 | **10 个真实行为断言失败**，0 errors；不是因新模块无法导入而失败 |
| 旧 r8 缓存负对照 / 修复后的完整存储集 | 旧源 1 failed、1 passed；新源 **103 passed** |
| 两轮真实 runner → assess → pool 取证入口 | 8 场景通过；另有独立外层/续采 2 场景证明，未建立 Telegram 连接 |
| 两个候选镜像的断网隔离预算 Lua | 各 7 个场景通过，各测试消耗 24；未接触生产 Redis |
| 冻结包独立检查 | 45 模块、45 个 overlay 成员、50 个发布包成员及全部 SHA 一致 |

扩大集保留失败为 `test_durable_approval_reactivates_parent_group`：测试预期父群 active，旧 r8 和新源都得到 pending。已独立复现旧源失败，并证明对应父群写回段未变；未修改或跳过原断言。它仍是单独的基线测试缺口，不计为本轮通过。早期并发负载下的一次 0.15 秒 owner deadline 用例未进入 RPC，以及工作树未发布 stale lane 抢先改 timer 的三条失败，均保留早期记录；完成兼容调整后的最终工作树 243 项通过，没有放宽原业务断言。

SQLite/假网络验证不能证明真实 PostgreSQL 并发锁的全部交错，也不能替代生产自然成熟和 UNKNOWN 截止接管。测试数存在重叠，不相加成独立用例总数。

## 发布边界与核验

- backend/growth：`local/vanguard:review-v5-repair-20261103-main-r9`；Celery：`local/vanguard:review-v5-repair-20261103-celery-r9`。
- 规范 Compose 仅替换三个应用镜像。保留账号 34 的九个优化开关及新证据接纳；2、33、35 仍 baseline。
- 共享 PG18、网关、Redis、前端容器的 ID、镜像、启动时间、授权环境和实际挂载保持。数据库连接池仍 2+1；未做迁移、全栈重建、预算重置或推广探测。
- 三应用 **45×3=135** 个 SHA 全匹配；维护心跳均晚于各容器本次启动。公网前后端从本地客户端 HTTPS 检查均为 **200**。
- 原 32 份保护证据的 records/metadata 前缀及稳定字段完整，220 条合法追加；缺失、非法追加和作用域错误均为 0。此时尚未到最早 **20:44:03** 成熟及次日 **02:44:03** deadline，不能把保留证明当作成熟/截止路径通过。
- PG 挂载列表原始哈希曾不一致。保留原严格失败记录后，独立只读检查证明同一容器的三个相同挂载记录之六种排列同时命中 pre/post 哈希；仅排列不同，没有忽略挂载字段变化。

生产源码只涉及六个限定模块，其中新增 `qualification_local_recovery.py`；其余 r8 overlay 原样继承，并显式纳入原先由基础镜像继承的 listener。未发布工作树中的其它 actor/checkpoint 等功能。

发布 manifest SHA：`03402bb75e29b11909e1562eaf19616d628fe33405b8fbaf288e7709809e6152`。发布包 SHA：`ce86df854bd5eff18a3e9ef686be6f5a7679e754a543513f02beacc7881e427b`。服务器私有目录：`/opt/vanguard/backups/review-v5-repair-r9-20261104`。原 r8 Compose 和镜像保留，可用本目录 `remote_r9_release.py --run rollback` 仅回退三应用镜像，保留原灰度配置、保护数据和计量。

## 自然运行及实际读取

20:03 首次只读观察中，2、33、34 号监听及 owner 正常；35 号重启后为 error、未持有 owner，已单独交由既有恢复路径诊断。该初始样本保留，不用应用 healthy 推断四账号均在线。执行器队列为空，未发现过期/失联调用候选；切换后的短样本尚无新增广告或加群成功，不能认定业务无阻塞或把无推进解释为节省。

**20:12:44 最终复核：四账号数据库状态均为 ONLINE，监听连接及 owner/session 均已恢复。** 35 号原先被 error 状态过滤；updated_at 早于本次容器启动，但该字段不证明错误发生时间，现存有界日志未能定位历史错误类别。恢复前确认风险、静态代理、租约及预算符合既有恢复条件，然后仅调用一次 `POST /api/accounts/35/connect`；认证身份核验成功，无重试、fallback、手写状态或配额清零。

最终自然业务记录为 **33 号广告成功 1 次，加群新增成功 0 次**。owner 队列有 1 个正常 survival 请求，过期/失联候选为 0；不能把非空正常队列称为阻塞，也不能把监听恢复当成全部审核已完成。三个应用启动时间未变，三个维护角色的心跳相对首次样本均前进约 480 秒且仍新鲜。32 份原保护前缀及稳定字段完整，合法追加增至 **270 条**，缺失及非法作用域/种类/记录仍为 0。

计量文件 inode/大小、原 SDK 与业务语义 fence **17:14:42** 均与发布前一致。新版本边界仅以 `O_RDONLY`、共享锁读取 4096 字节头记录，没有调用会修改文件维护状态的 snapshot，也没有重置计数或语义起点。20 点小时横跨 r8/r9，不能作为单一 r9 稳定窗口；r9 首个完整代码小时从 **21:00** 开始，完整 24 小时读数最早为 **11 月 5 日 21:00**，仍须来源完整且业务可比。

本轮没有重复旧成本采样，也没有用负对照用例数量折算线上节省。上一轮 17–18 点记录 917 次 SDK 进入、13–14 点参考 759 次，属于不同健康/工作量且存在计量缺口的窗口；既不能宣称已下降，也不能将 +20.8% 当优化因果效果。额外减少 30% 的正式降幅仍未知，合格三组对照仍未齐。

完整公共材料/跨账号等价共享、健康时长/到达量/积压端点/p95 独立来源、矩阵范围读取 coverage、旧 KeyError 精确根因及自然成熟/UNKNOWN 接管仍是整体方案的后续缺口。此次修复不等于方案全部验收完成。

## 证据

本地有限、脱敏工件位于 `tmp/review-v5-repair-r9-20261104/`：

- `prepared-release/manifest.json`、`package-manifest.json`、`frozen-group-tests/r9-final/result.json`。
- `recovery/final-evidence.json`、`recovery/README.zh-CN.md`、`recovery/parent-group-baseline-proof.json`。
- `storage/README.md`、`storage-review/README.md`、`storage-review/scope-refresh-progression.json`。
- `audit/frozen-package-audit.json`、`audit/boundary-comparison-reconciled.json`、`audit/pg-mount-order-proof.json`。
- `stage-result.json`、`activate-result.json`、`verify-result.json`、`measurement-boundary-result.json`。
- `runtime-followup.json`、`public-http-external.json`；源站主机 urllib 尝试另留 `public-http.json`，不据该来源失败判断公网不可用。
- `runtime-followup-final.json`、`final-runtime-summary.json`、`recovery-runtime/account35-action-result.json`：最终四账号、维护推进、自然业务及 35 号单次受控恢复。

前置问题与成本口径见[晚间复核](2026-11-04-review-read-v5-evening-recheck.md)；验收门槛见[收益验收协议](2026-11-04-review-read-v5-benefit-validation.md)。
