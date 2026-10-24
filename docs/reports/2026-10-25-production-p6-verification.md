# 生产 P6.3/P6.4 直接核查结果

日期：2026-10-25（Asia/Shanghai）\r\n方式：通过 `oracle4c24g` 上生产 backend 的既有数据库连接和内部 metrics 端点执行只读核查。未执行迁移、部署、Telegram API 调用、Outbox 写入或 Redis 修改。

## 生产版本与数据面

- `/health`：healthy。
- 公网 `/metrics` 未授权返回 `404`，生产 metrics 鉴权有效。
- `schema_migration_history` 最新为 `069_p1_runtime_reliability.sql`。
- `growth_operation`、`growth_outbox`、`growth_operation_attempt`、`qualification_snapshot`、`qualification_fact_version`、`p5_release_record`、`p5_gate_evaluation` 在生产数据库中不存在。
- `campaign` 与 `campaign_execution` 均为 `0`，因此当前没有可用于 P6.3 的生产 campaign/run。
- 生产有 8 个 Telegram account、1087 个 group；账号 ID `2、4、33、34、35` 当前 ONLINE，其余账号中 `5、31、32` 为 IDLE。该信息只用于核查，未触发 Telegram 操作。
- group 状态计数：`active=240`、`ad_blocked=8`、`join_failed=2`、`pending=348`、`pending_join=223`、`rejected=266`。
- 当前 `ad_delivery_account_id` 绑定数为 `0`。

## 生产门禁指标

从 backend 内部 metrics 读取，token 未输出：

- `session_lease_contention_total=47`，P6 要求为 `0`。
- join reconciliation：`pending=2`，`oldest_age_seconds=135717.888926`，约 37.7 小时，存在老化任务。
- ad reconciliation：`pending=0`。
- inbox reconciliation：`pending=2`、`manual_required=1`。
- `vanguard_ad_deliveries_total{status="unknown"}=1`。
- RPC read unknown：survival `1`、sync `2`、critical/review `2`。
- `sync_protection_triggers_total{reason="unknown"}=15558`。

## P6 结论

P6.3/P6.4 不能在当前生产环境开始。生产尚未部署 P2/P3/P5 schema 和应用控制面，也没有 campaign/run 或广告绑定可用于受控灰度；现有 session contention、join reconciliation 老化和 unknown 指标已经违反发布门禁。

本次核查没有改变生产状态。`P2_OUTBOX_ENABLED`、`P2_JOIN_OUTBOX_ENABLED`、`P2_AD_OUTBOX_ENABLED`、`P3_QUALIFICATION_SNAPSHOT` 继续保持关闭；不要通过手工插入快照、直接调用 Telegram 或绕过 Actor 来制造灰度条件。

## 必须先完成的生产准备

1. 按共享 PostgreSQL canonical Compose 以向后兼容方式部署并验收 P2/P3/P5 migrations `070–074` 和对应应用镜像。
2. 清理或解释现有 2 条 join reconciliation pending、37.7 小时老化任务，以及 1 条广告 unknown；session lease contention 必须回到 0。
3. 部署并验证 account + group + campaign/run scope guard，确认最多两个账号、两个 owned/Guardian 群和一个批准的 campaign/run。
4. 由真实前置资格 producer 生成 fresh、版本匹配的入群快照后，才可按单开关顺序进入 P6.3；随后至少观察 24–48 小时才能评估 P6.4。
