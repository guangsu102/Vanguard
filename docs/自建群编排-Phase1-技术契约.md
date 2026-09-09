# 自建群编排 Phase 1 技术契约

状态：已确认，作为开发基线。  
范围：自有 Telegram 用户账号和已登记机器人创建/加入自建群；不修改原有群池业务。

## 当前实现状态（本次续接）

本地实现现已覆盖 `owned_*` 持久化与迁移链、草稿和成员计划 API、持久化 Worker 状态机、受安全门保护的 Telethon 适配器、Bot profile 登记/验证、邀请链接读取与管理员撤销/重新生成、创建超时资产对账，以及前端操作界面。`OWNED_GROUP_EXECUTION_ENABLED` 仍默认关闭，Redis 全局停止与 fail-closed 安全门会在每次真实写入前再次校验。

2026-09-07 已将当前工作树部署到 `oracle4c24g` 的 `/opt/vanguard`，并通过
`https://vanguard.pipenai.xyz` 完成 origin/public health、前端、登录和鉴权验收；
数据库迁移 026–044 已完成。生产仍未配置 Telegram 凭据，且
`OWNED_GROUP_EXECUTION_ENABLED=false`，因此尚未进行真实 Telegram 账号或群操作 E2E。
正式开启执行前仍必须完成凭据/代理核验、两账号以内灰度和回滚演练。

## 1. 业务边界

新增独立模块“自建群编排”。模块只管理由系统可验证归属的自建超级群、其成员编排、管理员配置、邀请链接和审计。

原有群池、消息任务、客服会话不进入本模块，也不受本模块的全局停止影响。

Phase 1 一次只创建一个群；成员资源可包含用户账号和已登记机器人。机器人不能作为群主创建群。

## 2. 资源类型

### 用户账号

- 使用已授权的 Telegram 用户 Session。
- 可创建群、邀请成员、加入群、成为管理员。
- 账号列表显示 Telegram 用户名和手机号；日志和审计导出中的敏感值脱敏。

### 机器人

- 通过 BotFather 创建，系统负责引导、Token 验证和安全托管。
- 使用 Bot API，不与用户 Session 共用认证表或执行器。
- 可加入自建群、成为管理员和设置群内管理员头衔。
- 机器人显示名称可由 Bot API 修改；机器人用户名由 BotFather 修改。

## 3. 默认规则

- 群类型：超级群。
- 可见性：公开；可以切换为私有。
- 默认加入方式：群主用户账号直接邀请。
- 公开群必须配置合法的 Telegram username。
- 默认批次：5 个资源。
- 默认单群并发：1。
- 默认批次间隔：10 分钟。
- 网络类错误自动重试：最多 2 次。
- FloodWait：等待 Telegram 返回的完整冷却时间。
- PeerFlood、SpamBlock、永久权限错误：不自动循环重试。

## 4. 核心流程

```text
创建草稿
  -> 选择群主用户账号
  -> 填写标题、简介、公开/私有
  -> 选择用户账号和机器人
  -> 配置管理员、权限、群内头衔
  -> 预检查
  -> 冻结 selection_snapshot/config_snapshot
  -> 创建群
  -> 保存 Chat ID 和链接
  -> 分批邀请/加入
  -> 查询 Telegram 成员关系
  -> 需要管理员时提升并再次核验
  -> 完成或部分完成
```

Telegram 邀请接口返回成功不等于成员已经加入。必须重新查询成员关系：

- 普通成员最终状态：`MEMBER_VERIFIED`。
- 管理员最终状态：`ADMIN_VERIFIED`。
- 结果不确定时：`UNKNOWN`，先 reconcile 后再决定是否重试。

## 5. 重要状态

### 群资产

`DRAFT -> PRECHECKING -> CREATING -> READY`  
失败进入 `CREATE_FAILED` 或 `NEEDS_ATTENTION`；Telegram 群不自动删除，业务侧使用 `ARCHIVED`。

### 整批任务

`DRAFT -> QUEUED -> RUNNING -> PAUSED/STOPPING -> STOPPED`  
自然结束为 `COMPLETED` 或 `PARTIAL_COMPLETED`；不可恢复异常为 `FAILED`；服务崩溃或网络结果不确定为 `UNKNOWN`。

### 单项任务

`PENDING -> IN_PROGRESS -> INVITE_SENT/WAITING_APPROVAL -> MEMBER_VERIFIED`  
管理员资源继续进入 `ADMIN_PROMOTING -> ADMIN_VERIFIED`。已有成员标记 `SKIPPED_ALREADY_MEMBER`；永久失败、临时失败和待核验分别记录。

## 6. 数据实体

建议使用独立命名空间和表前缀：

- `owned_group_assets`：自建群资产。
- `owned_group_operations`：整批创建/入群操作、幂等键和配置快照。
- `owned_group_operation_items`：每个用户账号或机器人一条执行项。
- `owned_group_memberships`：当前成员关系和最后核验状态。
- `owned_group_admin_assignments`：管理员权限、群内管理员头衔和核验状态。
- `owned_group_invite_links`：公开链接/邀请链接及其状态。
- `owned_bot_profiles`：BotFather 创建后登记的机器人和加密 Token 引用。
- `owned_group_audit_events`：群、任务、资源和控制操作审计。

每次提交冻结：

- `selection_snapshot`：精确的 `resource_type/resource_id` 列表。
- `selection_snapshot_hash`：快照校验值。
- `config_snapshot`：批次、间隔、并发、重试和管理员配置。

## 7. 邀请链接

- 所有登录系统的用户都可以查看和复制邀请链接。
- 公开群显示公共链接；若生成额外邀请链接，也同时显示。
- 私有群显示当前有效邀请链接。
- 查看/复制不改变链接状态。
- 撤销、重新生成默认仅管理员可以执行，因为这些动作可能让旧链接失效。
- 链接加密存储，不写入普通日志和审计导出。

## 8. 管理员配置

选择资源加入群后，可以配置：

- 是否提升为管理员。
- 管理员权限集合。
- 当前群内管理员头衔，例如“客服主管”“内容管理员”。

群标题是整个群唯一的名称；每个管理员只能拥有自己的群内头衔。修改 Telegram 用户资料名或机器人显示名属于全局资料变更，不等同于群标题。

## 9. 停止语义

模块全局紧急停止只影响自建群编排：

- 停止自建群创建队列、入群队列、自动重试和定时任务。
- 不停止原有群池、消息任务、客服、账号健康检查。
- 已发出的当前 RPC 自然结束；Worker 不再领取新任务。
- 已创建的 Telegram 群不自动删除。
- 未执行项进入暂停或停止状态，可在核验后恢复。

## 10. 错误与恢复

标准 ReasonCode 至少包括：

`ALREADY_MEMBER`, `ACCOUNT_OFFLINE`, `SESSION_REVOKED`, `ACCOUNT_COOLDOWN`, `PRIVACY_RESTRICTED`, `NOT_MUTUAL_CONTACT`, `BANNED`, `NO_ADMIN_PERMISSION`, `FLOOD_WAIT`, `PEER_FLOOD`, `NETWORK_TIMEOUT`, `INVITE_EXPIRED`, `UNKNOWN_NEEDS_RECONCILE`。

创建群超时不能直接重试，必须先查询并 reconcile，防止重复建群。服务重启后过期 Worker lease 的项目先进入 `UNKNOWN`，核验后再继续。

## 11. 权限和安全

- 管理员：账号/机器人管理、群操作、全局停止、邀请链接管理。
- 操作员：创建草稿、预检、提交、暂停、恢复和失败重试；不能读取凭证。
- 审计员：只读和导出审计。
- User Session、Bot Token、邀请链接不进入日志。
- 不允许通过换账号、换代理绕过 Telegram 风控。
- 只允许操作已授权的自有账号、已登记机器人和本模块创建的群。

## 12. 最低验收标准

1. 选择 200 个资源中的 100 个，提交后快照准确且不随筛选变化。
2. 群主自动计入计划成员但不重复邀请。
3. 创建超时不会产生重复群。
4. 邀请接口成功但成员未核验时不能标记为成功。
5. 管理员提升必须二次核验。
6. FloodWait 按服务端时间等待，永久错误不无限重试。
7. 暂停/停止不再领取新任务，恢复不重复已核验成功项。
8. 邀请链接对所有登录用户可查看和复制。
9. 全局停止不影响原有群池和其他模块。
10. Session、Bot Token 和完整邀请链接不出现在日志。
