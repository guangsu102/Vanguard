# P5 Rollout Runbook — P2–P4 单账号 Canary、灰度与稳定性观察

本文档是 P5 发布控制面的操作手册。P5 把 P2 Outbox、P3 读缓存/快照与 P4 pacing/管理指标/历史治理绑定为一个 release unit,以账号级 cohort 为边界发布;P5 自身是决策层,不新增 Telegram 配额、发送并发或直连路径。

## 1. 控制面

```text
P5_ROLLOUT_MODE=off|shadow|canary|cohort|full   # off 透明(沿用底层旗标);shadow 只记录;其余模式由 P5 cohort 统一作用域
P5_CANARY_ACCOUNT_IDS=<resolved account ids>    # canary 解析后的账号;空= fail closed
P5_COHORT_PERCENT=<0..100>                      # 稳定 hash cohort 百分比;扩容只增不减
P5_HISTORY_APPLY=false                          # 整个 P5 期间保持 false;真实归档另行批准
P5_RELEASE_ID=<immutable release id>            # 每次修复后的发布必须使用新的 release id
```

- **governing 模式**(canary/cohort/full)下,P5 cohort 是唯一的 release-unit 作用域:它取代 `P2_ACCOUNT_IDS` 与 `P4_AD_PACING_ACCOUNT_IDS` 的 allowlist;`P2_OUTBOX_ENABLED`、lane 旗标、`P4_AD_PACING` 等主开关仍然必须打开。
- **shadow** 模式运行选号与 cohort 预演,但不激活发布、不触碰流量。
- **off** 模式对底层旗标透明;若仍有 active release,observer 会把它标记 rolled_back、清除遗留 stopped 状态并保持 legacy ads/join admission 可用。off 不会写入或保留 P5 kill switch。
- **kill switch** 是 observer 写入的 Redis stopped 状态(`vanguard:p5:rollout:v1:stopped`):被 stop 的 cohort 账号不再获得新的 ads/join 派发(dispatch 候选层过滤 + 已发布 ticket 的执行守卫),review/survival/verify/reconcile/exit 等维护通道继续运行,未确认结果进入 reconciliation,绝不自动重发。stopped 状态在新的 `P5_RELEASE_ID` 激活时自动清除,并给上一条 release 记录写入 `recovered_at`。
- 生产环境 governing 模式要求 `P5_RELEASE_ID` 非空(`validate_production_defaults` 启动校验);部署 governing 模式但没有匹配的冻结记录时,observer 会在一分钟内 fail closed(stop 无记录 cohort)。

## 2. 发布流程

### 2.1 冻结 release candidate(部署前)

在服务器上(backend 容器或同网络环境)执行:

```bash
cd /opt/vanguard/backend
python scripts/p5_preflight.py --freeze \
    --release-id p5-$(date +%Y%m%d)-<short-sha> \
    --image-digest sha256:<backend-image-digest> \
    --base-url http://127.0.0.1:18080 \
    --metrics-token-file /run/secrets/metrics.token
```

预检包含:canonical SQL 迁移已全部应用(只向前,不 downgrade)、数据库/Redis 就绪、无过期 Outbox/reconciliation 租约、无 FloodWait 活动暂停、无未解释的 reconciliation 老化任务、无 aged `pending/unknown` 广告、历史治理保持 dry-run、`/health` 200、`/metrics` 需要 bearer token 可读且无 token 被拒绝、`/api/operations/status` 需要认证,以及只读的 canary 选号预演。任何 critical 检查失败都会拒绝冻结(exit 1)。同一个 release id 重复冻结要求 image digest 与迁移 revision 完全一致,否则报 `p5_release_id_immutable`。

### 2.2 Shadow 预演(可选但推荐)

```text
P5_ROLLOUT_MODE=shadow
P5_RELEASE_ID=<同冻结记录>
P5_HISTORY_APPLY=false
```

部署后 observer 每分钟记录选号结果与 cohort 预览(`vanguard:p5:rollout:v1:state` 与结构化日志),流量不变。用预检输出的 `canary_preview.selected_account_id` 作为 canary 账号的解析结果。

### 2.3 单账号 Canary

```text
P5_ROLLOUT_MODE=canary
P5_CANARY_ACCOUNT_IDS=<解析出的账号 id>
P2_OUTBOX_ENABLED=true  P2_JOIN_OUTBOX_ENABLED=true  P2_AD_OUTBOX_ENABLED=true
P3_ENTITY_CACHE=true  P3_GROUP_FACT_CACHE=true  P3_SURVIVAL_CACHE=true  P3_QUALIFICATION_SNAPSHOT=true
P4_AD_PACING=true  P4_AD_PACING_SHADOW=false  P4_ADMIN_METRICS=true
P4_HISTORY_GOVERNANCE=true  P4_HISTORY_GOVERNANCE_DRY_RUN=true  P5_HISTORY_APPLY=false
```

- canary 账号走真实 P4 pacing 路径、P2 Outbox/reconciliation、P3 cache 与 Actor fencing。
- 2 小时 warm-up 后进入至少 24 小时观察;期间只保留正常任务流量,禁止临时提高发送并发或预算。
- gate 硬停止条件(任一成立,observer 立即 engage kill switch):session lease contention > 0、父群不变量失败、aged 广告 `pending/unknown` > 0、加群未知结果连续两个观察窗口增长、非法状态迁移/孤儿 Outbox/重复活动 operation、未解释 reconciliation 老化、基础设施不可用、cohort 外出现 GrowthOperation(allowlist 绕过)。
- hold 条件(暂停扩大,人工分析):receipt/outbox/survival 相对基线恶化 >10%、广告成功率下降 >5 个百分点、FloodWait 或读取预算消耗增加 >20%、eligible 账号 30 分钟无任务、单账号份额 > 公平份额 2 倍、duplicate target rate/queue age/survival backlog 持续上升、阶段最短观察未满。基线取激活时刻的前 24 小时聚合,存入 release 记录 `baseline_json`;所有既有阈值读自 `app/core/metrics.SLO`,发布脚本不复制第二套阈值。
- 每次观察结果都落库为 `p5_gate_evaluation` 行;`/api/operations/status` 的 `p5` 节与 `vanguard_p5_*` gauges 实时可见;`VanguardP5RolloutStopped`(critical)与 `VanguardP5GateHold`(warning)告警在 `monitoring/alerts.yml`。

### 2.4 灰度扩大(25% → 50% → 100%)

每阶段重新冻结/沿用同一 release id,只改 `P5_COHORT_PERCENT`:

```text
cohort 25  → 观察至少 2 小时
cohort 50  → 观察至少 2 小时
full(或 cohort 100)→ 持续观察 24–48 小时
```

- cohort 由稳定 hash(`config.p5_cohort_rank`)生成:扩容只增加成员,已加入账号永不移除;canary 账号在所有百分比下保持成员。
- 每次部署后 observer 检测 cohort 变化,追加 `stage_history_json` 并记录 `cohort_changed` 事件。
- gate 上一阶段有未恢复的 hold 时不要扩大;硬停止触发后 stopped cohort 会保持封锁,即使部署了更大的百分比也不会放行新流量。将模式切回 off 会清除该 P5 stop，以避免旧 stop 状态继续影响 legacy ads/join。

## 3. 回滚

1. 停止扩大:把 `P5_ROLLOUT_MODE` 改回 `off`;observer 把 active release 标记 rolled_back,写 `rollback_at/rollback_reason/rollback_metrics_json/affected_cohort_json`,并清除 P5 stopped 状态，确保 legacy ads/join 不被阻断。硬停止触发时产生的 cohort 封锁只在 governing 模式下有效。
2. 关闭异常 P4 lane:按需把 `P4_AD_PACING=false`(以及相关 lane 旗标);observer 会以 `release_flags_drift` hold 记录这次刻意关闭。
3. 从 allowlist 移除异常账号:清空/修改 `P5_CANARY_ACCOUNT_IDS` 后重新部署(仅影响后续 cohort 计算)。
4. 已创建的 Outbox、attempt、receipt、reconciliation 记录全部保留,不做任何删除。
5. 未确认操作继续进入 reconciliation,禁止自动重发(P2 语义保持)。
6. 只有当应用健康检查或 API 兼容性失败时,才回切上一个 immutable image digest(`docker compose ... up -d --no-deps backend`,使用上一 digest);数据库只执行向前兼容迁移,**绝不执行 `alembic downgrade`**。
7. `P5_HISTORY_APPLY=false` 在整个 P5 期间保持不变,因此不存在需要回滚的归档删除。
8. 若此前是 governing 模式硬停止，回滚后用**新的 release id** 重新走冻结与预检;新 release 激活会清除 stopped 状态并为上一条记录写 `recovered_at`。单纯切回 off 不会产生 stopped 记录，也不需要 recovery 事件。

## 4. 完成条件与历史治理批准

`full` 阶段满足以下条件时 observer 自动把 release 标记 completed:连续稳定 ≥24 小时、session lease contention 为 0、aged 广告 `pending/unknown` 为 0、加群未知结果不增长、父群状态不变量持续成立、reconciliation 无未解释老化、无孤儿/重复/非法迁移、公平份额无饥饿或独占、gate 全绿。

真实历史归档是 P5 之外的单独批准:P5 观察完成并复核 `growth_history_governance_batch` 的 dry-run 报告后,由运维显式把 `P5_HISTORY_APPLY=true`(同时 `P4_HISTORY_GOVERNANCE_DRY_RUN=false`)部署;在此之前 `run_pass` 始终只做 dry-run,可重复执行且不产生归档删除。

## 5. 快速命令

```bash
# 观察状态(受保护)
curl -s https://vanguard.pipenai.xyz/api/operations/status | jq .p5

# 指标
curl -s -H "Authorization: Bearer $(cat /run/secrets/metrics.token)" \
    https://vanguard.pipenai.xyz/metrics | grep vanguard_p5

# 手动清除 governing 模式遗留 stopped(通常切回 off 会自动清除)
redis-cli DEL vanguard:p5:rollout:v1:stopped

# 部署(共享 PostgreSQL canonical compose,仅应用服务)
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json up -d --no-deps backend
```

## 6. Redis 键与审计表

| 对象 | 位置 |
|---|---|
| 运行时状态 | `vanguard:p5:rollout:v1:state` |
| kill switch | `vanguard:p5:rollout:v1:stopped` |
| 观察窗口样本 | `vanguard:p5:rollout:v1:windows:{release_id}` |
| 发布记录 | 表 `p5_release_record`(release_id 唯一,image digest/迁移 revision/flag 快照/基线/阶段历史/回滚证据) |
| gate 审计 | 表 `p5_gate_evaluation`(每个观察窗口一行:结果、原因、指标向量、cohort) |
| 事件计数 | `vanguard_p5_events_total{event}` |
| 结构化日志 | `p5_release_frozen` / `p5_release_started` / `p5_cohort_stage` / `p5_gate_hold` / `p5_rollout_stopped` / `p5_rollout_disabled_without_legacy_stop` / `p5_previous_stop_cleared` / `p5_release_completed` |
