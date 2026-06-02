# 部署说明 (Deployment Instructions)

## 问题修复 (Bug Fixes)

已修复以下问题：
1. **TypeError: Cannot read properties of undefined (reading 'toUpperCase')** - 在 Proxies.vue 中添加了空值检查
2. **Passive event listener warning** - 这是 Element Plus 的已知问题，不影响功能

## 部署到 xd 服务器 (Deploy to xd Server)

### 方法 1: 使用部署脚本 (Using Deployment Script)

```bash
# 1. 确保你在 frontend 目录
cd /d/tanxuan/project/Vanguard/frontend

# 2. 给脚本添加执行权限
chmod +x deploy.sh

# 3. 运行部署脚本
./deploy.sh
```

### 方法 2: 手动部署 (Manual Deployment)

```bash
# 1. 上传 dist.tar.gz 到服务器
scp dist.tar.gz root@xd:/tmp/

# 2. SSH 登录到服务器
ssh root@xd

# 3. 备份现有部署（如果存在）
sudo mv /var/www/vanguard/frontend /var/www/vanguard/frontend.backup.$(date +%Y%m%d_%H%M%S)

# 4. 创建目录
sudo mkdir -p /var/www/vanguard/frontend

# 5. 解压文件
sudo tar -xzf /tmp/dist.tar.gz -C /var/www/vanguard/frontend

# 6. 设置权限
sudo chown -R www-data:www-data /var/www/vanguard/frontend
sudo chmod -R 755 /var/www/vanguard/frontend

# 7. 清理临时文件
rm /tmp/dist.tar.gz

# 8. 重新加载 nginx
sudo nginx -t && sudo systemctl reload nginx
```

## 验证部署 (Verify Deployment)

1. 打开浏览器访问你的应用
2. 进入"代理管理"页面
3. 确认不再出现 JavaScript 错误
4. 检查协议列显示正常（HTTP/HTTPS/SOCKS5）

## 文件说明 (Files)

- `dist.tar.gz` - 编译后的前端文件压缩包 (801KB)
- `deploy.sh` - 自动部署脚本
- `src/views/Proxies.vue` - 已修复的代理管理页面

## 注意事项 (Notes)

- 部署前会自动备份现有文件
- 确保 nginx 配置正确指向 `/var/www/vanguard/frontend`
- 如果遇到权限问题，请使用 sudo 执行命令
