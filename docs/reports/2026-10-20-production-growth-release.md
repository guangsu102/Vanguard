# 生产加群与广告修复发布记录（2026-10-20）

本次发布完成了会话隔离修复、资格状态修复和生产数据收敛。发布目标为 oracle4c24g，只使用 canonical Compose /opt/shared-postgres/stacks/vanguard/compose.json，没有停止或重建共享 PostgreSQL、共享网关、Vanguard Redis、前端或其他业务容器。

## 发布内容

基于生产镜像 local/vanguard:growth-ads-daily-quota-backoff-20261018T0900Z 构建增量镜像：

- backend/app/core/account/session_guard.py：捕获 Telethon update decoder 的 wrong-session 错误，触发现有会话隔离和断开。
- backend/app/core/account/pool.py：新客户端安装会话失败钩子，并在异常和取消路径释放租约。
- backend/app/modules/acquisition/qualification_service.py：审核允许/试投且 membership 已加入时，事务流程同步父群 status=active。

新增镜像：

- 标签：local/vanguard:session-qualification-repair-20261020T043930Z
- digest：sha256:9f97b40c01ec0eb71722a5f5beda49f6690022ef1c926a5678b0cf2647391a51

更新的服务：

backend、celery-worker、celery-beat、resource-search-worker、telegram-growth-worker-survival-continuity、telegram-guardian-worker。

运行时 Compose 经过 config --quiet 校验，使用 up -d --no-deps --no-build --pull never --force-recreate。发布前 Compose 备份保存在服务器 /opt/vanguard.file-backups/session-qualification-repair-20261020T043930Z/compose.before.json。

## 数据修复

原始审计清单中的 35 个父群最终全部为 active，且对应 membership 为 joined/approved/active。

- 23 个群在新代码处理存量审核时自动恢复。
- 11 个仍有未过期 completed/trial 授权、但最新续期审核排队的群通过事务回写。
- 第 35 个群（group 794）最新审核为有效的 trial 续期排队状态，未发现失效或保护标记，单独通过事务回写。
- 更新语句以 status='pending' 为并发条件，全部在事务内提交；没有清理审核、广告回执、预算或 Redis 数据。

## 验收结果

截至 2026-10-20 08:08–08:21 UTC：

- /health 返回 healthy，backend healthy；六个新服务运行中，重启次数为 0。
- 新服务使用同一镜像 digest；前端、Vanguard Redis、共享 PostgreSQL 网关的容器 ID 和镜像保持不变。
- 发布后 15 分钟内 growth、guardian、backend、celery 的 wrong session ID、Traceback、AccountSessionLeaseBusy 均为 0。
- 账号 2、33、34、35 均为 online，Redis 中四个 Telegram session lease 均在持续续期。
- 最近 24 小时广告：success 40、failed 2、skipped 1；pending/unknown 均为 0；逾期 survival pending 为 0。
- 最近 7 天加群：success 104、failed 8、skipped 32、pending 3。跳过/失败主要为 telegram_read_budget 19、join_wait_execution_capacity 13、join_failed 8。
- 账号 34 仍会出现读取预算退避和断开时 BrokenPipe；这轮没有再次出现 wrong-session 或租约竞争。

本地针对性回归测试为 76 passed；目标文件编译和 ruff --select E9,F 通过。完整 Ruff 的旧式 Optional/identity 风格问题未纳入本次发布。

## 架构改进路线

1. 统一资格状态机。把 Group、membership、qualification audit、ad profile 设计成一个有版本的状态机，所有状态转换走同一事务服务；增加数据库不变量：joined + approved + active + 当前有效授权必须能推出父群可投放状态。保留周期性 reconciliation，发现漂移后只执行有证据的修复并写入审计事件。
2. 单账号单会话所有者。每个 Telegram 账号只允许一个 session owner 和一条写入连接；监听、审核、加群、广告都通过 owner RPC 执行。使用 generation/fencing token，使旧 worker 即使存活也不能发送或续租；wrong-session 触发隔离、重连和告警。
3. 分离队列和预算。将监听、资格审核、加群、广告发送、survival 核验拆成独立队列，按业务优先级预留读取额度；采用 reservation、超时回收、老化和每账号公平调度，避免读取预算把广告或关键审核全部饿死。
4. 广告 durable outbox。发送前写入带幂等键的 outbox，发送后以回执和 survival 对账推进状态；worker 崩溃只进入 unknown/reconcile，不根据本地超时自动重发。把“意图、请求已发、回执、存活”作为不可逆阶段。
5. 事件 inbox 分区消费。按账号分区，记录 lag、最老事件、租约、毒消息和重放次数；超过阈值自动降级非关键任务并告警，避免 7948 条事实积压只能靠日志发现。
6. 建立 SLO 和发布门禁。监控可用 session 数、wrong-session rate、账号租约竞争、审核队列年龄、pending/unknown 广告年龄、eligible inventory、inbox lag、加群成功率和广告回执率。发布采用不可变 digest、单账号 canary、业务验收、自动回滚；数据库迁移与应用切换分开执行。

建议优先落地前两项和预算/队列隔离，再把 outbox、事件分区和 SLO 接入发布门禁。这样可以先消除本轮暴露的状态漂移与 session 竞争，再提升吞吐和故障恢复能力。
