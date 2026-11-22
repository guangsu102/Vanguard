# 广告与加群效率重构：R20 计量恢复

当前生产已由[R21](2026-11-06-growth-efficiency-r21.md)于北京时间11:29:34接续，仍仅33号灰度。下文保留R20实际发布与观测记录，不代表R21验收。

R20 于北京时间 **2026-11-06 10:15:38** 完成生产切换，仍仅以 33 号运行新引擎。独立授权、持久小步骤、按缺失事实取证、新群自身准入、双保底及兼容界面沿用已上线实现；R19 的新群首轮领取优先级保持原样。本次只覆盖三个计量模块，修复容量接近上限时的恢复与结算成本，不改变读取总额或业务判定。

## 实现与验证

- PostgreSQL 容量检查复用一次有界 JSON 解析与整组选择，Python 最终校验仍保留；最多回收 16 行，未结算引用、异常 JSON 和溢出继续保守保留并标记 unknown。
- writer 0.5 秒、statement 300ms、4096 行容量、两次尝试、连接池 2+1 均未放宽；不重置历史 gap、计数、窗口或贷款。
- 复用 recovery-v4 六类真实 PG18 成功证据，包括 full17/full55、提交前重试、提交后超时、取消重启和 JSONB 未知引用。该 PG 测试使用先前父镜像，未声称在 R19 新父镜像重跑。
- 两个精确生产角色分别 **337 项通过、无失败或跳过**；正式本地工作树普通 pytest 入口 **339 项通过**，另有直接遥测入口 197 项通过。
- 首轮精确角色各 339 项中有一项失败：本地关闭确认后的读取预留已改为 8，实际 R19 保留 24。本次不覆盖该模块。原失败留存，最终精确角色使用原 R19 已验证的 140 项加群测试及当前 197 项遥测测试；本地 8 次读取断言保持。
- 正式遥测测试目录增加纯 ORM 模型注册，不再依赖临时 runner 注入模型。没有导入应用生命周期或 P2。
- 17 组继承部署模拟通过，独立审查确认父镜像、三模块范围、三服务、灰度选择器及回退保护一致。

## 生产结果

发布目录为 `tmp/growth-efficiency-20261106-r20-telemetry-recovery`，清单 SHA-256 为 `ce278a1e19794f716630f63842a3f5a9316fb25c47b6c40ee1630a056ffa871d`，压缩包为 `4b0cdb1143ad6a7e1783499f2f623382c5d47916743411612d6ae6c4b88430e4`。

实际镜像：main/growth `9888c7d84592bd7368d9f24abedb8e84ac45f2217caf34ca1e209facdc023948`；Celery `bd75826c4b46953dd185e82aad6926fac8f1c0073ac1216fb273acfabe88067b`。领取 engine 仍为 `818f4b0273a76e19b76b4cc1ed2f88dde86224dec392e2840193e11fb0a42fbf`。

使用唯一运行时 Compose `/opt/shared-postgres/stacks/vanguard/compose.json`，最终 SHA-256 为 `b033de75a1ed8a20630defceb0799201275d44defaf81b2c32de4bbd4dd60289`。仅切换 backend、growth、Celery 三个应用服务。原始部署回执 SHA-256 为 `6aa62adb657b4c9d2b7fe5143e469b4bd184961af7519bb6ecdd96525b204389`。

回执确认 `selector=true/33`、`protected_unchanged=true`、`beat_restored=true`、`receipt_written=true`，schema、计量切换点、租约、预算、事实和 unknown 均未修改。46 项受保护容器身份保持，共享 PostgreSQL、Redis、网关与无关业务未重建。

10:16 健康检查：本地与公网健康、前端均 200，鉴权保护 401，新镜像匹配，容器重启计数均为 0。此短窗口未见 traceback，但日志文字 ERROR 计数为 growth 8、Celery 3，不能据此声称全部日志健康，也不能仅凭文字计数判定真实异常。本次恢复补丁未被宣称会消除之前关闭连接时的 CancelledError。

对 10:15:38–10:22:04 固定窗口执行的一次脱敏分类确认：growth 为 10 条 warning，Celery 为 281 条 warning、1 条真实 ERROR；Celery 明细达到 200 条上限，截断标记保留。唯一 ERROR 定点核验为 `CancelledError`，五个来源匹配的帧均属于 SQLAlchemy/asyncpg 的关闭与终止连接路径，没有应用帧。这不是因果或无害性证明。分类回执 `warning-source-probe-20261106T022316Z.json`，SHA-256 `993641aeb21d9142e6c4114c1d291758166b27cb06f2cd49d24d1477466ee59c`；未再扩大日志扫描。

## 自然验收与剩余工作

R20 唯一观察器已于北京时间 **10:26:28.694380** 启动，PID **2415805**，状态目录为 `/opt/vanguard/ops/r20-telemetry-recovery-observation-ce278a1e`，最早收集截止 **11 月 7 日 11:02**。接续前重新核对前任完整身份及独占锁，旧 session 排空后才启后继。旧样本和失败全部保留；前任最后样本至新启动的 **62.224820 秒**间隔单独记账，旧窗口未伪记自然完成。181 项观察器回归全部通过，无跳过；到期不自动验收或扩量。

10:30 只读核对首样本：完整进程身份、七角色镜像及来源哈希匹配；完整真实加群链为 0。广告机会 `due=0`，因 `dispatcher_scan_gap` 和 `observation_window_not_covered` 保留 `complete=false/rate=null`；源计量仍有 census 新鲜度、持久 gap/pending 与历史来源未解决标记。低读取或零失败不作验收。读取已有结果的命令见灰度接续文档。

严格 120 秒启动率仍为 unknown：历史 READY 缺少原时刻健康与可支付性证明，R17 全局 frontier 原型未接入生产。广告机会覆盖及匹配整任务成本也不能凭等待或缓存命中自动补全。真实加群 SDK → 平台响应 → 成员确认、广告成功率目标和读取成本下降仍需自然样本验证。

后续只在既有工作状态与 writer 内补齐未来事件原点及回执采集，不增加读取预算或新的业务门禁。33 号样本不足时延长观察；通过后才扩大第二账号。全量稳定七天后清理旧路径的阶段尚未开始。

参见 [灰度接续](../plans/2026-11-06-growth-efficiency-canary-runbook.md) 与 [R19 历史记录](2026-11-06-growth-efficiency-r19.md)。
