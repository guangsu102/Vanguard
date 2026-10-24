# P0 指标、预算账本与独立对账运行手册

## 组件边界

`backend` 和独立 `reconciliation` 进程通过 Redis 原子计数共享业务指标；每 30 秒由一个实例聚合 PostgreSQL 快照。Redis Lua 仍是实时预算授权真源，`account_budget_reservation` 只记录工作单元边界，避免按 Telegram RPC 写 PostgreSQL。`reconciliation_task` 是持久任务真源，`vanguard:reconciliation:v1` 只负责唤醒。

对账消费者在 `RECONCILIATION_SHADOW_MODE=true` 时只生产任务，不消费。未知加群和广告结果只做精确事实核验，不能按超时自动重发；无法证明时进入 `manual_required` 或保持原业务记录的 evidence fence。

群元数据刷新同样服从 session owner：如果账号已有监听进程持有会话，刷新按 `group_metadata:{group_id}` 转发到 owner；owner 不可用时任务退避，不在第二个进程创建 Telegram client。

对账 SLO 从当前事实核验周期的 `verification_due_at` 起算；`created_at` 保留任务最初创建时间。只有已经确认发出且事实核验仍等待管理员审批的加群申请，才可以把下一核验周期安排在已有 `reconciliation_next_at`。读取预算不足、网络失败、消费者崩溃和未知写入的重试不会重置周期起点；未来的审批复查不会误触发消费者停滞告警。每个完成的核验周期按任务 ID 和周期时间幂等记录延迟。

## 监控台

- Grafana：`/monitoring/grafana/`
- Prometheus：`/monitoring/prometheus/`
- Alertmanager：`/monitoring/alertmanager/`

三者都由现有 Nginx Basic Auth 保护，Grafana 还要求自身登录。Alertmanager 首期 receiver 为空的 `monitoring-console`，告警只在监控台显示，不配置外部消息渠道。

Prometheus 使用 UID 0 读取 root-only scrape token，并移除全部 Linux capabilities。首次创建数据卷后，应仅将 `vanguard_vanguard-prometheus` 卷的根目录所有者设置为 `0:0`；不要修改任何 PostgreSQL 或 Redis 卷。

## 生产私有文件

在服务器 root-only 目录 `/opt/vanguard/monitoring/private/` 保存 `runtime.env`、`grafana.env`、`postgres.env`、`redis.env`、`metrics.token`、`access.env`、`monitor-role.env` 和 `compose.env`，权限为 `600`。`access.env` 保存 Nginx Basic Auth 与 Grafana 登录凭据。模板在 `monitoring/private/*.example`，禁止把真实值复制回 Git。

PostgreSQL 监控账号应是单独的 `vanguard_monitor`，只授予 `CONNECT`、目标数据库的 `pg_monitor` 观察权限和合理的连接上限；不要复用 `vanguard_app` 密码。Redis exporter 使用现有 Vanguard Redis 私有凭据。

## 发布顺序

1. 备份 Vanguard 数据库、canonical Compose 和 Nginx 配置，记录 SHA-256；不要停止共享 PostgreSQL、网关或 Vanguard Redis。
2. 先上传并校验应用镜像和本目录配置，运行 `docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json config --quiet`。
3. 只应用 `067_growth_slo_budget_reconciliation.sql` 和增量 `068_reconciliation_verification_slo.sql`，确认 migration history checksum 后再打开 `BUDGET_LEDGER_ENABLED=true`。067 上线后的 checksum 保持不变；068 只新增核验周期列，不改写历史业务记录。
4. 先保持 `RECONCILIATION_STREAM_ENABLED=true`、`RECONCILIATION_SHADOW_MODE=true`，启动独立 `reconciliation` 服务，观察 30–60 分钟的 snapshot age、Stream lag 和任务重复率。
5. 将 Nginx snippet 放入现有 TLS server，执行 `nginx -t && systemctl reload nginx`；确认 `/metrics` 返回 404，三个监控路径需要 Basic Auth。
6. 账号 34 灰度时设置 `RECONCILIATION_SHADOW_MODE=false`、`RECONCILIATION_ACCOUNT_IDS=34`，观察至少 24 小时后再扩大范围。旧 `growth_maintenance` 对账号 34 的入口会被过滤，其他账号继续沿用旧路径。

只启动新服务时使用 overlay 的明确服务名和 `--no-deps`。不要对 overlay 执行 `down`，也不要执行包含 `postgres`、`redis` 或现有业务服务的全栈 `up`。

## 排障

- `vanguard_budget_ledger_snapshot_age_seconds > 180`：检查 reconciliation worker 的数据库连接和 Redis，先关闭 `BUDGET_LEDGER_ENABLED`，保留已写入账本。
- Stream oldest age 超过 1800 秒：检查 `XPENDING`、`XAUTOCLAIM`、数据库 lease；不要直接删除 Stream 或把 pending 任务改为成功。
- unknown 广告/加群：查看精确 `source_id` 的 Telegram 事实和 `manual_required` 任务；不要点击“重发”类旧入口。
- `parent_group_invariant_failures > 0`：暂停新广告准入，修正父群状态与 membership 审计后再恢复。

任一 SLO 恶化时，关闭 feature flag 即可停止新账本/消费者行为；不清空 Redis Stream，不删除 PostgreSQL 账本和对账任务。
