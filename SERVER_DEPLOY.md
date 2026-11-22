# Vanguard 生产部署指南（Oracle US）

2026-11-22 在 `oracle4c24g-us`（`147.224.11.146`）以新数据库部署。旧主机 `xd` 已到期，旧业务数据未迁入。 [当前登录入口](https://vanguard.dh113.xyz/login) · [XD 部署记录](docs/XD_DEPLOYMENT.md)

| 项目 | 当前配置 |
| --- | --- |
| SSH | `ssh oracle4c24g-us` |
| 公网域名 | `vanguard.dh113.xyz` |
| 应用目录与私有配置 | `/opt/vanguard`、`/opt/vanguard/.env.production` |
| 运行时 Compose | `/opt/shared-postgres/stacks/vanguard/compose.json` |
| 内网监听 | 后端 `172.17.0.1:23080`、前端 `172.17.0.1:23000` |
| PostgreSQL 18 | 共享实例 `tgegg/oracle-shared-postgres-0`，独立库 `vanguard` 和账号 `vanguard_app` |
| Redis 8 | Vanguard 专用 Redis，DB 3 为应用缓存，DB 4 和 5 分别用于 Celery 队列和结果 |

Docker 应用通过私有转发 `oracle-shared-postgres:5432` 连接共享数据库。承载转发的 `oracle-shared-postgres-apps.socket` 也供 Airport Growth 使用。

## 发布入口

先核对运行时服务名，构建并验证新镜像，再更新对应应用服务的镜像和必要配置。

在目标服务器执行以下检查；仅在对应镜像配置完成后更新指定应用服务：

```bash
ssh oracle4c24g-us
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json config --quiet
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json config --services
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json up -d --no-deps backend
```

前端发布指定 `frontend` 服务。更新 Telegram worker 前先查实际服务名。现有实例沿用运行时 Compose、数据库及 Redis，保留认证设置、私有环境文件和会话数据。共享 PostgreSQL、数据库转发、Traefik 及同机其他业务使用各自的维护流程。 [认证说明](AUTH_GUIDE.md)

## 验证

```bash
ssh oracle4c24g-us 'docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json ps'
curl -fsS https://vanguard.dh113.xyz/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://vanguard.dh113.xyz/login
```

业务开关、凭据及灰度状态须以目标环境和本次验收为准，不能沿用历史报告中的初始状态。

## 网站入口与证书

K3s Traefik 在 `vanguard` 命名空间提供 HTTP 跳转和 HTTPS 路由。`/api`（含 `/api/ws`）和 `/health` 转给后端，其余页面转给前端。

Cloudflare 的代理 A 记录指向 `147.224.11.146`，加密模式为**完全（严格）**。源站证书文件是 `/opt/shared-postgres/tls/dh113.xyz/origin.pem`，私钥是同目录的 `origin.key`。证书覆盖 `vanguard.dh113.xyz` 和 Airport Growth 的 `x.dh113.xyz`，有效期至 2029-11-21。Traefik 从两个命名空间各自的 `dh113-origin` Secret 读取证书。证书更换时同步更新这两个 Secret，并复核两个站点。 [Cloudflare 源站证书说明](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)

```bash
k3s kubectl -n vanguard get ingressroute https
openssl x509 -in /opt/shared-postgres/tls/dh113.xyz/origin.pem -noout -issuer -enddate -ext subjectAltName
```
