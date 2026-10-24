# P6.3/P6.4 灰度前置核查

日期：2026-10-24（Asia/Shanghai）  
范围：本地代码与发布文档审阅。未连接生产 PostgreSQL/Redis，未执行 Telegram 操作。

## 结论

P6.3 当前不能直接进入真实 Telegram 灰度。P5 release control plane 的 cohort 作用域是账号级：`P5_CANARY_ACCOUNT_IDS`、`P5_COHORT_PERCENT`。现有 P2 producer 没有独立的 P5 campaign/run/group allowlist：

- `produce_join_outbox` 从可重试 `Group` 候选全局取数，再按账号生成入群 Outbox。
- `produce_ad_outbox` 遍历账号全部启用的广告绑定和已加入群组。
- P5 的 cohort bypass 检查只验证账号是否越过 cohort 边界。

因此，仅设置 canary 账号不能结构性保证“最多 2 个 owned/Guardian 群”和“指定测试 campaign/run”。真实灰度必须先使用隔离 test 数据集，使该账号最多只有两个可操作目标和一个批准的测试 campaign/run；或者在放行前增加并测试 account + group + campaign/run admission guard。

## 必须提供的输入

1. 隔离 test PostgreSQL、Redis 地址和 canonical Compose 应用服务目标；不得停止、重建或替换共享生产 PostgreSQL/Redis。
2. 最多两个已登录且 Actor 可用的 Telegram 账号 ID，以及账号风险/预算状态确认。
3. 最多两个目标群的数据库 ID、Telegram ID、`owned`/`Guardian` 归属和权限确认。
4. 一个批准的测试 `campaign_id` 与 `run_id`，以及灰度开始和结束的 UTC 时间窗。
5. 前置资格 producer 产生 fresh、版本匹配的入群快照证据（`snapshot_id`、`group_version`、`rule_version`、`evidence_hash`、`source_fact_version`）；不得使用入群后结果伪造前置快照。
6. 业务负责人、运维负责人和回滚联系人；确认 P6.3 单开关顺序及 P6.4 24–48 小时观察批准。

## 放行顺序与停止条件

按单开关稳定窗口顺序：`P3_ENTITY_CACHE` → `P3_GROUP_FACT_CACHE` → `P3_SURVIVAL_CACHE` → 业务确认 fresh 前置快照后 `P3_QUALIFICATION_SNAPSHOT` → 广告 P2 Outbox → 稳定后才考虑 `P2_JOIN_OUTBOX_ENABLED`。在前置快照 producer 未通过集成验证前，入群开关保持关闭。

任一窗口出现 session lease contention、父群不变量失败、广告 `pending/unknown`、入群 unknown 持续增长、未解释 reconciliation 老化、FloodWait/预算跳过率回归或 cohort 越界，立即关闭最近启用的开关并回到 shadow。未知结果只进入 reconciliation，禁止自动重发。

## 当前可验证范围

已有快照单元测试覆盖不可变字段、unknown 禁止、证据哈希和事实版本失效；已有 producer scope 测试覆盖 fail-closed。仍缺少真实 `produce_join_outbox` 的入群前快照集成断言，以及 campaign/group 灰度范围断言。两项应作为 P6.3 hard gate，不得用静态测试替代。

