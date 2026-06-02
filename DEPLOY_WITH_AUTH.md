# Vanguard 完整部署指南（含认证系统）

## 📦 部署包信息

- **最新版本**: vanguard_20260524_175937.tar.gz
- **大小**: 64M

## 🔐 默认登录信息

- **用户名**: `admin`
- **密码**: `admin123`
- **⚠️ 重要**: 首次登录后请立即修改密码！

## 🚀 完整部署步骤

### 1. 上传部署包到服务器

```bash
cd /d/tanxuan/project/Vanguard
scp vanguard_20260524_175937.tar.gz xd:/root/
```

### 2. 登录服务器并解压

```bash
ssh xd
cd /root
mv Vanguard Vanguard.backup.$(date +%Y%m%d) 2>/dev/null || true
tar -xzf vanguard_20260524_175937.tar.gz
cd Vanguard
```

### 3. 配置环境变量

编辑 .env.production，确保 JWT_SECRET 已配置

### 4. 初始化数据库（创建用户表）

```bash
mysql -u vanguard -p vanguard < backend/migrations/009_admin_user.sql
```

### 5. 停止旧服务并启动新服务

```bash
docker stop vanguard-backend vanguard-bot vanguard-frontend 2>/dev/null || true
docker rm vanguard-backend vanguard-bot vanguard-frontend 2>/dev/null || true

docker-compose -f docker-compose.production.yml up -d backend bot
sleep 5
docker-compose -f docker-compose.production.yml up -d frontend
```

### 6. 验证部署

```bash
docker ps | grep vanguard
curl https://api.rensw.xyz/health
curl -I https://www.rensw.xyz
```

### 7. 测试登录

访问 https://www.rensw.xyz，使用 admin/admin123 登录

## 📝 部署检查清单

- [ ] 上传部署包
- [ ] 解压并配置环境变量
- [ ] 执行数据库迁移（创建用户表）
- [ ] 启动所有服务
- [ ] 测试登录功能
- [ ] 修改默认密码

## 📅 部署时间

2026-05-24
