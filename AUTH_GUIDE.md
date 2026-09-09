# Vanguard 认证系统说明

生产站点：`https://vanguard.pipenai.xyz`。

## 接口

- `POST /api/auth/login`：提交 `username`、`password`，返回 JWT。
- `GET /api/auth/user`：使用 `Authorization: Bearer <token>`。
- `POST /api/auth/logout`：注销客户端会话。
- `PUT /api/auth/password`：修改当前管理员密码。

## 首次管理员

首次部署通过 `scripts/codex_deploy_automation.py --bootstrap` 调用
`backend/scripts/init_admin.py` 创建管理员。用户名默认为 `admin`，密码由部署时
随机生成并只保存在目标机 `/opt/vanguard/.env.production`（0600）；仓库不包含
默认密码。登录后请立即轮换密码，并按组织的密钥保管流程保存新凭据。

## 部署后检查

```bash
curl -fsS https://vanguard.pipenai.xyz/health
ssh oracle4c24g 'cd /opt/vanguard && docker compose -f docker-compose.production.yml ps'
```

JWT 密钥至少 64 字符，生产环境不允许使用开发占位符。Telegram 群真实执行仍由
`OWNED_GROUP_EXECUTION_ENABLED=false` 阻断，需完成单独的安全审批后才可开启。
