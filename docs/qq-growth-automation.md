# QQ 自动化：素材绑定、入群任务与广告投放

管理入口：增长中心 → QQ 自动化，路由 `/qq-automation`。

QQ 账号绑定共用素材库中的广告素材，然后执行自己的 QQ 广告计划。QQ 群号、账号关联、
投放频率和执行记录使用独立表，不使用 Telegram 群 ID 或 Telegram 账号连接池。

## 当前能力与尚未完成的执行端

当前代码支持已入群同步、文字/图片/图文素材绑定、入群后一次投放、间隔投放、每日定时、
每账号及每群每日上限、暂停计划、投放回执和未知结果核对。多个 QQ 账号可以分别连接
独立 NapCat 实例。

**仅使用当前 NapCat OneBot 接入，无法实现主动申请加入任意目标群。**
上游 `set_group_add_request` 明确是同意/拒绝已收到的申请或邀请，并非主动申请接口。
本项目没有把这个接口当作主动加群接口，也没有实现或验证 NapCat 原生主动加群插件。

- 上游处理申请实现：
  <https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/group/SetGroupAddRequest.ts>
- 上游内核类型虽然声明 `reqToJoinGroup(groupCode, arg: unknown)`，但没有给出可使用的
  OneBot 主动申请契约；单凭此声明不能确认主动申请参数或实际可用性。
  <https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/services/NodeIKernelGroupService.ts>

未配置主动加群执行端时，未加入目标群的任务标记 `unsupported`，不会发送假的申请，
也不会把登记群号标记为已入群。已经加入的目标群仍可按计划投放。
因此，“与 Telegram 一样自动申请入群”的完整目标仍需补齐并实测主动加群执行端。

### 当前实例核实结果（2026-10-01）

Oracle 实例运行 NapCat `4.18.28`、QQNT `3.2.30-50969`。用户扫码后，登录状态为
`ready`，`isLogin` 和 `coreReady` 均为 `true`。通过该实例的接口实际读取了群列表和
用户提供的测试群信息；群列表确认当前账号尚未加入测试群。

该实例公布的 178 个 OneBot 动作中，没有主动申请入群动作。
`set_group_add_option` 设置群的入群方式，`set_group_add_request` 处理收到的申请；
两者均不能替代主动申请。QQNT 程序文件及公开接口类型也未提供可确认的主动申请参数契约。
目前没有提交真实入群申请，也没有发送测试广告。

线上 Vanguard 当前的全局 QQ 连接尚未启用，新自动化代码和数据库迁移尚未部署。
扫码登录验证的是 NapCat 账号状态，不能作为自动加群或整条投放流程已上线的证据。

## 使用顺序

1. 配置现有 NapCat 的 QQ 号、HTTP API 和 Token，扫码登录。
2. 在 QQ 自动化的账号页同步已加入群，确认登录 QQ 号与配置一致。
3. 创建广告计划，粘贴目标群号，选择模式、入群等待时间、发送间隔和每日上限。
4. 在素材绑定页选择 QQ 账号、计划和素材。素材与 Telegram 共用；图片必须使用
   HTTP/HTTPS 地址，Telegram `file_id` 不适用于 QQ。
5. 在账号配置中启用自动化，并启用广告计划。调度器每分钟检查一次。
6. 在加群任务和投放记录页查看执行状态。失败投放暂停；结果未知的投放需核对群消息。

新账号及新计划默认不启动自动化。已有群的首次同步时间作为入群等待的起点；明确的
再次入群会重新计算等待时间。禁言账号等待禁言结束，已退出的群不接收投放。

## 主动加群桥接契约

这是预留的执行端接入协议，**不是当前 NapCat 提供的 API，也不是已实现的主动加群插件**。
只有执行端实际支持主动申请入群，才应填写账号的 `join_api_url` 和 `join_api_token`。

Vanguard 向配置的完整 HTTP/HTTPS 地址发送 POST，带 Bearer 凭证及
`Idempotency-Key: <operation_id>`。请求：

```json
{
  "account_id": "10001",
  "group_number": "123456789",
  "verify_message": "申请验证消息",
  "operation_id": "持久化操作ID"
}
```

响应必须回传一致的账号和群号：

```json
{
  "status": "pending_approval",
  "account_id": "10001",
  "group_number": "123456789",
  "message": "申请已提交，等待群管理员处理"
}
```

支持的状态是 `pending_approval`、`joined`、`rejected`、`action_required`、`unsupported`。
执行端应持久化幂等键，同一操作 ID 不重复申请。`joined` 响应仍需通过匹配账号的
`get_group_list` 确认真实成员关系后才允许投放。网络超时、无效响应和账号不匹配保持
`unknown`，不自动重复申请。验证码和额外验证由执行端返回 `action_required`。

## 执行与恢复

- QQ 账号及目标群分别使用 Redis 租约串行执行。单账号每次调度最多一次广告及一次申请。
- 远端写操作前提交唯一操作记录和冻结的消息段，发送结果落库后才推进下一次投放。
- 超时、异步响应、无回执或进程中断不能证明未发送；对应投放暂停并等待核对。
- 手工恢复需要确认远端未发送。待审核及结果未知的申请不能直接重复排队。
- 每日预算按广告计划时区计算；账号总预算和加群预算按北京时间计算。
- 每次广告执行重新核对账号、计划、目标群、绑定素材和成员关系，保留现有群通知开关。
- 凭证通过现有加密服务存储，不通过管理 API 返回。加密依赖现有服务器的密钥配置。

## 数据库与发布

新增迁移 `050_qq_growth_automation`，对应 SQL 文件 `065_qq_growth_automation.sql`。
发布需要先备份 Vanguard 数据库并应用该迁移，再更新 backend、celery-worker、celery-beat
及 frontend。迁移保留已有 QQ 账号和群；旧登记群的成员状态初始化为 `unknown`，需同步确认。
回滚应用版本时保留发送记录，不删除或回退操作证据。

Oracle 必须遵守 [共享 PostgreSQL 部署规则](ORACLE_SHARED_POSTGRES.md)，使用
`/opt/shared-postgres/stacks/vanguard/compose.json`，明确指定要更新的应用服务并使用
`--no-deps`。QQ 定时任务运行在现有 `qq_commands` 队列，不新增数据库实例。
此功能的本地测试不涉及真实 QQ 入群、发广告或生产迁移。
