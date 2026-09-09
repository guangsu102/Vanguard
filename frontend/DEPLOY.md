# 前端生产部署

Vanguard 前端随生产 Compose 镜像部署到 `oracle4c24g`，公网地址为
`https://vanguard.pipenai.xyz`。主机 Nginx 将 `/` 转发到 loopback
`127.0.0.1:13000`，API `/api` 转发到 `127.0.0.1:18080`。

## 发布

在仓库根目录运行：

```bash
PYTHONIOENCODING=utf-8 python scripts/codex_deploy_automation.py
```

脚本会重新构建并重启前端服务，同时保留 `/opt/vanguard/data`、会话和环境文件。
不要使用旧的 `xd`、`/var/www/vanguard` 或单独覆盖主机 Nginx 的流程。

## 验证

```bash
ssh oracle4c24g 'docker inspect --format "{{.State.Health.Status}}" vanguard-frontend'
curl -fsS -o /dev/null -w '%{http_code}\n' https://vanguard.pipenai.xyz/
curl -fsS https://vanguard.pipenai.xyz/health
```
