# 生产诊断修复（2026-11-11 夜间诊断的代码修复）

对应诊断：[findings.json](../../tmp/system-diagnosis-20261111/findings.json)。
服务器侧磁盘/Docker 操作见 [ops-cleanup-runbook](2026-11-11-ops-cleanup-runbook.md)。

## 修复 1：ChatRestrictedError 被扩大为整账号 24h 冻结（账号 2 事故根因）

- `app/core/account/risk_guard.py` `classify_error`：在通用 `restricted` 兜底之前新增聊天级标记
  （`ChatRestrictedError` / `chat_restricted` / "restricted from sending messages in this chat"），
  归类为 `group_write_forbidden`。Telegram 对 CHAT_RESTRICTED 的定义就是"在当前聊天中被限制发言"。
- `USER_RESTRICTED`（SpamBot 账号级信号）保持 `account_restricted` 不变。
- `growth_results.risk_failure` 对 `group_write_forbidden` 本就不冻结账号，分类修正后 2 号这类
  错误不再触发 24h `risk_pause_until`；群成员被标记待复审（`persist_ad_result` 的
  `permission_changed` 路径），账号保持正常的群间轮换。
- AI 错误分类器提示词同步补充"in this chat => group_write_forbidden"，口径一致。

## 修复 2：权限失败后撤销冷却、连续换群尝试（33/35 号突发失败来源）

- `app/modules/acquisition/simple_limits.py` `revert_ad` 新增 `cooldown_seconds` 参数：
  权限失败仍回滚当日配额（死群不占额度），但冷却键改为"保留最小间隔"而非删除。
- `app/modules/acquisition/simple_growth.py` `ads()`：权限失败后按
  `max(动态 ad 间隔, 节流配置, 600s)` 保留冷却并**立即结束本轮 stage**（原来一轮最多再试 20 个群）。
  常量 `PERMISSION_FAILURE_SPACING_SECONDS = 600`。
- `ChatRestrictedError` 加入 `permission_changed` 错误集合，与 ChatWriteForbidden 同样处理。

## 修复 3：账号级冷却期空转导致广告阶段超时（22/23 次超时根因）

- `ads()` 入口在查询候选前先查 Redis `ads:{account_id}:due`，冷却中直接返回
  `account_cooldown + retry_after_seconds`（与 `join()` 同款预检）。
- 候选循环中 `Deferred("account_cooldown")` 直接返回，不再换群；`group_cooldown` 才继续换目标。
  冷却中的账号不再为每个候选付出数据库查询与客户端准备（每轮最多 100 个目标的空转消除）。

## 修复 4：删除通知积压（15.06 万 pending，99.4% 为删除通知）

- **入口过滤**：`app/modules/acquisition/adaptive_frequency.py` 新增 `relevant_ad_deletion`，
  `telegram_worker.handle_ad_deleted` 入队前先确认删除的是本账号已成功投递的广告
  （account_id + telegram_group_id 别名 + message_id + status=success，单条索引查询）。
  与发送落库竞态的极小概率漏报由每日存活复核兜底，不丢证据。
- **离线账号本地事实**：worker 维护 `_growth_local_fact_account_ids`（30s 刷新一次
  pending 本地事件的账号集合），2/33/35 这类退出运行范围的账号，其删除与 listener_fact
  照常领取处理——纯本地投影不需要重连 Telegram。
- **领取 SQL**：`event_inbox.claim/claim_batch` 的本地事实路径改为每账号有界窗口预选
  （live_account 部分索引直读最旧 N 条），大批积压下不再每次领取全量排序；
  旋转公平与 head-of-line 语义保持不变。

## 修复 5：成本遥测 4,096 条上限触发递归 JSON 回收查询反复超时（33 号）

- `app/modules/acquisition/qualification_cost.py`：
  - 回收扫描加**账号级 15 分钟冷却**（`retire_scan_not_before` 存入 coverage 标记行，
    每账号 advisory lock 天然互斥）；满容量后每个观察事件不再重扫历史。
  - 删除递归 SQL 首适配（`_postgres_retirement_selection`），PostgreSQL 与 SQLite 统一走
    "有界候选查询 + Python 首适配（`_ordered_first_fit`）+ 有界关联取回 + 计数核对"。
    仅回收已结算终态 Work 及全部 slice 的保证不变；歧义/计数不符仍 fail-closed。
  - 顺带修正统一路径下 JSON 布尔比较的方言差异（PG `astext` 为 "true"，SQLite 为 1）。

## 修复 6：OperationalError 只有异常类型、无消息无堆栈

- `telegram_worker.py` 数据库重试日志补 `error=safe_exception_message(...)` + 截断堆栈。
- `member_observation.py` 静默重试路径补 warning 日志（asset/update/error message）。

## 测试

- 新增 `tests/unit/test_production_diagnosis_regressions.py`（冷却保持、相关删除过滤）。
- 重写 `tests/unit/growth_efficiency_telemetry/test_qualification_cost_retirement_selection.py`
  以验证新的 Python 首适配与 fail-closed 语义（原测试针对已删除的递归 SQL 内部实现）。
- `tests/unit/core/test_account_risk_guard.py` 新增聊天级受限不冻结账号、USER_RESTRICTED 仍冻结的用例。
- 结果：直接相关套件全绿；全量单测 5707 通过 / 73 失败 / 54 跳过，
  73 个失败经基线 stash 对照确认为存量环境问题（失败集合与修改前完全一致）。
- ruff：修改区域零新增告警。

## 部署与观察（待执行）

1. 按常规流程部署 backend（`docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json up -d --no-deps backend`）。
2. 观察项：`growth_inbox` pending 曲线（应快速下降）、`irrelevant_deletions_filtered` 计数、
   权限失败发送频率（不应再出现分钟级 ≥5 次突发）、广告阶段超时次数、
   `qualification.telemetry.capacity_pressure` 相关慢查询消失。
3. 2/33/35 号账号：33、35 保持人工恢复流程；2 号解除冻结后建议先用保守配置
   （广告 30 条/天、≥30 分钟间隔；加群 10 次/天、≥2 小时间隔，仅明确允许推广的群）。
4. 3 条已外发结果未知的对账事件按 runbook E 节单独处理。
