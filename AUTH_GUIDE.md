# Vanguard 认证系统说明

生产站点为 [vanguard.dh113.xyz](https://vanguard.dh113.xyz)。

## 接口

- `POST /api/auth/login`：提交 `username`、`password`，返回 JWT。
- `GET /api/auth/user`：使用 `Authorization: Bearer <token>`。
- `POST /api/auth/logout`：注销客户端会话。
- `PUT /api/auth/password`：修改当前管理员密码。

## 首次管理员

管理员初始化使用 `backend/scripts/init_admin.py`，配置来自目标环境；不要为创建管理员运行旧的全栈 bootstrap。现有生产账号和认证设置须保留。私有环境文件 `/opt/vanguard/.env.production` 权限为 0600，仓库不包含默认密码。登录后按组织的密钥保管流程轮换与保存凭据。部署步骤统一见[生产部署指南](SERVER_DEPLOY.md)。

## 部署后检查

```bash
curl -fsS https://vanguard.dh113.xyz/health
ssh oracle4c24g-us 'docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json ps'
```

JWT 密钥至少 64 字符，生产环境不允许使用开发占位符。Telegram 群真实执行受功能开关和权限控制，实际状态以目标环境为准。
