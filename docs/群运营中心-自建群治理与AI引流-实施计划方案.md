# 群运营中心：自建群治理、AI运营与外部引流实施计划方案

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 滚动实施基线；阶段 4 已取消独立开发；阶段 5 已于 2026-09-11 完成本地开发与回归，待一次性真实 PostgreSQL CAS 门禁、阶段 6 Telegram 灰度及部署验收 |
| 编制日期 | 2026-09-09 |
| 适用项目 | Vanguard |
| 实施策略 | 少改动、最大化复用现有能力 |
| 调整后方案总工作量 | 35–55 人日；真实 Telegram 灰度另计 3–7 人日。按阶段 0/1/2/3/5/6 分别为 3–5、7–10、7–10、5–8、8–12、5–10 人日相加；阶段 4 原 4–7 人日已扣除；可选增强另行估算 |

## 1. 目标与背景

当前系统已经分别具备自建群编排、Guardian Bot 群治理和增长群广告投放能力，但部分管理入口尚未形成完整业务闭环：自建群创建完成后不能直接接入 Guardian 治理；群内推广账号的 AI/模板消息没有统一的账号级策略；成员身份无法在一个视图中区分。外部群软广已经可以通过广告素材 `link_url` 指向自建群，不再作为待建设的新业务能力。

本方案把业务统一为“群运营中心”，覆盖以下完整链路：

```text
创建自建群
  → 编排系统账号和机器人
  → 接入 Guardian Bot 治理
  → 配置群内推广账号与 AI 性格
  → 发送 AI/模板/活动消息
  → 推广账号搜索外部群并投放广告
  → 用户通过邀请链接进入自建群
  → 验证、反垃圾、处罚、公告和转化统计
```

本次采用“产品上统一、执行上分层”的原则：统一用户入口、群详情、成员视图和审计视图；继续保留自建群、Guardian 治理、增长投放各自的数据库语义、Worker、权限校验和停止开关。

## 2. 范围与明确边界

### 2.1 本次要交付

1. 自建群支持 Guardian Bot 绑定和治理能力。
2. 自建群支持入群验证、敏感词、白名单、反垃圾、警告、禁言、封禁。
3. 自建群支持公告、置顶、群内活动和优惠券。
4. 自建群内的推广账号可以按群配置 AI 消息或指定模块消息。
5. 每个推广账号拥有独立的 AI 性格、语气、兴趣和禁用主题。
6. 外部群搜群、加群和广告投放通过现有广告素材 `link_url` 指向自建群（现有能力已满足，不新增开发）。
7. 统一展示真实用户、系统广告账号、系统机器人三类成员。
8. 在前端提供统一群运营入口，同时保留现有页面兼容性。

### 2.2 本次不做

- 不合并 `OwnedGroupAsset` 与 `ManagedGroupBinding` 两张核心表。
- 不让自建群 Worker 和 Guardian Worker 共用状态机。
- 不重写现有 Guardian 事件处理链。
- 不把外部群广告和自建群内部 AI 互动定义成同一种广告任务。
- 不立即迁移全部历史成员数据到全新统一成员表。
- 不改变增长群池、客服、私聊和原有任务的停止语义。
- 不在没有 Telegram 凭据和灰度授权的情况下执行真实群操作。

## 3. 当前实现基线与缺口

### 3.1 已有能力

| 领域 | 当前实现 | 可复用能力 |
| --- | --- | --- |
| 自建群编排 | `backend/app/modules/owned_group`、`OwnedGroupAsset`、异步 Worker | 创建群、批量成员操作、管理员提权、邀请链接、重试、对账、审计 |
| Guardian 治理 | `backend/app/modules/guardian`、`ManagedGroupBinding` | 入群验证、规则匹配、敏感词、反垃圾、警告、禁言、封禁、公告、置顶、活动 |
| 增长投放 | `backend/app/modules/acquisition` | 搜群、加群、广告许可探测、AD_ONLY 账号、广告计划、发送和转化记录 |
| AI/模板消息 | `Speaker`、语义回复引擎、关键词触发、模板引擎 | AI 暖场、语义回复、指定模板、限流和冷却 |
| Bot 广播 | Guardian broadcaster 和 managed-group API | Bot 消息、频道消息、置顶公告、静默发送 |

### 3.2 必须补齐的缺口

1. `OwnedGroupAsset` 只有自己的资产和任务关系，没有 `Group`/`ManagedGroupBinding` 关联。
2. 群治理 API 只接受已经存在的 `ManagedGroupBinding`，自建群无法直接使用。
3. 治理代码中内部 `Group.id` 与 Telegram `chat_id` 的语义存在混用风险。
4. 自建群编排中的 `ResourceType` 只有 `user` 和 `bot`，不能表达真实用户、系统广告账号、系统机器人三类业务身份。
5. AI 设置目前以全局或群级为主，没有账号级可持久化 Persona。
6. `OwnedBotProfile` 与 Guardian Bot 账号有复用关系，需要通过角色和归属字段限制权限。

外部广告素材尚未结构化关联 `OwnedGroupAsset`，因此不能自动跟随邀请链接轮换或按承接资产统计；该项属于可选运维增强，不是本方案必须补齐的业务缺口。

## 4. 目标业务模型

### 4.1 群资产生命周期

自建群继续使用原有编排状态，在治理侧增加一段交接状态：

```text
DRAFT
  → PRECHECKING
  → CREATING
  → READY
  → GOVERNANCE_PENDING
  → MANAGED
  → DEGRADED / ARCHIVED
```

- `READY`：Telegram 群已经创建，成员编排任务完成或达到可交接条件。
- `GOVERNANCE_PENDING`：正在创建/关联核心群记录并检查 Guardian Bot 权限。
- `MANAGED`：Guardian Bot 权限核验通过，治理策略已初始化。
- `DEGRADED`：群仍存在，但 Bot 权限、同步或策略存在问题；禁止假定治理已经生效。
- `ARCHIVED`：业务侧归档，不删除 Telegram 群。

### 4.2 三条执行链

```text
自建群资产链：创建、邀请、成员核验、管理员和邀请链接

自建群治理链：Guardian Bot 事件、验证、审核、处罚、公告和活动

外部获客链：搜群、加群、广告许可探测、AD_ONLY 投放和转化归因
```

三条链通过 Telegram Chat ID、核心 `Group` 记录和治理绑定关联；不共用核心 Worker。

### 4.3 成员业务身份

统一群详情中使用以下业务字段：

```text
member_kind = real_user | system_ad_account | system_bot
```

| 成员类型 | 识别来源 | 说明 |
| --- | --- | --- |
| `real_user` | Telegram 用户事件、`User.telegram_id` | 自然加入或通过邀请链接进入的真实用户 |
| `system_ad_account` | `TelegramAccount`，通常为 `PROMOTER + AD_ONLY` | 系统控制的推广账号 |
| `system_bot` | `OwnedBotProfile` / Guardian Bot profile | 系统机器人，必须记录所属账号和机器人角色 |

成员视图还应显示 `source`、`parent_account_id`、群内角色、管理员权限、验证状态、最近核验时间和风控状态。

## 5. 最小改动架构

### 5.1 自建群到治理群的桥接

优先采用在 `owned_group_assets` 增加可空关联字段的方式，避免立即引入完整统一资产注册表：

```text
owned_group_assets
- core_group_id              nullable FK group.id
- managed_binding_id         nullable unique FK managed_group_binding.id
- guardian_bot_account_id   nullable FK telegram_account.id
- governance_status          pending / managed / degraded / disabled
- governance_enabled_at     nullable timestamp
```

启用治理时执行幂等流程：

```text
按 telegram_chat_id 查找或创建 core Group
  → 校验 Guardian Bot profile 和账号状态
  → 使用 Bot API 查询管理员身份与权限
  → 创建或复用 ManagedGroupBinding
  → 初始化验证、审核和处罚策略
  → 写回 owned_group_assets 关联字段
```

若历史数据或外键约束不适合直接加字段，可使用一张 `owned_group_governance_binding` 关联表实现同样语义；不改变两个领域的主表和 Worker。

### 5.2 ID 规范

统一约定：

```text
外部 API、前端和 Telegram 调用参数：telegram_chat_id
内部治理策略、违规和活动表关联：core_group_id
```

增加统一解析服务，所有旧接口先兼容现有参数，再逐步将内部调用迁移到明确的 `core_group_id`。

### 5.3 消息能力复用策略

不新建大型统一消息系统，先增加目标群解析和发送目的标识：

| 消息目的 | 发送者 | 复用实现 |
| --- | --- | --- |
| `governance` | Guardian Bot | Guardian 事件处理和处罚模块 |
| `announcement` | Guardian Bot | managed-group 公告/置顶接口 |
| `activity` | Guardian Bot 或授权账号 | 现有群内活动、优惠券和广播 |
| `community_ai` | 推广账号 | `Speaker`、语义回复和 AI 设置 |
| `template` | 推广账号或授权 Bot | 模板引擎和关键词触发 |
| `ad_delivery` | AD_ONLY 推广账号 | acquisition 广告投放链路 |

每条发送任务都必须经过现有的账号可用性、群权限、限流、去重和风控检查。

## 6. 分阶段实施计划

### 阶段 0：基线审计和 ID 修正

预计 3–5 人日。

工作内容：

- 核对 `Group.id`、`Group.group_id`、`OwnedGroupAsset.telegram_chat_id`、`ManagedGroupBinding.telegram_group_id` 的实际含义。
- 增加 `telegram_chat_id ↔ core_group_id` 解析服务。
- 修正治理策略读写和 Worker 查询中的 ID 混用。
- 检查历史重复 Chat ID，输出迁移报告。
- 为新功能增加独立 feature flag 和审计事件类型。

交付物：

- ID 映射服务和兼容测试。
- 历史数据检查脚本/报告。
- 治理策略读写不再依赖隐式 ID 推断。

验收：已有托管群的验证、禁言、处罚、公告和活动配置读取结果不变，配置保存后 Worker 使用同一个核心群记录。

### 阶段 1：自建群接入 Guardian 治理

预计 7–10 人日。

后端工作：

- 增加自建群与核心 `Group`、`ManagedGroupBinding` 的关联字段或桥接表。
- 新增“接入治理”幂等服务和接口。
- 复用 Guardian Bot 权限探针。
- 初始化验证、审核、处罚策略。
- 自建群 READY 后支持治理状态查询和 reconcile。
- 将自建群的 Telegram 更新纳入现有 Guardian 事件消费链。

建议接口：

```text
POST /api/owned-groups/{asset_id}/governance/bind
GET  /api/owned-groups/{asset_id}/governance
POST /api/owned-groups/{asset_id}/governance/reconcile
```

前端工作：

- `OwnedGroups.vue` 增加“接入治理”按钮和 Guardian Bot 选择。
- 显示权限探针结果、治理状态和失败原因。
- 成功后跳转现有治理策略页。
- `ManagedGroups.vue` 显示“自建群”来源和资产链接。

验收：自建群绑定 Bot 后可以使用入群验证、敏感词、反垃圾、警告、禁言、封禁、公告、置顶和活动；重复提交不会生成重复绑定。

### 阶段 2：自建群内推广账号 AI/模板消息

预计 7–10 人日。

工作内容：

- 让自建群通过核心 `Group` 目标解析复用现有 `Speaker`、语义回复和模板引擎。
- 增加按群、按账号的消息模式：`ai`、`template`、`off`。
- 增加触发方式：定时、关键词、回复、手动。
- 增加每日上限、群级冷却、重复消息去重和人工审核开关。
- 将自建群内部消息标记为 `community_ai` 或 `template`，不计入外部广告投放。

建议策略字段：

```text
group_id
account_id
mode
trigger_config
daily_limit
cooldown_seconds
allowed_topics
require_review
enabled
```

第一版可新增轻量的 `group_account_message_policy` 表；不修改现有广告计划表的语义。

验收：指定推广账号能在指定自建群发送 AI 或模板消息；关闭策略、达到额度或触发冷却后不会发送；多个账号不会在短时间发送相同内容。

### 阶段 3：账号级 AI 性格

预计 5–8 人日。

为控制改动量，第一版在 `TelegramAccount` 增加 JSON 配置 `ai_persona`，后续规模扩大再拆独立 Persona 表。

建议结构：

```json
{
  "name": "技术型群友",
  "tone": "自然、克制、简短",
  "interests": ["网络稳定性", "节点速度"],
  "expertise": ["技术排障"],
  "reply_length": "short",
  "preferred_topics": ["使用体验", "配置建议"],
  "forbidden_topics": ["敏感话题", "过度营销"],
  "ad_style": "软性分享",
  "system_prompt": ""
}
```

AI 生成上下文固定为：

```text
全局安全规则
  + 当前群治理规则
  + 账号 Persona
  + 当前对话上下文
  + 本次消息目的
```

验收：不同账号生成的消息风格可区分；Persona 不能绕过群规则、账号风控或群内消息限流，也不授予增长中心外部投放权限；同时不得给自建群内部消息新增增长中心广告许可检查。没有 Persona 的账号使用安全的中性默认值。

### 阶段 4：外部群广告引流到自建群（现有能力已满足，取消独立开发）

> **状态：已取消，不创建阶段 4 开发任务。** 2026-09-11 复核确认，增长中心已经支持推广账号自动搜索/加入外部群、按广告许可和额度发送软广，并通过 `AdCreative.link_url` 或 `{{link_url}}` 将用户引流到指定自建群。

现有链路直接满足业务目标：

```text
推广账号搜索并加入外部群
  → 增长中心按现有规则选择计划与软广素材
  → 素材 link_url 配置为自建群公开链接或邀请链接
  → 系统替换 {{link_url}} 或在正文末尾追加链接
  → 向外部群投放
```

因此，本阶段不新增 `destination_owned_group_asset_id`，不改造现有广告状态机，也不以“外部群引流到自建群”为名建设第二套投放链。

以下内容仅可在业务另行批准后作为“小型可选增强”单独立项，不属于当前阶段：

1. 在广告计划中直接选择 `OwnedGroupAsset`，减少人工复制链接。
2. 邀请链接轮换后自动同步新链接。
3. 自建群归档或链接失效时自动阻断关联投放。
4. 按承接自建群统计投放或入群转化。

如只实现目标下拉选择和链接同步，重新评估约 2–4 人日；如增加链接生命周期、审计和统计，需另行评审，原 4–7 人日估算不再生效。

现有能力回归验收：人工把自建群有效链接配置到广告素材后，软广实际内容包含该链接并发送到外部群；Growth 账号在自建群内部发送 community、promotion、AI 或模板消息继续不受增长中心广告配置影响。

### 阶段 5：成员分类和群运营中心前端整合

详细规格：`群运营中心-阶段5-成员分类与前端整合-需求规格.md`。

> **状态：本地开发完成，尚未提交、推送或部署。** 后端全量 2051 passed、2 skipped，前端全量 197 passed，Ruff、TypeScript 类型检查和生产构建通过；真实 PostgreSQL CAS 因未提供测试库尚未执行，10k 数据性能及真实 Telegram 灰度留待阶段 6。

总工作量预计 8–12 人日；后端、前端、QA 三人并行的日历工期约 4–7 个工作日。原 4–6 人日只足以完成前端入口和系统资源拼接；当前代码没有真实用户群关系、退群状态事实，为避免形成伪“全量成员”页面，增加一张轻量观察表及 Guardian 事件投影。

第一版不迁移所有历史成员表、不抓取 Telegram 全量名单。统一群详情服务组合已有数据和启用后的观察事实：

- 自建群编排成员：`OwnedGroupMembership`。
- 核心群推广账号：`GroupAccountMembership`。
- 真实用户：`OwnedGroupMemberObservation` 记录 Guardian 已观察的入群、发言、退群事实，`User` 只补充全局资料。
- Bot：`OwnedBotProfile`/Guardian Bot profile。

成员页固定显示 `data_scope=managed_and_observed`、`is_complete=false`，不得宣称 Telegram 全量或实时成员名单。成员业务分类由统一读模型推导，不持久化可编辑的 `member_kind`。

前端新增或调整：

```text
群运营中心
├── 群资产总览
├── 自建群编排
├── Guardian Bot 治理
├── 成员与角色
├── 群内 AI/模板消息
├── 公告与活动
└── 任务与审计
```

保留旧路径 `/owned-groups`、`/guardian/groups`、`/guardian/policies`，通过入口和跳转兼容已有书签。

验收：用户从自建群详情可以进入治理、成员、AI 消息、活动和审计；系统 Bot、系统推广账号、Guardian 已观察真实用户可正确分类去重；能力不可用时按状态显示并阻止操作；页面明确非全量成员范围，不新增外部引流卡片、状态、按钮或 API。

### 阶段 6：测试、灰度和上线

预计 5–10 人日，真实 Telegram 灰度另计 3–7 人日。

测试范围：

1. 数据迁移和旧数据兼容。
2. 自建群接入治理的幂等、权限和失败恢复。
3. 入群验证、敏感词、反垃圾、警告、禁言、封禁。
4. 公告、置顶、活动和优惠券权限。
5. AI Persona Prompt 组装和账号差异。
6. AI/模板消息限流、去重和人工审核。
7. 现有外部广告 `link_url`/CTA 回归；邀请链接自动轮换和入群归因不在本计划范围。
8. 三类成员识别和展示。
9. 自建群编排、治理与增长中心现有停止控制互不影响。
10. Bot、推广账号、真实用户权限不混淆。

## 7. 权限、安全和停止语义

### 7.1 发送者权限

| 角色 | 默认能力 |
| --- | --- |
| Guardian Bot | 入群验证、审核、警告、禁言、封禁、公告、置顶 |
| 推广账号 | 外部广告和被允许的自建群 AI/模板互动 |
| 系统机器人 | 按绑定的 Bot 角色授予有限能力，默认不拥有治理高权限 |
| 真实用户 | 普通群成员能力，受验证和治理策略约束 |

推广账号和广告机器人默认不能封禁、全员禁言或修改治理策略；如需管理员权限，必须按 Telegram 权限集合逐项授权并二次核验。

### 7.2 独立停止边界

```text
provisioning_stop  自建群创建、邀请、编排和重试
governance_stop    Guardian 事件、审核、处罚、公告和治理任务
增长中心现有停止控制  外部搜群、加群、广告许可探测和广告投放
```

任一链路停止时只影响对应执行域，不能通过换账号、换代理或重试绕过风控。阶段 4 已取消，因此本方案不新增 `acquisition_stop`；增长链继续使用现有自动加群、广告执行和账号级开关。

### 7.3 消息优先级

```text
治理通知/系统安全消息
  > 官方公告和活动
  > 真实用户触发的回复
  > 主动 AI 暖场
```

高优先级消息应占用独立额度或预留额度，避免普通 AI 发言挤占验证和治理通知资源。

## 8. 数据库与 API 变更清单

### 8.1 数据库迁移

预计 3–4 个正式迁移：

1. 自建群资产与核心群/Guardian 绑定关联、治理状态和唯一约束。
2. 账号 Persona 配置及必要的群账号消息策略。
3. 阶段 5 增加轻量 `OwnedGroupMemberObservation` 事实表，只记录 Guardian 已观察事件；成员分类仍由读模型推导，不持久化分类字段。

迁移要求：

- 先做只读重复数据检查，再加唯一约束。
- 新字段全部可空或有安全默认值，保证旧资产可以继续运行。
- 不因治理绑定失败删除 Telegram 群。
- 迁移失败时优先禁用新功能开关，不执行破坏性回滚。

### 8.2 API 变更

后续新增或扩展接口只来自尚未完成的治理、群内运营和统一视图阶段，主要分为：

- 自建群治理绑定、状态和 reconcile。
- 群治理策略对自建群的目标解析。
- 群内账号消息策略和 Persona。
- 统一成员详情和分类。
- 阶段 4 不新增广告目标或 CTA 接口；可选增强须另行立项。

写接口按业务合同返回或关联审计 ID；读接口返回 correlation ID 和已有的关联 ID，不为一次读取创建审计事件。资产标识、Telegram Chat ID 和能力状态只在与该接口语义相关且有可信来源时返回。

## 9. 文件影响范围（估算）

实际编码前需以当前分支和未提交改动为准，预计涉及：

| 层 | 主要范围 |
| --- | --- |
| 后端模型/迁移 | `owned_group/models*.py`、`guardian/models.py`、`core/group/models.py`、Alembic/SQL migrations |
| 后端 API | `owned_groups.py`、`owned_group_controls.py`、`managed_groups.py`、`group_governance.py`；不含阶段 4 广告 API |
| 后端服务/Worker | owned-group 交接服务、Guardian 目标解析、Speaker/AI Prompt；不含新的 ad delivery 目标解析 |
| 前端页面 | `OwnedGroups.vue`、`ManagedGroups.vue`、`GroupGovernancePolicies.vue`、`Accounts.vue`、群运营总览；不因阶段 4 修改 `Automation.vue` |
| 前端 API/状态 | `ownedGroups.ts`、`guardian.ts`、accounts API、相关 Pinia store；不新增阶段 4 automation API |
| 测试 | 迁移、权限、幂等、Prompt、现有广告 CTA 回归、成员分类、停止边界和 Telegram 适配器测试 |

## 10. 里程碑与交付标准

### M1：自建群可治理

完成阶段 0–1 后：

- 自建群可以绑定 Guardian Bot。
- 所有治理功能可以作用于自建群。
- 治理状态和失败原因可查询。
- 旧托管群功能无回归。

### M2：自建群可运营

完成阶段 2–3 后：

- 推广账号可按群启用 AI/模板消息。
- 每个账号有独立 Persona。
- 消息经过统一目标解析、限流、去重和审计。

### M3：外部群可引流（现有能力已具备）

无需新增阶段 4：

- 广告素材可通过现有 `link_url` 配置自建群公开链接或邀请链接。
- 推广账号沿用现有自动加群、软广投放、广告许可、额度和风控链。
- 外部广告与自建群内部消息保持独立配置和执行边界。
- 本里程碑只做现有链路回归，不产生新的数据库迁移或开发交付物。

### M4：群运营中心可用

完成阶段 5–6 后：

- 从一个群详情进入创建、治理、成员、AI、活动和审计。
- 权限和能力状态清晰展示。
- 完成 test001 和两账号以内 Telegram 灰度。

## 11. 上线与回滚方案

上线顺序：

```text
本地迁移与单元测试
  → test001 部署
  → 预检查和模拟治理
  → 测试群绑定 Guardian Bot
  → 验证验证/禁言/封禁/公告/置顶
  → 验证 Persona 和模板消息
  → 回归验证现有外部广告 link_url/CTA
  → 两账号以内灰度
  → 扩大账号和群规模
```

回滚原则：

- 优先关闭 `provisioning`、`governance`、`acquisition` 对应的新功能开关。
- 保留已创建的 Telegram 群和审计记录，不自动删除群。
- 保留旧 API 路径和旧字段读取逻辑。
- 数据库采用向前兼容迁移；只有确认无活动任务时才评估降级迁移。
- 每次灰度记录版本、迁移版本、Telegram Chat ID、Bot 权限快照和回滚时间。

当前生产环境仍需先确认 Telegram API ID、API Hash、Bot Token、代理和 `OWNED_GROUP_EXECUTION_ENABLED` 配置；在这些前置条件完成前，只执行代码、迁移、静态检查和模拟测试，不宣称真实 Telegram 闭环已经验收。

## 12. 待实施前的固定决策

以下默认值作为第一版基线：

1. 自建群接入治理采用显式“接入治理”操作，避免未经确认授予高权限。
2. Guardian Bot 负责治理和官方公告；推广账号负责外部投放及受控群内互动。
3. 外部广告继续使用素材中人工维护的 `link_url`；自动校验自建群链接有效性未立项，不得假定系统已经具备。
4. 没有配置 Persona 的账号使用中性安全 Prompt。
5. 自建群内部消息与外部群广告使用不同消息目的、统计口径和风控规则。
6. 第一版成员三分类采用统一读模型推导；新增轻量观察事实仅覆盖启用后的 Guardian 事件，完整历史成员重建与 Telegram 全量成员同步留到后续迭代，页面固定标记非全量。
7. 所有高权限动作都要求 Telegram 权限探针和执行后核验。

## 13. 最终决策

本项目采用以下实施决策：

```text
统一产品入口和群详情
  + 增加自建群 → Guardian 治理桥接
  + 复用现有 Speaker、模板、Guardian、广告投放能力
  + 增加账号级 AI Persona 和群级消息策略
  + 沿用现有广告素材 link_url 将外部流量引向自建群，不新增独立阶段
  + 保留自建群、治理、增长三条执行链的独立安全边界
```

这是当前需求下改动最少、复用率最高、上线风险可控的路径。先完成 M1，再逐步开放 M2 与 M4；M3 复用现有增长中心能力，仅做回归验收，不再实施原阶段 4，避免在 Telegram 真实执行尚未灰度验证前进行大规模结构重写。
