# XD 全新部署（2026-11-14）

> **历史记录。** 旧主机 `xd` 已到期。Vanguard 自 2026-11-22 起部署在 `oracle4c24g-us`，域名为 `vanguard.dh113.xyz`。 [当前生产部署指南](../SERVER_DEPLOY.md)

用户指定在 `ssh xd` 全新安装 Vanguard，使用 `vanguard.pipenai.xyz`，不迁移旧系统业务数据。管理员为 `admin`，密码按用户指定设置并已实测登录；本文不保存密码。

## 运行位置

| 项目 | 配置 |
| --- | --- |
| 服务器 | `137.175.65.47:58243`，SSH 别名 `xd` |
| 公网入口 | `https://vanguard.pipenai.xyz` |
| Vanguard 根目录 | `/opt/vanguard` |
| 本次构建源码 | `/opt/vanguard/releases/xd-fresh-20261114` |
| Vanguard 运行时 | `/opt/shared-postgres/stacks/vanguard/compose.json` |
| 应用私有配置 | `/opt/vanguard/.env.production`，0600 |
| 共享组件运行时 | `/opt/shared-services/compose.json` |
| 共享连接约定 | `/opt/shared-services/connection-contract.json` |
| 主机 Nginx | `/etc/nginx/conf.d/vanguard.conf` |
| 后端 / 前端监听 | `127.0.0.1:18080` / `127.0.0.1:13000` |

PostgreSQL、Redis、主机 Nginx 与 `airport-growth-multi-agent` 共用。实际容器连接已核对，两套应用均连接 `xd-shared-services` 网络，没有额外启动各自的数据库或 Redis。

| 系统 | PostgreSQL 数据库 / 账号 | Redis ACL 账号 / DB |
| --- | --- | --- |
| Vanguard | `vanguard` / `vanguard_app` | `vanguard` / 3（业务）、4（Celery）、5（结果） |
| Airport | `airport_growth` / `airport_growth_app` | `airport_growth` / 0–2 |

共享服务为 `xd-shared-postgres:5432`（PG18）、`xd-shared-redis:6379`（Redis 8）。连接凭据分别保存在 `/opt/shared-services/apps/vanguard.env`、`/opt/shared-services/apps/airport-growth.env`，均仅 root 可读。应用数据库角色没有超级用户、建库或创建角色权限；Redis 应用账号禁用了全局清空、配置和 ACL 管理等命令。

## 常规发布

先读取实际运行时服务名；构建新镜像后更新对应服务的镜像引用。只发布明确的 Vanguard 服务并使用 `--no-deps`，不以仓库旧 Compose 全栈覆盖运行环境。

```bash
ssh xd
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json config --quiet
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json config --services
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json up -d --no-deps backend
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json ps
```

前端及 worker 使用同一运行时文件，分别指定实际服务名。不得执行共享组件的 `down`、删除共享卷、重置公共凭据，或修改 Airport 的 Nginx 配置。Oracle 是独立历史环境，此次安装没有改动其数据库、Redis 或应用；其规则仍见 `ORACLE_SHARED_POSTGRES.md`。

## 持久化与首次配置

数据库和 Redis 使用共享 Compose 管理的持久卷；Vanguard 会话、上传、日志分别在 `/opt/vanguard/sessions`、`/opt/vanguard/data/uploads`、`/opt/vanguard/data/logs`。应用以 UID 1000 运行，生产环境关闭 DEBUG，JWT 和会话加密密钥独立生成。

这是空白系统，需在后台填写 Telegram API 配置、账号、代理及机器人，并按需要配置 AI / XBoard 集成。两个 Telegram worker 的心跳正常；未配置账号/机器人时，应用按既有逻辑显示 `degraded`，`last_error` 为空，不代表 worker 进程故障。未进行真实 Telegram 发信或营销任务测试。

## HTTPS

Let's Encrypt 证书位于 `/etc/letsencrypt/live/vanguard.pipenai.xyz/`，本次证书到期日为 2027-02-12，私钥权限为 0600。Nginx 保留 `/.well-known/acme-challenge/` 路径，`certbot.timer` 已启用，部署 hook 在续期后测试并 reload Nginx。

服务器原有 Python 包与系统 Certbot 存在依赖路径冲突，已通过 `/usr/local/bin/certbot` 包装器及 `/etc/systemd/system/certbot.service.d/10-system-python.conf` 固定使用系统 Python 依赖，未改动其他业务 Python 包。实际 `renew --dry-run --no-random-sleep-on-renew` 已成功。

## 本次安装修复及验证

修正 `backend/app/core/account/models.py` 的 11 个时间默认值，使 PostgreSQL 将 `CURRENT_TIMESTAMP` 作为表达式执行。新建库初始化会验证 QQ 模型表、列、索引并记录旧迁移 065 的基线，避免重复建表；历史 SQL 文件和原校验值保持不变。

实际 PostgreSQL 新建库、迁移至 078（53 条迁移记录）、管理员创建、镜像构建、HTTPS 登录、认证保护、静态资源、主要业务接口和证书续期演练均通过。线上检查共 18 项。767 个部署源码文件的 SHA-256 全部一致。相关本机 pytest 在进入数据库相关测试后长时间无进展，已中断，未作为完整单元测试通过记录。

最终后端镜像为 `vanguard-backend:xd-fresh-20261114-schemafix3`，前端镜像为 `vanguard-frontend:xd-fresh-20261114`。服务器证据位于 `/opt/vanguard/build-result.json`、`/opt/vanguard/schemafix-result.json`、`/opt/vanguard/release-result.json`、`/opt/vanguard/finalization-result.json`、`/opt/vanguard/renewal-result.json`，源码哈希位于本次源码目录的 `source-manifest.json`。本机线上验收报告位于 `.codex-deploy/xd-20261114/verification.json`。失败的首次初始化日志保留为 `release*.attempt*.json` / `release*.attempt*.log`。
