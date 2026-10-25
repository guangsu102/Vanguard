# 生产 P6.3/P6.4 重查结果

日期：2026-10-25（Asia/Shanghai）  
方式：通过 `oracle4c24g` 上运行中的 backend、PostgreSQL 和 Prometheus 执行只读查询。未执行迁移、部署、Telegram API 调用、Outbox 写入或 Redis 修改。

## 当前版本与开关

- backend 容器健康，镜像：`local/vanguard:p6-20261025-r2`。
- `schema_migration_history` 已应用至 `074_p5_release_control`，其中 070 P2、071 P3、072 P4、074 P5 已存在。
- P2/P3/P5 表已存在，但 `growth_operation`、`growth_outbox`、`growth_operation_attempt`、`qualification_snapshot`、`qualification_fact_version`、`p5_release_record`、`p5_gate_evaluation` 当前均为 0 行。
- 有效开关仍为 fail-closed：

  ```text
  P2_OUTBOX_ENABLED=false
  P2_JOIN_OUTBOX_ENABLED=false
  P2_AD_OUTBOX_ENABLED=false
  P2_SHADOW_MODE=true
  P3_ENTITY_CACHE=false
  P3_GROUP_FACT_CACHE=false
  P3_SURVIVAL_CACHE=false
  P3_QUALIFICATION_SNAPSHOT=false
  P4_AD_PACING=false
  P5_ROLLOUT_MODE=off
  P5_CANARY_ACCOUNT_IDS=""
  P5_COHORT_PERCENT=0
  P5_RELEASE_ID=""
  ```

## 灰度范围与数据

- 生产有 8 个 Telegram 账号；当前在线 promoter 账号 ID 为 2、4、33、34、35。
- `ad_campaign` 有 2 条记录：ID 1 active/enabled，ID 2 active/disabled；ID 1 为每个在线 promoter 绑定 6 条广告绑定。
- `campaign` 和 `campaign_execution` 均为 0；没有可直接用于 P6.3 的该类 campaign/run。
- 群总数为 1087，状态包含 active 240、pending 348、pending_join 223；账号已加入群数量为 2→207、4→75、33→106、34→105、35→99。
- 当前资格快照和事实版本均为 0。生产没有真实 fresh 入群前资格快照证据。
- 代码和 release control plane 仍只有账号 cohort scope，没有 group/campaign/run allowlist。仅设置两个账号不能限制到两个群或一个运行批次。

## 当前门禁

Prometheus 当前值：

- `vanguard_session_lease_contention_total=47`，要求为 0。
- reconciliation：join pending=2，最老年龄约 149974 秒（约 41.7 小时）；inbox pending=2、manual_required=1。
- inbox queue lag 约 524989 秒（约 6.08 天）。
- `vanguard_ad_unresolved_oldest_age_seconds=0`，ad reconciliation overdue=0；但累计 `vanguard_ad_deliveries_total{status="unknown"}=1`。
- P5 `release_active=0`、`gate_result=-1`、`cohort_size=0`。

## 结论

初次重查时的 4 条老化 reconciliation 已按现有安全路径处理：2 条账号 2 的 inbox 外部尝试记录转为 `manual_required`，2 条账号 34 的加群未知结果在重复 `RuntimeError` 且超过年龄阈值后转为 `manual_required`。没有删除源记录、释放 reservation、伪造外部成功或重发 Telegram 操作。处理后重新运行生产 preflight（2026-10-25 01:49 UTC，使用容器内 metrics token）结果为 `preflight_passed=true`：49 个 canonical migrations、依赖、过期租约、FloodWait、reconciliation 老化、广告 pending/unknown、health、metrics 和鉴权检查均通过。

P6.3/P6.4 仍不能开始。迁移和 preflight 通过不等于灰度可放行。生产仍缺少最多两个账号/两个群/一个批准 campaign-run 的 scope guard 或隔离数据集，也没有由真实 producer 产生的版本匹配 fresh 入群前资格快照；当前 P2/P3/P4/P5 rollout flags 继续全部关闭。`session_lease_contention_total` 是历史累计 47，不能通过重置指标伪造为零，需在后续真实观察窗口确认无新增竞争。

## 01:57 UTC 再次生产验证

- 带容器内 metrics token 的 `p5_preflight.py --check` 再次返回 `preflight_passed=true`。
- 当前 flags 解析值仍为全关闭：P2 Outbox/join/ad、P3 四类缓存/快照、P4 pacing 均为 `false`；P2 shadow 为 `true`；P5 mode 为 `off`、cohort 为 0、canary 为空。
- 数据库摘要：`growth_operation=0`、`growth_outbox=0`、`qualification_snapshot=0`、`qualification_fact_version=0`、`campaign_execution=0`、reconciliation `pending/leased=0`、`manual_required=5`。
- Prometheus：`ad_deliveries_total{status="unknown"}=1`（历史累计）、`ad_unresolved_oldest_age_seconds=0`、`session_lease_contention_total=47`、`p5_release_active=0`、`p5_gate_result=-1`。

因此本次仍未创建 Outbox、资格快照或 campaign/run，也未调用 Telegram 发送路径；P6.3/P6.4 继续保持阻塞。

