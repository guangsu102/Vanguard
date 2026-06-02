# Vanguard 前端部署问题总结

## 当前状态（2026-05-24）

### ✅ 已完成
1. **前端构建成功** - 修复了Dockerfile配置（从pnpm改为npm）
2. **前端容器运行正常** - 端口3000正常响应
3. **Nginx配置正确** - 外层nginx已配置api.rensw.xyz代理

### ❌ 当前问题
**后端容器持续重启** - CORS_ORIGINS环境变量解析失败

## 问题详情

### 问题1：后端CORS_ORIGINS解析错误

**错误信息：**
```
pydantic_settings.exceptions.SettingsError: error parsing value for field "CORS_ORIGINS" from source "EnvSettingsSource"
```

**原因：**
- `.env.production`中的格式：`CORS_ORIGINS=https://api.rensw.xyz,http://localhost:8000`
- `backend/app/core/config.py`中定义为：`CORS_ORIGINS: list[str]`
- pydantic_settings无法自动将逗号分隔的字符串解析为list

**尝试的解决方案：**
1. ✅ 添加了field_validator来解析逗号分隔的字符串
2. ❌ 但容器仍然报错（可能是代码未正确部署或validator有问题）

### 问题2：环境变量未加载

**现象：**
docker-compose启动时显示所有环境变量为空

**原因：**
- docker-compose默认读取`.env`文件，不会自动读取`.env.production`
- 需要使用`--env-file .env.production`参数

## 已修复的问题

### 1. 前端Dockerfile配置
**问题：** 使用pnpm但服务器只有npm
**解决：** 修改为使用npm ci

### 2. 前端nginx配置
**问题：** 容器内nginx配置了API代理（重复）
**解决：** 简化为只提供静态文件服务

### 3. 网络模式问题
**问题：** 使用host网络模式导致端口80冲突
**解决：** 改为端口映射模式（3000:80, 8000:8000）

### 4. 后端依赖缺失
**问题：** 缺少PyJWT模块
**解决：** 在requirements.txt中添加PyJWT>=2.8.0

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
    ├── vanguard-backend (:8000) - ❌ 重启中
    └── vanguard-frontend (:3000→80) - ✅ 运行正常
```

## 下一步解决方案

### 方案1：简化CORS配置
将CORS_ORIGINS改为str类型，在使用时split

### 方案2：检查validator语法
确保field_validator正确实现

### 方案3：临时使用默认值
注释掉环境变量，使用代码中的默认值测试

## 部署命令

```bash
# 本地打包并上传
./deploy-to-xd.sh

# 服务器上构建并启动
ssh xd
cd /root/Vanguard
docker compose -f docker-compose.production.yml --env-file .env.production up -d --build

# 查看日志
docker logs vanguard-backend -f
docker logs vanguard-frontend -f
```

## 服务器信息

- **服务器IP**: 137.175.65.47
- **SSH别名**: xd
- **部署目录**: /root/Vanguard
- **域名**: api.rensw.xyz
- **共享资源**: PostgreSQL (5432), Redis (6379) - 与XBoard系统共用
