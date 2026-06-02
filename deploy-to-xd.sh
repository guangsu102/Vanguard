#!/bin/bash
# Vanguard 部署脚本 - 部署到 xd 服务器

set -e

echo "=========================================="
echo "Vanguard 部署脚本"
echo "=========================================="

# 1. 打包项目
echo ""
echo "步骤 1: 打包项目..."
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PACKAGE_NAME="vanguard_${TIMESTAMP}.tar.gz"

tar -czf "$PACKAGE_NAME" \
    --exclude='.git' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='frontend/node_modules' \
    --exclude='frontend/dist' \
    --exclude='*.tar.gz' \
    backend/ \
    bot-matrix/ \
    frontend/ \
    docker-compose.production.yml \
    Dockerfile.backend \
    Dockerfile.bot \
    .env.production \
    README.md \
    DEPLOYMENT.md

echo "✓ 打包完成: $PACKAGE_NAME"

# 2. 上传到服务器
echo ""
echo "步骤 2: 上传到服务器..."
scp "$PACKAGE_NAME" xd:/root/

echo "✓ 上传完成"

# 3. 在服务器上解压并部署
echo ""
echo "步骤 3: 在服务器上部署..."
ssh xd << 'ENDSSH'
cd /root

# 解压
LATEST_TAR=$(ls -t vanguard_*.tar.gz | head -1)
echo "解压 $LATEST_TAR..."

# 备份旧版本
if [ -d "Vanguard" ]; then
    echo "备份旧版本..."
    mv Vanguard Vanguard.backup.$(date +%Y%m%d_%H%M%S)
fi

# 创建新目录并解压
mkdir -p Vanguard
tar -xzf "$LATEST_TAR" -C Vanguard/

cd Vanguard

echo "✓ 解压完成"
ENDSSH

echo ""
echo "=========================================="
echo "部署完成！"
echo "=========================================="
echo ""
echo "下一步操作："
echo "1. ssh xd"
echo "2. cd /root/Vanguard"
echo "3. docker-compose -f docker-compose.production.yml up -d --build"
echo ""
