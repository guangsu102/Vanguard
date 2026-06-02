#!/bin/bash
# Vanguard 项目打包脚本

echo "开始打包 Vanguard 项目..."

# 项目根目录
PROJECT_ROOT="/d/tanxuan/project/Vanguard"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PACKAGE_NAME="vanguard_${TIMESTAMP}.tar.gz"

cd "$PROJECT_ROOT"

# 创建临时目录
TEMP_DIR="/tmp/vanguard_package"
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR/Vanguard"

echo "复制项目文件..."

# 复制后端文件
cp -r backend "$TEMP_DIR/Vanguard/"
cp -r bot-matrix "$TEMP_DIR/Vanguard/"

# 复制前端文件（包括 dist）
cp -r frontend "$TEMP_DIR/Vanguard/"

# 复制配置文件
cp docker-compose.production.yml "$TEMP_DIR/Vanguard/"
cp Dockerfile.backend "$TEMP_DIR/Vanguard/"
cp Dockerfile.bot "$TEMP_DIR/Vanguard/"
cp .env.production "$TEMP_DIR/Vanguard/" 2>/dev/null || echo "警告: .env.production 不存在"

# 复制文档
cp DEPLOYMENT.md "$TEMP_DIR/Vanguard/" 2>/dev/null || true
cp README.md "$TEMP_DIR/Vanguard/" 2>/dev/null || true

# 创建部署说明
cat > "$TEMP_DIR/Vanguard/DEPLOY_INSTRUCTIONS.md" << 'EOF'
# Vanguard 部署说明

## 1. 解压文件
```bash
cd /root
tar -xzf vanguard_*.tar.gz
cd Vanguard
```

## 2. 配置环境变量
编辑 .env.production 文件，确保所有配置正确

## 3. 构建并启动服务
```bash
# 启动后端和 Bot
docker-compose -f docker-compose.production.yml up -d backend bot

# 启动前端
docker-compose -f docker-compose.production.yml up -d frontend
```

## 4. 检查服务状态
```bash
docker ps | grep vanguard
docker logs vanguard-backend
docker logs vanguard-bot
docker logs vanguard-frontend
```

## 5. 访问服务
- 后端 API: https://api.rensw.xyz
- 前端: https://www.rensw.xyz (需要配置 Nginx)
EOF

echo "打包文件..."
cd "$TEMP_DIR"
tar -czf "$PACKAGE_NAME" Vanguard/

# 移动到项目根目录
mv "$PACKAGE_NAME" "$PROJECT_ROOT/"

# 清理临时目录
rm -rf "$TEMP_DIR"

echo "打包完成: $PROJECT_ROOT/$PACKAGE_NAME"
echo "文件大小: $(du -h "$PROJECT_ROOT/$PACKAGE_NAME" | cut -f1)"
