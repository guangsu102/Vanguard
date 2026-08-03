# Vanguard NapCatQQ + OneBot 11 接入

Vanguard 的 QQ 通知使用普通 QQ 账号登录 NapCatQQ，再通过 OneBot 11 HTTP API
发送消息、通过 OneBot 11 WebSocket 接收群消息和群事件。不再需要 QQ 官方 Bot
的 AppID、AppSecret、群 OpenID 或主动消息权限。

> NapCatQQ 属于非官方客户端方案，可能违反 QQ 服务条款并触发限制登录、验证码、
> 冻结或封号。生产环境必须使用独立低价值 QQ 账号，并保留 Telegram/Webhook
> 降级通知通道。

## 当前版本

- 2026-07-19 核对的 NapCatQQ 正式版：`v4.18.9`
- 官方发布页：https://github.com/NapNeko/NapCatQQ/releases
- 官方 Docker 镜像：`mlikiowa/napcat-docker:latest`
- Docker 仓库：https://github.com/NapNeko/NapCat-Docker

Compose 默认跟随官方 `latest`。正式升级前应先记录当前镜像 digest，以便回滚。

## 需要准备的资料

1. 一个专用于 Vanguard 通知的 QQ 账号及 QQ 号。
2. 可以在手机 QQ 上确认该账号登录、扫码和设备验证的人员。
3. 目标 QQ 群号列表，以及每个群对应的后台显示名称。
4. 该 QQ 账号已经加入目标群；需要撤回其他成员消息时，应授予群管理员权限。
5. 哪些群接收告警、哪些群接收公告的业务选择。
6. 对非官方客户端风控和账号封禁风险的明确接受。

不需要提供 QQ 密码给 Vanguard，也不需要 QQ 官方 Bot AppID/AppSecret。OneBot
Access Token 可在部署时生成，要求至少 32 个随机字符。

## 环境变量

```dotenv
QQ_ONEBOT_ENABLED=true
QQ_ONEBOT_ACCOUNT_ID=<登录 NapCat 的 QQ 号>
QQ_ONEBOT_HTTP_URL=http://napcat:3000
QQ_ONEBOT_WS_URL=ws://napcat:3001
QQ_ONEBOT_ACCESS_TOKEN=<至少 32 个随机字符>
QQ_ONEBOT_REQUEST_TIMEOUT_SECONDS=10
QQ_ONEBOT_MESSAGE_RETENTION_DAYS=30
NAPCAT_IMAGE=mlikiowa/napcat-docker:latest
NAPCAT_UID=1000
NAPCAT_GID=1000
```

`QQ_ONEBOT_ACCESS_TOKEN` 必须与 NapCat OneBot 网络配置中的 Token 完全一致。

## 首次部署

在 `test001` 的 `/root/Vanguard` 中执行：

```bash
docker-compose -f docker-compose.test001.yml pull napcat
docker-compose -f docker-compose.test001.yml up -d napcat
docker logs vanguard-napcat
```

NapCat WebUI 仅监听服务器本机。建立 SSH 隧道：

```bash
ssh -L 6099:127.0.0.1:6099 test001
```

浏览器打开 `http://127.0.0.1:6099/webui`，使用容器日志中的 WebUI Token
进入后台，然后完成：

1. 使用手机 QQ 扫码登录准备好的专用账号。
2. 在“网络配置”中新建 OneBot 11 HTTP 服务，监听 `0.0.0.0:3000`。
3. 新建 OneBot 11 WebSocket 服务，监听 `0.0.0.0:3001`。
4. 两个服务配置相同的 `QQ_ONEBOT_ACCESS_TOKEN`。
5. 启用心跳事件，消息格式使用消息段数组。

随后启动 Vanguard：

```bash
docker-compose -f docker-compose.test001.yml up -d --build \
  backend celery-worker qq-onebot-worker frontend gateway
docker logs -f vanguard-qq-onebot-worker
```

进入管理后台“NapCat QQ群”，点击“同步群列表”，再为需要接收通知的群打开
“群通知”。Sub2API 告警与公告会沿用这些群开关。

## 验证

1. 页面连接状态为“在线”，QQ 号与 NapCat 实际登录账号一致。
2. 群同步数量与 QQ 账号已加入的群数量一致。
3. 向一个测试群发送通知，Celery 命令状态变为 `succeeded`。
4. 测试群的新消息能进入消息抽屉。
5. 对由该账号发送的测试消息执行撤回。
6. 触发一条 Sub2API 测试告警，确认只发送一次。

## 运维边界

- 不对公网开放 3000、3001、6099 端口。
- OneBot Token 泄露后立即同时轮换 NapCat 和 Vanguard 配置。
- 每次升级 NapCat 或 Linux QQ 前先备份三个 `napcat_*` Docker volume。
- 出现验证码或风控提示时暂停发送，人工在 NapCat WebUI 恢复登录。
- 不使用此通道批量拉群、私聊陌生用户或绕过群禁言。
