# P0 control plane deployment

时间：2026-10-20 UTC（北京时间 2026-10-21）。

本次发布完成了指标、SLO、混合预算账本、独立 Redis Stream 对账队列和监控栈。生产使用 canonical Compose `/opt/shared-postgres/stacks/vanguard/compose.json`，没有停止或重建共享 PostgreSQL、网关、Vanguard Redis、前端或其他业务容器。

## 已上线

- 应用镜像：`local/vanguard:p0-session-20261020T180745Z`。
- backend、celery-beat、celery-worker、resource-search-worker、growth worker、guardian worker 和独立 `reconciliation` 均运行中，重启次数为 0。
- 067 migration checksum 保持 `cd57cacf02b02a39566a773605ddfd77dcfa53168ec033914365027d9450c1c2`；068 只新增 `reconciliation_task.verification_due_at`，重复执行和 checksum 校验通过。
- 账号 34 已进入 active canary：`RECONCILIATION_STREAM_ENABLED=true`、`RECONCILIATION_SHADOW_MODE=false`、`RECONCILIATION_ACCOUNT_IDS=34`。其他账号的历史任务仍保留在数据库并按 rollout filter 不消费。
- 对账只调用事实读取；未知加群/广告结果仍禁止自动重发。consumer 在数据库 CAS 提交成功后才 ACK Stream。
- Prometheus、Alertmanager、Grafana、node-exporter、cAdvisor、postgres-exporter、redis-exporter、blackbox-exporter 均运行中，7/7 targets 为 up。Grafana 16 个面板查询均返回数据；Alertmanager receiver 为 `monitoring-console`，不配置外部通知。

## 当前读数

- 预算快照持续刷新，最新快照年龄小于 3 分钟；预算账本已有 join reconciliation、renewal、review 工作单元并成功结算。
- 最近账号 34 已发送申请出现 `success/confirmed`；广告 pending/unknown=0，survival overdue=0，父群不变量失败=0。
- session lease contention 15 分钟窗口已归零。
- 仍有一个 `VanguardInboxLag` warning：历史普通 `message` inbox 有积压，最老事件从 2026-10-18 起，主要在账号 2、34。历史记录未批量改写；未知外部操作继续保持 evidence fence。

## 入口与安全

- Grafana：`https://vanguard.pipenai.xyz/monitoring/grafana/`
- Prometheus：`https://vanguard.pipenai.xyz/monitoring/prometheus/`
- Alertmanager：`https://vanguard.pipenai.xyz/monitoring/alertmanager/`
- 三个入口均要求 Nginx Basic Auth；Grafana 还要求自身登录；公网 `/metrics` 返回 404。
- 真实凭据只在服务器 root-only `/opt/vanguard/monitoring/private/`，包括 `access.env`、`runtime.env`、`grafana.env`、`postgres.env`、`redis.env`、`metrics.token`、`monitor-role.env` 和 `compose.env`。

## 观察与后续门禁

当前是账号 34 active canary，尚未完成 24 小时观察，也没有关闭旧 `growth_maintenance` 全量入口。扩大到全部账号前必须确认：对账 P95≤5 分钟、无新的 session lease contention、survival overdue=0、父群不变量=0、预算快照年龄≤180 秒，并处理或人工确认 inbox 历史积压。

生产备份、镜像测试结果和容器边界快照在 `/opt/vanguard/releases/p0-session-20261020T180745Z/` 及 root-only `/opt/vanguard/backups/`。隔离 PG/Redis 测试容器已删除。
