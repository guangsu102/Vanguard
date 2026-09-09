# Vanguard 生产部署指南（oracle4c24g）

当前生产目标：`oracle4c24g`（`168.110.23.229:22`），公网入口为
`https://vanguard.pipenai.xyz`。主机 Nginx 转发到 loopback 后端
`127.0.0.1:18080`、前端 `127.0.0.1:13000`；同机的 `/opt/sub2api-dr`
服务由另一套 Compose 管理，禁止在 Vanguard 发布中停止或重配。

## 发布

```bash
# 在仓库根目录执行；首次目标机部署才加 --bootstrap
PYTHONIOENCODING=utf-8 python backend/../scripts/codex_deploy_automation.py --bootstrap

# 后续发布
PYTHONIOENCODING=utf-8 python backend/../scripts/codex_deploy_automation.py
```

脚本会把旧 `/opt/vanguard` 移到 `/opt/vanguard.file-backups/`，保留
`.env.production`、数据库/Redis 数据和会话目录；首次 bootstrap 执行基础
建表、增量迁移 026–044，并从环境变量创建管理员。管理员密码不写入仓库。

## 验证

```bash
ssh oracle4c24g 'cd /opt/vanguard && docker compose -f docker-compose.production.yml ps'
curl -fsS https://vanguard.pipenai.xyz/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://vanguard.pipenai.xyz/
```

生产环境初始阶段 `OWNED_GROUP_EXECUTION_ENABLED=false`，Telegram 凭据未配置；
启用真实群操作前必须另行完成凭据、代理、两账号以内灰度和回滚演练。

## Nginx 回滚

新增配置模板位于 `deploy/oracle4c24g-nginx.conf`，主机安装位置为
`/etc/nginx/conf.d/vanguard.conf`。每次变更前先备份该文件和
`sub2api.conf`，通过 `nginx -t` 后再 `systemctl reload nginx`；回滚时恢复最近的
`/root/vanguard-nginx-backups/<timestamp>/vanguard.conf`，然后再次测试并 reload。
