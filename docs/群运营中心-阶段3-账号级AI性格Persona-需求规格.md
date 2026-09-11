# 群运营中心阶段 3：账号级 AI 性格 Persona 需求规格

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.0 |
| 文档状态 | 可直接开发；阶段 2 集成任务须等待阶段 2 契约冻结 |
| 编制日期 | 2026-09-10 |
| 对应总方案 | 群运营中心：自建群治理、AI 运营与外部引流实施计划方案 |
| 对应阶段 | 阶段 3：账号级 AI 性格 |
| 用户确认状态 | 阶段 1 已完成；阶段 2 正在开发 |
| 代码基线 | 当前检出分支 codex/owned-group-foundation；阶段 2 正在并行开发，contracts/models/schemas/target/policy/content/trigger/execution/worker/tasks/API、前端页面及 048/034 迁移草稿均已出现，最终字段、状态机和接线仍以阶段 2 合并结果为准 |
| 前置依赖 | 阶段 1 双 ID/Guardian 治理；阶段 2 group_account_message_policy、execution、ContentService、ExecutionService |
| 预计工作量 | 8–11 人日；若阶段 2 已完成 LLM system prompt、缓存和出站治理 P0 改造，可回落至总方案的 5–8 人日 |

## 1. 本阶段结论

阶段 3 在 TelegramAccount 上增加一份账号级 AI Persona，并把它接入阶段 2 自建群 AI 内容生成链：

~~~text
阶段 2 创建 AI execution
  → 使用 execution.account_id 精确读取账号 Persona
  → 在 execution 创建事务内冻结 Persona 版本与快照
  → 加载全局安全规则和当前群治理规则
  → 组装不可越权的 Prompt
  → LLM 仅生成一条消息正文
  → 阶段 2 内容安全 + Guardian 无白名单出站检查
  → promotion 确定性追加 CTA/链接并再次检查
  → 人工审核、额度、冷却、去重、账号风控
  → 使用阶段 2 已指定账号发送
~~~

Persona 只改变 AI 文案的表达风格、选题偏好和措辞深度，不决定“能否发送、发到哪里、由谁发送、是否审核、发送频率或链接是什么”。

第一版不建立独立 Persona 主表。当前值按总方案保存在 TelegramAccount.ai_persona JSON 中；更新版本、哈希和最后修改信息保存在同一账号记录，使用 OwnedGroupAuditEvent 记录安全摘要。

## 2. 当前基线与开发前置

### 2.1 当前可直接复用

| 能力 | 现有实现 | 阶段 3 用法 |
| --- | --- | --- |
| 账号主数据 | backend/app/core/account/models.py::TelegramAccount | 增加 Persona JSON 和版本元数据 |
| 账号管理 | backend/app/api/accounts.py、frontend/src/views/Accounts.vue | 返回 Persona 摘要，增加独立 Persona 入口 |
| 全局群 AI 设置 | DEFAULT_GROUP_AI_INTERACTION_SETTINGS、normalize_group_ai_interaction_settings | 继续提供总开关、Token、长度、temperature、maxTokens |
| LLM | backend/app/core/ai/llm_client.py::LLMClient | 统一 system prompt 契约、缓存隔离和 Token 计量 |
| 阶段 2 消息契约/模型 | messaging_contracts.py、messaging_models.py、messaging_schemas.py、messaging_target.py | 已有 account_id、content_category、mode、allowed_topics、prompt_context 和 execution 快照骨架 |
| 阶段 2 内容/执行链 | 规格中的 OwnedGroupMessageContentService、OwnedGroupMessageExecutionService | 完成后作为唯一 Persona 消费和执行入口 |
| 群治理规则 | ModerationRule、ModerationSensitiveKeyword、GroupModerationPolicy | 构造治理投影并执行出站校验 |
| 审计 | OwnedGroupAuditEvent | resource_type=account_persona 的安全摘要 |

### 2.2 当前必须补齐的缺口

1. TelegramAccount 当前没有 ai_persona、版本、哈希和更新时间字段。
2. 当前账号通用 PUT 不是 Persona 专用接口，且 exclude_none 无法表达恢复默认。
3. 当前没有 Persona Schema、规范化、乐观锁、预览、重置和审计契约。
4. 当前阶段 2 规格明确使用全局中性 Prompt，阶段 3 需要在同一 ContentService 中插入 Persona。
5. LLMClient 的 LOCAL Provider 当前丢弃 system_prompt，可能使安全和 Persona 规则全部失效。
6. LLM 缓存键未显式包含 provider、max_tokens、execution、account_id 和 Persona revision。
7. LLM Token 估算未计入 system prompt，加入 Persona 后会低估预算。
8. GuardianRuleEngine.evaluate_message 会先执行发送者白名单，不可直接用于 AI 出站校验。
9. 当前阶段 2 文件和 048/034 迁移草稿已出现但仍在并行开发；API、Policy/Trigger/Content/Execution Service、Worker、任务、状态机和注册接线须以阶段 2 最终合并结果复核。

### 2.3 阶段 2 集成前置门槛

以下条件满足前，P3-DB-01/02、P3-BE-02/03/04/05、P3-API-01/02/03 及所有数据库/阶段 2 集成任务可以分支开发，但不得合并：

- 阶段 2 的 group_account_message_policy 和 group_account_message_execution 表结构已冻结。
- messaging_models 已注册进 backend/app/core/database.py、backend/migrations/env.py 和 backend/tests/conftest.py，且 048/034 是唯一 head。
- execution 已有 account_id、mode_snapshot、content_category、policy_revision 和 prompt_context；阶段 3 可在 prompt_context 写入服务端保留的业务快照节点。
- OwnedGroupMessageContentService 是 AI/模板生成的唯一入口。
- OwnedGroupMessageExecutionService 使用 execution.account_id，不会重新选号。
- Speaker 仅调用一次 acquire_by_id，TelegramExecutionService 不访问 AccountPool。
- OWNED_GROUP_MESSAGING_ENABLED/ownedGroupMessaging.enabled 停止门、OWNED_GROUP_MESSAGE 风险动作、审核、额度、冷却、幂等和去重契约已落地。
- 阶段 2 自建群消息与增长中心广告配置隔离测试通过。

Persona 合约、账号字段、专用 API、前端抽屉和纯 Prompt Builder 可以在上述门槛完成前并行开发。

## 3. 范围、目标与非目标

### 3.1 业务目标

1. 每个符合条件的 GROWTH 推广账号可以保存一份全局 Persona。
2. 同一账号在多个自建群中保持一致的基础表达风格。
3. 不同账号的有效 Prompt 能明确体现不同 Persona。
4. community AI 和 promotion AI 均可使用 Persona。
5. 没有 Persona 的账号继续使用安全、确定性的中性默认值。
6. Persona 修改只影响之后创建的新 AI execution。
7. Persona 不降低全局安全、群治理、阶段 2 内容安全和账号风控。

### 3.2 成功标准

- Persona CRUD、重置、预览和审计接口可用，并有 revision 乐观锁。
- Fake LLM 能证明 execution.account_id 与 Persona account_id 一致。
- 两账号同群同主题时，不仅 Prompt Persona 区块不同，预生产实际 AI 预览文本也能按 tone/reply_length 等规则区分。
- Persona 缺失时中性默认生效；损坏配置实际生成时失败关闭。
- template 模式不查询 Persona，不改变模板输出。
- promotion 的 URL 和 CTA 仍只来自阶段 2 promotion_config。
- 生成时与发送前均执行最新群治理规则的无白名单出站检查。
- configured/draft 等 `requires_system_role=true` 路径所选 Provider 必须保留独立 system 层；不能保留时禁止生成。中性兼容路径按第 11.1 节保留阶段 2 Provider 语义。
- Persona 更新不导致账号重连、AccountPool 重载、换号或历史内容漂移。

### 3.3 本阶段明确不做

- 原计划阶段 4 已确认由现有增长中心 `link_url` 能力满足并取消独立开发；不实现可选的自建群目标资产化、邀请链接自动轮换或归因。
- 不把 Persona 接入增长中心广告、外部群 AI 暖场、旧语义回复、私聊客服或 Guardian Bot。
- 不建立独立 Persona 主表、Persona 市场、多 Persona 切换或群级 Persona 覆盖。
- 不实现自动学习、自动画像、长期记忆或从群聊自动修改 Persona。
- 不让 Persona 修改账号职责、群策略、消息类别、审核、额度、冷却、风控或 Telegram 权限。
- 不允许 Persona 生成或覆盖 promotion_config.destination_url/cta_text。
- 不同步修改 Telegram 昵称、头像、公开简介或账号登录环境。
- 不新增发送 Worker、定时任务、Telegram 发送入口或 Persona 专属停止链。
- 不以真实 Telegram 发送作为开发完成的必要条件。

## 4. 角色与权限

| 动作 | admin | operator | auditor | 其他登录用户 |
| --- | --- | --- | --- | --- |
| 查看账号列表 Persona 摘要 | 是 | 是 | 是 | 是 |
| 查看完整 Persona | 是 | 否 | 否 | 否 |
| 新建/修改 Persona | 是 | 否 | 否 | 否 |
| 恢复中性默认 | 是 | 否 | 否 | 否 |
| 使用草稿 Persona 预览 | 是 | 否 | 否 | 否 |
| 修改 Persona 运行时开关 | 是 | 否 | 否 | 否 |
| 查看 Persona 审计摘要 | 是 | 是 | 是 | 否 |
| 查看 execution Persona 元数据 | 是 | 是 | 是 | 按阶段 2 权限 |
| 查看原始历史 persona_snapshot | 默认不通过 API 暴露 | 否 | 否 | 否 |

固定要求：

- PUT、reset、preview 必须显式 Depends(require_admin)，不能只依赖 accounts 路由的登录鉴权。
- account_id 来自路径，actor_id/role 来自 JWT，revision 来自数据库；不得信任客户端自报。
- 通用 AccountCreate/AccountUpdate 不接受 ai_persona。
- 通用 AccountResponse 只返回摘要，不返回 system_prompt 或完整 Persona。

## 5. 标识、适用范围与隔离边界

### 5.1 Persona 归属

~~~text
Persona owner = TelegramAccount.id
~~~

一个 TelegramAccount 第一版最多一份当前 Persona。不得使用以下值解析 Persona：

- OwnedBotProfile.id
- OwnedGroupAsset.owner_account_id
- 群主账号
- AccountPool 当前可用账号
- 第一个在线账号
- Guardian Bot account_id
- Telegram user/chat ID

执行链必须满足：

~~~text
execution.account_id
  == policy.account_id
  == persona owner TelegramAccount.id
  == Speaker 最终 acquire_by_id 的 account_id
~~~

任意不一致返回 PERSONA_ACCOUNT_MISMATCH，不调用 LLM 或 Telegram。

### 5.2 适用账号

Persona PUT、preview 和实际应用仅支持：

~~~text
TelegramAccount.account_type = promoter
AccountOperationConfig.operation_mode = growth
~~~

- 账号离线、inactive 或风险暂停不阻止管理员提前保存 Persona，但实际发送仍由阶段 2 准入和门禁决定。
- guardian_bot 返回 409 PERSONA_ACCOUNT_TYPE_UNSUPPORTED。
- ad_only 返回 409 PERSONA_ACCOUNT_MODE_UNSUPPORTED。
- AccountOperationConfig 缺失时 PUT/preview 返回 409 PERSONA_OPERATION_CONFIG_MISSING；不得沿用账号列表的展示默认值猜成 growth。
- 已有 Persona 的账号从 growth 切到 ad_only 时不删除配置，但 Persona 不生效；切回 growth 后只对新 execution 生效。
- GET 摘要/审计和 reset 不受当前 account_type/operation_mode 限制，确保角色或模式变化后管理员仍能查看安全摘要并清理旧配置。

### 5.3 消费范围

| 链路 | 是否读取 Persona | 规则 |
| --- | --- | --- |
| 阶段 2 community + mode_snapshot=ai | 是 | 影响语气、长度偏好、兴趣和选题排序 |
| 阶段 2 promotion + mode_snapshot=ai | 是 | 只影响正文表达；CTA/URL 不变 |
| 阶段 2 template | 否 | 不查询、不快照、不进入 Prompt |
| 阶段 2 preview | 是 | 保存 Persona 或管理员草稿 Persona |
| 增长中心广告 | 否 | 不影响 AdCreative、计划、许可或投放 |
| 外部群 AI 暖场/语义回复 | 否 | 保持现有行为 |
| 私聊客服 | 否 | 保持现有行为 |
| Guardian Bot 公告/活动 | 否 | 保持现有行为 |

### 5.4 与增长中心隔离

Persona 服务、Prompt Builder 和阶段 2 ContentService 禁止读取或导入：

- AdCampaign、AccountAdBinding、AdCreative、AdDeliveryScheduleState、AdDeliveryLog
- GroupAdProfile、GroupAdPolicyEvent、GroupAdOnlyAssessment、GroupAdHandover、GroupAdOnlyEvent
- auto_ads_enabled、acquisition_stop
- automation.ad_delivery_*、ad_capacity、ad_failure_policy、ad_only_recommendation
- 广告额度、广告冷却、广告预热、许可探测和 tracking link

总方案中的“Persona 不能绕过广告许可”解释为：Persona 不能授予现有增长中心外部广告投放权限；不得据此给自建群 promotion 增加增长广告许可检查。

## 6. Persona v1 领域契约

### 6.1 JSON Schema

~~~json
{
  "schema_version": 1,
  "name": "技术型群友",
  "tone": "自然、克制、简短",
  "interests": ["网络稳定性", "节点速度"],
  "expertise": ["技术排障"],
  "reply_length": "short",
  "preferred_topics": ["使用体验", "配置建议"],
  "forbidden_topics": ["敏感话题", "过度营销"],
  "ad_style": "soft_share",
  "catchphrases": ["我个人更关注稳定性"],
  "language_style": "zh_cn",
  "system_prompt": ""
}
~~~

system_prompt 为总方案兼容字段，对内语义固定为“低优先级自定义风格说明”，绝不能直接作为最高优先级 LLM system message。

### 6.2 字段约束

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| schema_version | Integer | 是 | 固定为 1 |
| name | String | 是 | NFKC 后 1–50 字符 |
| tone | String | 是 | NFKC 后 1–120 字符 |
| interests | String[] | 是 | 0–10 项，每项 1–40 字符 |
| expertise | String[] | 是 | 0–10 项，每项 1–40 字符 |
| reply_length | Enum | 是 | short/medium/long |
| preferred_topics | String[] | 是 | 0–12 项，每项 1–60 字符 |
| forbidden_topics | String[] | 是 | 0–20 项，每项 1–60 字符；v1 按规范化短语包含做确定性硬拦截 |
| ad_style | Enum | 是 | neutral/soft_share/experience_share/problem_solution |
| catchphrases | String[] | 是 | 0–8 项，每项 1–60 字符 |
| language_style | Enum | 是 | auto/zh_cn；两者最终均遵守阶段 2 简体中文输出合同 |
| system_prompt | String | 是 | 0–1000 字符 |

整份规范化 JSON 的 UTF-8 长度不得超过 16384 bytes。

### 6.3 规范化规则

1. Pydantic v2 model_config 使用 extra="forbid"。
2. 所有字符串执行 Unicode NFKC、trim、换行规范化。
3. 移除零宽字符、ASCII 控制字符和 Unicode 双向覆盖字符。
4. 数组删除空项，并按 casefold 后的规范化值去重，保留第一次顺序。
5. preferred_topics 与 forbidden_topics 规范化后不得相交。
6. catchphrases 不得包含 URL、价格、联系方式或 CTA。
7. system_prompt、tone 和列表项禁止模型控制标记、伪造 system/developer/assistant role、密钥索取或显式要求绕过安全/审核/风控。
8. 禁止接收 model、temperature、max_tokens、URL、CTA、account_id、group_id、review、quota、cooldown、risk 等未定义控制字段。
9. 每次 PUT 是完整替换；禁止 JSON Merge Patch 和 ORM 原地局部修改。
10. 规范化后按固定字段顺序、sort_keys=true、ensure_ascii=false、separators=(",", ":") 生成 canonical JSON，并计算 SHA-256。

注入特征扫描属于防御纵深，最终安全仍由 Prompt 优先级和生成后硬校验保证。

### 6.4 中性默认 Persona

ai_persona IS NULL 时不自动回填数据库，运行时使用：

~~~json
{
  "schema_version": 1,
  "name": "中性群友",
  "tone": "自然、克制、简短",
  "interests": [],
  "expertise": [],
  "reply_length": "short",
  "preferred_topics": [],
  "forbidden_topics": [],
  "ad_style": "neutral",
  "catchphrases": [],
  "language_style": "auto",
  "system_prompt": ""
}
~~~

该对象只用于稳定快照、hash 和审计解释。`persona_source_snapshot` 为 neutral_default、feature_disabled_default 或 legacy_default 时，Prompt Builder 必须跳过整个 Persona 风格区块，不应用上面 tone/reply_length/ad_style 等占位值；输出长度、语气和语言完全继承阶段 2 `groupAiInteraction`，从而与阶段 2 中性 Prompt 行为等价。

状态语义：

| 数据/开关 | persona_source_snapshot | 行为 |
| --- | --- | --- |
| ai_persona IS NULL | neutral_default | 使用中性默认 |
| Persona 总开关关闭 | feature_disabled_default | 使用中性默认 |
| 阶段 3 上线前非终态 AI execution | legacy_default | 使用迁移冻结的中性默认 |
| 合法账号 Persona | configured | 使用账号配置 |
| ai_persona={}、Schema 未知或数据损坏 | 无 | PERSONA_CONFIG_INVALID，实际生成失败关闭 |

不得把损坏配置静默当成“未配置”，避免管理员误以为定制风格已生效。

## 7. 数据模型与迁移

### 7.1 TelegramAccount 扩展

| 字段 | 类型 | 空值/默认 | 说明 |
| --- | --- | --- | --- |
| ai_persona | JSON(none_as_null=True) nullable、deferred | SQL NULL | 当前完整 Persona；SQL NULL 表示中性默认 |
| ai_persona_revision | Integer | NOT NULL DEFAULT 0 | 每次有效变更递增 |
| ai_persona_hash | String(64) nullable | NULL | 当前 canonical Persona SHA-256 |
| ai_persona_updated_at | DateTime nullable | NULL | Persona 最近变更时间 |
| ai_persona_updated_by | Integer nullable | NULL | 管理员用户 ID，逻辑关联 |

数据库要求：

- CHECK(ai_persona_revision >= 0)。
- ai_persona IS NULL 时 ai_persona_hash 必须为 NULL。
- ai_persona 非 NULL 时 revision >= 1 且 ai_persona_hash 为 64 位小写十六进制。
- ai_persona 及四个元数据字段放入 SQLAlchemy `deferred_group="account_persona"`；阶段 2模板链读取完整 TelegramAccount 时也不得 SELECT 这些列。账号列表/Persona API 使用一次查询显式 `undefer_group`，禁止逐行 lazy load。
- ai_persona 和 persona_snapshot 使用 `JSON(none_as_null=True)`；reset/模板分支必须写 SQL NULL，禁止写 JSON 字面量 null。
- 时间字段沿用仓库当前约定：数据库保存 UTC naive `DateTime`（服务端以 `datetime.utcnow()` 生成），API 序列化时明确按 UTC 输出并追加 `Z`；不得把服务器本地时区时间直接写入。客户端带 offset 的时间先归一化为 UTC，客户端无 offset 的时间请求返回 422。
- PostgreSQL hash CHECK 使用等价的 64 位 `[0-9a-f]` 约束；SQLite/Alembic 测试用 length/lower/GLOB 等价实现并分别测试。
- 第一版不对 JSON 内部字段建索引，不按 Persona 内容检索。
- SQLAlchemy 使用跨 SQLite/PostgreSQL 的 JSON 类型；服务每次赋值完整新 dict，禁止依赖原地变更追踪。
- ai_persona 不进入 AccountCreate、AccountUpdate 或账号批量导入。

通用 AccountResponse 仅增加：

~~~json
{
  "persona_configured": true,
  "persona_name": "技术型群友",
  "persona_revision": 3,
  "persona_applicable": true
}
~~~

persona_name 只读取已通过 Schema 校验的 name；损坏数据返回 persona_configured=true、persona_name=null、persona_applicable=false。

### 7.2 group_account_message_execution 扩展

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| persona_revision_snapshot | Integer nullable | AI execution 使用的版本；模板为 NULL |
| persona_source_snapshot | String(32) nullable | configured/neutral_default/feature_disabled_default/legacy_default |
| persona_snapshot | JSON(none_as_null=True) nullable | 规范化后的完整运行快照，供单次生成租约内的阶段 2 去重尝试及历史解释；模板写 SQL NULL |
| persona_hash | String(64) nullable | Persona canonical SHA-256 |
| prompt_template_version | String(32) nullable | 固定如 owned-group-persona-v1 |
| prompt_hash | String(64) nullable | 完整 system/user 生成输入指纹 |
| governance_rules_hash | String(64) nullable | 生成时治理规则投影指纹 |

规则：

- mode_snapshot=template 时所有 Persona/Prompt 字段为 NULL，代码不得查询账号 Persona。
- 新建 AI execution 时必须原子冻结 `persona_revision_snapshot`、`persona_source_snapshot`、`persona_snapshot`、`persona_hash`、`prompt_template_version` 五个字段；服务层拒绝这五个字段的半套快照。
- `persona_revision_snapshot` 表示实际应用的运行 Persona 版本：configured 取账号当前 ai_persona_revision；neutral_default/feature_disabled_default/legacy_default 固定为 0 并使用中性对象 hash，不把被关闭配置的 revision 冒充为已应用版本。draft 仅用于 preview，领域对象内部 revision=0，但 API 用 persona_revision=null 表示“未保存”，且不持久化 execution。
- queued AI execution 的 `prompt_hash`、`governance_rules_hash` 允许为 NULL；有输出成功并持久化 content 时，在同一生成结果事务写入本次实际输入的指纹，此后不可覆盖。
- 生成前失败的 execution 允许两个生成指纹为 NULL；configured/neutral_default 成功并进入 pending_review/ready_to_send 后两者必须非 NULL。feature_disabled_default/legacy_default 使用阶段 2 兼容链时只要求 prompt_hash，governance_rules_hash 保持 NULL，不能伪造未执行的阶段 3 治理投影。
- 人工编辑内容只更新 content/content_hash 和审核审计，不更新代表原始 LLM 输入的 prompt_hash。
- persona_snapshot 可在 90 天后与阶段 2 prompt_context 一起清空。
- revision、source、persona_hash、prompt_template_version、prompt_hash、governance_rules_hash 保留 365 天。
- execution 列表只返回 source、name、revision、hash 前 12 位和 Prompt 版本，不返回完整 persona_snapshot/system_prompt。

AI execution 创建时还必须在现有 `prompt_context` 写入服务端保留节点 `business_snapshot_v1`，不新增数据库列：

~~~json
{
  "business_snapshot_v1": {
    "allowed_topics": ["使用体验", "配置建议"],
    "group_title": "测试自建群"
  }
}
~~~

- `OwnedGroupMessageTarget` 增加只读 `group_title: str`，由 Resolver 已加载且完成双 ID 校验的 `Group.title or ""` 提供；不得另按 telegram_chat_id 再查一次群。该节点仅由服务端在创建 execution 的事务内从已锁定 policy 和已解析 target 构造；先删除传入 prompt_context 中同名键，再写入服务端值，客户端、触发器和管理员指令均不得覆盖。
- allowed_topics 按阶段 2 规范化后完整冻结，最多 20 项；group_title 执行第 9.6 节清洗并截断 255 字后冻结。生成、审核和发送重试只读该节点，不回读当前 policy.allowed_topics 或当前群名。
- template execution 不写该节点。所有尚需生成内容的 AI execution 都必须具有该节点；缺少时返回 `EXECUTION_BUSINESS_SNAPSHOT_MISSING`，不得回读当前策略补救。已具有 content 的历史 legacy_default 无需该节点即可继续审核/发送，因为不会再次生成。
- prompt_context 的其他触发上下文仍沿用阶段 2 脱敏规则；该字段 90 天清理只针对终态 execution，不会破坏可重试任务。

### 7.3 迁移与历史数据

阶段 2 若最终使用 SQL 048/Alembic 034，则阶段 3 建议：

~~~text
backend/migrations/049_add_account_ai_persona.sql
backend/migrations/050_add_owned_group_message_persona_snapshot.sql
backend/migrations/versions/035_add_account_ai_persona.py
backend/migrations/versions/036_add_owned_group_message_persona_snapshot.py
~~~

如阶段 2 实际编号变化，以合并后的迁移 head 顺延；不得并列两个 head，且必须同步 backend/scripts/apply_sql_migrations.py。

迁移步骤：

1. 增加 TelegramAccount 五个字段，历史账号保持 ai_persona=NULL、revision=0；同一迁移以 `ON CONFLICT(key) DO NOTHING` 为 `SystemSetting(key='app.runtime_settings')` 补种子行，value 至少含默认 `ownedGroupAiPersona={enabled:false,revision:0,updatedAt:null,updatedBy:null}`，已存在行绝不覆盖。
2. 增加 execution 七个可空快照字段。
3. 进入停写窗口：先把静态与运行时 `OWNED_GROUP_MESSAGING_ENABLED/ownedGroupMessaging.enabled` 都设为 false，阻止全部新建/领取；让正在执行的 generating/sending 优雅完成后，再滚动重启 API，并优雅 drain 后停止所有可能创建或领取 execution 的 Celery、scheduled、keyword、reply、消息事件消费者和 Worker。探针必须证明手工创建 API 返回消息功能关闭且 execution 行数不再增长。只停 Telegram 发送 Worker 不足以进入迁移。
4. 确认 generating/sending 已清零；超时未收敛的任务按阶段 2 租约/未知发送结果合同处置，不由迁移猜测。回填事务对 `group_account_message_execution` 取得 `SHARE ROW EXCLUSIVE` 表锁，确保预检、回填和终检期间没有并发 INSERT/UPDATE 穿透停写门。
5. 对 queued AI execution 冻结中性 Persona、source=legacy_default、prompt_template_version=owned-group-neutral-legacy-v1。迁移必须按 execution.policy_id=policy.id 连接 policy，并验证 `execution.owned_group_asset_id=policy.owned_group_asset_id=asset.id`、`execution.account_id=policy.account_id`、`execution.core_group_id=policy.core_group_id=asset.core_group_id=Group.id`、`execution.telegram_chat_id=asset.telegram_chat_id=Group.group_id`：allowed_topics 只取 policy 当前值，group_title 只取 `Group.title or ""`，一次性回填 business_snapshot_v1 后等待正常生成；不得使用可能已漂移的 OwnedGroupAsset.title。这是无法追溯历史策略时的明确迁移边界。policy、asset、Group 任一缺失、任一 ID 不一致或 telegram_chat_id 为空时，迁移预检失败并输出 execution_id，不猜测、不回读到别的对象。
6. 对已有内容的 pending_review/ready_to_send AI execution 冻结中性 Persona、source=legacy_default、prompt_template_version=owned-group-neutral-legacy-v1；prompt_hash/governance_rules_hash 保持 NULL，不伪造历史指纹。
7. 终态历史 execution 可保持 NULL，API 解释为 legacy_untracked，不反推当前 Persona。
8. 增加约束前执行 ai_persona/hash/revision 一致性检查；同一锁事务终检 `mode_snapshot='ai' AND status='queued'` 的五个 Persona 快照字段及 business_snapshot_v1 缺失数必须为 0，否则整体回滚。
9. 保持停写，部署同一 SHA 的阶段 3 API、调度器和 Worker；确认全部旧进程、旧消费者已退出且新建服务路径会原子写全快照后，才恢复 owned-group messaging 双开关与消费者。任一检查失败时保持消息功能关闭，不允许边回填边开放入口。
10. 升级不修改账号状态、operation_mode、阶段 2 policy、消息内容或 Telegram 数据。downgrade 只从 app.runtime_settings.value 移除 ownedGroupAiPersona 节点并保留全部其他节点和该 SystemSetting 行；即使种子行由 049/035 创建也不整行删除，避免误删升级后其他设置写入。

### 7.4 Persona 审计复用

复用 OwnedGroupAuditEvent：

~~~text
resource_type = account_persona
resource_id   = TelegramAccount.id
group_asset_id = NULL；preview 时可写实际 asset_id
~~~

新增 event_type：

~~~text
account_persona_created
account_persona_updated
account_persona_reset
account_persona_preview_generated
account_persona_preview_failed
~~~

before_state/after_state 只能保存：

- configured、revision、persona_hash。
- changed_fields 字段名列表。
- system_prompt_sha256 和字符长度。
- schema_version、result、reason_code。

禁止保存 Persona 原始自由文本、完整 system_prompt、群聊上下文、完整 Prompt、手机号、Session、Token、代理或 LLM 密钥。

## 8. Persona 解析、冻结与并发

### 8.1 领域对象与解析服务

新增 `AccountPersonaService`，对外只返回不可变领域对象：

~~~python
@dataclass(frozen=True)
class EffectivePersona:
    account_id: int
    source: Literal[
        "configured",
        "neutral_default",
        "feature_disabled_default",
        "legacy_default",
        "draft",
    ]
    revision: int
    persona: PersonaV1
    persona_hash: str
~~~

建议接口：

~~~python
async def resolve_for_execution(
    *,
    db: AsyncSession,
    account_id: int,
    mode_snapshot: str,
    content_category: str,
) -> EffectivePersona | None: ...

async def snapshot_for_new_execution(
    *,
    db: AsyncSession,
    execution: GroupAccountMessageExecution,
    account_id: int,
) -> None: ...
~~~

强制规则：

- 唯一解析键是当前 execution 已冻结的 `account_id`；不得从 AccountPool 当前连接、群 owner、账号列表第一条、最近活跃账号或运行线程上下文反推。
- `mode_snapshot=template` 立即返回 `None`，不得读取 Persona 表字段。
- `mode_snapshot=ai` 但账号不存在、角色不适用、Persona 数据损坏时失败关闭，不得换用其他账号 Persona。
- Persona 只改变语言表现，不得改变 execution 的 account_id、asset_id、Telegram 发送身份、计划时间或审核策略。
- v1 不为 Persona 配置增加 Redis 服务端缓存：执行创建本来就需要锁账号并冻结快照，数据库是唯一事实源；避免引入 revision 指针、失效顺序和陈旧读问题。前端内存缓存与第 11 节 LLM 响应缓存不受此条影响。
- `draft` 只允许 Persona preview 在内存中使用；`snapshot_for_new_execution` 收到 draft 必须拒绝，execution.persona_source_snapshot 永远不能写 draft。

### 8.2 新建 execution 的冻结时点

阶段 2 创建 AI execution 时，在同一个数据库事务内按以下顺序执行：

1. 按固定顺序锁定 message policy、`execution.account_id` 对应 TelegramAccount、再锁 AccountOperationConfig；operation config 不存在立即失败。
2. 校验账号存在、`account_type=promoter` 且 `operation_mode=growth`，并由阶段 2校验账号状态、风险和群成员资格。
3. 读取阶段 2 内容策略、全局 AI 互动配置和 Persona 总开关；将规范化 allowed_topics 和已清洗 group_title 写入 `prompt_context.business_snapshot_v1`。
4. 解析并规范化 Persona；计算 canonical JSON SHA-256。
5. 将 Persona、来源、版本、哈希和 `prompt_template_version` 写入 execution；总开关关闭时版本仍冻结，但后续走阶段 2 中性兼容分支。
6. 创建 execution 并提交事务；提交之后的生成、审核和发送重试只允许从 execution 读取 Persona，不再读取账号当前 ai_persona。全局 AI 设置与群治理仍按阶段 2 和第 9/10 节的时点读取。

不得在定时任务真正运行、LLM 调用或 Telegram 发送时重新读取“当前 Persona”，否则同一 execution 的 Persona 会漂移。阶段 2 当前没有跨 tick 生成重试状态机：LLM/Prompt/治理生成失败直接进入终态 failed/skipped，不设置 next_retry_at、不再次领取；修复后由管理员或下一次触发创建新 execution。阶段 2 在同一 generating 租约内为内容去重执行的最多 3 次同步生成保留，但它不是失败重试，始终使用同一冻结快照。输出成功并持久化 content 时同事务写 prompt_hash，governance_rules_hash 按第 7.2 节来源条件写入。

### 8.3 配置更新的乐观并发

PUT/RESET 均要求 `expected_revision`，服务端先规范化目标值，再按固定顺序对 TelegramAccount、AccountOperationConfig 执行 `SELECT ... FOR UPDATE`（配置缺失时 reset 仍可继续清理账号行）：

1. PUT 目标 hash 与当前 hash 相同，或 RESET 时当前已为 NULL：按安全重放幂等成功返回；即使 expected_revision 是该请求成功前的旧值，也不递增、不更新时间、不写审计。
2. 目标值不同且当前 revision 与 expected_revision 不同：返回 `409 PERSONA_REVISION_CONFLICT`，响应带当前 revision 与摘要。
3. 目标值不同且 revision 匹配：revision 加一，原子更新全部 Persona 字段并写审计。
4. 审计写入失败：整个配置事务回滚。

所有修改 `AccountOperationConfig.operation_mode` 的现有入口也必须采用 TelegramAccount → AccountOperationConfig 的同一锁顺序；否则 Persona PUT/执行创建与 growth→ad_only 切换存在竞态。模式切换先获得锁者决定 execution 是否可创建，后获得者看到提交后的确定状态。

客户端不得以最后写入覆盖处理 409；必须提示“配置已被其他管理员更新”，重新加载后由用户确认再提交。

### 8.4 配置变更对任务的影响

| execution 时点/模式 | 更新后行为 |
| --- | --- |
| 尚未创建的新 AI execution | 使用新 revision |
| 已创建、queued（含 scheduled_at 未到）的 AI execution | 保持创建时快照 |
| generating/pending_review/ready_to_send/sending 的 AI execution | 保持原快照 |
| ready_to_send 且设置 next_retry_at 的 AI execution | 保持原快照和已持久化 content；只重试发送，不重新生成 |
| 模板 execution | 始终不使用 Persona |
| failed/skipped/sent/rejected/expired/cancelled | 不追溯修改 |

产品不提供“批量刷新存量任务 Persona”或单任务强制换 Persona。pending_review 可按阶段 2拒绝后新建；queued/ready_to_send 如必须作废，使用阶段 2 `policy.enabled=false` 或对应类别 mode=off 的既有取消语义，再重新启用并新建。sending 不强杀，禁止直接改数据库状态。

## 9. Prompt 组装合同

### 9.1 唯一组装入口

新增 `OwnedGroupPromptBuilder`，阶段 2 的 AI 社区消息、AI 推广消息和 Persona 预览必须复用同一入口：

~~~python
def build(
    *,
    execution: ExecutionPromptInput,
    group: OwnedGroupPromptGroupContext,
    persona: EffectivePersona,
    global_ai_settings: Mapping[str, Any],
    governance: GovernanceProjection,
    context: list[UntrustedContextMessage],
) -> BuiltPrompt:
    """返回 system_prompt、user_prompt、requires_system_role、effective_constraints 和各输入 hash。"""
~~~

禁止在路由、调度器、前端或 Telegram 发送层自行拼接 Persona 文本。`prompt_template_version` 初始固定为 `owned-group-persona-v1`；模板结构发生影响结果的变化必须升版本。

Prompt Builder 接收 `get_group_ai_interaction_settings()` 返回且已由 `normalize_group_ai_interaction_settings()` 规范化的只读 Mapping，不引用当前不存在的 ORM/Pydantic 类型。必须用显式版本注册表分派，例如 `{"owned-group-persona-v1": build_v1}`。execution 创建时冻结版本；代码升级后尚未生成的 queued 任务仍用冻结版本，不得自动套用最新模板。旧版本至少保留到该版本非终态 execution 清零；缺失版本写 `PROMPT_TEMPLATE_VERSION_UNSUPPORTED` 并失败关闭。

为保证默认关闭不破坏已完成的阶段 2，先从当前 ContentService 提取唯一纯函数 `build_phase2_neutral_prompt(...) -> Phase2PromptParts`，其 `system_prompt` 与 `user_prompt` 在相同规范化输入下必须和阶段 2 当前实现逐字一致，包括 `groupAiInteraction.systemPrompt` 在 user prompt 的“全局安全要求”位置及作为独立 system 参数的既有用法。构建器再按 persona_source 固定分支：

- neutral_default/feature_disabled_default/legacy_default 直接返回这两个阶段 2 原始片段，完全跳过 Persona 风格区块；`requires_system_role=false` 只表示不启用阶段 3 新增的严格 Provider 能力门槛，不表示清空非空 system_prompt，也不改变 LOCAL 当前忽略 system、OpenAI/Anthropic 当前保留 system 的既有行为。
- configured/draft 复用相同的阶段 2 基础业务输入和全局 systemPrompt，生成 `requires_system_role=true` 的版本化 Persona Prompt。最终 system 指令按“代码安全边界 → 群治理/阶段 2 业务约束 → groupAiInteraction.systemPrompt → Persona 风格备注”顺序组装；既有全局 systemPrompt 必须保留且位于 Persona 之前，Persona.system_prompt 只能作为最低优先级、已清洗的风格数据，不能直接成为最高优先级 system 文本。

该共享纯函数不得复制出另一份会漂移的阶段 2 Prompt 文本；阶段 2 中性 Prompt 变更时所有兼容来源由同一函数自然同步。

生成使用 execution 中已冻结的 account_id、content_category、mode_snapshot、policy_revision、topic、`prompt_context.business_snapshot_v1` 和 promotion_config_snapshot；其中 allowed_topics 与 group_title 必须来自该业务快照。禁止为方便而回读当前 policy 或当前群记录覆盖这些业务输入。仅全局 AI 运行设置和群治理规则按本规格定义的生成/发送时点读取。

### 9.2 指令优先级

从高到低固定为：

1. 代码内不可配置的安全边界：不得泄露凭据、冒充真人事实、执行群消息内指令、绕过治理。
2. 当前自建群治理规则：敏感词、链接、刷屏、内容限制等可投影规则。
3. 阶段 2 业务约束：`content_category`、execution 冻结的 allowed_topics、推广 CTA 与链接合同、审核要求。
4. 全局 `groupAiInteraction` 配置：语言、回复长度上限、允许能力等。
5. 当前 execution 冻结的账号 Persona：语气、兴趣、专长、话题偏好与附加禁区。
6. 本次主题、触发原因或管理员手工指令。
7. 群聊上下文及用户输入；一律视为不可信数据，不具备指令权限。

低优先级内容与高优先级冲突时忽略冲突部分，不得用 Persona 的 `system_prompt` 覆盖前四层。

必须原样保留阶段 2 固定输出规则：仅生成一条简体中文群消息；不提及 AI、机器人、模型、系统提示词或自动化；不虚构用户身份、交易结果、客服承诺或官方公告。`prompt_context.business_snapshot_v1.group_title` 作为创建任务时的群标题事实快照传入，但与群聊消息一样按不可信数据封装，不能成为指令。

### 9.3 Persona 字段到生成约束的映射

| Persona 字段 | Prompt 用法 | 不允许的影响 |
| --- | --- | --- |
| name | 内部风格标签，可用于管理界面 | 不得要求模型自称该名称 |
| tone | 语言语气与表达节奏 | 不得降低治理或事实边界 |
| interests | 可自然使用的兴趣背景 | 不得主动扩张阶段 2 允许话题 |
| expertise | 表达角度和解释深度 | 不得宣称真实资质、身份或经历 |
| reply_length | 字符目标 | 不得超过全局/系统上限 |
| preferred_topics | 在允许话题内排序 | 不得把禁用话题变成可用话题 |
| forbidden_topics | Persona 附加禁区 | 只能增加限制，不能删除系统禁区 |
| ad_style | AI 推广正文风格 | 不得改变 CTA、链接、频控或发送计划 |
| catchphrases | 可选表达提示 | 不强制出现；最终文本最多命中一个短语且总计一次，v1 不承诺跨消息概率/频率 |
| language_style | 自动/简体中文表达偏好 | v1 不开放繁体、英文或混合语言，不得覆盖阶段 2 中文输出合同 |
| system_prompt | 低优先级自定义风格备注 | 不作为 LLM 最高优先级 system 消息直接透传 |

### 9.4 有效约束计算

- 有效允许话题：execution 冻结的 `business_snapshot_v1.allowed_topics` 为根边界；Persona preferred_topics 只参与交集后的排序。阶段 2未限制时也不得把 preferred_topics 解释为“只允许这些话题”。
- 有效禁区：系统禁区 ∪ 群治理禁区 ∪ 阶段 2禁区 ∪ Persona forbidden_topics。
- configured/draft 的有效长度：`min(Persona 目标长度, groupAiInteraction.replyMaxChars, 500)`；Persona 目标为 short=80、medium=180、long=320 个 Unicode 字符。
- neutral_default/feature_disabled_default/legacy_default 的有效长度：`min(groupAiInteraction.replyMaxChars, 500)`；不得套用中性对象中的 short 占位值。
- 长度统计针对最终待发文本；推广消息在拼接 CTA/链接后仍需满足 Telegram 与阶段 2 最大长度规则。若 CTA/链接已占满预算，正文必须缩短而不是截断链接。
- AI 推广只允许 Persona 改写正文。阶段 2 `promotion_config.cta_text` 和 `destination_url` 由确定性代码原样追加；v1 不生成追踪参数、追踪链接或额外披露文本，禁止 LLM 改写、删除或新增链接。
- community 继续禁止注册链接、邀请链接、价格、折扣、购买引导和私聊导流；promotion 继续强制人工审核。Persona 不得改变这两条阶段 2合同。
- 中性默认不增加兴趣、专长、口头禅、推广偏好或自定义提示。
- 中性三类来源不向 Prompt 注入 name、tone、reply_length、ad_style 或“中性 Persona”说明，确保阶段 2 原 Prompt 结构与行为不因未配置 Persona 改变。

### 9.5 治理规则投影

`GovernanceProjectionService` 按 `core_group_id` 读取当前生效的 `ModerationRule`、`ModerationSensitiveKeyword` 和 `GroupModerationPolicy`，不得使用 `telegram_chat_id` 查询数据库治理对象。查询语义拆开固定：ModerationRule 与 ModerationSensitiveKeyword 使用 `enabled=true AND (group_id IS NULL OR group_id=:core_group_id)`；GroupModerationPolicy 没有 enabled 字段，只读取唯一的 `group_id=:core_group_id` 群级行，不引用 nullable global 行、不做本阶段未实现的全局覆盖合并。群级 policy 缺失或返回多行均按 `GOVERNANCE_CONTEXT_UNAVAILABLE` 失败关闭。

传入 Prompt 的安全摘要固定为：

~~~json
{
  "core_group_id": 128,
  "blocked_keyword_categories": ["sensitive"],
  "forbidden_terms": ["示例词"],
  "active_domain_rule_count": 2,
  "active_frequency_rule_count": 1,
  "active_image_rule_count": 0,
  "moderation_policy_hash": "sha256",
  "rules_revision": "sha256"
}
~~~

- `forbidden_terms` 来源仅为生效 `ModerationRule.rule_type=keyword` 的 pattern 与生效 `ModerationSensitiveKeyword.normalized_text`；`blocked_keyword_categories` 为上述敏感词 category 的去重排序值。两者 NFKC 清洗、去控制字符后按 `source_type, group_scope, id` 稳定排序。
- forbidden_terms 最多向 Prompt 投影 200 条、单条最多 64 字；超长项不截断、不进入 Prompt。全部 keyword、sensitive-keyword、domain 规则仍由生成后和发送前的确定性检查器执行，不能只检查投影前 200 条。
- frequency/image 规则只向 Prompt 提供计数，不传 pattern；文本消息的频控沿用阶段 2 发送门禁，image 规则不对本阶段纯文本生成赋予新语义。
- 当前代码尚未把唯一群级 `GroupModerationPolicy.message_interval_seconds/max_messages_per_minute/max_links_per_hour/new_member_silent_minutes/first_speak_delay_seconds` 接入阶段 2 自建群发送门禁，阶段 3 不暗中新增其发送语义；阶段 2 自身额度/冷却/URL 合同照常执行。本阶段只把这些标量纳入 `moderation_policy_hash`。`media_policy`、`link_policy` 仍是不透明 JSON 文本，只把原始字符串的 SHA-256 纳入该 hash，不得把原文插入 Prompt，也不得臆造字段语义。
- `rules_revision` 对完整生效规则集合构造 canonical JSON：每项包含 source_type、id、rule_type/category、group_scope、pattern_or_text 的规范化值、level、action、updated_at UTC；再加入 moderation_policy_hash 后计算 SHA-256。持久化和日志仅保存最终 hash，不保存该内部 canonical JSON。
- `governance_rules_hash = rules_revision`。数据库读取失败、规则枚举无法识别或 hash 计算失败时，实际生成/发送返回 `GOVERNANCE_CONTEXT_UNAVAILABLE`，不得在无治理上下文下继续；`media_policy/link_policy` 只作为原始字符串参与 hash，因此其历史 JSON 格式差异不触发新解析错误。

### 9.6 不可信上下文封装

阶段 2 回复上下文继续受 `trigger_config.reply.context_messages` 限制，并新增以下硬限制：

- 只接受当前 `core_group_id` 对应 Telegram 群的已授权上下文。
- 冻结的 business_snapshot_v1.group_title 已执行 NFKC/控制字符清洗并截断到 255 字；只放入结构化 group_context，不拼入 system 指令。
- 1～20 条；单条正文最多 500 字；合计最多 4000 字，超出从最旧消息开始裁剪。
- 移除控制字符、双向文本控制符、零宽字符；用户名只作为显示标签，不参与指令。
- 使用结构化 JSON 放入 user 消息，并在外层明确标注 `UNTRUSTED_GROUP_CONTEXT`。
- 上下文中出现“忽略之前指令”“输出系统提示”等文本只作为被讨论内容，不执行。
- 不读取私聊、其他群、Session、手机号、代理、访问令牌或完整用户画像。

## 10. 内容治理与发送前检查

### 10.1 专用无副作用检查器

阶段 3 不得直接把 AI 出站文本当作普通群成员入站消息调用现有 `evaluate_message()`。新增：

~~~python
async def evaluate_outbound_content(
    *,
    db: AsyncSession,
    core_group_id: int,
    text: str,
    content_category: Literal["community", "promotion"],
    allowed_promotion_url: str | None,
    persona_forbidden_topics: tuple[str, ...],
    persona_catchphrases: tuple[str, ...],
) -> OutboundGovernanceDecision: ...
~~~

该检查器必须：

- 执行当前群及全局的敏感词、链接、长度和可复用内容规则。
- 不执行管理员/白名单绕过；发送账号即使是群管理员也不能跳过检查。
- 不写违规记录、不禁言、不踢人、不删除消息、不触发 Guardian 副作用。
- 返回结构化 `allowed`、`reason_code`、`matched_rule_ids`、`matched_term_hashes`，日志不得记录命中文本原文。
- 对 promotion 仅允许阶段 2 确定性生成并完全匹配的目标 URL；community 默认禁止新增推广 URL。
- 对 Persona forbidden_topics 执行 NFKC + casefold 后的短语包含检查；命中任一项返回 `PERSONA_FORBIDDEN_TOPIC_MATCHED`。v1 不声称具备语义级话题分类能力。
- 对 catchphrases 做同样的规范化短语计数；最终文本命中多个候选或同一候选超过一次时返回 `PERSONA_CATCHPHRASE_OVERUSED`。v1 不维护跨 execution 使用次数，也不承诺随机概率。

### 10.2 三次检查时点

以下三次阶段 3 检查只对 `persona_source_snapshot in (configured, neutral_default)` 的实际 execution 生效；所有真正调用 LLM 的 Persona preview 无论运行时开关是否打开，都执行同一生成后检查。`feature_disabled_default` 和 `legacy_default` 实际 execution 走阶段 2 原有内容校验/发送门禁，不进入本节新增检查器；这是默认关闭和紧急回滚保持阶段 2 行为不变的代码级旁路。

1. LLM 生成正文后检查正文；失败则不进入 CTA 拼接。
2. 推广正文拼接确定性 CTA/链接后检查完整文本；社区消息直接检查最终文本。
3. Telegram 发送前读取最新治理规则再次检查最终文本，覆盖“生成后规则被管理员更新”的情况。

人工审核中如允许编辑正文，每次保存只重新计算 `content_hash`，保留原始生成输入的 `prompt_hash`，并立即执行第 2 次检查（包含 Persona 禁用短语）；实际发送仍执行第 3 次检查。编辑前后 content_hash 写阶段 2 审核审计。

适用本节检查时，`execution.governance_rules_hash` 固定保存生成时投影 hash，不在发送前覆盖。第 3 次检查把 `generation_governance_rules_hash` 与 `send_governance_rules_hash` 写入阶段 2 `message_execution_sent/skipped` 审计安全摘要；规则变化但仍允许时可发送，变化后阻断时写 `CONTENT_POLICY_CHANGED`。

### 10.3 拒绝与重试语义

| 场景 | execution 状态与 error_code | 是否自动重试 |
| --- | --- | --- |
| Prompt 输入损坏/Persona 非法 | failed；PERSONA_CONFIG_INVALID | 否 |
| 业务快照缺失 | failed；EXECUTION_BUSINESS_SNAPSHOT_MISSING | 否；仅修复/迁移后新建任务 |
| 冻结 Prompt 版本无构建器 | failed；PROMPT_TEMPLATE_VERSION_UNSUPPORTED | 否；恢复版本代码后新建 execution |
| 治理上下文不可用 | failed；GOVERNANCE_CONTEXT_UNAVAILABLE | 否；恢复后新建 execution |
| Prompt 超出不可裁剪预算 | failed；AI_PROMPT_TOO_LARGE | 否 |
| Provider 不支持 system/fallback 不安全 | failed；AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED/AI_PROVIDER_UNSAFE | 否；修复配置后新建 execution |
| LLM 输出命中治理 | skipped；CONTENT_POLICY_BLOCKED | 否，避免换词绕过 |
| 最终 CTA/链接检查失败 | failed；PROMOTION_COMPOSITION_INVALID | 否 |
| 发送前规则变更导致阻断 | skipped；CONTENT_POLICY_CHANGED | 否 |
| LLM 临时超时/限流/空响应 | failed；沿用阶段 2 既有 Provider 错误码 | 否；不设置 next_retry_at，恢复后新建 execution |
| generating 租约过期 | failed；AI_GENERATION_LEASE_EXPIRED | 否；不重新调用 LLM |

表中分号后的代码写入 `GroupAccountMessageExecution.error_code`；`error_message` 只写固定脱敏文案。

禁止把提供商错误字符串、兜底异常文案或未通过治理的内容当作消息发送。

## 11. LLM 调用、缓存与 Token 预算

### 11.1 提供商能力门槛

- 每个 Provider 适配器显式声明不可变能力 `supports_system_role: bool`，LLMClient 暴露 `capabilities()`；能力检查必须发生在缓存读取和网络调用之前。
- OpenAI/Anthropic 只有在适配器合同测试证明请求分别发送 system/user 角色后才可声明 true；当前 LOCAL 的 `_call_local(prompt, ...)` 丢弃 system_prompt，初始必须声明 false，直到接口改造和合同测试通过。
- `BuiltPrompt.requires_system_role=true` 时，适配器必须保留 `system_prompt` 与 `user_prompt` 的角色语义；否则实发/预览在调用前失败关闭并返回 `AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED`。不得把 system 文本简单拼入用户文本后宣称等价。
- 应用启动及保存 `groupAiInteraction` Provider 配置时做本地能力检查，不发真实模型请求；不安全的提供商显示为“Persona AI 不可用”。Persona PUT 只保存风格配置，不重复探测 Provider。
- 自定义 OpenAI-compatible base_url 还必须在预生产执行“冲突 system/user 指令”canary 并留下 provider、base_url origin hash、model、时间和通过结果；未通过不得打开 Persona 运行时开关，canary 不写真实 Persona 或群内容。
- 自动 fallback 只能落到同样支持 system 指令且通过能力检查的提供商，否则返回 `AI_PROVIDER_UNSAFE`。
- `requires_system_role=false` 的中性兼容分支允许使用阶段 2 已支持的 Provider，并原样保留阶段 2 可能非空的 system_prompt 及各 Provider 的既有处理语义；因此 Persona 双开关关闭或账号未配置不会仅因 LOCAL=false 而关闭阶段 2 中性 AI。该标志只是 Stage3 严格能力门槛，不是“system_prompt 为空”的断言；此例外不得用于 configured/draft Persona。

### 11.2 LLMClient 合同变更

禁止修改现有 `LLMClient.generate(prompt, model, temperature, max_tokens, system_prompt) -> str` 的公开签名、返回类型、缓存版本和 Provider 兼容语义；所有阶段 3 之外的调用方继续使用该入口。阶段 3 新增结构化缓存上下文和独立入口：

~~~python
@dataclass(frozen=True, slots=True)
class LLMCacheContext:
    cache_scope: str
    execution_id: int | None
    account_id: int
    persona_revision: int | None
    generation_attempt: int
    prompt_template_version: str
    persona_hash: str
    governance_rules_hash: str | None
    policy_revision: int
    asset_id: int
    content_category: Literal["community", "promotion"]
    preview_nonce: str | None = None


async def generate_response(
    self,
    *,
    system_prompt: str,
    user_prompt: str,
    requires_system_role: bool,
    cache_context: LLMCacheContext,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LLMResponse: ...
~~~

`generate_response()` 只供阶段 3 Persona 路径调用；`requires_system_role=true` 时在读缓存前执行第 11.1 节能力门槛，false 时保持阶段 2 Provider 语义。所有适配器在该新入口返回统一的 content、provider、model、usage、finish_reason、request_id；错误通过异常或结构化失败返回，不允许混入 content。新旧入口可共用私有 Provider 调用函数，但 `generate()` 必须继续只返回 str，禁止让旧调用方被动迁移。

`LLMCacheContext` 是必填的不可变值对象，构造时校验正整数 ID、generation_attempt=1..3、64 位小写十六进制 hash、已知模板版本和内容类别。实发要求 execution_id 非空、preview_nonce=NULL；预览要求 execution_id=NULL、preview_nonce 为本次 UUID，draft 的 persona_revision 可为 NULL。调用方负责传完整业务材料，LLMClient 负责注入解析后的 provider/model/temperature/max_tokens 和 system/user hash，并按第 11.3 节 canonical 化；禁止调用方先拼不透明字符串代替独立字段。

### 11.3 缓存隔离

现有仅由 Prompt/模型/温度组成的 MD5 键不足以隔离 Persona，阶段 3 改为 canonical JSON + SHA-256。键材料至少包含：

~~~json
{
  "cache_schema_version": "llm:v2",
  "provider": "openai",
  "model": "configured-model",
  "temperature": 0.7,
  "max_tokens": 300,
  "system_prompt_sha256": "...",
  "user_prompt_sha256": "...",
  "provider_endpoint_sha256": "64-char-sha256",
  "cache_scope": "execution:9001:account:17:persona:3:generation:1",
  "execution_id": 9001,
  "account_id": 17,
  "persona_revision": 3,
  "generation_attempt": 1,
  "prompt_template_version": "owned-group-persona-v1",
  "persona_hash": "...",
  "governance_rules_hash": "...",
  "policy_revision": 6,
  "asset_id": 25,
  "content_category": "community",
  "preview_nonce": null
}
~~~

- canonical JSON 固定 UTF-8、字段名升序、无额外空白，NULL/布尔/数字使用 JSON 标准表示；LLMClient 必须拒绝缺字段、未知字段或与已解析调用参数冲突的材料，完整对象 SHA-256 后才生成 Redis key。
- LLMClient 在选定实际 Provider 后自行注入 `provider_endpoint_sha256`。官方端点使用 `sha256("official:{provider}:default")`；自定义 base_url 只对规范化后的 `lowercase scheme + IDNA lowercase host + 非默认 port + 规范化 base path` 计算 SHA-256，禁止 userinfo/query/fragment，绝不把原始 URL、凭据或请求参数放进键、日志、指标。相同 provider/model 但 endpoint hash 不同必须 cache miss；发生 fallback 时按实际 fallback Provider/endpoint 重新做能力检查和键计算，禁止把结果写到首选 Provider 的键。
- 实际任务的 cache_context 必须分别提供 execution_id、account_id、persona_revision 和本次 generating 租约内的 generation_attempt；cache_scope 仅是可读隔离维度，不能替代这些独立字段。禁止跨账号、跨 execution 或跨去重尝试命中缓存。generation_attempt 是 1..3 的进程内局部值，不复用发送 attempt_count，也不新增数据库生成重试字段。
- preview 使用一次性 UUID scope，与实发缓存完全隔离；相同预览重复请求也不得复用实际任务内容。
- 不得在缓存键、日志或指标 label 中保存原始 Prompt/Persona；只保存 hash。
- Redis key 固定为 `llm:v2:{sha256(canonical_key_material)}`；value 为不含 Prompt 的 LLMResponse 安全信封。Persona 场景 TTL 沿用现有默认 3600 秒且上限 3600 秒，更新 Persona 不扫描缓存，新 revision/hash 自然 miss。
- 缓存只写内容非空且 Provider 调用成功的响应；异常、空文本、治理结果和错误文案不缓存。读取失败按 cache miss 调 Provider，写入失败只告警并返回已成功生成的内容；preview 限流 Redis 故障仍按 12.5 失败关闭，两者不可混用。

### 11.4 Token 与调用顺序

- Token 预算必须计算 system_prompt + user_prompt + 预留响应，不得只计算用户文本。
- 超预算时按“最旧上下文 → 非关键 Persona 描述 → 可选示例”顺序裁剪，安全边界、治理规则、业务限制和 CTA 合同不可裁剪。
- 裁剪后重新计算 prompt_hash；仍超限返回 `AI_PROMPT_TOO_LARGE`，不调用提供商。
- LLM 调用只发生在 execution、内容策略、Persona 与治理摘要均已冻结或读取成功之后。
- 每个 execution 的 LLM 调用次数、输入/输出 Token、费用继续进入阶段 2 统计。系统级聚合进入低基数指标；按 account_id、persona_source 的聚合使用第 15.1 节结构化 usage 事件离线统计，account_id 不进入 Prometheus label。

## 12. HTTP API 规格

### 12.1 路由与鉴权

基础路径：`/api/accounts/{account_id}/ai-persona`。全部接口沿用现有 JWT 与阶段 2 owned-group message 响应信封，顶层统一使用 correlation_id 并回传 `X-Correlation-ID`；中间件不得返回敏感自由文本。

| 方法 | 路径 | 用途 | 权限 |
| --- | --- | --- | --- |
| GET | /api/accounts/{account_id}/ai-persona | 查看完整配置与适用性 | admin |
| PUT | /api/accounts/{account_id}/ai-persona | 全量创建/更新 | admin |
| POST | /api/accounts/{account_id}/ai-persona/reset | 恢复中性默认 | admin |
| POST | /api/accounts/{account_id}/ai-persona/preview | 使用已保存或草稿 Persona 预览 | admin |
| GET | /api/accounts/{account_id}/ai-persona/audit-events | 查看安全审计摘要 | admin/operator/auditor |

PUT、reset、preview 显式依赖 `require_admin`；审计读取复用 `_AUDIT_READER_ROLES` 语义。不得依赖 accounts 路由仅登录即可访问的全局依赖。

所有成功响应沿用阶段 2 信封；下文“data payload”均放入 `data`，并在顶层返回 `correlation_id`：

~~~json
{
  "data": {"account_id": 17},
  "correlation_id": "req-uuid"
}
~~~

统一错误体：

~~~json
{
  "error": {
    "code": "PERSONA_REVISION_CONFLICT",
    "message": "Persona revision conflict",
    "details": {
      "field_errors": [],
      "current_revision": 4
    },
    "retryable": false
  },
  "correlation_id": "req-uuid"
}
~~~

`message` 使用固定安全文案；自由文本校验问题只返回字段名和错误类型，不回显客户端原值。

### 12.2 GET 完整 Persona

~~~http
GET /api/accounts/17/ai-persona
~~~

200 data payload：

~~~json
{
  "account_id": 17,
  "account_type": "promoter",
  "operation_mode": "growth",
  "persona_applicable": true,
  "blocking_reason": null,
  "configured": true,
  "revision": 3,
  "persona_hash": "64-char-sha256",
  "persona": {
    "schema_version": 1,
    "name": "技术型群友",
    "tone": "自然、克制、简短",
    "interests": ["网络稳定性", "节点速度"],
    "expertise": ["技术排障"],
    "reply_length": "short",
    "preferred_topics": ["使用体验"],
    "forbidden_topics": ["过度营销"],
    "ad_style": "soft_share",
    "catchphrases": ["我个人更关注稳定性"],
    "language_style": "zh_cn",
    "system_prompt": ""
  },
  "updated_at": "2026-09-10T10:00:00Z",
  "updated_by": {
    "id": 2,
    "username": "admin"
  },
  "feature": {
    "static_enabled": true,
    "runtime_enabled": true,
    "effective_enabled": true,
    "runtime_will_apply": true
  },
  "limits": {
    "max_total_bytes": 16384,
    "max_system_prompt_chars": 1000
  },
  "preview_targets": [
    {
      "owned_group_asset_id": 25,
      "group_name": "测试自建群",
      "policy_id": 8,
      "policy_revision": 6,
      "available_categories": ["community", "promotion"],
      "governance_status": "degraded",
      "runtime_send_eligible": false,
      "blocking_reasons": ["GOVERNANCE_NOT_MANAGED"]
    }
  ]
}
~~~

行为：

- 从未配置时 `configured=false`、revision=0、hash/persona/updated_at/updated_by 为 null。
- 配置后执行过 reset 时 `configured=false`、revision>0、hash/persona 为 null，updated_at/updated_by 保留本次 reset 信息。
- 账号不存在返回 404 `ACCOUNT_NOT_FOUND`。
- guardian_bot 返回 200 但 persona_applicable=false、blocking_reason=`PERSONA_ACCOUNT_TYPE_UNSUPPORTED`；前端只读展示，不显示保存/预览。
- promoter + ad_only 返回 200 但 persona_applicable=false、blocking_reason=`PERSONA_ACCOUNT_MODE_UNSUPPORTED`；已存 Persona 仍返回，便于切回 growth 后继续使用。
- AccountOperationConfig 缺失返回 200、persona_applicable=false、blocking_reason=`PERSONA_OPERATION_CONFIG_MISSING`；不自动创建默认配置。
- 数据损坏返回 409 `PERSONA_CONFIG_INVALID`，不得把损坏 JSON 直接返回客户端；响应只含 revision/hash 和修复提示。
- preview_targets 由服务端按 account_id 查询阶段 2 policy，返回目标映射有效、账号绑定一致且至少一个类别 mode=ai 的目标；允许阶段 2规定可预览的 managed/degraded/disabled 治理状态，并返回 runtime_send_eligible/blocking_reasons，不返回 telegram_chat_id。请求 preview 时必须再次校验，不能信任旧列表。

### 12.3 PUT 全量创建或更新

~~~http
PUT /api/accounts/17/ai-persona
Content-Type: application/json
~~~

~~~json
{
  "expected_revision": 3,
  "persona": {
    "schema_version": 1,
    "name": "技术型群友",
    "tone": "自然、克制、简短",
    "interests": ["网络稳定性", "节点速度"],
    "expertise": ["技术排障"],
    "reply_length": "short",
    "preferred_topics": ["使用体验", "配置建议"],
    "forbidden_topics": ["过度营销"],
    "ad_style": "soft_share",
    "catchphrases": ["我个人更关注稳定性"],
    "language_style": "zh_cn",
    "system_prompt": "避免绝对化承诺，像普通群友一样表达。"
  }
}
~~~

成功返回 200，结构同 GET；首次创建 expected_revision 必须为 0。语义为完整替换，缺字段、null 字段、未知字段均返回 422。

事务步骤：

1. 鉴权并加载账号及 AccountOperationConfig。
2. 校验 account_type=promoter、operation_mode=growth。
3. 规范化、语义校验、计算目标 hash。
4. 锁账号行，先按 8.3 判断同目标值安全重放，再校验 expected_revision。
5. 不同目标值更新字段、递增 revision、写审计。
6. 提交后直接返回已提交账号快照；v1 没有服务端 Persona 缓存，不执行 Redis 失效。

不提供 PATCH，避免旧客户端留下半套 Persona。

### 12.4 POST 恢复中性默认

~~~http
POST /api/accounts/17/ai-persona/reset
Content-Type: application/json
~~~

~~~json
{
  "expected_revision": 4
}
~~~

- 当前已配置：将 ai_persona/hash 置空，revision 从 4 增加到 5，并把 updated_at/updated_by 更新为本次管理员与时间，写 `account_persona_reset`。
- 当前已是默认：按安全重放幂等返回；即使 expected_revision 是首次 reset 前的旧值，也不递增、不写审计。
- 即使 revision 已大于 0，重置后 GET 仍返回该历史 revision，configured=false；下一次创建从此 revision+1，而不是从 1 重启。
- 已创建 AI execution 保持原 Persona 快照；新 execution 使用 neutral_default。
- 账号类型/模式已变为不适用时，管理员仍可 reset 清理旧配置。

### 12.5 POST Persona 预览

请求：

~~~json
{
  "owned_group_asset_id": 25,
  "content_category": "community",
  "trigger_type": "manual",
  "topic": "节点稳定性",
  "sample_context": [
    {
      "message_id": 10001,
      "user_name": "测试用户",
      "text": "最近晚高峰节点体验怎么样？"
    }
  ],
  "draft_persona": {
    "schema_version": 1,
    "name": "技术型群友",
    "tone": "自然、克制、简短",
    "interests": ["网络稳定性"],
    "expertise": ["技术排障"],
    "reply_length": "short",
    "preferred_topics": ["使用体验"],
    "forbidden_topics": ["过度营销"],
    "ad_style": "soft_share",
    "catchphrases": [],
    "language_style": "zh_cn",
    "system_prompt": ""
  }
}
~~~

字段语义：

- `owned_group_asset_id` 必填，目标映射必须有效且阶段 2 policy.account_id=路径 account_id；禁止跨账号借群预览。按阶段 2 合同，governance_status=degraded/disabled 可预览但响应必须警告实际发送会阻断。
- `content_category` 仅 community/promotion，且对应阶段 2 policy 已启用 AI 模式；模板策略不可预览 Persona。
- `trigger_type` 第一版固定为 manual；scheduled/keyword/reply 只由阶段 2真实 execution 触发，不由预览伪造。
- `topic` 必填，1–100 字，与阶段 2 MessageGenerationRequest 一致；必须通过阶段 2 允许话题及 Persona 禁区校验。
- `sample_context` 可选并受 9.6 限制；为空时不从 Telegram 自动拉取历史消息。
- `draft_persona` 可选；提供时只用于本次预览且不保存，不提供时使用账号已保存 Persona/中性默认。
- 使用 draft 时 `persona_revision=null`、`base_account_revision=账号当前 revision`；未使用 draft 时 `persona_revision` 为实际解析版本。两种情况都返回本次有效 Persona hash。

200 data payload：

~~~json
{
  "preview_id": "uuid",
  "account_id": 17,
  "owned_group_asset_id": 25,
  "content_category": "community",
  "persona_source": "draft",
  "persona_revision": null,
  "base_account_revision": 3,
  "persona_hash": "64-char-sha256",
  "prompt_template_version": "owned-group-persona-v1",
  "prompt_hash": "64-char-sha256",
  "governance_rules_hash": "64-char-sha256",
  "sample_text": "晚高峰建议重点看延迟波动和丢包，不要只看瞬时测速。",
  "promotion_composition": null,
  "effective_constraints": {
    "language": "zh_cn",
    "max_chars": 80,
    "allowed_topics": ["使用体验"],
    "forbidden_topic_count": 4,
    "promotion_url_locked": true
  },
  "governance": {
    "allowed": true,
    "reason_code": null
  },
  "feature": {
    "effective_enabled": false,
    "runtime_will_apply": false
  },
  "warnings": ["PERSONA_FEATURE_DISABLED_FOR_RUNTIME"],
  "usage": {
    "provider": "openai",
    "model": "configured-model",
    "input_tokens": 210,
    "output_tokens": 28
  }
}
~~~

预览硬边界：

- 不创建 execution，不排队、不进入 TelegramExecutionService、不占阶段 2 发送额度/冷却/去重/风控预留。
- 预览会产生真实 LLM Token 成本，按 account_id 限制 10 次/滚动小时、单管理员 30 次/滚动小时；超限返回 429 `PERSONA_PREVIEW_RATE_LIMITED`。
- 预览限流存储不可用时返回 503 `PERSONA_PREVIEW_RATE_LIMIT_UNAVAILABLE`，不得无保护调用 LLM。
- 总开关关闭时允许管理员做预览验证，但明确返回 runtime_will_apply=false；预览不改变实发开关。
- 预览解析用于“查看启用后的效果”，因此开关关闭时仍按 draft_persona→saved configured→neutral_default 的顺序解析；不得把已保存配置替换成 feature_disabled_default。该例外只存在于 preview，不得写入实际 execution。
- 使用与实发一致的 Prompt Builder、Provider 能力检查和生成后治理检查；不通过时不返回 sample_text。
- promotion 预览使用阶段 2 policy 的 promotion_config 确定性原样追加 CTA/URL；v1 不生成追踪参数。community 固定返回 `promotion_composition=null`；promotion 返回 `{body_text, cta_text, destination_url, final_text}`，其中 final_text 必须逐字等于 sample_text，另外三项分别是本次实际生成正文和实际拼接的 CTA/URL。该对象只暴露本次预览最终会展示/发送的输出片段，不返回 promotion_config 的其他键、策略内部字段或追踪材料。
- 不返回 raw system/user prompt、原始治理词、完整上下文或提供商请求体。
- `account_persona_preview_generated/failed` 审计只写 draft hash、规则 hash、结果和原因码。

### 12.6 GET Persona 审计事件

~~~http
GET /api/accounts/17/ai-persona/audit-events?cursor=opaque&limit=50&event_type=account_persona_updated
~~~

参数：limit 默认 50、范围 1–100；cursor 为服务端不透明游标，排序固定 `created_at DESC, id DESC`；event_type 可选且仅接受本阶段五种事件。

~~~json
{
  "items": [
    {
      "id": 900,
      "event_type": "account_persona_updated",
      "account_id": 17,
      "asset_id": null,
      "actor": {"id": 2, "username": "admin"},
      "created_at": "2026-09-10T10:00:00Z",
      "before": {"configured": true, "revision": 2, "persona_hash": "..."},
      "after": {"configured": true, "revision": 3, "persona_hash": "..."},
      "changed_fields": ["tone", "reply_length"],
      "result": "success",
      "reason_code": null
    }
  ],
  "next_cursor": null
}
~~~

以上 JSON 为 200 data payload。审计响应不得包含字段值、system_prompt、上下文、Prompt、消息正文或密钥。

### 12.7 阶段 2 API 的增量字段

阶段 2 策略详情/列表增加只读摘要：

~~~json
{
  "persona": {
    "account_id": 17,
    "configured": true,
    "name": "技术型群友",
    "revision": 3,
    "applicable": true,
    "effective_enabled": true
  }
}
~~~

阶段 2 execution 详情增加：

~~~json
{
  "persona": {
    "source": "configured",
    "name": "技术型群友",
    "revision": 3,
    "hash_prefix": "a1b2c3d4e5f6"
  },
  "prompt_template_version": "owned-group-persona-v1",
  "prompt_hash_prefix": "0123456789ab",
  "governance_rules_hash_prefix": "fedcba987654"
}
~~~

仅返回快照摘要，不能用“账号当前 Persona”替换历史 execution 的展示值。

### 12.8 错误码

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 400 | PERSONA_TOPIC_NOT_ALLOWED | 主题不在阶段 2 允许范围 |
| 400 | PERSONA_TOPIC_FORBIDDEN | 与附加禁区冲突 |
| 401 | AUTHENTICATION_REQUIRED | 未登录/Token 失效；沿用阶段 2 路由错误码 |
| 403 | ADMIN_REQUIRED | 非 admin 访问完整 GET/PUT/reset/preview/运行时开关写接口 |
| 403 | PERSONA_AUDIT_READER_REQUIRED | 非 admin/operator/auditor 读取 Persona 审计 |
| 404 | ACCOUNT_NOT_FOUND | account_id 不存在 |
| 404 | OWNED_GROUP_ASSET_NOT_FOUND | 预览 asset 不存在 |
| 409 | PERSONA_REVISION_CONFLICT | 乐观锁冲突 |
| 409 | PERSONA_ACCOUNT_TYPE_UNSUPPORTED | guardian_bot 等不适用类型 |
| 409 | PERSONA_ACCOUNT_MODE_UNSUPPORTED | ad_only 等不适用模式 |
| 409 | PERSONA_OPERATION_CONFIG_MISSING | 账号缺少运行职责配置 |
| 409 | PERSONA_ACCOUNT_MISMATCH | policy/execution/路径账号不一致 |
| 409 | PERSONA_CONFIG_INVALID | 库内配置损坏或 Schema 不支持 |
| 409 | PERSONA_POLICY_MODE_UNSUPPORTED | 阶段 2 策略不是 AI 模式 |
| 409 | PERSONA_FEATURE_REVISION_CONFLICT | Persona 运行时开关版本冲突 |
| 422 | PERSONA_VALIDATION_FAILED | Schema、长度或语义校验失败 |
| 422 | PERSONA_PROMPT_INJECTION_REJECTED | 自由文本包含明确越权指令 |
| 422 | AI_PROMPT_TOO_LARGE | 不可裁剪部分仍超 Token 预算 |
| 429 | PERSONA_PREVIEW_RATE_LIMITED | 预览频率超限 |
| 503 | PERSONA_PREVIEW_RATE_LIMIT_UNAVAILABLE | 无法可靠执行预览限流 |
| 503 | AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED | Provider 不支持 system 指令 |
| 503 | AI_PROVIDER_UNSAFE | fallback 无安全能力 |
| 503 | GOVERNANCE_CONTEXT_UNAVAILABLE | 群治理规则不可可靠加载 |
| 503 | PERSONA_FEATURE_SETTING_NOT_INITIALIZED | app.runtime_settings 种子行缺失；禁止首次请求无锁创建 |

运行 execution 的 `CONTENT_POLICY_BLOCKED`、`CONTENT_POLICY_CHANGED`、`PROMOTION_COMPOSITION_INVALID` 写入 execution.error_code，API 查询沿用阶段 2 错误合同，不伪装为 HTTP 请求失败。

## 13. 前端需求

### 13.1 账号列表入口

`Accounts.vue` 增加“AI 性格”列和操作按钮：

| 状态 | 标签 | 操作 |
| --- | --- | --- |
| promoter + growth + 已配置 | 已配置 · v{revision} | 查看/编辑/预览 |
| promoter + growth + 未配置 | 中性默认 | 配置/预览 |
| promoter + ad_only | 当前不适用 | 查看已有配置；允许管理员重置 |
| 数据损坏 | 配置异常 | 只显示错误和“恢复默认” |
| 功能开关关闭 | 已暂停应用 | 仍允许配置；预览提示不会实发 |

- Accounts.vue 沿用阶段 1 的推广账号专页边界，只请求和展示 `account_type=promoter`；不得为展示“不支持”而把 guardian_bot 混回该表。guardian_bot 不适用性只由后端 GET/PUT/preview 负向合同测试证明，GuardianBots.vue 不新增 Persona 入口。
- 普通账号列表响应只使用 Persona 摘要，不为每行请求完整 Persona，避免 N+1。
- 完整 Persona 只在管理员打开抽屉时按 account_id 加载。
- 抽屉的群/类别选项只使用完整 GET 返回的 preview_targets；degraded/disabled 目标可选但显示“仅预览、不可发送”，无目标时解释原因并禁用预览，不在前端拼接跨资产查询。
- 切换账号必须清空上一个账号的草稿、预览、校验错误和缓存；禁止串号展示。

### 13.2 Persona 抽屉

新增 `AccountPersonaDrawer.vue`，不要复用只能处理简单账号字段的通用 FormDrawer。宽度建议 720px，分为：

1. 账号与适用状态：账号显示名、account_id、类型、operation_mode、配置版本、开关状态。
2. 基础性格：name、tone、language_style、reply_length。
3. 兴趣与专长：interests、expertise、preferred_topics、forbidden_topics，使用可计数 Tag 输入。
4. 推广表达：ad_style、catchphrases；明确“只影响 AI 推广正文，不改 CTA/链接”。
5. 高级风格说明：system_prompt 文本域，显示“低优先级风格备注，不会覆盖安全规则”，计数 0/1000。
6. 预览区：选择已绑定且 AI 策略可用的自建群、类别、主题、可选上下文后生成示例。
7. 底部操作：取消、恢复中性默认、预览、保存。

前端必须使用与后端相同的枚举和显式长度提示，但前端校验不能替代服务端校验。

### 13.3 保存、冲突与重置交互

- 打开抽屉记录 `loadedRevision`；保存/重置提交 expected_revision=loadedRevision。
- 未改动时保存按钮禁用；客户端规范化后的表单与已加载值相同，不发 PUT。
- 保存成功用返回体覆盖 store，不自行 revision+1。
- 409 revision conflict 时保留本地草稿，弹窗显示“重新加载”和“复制草稿”；不得自动覆盖服务端。
- 恢复默认必须二次确认，提示“只影响之后创建的 AI 任务，已创建任务不变”。
- 后端 422 按 field_errors 定位字段；不得把用户自由文本写入前端遥测或控制台。

### 13.4 预览交互

- 默认不携带 sample_context；管理员明确添加后才发送。
- 生成期间按钮 loading 并防重复点击；前端生成 `X-Request-ID`，服务端仍负责限流。
- 预览结果同时展示样例、Persona 来源/版本、有效长度、治理结果、是否会用于运行时及 Token 用量；promotion 必须直接使用响应的 promotion_composition 分区展示正文、CTA、只读 URL，禁止前端按换行或正则拆 sample_text。
- 不展示 raw Prompt；“复制”只复制 sample_text。
- promotion 预览将正文与确定性 CTA/URL 分区展示，URL 区只读。
- 预览失败保留表单，不保留上次样例为当前成功结果。

### 13.5 阶段 2 页面联动

阶段 2 策略页按绑定 account_id 展示 Persona 摘要：

- AI 模式：显示“账号 Persona：技术型群友 v3”或“中性默认”。
- template 模式：显示“模板模式不使用 Persona”，不展示启用开关。
- operation_mode 不再为 growth：显示阻断原因，禁止新建 AI execution，沿用阶段 2 保存校验。
- 管理员可跳转到账号 Persona 抽屉；operator/auditor 只能查看摘要，不可打开完整配置。

### 13.6 Store 与类型

新增 `frontend/src/api/accountPersonas.ts` 和 `stores/accountPersona.ts`：

- API TypeScript 类型逐字段对应 12 节，不使用 `any`。
- 完整配置缓存键为 `accountId:revision`；账号摘要变化或 PUT/reset 成功时清理旧 key。
- `draftPersona` 仅保存在组件内存，关闭抽屉清除，不写 localStorage/sessionStorage/URL。
- API 错误按 code 分支，不依赖可变 message 文案。

### 13.7 Settings 运行时开关调用链

运行时开关固定复用现有 `frontend/src/api/settings.ts` → `frontend/src/stores/settings.ts` → `Settings.vue` 链路，不放入 accountPersonas Store，避免同一个设置出现两套状态源：

- 将当前同时用于 GET/PUT 的 `SettingsFormData` 拆为 `SettingsReadData` 与 `SettingsUpdatePayload`。前者在现有字段外增加只读 `ownedGroupAiPersona: {enabled, revision, updatedAt, updatedBy, staticEnabled, effectiveEnabled}`；后者只含阶段 2 已有可写节点，类型上绝不包含 ownedGroupAiPersona。
- `settingsApi.get()` 返回 `SettingsReadData`；现有 `settingsApi.update(payload: SettingsUpdatePayload)` 只调用通用 PUT；新增 `settingsApi.updateOwnedGroupAiPersonaFeature({expectedRevision, enabled})`，只调用 `PUT /settings/owned-group-ai-persona`。该接口归属现有 Settings 合同，HTTP 请求/响应直接使用 camelCase；当前 apiClient 不做键名转换，组件不得假设存在全局 snake/camel 转换层。
- Settings Store 新增 `updateOwnedGroupAiPersonaFeature(enabled)`：从当前只读节点取 expectedRevision，调用专用 API，成功后用服务端 data 覆盖节点，不自行 revision+1。普通 `updateSettings()` 必须先构造 `SettingsUpdatePayload`，不能把整个 SettingsReadData 展开提交。
- 409 PERSONA_FEATURE_REVISION_CONFLICT 时立即重新 GET 最新设置、保留用户本次意图并提示“设置已被其他管理员修改，请确认后重试”，不得自动重放写请求。403/503 按第 12.8 节 code 展示固定文案。
- Settings.vue 在群 AI 区显示“静态开关、运行时开关、最终生效”三项。静态 false 时仍允许 admin 预先保存运行时 true，但必须显示“需启用环境开关并重启后端才会生效”，不得显示为已生效；非 admin 只读且不渲染保存控件。

## 14. 功能开关与停止语义

### 14.1 双开关

| 层级 | 配置 | 默认 | 生效方式 |
| --- | --- | --- | --- |
| 静态环境 | OWNED_GROUP_AI_PERSONA_ENABLED | false | 服务启动读取；紧急回滚 |
| 运行时设置 | ownedGroupAiPersona.enabled | false | automation settings 动态读取 |

有效开关：

~~~text
effective_enabled = static_enabled AND runtime_enabled
~~~

- 任一关闭时，新建 AI execution 冻结 `feature_disabled_default` 中性 Persona。
- 已创建 execution 不重写快照，避免审核后的内容无故改变。
- feature_disabled_default 必须走第 9.1/10.2 节阶段 2 兼容旁路：逐字复用阶段 2 的 system/user Prompt、`generate()->str` Provider 调用和既有内容门禁，不启用阶段 3 严格 system 角色能力门槛，不执行阶段 3 新增出站检查。关闭 Persona 不关闭或改变阶段 2 模板/中性 AI 消息。
- 双开关开启但账号未配置时 source=neutral_default：逐字复用阶段 2 的 system/user Prompt 且不注入 Persona 风格，但执行阶段 3 新增出站治理并使用 `generate_response()`/v2 缓存；其 requires_system_role=false，不因 LOCAL 当前缺少 system 支持而停用，非空 system_prompt 仍按该 Provider 阶段 2 既有语义处理。
- 开关只控制 Persona 应用，不控制是否发送；停止实际发送必须使用阶段 2 `OWNED_GROUP_MESSAGING_ENABLED=false` 或 `ownedGroupMessaging.enabled=false`。
- 运行时设置修改沿用 automation settings 的权限、版本和审计；不得把 Persona 原文写入运行时设置。

### 14.2 与既有停止链的关系

| 停止/开关 | 阶段 3 行为 |
| --- | --- |
| OWNED_GROUP_MESSAGING_ENABLED=false 或 ownedGroupMessaging.enabled=false | 阶段 2 不创建/发送，Persona 不单独绕过 |
| 群 provisioning_stop | 不直接作为 Persona 配置禁用；任务能否创建沿用阶段 2 |
| governance_stop | 只停止 Guardian 动作；阶段 2/3 仍按资产当前 governance_status=managed 和其他消息门禁判断 |
| acquisition_stop | 完全不读取、不影响自建群 Persona 消息 |
| auto_ads_enabled | 完全不读取、不影响自建群 Persona 消息 |
| Persona feature disabled | 仅将新 AI execution 降为中性，不影响模板和发送链 |

阶段 3 不新增 Worker stop reason，不创建第二套“暂停消息”状态。

### 14.3 运行时开关写入合同

现有通用 `PUT /api/settings` 只有登录鉴权、没有 revision/审计，不能用于修改 Persona 开关。阶段 3 在同一路由增加管理员专用接口：

~~~http
PUT /api/settings/owned-group-ai-persona
~~~

~~~json
{
  "expectedRevision": 0,
  "enabled": true
}
~~~

成功 data payload：

~~~json
{
  "enabled": true,
  "revision": 1,
  "updatedAt": "2026-09-10T10:00:00Z",
  "updatedBy": 2,
  "staticEnabled": false,
  "effectiveEnabled": false
}
~~~

实现要求：

- 显式 `Depends(require_admin)`；operator/auditor/普通用户返回 403。
- 在 `app.runtime_settings` 的 `ownedGroupAiPersona` 节点持久化 enabled/revision/updatedAt/updatedBy，默认 revision=0、enabled=false。
- 049/035 迁移负责种子行。专用接口对 `SystemSetting(key=app.runtime_settings)` 执行 `SELECT ... FOR UPDATE`；若行仍缺失返回 503 `PERSONA_FEATURE_SETTING_NOT_INITIALIZED`，不得在首次请求中用“先查后插”制造并发主键竞态。
- 新增无内部 commit 的原子节点更新 helper：锁行后解析完整 value，只替换 ownedGroupAiPersona，保留其他节点，同事务写审计，由路由统一 commit；不得调用当前会自行 commit 的通用 `save_app_runtime_settings()` 拼接审计。
- 同 enabled 安全重放不递增 revision；有效变更递增并在同一事务写 `account_persona_feature_updated`，resource_type=persona_setting、resource_id=NULL，只记录 enabled/revision 前后值。审计写失败必须回滚开关变更。
- `SettingsUpdate` 不新增可写 `ownedGroupAiPersona` 字段；通用 PUT 即使收到该未知字段也不能修改开关。GET /api/settings 返回只读节点供界面展示，并由服务端补充不持久化的 staticEnabled/effectiveEnabled；专用 PUT 成功响应同样返回这两个计算值。所有其他 `app.runtime_settings` 写入口也必须使用同一行锁/原子 mutation helper并保留 ownedGroupAiPersona，避免并发通用设置保存把 revision 回滚成旧值。
- `normalize_app_runtime_settings`、`get_owned_group_ai_persona_settings` 必须保留 revision 元数据，不能把未知节点静默丢弃。
- 静态环境开关仍是更高优先级；运行时 API 不能把静态 false 变为有效 true。
- 功能开关审计不混入账号级 `/api/accounts/{account_id}/ai-persona/audit-events`；admin/operator/auditor 通过现有 `GET /api/owned-groups/audit-events?resource_type=persona_setting&event_type=account_persona_feature_updated` 查询，沿用现有 offset/limit 分页与脱敏输出，本阶段不改造该通用接口为 cursor。

这项设置变更属于阶段 3 的第六个写接口，不计入账号 Persona 基础路径下的五个接口。

## 15. 可观测性、审计与数据保留

### 15.1 结构化日志

统一事件：

~~~text
account_persona.resolve
account_persona.snapshot
account_persona.preview
owned_group_prompt.build
owned_group_persona.llm_usage
owned_group_outbound_governance.evaluate
~~~

允许字段：request_id、execution_id、account_id、asset_id、core_group_id、content_category、persona_source、revision、hash_prefix、prompt_template_version、provider、model、input_tokens、output_tokens、cost_microunits、cache_hit、result、reason_code、duration_ms。费用只用整数 microunits，禁止浮点 label。

禁止字段：原始 Persona 字段值、system/user prompt、上下文正文、最终消息全文、敏感词原文、手机号、session、token、代理和 LLM 密钥。

### 15.2 指标

~~~text
account_persona_configured
account_persona_update_total{result,reason_code}
account_persona_resolve_total{source,result}
account_persona_preview_total{result,reason_code,content_category}
account_persona_preview_duration_seconds
owned_group_persona_prompt_build_total{result,content_category}
owned_group_persona_llm_tokens_total{direction,provider,model,persona_source}
owned_group_persona_outbound_block_total{stage,reason_code,content_category}
owned_group_persona_account_mismatch_total
owned_group_persona_growth_ad_config_access_total
~~~

约束：

- 当前仓库没有统一 Prometheus 注册层，上述名称是必须产生的逻辑指标合同：第一版可由同名结构化计数事件接入现有日志/统计采集；若本阶段引入 exporter，名称和 label 必须保持一致，不另造第二套。`account_persona_configured` 是 Gauge，其余 `_total` 是 Counter，duration 是 Histogram。
- account_id、asset_id、revision、Persona name/hash 不作为 Prometheus label，避免高基数。
- 按账号核算 Token/费用时从 `owned_group_persona.llm_usage` 的结构化事件按 account_id 聚合；系统指标只保留 provider/model/persona_source 等受控低基数字段。Provider 未返回精确 usage 时写 `usage_source=estimated`，不得把估算值伪装成账单值。
- `owned_group_persona_growth_ad_config_access_total` 是已知广告配置访问器上的辅助 tripwire，正常值必须恒为 0；它不能证明不存在直接 SQL/新依赖访问，隔离验收必须同时使用 AST import 守卫、依赖 spy 和 SQL table spy。
- 配置审计成功率、预览失败率、Provider 不安全错误、账号不一致为告警候选。

### 15.3 数据保留

- Persona 当前配置随账号保留；账号删除沿 FK/现有账号删除策略处理。
- terminal execution.persona_snapshot 在 90 天后清空；阶段 2 content/prompt_context 按阶段 2 自身保留任务处理，阶段 3 不重复删除。
- execution Persona/Prompt/治理 hash 元数据随 execution 生命周期保留且至少 365 天；阶段 3 不单独删除 execution 行。
- Persona 配置及功能开关安全审计摘要保留 365 天；当前 OwnedGroupAuditEvent 没有通用清理机制，由 P3-RET-01 新增定向清理。
- LLM Provider 原始请求/响应不得因本阶段新增持久化；调试仅使用脱敏 hash。

P3-RET-01 每日执行一次，使用主键游标每批最多 500 行并提交后继续：只对 terminal 且 `updated_at < now-90d` 的 execution 清空 persona_snapshot，不清理任何非终态任务；只删除 `resource_type IN (account_persona, persona_setting)`、event_type 属于本阶段明确定义集合且 `created_at < now-365d` 的审计，绝不删除其他 OwnedGroupAuditEvent。任务需有 scanned/updated/deleted/failed 指标、分布式单实例锁和 dry-run 命令。

## 16. 事务、幂等与故障处理

### 16.1 事务边界

| 操作 | 单事务内容 | 事务外内容 |
| --- | --- | --- |
| PUT/RESET | 锁账号、校验 revision、更新 Persona、写审计 | 无；v1 无服务端 Persona 缓存 |
| 创建 AI execution | 锁/读策略与账号、解析 Persona、冻结快照、创建 execution | LLM 调用、发送 |
| preview | 事务 A 读取账号/策略/规则快照；事务 B 写结果安全审计 | 两事务之间调用 LLM；失败审计也用独立短事务 |
| 实际生成 | 事务 A 领取租约并置 generating；事务 B 写内容/usage/指纹/治理结果 | 两事务之间调用 Provider；正常异常按 10.3 终态化，进程崩溃由 stale-generating 收敛任务置 failed |

不得持有数据库事务跨越 LLM 或 Telegram 网络调用。

### 16.2 幂等语义

- PUT/RESET 不新增幂等表：通过目标值一致优先于 revision 冲突的规则支持网络安全重放；不同目标值仍严格执行 expected_revision。
- execution 创建和生成继续使用阶段 2 幂等键，不因 Persona 新建第二条 execution。
- 阶段 3 不新增生成重试；LLM cache 仅是同一安全输入的性能优化，不能把 failed/skipped execution 重新排队。发送重试只使用已持久化 content/Persona 元数据，不再次调用 LLM。
- preview 不承诺内容幂等；每次生成新的 preview_id 和隔离 cache_scope。

### 16.3 故障矩阵

| 故障 | 行为 | 恢复 |
| --- | --- | --- |
| LLM 响应缓存不可用 | 按 cache miss 调 Provider；写缓存失败不丢成功结果 | 自动恢复 |
| preview 限流存储不可用 | 返回 PERSONA_PREVIEW_RATE_LIMIT_UNAVAILABLE，不调用 LLM | 存储恢复后重试 |
| Persona 数据库不可用 | 不创建新 AI execution/不预览 | 基础设施恢复后重试 |
| Persona JSON 损坏 | 实发失败关闭，管理员可 reset | 修复或恢复默认 |
| 治理规则读取失败 | execution 终态 failed，不生成/不发送 | 恢复后新建 execution |
| requires_system_role=true 但 Provider 不支持 system | 不调用/不 fallback 到不安全 Provider | 改配置或适配器 |
| LLM 超时/限流 | execution 终态 failed，不设置 next_retry_at | 恢复后新建 execution |
| LLM 返回空/错误文本 | execution 终态 failed | 新建 execution，不发送错误文案 |
| Worker 在 generating 期间崩溃 | 租约过期后原子置 failed、AI_GENERATION_LEASE_EXPIRED、清理 lease | 不重新调用 LLM；新建 execution |
| 出站治理阻断 | skipped/failed，记录 execution.error_code | 人工调整规则或新建任务 |
| Persona 更新与任务创建并发 | 行锁决定确定的前/后 revision | 无需补偿 |
| 审计写入失败 | PUT/RESET 回滚；preview 主结果可返回失败并告警 | 修复审计存储后重试 |

stale-generating 收敛实现固定为：Worker 每个 tick 在正常领取之外，按 id 升序分批扫描 `status=generating AND (lease_expires_at IS NULL OR lease_expires_at < now_utc)`；`fail_stale_generating(execution_id)` 以 `SELECT ... FOR UPDATE SKIP LOCKED` 二次确认状态/到期条件，写 `status=failed`、`error_code=AI_GENERATION_LEASE_EXPIRED`、固定脱敏 error_message，清空 lease_id/lease_expires_at/next_retry_at，revision+1，并写 `message_execution_failed` 审计后同事务提交。不得增加 attempt_count、不得转 queued、不得调用 LLM。原 Worker 若迟到返回，因 status/lease 不匹配必须丢弃结果且不写 content。

### 16.4 安全失败原则

- 无法证明账号一致、治理有效或 Provider 保留系统指令时，不生成、不发送。
- Persona 不可用但明确是“未配置/功能关闭”时，才允许使用规定的中性默认。
- 不允许通过尝试不同 Persona、temperature、Provider 或重复生成来规避内容治理结果。
- 所有异常路径不得让模板模式意外转成 AI 模式。
- AI execution 的 Persona/Prompt/Provider/治理失败只能保持 `mode_snapshot=ai` 并按错误码 failed/skipped；绝不查询模板、切换为 template、填充 default_template_id 或发送模板兜底内容。

## 17. 验收用例（P0/P1 共 90 条）

P0 是自动化代码合并门槛，任一失败均不得灰度；P1 是上线前验收门槛，可包含明确标注的预生产人工验证，但必须留存结果。

### 17.1 Schema、CRUD 与权限

1. **P0** admin 可为 promoter + growth 账号创建完整 Persona，revision 从 0 变 1，hash 为规范化 JSON 的 SHA-256。
2. **P0** admin 可读取完整 Persona，响应字段与 12.2 一致且不包含数据库内部密钥。
3. **P0** 通用 AccountCreate/AccountUpdate 携带 ai_persona 时固定返回 422，不能静默忽略或旁路专用接口。
4. **P0** operator、auditor 和普通用户调用完整 GET 均返回 403。
5. **P0** 非 admin 调用 PUT、reset、preview 均返回 403，且无数据库、缓存和审计变更。
6. **P1** schema_version 非 1 返回 422 和 schema_version 字段错误。
7. **P1** 缺少任一必填字段返回 422；服务端不自动补成用户配置。
8. **P1** 多余字段返回 422，证明 extra=forbid。
9. **P1** 各字符串和数组项边界值通过；超过任一上限返回对应字段错误。
10. **P0** 规范化 JSON 超过 16384 UTF-8 bytes 返回 422，不写原文日志。
11. **P0** 控制字符、零宽字符和双向覆盖字符被移除；移除后为空的必填字段返回 422。
12. **P1** 数组 NFKC + 大小写无关去重后保持首次顺序。
13. **P0** preferred_topics 与 forbidden_topics 规范化后冲突返回 422。
14. **P0** 明确要求忽略系统/安全/治理指令的 system_prompt 返回 PERSONA_PROMPT_INJECTION_REJECTED。
15. **P1** 合法风格备注可保存，但在 Prompt 中位于安全、治理、业务和全局 AI 设置之后。

### 17.2 适用性、并发与重置

16. **P0** guardian_bot PUT 返回 PERSONA_ACCOUNT_TYPE_UNSUPPORTED，原账号记录不变。
17. **P0** promoter + ad_only PUT 返回 PERSONA_ACCOUNT_MODE_UNSUPPORTED；缺少 AccountOperationConfig 返回 PERSONA_OPERATION_CONFIG_MISSING，均不得猜成 growth。
18. **P1** growth 账号已配置后切到 ad_only，配置保留但 applicable=false；切回 growth 后新任务再次使用。
19. **P1** offline/inactive/risk paused 的 growth 账号允许提前保存 Persona，但实际发送仍被阶段 2 门禁阻断。
20. **P0** 两个管理员以相同 expected_revision 提交不同内容，只有一个成功，另一个返回 409；Persona PUT/执行创建与 growth↔ad_only 并发时按账号→配置锁顺序得到确定结果。
21. **P1** 提交规范化后相同内容返回 200，不递增 revision、不更新时间、不新增审计。
22. **P1** PUT/RESET 成功响应丢失后以原 expected_revision 重放相同目标值，返回当前成功状态且不产生第二次变更；不同目标值返回冲突。
23. **P0** reset 将配置/hash 清空并递增 revision；新任务使用 neutral_default。
24. **P1** 已是默认时重复 reset 幂等成功，不递增 revision。
25. **P0** PUT/RESET 审计失败时配置事务回滚，不能出现无审计变更。

### 17.3 解析、快照与 Prompt

26. **P0** AI execution 只按 execution.account_id 解析 Persona，不读取群 owner 或第一个在线账号。
27. **P0** policy.account_id、execution.account_id、Persona owner 任一不一致时返回 PERSONA_ACCOUNT_MISMATCH，LLM 未调用。
28. **P0** 同一账号在两个自建群的新 AI execution 使用同一 Persona revision/hash。
29. **P1** 预生产用两个账号以明显不同 tone/reply_length、同群同主题同上下文且 temperature=0 各预览 3 次；Prompt 账号不串用，实际 sample_text 不全相同且至少 5/6 条通过对应 Persona 风格规则人工盲审并留档。该项不依赖 CI 的随机模型输出判定。
30. **P0** Persona 在 execution 创建事务内冻结五个 Persona/模板字段和 `business_snapshot_v1`；有输出成功并持久化 content 时同事务写 prompt_hash，configured/neutral_default 同时写 governance_rules_hash，兼容旁路保持治理 hash 为 NULL，任一阶段均满足字段不变量。
31. **P0** 创建后更新 Persona、policy.allowed_topics、群标题或部署新 Prompt 模板，queued execution 仍使用旧 Persona、旧 `business_snapshot_v1` 和旧 prompt_template_version；旧构建器不存在时失败关闭而非换新版本。
32. **P0** generating 只使用冻结 Persona；同租约内容去重尝试各用独立 generation_attempt cache scope。生成失败直接终态且不设置 next_retry_at；stale generating 被原子置 failed/AI_GENERATION_LEASE_EXPIRED 且不再调 LLM。ready_to_send + next_retry_at 的发送重试不读取当前 Persona、不调用 LLM，只发送已审核内容。
33. **P1** 阶段 3 上线前非终态 AI execution 被迁移为 legacy_default 中性快照。
34. **P1** 终态旧 execution 显示 legacy_untracked，不伪造成当前 Persona。
35. **P0** ai_persona IS NULL 时新 AI execution 使用 neutral_default，中性对象 canonical hash 稳定，且 Prompt Builder 完全跳过 Persona 风格区块。
36. **P0** Persona 总开关任一层关闭时，新 AI execution 使用 feature_disabled_default；仅 admin 可用专用 settings 接口改运行时开关，revision 冲突和通用 settings PUT 旁路均被阻断。
37. **P0** 开关关闭不改写已创建 execution 快照。
38. **P0** ai_persona={}、未知 Schema 或库内非法数据导致 PERSONA_CONFIG_INVALID，不静默降级。
39. **P0** 指令优先级测试证明 Persona 不能覆盖安全、群治理、阶段 2业务或全局 AI 限制。
40. **P1** Persona preferred_topics 只对 execution 冻结的 `business_snapshot_v1.allowed_topics` 内候选排序，不能扩大集合。
41. **P0** Persona forbidden_topics 只增加禁区，不能解除系统、治理或阶段 2禁区。
42. **P1** reply_length 分别映射 80/180/320 并与全局上限和 500 取最小值；单条输出最多命中一个 catchphrase 一次，超出时确定性阻断。

### 17.4 模板与增长中心隔离

43. **P0** community + template 创建、预览、生成和发送链均不查询 Persona。
44. **P0** promotion + template 的最终文本与阶段 2 基线逐字一致，Persona 修改不影响结果。
45. **P0** template execution 的七个 Persona/Prompt 字段均为 NULL。
46. **P0** Persona 关闭不关闭阶段 2 模板消息。
47. **P0** Persona 双开关关闭时走阶段 2 完整兼容旁路，快照测试证明 system/user Prompt 逐字一致、旧 `generate()->str` 调用不变且不执行阶段 3 新检查；双开关开启但账号未配置时复用同一 system/user Prompt、无 Persona 风格且执行阶段 3 出站治理。两者均不整体禁用 AI，也不因 LOCAL 缺少 system 能力而失败。
48. **P0** 自建群 community/promotion 不读取 auto_ads_enabled；true/false 下行为一致。
49. **P0** 自建群 community/promotion 不读取 acquisition_stop；停止/恢复增长获客不改变结果。
50. **P0** 自建群 Persona 链不查询 AdCampaign、AccountAdBinding、AdCreative、AdDeliveryLog。
51. **P0** 自建群 Persona 链不查询 GroupAdProfile、GroupAdPolicyEvent、GroupAdOnlyAssessment/Handover/Event。
52. **P0** 增长中心广告开关、额度、冷却和失败策略任意组合，不改变阶段 3 Prompt 或阶段 2 自建群发送准入。
53. **P0** AST import 守卫、增长广告依赖 fail-fast spy 和数据库 SQL table spy 均证明 Persona/阶段 2自建群链未访问禁用广告模块/表；辅助 access_total 同时保持 0。
54. **P1** Persona 不接入外部群 AI 暖场、旧语义回复、私聊客服和 Guardian Bot 文案。
55. **P0** Persona 更新不触发账号重连、AccountPool 重载、换号、加群或 Telegram 资料修改。

### 17.5 推广内容与治理

56. **P0** community AI 使用 Persona 风格但不会自动附加 promotion URL/CTA。
57. **P0** promotion AI 仅由 LLM 生成正文，CTA/URL 由阶段 2 promotion_config 确定性追加；预览返回 body/CTA/URL/final 的结构化 composition，final_text=sample_text，前端不自行拆分。
58. **P0** Persona system_prompt 要求修改/删除/增加链接时被忽略或拒绝，最终 URL 仍唯一匹配配置。
59. **P1** 最终长度计算包含 CTA/URL；空间不足时缩短正文而不截断链接。
60. **P0** 生成正文后执行一次出站治理检查；失败时不拼 CTA。
61. **P0** promotion 拼接 CTA/URL 后再次检查完整文本。
62. **P0** Telegram 发送前读取最新规则并第三次检查；生成后新增禁词可阻断发送，生成 hash 保持不变且发送检查 hash 写阶段 2 审计。
63. **P0** 出站检查不执行管理员、Bot 或发送账号白名单绕过。
64. **P0** 出站检查不写用户违规、不删除、不禁言、不踢人且不触发 Guardian 动作。
65. **P0** 治理投影使用 core_group_id：Rule/Keyword 只取 enabled 的全局+本群项，GroupModerationPolicy 只取唯一群级行且不访问不存在的 enabled；缺失/歧义失败关闭。telegram_chat_id 仅用于 Telegram 发送，link/media 原文不进入 Prompt。
66. **P0** 治理数据库不可用时返回 GOVERNANCE_CONTEXT_UNAVAILABLE，不生成、不发送。
67. **P1** Prompt 只投影前 200 条禁词，但发送前检查仍覆盖第 201 条及之后的完整规则。
68. **P0** 内容治理阻断后不得通过自动更换 Persona/Provider/temperature 或无限重生成绕过。

### 17.6 Provider、缓存、Token 与上下文

69. **P0** 新 `generate_response()` 的 OpenAI 和 Anthropic 适配测试证明 system_prompt 与 user_prompt 使用独立角色；原 `generate()->str` 的签名、字符串返回值和至少一个既有调用回归保持不变。
70. **P0** configured/draft 的 requires_system_role=true 且 LOCAL 不支持 system 指令时，在调用前返回 AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED；中性兼容路径仍保持阶段 2 行为。
71. **P0** requires_system_role=true 时 fallback Provider 不支持 system 指令则返回 AI_PROVIDER_UNSAFE，不发送兜底错误字符串。
72. **P0** LLM 空响应、异常文案、Provider/Persona/Prompt/治理错误不进入 Telegram 消息，并保持 mode_snapshot=ai；模板查询 spy 为 0，不切换 template、不读取 default_template_id、不发送模板兜底。
73. **P0** v2 缓存键使用 canonical JSON + SHA-256，逐字段覆盖第 11.3 节完整材料及实际 provider_endpoint_sha256；缺字段、未知字段、类型非法或调用参数冲突均拒绝，不把业务字段只塞入 cache_scope。同 provider/model 切换 base_url 或 fallback endpoint 必须 cache miss。
74. **P0** 同 Prompt、不同 account_id/persona_revision/execution_id 不得命中同一缓存。
75. **P0** preview 缓存 scope 与实际 execution 完全隔离。
76. **P1** Persona 更新无需扫描缓存；新 hash/revision 自动 miss，旧键按 TTL 过期。
77. **P0** Token 预算覆盖 system + user + 响应预留，超预算按规定顺序裁剪并重算 hash。
78. **P0** 安全边界/治理/业务约束裁剪后仍超限时返回 AI_PROMPT_TOO_LARGE，Provider 未调用。
79. **P0** 不可信上下文中的 Prompt 注入文本只作为 JSON 数据，不能改变生成指令。
80. **P1** 上下文单条、总量、数量和字符清洗边界均按 9.6 执行。

### 17.7 预览、前端、审计与迁移

81. **P0** preview 不创建 execution、不调 Telegram、不占发送额度/冷却/风控预留。
82. **P0** preview 校验目标映射有效、策略 account_id 匹配且对应类别 mode=ai；degraded/disabled 可预览并返回发送阻断警告，跨账号或模板策略被拒绝。
83. **P1** 未传 draft_persona 时使用已保存/中性 Persona；传入草稿时不保存账号配置。
84. **P1** Persona 功能关闭仍可预览，但响应和 UI 明确 runtime_will_apply=false。
85. **P0** 预览按账号和管理员双维度限流，超限不调用 LLM。
86. **P0** Persona 审计列表仅返回 hash/版本/changed_fields/原因码，不返回字段原值、Prompt 或上下文。
87. **P1** 账号列表单请求返回 Persona 摘要，无逐行完整 Persona N+1 请求。
88. **P0** 前端切换账号清除上一账号草稿和预览；409 冲突不会覆盖服务端或丢失本地草稿。
89. **P0** 升级/降级/历史回填及 app.runtime_settings 种子幂等分别通过 PostgreSQL 和测试数据库验证，迁移 head 唯一；两个并发开关写与一个并发通用设置写不丢 revision/其他节点；保留清理只清空 90 天前终态 persona_snapshot、只删除 365 天前 account_persona/persona_setting 本阶段审计，非终态和其他审计零删除。
90. **P0** 阶段 1/2 回归、后端 Persona 套件、前端构建/类型检查全部通过，且测试日志不存在 Persona/Prompt/凭据原文。

## 18. 开发任务拆分

### 18.1 可立即并行开发并合并（不依赖阶段 2 最终模块）

| 任务 | 工作内容 | 产物 | 估时 | 依赖 |
| --- | --- | --- | --- | --- |
| P3-BE-01 | PersonaV1 Schema、NFKC/安全清洗、语义校验、canonical hash、中性默认 | 纯领域模块与单测 | 0.75 人日 | 无 |
| P3-AI-01 | OwnedGroupPromptBuilder、优先级、上下文封装、hash | 纯构建器与快照测试 | 1 人日 | P3-BE-01 |
| P3-AI-02 | 保留 generate()->str，新增 generate_response()/LLMCacheContext、Provider 能力、v2 SHA-256 缓存与 Token 预算 | LLMClient/适配器及旧调用回归测试 | 1–1.5 人日 | 无 |
| P3-GOV-01 | evaluate_outbound_content 无白名单/无副作用检查器 | 治理服务与测试 | 0.75 人日 | 阶段 1 治理模型 |
| P3-FE-00 | Persona 表单纯组件和 TypeScript 合同 | 不调用后端的组件/类型测试 | 0.5 人日 | 12 节合同 |

### 18.2 阶段 2 契约冻结后开发

| 任务 | 工作内容 | 产物 | 估时 | 依赖 |
| --- | --- | --- | --- | --- |
| P3-DB-01 | TelegramAccount 五字段、约束、app.runtime_settings 幂等种子、SQL/Alembic 升降级 | 049/035 迁移、模型 | 0.75 人日 | 阶段 2 048/034 head 已合并、P3-BE-01 |
| P3-BE-02 | AccountPersonaService CRUD、revision 行锁、幂等、无服务端缓存 | 服务与单测 | 1 人日 | P3-DB-01 |
| P3-API-01 | GET/PUT/reset、摘要字段、显式 admin 鉴权、统一错误码 | 路由/API 测试 | 0.75 人日 | P3-BE-02 |
| P3-AUD-01 | OwnedGroupAuditEvent 安全摘要写入与账号游标查询 | 审计服务/API 测试 | 0.5 人日 | P3-BE-02 |
| P3-FLAG-01 | Persona 运行时设置节点、管理员专用 revision 接口和审计 | 设置 API/服务测试 | 0.5 人日 | 阶段 2 settings 合并后 |
| P3-FE-01 | 账号摘要、完整 CRUD Store 与 Persona 抽屉接真实 API | 前端页面与组件测试 | 0.75 人日 | P3-API-01、P3-FE-00 |
| P3-DB-02 | execution 七个快照字段与 legacy_default 回填 | 顺延迁移 | 0.5 人日 | 阶段 2 表已合并 |
| P3-BE-03 | execution 创建事务内 Persona 与 business_snapshot_v1 解析/冻结 | ExecutionService 集成 | 0.75 人日 | P3-BE-02、P3-DB-02 |
| P3-BE-04 | AI ContentService 接 Prompt Builder；模板硬旁路；AI 失败禁止模板 fallback | ContentService 集成 | 0.75 人日 | P3-AI-01、阶段 2 ContentService |
| P3-BE-05 | 生成后/CTA 后/发送前三次治理检查 | 内容与发送链集成 | 0.75 人日 | P3-GOV-01、阶段 2发送链 |
| P3-BE-06 | stale generating 租约终态收敛、同租约 generation_attempt 缓存隔离 | ExecutionService/Worker 测试 | 0.25 人日 | P3-BE-04、阶段 2 Worker |
| P3-API-02 | preview 接口、限流、隔离缓存和审计 | API/服务测试 | 0.75 人日 | P3-BE-04、P3-AI-02 |
| P3-API-03 | 阶段 2 policy/execution Persona 摘要增量字段 | API 测试 | 0.25 人日 | P3-BE-03 |
| P3-FE-02 | 预览区、阶段 2策略页摘要与跨页入口 | 前端组件测试 | 0.75 人日 | P3-API-02/03 |
| P3-OBS-01 | 日志、指标、增长广告配置零访问断言 | 仪表与告警规则 | 0.5 人日 | 集成完成 |
| P3-RET-01 | Persona execution 正文型快照及 account_persona/persona_setting 审计的批量保留清理 | 每日任务、配置和测试 | 0.25–0.5 人日 | P3-DB-02 |
| P3-QA-01 | 90 条验收、迁移双库、阶段 1/2 回归、灰度记录 | 测试报告 | 1 人日 | 全部 |

说明：任务估时存在并行关系，不应简单相加；总体以文档信息中的 8–11 人日为准。若阶段 2 已实现 P3-AI-02/P3-GOV-01 等共用 P0 能力，只做复核和补测，不重复建链。

### 18.3 推荐合并顺序

~~~text
MR1  Persona Schema + LLM system/cache/token
MR2  Prompt Builder + 出站治理检查器 + 纯单测
—— 阶段 2 表结构/迁移 head 冻结门 ——
MR3  账号字段与 execution 快照两组顺序迁移
MR4  Persona Service + CRUD/审计 API
MR5  Content/Execution 集成
MR6  preview + 阶段 2 API 摘要
MR7  前端抽屉/预览/策略联动
MR8  指标、保留清理、全量回归、灰度配置
~~~

每个 MR 保持默认开关关闭；P3-DB-01 可提前起草但其 Alembic revision 必须以下阶段 2 最终 head 为 down_revision，不得从 033 分叉；不得在 MR5 前以临时方式从阶段 2 Worker 直接读取账号当前 Persona。

## 19. 文件级改动清单

以下路径以当前代码基线和阶段 2 规格命名为准；阶段 2 实际模块路径不同，只允许改路径，不改变本规格的职责边界。

### 19.1 后端新增

~~~text
backend/app/core/account/persona.py
backend/app/api/account_personas.py
backend/app/modules/owned_group/messaging_prompt_builder.py
backend/app/modules/owned_group/messaging_outbound_governance.py
backend/app/modules/owned_group/messaging_retention.py
backend/migrations/049_add_account_ai_persona.sql
backend/migrations/050_add_owned_group_message_persona_snapshot.sql
backend/migrations/versions/035_add_account_ai_persona.py
backend/migrations/versions/036_add_owned_group_message_persona_snapshot.py
~~~

职责：

- `persona.py`：PersonaV1、规范化、hash、EffectivePersona、AccountPersonaService。
- `account_personas.py`：五个专用接口、权限与错误映射，不承载业务规则。
- `account_personas.py` 内 API 请求/响应 Schema 与领域 PersonaV1 分层，不直接暴露 ORM。
- `messaging_prompt_builder.py`：纯 Prompt 组装与 hash；不访问 Telegram、Redis 或增长广告模型。
- `messaging_outbound_governance.py`：无白名单、无副作用的最终文本检查。

### 19.2 后端修改

~~~text
backend/app/core/account/models.py
backend/app/api/accounts.py
backend/app/api/automation.py  # operation_mode 写入口统一账号→配置锁顺序
backend/app/main.py
backend/app/core/database.py
backend/app/core/ai/llm_client.py
backend/app/modules/guardian/moderation/rule_engine.py  # 仅抽取可复用纯匹配逻辑
backend/app/core/runtime_settings.py
backend/app/core/automation_settings.py
backend/app/core/config.py
backend/app/api/settings.py
backend/app/core/scheduler/tasks.py
backend/app/celery.py
backend/scripts/apply_sql_migrations.py
backend/app/api/owned_group_messages.py
backend/app/modules/owned_group/messaging_contracts.py
backend/app/modules/owned_group/messaging_models.py
backend/app/modules/owned_group/messaging_schemas.py
backend/app/modules/owned_group/messaging_target.py
backend/app/modules/owned_group/messaging_policy_service.py
backend/app/modules/owned_group/messaging_trigger_service.py
backend/app/modules/owned_group/messaging_content_service.py
backend/app/modules/owned_group/messaging_execution_service.py
backend/app/modules/owned_group/messaging_worker.py
backend/migrations/env.py
backend/tests/conftest.py
.env.example
docker-compose.yml
docker-compose.production.yml
~~~

修改约束：

- `accounts.py` 只加列表摘要和必要 ORM 映射；Persona 写接口放独立路由。
- `automation.py` 的单个/批量 operation_mode 更新与 Persona PUT/执行创建统一采用 TelegramAccount → AccountOperationConfig 锁顺序；批量时先按 account_id 升序锁全部账号，再按相同顺序锁配置，避免交叉死锁。
- `llm_client.py` 保留现有 `generate()->str` 原签名与行为；新增 `generate_response(..., cache_context)->LLMResponse`，阶段 3 ContentService 是新入口唯一业务调用方。不得把本阶段扩展变成全仓旧调用迁移。
- `rule_engine.py` 只抽取共同的纯规则匹配，不能让出站检查继承白名单和处罚副作用。
- 阶段 2 ContentService 是 Persona 唯一消费点；Scheduler/Worker/TelegramExecutionService 不拼 Prompt。
- 不修改增长中心广告 API、Campaign 服务、AdDelivery Worker 和 acquisition stop 逻辑。
- `database.py`、`migrations/env.py`、`tests/conftest.py` 显式注册/导入阶段 2 execution 模型后再生成 035/036；`.env.example` 和两套 Compose 显式传递 `OWNED_GROUP_AI_PERSONA_ENABLED=false`，生产不得依赖未写出的隐式默认。
- CI 对 persona.py、account_personas.py、messaging_prompt_builder/content/execution/outbound_governance/worker 做 AST import 黑名单；集成测试记录 SQL 访问表名并拒绝第 5.4 节广告表。

### 19.3 前端新增

~~~text
frontend/src/api/accountPersonas.ts
frontend/src/stores/accountPersona.ts
frontend/src/components/accounts/AccountPersonaDrawer.vue
frontend/src/components/accounts/PersonaForm.vue
frontend/src/components/accounts/PersonaPreviewPanel.vue
~~~

### 19.4 前端修改

~~~text
frontend/src/views/Accounts.vue
frontend/src/api/accounts.ts
frontend/src/stores/account.ts
frontend/src/views/OwnedGroupMessaging.vue
frontend/src/api/ownedGroupMessaging.ts
frontend/src/views/Settings.vue
frontend/src/api/settings.ts
frontend/src/stores/settings.ts
~~~

若 `frontend/src/components/accounts/` 当前不存在则创建；不把大型 Persona 表单继续堆入 Accounts.vue。

## 20. 测试文件与验证命令

### 20.1 后端新增测试

~~~text
backend/tests/test_account_persona_schema.py
backend/tests/test_account_persona_service.py
backend/tests/test_account_persona_api.py
backend/tests/test_account_persona_audit_api.py
backend/tests/test_account_persona_feature_settings.py
backend/tests/test_owned_group_persona_prompt_builder.py
backend/tests/test_owned_group_outbound_governance.py
backend/tests/test_llm_system_prompt_contract.py
backend/tests/test_llm_persona_cache_isolation.py
backend/tests/test_owned_group_persona_execution_snapshot.py
backend/tests/test_owned_group_persona_content_integration.py
backend/tests/test_owned_group_persona_stale_generation.py
backend/tests/test_owned_group_persona_preview.py
backend/tests/test_owned_group_persona_growth_ad_isolation.py
backend/tests/test_owned_group_persona_migrations.py
backend/tests/test_owned_group_persona_retention.py
~~~

测试要求：

- Persona Schema/Prompt Builder 使用纯单测和参数化边界测试。
- API 使用 admin/operator/auditor/普通用户四类身份。
- 事务并发使用两个独立数据库 Session，不用 mock 伪造行锁。
- FakeLLM 记录 system/user 的 hash 和调用次数，不把 Prompt 打进测试输出。
- stale-generating 测试让 FakeLLM 延迟返回，在第二 Session 收敛过期租约；断言迟到结果不写 content、调用次数不增加、状态保持 failed。
- FakeTelegram 只记录 account_id、chat_id 和 content_hash；P0 验收不发送真实消息。
- PostgreSQL 验证真实 JSON、CHECK、行锁和迁移；SQLite 只用于快速兼容测试。

### 20.2 前端新增测试

~~~text
frontend/src/api/accountPersonas.spec.ts
frontend/src/stores/accountPersona.spec.ts
frontend/src/components/accounts/AccountPersonaDrawer.spec.ts
frontend/src/components/accounts/PersonaForm.spec.ts
frontend/src/components/accounts/PersonaPreviewPanel.spec.ts
frontend/src/views/Accounts.persona.spec.ts
frontend/src/api/settings.spec.ts
frontend/src/stores/settings.spec.ts
frontend/src/views/Settings.spec.ts
~~~

至少覆盖：字段边界、权限只读、账号切换清理、保存成功、409 草稿保留、reset 二次确认、preview loading/失败、功能关闭提示、模板不展示 Persona 应用状态；SettingsReadData 与 SettingsUpdatePayload 隔离、通用 PUT 不含 Persona 节点、专用 PUT 携带 expectedRevision、开关 409 刷新但不自动重放、静态 false 的未生效提示。

### 20.3 聚焦验证命令

~~~powershell
cd E:\project\Vanguard\backend
pytest tests/test_account_persona_schema.py tests/test_account_persona_service.py tests/test_account_persona_api.py -v
pytest tests/test_owned_group_persona_prompt_builder.py tests/test_owned_group_outbound_governance.py -v
pytest tests/test_llm_system_prompt_contract.py tests/test_llm_persona_cache_isolation.py -v
pytest tests/test_owned_group_persona_execution_snapshot.py tests/test_owned_group_persona_content_integration.py tests/test_owned_group_persona_stale_generation.py tests/test_owned_group_persona_preview.py -v
pytest tests/test_owned_group_persona_growth_ad_isolation.py tests/test_owned_group_persona_migrations.py -v

cd E:\project\Vanguard\frontend
npm run test -- accountPersona AccountPersona PersonaForm PersonaPreview Accounts.persona
npm run type-check
npm run build
~~~

### 20.4 全量回归

~~~powershell
cd E:\project\Vanguard\backend
pytest tests\ -v
ruff check app\ tests\

cd E:\project\Vanguard\frontend
npm run test
npm run type-check
npm run build
~~~

阶段 2 实际测试文件名若变化，MR 中同步更新命令；不能以“文件不存在”为理由跳过 Persona 与阶段 2 的集成回归。

## 21. 上线、灰度与回滚

### 21.1 上线前检查

1. 阶段 2 契约门槛全部满足，迁移 head 唯一。
2. 所有计划承载 configured/draft Persona 的 LLM Provider 通过 system 指令能力探测；LOCAL 未支持时不得承载 Persona，但可继续服务 requires_system_role=false 的阶段 2 中性兼容路径。
3. 90 条验收通过，尤其是模板旁路、账号一致性和增长广告配置零访问。
4. 静态与运行时 Persona 开关均保持 false。
5. 数据库备份、迁移耗时、表锁影响和回滚命令已在预生产演练。
6. 选取至少两个测试 growth 账号和一个 managed 自建群；不得使用真实用户群作为首批验证。

### 21.2 灰度步骤

| 步骤 | 操作 | 放行标准 |
| --- | --- | --- |
| 1 | 部署后端迁移/API/前端，双开关关闭 | 阶段 1/2 行为无变化 |
| 2 | 在预生产把双开关打开，使用两个测试账号验证完整链 | 两 Persona Prompt 与实际预览风格均可区分，无串号 |
| 3 | 生产双开关关闭，为唯一指定 canary 账号保存 Persona 并只做 preview | revision、审计、Provider、治理均正常 |
| 4 | 生产打开静态开关，运行时仍关闭 | 新 AI execution 显示 feature_disabled_default |
| 5 | 确认除 canary 外账号均为未配置，再打开生产运行时开关，单账号 + 单自建群灰度 community AI | 未配置账号保持阶段 2 中性基线，24 小时无 P0 告警 |
| 6 | 增加 promotion AI 灰度 | CTA/URL 确定性、三次治理检查通过 |
| 7 | 逐步为 10% growth 账号配置 Persona，再逐步扩大配置覆盖 | 失败率、Token、阻断率在阈值内 |

阶段 3 灰度不得打开或改变现有增长中心外部群广告能力，也不得更改增长中心广告配置。

双开关是全局开关，第一版没有租户/账号 allowlist；灰度单位是“已配置 Persona 的账号集合”。因此生产运行时开关打开前必须盘点 `ai_persona IS NOT NULL`，确保只有本轮 canary 账号，且未配置账号的中性 Prompt 回归等价已经通过。

### 21.3 立即回滚触发条件

- 发现 execution.account_id 与 Persona/发送账号不一致。
- Persona 或 Prompt 原文进入日志、指标、审计或通用账号响应。
- template 输出因 Persona 发生变化。
- 自建群消息开始读取 auto_ads_enabled、acquisition_stop 或任何增长广告对象。
- requires_system_role=true 的 Provider 丢弃 system 指令仍被允许生成。
- 出站治理被发送账号白名单绕过。
- promotion CTA/URL 被 LLM 修改、删除或增加。

### 21.4 回滚操作

1. 将 `ownedGroupAiPersona.enabled=false`；若运行时设置不可用，将 `OWNED_GROUP_AI_PERSONA_ENABLED=false` 并滚动重启后端。
2. 确认新 AI execution 使用 feature_disabled_default；阶段 2 模板和中性 AI 链继续工作。
3. 对已生成未发送且疑似受影响的 execution，pending_review 按阶段 2拒绝；queued/ready_to_send 通过禁用 policy 或将对应类别 mode 设为 off 触发既有取消语义，不直接改数据库状态。
4. 保留 Persona 配置、revision、hash 和审计，便于修复后恢复；不做批量清空。
5. 应用版本回滚时保留新增 nullable 列。只有确认不存在依赖这些列的 execution、应用版本和回滚窗口后，才执行 Alembic downgrade。

数据库降级不是首选紧急回滚手段；功能开关和应用版本回滚应先完成。

## 22. Definition of Done

以下全部满足才可声明阶段 3 完成：

- PersonaV1 Schema、规范化、语义校验、中性默认、hash 合同实现并有边界测试。
- TelegramAccount 字段和 execution 快照字段均有 SQL + Alembic 升降级，历史数据迁移可解释。
- 五个专用 API、显式权限、乐观锁、幂等、预览限流和安全审计可用。
- AI execution 在创建事务内按 account_id 冻结 Persona；生成失败不自动重试，发送重试不回读当前配置。
- 进程崩溃遗留的 stale generating 会终态失败且不重放 LLM；阶段 2 同租约去重生成使用独立 cache scope。
- template 路径有代码级硬旁路和查询零次断言。
- Prompt 优先级、冻结 business_snapshot_v1、上下文不可信封装、治理投影和 system_prompt 低优先级语义实现。
- LLM Provider system 能力、缓存隔离和完整 Token 预算通过测试。
- Persona 功能有效启用时，community/promotion 均通过生成后和发送前治理；功能关闭或 execution 来源为 legacy/feature_disabled 时严格保持阶段 2 行为。promotion CTA/URL 始终由阶段 2 确定性代码控制。
- Growth 账号在自建群发送消息/推广完全不受增长中心广告配置影响，并有零访问指标与回归测试。
- 前端账号列表、Persona 抽屉、预览、冲突处理、阶段 2摘要联动完成。
- 结构化日志/指标不包含 Persona、Prompt、上下文和凭据原文。
- 90 条验收、阶段 1/2 回归、后端 lint/测试、前端测试/type-check/build 全部通过。
- 默认双开关关闭，完成预生产迁移演练、Provider 探测、灰度记录和回滚演练。

## 23. 已固定决策与开发禁止项

### 23.1 已固定决策

1. 阶段 3 只做账号级 Persona；现有增长中心外部群广告保持原状，已取消的原阶段 4 及其可选 CTA 资产化、归因不实施。
2. Persona 归属 TelegramAccount.id，全局作用于该账号的所有阶段 2 自建群 AI 消息。
3. 适用条件固定为 account_type=promoter 且 operation_mode=growth。
4. community AI 与 promotion AI 使用 Persona；任何 template 模式完全不使用。
5. Persona 修改只影响之后创建的新 execution；存量任务使用冻结快照。
6. `ai_persona IS NULL` 是中性默认；空对象、未知 Schema 和损坏数据是错误，不静默降级。
7. 第一版每账号一份当前 Persona，无群级覆盖、无多版本选择、无自动学习。
8. system_prompt 是低优先级风格备注，不是可直接覆盖系统指令的逃生口。
9. promotion CTA/URL 由阶段 2确定性生成，Persona 只影响正文。
10. 出站治理没有管理员/白名单绕过且没有处罚副作用。
11. core_group_id 用于内部规则，telegram_chat_id 只用于 Telegram I/O。
12. Persona 双开关默认关闭；关闭后新 AI 任务中性化，不停止阶段 2消息。
13. Growth 自建群消息和推广不读取、不继承、不受增长中心广告配置、开关、额度、冷却和增长中心停止链影响。
14. AI 链任一失败保持 AI 失败语义，绝不自动回退模板；Persona 功能关闭则走显式阶段 2兼容旁路，不是错误兜底。
15. allowed_topics 与群标题在 execution 创建时冻结进 business_snapshot_v1，生成与发送重试不回读当前 policy/群记录。
16. 生成失败不跨 tick 自动重试；stale generating 租约终态失败。同租约内容去重尝试不改变 Persona/业务快照且必须隔离 LLM 缓存。

### 23.2 开发禁止项

- 禁止从 AccountPool、群 owner 或在线账号列表推断 Persona 账号。
- 禁止在发送时回读当前 Persona 覆盖 execution 快照。
- 禁止让 template 路径为了展示摘要而读取或快照 Persona。
- 禁止 AI 失败后查询模板、修改 mode_snapshot、读取 default_template_id 或发送模板兜底。
- 禁止把用户 Persona system_prompt 原样作为最高优先级系统指令。
- 禁止在 configured/draft 或其他 requires_system_role=true 路径中，因 Provider 不支持 system 角色而拼接文本后继续实发。
- 禁止复用带发送者白名单或处罚副作用的入站治理入口。
- 禁止把增长中心广告许可当作阶段 2 自建群 promotion 的前置条件。
- 禁止用 Persona 自动生成新 URL、追踪参数、Telegram 邀请链接或外部投放权限。
- 禁止把原始 Persona、Prompt、上下文、最终消息全文写入审计/日志/指标。
- 禁止在阶段 2 契约未冻结时以临时字段或临时 Worker 分支抢接 Persona。

本文档字段、状态、权限、错误码、事务边界和 90 条验收均为阶段 3 开发合同；如阶段 2 最终实现与本文的模块路径或迁移编号不同，只允许做机械适配。任何涉及账号选择、快照时点、模板旁路、治理优先级或增长中心隔离的语义变化，必须先更新需求并重新评审。
