# Vanguard 生产部署与认证

生产入口为 `https://vanguard.pipenai.xyz`，主机为 `oracle4c24g`，部署目录为
`/opt/vanguard`。不要使用旧的 `xd`、`rensw.xyz` 或固定默认密码说明。

## 首次部署

```bash
PYTHONIOENCODING=utf-8 python scripts/codex_deploy_automation.py --bootstrap
```

`--bootstrap` 会初始化 PostgreSQL、Redis、迁移 026–044，并执行
`backend/scripts/init_admin.py`。管理员用户名和随机密码来自目标机的
`.env.production`（权限 0600）；密码不会提交到 Git。首次登录后请立即在系统设置中轮换。

## 认证接口

- 登录：`POST https://vanguard.pipenai.xyz/api/auth/login`
- 当前用户：`GET /api/auth/user`（`Authorization: Bearer <token>`）
- 修改密码：`PUT /api/auth/password`

## 验证

```bash
curl -fsS https://vanguard.pipenai.xyz/health
ssh oracle4c24g 'docker logs --tail 100 vanguard-backend'
```

真实 Telegram 群执行默认关闭；开启前必须满足 P0 安全门、Redis fail-closed、
凭据/代理和灰度回滚要求。
