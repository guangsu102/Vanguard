# P6.2 隔离 Shadow 与 P3 读取缓存验收

日期：2026-10-25（Asia/Shanghai）  
范围：隔离 test PostgreSQL/Redis/app；未连接生产 Telegram、生产应用或共享 PostgreSQL/Redis。

## 环境

- 源码目录：`/opt/vanguard-p6-src`
- Compose 项目：`vanguard-p6`
- app：`vanguard-p6-app`，loopback `127.0.0.1:28081`
- PostgreSQL：`vanguard-p6-postgres`，数据库 `vanguard_p6`
- Redis：`vanguard-p6-redis`
- app health：`{"status":"healthy","version":"1.0.0"}`

本轮只重建了隔离 app 容器。共享 PostgreSQL、共享 Redis、生产 worker、Telegram session 和生产 feature flags 均未修改。

## 开关状态

```text
P2_OUTBOX_ENABLED=false
P2_JOIN_OUTBOX_ENABLED=false
P2_AD_OUTBOX_ENABLED=false
P2_SHADOW_MODE=true
P3_ENTITY_CACHE=true
P3_GROUP_FACT_CACHE=true
P3_SURVIVAL_CACHE=true
P3_QUALIFICATION_SNAPSHOT=false
P5_ROLLOUT_MODE=shadow
```

P3 三条读取 lane 已按顺序单独启用；资格快照和所有 P2 写入仍 fail-closed。

## 结果

- Entity cache：key `tg:entity:7:username:p6entityprobe`，TTL `21600` 秒；首次和第二次均为 `fresh`，Actor reader 调用 1 次。
- Group fact cache：membership 与 permission key TTL 均为 `300` 秒；每个目标第二次读取命中，Actor reader 调用 1 次。
- Survival cache：key `tg:survival:7:p6survivalprobe:msg-1`，TTL `120` 秒；第二次读取命中，Actor reader 调用 1 次。
- P3 cache/invalidation 回归：`17 passed`。
- P2 producer、Outbox、资格快照回归：`33 passed`。
- Metrics bearer protection：未带 token `404`，正确 token `200`；operations API 未授权 `401`。
- Shadow metrics：`session_lease_contention_total=0`；join/ad reconciliation pending 均为 `0`；join/ad reconciliation oldest age 均为 `0`；`reconciliation_shadow_mode=1`。
- 清理隔离 invariant probe fixture 后，`growth_operation`、`growth_outbox`、`growth_operation_attempt`、`reconciliation_task`、`account_budget_reservation`、`qualification_snapshot` 均为 `0`，Redis probe key 数为 `0`。

## 门禁结论

P6.2 隔离 test shadow 和 P3 读取缓存验收通过。该结论只覆盖隔离环境中的缓存、认证、健康和无副作用基线，不等同于生产放行。

P6.3 真实 Telegram 灰度仍暂停：当前没有具体账号 ID、群 ID、campaign/run ID；P5 现有 cohort 只支持账号范围，不能结构性限制两个群和指定运行批次；入群前 fresh 资格快照 producer 也尚未完成真实集成证据。`P3_QUALIFICATION_SNAPSHOT`、`P2_AD_OUTBOX_ENABLED`、`P2_JOIN_OUTBOX_ENABLED` 继续关闭，尤其不得开启入群 Outbox。

P6.4 尚未开始。需要先完成有 scope guard 的受控灰度，再收集连续 24–48 小时生产 telemetry 和回滚记录。

## 下一步输入

开始 P6.3 前必须提供：最多两个账号 ID、最多两个 owned/Guardian 群 ID、批准的 `campaign_id`/`run_id`、入群前 fresh 快照证据（`snapshot_id`、版本和 `evidence_hash`）以及灰度 UTC 时间窗。收到后先补 scope guard 和 dry-run 验证，再按单开关顺序进入真实 Telegram。
