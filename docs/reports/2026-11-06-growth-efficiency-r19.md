# 广告与加群效率重构：R19 接续记录

本页保留 R19 的发布与验证历史。R19 于北京时间 09:31:29 上线，10:15:38 已由 [R20 三模块计量补丁](2026-11-06-growth-efficiency-r20.md) 接续，唯一新引擎账号仍为 33。独立授权、持久工作、缺失事实小步骤、新群自身准入、原账本双保底和兼容界面已上线。生产上线记录与历史证据见 [实施报告](2026-11-06-growth-efficiency-r12.md)，后续自然验收见 [灰度接续](../plans/2026-11-06-growth-efficiency-canary-runbook.md)。

## R19 调度修复

R18 的加群保底不能改变审核领取顺序。在实际 `claim_next` 的隔离回归中，新群的首轮取证排在 31 条历史 READY 工作之后。R19 只修改该领取排序并增加 `_first_join_priority`，不新增预算或判定规则。

- 优先级必须由真实、已确认加入来源及当前成员关系、加入代次、内容范围共同证明。
- 预算拒绝及明确证明零 SDK 的恢复保留首步优先；实际消耗、未知执行或完成本地检查点后恢复普通顺序。
- 原授权、owner、成员锁、版本、预算预留和 SDK 扣费继续独立检查；排序提示本身不授权读取或发送。
- main / Celery 冻结角色各 140 项通过；合入本地实际 backend 后，仓库套件 142 项通过，均无失败或跳过。
- 候选 engine SHA-256 为 `818f4b0273a76e19b76b4cc1ed2f88dde86224dec392e2840193e11fb0a42fbf`。其余 46 个原有顶层函数源码保持原样。

直接复现仓库回归：

```powershell
backend/.venv/Scripts/python.exe -B -m pytest -c pyproject.toml -o addopts='' -q backend/tests/unit/growth_efficiency_join_floor
```

这不是生产启动时延或吞吐提高的证明。

## 生产切换

R19 仅以精确补丁叠加原 R18 main / Celery 镜像，更新 backend、growth、Celery 三个应用角色。实际 manifest 为 `dbb7383ba9d75754a4eb386a4794b4a5771c3c8dc9a2edd344f88055997bfb9f`。原始部署回执 `deployment-applied-remote-original.json` 的 SHA-256 为 `b0d3e5febdd858f0818df0be39301ecc088c1bcae70182aeff7ad79e0faa3b44`。

部署前停止旧领取，宽限退出后再启动新角色；唯一选择器保持 `true/33`。原始回执确认未修改 schema、计量切换点、租约、预算、事实或 unknown；beat 恢复，46 项受保护容器身份保持。共享 PG、Redis、网关及无关业务均未重建。

09:32 的新镜像与 HTTP 检查通过，容器 restart_count 全为 0，鉴权入口保持 401。日志字面计数包含 growth ERROR 12、Celery ERROR 11 及一处 traceback，正在固定窗口分类；不能由 HTTP 成功推断日志正常。该初始检查不是自然业务验收。

固定 09:31:29–09:37:52 窗口的后续单次分类确认：growth 14 条均为 warning，33 号存在明确 `AccountSessionLeaseBusy`；Celery 共 249 条 warning、1 条真实 ERROR，末端异常候选为 `CancelledError`，账号及调用来源待定位。分类明细达到 200 条上限，截断标记保留；不能称日志全健康，也不能仅凭取消异常推断 R19 领取函数失败。回执 `warning-source-probe-20261106T014049Z.json`。

09:44:42 安全接续唯一只读观察器 PID 2281274。预检与实际停止均验证前任完整身份及独占锁，旧 session 排空后才启动后继。旧样本和原失败保留，151.321 秒跨版本间隔明确记账。新状态目录为 `/opt/vanguard/ops/r19-claim-priority-observation-dbb7383b`，最早收集截止为 **11 月 7 日北京时间 10:02**；到时仍须人工核验样本及指标，不自动验收或扩容。09:46 已核对首样本的进程身份、七角色镜像和来源哈希匹配，完整真实加群链仍为 0，广告完成率和严格 SLA 仍不可评估。

## 真实 PG18 验证

第一轮实际运行于北京时间 09:03，回执为 `tmp/growth-efficiency-20261106-r19-claim-priority/pg18-functional/actual-pg18-20261106T010314Z.json`，SHA-256 `45b4937e1f606d8f533a6f8fb2fd7e681bfc4973a2c9835b12c68d798b77af37`。

本轮失败，不能计为 20 场景通过。测试在 seed → enqueue → `_member_lock` 的 ORM 自动关联加载中出现 SQLSTATE `42P01`，尚未完成领取场景。纯外键闭包遗漏了 `TelegramAccount.operation_config` 的 selectin 反向依赖。修订仅补测试所需实际 ORM 表，不修改 engine、模型、约束或业务断言；原包和原失败回执保留。

第一轮基线、候选和场景脚本哈希匹配；临时 app / PG 容器已全部移除，共享 PostgreSQL 身份保持。隔离测试不连接生产数据库、Telegram、Redis 或 AI。

第二轮于北京时间 09:16 执行，**20 个真实 PG18 场景全部通过**。夹具仅新增 `telegram_account_operation_config`，20 场景函数及断言保持原样；候选 engine 字节不变。原始回执为 `pg18-functional/attempt2-orm-eager/actual-pg18-20261106T011618Z.json`，SHA-256 `fdb42d98196242c8148fdfa0feb433d87cdc6923502d3e473572a35d38fcca26`。临时容器全部清理，共享 PG 身份保持。该结果验证领取功能，不验证生产 120 秒启动率或吞吐。

## 补充计量的独立阻塞

R16 recovery-v3 的实际 PG18 六场景为 5 通过、1 失败。55 工作场景在第三个 census 容量步骤两次超出 0.5 秒，游标停在 32，缺少完成扫描标记。不能以本地 SQLite 或其余五例通过替代此失败。

原始恢复回执为 `tmp/growth-efficiency-20261106-r16/recovery-v3/pg18/six-case-suite/transport-recovered-receipt-20261106T005915Z.json`，SHA-256 `c4c55f0963827de89cd7ad3188afd262a64c7ac49317da0fea8f94813195fd20`。

recovery-v4 正在将 cost 容量检查的重复 PostgreSQL JSON 解析合并到一个语句；仍保留 4096 行容量、每轮最多 16 行整组回收、0.5 秒 writer、300ms statement 和 2+1 连接池。异常 JSON、未结算引用、取消或提交不确定继续标记 unknown，不删历史缺口，不修改预算。

北京时间 09:19 执行的 v4 full55 单例已真实通过 PG18；原始回执为 `tmp/growth-efficiency-20261106-r16/recovery-v4/pg18/actual-pg18-20261106T011956Z.json`，SHA-256 `9471dc6b84cf867936df925417d329d3e7e4a1060cb91a2bd29deea732eead36`。原限制未放宽，临时容器全部移除，共享 PG 身份保持。完整六场景与发布仍待完成；它也没有将严格 SLA 或自然业务验收改成通过。

北京时间 09:24 执行的完整六场景现已**全部通过**，包括 full17、full55、提交前重试、提交后超时、取消重启及 JSONB 未知引用。回执为 `pg18/six-case-suite/actual-pg18-20261106T012449Z.json`，SHA-256 `8cc51d2de9ac6d69f259c69bde12dfb5252d77b90608580da44e062225579072`。正常 full55 的 25 次写入全部成功，最长 0.3784 秒；注入故障仅出现预期失败且完整恢复。未知引用场景保留原 4096 条记录且未伪造新 admission。严格 SLA 继续为 not_evaluable/null。三模块将按本地 R18 精确基线合并，补充计量尚未进入生产。

三模块现已按实际本地基线 SHA 精确合并，原件保存在 `recovery-v4/local-merge/before`。实际 backend 的 **339 项组合回归全部通过**，其中包括完整 142 项加群测试；新增 8 项 first-fit、guard、溢出保护测试已进入仓库并单独通过。原 census 的 36 条断言及 35/18 项样本保留，只调整分页批次期望。首轮组合的模型注册失败及所有旧 PG 失败均保留，没有减少断言来取得通过。

精确交付绑定见 `tmp/growth-efficiency-20261106-r16/recovery-v4/final-local-handoff.json`，SHA-256 `f8e2702b01eb0e57874e17f08cdf97d82a0306e50d7e0edde85b6feb748b3fb2`。该补丁目前仅本地合并，未进入 R19 生产镜像。

## 自然业务与剩余交付

北京时间 09:06 的已有 R18 样本中，完整真实加入链为 0。广告机会分母存在 dispatcher 扫描及窗口覆盖缺口，完成率为 null；严格 120 秒启动率及匹配任务成本仍 unknown。北京时间 09:10 核对唯一观察器身份、来源哈希和七角色镜像均匹配。

现有等待和历史计量不能恢复原 READY 时刻的健康、可支付性及完整事务事件。R17 frontier 仍是未接入生产的原型，不能将队列为空、当前可支付或后来的成功预留补记为原时刻通过。

R19 须在实际 PG18 领取场景通过后才能发布；计量容量候选须独立通过实际边界测试。继续仅 33 号自然灰度，未满足真实加入确认、成熟先例、完整广告机会及匹配成本前不扩第二账号。全量稳定七天后的旧路径删除尚未执行。
