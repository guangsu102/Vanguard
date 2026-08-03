# ===========================================
# Vanguard 生产部署检查清单
# ===========================================

## 部署前检查 (Pre-Deployment)

### 代码审查
- [ ] 所有 PR 已通过代码审查
- [ ] 主分支已与最新代码同步
- [ ] 版本号已更新

### 测试验证
- [ ] 所有单元测试通过 (`pytest tests/unit/ -v`)
- [ ] 所有集成测试通过 (`pytest tests/integration/ -v`)
- [ ] 安全测试通过
- [ ] 性能基准测试达标

### 安全检查
- [ ] 敏感信息未提交到代码库
- [ ] 依赖包无已知漏洞 (`pip audit` 或 `trivy`)
- [ ] SSL 证书已配置
- [ ] API 密钥已轮换（如需要）

### 配置检查
- [ ] `.env.production` 已正确配置
- [ ] 数据库连接配置正确
- [ ] Redis 配置正确
- [ ] Telegram API 凭证有效
- [ ] CORS 配置正确

### Sub2API 对接检查
- [ ] 已在 Sub2API 后台轮换 Admin API Key；本次排查中使用过的旧 Key 不再继续使用
- [ ] Vanguard 已配置 `SUB2API_ENABLED=true`、`SUB2API_BASE_URL=https://api.pipenai.xyz`、新 `SUB2API_ADMIN_API_KEY` 和合理的 `SUB2API_TIMEOUT`
- [ ] 已生成独立的 32 字符以上随机 HMAC Secret，Vanguard 的 `SUB2API_ALERT_WEBHOOK_SECRET` 与 Sub2API 的 `VANGUARD_WEBHOOK_SECRET` 完全一致
- [ ] Sub2API 已配置 `VANGUARD_WEBHOOK_BASE_URL=https://www.dh113.xyz/api/integrations/sub2api`、稳定且唯一的 `VANGUARD_WEBHOOK_INSTANCE_ID`、正确的 `VANGUARD_WEBHOOK_SOURCE_URL`
- [ ] Sub2API 已配置 `OPS_ENABLED=true`，并已启用需要转发的告警规则
- [ ] 两台服务器时间已通过 NTP 同步，时钟偏差小于 `SUB2API_ALERT_TIMESTAMP_TOLERANCE`（默认 300 秒）
- [ ] Vanguard 通知设置已明确：是否开启 Sub2API 告警/恢复通知、公告通知、Telegram/QQ 目标群
- [ ] Telegram Bot 在目标群具备发消息权限；启用公告置顶时同时具备置顶权限
- [ ] NapCat 专用 QQ 账号已加入目标群，OneBot HTTP/WS 与强 Token 已配置（仅在启用 QQ 通知时需要）
- [ ] 定时公告按当前限制采用“到发布时间再激活/更新”的操作方式；未来 `starts_at` 不依赖 webhook 自动补发

---

## 部署执行 (Deployment)

### 备份
1. [ ] 数据库已备份
2. [ ] 配置文件已备份
3. [ ] 上传文件已备份（如适用）

### 部署步骤
1. [ ] 先备份数据库，并暂停所有 Sub2API 发券活动
2. [ ] 立即轮换 Sub2API Admin API Key，把新 Key 同步到 Vanguard 后重建后端/worker，旧 Key 确认失效
3. [ ] 发布 Sub2API 兑换码幂等修复和 webhook 代码，但保持 `VANGUARD_WEBHOOK_ENABLED=false`
4. [ ] 执行 Vanguard Alembic/SQL 迁移
5. [ ] 发布 Vanguard 后端并确认 `/api/integrations/sub2api/alerts` 与 `/announcements` 不再返回 404
6. [ ] 在 Sub2API 配置共享 HMAC Secret，设置 `VANGUARD_WEBHOOK_ENABLED=true` 后重建服务
7. [ ] 验证新字段和表
8. [ ] 重启相关 worker，再恢复 Sub2API 发券活动
9. [ ] 健康检查通过

### 验证
1. [ ] 健康检查端点正常
2. [ ] API 响应正常
3. [ ] WebSocket 连接正常
4. [ ] Telegram Bot 响应正常
5. [ ] 新字段可读写
6. [ ] 新表可查询
7. [ ] 使用固定 `Idempotency-Key` 连续请求两次 Sub2API 兑换码生成接口，第二次响应标记 replay 且返回同一个非 `***` 兑换码
8. [ ] 触发一条测试告警及恢复事件，Vanguard 目标群各收到一次且 Sub2API 无 webhook 失败日志
9. [ ] 发布一条当前生效的公开测试公告，确认目标群只收到一次；随后归档测试公告
10. [ ] 创建定向公告，确认不会转发到 Vanguard 公共群

---

## 部署后检查 (Post-Deployment)

### 监控检查
- [ ] Grafana 仪表盘可访问
- [ ] 错误率在正常范围
- [ ] 响应时间正常
- [ ] 数据库连接正常
- [ ] Redis 连接正常

### 功能检查
- [ ] 用户注册流程正常
- [ ] 消息收发正常
- [ ] 违规检测正常
- [ ] 惩罚执行正常

### 告警检查
- [ ] 无关键告警
- [ ] 告警通道畅通
- [ ] 值班人员已通知

---

## 回滚准备 (Rollback)

### 如需回滚
1. 恢复数据库备份
2. 回退 Alembic 到上一个版本
3. 回滚后端发布
4. 验证健康检查
5. 通知相关人员

### 回滚后
- [ ] 确认旧版本运行正常
- [ ] 通知支持团队
- [ ] 记录回滚原因
- [ ] 创建回滚事件报告

---

## 签署确认

| 角色 | 姓名 | 日期 | 签名 |
|------|------|------|------|
| 开发负责人 | | | |
| 运维负责人 | | | |
| 产品负责人 | | | |
| 测试负责人 | | | |
