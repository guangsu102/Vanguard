# Vanguard 部署成功报告

## 部署时间
2026-05-24 22:32

## ✅ 部署状态

### 前端服务
- **状态**: ✅ 运行正常
- **容器**: vanguard-frontend
- **端口**: 3000:80
- **健康状态**: healthy
- **访问地址**: https://api.rensw.xyz

### 后端服务
- **状态**: ✅ 运行正常
- **容器**: vanguard-backend
- **端口**: 8000:8000
- **健康状态**: healthy
- **API地址**: https://api.rensw.xyz/api
- **健康检查**: https://api.rensw.xyz/health
- **响应**: `{"status":"healthy","version":"1.0.0"}`

## 修复的问题

### 1. 前端Dockerfile配置
- **问题**: 使用pnpm但服务器只有npm
- **解决**: 修改为使用`npm ci`

### 2. 前端nginx配置
- **问题**: 容器内nginx重复配置API代理
- **解决**: 简化为只提供静态文件服务

### 3. Docker网络模式
- **问题**: host网络模式导致端口80冲突
- **解决**: 改为端口映射模式

### 4. 后端依赖缺失
- **问题**: 缺少PyJWT模块
- **解决**: 在requirements.txt中添加`PyJWT>=2.8.0`

### 5. CORS_ORIGINS配置
- **问题**: pydantic_settings无法解析逗号分隔的字符串为list
- **解决**: 
  - 将CORS_ORIGINS改为str类型
  - 添加`cors_origins_list` property进行解析
  - 修改main.py使用新的property

### 6. AccountPool导入错误
- **问题**: main.py从错误的模块导入AccountPool
- **解决**: 修改为从`app.core.account`导入

## 服务器架构

```
外部访问 (https://api.rensw.xyz)
    ↓
系统Nginx (:80/:443)
    ├── /api/* → localhost:8000 (后端API)
    ├── /ws/* → localhost:8000 (WebSocket)
    └── /* → localhost:3000 (前端静态文件)
        ↓
Docker容器
    ├── vanguard-backend (:8000) - ✅ 运行正常
    └── vanguard-frontend (:3000→80) - ✅ 运行正常
```

## 验证结果

```bash
# 后端健康检查
$ curl https://api.rensw.xyz/health
{"status":"healthy","version":"1.0.0"}

# 前端访问
$ curl -I https://api.rensw.xyz
HTTP/1.1 200 OK

# 容器状态
$ docker ps | grep vanguard
vanguard-frontend   Up (healthy)   0.0.0.0:3000->80/tcp
vanguard-backend    Up (healthy)   0.0.0.0:8000->8000/tcp
```

## 部署命令

```bash
# 本地打包并上传
./deploy-to-xd.sh

# 服务器上构建并启动
ssh xd
cd /root/Vanguard
docker compose -f docker-compose.production.yml --env-file .env.production down
docker compose -f docker-compose.production.yml --env-file .env.production build backend frontend
docker compose -f docker-compose.production.yml --env-file .env.production up -d backend frontend
```

## 服务器信息

- **服务器IP**: 137.175.65.47
- **SSH别名**: xd
- **部署目录**: /root/Vanguard
- **域名**: api.rensw.xyz
- **共享资源**: PostgreSQL (5432), Redis (6379) - 与XBoard系统共用
