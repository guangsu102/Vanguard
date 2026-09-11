# 群运营中心阶段 2：自建群内推广账号 AI 与模板消息需求规格

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.1 |
| 文档状态 | 可直接开发 |
| 编制日期 | 2026-09-10 |
| 对应总方案 | 群运营中心：自建群治理、AI 运营与外部引流实施计划方案 |
| 对应阶段 | 阶段 2：自建群内推广账号 AI/模板消息 |
| 依赖阶段 | 阶段 0 的双 ID 解析、阶段 1 的自建群核心 Group 关联 |
| 预计工作量 | 10–14 人日；相较总方案增加群内广告独立配置、模板域隔离、增长广告反向排除与并发回归，不含真实 Telegram 凭据申请与生产灰度等待 |
| v1.1 修订 | GROWTH 账号在自建群发送普通消息或群内广告，均与增长中心广告配置完全隔离 |

## 1. 本阶段结论

本阶段交付一条按“自建群 + 推广账号”配置、可审核、可限流、可去重、可追踪的群内消息链：

~~~text
READY 且已接入治理的自建群
  → 选择已在群内的 GROWTH 推广账号
  → 配置 ai / template / off 模式
  → 配置 scheduled / keyword / reply / manual 触发
  → 生成或渲染消息
  → 内容安全、额度、冷却、跨账号去重和账号风控检查
  → 可选人工审核
  → 使用指定账号发送到指定 Telegram Chat
  → 写入执行记录、消息记录和审计事件
~~~

阶段 2 不新建大型统一消息平台，不修改广告计划表的语义。现有 Speaker、SemanticGroupReplyEngine、TemplateEngine、KeywordTrigger、AccountPool、TelegramExecutionService 和 AccountRiskGuard 继续复用，但新增一层自建群消息编排服务，解决现有能力缺少按群按账号策略、精确账号发送、审核队列、并发去重和执行审计的问题。

本阶段同时固定“自建群消息域独立”原则：GROWTH 账号在 OwnedGroupAsset 对应自建群内发送普通互动消息或群内推广广告，均只读取阶段 2 的群账号策略，不读取增长中心的广告计划、广告开关、广告额度、广告冷却、广告预热资格、广告许可探测或 acquisition_stop。群内广告仍受阶段 2 自身的审核、额度、冷却、内容去重和账号级风险控制，不等于无风控发送。

本阶段固定新增：

1. group_account_message_policy：按自建群和推广账号保存消息策略。
2. group_account_message_execution：保存触发、生成、审核、发送和失败全过程。
3. 自建群消息 API、事件路由、定时调度和执行 Worker。
4. 自建群详情下的“群内消息”配置与审核页面。

## 2. 当前实现基线与必须补齐的缺口

### 2.1 可直接复用

| 现有能力 | 复用方式 |
| --- | --- |
| OwnedGroupAsset | 作为自建群资产入口，取得 core_group_id 和 telegram_chat_id |
| OwnedGroupMembership | 判断指定推广账号是否已在自建群内 |
| Group | 作为内部群主记录，所有策略内部关联使用 Group.id |
| Speaker | 复用内容发送、模板调用和 AcquisitionMessage 写入能力 |
| SemanticGroupReplyEngine | 复用语义判定、上下文窗口和 AI 回复生成能力 |
| TemplateEngine / MessageTemplate | 复用模板加载和变量渲染能力 |
| KeywordTrigger | 复用关键词匹配、触发冷却、模板和 AI 动作配置 |
| AccountPool.acquire_by_id | 获取策略指定的精确推广账号 |
| TelegramExecutionService | 执行 Telegram 群消息并接入统一风控 |
| AccountRiskGuard | 发送前检查账号风险，发送失败后记录风险事件 |
| groupAiInteraction 运行设置 | AI 总开关、Token 预算、回复长度、语气和安全规则 |

### 2.2 当前缺口

现有 Speaker.speak_in_group 不能直接作为阶段 2 的公开编排入口，原因如下：

- 参数 group_id 的语义不明确，在内部 Group.id 与 Telegram Chat ID 之间存在混用风险。
- 未指定 account_id 时会从账号池选择任意账号，不满足按策略精确发送。
- 只有全局群 AI 设置，没有自建群 + 账号级消息模式。
- 没有四种触发方式的统一执行状态。
- 没有人工审核后的内容快照。
- 没有多个账号在同一群的内容去重。
- AcquisitionMessage 只能记录已发送消息，不能表达待审核、跳过、重试和失败。
- 现有主动暖场会更新外部广告预热计数，不符合自建群内部消息的业务语义。

### 2.3 兼容原则

- 旧 Speaker.speak_in_group 保留，旧调用行为不得因阶段 2 回归。
- 阶段 2 新增显式目标对象和新发送入口，不继续扩大含义不明的 group_id 参数。
- 自建群普通消息和群内推广广告都不写 ad_delivery_log，不占外部广告投放次数，不改变外部群广告资格。
- 自建群内部消息不增加 GroupAccountMembership.interaction_sent_today。
- 已被阶段 2 接管的自建群事件，旧关键词和旧语义回复链不得再次处理。
- 现有外部群 AI 暖场任务遇到自建群时必须跳过，避免双重发送。

### 2.4 自建群消息域与增长中心广告域隔离

阶段 2 的 owned_group_messaging 域不得依赖以下增长中心广告对象或设置：

| 增长中心对象/设置 | 阶段 2 行为 |
| --- | --- |
| AdCampaign、AccountAdBinding、AdCreative | 不查询、不绑定、不要求存在 |
| AdDeliveryScheduleState、AdDeliveryLog | 不创建、不更新，不用于阶段 2 状态 |
| GroupAdProfile、GroupAdPolicyEvent | 不读取群外广告许可，不写许可事件 |
| GroupAdOnlyAssessment、GroupAdHandover、GroupAdOnlyEvent | 不读取、不创建、不触发移交 |
| AdDeliveryPolicy.GROWTH / AD_ONLY | 不用于判断自建群消息是否可发 |
| AccountOperationConfig.auto_ads_enabled | 完全忽略；true/false 均不影响阶段 2 |
| automation.ad_delivery_throttle | 不读取其 enabled 或发送间隔 |
| automation.ad_delivery_execution | 不读取其 enabled、批量、租约或 growth_group_global_cooldown_seconds |
| automation.ad_capacity | 不读取广告时间窗、群容量、预热、探测和存活检查参数 |
| automation.ad_failure_policy | 不因外部群广告失败策略退出自建群或阻断自建群消息 |
| automation.ad_only_recommendation | 不读取、不生成移交建议 |
| acquisition_stop | 不阻断自建群普通消息或群内推广广告 |
| GroupAccountMembership.warmup_status / probe_status / ad_status | 不作为阶段 2 准入或发送门禁 |
| ad_delivery_log | 不写入，也不用于阶段 2 额度和冷却统计 |

阶段 2 仍必须读取共享账号安全状态：

- TelegramAccount.is_active、status、risk_level、risk_pause_until。
- AccountOperationConfig.enabled、operation_mode。
- AccountOperationConfig.max_messages_per_day 作为账号级全局出站安全硬上限。
- AccountRiskGuard 的封禁、FloodWait、PeerFlood 和账号限制结果。
- OwnedGroupMembership 的真实群成员资格。

禁止为了复用现有广告投放代码而调用 deliver_ads、AdCampaign dispatcher 或创建“仅用于自建群”的虚假广告计划。自建群消息与广告必须统一进入 group_account_message_execution。

反向隔离同样是强制要求：

- 增长中心创建或修改 AdCampaign/AccountAdBinding 时，不允许把非 archived 的 OwnedGroupAsset 对应 Group 作为目标。
- 旧数据或并发写入导致广告计划显式命中自建群时，广告 dispatcher 必须在最终发送前再次解析 telegram_chat_id/core_group_id 并跳过。
- 统一跳过原因使用 OWNED_GROUP_AD_DOMAIN_EXCLUDED。
- 该跳过不得创建 AdDeliveryScheduleState、AdDeliveryLog、存活探测或 tracking link。
- 自建群 promotion 只能从阶段 2 执行表发送，防止增长广告 dispatcher 与阶段 2 双通道各发一条。

## 3. 目标、成功标准与非目标

### 3.1 业务目标

1. 管理员可为一个自建群内的每个推广账号分别配置 ai、template 或 off。
2. 同一策略可独立开启定时、关键词、回复和手动触发。
3. 所有自动发送均受策略额度、群级冷却、全局硬上限和账号风控限制。
4. require_review 开启后，消息必须先生成并进入审核队列，批准后才可发送。
5. 同一群多个账号不得在去重窗口内发送相同或规范化后相同的普通消息或群内广告。
6. 每次未发送、已发送、失败或人工拒绝均能查询原因。
7. 阶段 2 的普通消息、群内推广广告、外部群广告投放和 Guardian 治理消息能够明确区分。

### 3.2 阶段成功标准

- 指定推广账号能在指定自建群发送 AI 或模板消息。
- 发送实际使用 policy.account_id，不发生账号池自动换号。
- 当前 content_category 对应 mode=off、policy.enabled=false、功能开关关闭、额度耗尽或冷却未结束时不调用 Telegram。
- 四种触发均生成统一执行记录并遵循同一套门禁。
- require_review=true 时，未经批准的消息绝不进入发送状态。
- 相同 source_message_id 的关键词或回复事件最多生成一条有效执行记录。
- 同群多个账号的相同内容在去重窗口内最多一条成功发送。
- 所有成功消息的生成目的标记为 community_ai 或 template，并用 content_category 区分 community 与 promotion。
- 旧外部群广告和 AI 暖场流程回归通过。

### 3.3 本阶段不做

- 不实现阶段 3 的账号级 AI Persona、口头禅、语言风格或独立提示词。
- 原计划阶段 4 已确认由现有增长中心 `link_url` 能力满足并取消独立开发；本阶段不实现可选的自建群目标资产化、链接自动轮换或转化归因，也不改写现有外部群许可探测和投放链。
- 不允许 AD_ONLY 账号发送自建群内部 AI/模板消息。
- 不允许 Guardian Bot 使用本阶段策略发送模板消息；Bot 公告和活动继续走既有链路。
- 不允许 community 类消息包含注册链接、购买链接、邀请链接或营销 CTA。
- promotion 类群内广告允许使用阶段 2 策略中独立配置的推广链接和 CTA，但不得从增长中心广告计划、追踪链接生成器或外部广告模板隐式取得。
- 不允许推广账号执行禁言、封禁、删除消息、置顶或修改群权限。
- 不自动把推广账号邀请进群；账号入群继续使用自建群编排能力。
- 不提供发送成功后自动删除、自动编辑或批量撤回。
- 不提供跨群批量启用策略。
- 不提供绕过限额、冷却、审核或风控的“强制发送”。

## 4. 用户角色与权限

当前后端只有 get_current_user 和 require_admin 两级依赖，因此第一版按下表实现：

| 操作 | admin | 其他已登录用户 |
| --- | --- | --- |
| 查看可选账号、策略和执行历史 | 是 | 是 |
| 生成预览 | 是 | 是 |
| 新建、修改、启用、停用策略 | 是 | 否 |
| 创建手动发送任务 | 是 | 否 |
| 审核通过、修改后通过、拒绝 | 是 | 否 |
| 修改 require_review 为 false | 是 | 否 |
| 查看完整失败详情和审计 | 是 | 仅脱敏摘要 |

所有写接口必须再次声明 Depends(require_admin)，不能只依赖 main.py 路由级的 get_current_user。

## 5. 名词、标识与前置条件

### 5.1 ID 规范

| 字段 | 含义 | 使用位置 |
| --- | --- | --- |
| asset_id / owned_group_asset_id | OwnedGroupAsset.id | 自建群业务入口、API 路径和审计 |
| core_group_id | Group.id | 数据库关联、策略、额度和统计 |
| telegram_chat_id | Telegram 群 Chat ID | Telegram 客户端调用、事件匹配 |
| account_id | TelegramAccount.id | 推广账号配置和 AccountPool 精确获取 |
| telegram_user_id | Telegram 用户 ID | 仅用于校验群成员身份，不作为策略外键 |
| policy_id | group_account_message_policy.id | 策略和执行关联 |
| execution_id | group_account_message_execution.id | 一次消息执行全流程 |
| source_message_id | Telegram 群内原消息 ID | 关键词/回复幂等和 reply_to |

禁止：

- 用 OwnedGroupAsset.id 调 Telegram。
- 用 Telegram Chat ID 查询以 Group.id 为外键的策略。
- 用 Telegram 用户 ID 查询 TelegramAccount 主键。
- 用 account_id 替代 OwnedBotProfile.id，或反向混用。
- 通过正负号、数值位数或 abs() 猜测 ID 类型。

### 5.2 显式目标对象

阶段 2 的服务层必须传递以下对象：

~~~python
@dataclass(frozen=True)
class OwnedGroupMessageTarget:
    owned_group_asset_id: int
    core_group_id: int
    telegram_chat_id: int
    managed_binding_id: int
~~~

目标解析必须验证：

1. OwnedGroupAsset 存在且 status=ready。
2. archived_at 为空。
3. core_group_id、telegram_chat_id、managed_binding_id 均非空。
4. Group.id 等于 core_group_id。
5. Group.group_id 等于 telegram_chat_id。
6. ManagedGroupBinding.id 等于 managed_binding_id。
7. Binding.group_id 等于 core_group_id，Binding.telegram_group_id 等于 telegram_chat_id。
8. Binding 为 ACTIVE。

任一不满足返回 TARGET_MAPPING_INVALID，禁止尝试推断或自动覆盖。

### 5.3 策略创建与启用前置

- 资产存在时允许创建 disabled 策略，便于阶段 1 并行开发。
- enabled=true 必须满足目标解析全部条件。
- enabled=true 必须满足 governance_status=managed。
- governance_status=degraded 或 disabled 时，预览仍可用，发送全部阻断。
- governance_stop 只停止 Guardian 治理任务，不直接删除或修改消息策略。
- 已进入 sending 前若治理状态变化，发送前最后一次门禁仍必须阻断。

### 5.4 推广账号准入

策略账号必须同时满足：

1. TelegramAccount.account_type=promoter。
2. TelegramAccount.is_active=true。
3. TelegramAccount.status 不为 error 或 banned。
4. TelegramAccount.risk_level 为 normal 或 watch。
5. risk_pause_until 为空或已到期。
6. AccountOperationConfig 存在且 enabled=true。
7. AccountOperationConfig.operation_mode=growth。
8. OwnedGroupMembership.resource_type=user。
9. OwnedGroupMembership.resource_id=TelegramAccount.id。
10. Membership.status 为 member_verified、admin_verified 或 skipped_already_member。
11. Membership.last_verified_at 在 24 小时内；超过 24 小时必须先执行只读成员探针。

无论 content_category 是 community 还是 promotion，auto_ads_enabled 均不作为阶段 2 准入条件；promotion 属于 owned_group_messaging，不属于增长中心 ad_delivery。账号为 ad_only 时无条件返回 ACCOUNT_MODE_NOT_ALLOWED。

策略只能使用指定账号。AccountPool.acquire_by_id 失败时进入重试或失败，禁止回退到 AccountPool.acquire。

## 6. 领域枚举与状态机

### 6.1 消息模式

~~~text
ai        使用 AI 生成内容，purpose=community_ai
template  使用 MessageTemplate 渲染内容，purpose=template
off       保留配置但不接受任何发送触发
~~~

- policy.mode 只控制 content_category=community 的生成方式。
- promotion_config.mode 独立控制 content_category=promotion 的生成方式。
- 因此同一账号可同时使用“AI 普通互动 + 模板群内广告”，二者不要求采用相同 mode。
- execution.mode_snapshot 按 content_category 选择对应 mode 后冻结。

### 6.2 触发类型

~~~text
scheduled  定时触发
keyword    关键词触发
reply      用户回复或语义回复触发
manual     管理员手动触发
~~~

### 6.3 内容类别

~~~text
community  普通群聊、问答、暖场和用户回复
promotion  仅在自建群内发送的推广广告
~~~

content_category 与 message_purpose 分工：

- message_purpose 表示内容生成方式：AI 为 community_ai，模板为 template。
- content_category 表示业务内容：普通消息为 community，群内广告为 promotion。
- promotion 不是 ad_delivery，不进入增长中心广告投放链。
- 同一个策略可以让不同触发器选择不同 content_category。

### 6.4 执行状态

| 状态 | 是否终态 | 含义 |
| --- | --- | --- |
| queued | 否 | 已创建，等待生成或渲染 |
| generating | 否 | Worker 已领取，正在生成或渲染 |
| pending_review | 否 | 内容已冻结，等待人工审核 |
| ready_to_send | 否 | 已通过审核或无需审核，等待发送 |
| sending | 否 | 已领取发送租约，正在调用 Telegram |
| sent | 是 | Telegram 返回 message_id |
| skipped | 是 | 被开关、额度、冷却、去重或资格门禁跳过 |
| failed | 是 | 达到最大重试次数或发生永久错误 |
| rejected | 是 | 人工审核拒绝 |
| expired | 是 | 待审核超过有效期 |
| cancelled | 是 | 策略停用前尚未发送的任务被取消 |

允许的状态迁移：

~~~text
queued → generating
generating → pending_review | ready_to_send | skipped | failed
pending_review → ready_to_send | rejected | expired | cancelled
ready_to_send → sending | skipped | cancelled
sending → sent | ready_to_send | failed
~~~

禁止从任何终态重新发送。需要重发时必须创建新的 execution_id 和 idempotency_key。

## 7. 按群按账号消息策略

### 7.1 策略唯一性

同一个 owned_group_asset_id + account_id 只能存在一条策略。第一版不做硬删除，停用使用 enabled=false；历史执行永久保持对 policy_id 的可追踪性。

### 7.2 策略字段语义

| 字段 | 必填 | 默认 | 规则 |
| --- | --- | --- | --- |
| owned_group_asset_id | 是 | 无 | API 路径提供 |
| core_group_id | 是 | resolver 得到 | 仅服务端写入 |
| account_id | 是 | 无 | 必须通过账号准入 |
| mode | 是 | off | community 的 ai / template / off |
| default_template_id | 条件必填 | null | community 且 mode=template 时必填 |
| trigger_config | 是 | v1 默认结构 | 见 7.3 |
| promotion_config | 是 | 安全默认结构 | 自建群推广广告独立内容配置 |
| daily_limit | 是 | 5 | 0–100；0 表示不允许发送 |
| cooldown_seconds | 是 | 3600 | 60–86400 |
| allowed_topics | 是 | [] | AI 自动触发至少 1 项 |
| require_review | 是 | true | false 仅 admin 可设置 |
| enabled | 是 | false | 启用时执行完整前置检查 |
| revision | 服务端 | 1 | 乐观并发版本 |

### 7.3 trigger_config v1

统一 JSON 结构：

~~~json
{
  "version": 1,
  "scheduled": {
    "enabled": false,
    "timezone": "Asia/Shanghai",
    "weekdays": [1, 2, 3, 4, 5, 6, 7],
    "times": [],
    "jitter_seconds": 0,
    "content_category": "community"
  },
  "keyword": {
    "enabled": false,
    "trigger_ids": [],
    "reply_to_source": true,
    "content_category": "community"
  },
  "reply": {
    "enabled": false,
    "strategy": "directed",
    "semantic_min_confidence": 0.75,
    "context_messages": 6,
    "content_category": "community"
  },
  "manual": {
    "enabled": true,
    "allowed_content_categories": ["community", "promotion"]
  },
  "dedupe_window_seconds": 21600
}
~~~

字段校验：

- version 第一版只能为 1。
- timezone 第一版固定为 Asia/Shanghai。
- weekdays 取值 1–7，1 表示周一；去重后升序保存。
- times 使用 HH:MM，最多 12 个，去重后升序保存。
- scheduled.enabled=true 时 times 不得为空。
- jitter_seconds 范围 0–900。
- scheduled、keyword、reply 的 content_category 只能为 community 或 promotion。
- content_category=promotion 时 promotion_config.mode 不能为 off。
- keyword.trigger_ids 最多 50 个，必须全部存在且 enabled=true。
- community 按 policy.mode 校验关键词动作；template 对应 reply_template，ai 对应 reply_ai。
- promotion 按 promotion_config.mode 校验关键词动作；template 对应 reply_template，ai 对应 reply_ai。
- reply.strategy 只能为 directed 或 semantic。
- 当前 content_category 对应 mode=template 时只允许 directed；semantic 仅允许对应 mode=ai。
- semantic_min_confidence 范围 0.50–1.00。
- context_messages 范围 1–20。
- manual.allowed_content_categories 为 community/promotion 的非空子集，去重后保存。
- dedupe_window_seconds 范围 600–86400。
- 四类触发全部关闭时允许保存，但 enabled=true 时不会产生消息。
- 第一版每种自动触发只绑定一个 content_category；常用组合为 scheduled=promotion、keyword/reply=community。若同一账号需要同类别之外的第二套定时规则，后续将 scheduled 扩展为 items[]，本阶段不做隐式多规则。

### 7.4 allowed_topics

- 最多 20 项。
- 每项去首尾空格后 1–50 个字符。
- 大小写不敏感去重。
- community mode=ai 或 promotion_config.mode=ai 开启 scheduled/semantic reply 时至少 1 项。
- 全局禁用主题和安全规则优先级高于 allowed_topics，策略不能覆盖。
- 阶段 2 不读取账号 Persona；AI 使用全局中性群聊提示词。

### 7.5 promotion_config

统一 JSON 结构：

~~~json
{
  "mode": "off",
  "default_template_id": null,
  "destination_url": null,
  "cta_text": null
}
~~~

固定规则：

- mode 只能为 ai、template 或 off；mode=off 时所有 content_category=promotion 的自动触发配置保存失败，手动 promotion 返回 OWNED_GROUP_PROMOTION_DISABLED。
- mode=template 时 default_template_id 必填，且模板 message_type 只能为 share 或 guide。
- mode=ai 时允许 AI 生成推广正文；destination_url 和 cta_text 由系统在生成后确定性追加，禁止让模型自行编造链接。
- destination_url 可空，表示纯文本群内推广；非空时必须为 HTTPS，最大 512 字符。
- promotion 内容中出现的 URL 必须与 destination_url 完全一致，禁止从增长中心 tracking link、AdCampaign 或 KeywordTrigger handler 自动生成链接。
- cta_text 可空，最大 100 字；非空时不能包含另一个 URL。
- promotion 强制人工审核，effective_require_review 恒为 true，即使策略 require_review=false。
- promotion_config 只属于当前 group + account 策略，不与增长中心广告素材、广告计划或投放设置同步。

### 7.6 策略更新

- PUT 必须携带当前 revision。
- revision 不一致返回 409 POLICY_REVISION_CONFLICT。
- 修改成功 revision+1。
- 修改策略不改变 pending_review 内容快照。
- enabled 从 true 改为 false 时，queued、pending_review、ready_to_send 全部改为 cancelled；sending 不强杀，但发送前门禁会再次读取 enabled。
- policy.mode 改为 off 只取消 community 的未发送执行；promotion_config.mode 不为 off 时 promotion 仍可运行。
- promotion_config.mode 改为 off 只取消 promotion 的未发送执行；community 不受影响。
- enabled=false 才取消该 policy 下两类全部未发送执行。
- 更新 account_id 不允许；需要换账号时停用旧策略并为新账号创建策略。

## 8. 四类触发详细需求

### 8.1 定时触发 scheduled

1. Celery Beat 每 60 秒调用调度器。
2. 调度器只扫描 enabled=true、scheduled.enabled=true，且 scheduled.content_category 对应 mode!=off 的策略。
3. 使用 Asia/Shanghai 计算本地日期、星期和时间槽。
4. idempotency_key 格式为 schedule:{policy_id}:{YYYYMMDDHHmm}。
5. 同一时间槽重复扫描只创建一条执行。
6. jitter_seconds 在首次创建时生成稳定随机偏移并写 scheduled_at，重启后不得重新随机。
7. Worker 只处理 scheduled_at<=now 的记录。
8. 策略在偏移等待期间被停用时，执行进入 cancelled。
9. content_category=community 的模板模式使用策略 default_template_id。
10. content_category=promotion 的模板模式使用 promotion_config.default_template_id。
11. AI community 从 allowed_topics 中轮换选题，最近 3 次已用主题排到末尾。
12. AI promotion 使用 allowed_topics 生成正文，并只追加 promotion_config 中的 CTA 和链接。

### 8.2 关键词触发 keyword

1. 推广账号事件 Worker 收到群消息后，先按 telegram_chat_id 解析自建群。
2. 忽略 Vanguard 管理的推广账号、Guardian Bot 和系统 Bot 自己发送的消息，防止循环。
3. 仅加载目标群中 keyword.enabled=true 的有效策略。
4. 使用现有 KeywordTrigger 匹配器处理大小写、正则、优先级和触发冷却。
5. 同一源消息若命中多个关键词，仅采用 priority 最高的一个；相同 priority 取 trigger_id 最小值。
6. 同一源消息若存在多个候选账号策略，只选择一个账号：
   - 先排除当前不可用账号。
   - 按该群 last_sent_at 最早优先。
   - 再按 policy_id 升序。
7. group 级 idempotency_key 为 keyword:{asset_id}:{source_message_id}:{trigger_id}。
8. reply_to_source=true 时发送必须带 reply_to_message_id=source_message_id。
9. KeywordTrigger 只复用匹配文本、动作类型、优先级和冷却；阶段 2 明确忽略 KeywordTrigger.template_id。
10. community 模板模式只使用策略 default_template_id，promotion 只使用 promotion_config.default_template_id；两者都必须是当前资产的 owned_group scope 模板。
11. AI 模式把命中关键词、原消息和最近上下文作为生成输入。
12. KeywordTrigger.requires_review、策略 require_review 和 promotion 强制审核使用逻辑 OR。

### 8.3 回复触发 reply

directed 策略：

- 原消息明确 reply 到该推广账号最近消息，或正文 @ 提及该账号时触发。
- 一条原消息最多选择一个推广账号策略。
- 默认 reply_to_message_id=source_message_id。
- community 模板模式使用策略 default_template_id。
- promotion 模板模式使用 promotion_config.default_template_id。
- ai 模式使用原消息和最多 context_messages 条上下文。

semantic 策略：

- 仅 ai 模式可用。
- 复用 SemanticGroupReplyEngine 的上下文窗口和语义判定。
- 先应用策略 allowed_topics，再应用 semantic_min_confidence。
- 全局 groupAiInteraction.enabled=false 时不生成执行。
- 同一 source_message_id 的语义回复只能预留一次。
- 语义引擎只负责“是否回复”和生成上下文，不得自行选择任意账号；账号由策略选择器确定。

回复事件 idempotency_key：

~~~text
reply:{asset_id}:{source_message_id}:{strategy}
~~~

### 8.4 手动触发 manual

- 只有 admin 可创建手动执行。
- manual.enabled=false 时接口返回 409 MANUAL_TRIGGER_DISABLED。
- 手动触发也必须经过资格、额度、冷却、内容安全、去重和风控。
- 手动触发不得提供 bypass_limit、force、skip_review 等字段。
- 可选 scheduled_at；为空时立即入队，最长只允许预约未来 7 天。
- 当前 content_category 对应 mode=ai 时可提供 instruction 和 topic；topic 必须属于 allowed_topics。
- 请求必须提供 content_category，且包含在 manual.allowed_content_categories。
- 当前 content_category 对应 mode=template 且为 community 时可提供 template_id 和 variables；不提供时使用策略 default_template_id。
- 当前 content_category 对应 mode=template 且为 promotion 时只允许 promotion_config.default_template_id，不接受任意广告模板 ID。
- content_category=promotion 时不接受请求体中的 destination_url 或 tracking_link，链接只能来自 promotion_config。
- 请求必须携带 Idempotency-Key，长度 8–128。

### 8.5 预览

- 预览不创建 execution，不消耗每日额度和冷却。
- 预览会调用模板渲染或 AI，因此 AI 预览消耗全局 Token 预算。
- 预览仍执行内容安全校验。
- 预览不得调用 Telegram。
- 预览响应返回 content_category、normalized_content、content_hash、warnings 和 would_require_review。
- 预览事件写 OwnedGroupAuditEvent，但不保存完整会话上下文。

## 9. 内容生成、模板和安全要求

### 9.1 AI 内容

生成输入按以下顺序组装：

~~~text
全局安全 system prompt
  → groupAiInteraction 的语气、长度和禁止规则
  → 自建群标题
  → policy.allowed_topics
  → trigger_type 和触发上下文
  → 最近群聊上下文
  → 管理员手动 instruction（如有）
~~~

固定要求：

- 输出仅一条中文群消息。
- 最终长度不得超过 min(groupAiInteraction.replyMaxChars, 500)。
- 不提及 AI、机器人、模型、系统提示词或自动化。
- 不虚构用户身份、交易结果、客服承诺或官方公告。
- 不使用阶段 3 Persona。
- AI 生成失败时不自动使用广告模板兜底。
- AI 无可用 LLM 时返回 AI_PROVIDER_UNAVAILABLE。

community 附加要求：

- 不包含注册链接、邀请链接、价格、折扣、购买引导或私聊导流。
- AI 输出中出现任何 URL 均由安全校验阻断。

promotion 附加要求：

- 仅允许在 promotion_config.mode!=off 时生成。
- 模型只生成推广正文，不向模型提供增长中心广告计划或 tracking link。
- destination_url 和 cta_text 由 ContentService 在模型输出通过安全检查后确定性追加。
- 最终内容中的 URL 只能等于 promotion_config.destination_url。
- promotion 强制进入 pending_review，不允许 AI 生成后直接发送。
- 生成、额度、冷却和统计只使用 owned_group_messaging 域，不调用广告 dispatcher。

### 9.2 模板内容

可用于阶段 2 的 MessageTemplate 必须：

- enabled=true。
- community 的 message_type 为 interaction 或 qa。
- promotion 的 message_type 为 share 或 guide，且只能使用 promotion_config.default_template_id。
- 内容通过阶段 2 安全校验。
- 不包含 register_link 变量。

阶段 2 模板变量白名单：

~~~text
{{group_name}}
{{account_name}}
{{user_name}}
{{current_date}}
{{current_time}}
{{promotion_url}}
{{promotion_cta}}
~~~

规则：

- scheduled 没有 user_name；模板使用该变量时保存或预览失败。
- community 模板不得使用 promotion_url 或 promotion_cta。
- promotion_url 和 promotion_cta 只从当前策略 promotion_config 注入，不调用增长中心追踪链接生成器。
- 未识别变量或渲染后仍有 {{...}} 占位符时失败。
- 禁止执行任意表达式、函数、过滤器或动态代码。
- user_name、group_name 和 account_name 在渲染前做纯文本转义。
- template_id 必须在生成时写入 execution 快照。

### 9.3 内容规范化与哈希

去重前执行：

1. Unicode NFKC 规范化。
2. 移除零宽字符。
3. 英文字母转小写。
4. 连续空白折叠为一个空格。
5. 去除首尾空白。
6. 连续标点按安全规则折叠。
7. 对规范化结果计算 SHA-256 十六进制 content_hash。

不得仅按原始字符串比较。

### 9.4 审核内容快照

- AI 生成或模板渲染后，将 content、content_hash、content_category、mode_snapshot、template_id、promotion_config_snapshot 和 policy_revision 写入 execution。
- pending_review 后内容不可被后台自动重新生成。
- 审核接口可传 content_override；传入后重新做长度、安全、模板残留和去重检查，并 revision+1。
- 审核通过保存 reviewer_id 和 reviewed_at。
- 待审核超过 24 小时转 expired。

## 10. 发送门禁、限额、冷却、去重与风控

### 10.1 发送前固定检查顺序

每次从 ready_to_send 进入 sending 前，按以下顺序执行：

1. OWNED_GROUP_MESSAGING_ENABLED 静态开关。
2. ownedGroupMessaging.enabled 运行开关。
3. ownedGroupMessaging.dryRun。
4. 资产状态、治理状态和双 ID 映射。
5. policy.enabled=true，且本次 content_category 对应的 policy.mode 或 promotion_config.mode 不为 off。
6. trigger_type 对应开关仍为 true。
7. content_category 合法；promotion 时 promotion_config.mode!=off。
8. 推广账号资格和群成员资格。
9. TelegramAccount 状态、risk_level、risk_pause_until、AccountOperationConfig.enabled 和指定账号会话可用性的只读预检；本步骤不得调用 AccountRiskGuard.check_and_reserve，不得预占任何风险额度。
10. 内容安全校验。
11. 全局每日群上限和账号上限。
12. 策略每日上限。
13. 群级冷却。
14. source idempotency。
15. 同群内容去重。
16. 原子领取发送租约。
17. OwnedGroupMessageExecutionService 调用 Speaker.send_owned_group_message；Speaker 仅调用一次 AccountPool.acquire_by_id 获取 policy.account_id 对应账号。
18. Speaker 将已获取的 account 对象传给 TelegramExecutionService.send_owned_group_message；TelegramExecutionService 不访问 AccountPool，并在 client.send_message 紧前唯一一次执行 AccountRiskGuard.check_and_reserve。
19. Telegram 发送。
20. 写 sent、AcquisitionMessage 和审计事件。

任何失败都必须写明确 reason_code。禁止更换账号、代理或重试来绕过第 4–15 项的业务门禁。

风险预算计数语义固定如下：

- OwnedGroupMessageExecutionService、Speaker 及其他调用方禁止自行调用 check_and_reserve；每次 execution 的唯一预占入口是 TelegramExecutionService.send_owned_group_message 内部的 _risk_operation。
- 因内容安全、阶段 2 额度、冷却、幂等、内容去重、审核拒绝或发送租约失败而未进入 Telegram 写调用的 execution，不增加 outbound_message 计数。
- 一旦 check_and_reserve 成功并开始调用 client.send_message，无论最终成功、明确失败或 TELEGRAM_SEND_OUTCOME_UNKNOWN，均保留这 1 次 outbound_message 预占，不回退安全计数。
- 同一个 Telegram 写尝试的 outbound_message 计数只能增加 1，禁止前置预占与底层发送双重预占。

### 10.2 每日额度

- 统计时区固定 Asia/Shanghai。
- policy.daily_limit 统计该 policy 当日 sent 数。
- 全局群上限统计同一 core_group_id 下所有阶段 2 sent 数。
- 全局账号上限统计同一 account_id 下所有阶段 2 sent 数。
- community 与 promotion 合并占用上述阶段 2 额度，并分别输出分类统计。
- 不读取增长中心广告日额度、AdCampaign 次数或 ad_delivery_log 计数。
- 生效额度取策略上限和全局硬上限中的最小值。
- pending_review、rejected、skipped、failed 不计入已发送额度。
- ready_to_send 不预扣额度，但发送前在同一数据库锁内复核，避免并发超发。
- daily_limit=0 时所有发送进入 skipped，reason_code=POLICY_DAILY_LIMIT_ZERO。

### 10.3 群级冷却

- 冷却对象是 core_group_id，不是 policy_id。
- 查询该群最近一条阶段 2 sent 的 sent_at。
- 生效冷却为 max(policy.cooldown_seconds, ownedGroupMessaging.minGroupCooldownSeconds)。
- 关键词、回复、定时和手动全部遵循冷却。
- community 与 promotion 共用阶段 2 群级冷却；不读取 growth_group_global_cooldown_seconds。
- 高优先级 Guardian 通知不占阶段 2 冷却。
- 冷却阻断后 execution 进入 skipped，不自动延后；新的事件或下一个时间槽可再次触发。

### 10.4 跨账号内容去重

- 去重对象为 core_group_id + content_hash。
- 时间窗为 max(policy.trigger_config.dedupe_window_seconds, 全局 contentDedupeWindowSeconds)。
- pending_review、ready_to_send、sending、sent 均占用内容。
- rejected、expired、cancelled、failed 不占用。
- 数据库执行表是最终判断依据，Redis 只作为加速缓存。
- 在资产行锁或 PostgreSQL advisory lock 内完成“查询重复 + 状态更新”。
- AI 内容重复时最多重新生成 2 次；仍重复则 skipped/DUPLICATE_CONTENT。
- 模板内容重复时直接 skipped/DUPLICATE_CONTENT。
- Redis 不可用时必须回退数据库判断，不得直接放行。

### 10.5 并发锁

- 每个 telegram_chat_id 使用现有 Telegram chat 事务锁或相同 advisory lock 规范。
- Worker 领取 execution 使用 SELECT FOR UPDATE SKIP LOCKED。
- sending 租约默认 120 秒。
- Worker 崩溃后，租约超时的 sending 可恢复为 ready_to_send，但必须先查询本地执行记录；不能盲目重复发送。
- 若 Telegram 调用超时且无法确认是否已发送，状态置 failed，error_code=TELEGRAM_SEND_OUTCOME_UNKNOWN，禁止自动重试。

### 10.6 风控失败

| 类型 | 处理 |
| --- | --- |
| FloodWait 且等待时间小于 15 分钟 | next_retry_at 后重试，最多 3 次 |
| FloodWait 大于等于 15 分钟 | failed，并由 AccountRiskGuard 暂停账号 |
| PeerFlood / account restricted / banned | failed，记录风险事件，禁用后续发送 |
| 网络超时且确认未提交 | 指数退避重试，最多 3 次 |
| 无发言权限 / 已离群 | failed，并将成员资格标记待 reconcile |
| 发送结果未知 | failed，不自动重试 |
| 内容或业务门禁失败 | skipped，不计技术失败 |

## 11. 数据模型需求

### 11.1 group_account_message_policy

建议 SQLAlchemy 字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| id | Integer | PK，自增 |
| owned_group_asset_id | Integer | FK owned_group_assets.id，ondelete=CASCADE，非空 |
| core_group_id | Integer | FK group.id，ondelete=RESTRICT，非空 |
| account_id | Integer | FK telegram_account.id，ondelete=RESTRICT，非空 |
| mode | String(16) | ai/template/off，默认 off |
| default_template_id | Integer nullable | FK acquisition_message_template.id，ondelete=RESTRICT |
| trigger_config | JSON | 非空，默认 v1 结构 |
| promotion_config | JSON | 非空，默认 disabled 安全结构 |
| daily_limit | Integer | 非空，默认 5 |
| cooldown_seconds | Integer | 非空，默认 3600 |
| allowed_topics | JSON | 非空，默认 [] |
| require_review | Boolean | 非空，默认 true |
| enabled | Boolean | 非空，默认 false |
| revision | Integer | 非空，默认 1 |
| created_by | Integer nullable | admin_user.id 逻辑关联 |
| updated_by | Integer nullable | admin_user.id 逻辑关联 |
| created_at | DateTime | 非空 |
| updated_at | DateTime | 非空 |

数据库约束和索引：

- UNIQUE(owned_group_asset_id, account_id)。
- CHECK mode IN ('ai','template','off')。
- CHECK daily_limit BETWEEN 0 AND 100。
- CHECK cooldown_seconds BETWEEN 60 AND 86400。
- CHECK revision >= 1。
- INDEX(core_group_id, enabled)。
- INDEX(account_id, enabled)。
- JSON 详细校验放 Pydantic 和服务层，数据库只保证非空。

### 11.2 group_account_message_execution

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | BigInteger | PK，自增 |
| policy_id | Integer | FK policy.id，ondelete=RESTRICT |
| owned_group_asset_id | Integer | 触发时资产快照 |
| core_group_id | Integer | 触发时内部群快照 |
| telegram_chat_id | BigInteger | 触发时 Telegram 目标快照 |
| account_id | Integer | 触发时指定账号快照 |
| trigger_type | String(16) | scheduled/keyword/reply/manual |
| message_purpose | String(32) | community_ai/template |
| content_category | String(16) | community/promotion |
| mode_snapshot | String(16) | ai/template |
| policy_revision | Integer | 触发时策略版本 |
| status | String(32) | 执行状态 |
| source_message_id | BigInteger nullable | 关键词/回复源消息 |
| reply_to_message_id | BigInteger nullable | Telegram 回复目标 |
| keyword_trigger_id | Integer nullable | FK acquisition_keyword_trigger.id |
| template_id | Integer nullable | FK acquisition_message_template.id |
| topic | String(100) nullable | AI 选题 |
| prompt_context | JSON nullable | 只保存脱敏后的结构化摘要 |
| promotion_config_snapshot | JSON nullable | promotion 使用的链接、CTA 和模板快照 |
| content | Text nullable | 最终待审/待发内容 |
| content_hash | String(64) nullable | 规范化内容 SHA-256 |
| idempotency_key | String(128) | 唯一 |
| correlation_id | String(128) | API、事件和日志关联 |
| scheduled_at | DateTime nullable | 计划执行 UTC 时间 |
| next_retry_at | DateTime nullable | 重试 UTC 时间 |
| lease_id | String(128) nullable | Worker 租约 |
| lease_expires_at | DateTime nullable | 租约到期 |
| attempt_count | Integer | 默认 0 |
| revision | Integer | 默认 1，审核并发控制 |
| requested_by | Integer nullable | 手动请求管理员 |
| reviewer_id | Integer nullable | 审核管理员 |
| reviewed_at | DateTime nullable | 审核时间 |
| telegram_message_id | BigInteger nullable | 成功发送返回值 |
| error_code | String(64) nullable | 机器可读原因 |
| error_message | Text nullable | 脱敏错误 |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |
| sent_at | DateTime nullable | 成功发送时间 |

约束和索引：

- UNIQUE(idempotency_key)。
- CHECK trigger_type、message_purpose、content_category、mode_snapshot、status 在枚举范围。
- CHECK attempt_count BETWEEN 0 AND 3。
- INDEX(status, scheduled_at, next_retry_at)。
- INDEX(policy_id, created_at)。
- INDEX(core_group_id, sent_at)。
- INDEX(core_group_id, content_category, sent_at)。
- INDEX(account_id, sent_at)。
- INDEX(core_group_id, content_hash, created_at)。
- INDEX(owned_group_asset_id, source_message_id)。

### 11.3 AcquisitionMessage 兼容扩展

增加可空字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| message_purpose | String(32) nullable | community_ai/template |
| content_category | String(16) nullable | community/promotion |
| owned_group_execution_id | BigInteger nullable unique | FK execution.id |
| core_group_id | Integer nullable | FK group.id |
| content_hash | String(64) nullable | 与 execution 一致 |

兼容规则：

- 现有 group_id 字段继续保存 Telegram Chat ID，不在本阶段重命名。
- 阶段 2 的 source of truth 是 execution，AcquisitionMessage 是成功发送兼容记录。
- community 的 message_type 使用 interaction 或 qa；promotion 使用 share 或 guide。
- 生成方式使用 message_purpose 区分，普通内容/群内广告使用 content_category 区分。
- 一个 sent execution 只能对应一个 AcquisitionMessage。
- content_category=promotion 仍不得写 ad_delivery_log。

### 11.4 MessageTemplate 所有权隔离

仅引用现有全局 acquisition_message_template 会导致增长中心停用或修改模板时影响自建群，因此必须为 MessageTemplate 增加作用域：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| scope | String(16) | acquisition/owned_group，非空，默认 acquisition |
| owned_group_asset_id | Integer nullable | FK owned_group_assets.id，ondelete=CASCADE |

数据库约束：

- scope=acquisition 时 owned_group_asset_id 必须为空。
- scope=owned_group 时 owned_group_asset_id 必须非空。
- INDEX(scope, owned_group_asset_id, enabled)。
- 现有全部模板迁移为 scope=acquisition、owned_group_asset_id=null。

服务约束：

- /api/acquisition/message-templates 只查询和修改 scope=acquisition；传入 owned_group 模板 ID 时返回 404。
- 阶段 2 只查询和修改 scope=owned_group 且 owned_group_asset_id=当前 asset_id 的模板。
- 旧 TemplateEngine.load_templates 和 get_random_template 默认强制 scope=acquisition，不得把 owned_group 模板装入增长运行缓存。
- 阶段 2 使用按 scope=owned_group + owned_group_asset_id 分区的查询和缓存键；不得选择 acquisition 模板。
- 缓存键至少包含 scope 和 asset_id，禁止两个作用域仅按 template_id/message_type 共用缓存。
- PolicyService 校验 default_template_id 和 promotion_config.default_template_id 均属于当前资产。
- KeywordTrigger.template_id 继续属于 acquisition 域，阶段 2 只复用 KeywordTrigger 的匹配规则并忽略该字段。
- 模板启用状态或正文更新只影响之后新创建的 execution；execution 已冻结的 content 不随模板变化。
- 不允许从增长中心模板隐式复制 register_link、tracking link 或广告计划变量。

### 11.5 审计表复用

复用 OwnedGroupAuditEvent，新增 event_type：

~~~text
message_policy_created
message_policy_updated
message_policy_enabled
message_policy_disabled
message_preview_generated
message_execution_created
message_review_approved
message_review_rejected
message_execution_sent
message_execution_skipped
message_execution_failed
~~~

审计 before_state 和 after_state 不保存完整群聊上下文、Session、Token、代理凭据或 LLM 密钥。

### 11.6 迁移文件

以当前分支迁移头为基线建议：

- backend/migrations/versions/034_add_owned_group_messaging.py
- backend/migrations/048_add_owned_group_messaging.sql

正式编码前必须重新检查 Alembic head；若阶段 1 或其他分支已占用编号，顺延文件名并将 down_revision 指向实际最新 head。

迁移顺序：

1. 只读检查重复 asset/account 组合和孤立 ID。
2. 新建 policy。
3. 新建 execution。
4. 为 MessageTemplate 增加 scope 和 owned_group_asset_id，历史模板回填 acquisition。
5. 为 AcquisitionMessage 增加可空兼容字段。
6. 增加约束和索引。
7. 不回填历史广告或暖场消息为阶段 2 消息。

## 12. 后端服务设计

### 12.1 建议模块

~~~text
backend/app/modules/owned_group/
├── messaging_contracts.py
├── messaging_models.py
├── messaging_schemas.py
├── messaging_target.py
├── messaging_policy_service.py
├── messaging_trigger_service.py
├── messaging_content_service.py
├── messaging_execution_service.py
├── messaging_event_router.py
└── messaging_worker.py

backend/app/api/
└── owned_group_messages.py
~~~

### 12.2 核心服务职责

OwnedGroupMessageTargetResolver：

- 只负责 asset、Group、Binding 双 ID 映射与状态校验。
- 返回 OwnedGroupMessageTarget。
- 不发消息、不修复冲突、不自动创建 Group。

OwnedGroupMessagePolicyService：

- 列出可选账号与不可选原因。
- 创建、更新、启用、停用策略。
- 规范化 trigger_config、promotion_config 和 allowed_topics。
- 进行 revision 乐观锁。
- 写策略审计。

OwnedGroupMessageTriggerService：

- 处理四种触发。
- 选择唯一策略账号。
- 生成 idempotency_key。
- 创建 queued execution。
- 不直接调用 Telegram。

OwnedGroupMessageContentService：

- 按 content_category 构造 AI prompt 和生成内容。
- 按 content_category 选择模板、安全渲染并追加策略内 CTA。
- 内容规范化、哈希、安全校验和重复检测。
- 不负责账号池和 Telegram。

OwnedGroupMessageExecutionService：

- 执行发送前完整门禁。
- 精确获取 policy.account_id。
- 不导入或调用 AdCampaign、广告 dispatcher、广告容量服务和广告投放设置读取函数。
- 调用 Speaker 新入口和 TelegramExecutionService。
- 原子更新执行状态、AcquisitionMessage 和审计。

### 12.3 Speaker 新入口

新增方法，旧 speak_in_group 不改签名：

~~~python
async def send_owned_group_message(
    self,
    *,
    target: OwnedGroupMessageTarget,
    account_id: int,
    content: str,
    purpose: Literal["community_ai", "template"],
    content_category: Literal["community", "promotion"],
    execution_id: int,
    reply_to_message_id: int | None = None,
) -> SpeakResult:
    ...
~~~

TelegramExecutionService 同时新增唯一底层入口：

~~~python
async def send_owned_group_message(
    self,
    account: Any,
    telegram_chat_id: int,
    message: str,
    *,
    reply_to: int | None = None,
    execution_id: int,
) -> int | None:
    ...
~~~

职责链固定为：

~~~text
OwnedGroupMessageExecutionService
  → Speaker.send_owned_group_message
      → AccountPool.acquire_by_id(account_id, purpose="owned_group_message")
      → TelegramExecutionService.send_owned_group_message(account, telegram_chat_id, ...)
          → _risk_operation(OWNED_GROUP_MESSAGE)
          → client.send_message(...)
~~~

固定行为：

- AccountPool 只由 Speaker 访问；Speaker 仅调用一次 acquire_by_id(account_id, purpose="owned_group_message")，禁止 fallback 到 acquire 或二次取号。
- TelegramExecutionService 接收 Speaker 已取得的 account 对象，不注入、不调用 AccountPool，也不按 account_id 重新获取账号。
- target.core_group_id 用于数据库与额度。
- target.telegram_chat_id 用于 Telegram。
- source 固定为 owned_group_message。
- community 与 promotion 均调用 TelegramExecutionService.send_owned_group_message，不调用 send_ad。
- AccountRiskGuard.check_and_reserve 只允许由 TelegramExecutionService.send_owned_group_message 内部的 _risk_operation 调用一次，位置在全部业务门禁、execution lease 和精确账号获取之后、client.send_message 紧前；Speaker 和 OwnedGroupMessageExecutionService 不得重复预占。
- reply_to_message_id 透传到底层 Telegram 发送。
- 不自动生成内容，不推断 message_type，不选择其他账号。
- 不在发送成功后 sleep；定时随机由 scheduled_at 的 jitter 处理。
- 发送成功由 ExecutionService 在同一业务事务中写兼容消息记录。

### 12.4 独立账号风控动作

现有 AccountRiskAction.GROUP_MESSAGE 会进入 acquisition 暖号上下文，AccountRiskAction.AD_DELIVERY 会进入广告域，两者都不能直接用于阶段 2。新增：

~~~python
class AccountRiskAction(str, Enum):
    OWNED_GROUP_MESSAGE = "owned_group_message"
~~~

实现要求：

1. 将 OWNED_GROUP_MESSAGE 加入 MESSAGE_ACTIONS，使其继续占用账号全局 outbound_message 硬上限。
2. 不将其加入 ACQUISITION_GROUP_WRITE_ACTIONS，禁止触发增长中心暖场、广告资格或广告失败副作用。
3. check 操作跳过 get_account_warmup_policy_settings、account_warmup_context 和所有广告 setting getter。
4. 在 DEFAULT_ACTION_BUDGETS 中显式新增 `AccountRiskAction.OWNED_GROUP_MESSAGE: RiskBudget(daily_limit=0, cooldown_seconds=0)`，禁止只加枚举而遗漏默认项。
5. `_budget_for_action` 对 OWNED_GROUP_MESSAGE 固定返回上述默认值，忽略 runtime `actions.owned_group_message` 的 daily_limit/cooldown_seconds 覆盖，避免增长中心运行设置重新影响阶段 2。
6. `_reserve_budget` 对 OWNED_GROUP_MESSAGE 的 action_limit 和 cooldown_seconds 固定为 0，不允许 `_adjust_budget_for_risk` 将 0 预算提升为 1，不写动作日计数键或动作冷却键；但因该动作属于 MESSAGE_ACTIONS，在唯一一次实际发送预占时仍必须检查并占用账号 outbound_message 全局硬上限。
7. Telegram 成功/失败仍写 account_risk_event，并处理 FloodWait、PeerFlood、account restricted、banned 和群发言权限。
8. 成功发送可调用通用 group-write capability reconcile，但不得修改 warmup_status、probe_status、ad_status 或 auto_ads_enabled。
9. TelegramExecutionService 新增 send_owned_group_message，内部固定使用 OWNED_GROUP_MESSAGE；禁止调用者通过 source 字符串推断成 GROUP_MESSAGE、AI_WARMUP 或 AD_DELIVERY。
10. 阶段 2 自有 Redis 键统一使用 owned_group:message: 前缀；账号通用出站硬上限继续使用 risk:account:{account_id}:daily:outbound_message:{date}。

该动作是账号安全域与业务配置域的分界：共享封禁和总出站安全，但不共享增长广告或暖号业务门禁。

### 12.5 事件路由所有权

收到 Telegram 群消息时：

1. 以 telegram_chat_id 查询 OwnedGroupAsset。
2. 若是自建群，无论是否存在活动策略，都标记 owned_group_event=true。
3. 自建群自动回复只允许进入 messaging_event_router。
4. 旧 acquisition keyword handler 和 SemanticGroupReplyEngine 不得并行再次发送。
5. 若功能开关关闭或无策略，只记录 debug/metric 后结束。
6. 非自建群继续走原有 acquisition 流程。

### 12.6 AI 全局设置复用

- ai 模式要求 groupAiInteraction.enabled=true。
- 复用 dailyTokenBudget、replyMaxChars、tone、temperature、maxTokens 和 blockAiSelfDisclosure。
- 不要求 allowProactiveWarmup=true；该字段只控制旧主动暖场。
- template 模式不依赖 AI 总开关。
- 旧 run_group_ai_warmup 必须排除存在 OwnedGroupAsset 的 Group，避免与阶段 2 重复。

### 12.7 增长中心隔离的代码依赖约束

为避免后续开发通过“复用广告服务”重新产生耦合，增加以下静态约束：

- messaging_policy_service.py、messaging_trigger_service.py、messaging_content_service.py、messaging_execution_service.py 和 messaging_worker.py 禁止 import AdCampaign、AccountAdBinding、AdCreative、AdDeliveryScheduleState、AdDeliveryLog、AdDeliveryPolicy、GroupAdProfile、GroupAdPolicyEvent、GroupAdOnlyAssessment、GroupAdHandover、GroupAdOnlyEvent 或广告投放 dispatcher。
- 阶段 2 服务禁止调用 get_ad_delivery_throttle_settings、get_ad_delivery_execution_settings、get_ad_capacity_settings、get_ad_failure_policy_settings、get_ad_only_recommendation_settings。
- 可共享 AccountRiskGuard、TelegramExecutionService、AccountPool 和账号级通用出站安全限制。
- 单元测试通过 monkeypatch 将全部增长广告设置设为相反值，阶段 2 的门禁结果必须保持不变。

旧增长广告链需要增加两层排除：

1. acquisition API 创建/更新 AccountAdBinding 或显式 target_group_ids 时，若目标 Group 对应未归档 OwnedGroupAsset，返回 409 OWNED_GROUP_AD_DOMAIN_EXCLUDED。
2. 广告 dispatcher 在解析最终 Telegram 目标后、创建调度状态或日志前再次检查 OwnedGroupAsset；命中时直接 skip，防止旧数据、并发或绕过 API 的写入产生双通道投放。

两层排除仅禁止增长广告链向自建群发送，不影响阶段 2 的 promotion。

并发要求：

- 旧广告 dispatcher、阶段 2 ExecutionService、OwnedGroupAsset 写入 telegram_chat_id/core_group_id 的流程必须使用同一个 telegram_chat_id advisory lock 规范。
- dispatcher 获取共享 chat lock 后在锁内重新解析 OwnedGroupAsset，并将锁保持到 Telegram 调用返回或明确放弃发送。
- 不允许在锁外检查“不属于自建群”后再调用 Telegram。
- 并发创建/接管自建群与旧广告发送时，只允许阶段 2 域获得后续发送权。

## 13. API 契约

所有接口前缀：

~~~text
/api/owned-groups/{asset_id}/messages
~~~

所有响应必须返回 request_id 或 correlation_id。成功响应使用 data；错误响应使用统一 error 对象。

### 13.1 查询可选推广账号

~~~http
GET /api/owned-groups/{asset_id}/messages/eligible-accounts
~~~

响应：

~~~json
{
  "data": [
    {
      "account_id": 21,
      "display_name": "运营号A",
      "status": "online",
      "risk_level": "normal",
      "operation_mode": "growth",
      "membership_status": "member_verified",
      "last_verified_at": "2026-09-10T02:10:00Z",
      "eligible": true,
      "blocking_reasons": [],
      "policy_id": 8
    }
  ],
  "asset": {
    "asset_id": 12,
    "core_group_id": 77,
    "telegram_chat_id": -1001234567890,
    "governance_status": "managed"
  }
}
~~~

列表必须同时返回不可选账号和 blocking_reasons，便于前端解释；不得只返回空列表。

### 13.2 查询策略列表

~~~http
GET /api/owned-groups/{asset_id}/messages/policies
~~~

可选参数：enabled、mode、account_id。

每条策略附加只读字段：

- account_display_name
- account_eligible
- account_blocking_reasons
- sent_today
- community_sent_today
- promotion_sent_today
- group_sent_today
- remaining_today
- last_sent_at
- cooldown_until
- pending_review_count

### 13.3 查询单个策略

~~~http
GET /api/owned-groups/{asset_id}/messages/policies/{policy_id}
~~~

asset_id 与 policy.owned_group_asset_id 不匹配时返回 404，不能跨资产读取。

### 13.4 创建策略

~~~http
POST /api/owned-groups/{asset_id}/messages/policies
~~~

请求：

~~~json
{
  "account_id": 21,
  "mode": "ai",
  "default_template_id": null,
  "trigger_config": {
    "version": 1,
    "scheduled": {
      "enabled": true,
      "timezone": "Asia/Shanghai",
      "weekdays": [1, 2, 3, 4, 5],
      "times": ["10:30", "18:30"],
      "jitter_seconds": 300,
      "content_category": "community"
    },
    "keyword": {
      "enabled": true,
      "trigger_ids": [4, 9],
      "reply_to_source": true,
      "content_category": "community"
    },
    "reply": {
      "enabled": true,
      "strategy": "semantic",
      "semantic_min_confidence": 0.78,
      "context_messages": 6,
      "content_category": "community"
    },
    "manual": {
      "enabled": true,
      "allowed_content_categories": ["community", "promotion"]
    },
    "dedupe_window_seconds": 21600
  },
  "promotion_config": {
    "mode": "ai",
    "default_template_id": null,
    "destination_url": "https://example.com/product",
    "cta_text": "需要时可以查看详情"
  },
  "daily_limit": 5,
  "cooldown_seconds": 3600,
  "allowed_topics": ["节点使用体验", "客户端设置"],
  "require_review": true,
  "enabled": false
}
~~~

返回 201。重复 asset/account 返回 409 POLICY_ALREADY_EXISTS，并返回现有 policy_id。

### 13.5 修改策略

~~~http
PUT /api/owned-groups/{asset_id}/messages/policies/{policy_id}
~~~

请求为完整替换，必须包含 revision：

~~~json
{
  "revision": 3,
  "mode": "template",
  "default_template_id": 15,
  "trigger_config": {
    "version": 1,
    "scheduled": {
      "enabled": false,
      "timezone": "Asia/Shanghai",
      "weekdays": [1, 2, 3, 4, 5, 6, 7],
      "times": [],
      "jitter_seconds": 0,
      "content_category": "community"
    },
    "keyword": {
      "enabled": true,
      "trigger_ids": [10],
      "reply_to_source": true,
      "content_category": "community"
    },
    "reply": {
      "enabled": true,
      "strategy": "directed",
      "semantic_min_confidence": 0.75,
      "context_messages": 6,
      "content_category": "community"
    },
    "manual": {
      "enabled": true,
      "allowed_content_categories": ["community", "promotion"]
    },
    "dedupe_window_seconds": 21600
  },
  "promotion_config": {
    "mode": "template",
    "default_template_id": 18,
    "destination_url": "https://example.com/product",
    "cta_text": "查看活动详情"
  },
  "daily_limit": 4,
  "cooldown_seconds": 3600,
  "allowed_topics": [],
  "require_review": true,
  "enabled": true
}
~~~

返回更新后的 revision 和所有派生状态。第一版不提供 PATCH，避免 trigger_config 局部合并语义不一致。

### 13.6 生成预览

~~~http
POST /api/owned-groups/{asset_id}/messages/policies/{policy_id}/preview
~~~

请求：

~~~json
{
  "trigger_type": "manual",
  "content_category": "promotion",
  "topic": "客户端设置",
  "instruction": "自然介绍新客户端功能，不夸大",
  "template_id": null,
  "variables": {}
}
~~~

响应：

~~~json
{
  "data": {
    "content": "大家最近都在用哪个客户端？稳定性怎么样？",
    "normalized_content": "大家最近都在用哪个客户端?稳定性怎么样?",
    "content_hash": "sha256...",
    "message_purpose": "community_ai",
    "content_category": "promotion",
    "warnings": [],
    "would_require_review": true
  }
}
~~~

### 13.7 创建手动执行

~~~http
POST /api/owned-groups/{asset_id}/messages/policies/{policy_id}/executions
Idempotency-Key: 8-128 characters
~~~

请求：

~~~json
{
  "trigger_type": "manual",
  "content_category": "promotion",
  "topic": "客户端设置",
  "instruction": "自然介绍新功能，不夸大效果",
  "template_id": null,
  "variables": {},
  "reply_to_message_id": null,
  "scheduled_at": null
}
~~~

返回 202，data 至少包含 execution_id、status、correlation_id、scheduled_at。相同 Idempotency-Key 和相同请求体返回同一执行；相同 key 不同请求体返回 409 IDEMPOTENCY_KEY_REUSED。

### 13.8 查询执行列表

~~~http
GET /api/owned-groups/{asset_id}/messages/executions
~~~

查询参数：

- policy_id
- account_id
- trigger_type
- content_category
- status
- created_from
- created_to
- page
- page_size，默认 20，最大 100

响应需返回 total、items，并包含内容摘要、错误码、审核人、Telegram message ID 和时间线。

### 13.9 查询执行详情

~~~http
GET /api/owned-groups/{asset_id}/messages/executions/{execution_id}
~~~

详情包括：

- 策略快照
- 目标双 ID
- 触发来源
- 内容与 content_hash
- 状态迁移时间
- 审核信息
- 重试次数
- error_code 和脱敏 error_message
- 关联 audit_event_ids

### 13.10 审核通过

~~~http
POST /api/owned-groups/{asset_id}/messages/executions/{execution_id}/approve
~~~

请求：

~~~json
{
  "revision": 1,
  "content_override": null
}
~~~

仅 pending_review 可调用。成功后状态为 ready_to_send，返回 200。

### 13.11 审核拒绝

~~~http
POST /api/owned-groups/{asset_id}/messages/executions/{execution_id}/reject
~~~

请求：

~~~json
{
  "revision": 1,
  "reason": "内容不适合当前群话题"
}
~~~

reason 必填，1–500 字。成功后状态为 rejected。

### 13.12 自建群模板接口

~~~http
GET  /api/owned-groups/{asset_id}/messages/templates
POST /api/owned-groups/{asset_id}/messages/templates
PUT  /api/owned-groups/{asset_id}/messages/templates/{template_id}
~~~

GET 可按 message_type、content_category、enabled 查询，只返回 scope=owned_group 且属于当前 asset_id 的模板。

GET/POST/PUT 的模板响应至少包含：

~~~json
{
  "id": 31,
  "name": "群内活动介绍",
  "content": "本周的新功能已经开放，{{promotion_cta}}\n{{promotion_url}}",
  "message_type": "guide",
  "content_category": "promotion",
  "template_variables": ["promotion_cta", "promotion_url"],
  "enabled": true,
  "created_at": "2026-09-10T10:00:00Z",
  "updated_at": "2026-09-10T10:00:00Z"
}
~~~

其中 content_category 由 message_type 映射后返回，不单独落库；POST 成功返回 201，PUT 成功返回 200。

POST/PUT 请求：

~~~json
{
  "name": "群内活动介绍",
  "content": "本周的新功能已经开放，{{promotion_cta}}\n{{promotion_url}}",
  "message_type": "guide",
  "template_variables": ["promotion_cta", "promotion_url"],
  "enabled": true
}
~~~

规则：

- 服务端强制写 scope=owned_group 和当前 owned_group_asset_id，请求体不得传 scope 或其他 asset_id。
- interaction/qa 作为 community 模板；share/guide 作为 promotion 模板。
- 复用 TemplateEngine 渲染，但变量白名单和 URL 安全由 OwnedGroupMessageContentService 检查。
- 被 policy 引用的模板不能删除，第一版不提供 DELETE；可 enabled=false，但启用策略和新 execution 会被阻断。
- acquisition 的模板接口无法读取、修改或停用这些模板。

### 13.13 统一错误响应

~~~json
{
  "error": {
    "code": "POLICY_DAILY_LIMIT_REACHED",
    "message": "该账号在此群的今日发送额度已用完",
    "details": {
      "policy_id": 8,
      "daily_limit": 5,
      "sent_today": 5
    },
    "retryable": false
  },
  "correlation_id": "msg-..."
}
~~~

主要错误码：

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | INVALID_TRIGGER_CONFIG | 触发配置格式或组合非法 |
| 400 | INVALID_PROMOTION_CONFIG | 群内广告配置非法 |
| 400 | PROMOTION_URL_INVALID | 推广链接不是策略允许的 HTTPS 地址 |
| 400 | PROMOTION_TEMPLATE_REQUIRED | promotion mode=template 但未配置当前资产模板 |
| 400 | INVALID_TEMPLATE_VARIABLE | 模板变量不支持或缺失 |
| 400 | CONTENT_SAFETY_BLOCKED | 内容命中安全规则 |
| 403 | ADMIN_REQUIRED | 非管理员调用写接口 |
| 404 | OWNED_GROUP_NOT_FOUND | 自建群不存在 |
| 404 | POLICY_NOT_FOUND | 策略不存在或不属于资产 |
| 404 | EXECUTION_NOT_FOUND | 执行不存在或不属于资产 |
| 404 | OWNED_GROUP_TEMPLATE_NOT_FOUND | 模板不存在、作用域不符或不属于资产 |
| 409 | TARGET_MAPPING_INVALID | 双 ID 或 Binding 不一致 |
| 409 | OWNED_GROUP_AD_DOMAIN_EXCLUDED | 增长广告写接口命中自建群；dispatcher 使用相同值作为 skip reason |
| 409 | GOVERNANCE_NOT_MANAGED | 群未处于 managed |
| 409 | POLICY_ALREADY_EXISTS | 同群同账号策略已存在 |
| 409 | POLICY_REVISION_CONFLICT | 策略版本冲突 |
| 409 | EXECUTION_REVISION_CONFLICT | 审核版本冲突 |
| 409 | ACCOUNT_NOT_ELIGIBLE | 账号不符合准入 |
| 409 | ACCOUNT_MODE_NOT_ALLOWED | 账号为 ad_only |
| 409 | MANUAL_TRIGGER_DISABLED | 手动触发关闭 |
| 409 | OWNED_GROUP_PROMOTION_DISABLED | 策略未允许群内广告 |
| 409 | REVIEW_STATUS_INVALID | 当前状态不可审核 |
| 409 | IDEMPOTENCY_KEY_REUSED | 幂等键请求体冲突 |
| 422 | AI_PROVIDER_UNAVAILABLE | AI 模式无可用模型 |
| 429 | POLICY_DAILY_LIMIT_REACHED | 策略每日额度耗尽 |
| 429 | GROUP_COOLDOWN_ACTIVE | 群级冷却中 |

自动触发被业务门禁阻断时不返回 HTTP，而是在 execution 写 skipped 和相同 error_code。

## 14. Worker、调度与一致性

### 14.1 Celery 配置

新增 Beat：

~~~python
"owned-group-message-dispatch-every-minute": {
    "task": "app.modules.owned_group.messaging_tasks.dispatch_owned_group_messages",
    "schedule": 60.0,
    "kwargs": {"limit": 50},
    "options": {"queue": "owned_group"}
}
~~~

新增 task route，并复用 owned_group 队列。第一版不新增独立容器。

### 14.2 每次 tick

1. 使超过 24 小时的 pending_review 进入 expired。
2. 为到点定时策略创建幂等 execution。
3. 领取 queued，生成或渲染内容。
4. 处理无需审核的 ready_to_send。
5. 处理已审核和到达 next_retry_at 的 ready_to_send。
6. 回收 lease_expires_at 已过期且结果可确认未发送的 sending。
7. 输出 processed、sent、skipped、failed、pending_review 指标。

### 14.3 事务边界

- 创建 execution 与 idempotency_key 唯一约束在同一事务。
- 内容去重检查和占用状态写入在同一群锁内。
- 发送 Telegram 前提交 sending 和租约，避免长事务持锁。
- Telegram 成功后以 execution_id 条件更新 sending→sent。
- 写 sent、AcquisitionMessage 和审计尽量同事务。
- 若 Telegram 已成功但数据库提交失败，使用 telegram_message_id 和 correlation_id 做人工 reconcile；不得自动重复发。

### 14.4 重试

- 最大 3 次，退避 60 秒、300 秒、900 秒。
- 永久业务错误不重试。
- Telegram 结果未知不重试。
- 策略停用、群降级或账号资格失效时直接 cancelled/skipped。
- 重试始终使用原 account_id、content 和 content_hash，不重新选号、不重新生成。

## 15. 功能开关与停止语义

### 15.1 静态开关

新增环境变量：

~~~text
OWNED_GROUP_MESSAGING_ENABLED=false
~~~

默认 false。关闭时：

- 查询和预览接口可用。
- 策略可保存但不能启用。
- 自动触发不创建新执行。
- ready_to_send 不调用 Telegram。
- 已发送历史不修改。

### 15.2 运行设置

在 AppRuntimeSettings 新增：

~~~json
{
  "ownedGroupMessaging": {
    "enabled": false,
    "dryRun": true,
    "globalMaxPerGroupPerDay": 20,
    "globalMaxPerAccountPerDay": 30,
    "minGroupCooldownSeconds": 300,
    "contentDedupeWindowSeconds": 21600,
    "reviewTtlHours": 24,
    "maxSendAttempts": 3
  }
}
~~~

范围：

- globalMaxPerGroupPerDay：0–1000。
- globalMaxPerAccountPerDay：0–1000。
- minGroupCooldownSeconds：60–86400。
- contentDedupeWindowSeconds：600–86400。
- reviewTtlHours：1–168。
- maxSendAttempts 第一版固定 1–3。

dryRun=true 时允许生成、渲染和审核，但 ready_to_send 在最终门禁变为 skipped，error_code=DRY_RUN_ENABLED，绝不调用 Telegram。

### 15.3 与三条主停止链的关系

- provisioning_stop 不阻断已存在群的阶段 2 消息。
- governance_stop 只阻断 Guardian 动作；阶段 2 仍依据资产当前 governance_status 判断。
- acquisition_stop 不阻断自建群内部消息，也不能用于控制阶段 2。
- 增长中心所有广告运行设置、广告计划状态和 auto_ads_enabled 均不影响阶段 2 的 community 或 promotion。
- 共享账号风险状态和账号级全局出站硬上限仍然生效；“不受广告配置影响”不等于绕过账号安全。
- 阶段 2 紧急停止只使用 OWNED_GROUP_MESSAGING_ENABLED 或 ownedGroupMessaging.enabled。
- 任何开关关闭都不能通过手动接口、换账号、换代理或重试绕过。

## 16. 前端需求

### 16.1 页面与路由

阶段 1 的 OwnedGroups.vue 已较复杂，阶段 2 新建独立页面：

~~~text
路由：/owned-groups/:assetId/messaging
页面：frontend/src/views/OwnedGroupMessaging.vue
入口：OwnedGroups.vue 每行“群内消息”按钮
API：frontend/src/api/ownedGroupMessaging.ts
Store：frontend/src/stores/ownedGroupMessaging.ts
~~~

按钮规则：

- status=ready 且 core_group_id 非空时可进入。
- 未接入治理时可进入查看和预配置，但顶部显示“治理未就绪，不能启用或发送”。
- archived 资产按钮禁用。

### 16.2 页面布局

顶部资产摘要：

- 群名称
- Telegram Chat ID
- core_group_id
- governance_status
- 功能开关状态
- 今日群发送数 / 全局群上限
- 今日普通消息数 / 群内广告数
- 最近发送时间

Tab 1“账号策略”：

- 账号名称、在线状态、风险等级、群成员状态
- mode
- 已开启触发及各触发的 content_category
- 今日已发 / 策略上限
- 冷却至
- 待审核数
- enabled
- 编辑、预览、手动触发、查看历史

Tab 2“审核队列”：

- 按 created_at 升序
- 展示群、账号、触发来源、内容、主题、创建时间、过期倒计时
- 通过、修改后通过、拒绝
- 操作前显示 revision 冲突提示

Tab 3“执行历史”：

- 支持账号、触发类型、状态、时间筛选
- 状态时间线
- error_code 和可读原因
- sent 状态显示 Telegram message_id

Tab 4“群内模板”：

- 只调用当前 asset_id 下的阶段 2 模板 API。
- 支持 content_category、message_type、enabled 筛选。
- 展示名称、普通消息/群内广告类别、message_type、内容摘要、变量、启用状态和更新时间。
- 支持新建、编辑、启用、停用和内容预览；第一版不显示删除入口。
- 保存或停用后刷新当前资产模板列表及策略抽屉的模板下拉框。

### 16.3 群内模板管理

模板抽屉字段与校验：

1. 名称必填，去除首尾空白后长度 1–100。
2. 内容必填，长度 1–5000；实时高亮 `{{variable}}` 并拒绝未闭合占位符。
3. 内容类别必填：community 或 promotion。类别不作为请求字段保存，只用于限定 message_type。
4. community 只可选择 interaction/qa；promotion 只可选择 share/guide。
5. template_variables 由正文占位符去重提取，按第 9.2 节白名单校验后作为字符串数组提交，禁止用户另填不在正文中的变量。
6. enabled 默认 true。

交互与隔离：

- 前端请求体不得携带 scope 或 owned_group_asset_id，二者由服务端根据路径强制写入。
- 切换 assetId 时必须清空模板列表、详情和下拉缓存，再加载新资产数据，禁止短暂展示上一资产模板。
- community 模板使用 promotion_url/promotion_cta、任意模板使用 register_link 或未知变量时，提交前阻止并展示具体变量。
- 停用模板前提示“已引用该模板的策略不会被自动改写，但之后新执行将被阻断”；不提供跨资产复制。
- 策略抽屉只列当前资产、enabled=true 且类别匹配的模板，已停用但被当前策略引用的模板以“已停用”只读显示，保存时要求重新选择或关闭对应 mode。
- 增长中心模板页面、API 和 Store 不得复用或合并这份列表。

### 16.4 策略抽屉

字段：

1. 推广账号：创建后不可改。
2. 模式：AI、模板、关闭。
3. 普通消息默认模板：template 模式必填，只列 interaction/qa 且 enabled 模板。
4. 定时触发：星期、时间点、随机延迟、普通消息/群内广告类别。
5. 关键词触发：多选兼容动作的 KeywordTrigger、普通消息/群内广告类别。
6. 回复触发：directed；AI 模式额外可选 semantic，并选择内容类别。
7. 手动触发开关及允许的内容类别。
8. 群内广告模式：AI、模板、关闭，与普通消息 mode 独立。
9. 群内广告默认模板：只列 share/guide 且 enabled 模板。
10. 群内广告链接和 CTA：只保存在当前策略。
11. 每日上限。
12. 群级冷却。
13. 允许主题。
14. 人工审核。
15. 启用状态。

交互规则：

- require_review 默认打开。
- 非 admin 只读。
- account 不可选时显示全部 blocking_reasons。
- 普通消息 mode=off 时折叠普通消息相关配置但不清空；promotion mode 独立显示。
- AI 自动触发且主题为空时前端阻止提交。
- template 模式选择 AI keyword trigger 时前端阻止提交。
- 任一触发选择 promotion 但 promotion_config.mode=off 时阻止提交。
- promotion 模板列表不得加载增长中心 AdCreative 或广告计划素材。
- 页面不读取或展示 auto_ads_enabled、AdCampaign、外部广告额度、广告冷却和 acquisition_stop 作为可发送条件。
- promotion 始终显示“强制审核”，不提供关闭按钮。
- 保存后以服务端规范化结果覆盖本地表单。
- 关闭抽屉前有未保存修改时二次确认。

### 16.5 预览与手动触发

- 预览结果显示消息目的、字符数、安全警告和是否需要审核。
- 预览和执行历史显示 community/普通消息或 promotion/群内广告标签。
- 预览按钮不得显示“已发送”。
- 手动触发成功只提示“已进入队列/待审核”，不得在收到 202 时提示发送成功。
- 只有 execution.status=sent 才显示“发送成功”。
- API 429 展示剩余额度或 cooldown_until。

### 16.6 状态文案

| 后端状态 | 前端文案 |
| --- | --- |
| queued | 排队中 |
| generating | 生成中 |
| pending_review | 待审核 |
| ready_to_send | 待发送 |
| sending | 发送中 |
| sent | 已发送 |
| skipped | 已跳过 |
| failed | 失败 |
| rejected | 已拒绝 |
| expired | 已过期 |
| cancelled | 已取消 |

## 17. 可观测性与数据保留

### 17.1 结构化日志

每条日志至少包含：

~~~text
correlation_id
execution_id
policy_id
owned_group_asset_id
core_group_id
telegram_chat_id
account_id
trigger_type
message_purpose
content_category
status
error_code
attempt_count
~~~

不得记录：

- Telegram session_string 或 auth_key。
- Bot Token、API Hash、代理密码。
- LLM API Key。
- 未脱敏的完整历史会话。

### 17.2 指标

建议新增：

~~~text
owned_group_message_execution_total{trigger,mode,content_category,status,error_code}
owned_group_message_sent_total{trigger,purpose,content_category}
owned_group_message_review_pending
owned_group_message_generation_seconds
owned_group_message_send_seconds
owned_group_message_deduplicated_total
owned_group_message_gate_block_total{gate}
owned_group_message_growth_ad_config_access_total
~~~

owned_group_message_growth_ad_config_access_total 正常值必须恒为 0；若阶段 2 服务读取增长中心广告设置，应视为隔离回归并告警。

### 17.3 数据保留

- policy 永久保留，停用不删除。
- execution 完整内容保留 90 天。
- 90 天后 content 和 prompt_context 可清空，但状态、哈希、错误码、时间和 Telegram message_id 保留 365 天。
- OwnedGroupAuditEvent 按现有审计保留策略执行。
- 清理任务必须分批，不能删除仍处于非终态的执行。

## 18. 验收用例

### 18.1 策略与资格

1. READY、managed 自建群可为 member_verified 的 growth promoter 创建 disabled 策略。
2. 同一群同一账号重复创建返回 POLICY_ALREADY_EXISTS。
3. ad_only 账号不能启用策略。
4. guardian_bot 不能创建阶段 2 策略。
5. 已离群、error、banned、limited、frozen、quarantined 账号不能启用。
6. 成员核验超过 24 小时时先探针，探针失败不启用。
7. 双 ID 或 Binding 不一致时不能启用。
8. revision 过期时更新返回 409，不覆盖新配置。
9. 停用策略会取消所有未发送执行。

### 18.2 定时触发

10. 到达配置时间后创建唯一 execution。
11. 同一分钟重复 tick 不重复创建。
12. 星期不匹配时不创建。
13. jitter 写入后 Worker 重启不改变 scheduled_at。
14. content_category 对应 mode=template 时使用该类别的默认模板。
15. content_category 对应 mode=ai 时生成内容且 purpose=community_ai。

### 18.3 关键词和回复

16. 同消息命中多个关键词只选最高优先级。
17. 同消息多个账号策略只选择一个账号。
18. 重复投递同一 Telegram update 不重复创建 execution。
19. reply_to_source=true 时发送带正确 source_message_id。
20. directed 只对回复或提及指定账号触发。
21. template+semantic 配置被 API 拒绝。
22. 自建群事件不会再被旧关键词/语义链重复处理。
23. 管理账号自己发出的消息不会触发回复循环。

### 18.4 审核

24. require_review=true 的执行生成后停在 pending_review。
25. 未审核时 Worker 多次 tick 均不调用 Telegram。
26. 审核通过后进入 ready_to_send。
27. 修改后通过会重算哈希并重新过安全和去重。
28. 审核拒绝后不能再次批准。
29. 超过 24 小时自动 expired。
30. 两名管理员并发审核只有正确 revision 成功。

### 18.5 额度、冷却与去重

31. policy.daily_limit 达到后新执行 skipped。
32. 全局群上限低于策略上限时按全局上限阻断。
33. 全局账号上限达到后该账号在其他自建群也阻断。
34. 同群不同账号在冷却期内不能发送。
35. 同群相同模板内容在去重窗口内只发送一条。
36. 规范化前仅大小写、空白或零宽字符不同仍判重复。
37. AI 重复最多重生成 2 次，仍重复则 skipped。
38. Redis 不可用时数据库去重仍生效。
39. 并发两 Worker 领取同 execution 只发送一次。

### 18.6 开关、风控和失败

40. 静态开关关闭时不调用 Telegram。
41. dryRun=true 时能预览和生成，但发送执行为 DRY_RUN_ENABLED。
42. groupAiInteraction.enabled=false 只阻断 AI，不阻断合规模板。
43. acquisition_stop 不影响阶段 2。
44. governance_status 变为 degraded 后最终门禁阻断发送。
45. 指定账号不可用时失败，不换号。
46. FloodWait 按规则退避且最多 3 次。
47. Telegram 结果未知时不自动重试。
48. 无发言权限时失败并标记成员资格待 reconcile。

### 18.7 兼容与统计

49. sent execution 生成唯一 AcquisitionMessage 兼容记录。
50. message_purpose 正确区分 community_ai 和 template，content_category 正确区分 community 和 promotion。
51. community 和 promotion 均不写 ad_delivery_log。
52. 阶段 2 不增加 interaction_sent_today。
53. 旧外部群暖场不再选择 OwnedGroupAsset 对应群。
54. 旧非自建群关键词、语义回复和广告投放行为不变。
55. 前端收到 202 只显示排队或待审核，不误报发送成功。
56. 所有 skipped/failed 都能从执行详情看到机器错误码。

### 18.8 自建群广告与增长中心隔离

57. auto_ads_enabled=false 时，符合阶段 2 策略的 community 消息仍可发送。
58. auto_ads_enabled=false 时，符合阶段 2 策略的 promotion 群内广告仍可发送。
59. automation.ad_delivery_throttle.enabled=false 不影响阶段 2。
60. automation.ad_delivery_execution.enabled=false 不影响阶段 2。
61. automation.ad_capacity.enabled=false 或广告时间窗关闭不影响阶段 2。
62. acquisition_stop=true 不影响阶段 2 的 community 和 promotion。
63. 不存在 AdCampaign、AccountAdBinding 或 AdCreative 时，阶段 2 promotion 仍可按独立模板发送。
64. 外部广告额度耗尽、群级广告冷却中或广告资格未通过时，不阻断阶段 2。
65. GroupAccountMembership.warmup_status、probe_status、ad_status 变化不改变阶段 2 准入结果。
66. promotion 执行只写 group_account_message_execution 和 AcquisitionMessage，不写 ad_delivery_log。
67. promotion 的链接只来自 policy.promotion_config，不调用增长中心 tracking link 生成器。
68. 增长中心广告配置任意变更前后，相同阶段 2 输入得到相同门禁结果。
69. 账号 risk_level、risk_pause_until 或账号级全局消息硬上限仍可阻断发送。
70. promotion 未审核不能发送，即使增长中心广告配置允许自动投放。
71. 旧增长广告计划显式命中 OwnedGroupAsset 时，dispatcher 以 OWNED_GROUP_AD_DOMAIN_EXCLUDED 跳过。
72. 上述跳过不创建 AdDeliveryScheduleState、AdDeliveryLog、tracking link 或存活探测任务。
73. 增长中心模板接口不能查询、修改或停用 owned_group scope 模板。
74. Fake RiskGuard 证明阶段 2 只使用 OWNED_GROUP_MESSAGE，不使用 GROUP_MESSAGE、AI_WARMUP 或 AD_DELIVERY。
75. 同一 policy 可配置 community mode=ai、promotion_config.mode=template，并分别正确生成和发送。
76. policy.enabled=true、policy.mode=off、promotion_config.mode=template 或 ai 时，promotion 可继续生成和发送，community 被拒绝。
77. policy.enabled=true、promotion_config.mode=off、policy.mode=ai 或 template 时，community 可继续生成和发送，promotion 被拒绝。
78. policy.mode 从 ai/template 改为 off 时，只取消 content_category=community 的 queued、pending_review、ready_to_send 执行，不取消 promotion。
79. promotion_config.mode 从 ai/template 改为 off 时，只取消 content_category=promotion 的 queued、pending_review、ready_to_send 执行，不取消 community。
80. 增长广告 dispatcher 与 OwnedGroupAsset 建立或映射更新并发发生时，共享 telegram_chat_id advisory lock 内的最终复核可阻止增长广告发送，且不创建广告调度状态、日志或 tracking link。
81. 旧 acquisition TemplateEngine.load_templates/get_random_template 永不加载或返回 scope=owned_group 的模板。
82. 阶段 2 模板查询、随机选择和按 ID 渲染均不返回 scope=acquisition 的模板，即使 template_id 或 message_type 相同。
83. 两个 OwnedGroupAsset 使用相同 message_type 或同名模板时，缓存按 scope+asset_id 隔离，切换资产、更新或停用不会串读。
84. 管理员可在“群内模板”Tab 新建、编辑、启用和停用模板；策略下拉只出现当前资产的匹配模板，增长中心模板页面不可见。
85. runtime actions.owned_group_message 配置任意 daily_limit/cooldown_seconds 均不能改变固定 0/0；发送后不写 risk:account:{account_id}:daily:owned_group_message:* 或 cooldown:owned_group_message 键。
86. 内容安全、阶段 2 额度、群级冷却、source idempotency、内容去重、审核拒绝或发送租约任一处拦截时，不调用 Telegram，账号 outbound_message 计数保持不变。
87. 每次真正发起 client.send_message 只使 outbound_message 增加 1，不发生前置与底层双预占；发送成功、明确失败和 TELEGRAM_SEND_OUTCOME_UNKNOWN 三种结果均保留该 1 次计数。
88. 一次执行中只有 Speaker 调用一次 AccountPool.acquire_by_id，且将同一 account 对象传给 TelegramExecutionService；TelegramExecutionService 不访问 AccountPool，不发生双 acquire，也不 fallback 换号。

## 19. 开发任务拆分

| 任务 | 内容 | 建议工时 | 依赖 |
| --- | --- | --- | --- |
| P2-BE-01 | 枚举、Pydantic 契约、trigger_config 规范化 | 0.5 人日 | 无 |
| P2-DB-01 | policy、execution、AcquisitionMessage 兼容列及 MessageTemplate scope/owned_group_asset_id 迁移与模型 | 1.25 人日 | P2-BE-01 |
| P2-BE-02 | TargetResolver 与账号 eligibility | 0.5 人日 | 阶段 1 双 ID |
| P2-BE-03 | PolicyService、乐观锁、审计 | 0.75 人日 | P2-DB-01 |
| P2-BE-04 | ContentService、AI/模板、群内广告配置、安全、哈希 | 1.25 人日 | 现有 AI/模板 |
| P2-BE-05 | 四类 TriggerService 与事件路由 | 1 人日 | P2-BE-03 |
| P2-BE-06 | ExecutionService、Speaker 精确发送、门禁及 Telegram 紧前唯一风险预占 | 1.25 人日 | P2-BE-04 |
| P2-BE-07 | Worker、Celery、租约、重试、过期 | 0.75 人日 | P2-BE-05/06 |
| P2-BE-08 | 14 个 API、错误码和 OpenAPI | 1 人日 | P2-BE-03/06 |
| P2-BE-09 | 增长广告 API/dispatcher 排除 OwnedGroupAsset、共享 chat 锁和发送前复核 | 0.5 人日 | 阶段 1 OwnedGroupAsset、P2-BE-06 |
| P2-FE-01 | API 类型、Store、页面路由和资产摘要 | 0.5 人日 | P2-BE-08 |
| P2-FE-02 | 策略抽屉、预览、手动触发 | 0.75 人日 | P2-FE-01 |
| P2-FE-03 | 审核队列、执行历史和错误展示 | 0.75 人日 | P2-FE-01 |
| P2-FE-04 | 群内模板 Tab、模板抽屉、资产级缓存刷新与作用域隔离 | 0.5 人日 | P2-BE-08、P2-FE-01 |
| P2-QA-01 | 后端单测、单次风险预占、并发、增长广告隔离与兼容回归 | 1.5 人日 | 全部后端 |
| P2-QA-02 | 前端单测、构建、模拟器 E2E | 0.75 人日 | 全部前端 |

可并行：

- P2-FE-01 可在 API schema 冻结后与后端服务并行。
- P2-BE-04 可与 P2-BE-02/03 并行。
- P2-QA-01 的模型和纯函数测试可随对应任务同步编写。

## 20. 文件影响范围

### 20.1 后端新增

~~~text
backend/app/api/owned_group_messages.py
backend/app/modules/owned_group/messaging_contracts.py
backend/app/modules/owned_group/messaging_models.py
backend/app/modules/owned_group/messaging_schemas.py
backend/app/modules/owned_group/messaging_target.py
backend/app/modules/owned_group/messaging_policy_service.py
backend/app/modules/owned_group/messaging_trigger_service.py
backend/app/modules/owned_group/messaging_content_service.py
backend/app/modules/owned_group/messaging_execution_service.py
backend/app/modules/owned_group/messaging_event_router.py
backend/app/modules/owned_group/messaging_worker.py
backend/app/modules/owned_group/messaging_tasks.py
backend/migrations/versions/034_add_owned_group_messaging.py
backend/migrations/048_add_owned_group_messaging.sql
~~~

### 20.2 后端修改

~~~text
backend/app/main.py
backend/app/celery.py
backend/app/core/config.py
backend/app/core/runtime_settings.py
backend/app/core/automation_settings.py
backend/app/core/account/risk_guard.py
backend/app/core/account/telegram_execution.py
backend/app/modules/acquisition/models.py
backend/app/api/acquisition.py
backend/app/api/automation.py
backend/app/modules/acquisition/handler.py
backend/app/modules/acquisition/automation.py
backend/app/modules/acquisition/auto_reply/speaker.py
backend/app/modules/acquisition/auto_reply/semantic_reply.py
backend/app/modules/acquisition/auto_reply/templates.py
backend/app/modules/owned_group/__init__.py
backend/.env.example
~~~

### 20.3 前端新增

~~~text
frontend/src/views/OwnedGroupMessaging.vue
frontend/src/api/ownedGroupMessaging.ts
frontend/src/stores/ownedGroupMessaging.ts
frontend/src/views/OwnedGroupMessaging.spec.ts
frontend/src/api/ownedGroupMessaging.spec.ts
~~~

### 20.4 前端修改

~~~text
frontend/src/views/OwnedGroups.vue
frontend/src/router/index.ts
frontend/src/config/automationDefaults.ts
~~~

## 21. 测试文件建议

后端：

~~~text
backend/tests/test_owned_group_message_contracts.py
backend/tests/test_owned_group_message_models.py
backend/tests/test_owned_group_message_target.py
backend/tests/test_owned_group_message_policy.py
backend/tests/test_owned_group_message_content.py
backend/tests/test_owned_group_message_triggers.py
backend/tests/test_owned_group_message_execution.py
backend/tests/test_owned_group_message_worker.py
backend/tests/test_owned_group_message_api.py
backend/tests/test_owned_group_message_concurrency.py
backend/tests/test_owned_group_message_legacy_compat.py
backend/tests/test_owned_group_message_risk_action.py
backend/tests/test_owned_group_message_template_scope.py
backend/tests/test_owned_group_message_growth_ad_isolation.py
~~~

必须覆盖的测试技术：

- SQLite 可覆盖纯模型和服务校验。
- PostgreSQL 集成测试覆盖 advisory lock、SKIP LOCKED、唯一约束和并发额度。
- Fake AccountPool 断言仅 Speaker 调用一次 acquire_by_id，且从不调用 acquire；Fake TelegramExecutionService 断言收到同一 account 对象且自身不访问账号池。
- Fake TelegramExecutionService 断言正确 telegram_chat_id、account_id 和 reply_to_message_id。
- Fake LLM 覆盖重复重生成、Token 预算和安全阻断。
- Fake Redis 故障覆盖数据库去重回退。
- Celery eager 模式覆盖定时槽幂等和重试。
- 参数化测试将全部增长中心广告设置置为 true/false 和极端额度，断言阶段 2 结果不变。
- spy 断言阶段 2 服务未调用广告配置读取函数、广告 dispatcher、AdCampaign 或 tracking link 服务。
- Fake Redis + Fake Telegram 断言业务 skip 不增加 outbound_message，单次 Telegram 尝试只增加 1，失败/结果未知不释放，并且不写 OWNED_GROUP_MESSAGE 动作日计数键或冷却键。
- 参数化 runtime actions.owned_group_message 的非零 daily_limit/cooldown_seconds，断言 _budget_for_action 和 _reserve_budget 始终执行固定 0/0。

前端：

~~~text
frontend/src/views/OwnedGroupMessaging.spec.ts
frontend/src/api/ownedGroupMessaging.spec.ts
frontend/src/stores/ownedGroupMessaging.spec.ts
frontend/src/views/OwnedGroups.spec.ts
~~~

前端至少覆盖：

- 账号不可选原因。
- 模式切换的字段联动。
- trigger_config 校验。
- 202 不误报已发送。
- 审核 revision 冲突。
- 额度与冷却错误展示。
- 非 admin 只读。
- 群内模板新建、编辑、启停、变量校验及切换资产后的列表清理。
- API/Store 不发送 scope/owned_group_asset_id，且不会把增长中心模板合并进群内模板缓存。
- 策略模板下拉只展示当前资产、enabled 且 content_category 匹配的模板。

## 22. 灰度、上线与回滚

### 22.1 上线顺序

1. 合并迁移和模型，功能开关保持 false。
2. 部署 API 和 Worker，运行迁移后只做查询与预览。
3. 设置 runtime enabled=true、dryRun=true，使用模拟 Telegram 验证全链路。
4. 选择 1 个 test001 自建群和 1 个 growth 推广账号。
5. 策略 require_review=true，daily_limit=1，cooldown_seconds>=3600。
6. 确认指定账号、Chat ID、内容和审计全部正确。
7. 在取得真实 Telegram 凭据、代理与灰度授权后，将 dryRun=false。
8. 连续观察 24 小时，无重复发送、无账号风险升级后再扩到 2 个账号。

### 22.2 回滚

- 首选关闭 ownedGroupMessaging.enabled。
- 紧急时关闭 OWNED_GROUP_MESSAGING_ENABLED 并重启 backend/worker。
- 不删除策略和执行历史。
- 不回滚已发送 Telegram 消息。
- 数据库迁移保留新增表和可空列；应用旧版本可忽略。
- 若必须数据库 downgrade，先确认所有非终态 execution 已取消并导出审计。

### 22.3 真实 Telegram 安全边界

- 本地和 test001 默认使用 Fake/Mock Telegram 适配器。
- 没有正式 API ID、API Hash、有效 Session、代理和明确灰度授权时，不执行真实发送。
- 不在日志、测试快照、文档或前端响应中输出凭据。

## 23. Definition of Done

阶段 2 只有同时满足以下条件才可标记完成：

- 两张新表、AcquisitionMessage 兼容列、MessageTemplate scope/owned_group_asset_id 字段、数据回填、约束和索引全部完成。
- 策略、目标解析、内容、触发、审核、执行和 Worker 服务完成。
- 第 13 节全部接口完成并生成准确 OpenAPI。
- 前端独立页面、群内模板管理、策略抽屉、预览、审核队列和执行历史完成。
- 四种触发均走统一 execution 状态机。
- 指定账号发送已由测试证明不会自动换号。
- 账号池职责唯一归属 Speaker；TelegramExecutionService 只接收 account 对象并负责唯一风险预占与底层发送，不发生双 acquire。
- community mode 与 promotion mode 可独立配置，AI 互动与模板广告可在同一账号策略并存。
- 每日额度、群级冷却、跨账号去重和风险门禁均有并发测试。
- require_review=true 未审核不发送已由自动化测试证明。
- 阶段 2 消息不写广告日志、不改变广告资格或广告预热计数。
- auto_ads_enabled、五类广告运行设置和 acquisition_stop 的取值变化不改变阶段 2 发送结果。
- OWNED_GROUP_MESSAGE 风控动作只保留账号安全，不进入增长广告或暖号业务门禁。
- 风险预算只在 Telegram 写调用紧前预占一次；业务 skip 不占 outbound_message，真实尝试的成功、失败或结果未知均只保留 1 次计数，runtime 动作预算不能覆盖固定 0/0。
- owned_group scope 模板与 acquisition 模板在 API、TemplateEngine 查询和运行缓存中双向隔离，不同 OwnedGroupAsset 的缓存也不得串读。
- 旧增长广告 API 和 dispatcher 均会排除 OwnedGroupAsset，避免双通道发送。
- 旧自建群编排、Guardian 治理、外部群关键词、语义回复、暖场和广告回归通过。
- 后端 pytest、Ruff，前端 type-check、test、build 全部通过。
- 使用模拟 Telegram 完成 88 条验收用例，或为无法自动化项提供明确灰度记录。
- 功能开关默认关闭，dryRun 默认开启。
- 真实 Telegram 只在凭据与灰度授权齐全后进行单群单账号验收。
- 最终交付记录代码 SHA、迁移版本、测试结果、目标 asset_id、core_group_id、telegram_chat_id、account_id 和回滚开关。

## 24. 开发实施固定决策

以下事项不再留给开发阶段临时决定：

1. 阶段 2 只支持 TelegramAccount.account_type=promoter 的用户账号。
2. 阶段 2 只支持 operation_mode=growth，明确排除 ad_only。
3. auto_ads_enabled 和增长中心广告配置不得参与自建群普通消息或群内广告资格判断。
4. 一个自建群 + 一个推广账号只有一条策略。
5. 策略不硬删除，只停用。
6. template 的 community 使用 interaction/qa 模板，promotion 使用策略独立指定的 share/guide 模板。
7. 第一版禁止 register_link；promotion_url 和 promotion_cta 只能由 promotion_config.destination_url 和 cta_text 注入。
8. AI 使用全局中性提示词，不提前实现阶段 3 Persona。
9. 四类触发统一创建 execution，不允许 API 同步直发 Telegram。
10. 人工审核在内容生成/渲染之后、Telegram 发送之前。
11. 手动触发不具有任何额度、冷却、审核或风控绕过能力。
12. 策略账号是精确账号，失败时绝不从账号池换号。
13. 数据库关联只用 core_group_id，Telegram 调用只用 telegram_chat_id。
14. 关键词/回复同一源消息只选择一个账号策略。
15. 群级冷却跨该群所有阶段 2 账号生效。
16. 内容去重使用规范化后的 SHA-256，并覆盖不同账号。
17. 数据库是去重最终依据，Redis 故障不得放行重复内容。
18. 阶段 2 的生成目的只标记 community_ai 或 template，并以 content_category 区分 community/promotion。
19. 阶段 2 的普通消息和群内广告均不写 ad_delivery_log、不更新 interaction_sent_today。
20. 自建群事件只由阶段 2 路由处理，旧关键词和语义链不得重复发送。
21. 旧主动群 AI 暖场排除 OwnedGroupAsset 对应群。
22. AI 总开关只控制 ai；合规模板不依赖 AI 总开关。
23. 功能静态开关默认 false，运行 dryRun 默认 true。
24. Telegram 发送结果未知时禁止自动重试。
25. 第一版使用现有 owned_group Celery 队列，不新增消息容器。
26. 群内 promotion 不读取 AdCampaign、AdDeliveryPolicy、广告额度、广告冷却、广告预热、广告探测或 acquisition_stop。
27. promotion 强制人工审核；增长中心允许自动投放也不能绕过。
28. 账号级风险和通用出站安全硬上限继续生效。
29. 阶段 2 固定使用 AccountRiskAction.OWNED_GROUP_MESSAGE，不复用 GROUP_MESSAGE、AI_WARMUP 或 AD_DELIVERY。
30. 阶段 2 模板必须为当前资产 owned_group scope，增长中心模板接口不能修改。
31. 增长广告 API 和 dispatcher 必须双层排除非归档 OwnedGroupAsset。
