# 群运营中心阶段 1：自建群接入 Guardian 治理需求规格

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 开发基线，可直接进入任务拆分与编码 |
| 编制日期 | 2026-09-09 |
| 来源方案 | `docs/群运营中心-自建群治理与AI引流-实施计划方案.md` |
| 对应阶段 | 阶段 1：自建群接入 Guardian 治理 |
| 对应里程碑 | M1：自建群可治理 |
| 预计工作量 | 7–10 人日，不含阶段 0 和真实 Telegram 灰度 |
| 适用分支基线 | `codex/owned-group-foundation`，开发开始前须重新确认 |

## 1. 本阶段结论

本阶段交付一条显式、幂等、可核验、可恢复的治理接入链：

```text
READY 自建群
  → 选择已登记且已验证的 Guardian Bot
  → 核验 Bot 身份、群身份和管理员权限
  → 查找或创建核心 Group
  → 查找或创建 ManagedGroupBinding
  → 初始化默认治理策略
  → 写回自建群治理关联
  → Guardian Worker 仅对有效绑定消费事件
  → 进入现有策略、敏感词、白名单、公告、置顶和活动能力
```

本阶段固定采用在 `owned_group_assets` 增加关联字段的方案，不新建通用资产中心，也不合并 `OwnedGroupAsset` 与 `ManagedGroupBinding`。

绑定接口只负责权限探针和 Vanguard 内部建链，不自动邀请 Bot、不自动授予管理员权限、不修改 Telegram 群权限。Bot 加群和管理员提权继续使用现有自建群编排能力，实时 Telegram 探针是最终判断依据。

## 2. 开发前置条件

阶段 1 可以编码，但在以下阶段 0 条件全部满足前不得上线或宣称完成 M1：

1. 已提供唯一的 `telegram_chat_id ↔ core_group_id` 解析服务。
2. 已确认 `Group.id` 是内部主键，`Group.group_id`、`OwnedGroupAsset.telegram_chat_id`、`ManagedGroupBinding.telegram_group_id` 是 Telegram Chat ID。
3. Guardian 策略、规则、白名单、敏感词和处罚记录的内部关联使用 `core_group_id`；Telegram Bot API 调用只使用 `telegram_chat_id`。
4. 已完成历史 Telegram Chat ID 重复与冲突检查，冲突数据不得自动覆盖。
5. 已提供自建群治理 feature flag、治理停止状态和审计事件基础能力。
6. 已有托管群的验证、治理、处罚、公告和活动回归测试通过。

阶段 1 代码必须通过一个明确的目标对象传递两类 ID，禁止使用含义不明的单个 `group_id` 在数据库查询和 Telegram 调用之间透传：

```python
GuardianGroupTarget(
    core_group_id: int,
    telegram_chat_id: int,
    managed_binding_id: int,
    bot_account_id: int,
)
```

## 3. 目标、成功标准与非目标

### 3.1 业务目标

1. 操作员可以从一个 `READY` 自建群发起“接入治理”。
2. 系统能明确告诉操作员 Bot 是否在目标群、是否为管理员、缺少哪些权限。
3. 接入成功后，自建群可以复用现有 Guardian 治理能力。
4. 重复请求、接口超时、Worker 重启和权限变化不会产生重复核心群或重复绑定。
5. 治理失效时页面必须显示 `degraded` 和可执行的失败原因，不能继续显示“已治理”。
6. 未显式接入的自建群不得因 Bot 收到消息而被自动纳入治理。

### 3.2 阶段成功标准

- 同一个自建群重复绑定同一个 Bot，只返回同一组 `core_group_id` 和 `managed_binding_id`。
- 同一个核心群最多存在一个主 Guardian Bot 绑定。
- 接入成功后，现有入群验证、敏感词、白名单、反垃圾、警告、禁言、封禁、公告、置顶和群活动均能解析到该自建群。
- Guardian Worker 只处理“Bot 与群匹配、绑定为 ACTIVE、自建群治理状态为 managed”的更新。
- Bot 被移除、降为普通成员或丢失必要权限后，reconcile 将自建群标记为 `degraded` 并停止治理动作。
- 旧托管群不要求补建 `OwnedGroupAsset`，现有功能与入口不回归。

### 3.3 本阶段不做

- 不自动创建 Telegram 群。
- 不自动把 Guardian Bot 拉入群或提升为管理员。
- 不实现自建群内推广账号的 AI/模板消息。
- 不实现账号级 AI Persona。
- 不实现外部群广告 CTA 和转化归因。
- 不实现统一三类成员读模型。
- 不重写 Guardian 规则引擎、处罚引擎、广播器或活动系统。
- 不合并 Guardian Bot Profile 与 Owned Bot Profile。
- 不提供删除 Telegram 群或删除历史治理记录的能力。
- 第一版不提供“更换主 Bot”或“解除绑定”操作；需更换时先由后续需求定义安全迁移流程。

## 4. 用户角色与权限

| 角色 | 查询治理状态 | 发起接入 | 执行 reconcile | 配置治理策略 | 查看审计 |
| --- | --- | --- | --- | --- | --- |
| 管理员 `admin` | 是 | 是 | 是 | 是 | 是 |
| 操作员 `operator` | 是 | 是 | 是 | 按现有 Guardian 权限 | 是 |
| 审计员 `auditor` | 是 | 否 | 否 | 否 | 是 |
| 其他登录用户 | 否 | 否 | 否 | 否 | 否 |

“接入治理”和 reconcile 复用 `require_owned_group_operator`。读取接口复用审计只读角色判断。所有接口继续要求登录鉴权。

## 5. 名词与 ID 规范

| 名称 | 含义 | 使用位置 |
| --- | --- | --- |
| `asset_id` | `OwnedGroupAsset.id` | 自建群 API 路径、前端选中项 |
| `telegram_chat_id` | Telegram 超级群 ID | Telegram Bot API、外部 API 响应、前端展示 |
| `core_group_id` | `Group.id` | 内部策略、规则、处罚及活动关联 |
| `managed_binding_id` | `ManagedGroupBinding.id` | Guardian 绑定操作与现有管理群能力 |
| `guardian_bot_account_id` | `TelegramAccount.id` | Guardian Worker 和绑定关系；不是 Profile ID |
| `owned_bot_profile_id` | `OwnedBotProfile.id` | 自建群成员编排；不是 Guardian 绑定外键 |
| `guardian_bot_profile_id` | `GuardianBotProfile.id` | Guardian Bot 管理页面；不是绑定请求参数 |

禁止规则：

- 禁止把 `OwnedBotProfile.id` 传给 `bot_account_id`。
- 禁止把 `Group.id` 传给 Telegram Bot API。
- 禁止把 Telegram Chat ID 直接写入以 `core_group_id` 为语义的策略外键。
- 禁止仅凭数值相等推断两类 ID 是同一个 ID。

## 6. 治理状态模型

自建群创建状态 `OwnedGroupAsset.status` 保持现状，不增加 `managed` 等值。新增独立字段 `governance_status`：

| 状态 | 含义 | 页面主操作 |
| --- | --- | --- |
| `disabled` | 尚未接入治理，或阶段功能对该资产未启用 | 接入治理 |
| `pending` | 接入或 reconcile 正在执行 | 查看状态，禁止重复发起不同 Bot 请求 |
| `managed` | 绑定存在、Bot 身份与必要权限核验通过 | 治理设置、重新检测 |
| `degraded` | 已尝试接入或曾经有效，但当前绑定、Bot、权限或同步异常 | 查看原因、重新检测 |

允许的状态流转：

```text
disabled → pending → managed
disabled → pending → degraded
degraded → pending → managed
degraded → pending → degraded
managed  → managed              同一 Bot 重复绑定，幂等返回
managed  → pending → degraded   reconcile 发现权限丢失
degraded → disabled             仅资产归档或管理员后续禁用流程使用
```

约束：

- 治理状态变化不得改变 `OwnedGroupAsset.status`。
- `OwnedGroupAsset.status=archived` 时，治理事件必须停止处理；绑定和审计记录保留。
- `managed` 必须同时满足 `core_group_id`、`managed_binding_id`、`guardian_bot_account_id` 非空，且对应 `ManagedGroupBinding.binding_status=active`。
- `pending` 超过 2 分钟仍未完成时，GET 状态返回 `stale_pending=true`，前端提示执行 reconcile；不得自动再次绑定其他 Bot。

## 7. Bot 可用性与归属规则

### 7.1 可选 Bot 条件

前端只显示同时满足以下条件的 Bot：

1. 存在 `TelegramAccount`，`account_type=guardian_bot`。
2. `TelegramAccount.is_active=true`，状态不是 `error` 或 `banned`，风险等级不是 `limited`、`frozen`、`quarantined`。
3. 存在启用的 `GuardianBotProfile`，且运行 Token 非空。
4. 存在启用的 `OwnedBotProfile`，`status=verified`。
5. `OwnedBotProfile.owner_account_id == OwnedGroupAsset.owner_account_id`。
6. 两个 Profile 的 `account_id` 相同。
7. 两个 Profile 均有 `bot_user_id` 时必须相同。

请求参数使用 `guardian_bot_account_id`。后端必须重复执行全部校验，不信任前端筛选结果。

### 7.2 运行身份一致性

权限探针使用当前 Guardian Worker 实际使用的 `GuardianBotProfile.bot_token`。`getMe` 返回的 Bot ID 必须满足：

- 与 `GuardianBotProfile.bot_user_id` 一致；字段为空时允许首次回填。
- 与 `OwnedBotProfile.bot_user_id` 一致；字段为空时允许首次回填。
- 返回对象 `is_bot=true`。

任意已存在 ID 不一致时返回 `guardian_bot_identity_mismatch`，不得覆盖旧 ID，不得创建绑定。

### 7.3 Bot 加群与提权前置

接入前，操作员应在现有自建群成员编排中完成 Bot 加群和管理员配置。建议管理员权限至少包括：

```text
can_delete_messages
can_restrict_members
can_invite_users
can_pin_messages
```

`OwnedGroupMembership` 的本地状态只用于页面预提示；绑定是否成功以实时 `getChatMember` 结果为准。

## 8. Telegram 权限探针

### 8.1 调用顺序

每次首次接入和 reconcile 必须按以下顺序执行：

1. `getMe`：确认 Token 对应的 Bot 身份。
2. `getChat(telegram_chat_id)`：确认群存在、可访问且类型为 `supergroup`。
3. `getChatMember(telegram_chat_id, bot_user_id)`：读取 Bot 角色与管理员权限。
4. 根据必要权限生成能力矩阵和缺失权限列表。
5. 关闭本次创建的 Telegram client，不复用未关闭的临时连接。

### 8.2 必要权限

| 权限 | 对应能力 | 缺失时处理 |
| --- | --- | --- |
| 管理员或创建者身份 | 所有治理动作 | 整体接入失败，`degraded` |
| `can_delete_messages` | 删除违规消息、反垃圾 | 整体接入失败，`degraded` |
| `can_restrict_members` | 禁言、封禁、踢出未验证成员 | 整体接入失败，`degraded` |
| `can_invite_users` | 处理邀请与入群请求 | 整体接入失败，`degraded` |
| `can_pin_messages` | 公告置顶 | 整体接入失败，`degraded` |

若 Bot 状态为 `creator`，视为拥有上述必要权限。状态为 `member`、`restricted`、`left` 或 `kicked` 均不能进入 `managed`。

### 8.3 权限快照

只保存白名单字段，不保存完整 Telegram 原始响应：

```json
{
  "schema_version": 1,
  "source": "owned_group_governance_bind",
  "chat_type": "supergroup",
  "bot_user_id": 123456789,
  "bot_status": "administrator",
  "granted_permissions": [
    "can_delete_messages",
    "can_restrict_members",
    "can_invite_users",
    "can_pin_messages"
  ],
  "missing_permissions": [],
  "probed_at": "2026-09-09T12:00:00Z"
}
```

异常信息必须经过现有安全清洗逻辑，不得保存 Bot Token、代理凭据、Session、完整邀请链接或 Telegram HTTP 请求 URL。

## 9. 数据模型需求

### 9.1 `owned_group_assets` 新增字段

| 字段 | 类型 | 可空 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `core_group_id` | Integer FK `group.id` | 是 | NULL | 对应核心群记录 |
| `managed_binding_id` | Integer FK `managed_group_binding.id` | 是 | NULL | 对应 Guardian 主绑定，全表唯一 |
| `guardian_bot_account_id` | Integer FK `telegram_account.id` | 是 | NULL | 当前选择的 Guardian Bot 账号 |
| `governance_status` | String(32) | 否 | `disabled` | `disabled/pending/managed/degraded` |
| `governance_pending_at` | DateTime | 是 | NULL | 最近一次进入 pending 的时间，用于识别卡死请求 |
| `governance_enabled_at` | DateTime | 是 | NULL | 首次成功进入 managed 的时间，不因 reconcile 覆盖 |
| `governance_last_checked_at` | DateTime | 是 | NULL | 最近一次实时权限探针完成时间 |
| `governance_last_error_code` | String(64) | 是 | NULL | 机器可读失败码 |
| `governance_last_error_message` | Text | 是 | NULL | 已清洗、可展示的失败摘要 |

索引与约束：

- `managed_binding_id` 唯一索引，防止一个 Guardian 绑定回写给多个自建群资产。
- `core_group_id` 普通索引。
- `guardian_bot_account_id, governance_status` 组合索引，供 Worker 和状态统计使用。
- `governance_status` 增加 CHECK 约束，仅允许四个固定值。
- 外键删除策略统一使用 `SET NULL`，不得级联删除自建群资产。
- 已有资产迁移后统一为 `governance_status=disabled`，不自动创建绑定。

### 9.2 现有表约束

- 继续依赖 `Group.group_id` 的唯一约束保证一个 Telegram Chat ID 只有一个核心群。
- 继续依赖 `ManagedGroupBinding.group_id` 的唯一约束保证一个核心群只有一个主 Bot。
- 不向 `ManagedGroupBinding` 增加 owned-group 专属字段；来源关系通过 `OwnedGroupAsset.managed_binding_id` 反查。
- 绑定成功时 `Group.discovery_source`：新建记录写 `owned_group_governance`；复用旧记录时不得覆盖已有非空来源。

### 9.3 迁移文件

按当前仓库编号，开发时预计新增：

- Alembic：`backend/migrations/versions/033_add_owned_group_governance.py`。
- 生产 SQL：`backend/migrations/047_add_owned_group_governance.sql`。

若开发开始时迁移 head 已变化，使用新的顺序号，但迁移内容和约束保持不变。升级必须向前兼容；降级只删除本阶段新增索引、约束和字段，不删除核心群、绑定或 Telegram 资产。

## 10. 后端服务需求

新增 `backend/app/modules/owned_group/governance.py`，集中实现以下服务，API 层不得复制业务逻辑：

```python
get_governance_status(asset_id, actor) -> GovernanceStatusResult
bind_governance(asset_id, guardian_bot_account_id, actor, correlation_id) -> GovernanceStatusResult
reconcile_governance(asset_id, actor, correlation_id) -> GovernanceStatusResult
resolve_guardian_bot(asset, guardian_bot_account_id) -> EligibleGuardianBot
probe_guardian_permissions(asset, eligible_bot) -> GuardianPermissionProbe
```

### 10.1 首次接入流程

`bind_governance` 必须执行：

1. 使用 `SELECT ... FOR UPDATE` 锁定目标 `OwnedGroupAsset`。
2. 校验资产存在、`status=ready`、`telegram_chat_id` 非空、未归档。
3. 校验阶段 0 治理 gate 允许执行。
4. 若已 `managed` 且请求 Bot 与当前 Bot 相同，直接幂等返回，不重复建记录。
5. 若已 `managed` 且请求 Bot 不同，返回 409，不修改任何关联。
6. 校验双 Profile、账号状态、风险状态和所有权。
7. 写入 `guardian_bot_account_id`、`governance_status=pending`、`governance_pending_at=now`，清空旧错误并提交短事务。
8. 在事务外执行 Telegram 权限探针。
9. 重新锁定资产，确认请求期间 Bot 和资产状态未被改变。
10. 按 `telegram_chat_id` 查找或创建 `Group`。
11. 按 `Group.id` 查找 `ManagedGroupBinding`：不存在则创建；同 Bot 则复用；不同 Bot 则冲突。
12. 初始化默认治理策略；已存在的策略不得覆盖。
13. 在同一数据库事务内写回三个关联 ID、权限快照、`governance_status=managed` 和时间字段。
14. 写成功审计并提交。

任何步骤失败时：

- 不删除 Telegram 群。
- 不删除既有核心群、绑定、策略或审计。
- 将本次资产状态更新为 `degraded`，记录已清洗错误和失败审计。
- 成功或失败完成时均清空 `governance_pending_at`；只有进程中断形成的 pending 才保留该时间。
- 若失败发生在第 9 步的并发状态检查，返回 `governance_state_changed`，不得用旧探针结果覆盖新状态。

### 10.2 默认治理策略

首次创建时使用现有安全默认值：

```text
入群验证：关闭
验证类型：captcha
验证超时：5 分钟
最大尝试：3 次
白名单跳过：开启
未验证自动踢出：关闭
自动踢出延迟：10 分钟

发言间隔：10 秒
每分钟最大发言：5
每小时最大链接：3
新人静默：5 分钟
首次发言延迟：30 秒

警告阈值：3
达到阈值后禁言：开启
禁言时长：300 秒
封禁阈值：5
重复违规窗口：24 小时
警告自动重置：7 天
严重违规默认动作：mute
```

默认配置的数据库关联键必须是 `core_group_id`。接入过程只补缺失记录，不覆盖用户已有配置，不自动创建敏感词或白名单条目。

### 10.3 reconcile 流程

reconcile 是“读取 Telegram 实际状态并修复内部关系”，不得自动邀请 Bot 或提升权限：

1. 资产必须存在、未归档且已有 `guardian_bot_account_id`。
2. 设置 `pending`，重新执行双 Profile 和实时权限探针。
3. 内部 `Group` 或同 Bot `ManagedGroupBinding` 缺失时允许幂等补建。
4. 权限满足时将 Binding 设为 `active`、Bot 角色设为 `admin/owner`、资产设为 `managed`。
5. Bot 为普通成员或权限缺失时将 Binding 设为 `degraded`、资产设为 `degraded`。
6. Bot 已离群或被踢时将 Binding 设为 `inactive`、资产设为 `degraded`。
7. 核心群被另一个 Bot 占用时返回 409，禁止替换主 Bot。
8. 每次执行更新 `governance_last_checked_at` 并写审计。

## 11. API 契约

### 11.1 接入治理

```http
POST /api/owned-groups/{asset_id}/governance/bind
Content-Type: application/json

{
  "guardian_bot_account_id": 42
}
```

成功统一返回 HTTP 200；首次创建和幂等复用通过 `data.reused` 区分，避免前端因 200/201 分支产生差异。

### 11.2 查询治理状态

```http
GET /api/owned-groups/{asset_id}/governance
```

GET 只读数据库，不触发 Telegram 请求，不改变时间或状态。

### 11.3 重新检测与修复

```http
POST /api/owned-groups/{asset_id}/governance/reconcile
Content-Type: application/json

{}
```

### 11.4 统一成功响应

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "asset_id": 18,
    "asset_status": "ready",
    "telegram_chat_id": -1001234567890,
    "core_group_id": 311,
    "managed_binding_id": 27,
    "guardian_bot_account_id": 42,
    "guardian_bot_profile_id": 9,
    "owned_bot_profile_id": 16,
    "governance_status": "managed",
    "binding_status": "active",
    "bot_role": "admin",
    "permission_probe": {
      "status": "passed",
      "required_permissions": [
        "can_delete_messages",
        "can_restrict_members",
        "can_invite_users",
        "can_pin_messages"
      ],
      "granted_permissions": [
        "can_delete_messages",
        "can_restrict_members",
        "can_invite_users",
        "can_pin_messages"
      ],
      "missing_permissions": [],
      "checked_at": "2026-09-09T12:00:00Z"
    },
    "capabilities": {
      "verification": true,
      "sensitive_keywords": true,
      "anti_spam": true,
      "warn": true,
      "mute": true,
      "ban": true,
      "announcement": true,
      "pin_message": true,
      "activity": true
    },
    "failure": null,
    "governance_pending_at": null,
    "governance_enabled_at": "2026-09-09T12:00:00Z",
    "governance_last_checked_at": "2026-09-09T12:00:00Z",
    "stale_pending": false,
    "reused": false,
    "correlation_id": "og-gov-18-..."
  }
}
```

`disabled` 状态下，关联 ID、Bot、probe 和 enabled 时间允许为 `null`；`capabilities` 全部为 `false`。

### 11.5 统一错误响应

```json
{
  "detail": {
    "reason": "guardian_permissions_missing",
    "message": "Guardian Bot 缺少治理所需权限",
    "retryable": true,
    "asset_id": 18,
    "missing_permissions": ["can_restrict_members"],
    "correlation_id": "og-gov-18-..."
  }
}
```

| HTTP | Reason Code | 场景 | 可重试 |
| --- | --- | --- | --- |
| 403 | `owned_group_role_forbidden` | 当前用户无操作权限 | 否 |
| 404 | `owned_group_asset_not_found` | 资产不存在 | 否 |
| 409 | `asset_not_ready` | 资产不是 READY 或已归档 | 条件满足后可重试 |
| 409 | `governance_bot_change_not_supported` | 已治理资产请求了不同 Bot | 否 |
| 409 | `managed_binding_conflict` | 核心群已绑定另一个 Bot | 否，需人工处理 |
| 409 | `guardian_bot_identity_mismatch` | 双 Profile 或 getMe 身份不一致 | 否，需修复凭据 |
| 409 | `governance_not_bound` | 未选择 Bot 就执行 reconcile | 否 |
| 422 | `guardian_bot_not_eligible` | 账号类型、状态、风险或 Profile 不合格 | 条件满足后可重试 |
| 422 | `guardian_bot_not_member` | Bot 不在目标群 | 是 |
| 422 | `guardian_bot_not_admin` | Bot 不是管理员或创建者 | 是 |
| 422 | `guardian_permissions_missing` | 缺少必要管理员权限 | 是 |
| 422 | `telegram_chat_type_unsupported` | 目标不是超级群 | 否 |
| 502 | `telegram_probe_failed` | Telegram API 返回非限流失败 | 是 |
| 504 | `telegram_probe_timeout` | 探针超时 | 是 |
| 503 | `governance_feature_disabled` | 功能开关未开启 | 否 |
| 503 | `governance_stop_enabled` | 治理停止开关开启 | 恢复后可重试 |
| 409 | `governance_state_changed` | 探针期间状态被并发修改 | 是 |

## 12. Guardian Worker 接入要求

### 12.1 更新分发门禁

当前 Worker 在分发每条群消息、新成员和离群事件前必须解析有效目标，并满足：

```text
ManagedGroupBinding.telegram_group_id == update.chat.id
AND ManagedGroupBinding.bot_account_id == 当前轮询 Bot account_id
AND ManagedGroupBinding.binding_status == active
```

若该 Telegram Chat ID 对应一个 `OwnedGroupAsset`，还必须满足：

```text
OwnedGroupAsset.status == ready
AND OwnedGroupAsset.governance_status == managed
AND OwnedGroupAsset.managed_binding_id == ManagedGroupBinding.id
AND OwnedGroupAsset.guardian_bot_account_id == 当前轮询 Bot account_id
AND 自建群治理 gate 未停止
```

不满足时只记录计数型跳过日志，不调用验证、规则、处罚、广播或活动处理器。

### 12.2 自动同步边界

- 非自建群继续保留现有 Guardian 自动发现/同步能力，确保旧托管群不回归。
- Telegram Chat ID 已存在于 `OwnedGroupAsset`、但治理状态不是 `managed` 时，普通消息、成员事件和 `my_chat_member` 更新均不得自动创建可执行绑定。
- 已显式绑定的自建群可根据 `my_chat_member` 更新现有 Binding 状态，但不得更换 Bot。
- Worker 发现 Bot 离群或权限下降时，将 Binding 与资产标记为降级；不得继续执行高权限动作。

### 12.3 ID 传递

Worker 解析目标后：

- 查询验证配置、规则、白名单、敏感词、警告和处罚策略时传 `core_group_id`。
- 调用 `sendMessage`、`deleteMessage`、`restrictChatMember`、`banChatMember`、`pinChatMessage` 时传 `telegram_chat_id`。
- 活动筛选按内部关联查询，最终广播按 Telegram Chat ID 分组。

### 12.4 可观测性

Worker 快照新增：

```json
{
  "owned_group_governance": {
    "managed_assets": 2,
    "degraded_assets": 1,
    "processed_updates": 15,
    "skipped_unbound_updates": 4,
    "skipped_mismatched_bot_updates": 1,
    "permission_degraded": 1
  }
}
```

日志只允许记录 `asset_id`、`core_group_id`、`telegram_chat_id`、`managed_binding_id`、Bot account ID、reason code 和 correlation ID。

## 13. 现有治理能力接通要求

绑定成功不等于重新开发治理功能。本阶段必须确保以下现有入口能正确解析目标：

| 能力 | 复用入口 | 本阶段要求 |
| --- | --- | --- |
| 入群验证 | `/api/group-governance/verification` | 外部 Telegram Chat ID 经 resolver 转为 core ID |
| 发言/反垃圾 | `/api/group-governance/moderation` | 策略查询和 Worker 使用同一 core ID |
| 警告/禁言/封禁 | punishment policy + Guardian Worker | DB 用 core ID，Telegram 动作用 chat ID |
| 敏感词 | `/api/moderation-sensitive-keywords` | 群级数据关联 core ID，旧参数兼容 |
| 白名单 | `/api/rules/whitelist` | 群级数据关联 core ID，旧参数兼容 |
| 公告/置顶 | `/api/managed-groups/{id}/pinned-message` | 通过 managed_binding_id 使用目标 chat ID |
| 活动/优惠券 | managed-group campaign | 目标解析正确且 Bot 绑定一致 |

兼容要求：现有前端可继续使用 `groupId` 查询参数，但该值在前端固定表示 Telegram Chat ID；后端必须显式解析，不能再依靠变量名推断。

## 14. 前端需求

### 14.1 `OwnedGroups.vue`

在选中资产的“资产状态与操作”区域增加“Guardian 治理”卡片。

展示字段：

- 治理状态标签。
- Guardian Bot 显示名、用户名和 account ID。
- 最近检测时间。
- Bot 当前角色。
- 已授予权限、缺失权限。
- 可用能力矩阵。
- 最近失败原因和 reason code。

按钮规则：

| 条件 | 按钮 |
| --- | --- |
| 资产非 READY 或已归档 | “接入治理”禁用，并显示原因 |
| `disabled` | “接入治理” |
| `pending` | 按钮 loading，允许刷新状态，不允许选择不同 Bot |
| `managed` | “治理设置”“重新检测” |
| `degraded` | “查看失败原因”“重新检测” |

接入对话框：

1. Bot 下拉框只显示第 7.1 节可选 Bot。
2. 每项显示 Bot 名称、`@username`、account ID、Owned Profile 验证状态和 Guardian 健康状态。
3. 若本地成员记录不是已核验管理员，显示黄色提示，但允许用户发起实时探针。
4. 明确提示“本操作不会自动拉 Bot 入群或授予管理员权限”。
5. 确认按钮调用 bind；成功后刷新资产和治理状态。
6. 失败时展示安全错误摘要及缺失权限，不展示原始异常。

点击“治理设置”跳转：

```text
/guardian/policies
  ?groupId={telegram_chat_id}
  &title={asset.title}
  &botId={guardian_bot_account_id}
  &source=owned_group
  &assetId={asset_id}
```

`OwnedGroups.vue` 同时读取 `route.query.assetId`，从 `ManagedGroups.vue` 返回时自动选中该资产。

### 14.2 `ManagedGroups.vue`

列表新增：

- “来源”列：`自建群` 或 `现有托管群`。
- 自建群资产 ID 和资产标题。
- “查看自建群”按钮，跳转 `/owned-groups?assetId={asset_id}`。

后端 `ManagedGroupBindingResponse` 增加可空字段：

```text
source_type: owned_group | managed_group
owned_group_asset_id: int | null
owned_group_asset_title: string | null
```

来源字段由 `OwnedGroupAsset.managed_binding_id` 外连接推导，不写入 `ManagedGroupBinding`。

### 14.3 前端 API 与 Store

`frontend/src/api/ownedGroups.ts` 增加：

```typescript
getGovernance(assetId: number)
bindGovernance(assetId: number, guardianBotAccountId: number)
reconcileGovernance(assetId: number)
```

`OwnedGroupAsset` 增加数据库关联和治理状态字段；新增强类型 `OwnedGroupGovernanceStatus`、`PermissionProbe`、`GovernanceCapabilities`、`GovernanceFailure`。

`frontend/src/stores/ownedGroup.ts` 增加按 `asset_id` 缓存的治理状态。离开页面时可保留非敏感状态，但不得持久化 Token、邀请链接或原始 Telegram 响应。

## 15. 幂等、并发与一致性

1. 相同资产、相同 Bot 的重复 bind 必须返回同一 Binding，不重复初始化策略。
2. 两个相同 bind 并发时，通过资产行锁和现有唯一约束保证只成功创建一组关系。
3. 两个不同 Bot 并发绑定同一资产时，只有先完成者可成为主 Bot；另一请求返回 409。
4. 外部 Telegram 探针不得在持有长数据库事务或行锁时执行。
5. 探针前后的资产版本必须重新核验；若 Bot、Chat ID 或资产状态变化，丢弃探针结果。
6. 数据库提交失败时不得宣称接入成功；已经存在的 Telegram 状态保持不变，用户可 reconcile。
7. GET 状态不得触发隐式写入、默认策略创建或 Telegram 请求。
8. reconcile 可以补建缺失内部关系，但不得覆盖不同 Bot 的既有绑定。

## 16. 审计要求

新增事件类型：

```text
owned_group_governance_bind_started
owned_group_governance_bound
owned_group_governance_bind_failed
owned_group_governance_reconcile_started
owned_group_governance_reconciled
owned_group_governance_reconcile_failed
owned_group_governance_degraded
owned_group_governance_update_skipped
```

每条接入/检测审计至少包含：

- `group_asset_id`
- `actor_id`
- `before_state` / `after_state`
- `result`
- `reason_code`
- `correlation_id`
- `guardian_bot_account_id`
- `core_group_id`、`managed_binding_id`（存在时）
- 必要权限的布尔快照和缺失权限列表

Token、Session、代理认证信息、完整邀请链接和原始异常栈不得进入审计字段或导出。

## 17. 功能开关与停止语义

阶段 1 依赖阶段 0 提供的治理 gate，语义固定为：

```text
OWNED_GROUP_GOVERNANCE_ENABLED=false  默认关闭新接入和自建群事件消费
governance_stop=true                 运行时停止自建群 Guardian 事件与治理动作
```

要求：

- gate 关闭时，GET 状态仍可用。
- bind 和 reconcile 返回 503，不执行 Telegram 请求。
- 已绑定资产和策略数据保留，不改为 `disabled`，恢复后继续 reconcile。
- `governance_stop` 不停止自建群创建/邀请 Worker，也不停止 acquisition 搜群和投放链。
- `provisioning_stop` 或 `acquisition_stop` 不得隐式停止旧托管群的 Guardian 治理。
- 静态配置必须加入 `.env.example`、`backend/app/core/config.py` 和生产 Compose 中需要运行治理代码的服务。

## 18. 验收用例

### 18.1 正常链路

1. 给定 READY 自建超级群和合格 Guardian Bot，Bot 已拥有四项必要权限；执行 bind 后返回 `managed`，三个关联 ID 非空。
2. 查询数据库只有一个 `Group`、一个 `ManagedGroupBinding` 和每类一个默认治理策略。
3. 再次提交同一 Bot，返回 `reused=true`，所有 ID 不变，记录幂等成功审计。
4. 从自建群页面进入治理策略页，读取与保存均作用于同一个 core group。
5. 在测试适配器中触发新成员、敏感词和刷屏事件，分别进入验证、删除/警告和处罚链。
6. 发送公告并置顶，实际调用使用资产的 Telegram Chat ID。
7. 创建群级活动时，目标绑定和 Bot account ID 与资产关联一致。

### 18.2 权限与失败

8. Bot 不在群内时 bind 返回 422、资产为 `degraded`、reason 为 `guardian_bot_not_member`。
9. Bot 是普通成员时返回 `guardian_bot_not_admin`。
10. 缺少任一必要权限时返回 `guardian_permissions_missing` 并返回精确缺失列表。
11. Bot Token 实际身份与两个 Profile 不一致时返回 409，不覆盖 Profile，不创建绑定。
12. Bot 账号停用、Profile 未验证或所有权不匹配时，Telegram API 不应被调用。
13. 目标不是超级群时返回 `telegram_chat_type_unsupported`。
14. Telegram 超时后资产为 `degraded`；reconcile 成功后恢复 `managed`，不产生重复记录。

### 18.3 冲突与并发

15. 同一资产两个相同 Bot 并发绑定，只有一组关系。
16. 同一资产两个不同 Bot 并发绑定，第二个请求返回 409。
17. 核心群已绑定另一个 Bot 时不得替换，不得更改原 Binding 状态。
18. 探针期间资产被归档或 Chat ID 变化时，旧探针结果不得写回。

### 18.4 Worker 安全边界

19. Guardian Bot 收到未显式接入的自建群消息时，不创建可执行绑定、不执行规则和处罚。
20. 绑定 Bot A 的群更新由 Bot B 收到时，Bot B Worker 必须跳过。
21. Binding 为 degraded/inactive 时，新成员和消息更新不得进入治理处理器。
22. 自建群治理 gate 停止时，旧托管群按原逻辑运行，自建群更新跳过。
23. Bot 被降权后 reconcile 将资产降级；恢复权限后 reconcile 可恢复 managed。

### 18.5 兼容与安全

24. 已有托管群无需自建群资产，原策略、公告、活动和 Worker 流程保持可用。
25. 旧自建群资产迁移后默认 disabled，不自动发送任何 Telegram 请求。
26. 接口响应、应用日志、审计导出和前端状态中均不存在 Bot Token、Session、代理密码或完整邀请链接。
27. 所有写接口鉴权和角色校验覆盖 admin/operator/auditor 边界。
28. Alembic 和生产 SQL 均可在含旧资产的数据库升级；迁移不会删除业务数据。

## 19. 开发任务拆分

| 编号 | 任务 | 主要文件 | 完成标准 | 估算 |
| --- | --- | --- | --- | --- |
| BE-01 | 数据迁移与模型 | `owned_group/models.py`、双迁移文件 | 字段、索引、CHECK、FK 完成；旧数据默认 disabled | 1 人日 |
| BE-02 | 治理服务与权限探针 | 新增 `owned_group/governance.py` | 双 Profile 校验、探针、幂等建链、默认策略、reconcile | 2 人日 |
| BE-03 | 三个治理 API | 新增或扩展 `api/owned_groups.py`/独立 router | 契约、权限、错误码、审计齐全 | 1 人日 |
| BE-04 | Guardian Worker 门禁 | `workers/telegram_worker.py`、Guardian 调用链 | 自建群显式绑定过滤、Bot 匹配、状态降级 | 1–1.5 人日 |
| BE-05 | ManagedGroup 来源读模型 | `api/managed_groups.py` | 列表返回 source 和 asset 链接，不改主表 | 0.5 人日 |
| FE-01 | API 类型与 Store | `api/ownedGroups.ts`、`stores/ownedGroup.ts` | 强类型、状态缓存、错误清洗 | 0.5 人日 |
| FE-02 | 自建群治理卡片与对话框 | `views/OwnedGroups.vue` | Bot 筛选、按钮状态、权限展示、跳转 | 1 人日 |
| FE-03 | 托管群来源展示 | `views/ManagedGroups.vue` | 来源列、返回自建群链接 | 0.5 人日 |
| QA-01 | 后端单元/集成测试 | owned-group、guardian、worker tests | 覆盖第 18 节核心场景 | 1–1.5 人日 |
| QA-02 | 前端测试与构建 | API spec、组件测试、type-check/build | 成功、降级、禁用、跳转均覆盖 | 0.5 人日 |

建议编码顺序：`BE-01 → BE-02 → BE-03/BE-04 → BE-05 → FE-01 → FE-02/FE-03 → QA`。

## 20. 测试文件建议

新增：

```text
backend/tests/unit/modules/owned_group/test_governance_service.py
backend/tests/unit/modules/owned_group/test_governance_api.py
backend/tests/unit/modules/owned_group/test_governance_worker_gate.py
backend/tests/integration/test_owned_group_guardian_bridge.py
frontend/src/api/ownedGroupGovernance.spec.ts
```

扩展：

```text
backend/tests/integration/test_guardian_boundaries.py
backend/tests/unit/core/test_managed_group_operations.py
backend/tests/unit/test_telegram_worker.py
frontend/src/api/ownedGroups.spec.ts
```

后端最低验证命令：

```bash
cd backend
pytest tests/unit/modules/owned_group/test_governance_service.py -v
pytest tests/unit/modules/owned_group/test_governance_api.py -v
pytest tests/unit/modules/owned_group/test_governance_worker_gate.py -v
pytest tests/integration/test_owned_group_guardian_bridge.py -v
pytest tests/integration/test_guardian_boundaries.py -v
```

前端最低验证命令：

```bash
cd frontend
npm run test -- ownedGroups
npm run type-check
npm run build
```

## 21. Definition of Done

阶段 1 只有同时满足以下条件才可标记完成：

- 数据迁移、模型、服务、三个 API、Worker 门禁和两个前端页面全部完成。
- 第 18 节 28 条验收用例均有自动化测试或明确的 test001/Telegram 灰度用例。
- 阶段 0 的 ID 映射与历史冲突检查已完成，没有隐式 `group_id` 猜测。
- 相同 Bot 重试和并发请求不会产生重复 Group、Binding 或默认策略。
- 未显式接入、非 ACTIVE、Bot 不匹配、资产归档或治理停止时均不会执行治理动作。
- 旧托管群的验证、敏感词、白名单、处罚、公告、置顶和活动无回归。
- 本地单测、集成测试、前端 type-check 和 build 通过。
- test001 使用模拟 Telegram 适配器完成全链路验收。
- 真实 Telegram 验收仅在凭据、代理、功能开关和两账号以内灰度授权齐全后执行。
- 最终交付记录代码 SHA、迁移版本、测试结果、目标测试群 Chat ID、Bot 权限快照和回滚开关。

## 22. 开发实施固定决策

以下事项不再留给开发阶段临时决定：

1. 使用 `owned_group_assets` 直连字段，不建新的桥接主表。
2. `governance_status` 与自建群创建状态分离。
3. 请求使用 Guardian Bot 的 `TelegramAccount.id`，不使用两个 Profile 的 ID。
4. Bot 必须同时具备可运行 Guardian Profile 和已验证、同归属的 Owned Bot Profile。
5. 接入接口不自动执行 Telegram 邀请或提权。
6. 四项管理员权限全部满足才进入 `managed`，第一版不提供“部分治理成功”。
7. 相同 Bot 重复绑定幂等返回；第一版不支持直接换 Bot。
8. 未显式接入的自建群不得被 Guardian Worker 自动激活。
9. GET 状态永远只读；reconcile 才执行实时探针和内部修复。
10. 数据库关联使用 core ID，Telegram 动作使用 chat ID，二者必须通过 resolver 显式获得。
