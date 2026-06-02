# Bot Matrix 数据库迁移

## 迁移说明

这些迁移用于创建 Bot 矩阵所需的数据库表。

### 迁移文件列表

1. `001_create_tg_users.sql` - Telegram 用户表
2. `002_create_trial_accounts.sql` - 试用账号记录表
3. `003_create_checkin_records.sql` - 签到记录表
4. `004_create_affiliate_posters.sql` - 推广海报记录表
5. `005_create_violation_records.sql` - 违规记录表
6. `006_create_ban_records.sql` - 封禁记录表

## 执行方式

### 使用 Docker

```bash
docker-compose exec postgres psql -U xboard_bot -d xboard_bot_matrix -f migrations/001_create_tg_users.sql
docker-compose exec postgres psql -U xboard_bot -d xboard_bot_matrix -f migrations/002_create_trial_accounts.sql
# ... 其他迁移
```

### 使用 psql 直接连接

```bash
PGPASSWORD=xboard_bot_password psql -h localhost -U xboard_bot -d xboard_bot_matrix -f migrations/001_create_tg_users.sql
```
