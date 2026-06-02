# Vanguard 认证系统说明

## 🔐 登录信息

### 默认管理员账号

- **用户名**: `admin`
- **密码**: `admin123`
- **角色**: 管理员 (admin)

⚠️ **重要**: 首次登录后请立即修改密码！

## 📋 认证功能

### 1. 用户登录
- **接口**: `POST /api/auth/login`
- **请求**: username + password
- **响应**: token + user info

### 2. 获取用户信息
- **接口**: `GET /api/auth/user`
- **请求头**: `Authorization: Bearer <token>`

### 3. 用户登出
- **接口**: `POST /api/auth/logout`

### 4. 修改密码
- **接口**: `PUT /api/auth/password`

## 🔧 部署后初始化

### 方法 1: 使用数据库迁移脚本（推荐）

```bash
cd /root/Vanguard
mysql -u vanguard -p vanguard < backend/migrations/009_admin_user.sql
```

### 方法 2: 使用 Python 脚本

```bash
docker exec -it vanguard-backend python /app/scripts/create_admin.py
```

## 🛡️ 安全说明

### JWT Token
- **有效期**: 7 天
- **算法**: HS256
- **密钥**: 配置在 .env.production 的 JWT_SECRET

### 密码加密
- **算法**: bcrypt
- **强度**: 12 rounds

### 角色权限
- **admin**: 管理员，拥有所有权限
- **operator**: 操作员，可以管理日常操作
- **viewer**: 查看者，只读权限

## 📝 使用流程

1. 用户访问 https://www.rensw.xyz
2. 自动跳转到登录页面
3. 输入用户名和密码
4. 登录成功后获得 JWT token
5. Token 存储在 localStorage
6. 后续请求自动携带 token

## 🔄 修改默认密码

登录后进入"系统设置" → "修改密码"

## 📅 更新时间

2026-05-24
