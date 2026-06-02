# Telegram 账号登录功能说明

本文档说明如何使用 Vanguard 系统的 Telegram 账号登录功能。

## 功能概述

系统支持两种方式添加 Telegram 账号：

1. **验证码登录**：通过手机号接收验证码完成登录（支持 2FA）
2. **Session 文件导入**：直接导入已登录的 `.session` 文件

---

## 方式 1：验证码登录

### 流程说明

```
Step 1: 输入手机号 → 发送验证码
Step 2: 输入验证码 → 验证登录
Step 3: (如果开启 2FA) 输入 2FA 密码
Step 4: 完成登录，保存账号
```

### 前端使用

1. 点击"添加账号"按钮
2. 选择"验证码登录"
3. 输入手机号（格式：`+8613800138000`）
4. 选择 API 配置（默认：`default`）
5. 输入国家代码（如：`US`、`CN`）
6. 点击"下一步"，系统发送验证码
7. 输入收到的 5 位验证码
8. 如果账号开启了 2FA，输入 2FA 密码
9. 完成登录

### API 端点

#### 1. 发送验证码

```http
POST /api/accounts/auth/send-code
Content-Type: application/json

{
  "phone": "+8613800138000",
  "api_config_name": "default"
}
```

**响应**：

```json
{
  "code": 0,
  "message": "Verification code sent",
  "data": {
    "session_id": "+8613800138000_abc123...",
    "phone_code_hash": "abc123...",
    "code_type": "SentCodeTypeSms",
    "timeout": 300
  }
}
```

#### 2. 验证验证码

```http
POST /api/accounts/auth/verify-code
Content-Type: application/json

{
  "session_id": "+8613800138000_abc123...",
  "code": "12345"
}
```

**响应（无 2FA）**：

```json
{
  "code": 0,
  "message": "Login successful",
  "data": {
    "status": "success",
    "requires_2fa": false,
    "user_id": 123456789,
    "username": "myusername",
    "session_string": "1AQAAA..."
  }
}
```

**响应（需要 2FA）**：

```json
{
  "code": 0,
  "message": "2FA required",
  "data": {
    "status": "requires_2fa",
    "requires_2fa": true,
    "session_id": "+8613800138000_abc123..."
  }
}
```

#### 3. 验证 2FA 密码

```http
POST /api/accounts/auth/verify-2fa
Content-Type: application/json

{
  "session_id": "+8613800138000_abc123...",
  "password": "my2fapassword"
}
```

**响应**：

```json
{
  "code": 0,
  "message": "Login successful",
  "data": {
    "status": "success",
    "user_id": 123456789,
    "username": "myusername",
    "session_string": "1AQAAA..."
  }
}
```

#### 4. 完成登录

```http
POST /api/accounts/auth/complete-login
Content-Type: application/json

{
  "phone": "+8613800138000",
  "api_config_name": "default",
  "country_code": "CN",
  "country_name": "China",
  "session_string": "1AQAAA..."
}
```

**响应**：

```json
{
  "id": 1,
  "phone": "+8613800138000",
  "status": "online",
  "country_code": "CN",
  "country_name": "China",
  "api_config_name": "default",
  ...
}
```

---

## 方式 2：Session 文件导入

### 流程说明

如果你已经有 Telegram 的 `.session` 文件（通过 Telethon 或其他工具生成），可以直接导入。

### 前端使用

1. 点击"添加账号"按钮
2. 选择"导入 Session 文件"
3. 输入手机号
4. 选择 API 配置
5. 输入国家代码
6. 上传 `.session` 文件
7. 点击"导入"

### API 端点

```http
POST /api/accounts/auth/import-session
Content-Type: multipart/form-data

phone: +8613800138000
api_config_name: default
country_code: CN
country_name: China
session_file: [File: account.session]
```

**响应**：

```json
{
  "code": 0,
  "message": "Session imported successfully",
  "data": {
    "account_id": 1,
    "phone": "+8613800138000",
    "user_id": 123456789,
    "username": "myusername"
  }
}
```

---

## 后端实现细节

### 核心模块

1. **`app/core/account/auth_helper.py`**
   - `TelegramAuthHelper` 类：处理登录流程
   - `send_code()`: 发送验证码
   - `verify_code()`: 验证验证码
   - `verify_2fa()`: 验证 2FA 密码
   - `import_session()`: 导入 session 文件

2. **`app/api/accounts.py`**
   - `/auth/send-code`: 发送验证码端点
   - `/auth/verify-code`: 验证验证码端点
   - `/auth/verify-2fa`: 验证 2FA 端点
   - `/auth/complete-login`: 完成登录端点
   - `/auth/import-session`: 导入 session 端点

3. **`app/core/account/models.py`**
   - `TelegramAccount.session_string`: 存储 Telethon session string

### Session 管理

- **临时 Session**：登录过程中的临时 session 存储在内存中（`TelegramAuthHelper._login_sessions`）
- **持久化 Session**：登录成功后，session string 存储在数据库的 `session_string` 字段
- **Session 过期**：临时 session 5 分钟后自动过期
- **Session 清理**：定期清理过期的临时 session

### 安全考虑

1. **Session String 加密**：建议在生产环境中对 `session_string` 字段进行加密存储
2. **Rate Limiting**：Telegram API 有速率限制，频繁请求会触发 `FloodWaitError`
3. **Session 文件验证**：导入 session 文件时会验证其有效性
4. **临时文件清理**：上传的 session 文件处理后立即删除

---

## 数据库迁移

添加 `session_string` 字段到 `telegram_account` 表：

```bash
cd backend
alembic upgrade head
```

迁移文件：`backend/migrations/versions/001_add_session_string.py`

---

## 常见问题

### 1. 验证码收不到？

- 检查手机号格式是否正确（必须包含国家代码，如 `+86`）
- 确认 Telegram API ID 和 API Hash 配置正确
- 检查是否触发了 Telegram 的速率限制

### 2. 验证码错误？

- 验证码为 5 位数字
- 验证码有效期为 5 分钟
- 如果多次输入错误，需要重新请求验证码

### 3. 2FA 密码错误？

- 确认输入的是 Telegram 的两步验证密码（不是手机锁屏密码）
- 如果忘记密码，需要通过 Telegram 官方应用重置

### 4. Session 文件导入失败？

- 确认文件是 `.session` 格式
- 确认 session 文件未过期
- 确认 API ID 和 API Hash 与生成 session 时使用的一致
- 确认手机号与 session 文件对应的账号一致

### 5. 登录后账号状态为 offline？

- 检查代理配置是否正确
- 检查网络连接
- 查看后端日志获取详细错误信息

---

## 开发调试

### 启动后端

```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 启动前端

```bash
cd frontend
npm run dev
```

### 查看日志

```bash
# 后端日志
docker-compose logs -f backend

# 数据库日志
docker-compose logs -f postgres
```

### 测试 API

使用 Postman 或 curl 测试 API 端点：

```bash
# 发送验证码
curl -X POST http://localhost:8000/api/accounts/auth/send-code \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"phone": "+8613800138000", "api_config_name": "default"}'
```

---

## 参考资料

- [Telethon 文档](https://docs.telethon.dev/)
- [Telegram API 文档](https://core.telegram.org/api)
- [获取 API ID 和 API Hash](https://my.telegram.org/apps)
