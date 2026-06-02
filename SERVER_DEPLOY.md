# Vanguard 服务器部署指南

## 📦 部署包信息

- **文件名**: vanguard_20260524_174907.tar.gz
- **大小**: 64M
- **位置**: /d/tanxuan/project/Vanguard/

## 🚀 部署步骤

### 1. 上传部署包到服务器

```bash
# 在本地执行
scp vanguard_20260524_174907.tar.gz xd:/root/
```

### 2. 登录服务器并解压

```bash
# 登录服务器
ssh xd

# 解压文件
cd /root
tar -xzf vanguard_20260524_174907.tar.gz
cd Vanguard
```

### 3. 配置环境变量

编辑 .env.production 文件，确保所有配置正确。

### 4. 停止旧服务

```bash
docker stop vanguard-backend vanguard-bot vanguard-frontend 2>/dev/null || true
docker rm vanguard-backend vanguard-bot vanguard-frontend 2>/dev/null || true
```

### 5. 启动服务

```bash
# 启动后端和 Bot
docker-compose -f docker-compose.production.yml up -d backend bot

# 检查状态
docker ps | grep vanguard

# 测试后端
curl https://api.rensw.xyz/health

# 启动前端
docker-compose -f docker-compose.production.yml up -d frontend
```

### 6. 验证部署

```bash
# 查看所有容器
docker ps | grep vanguard

# 测试 API
curl https://api.rensw.xyz/health

# 测试前端
curl -I https://www.rensw.xyz
```

## 🔐 系统访问说明

**重要**: 当前项目没有传统的用户登录系统。前端直接连接到后端 API。

## 📅 部署时间

2026-05-24
