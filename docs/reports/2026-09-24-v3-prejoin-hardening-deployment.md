# Vanguard v3 证据与入群候选排序部署记录

部署时间：2026-09-24 00:11–00:17 CST。目标：oracle4c24g:/opt/vanguard。公网：https://vanguard.pipenai.xyz。生产后端端口：127.0.0.1:18080。候选验证端口 127.0.0.1:18082 已释放。

## 发布范围

只覆盖 backend/app/modules/acquisition 的 7 个模块：automation.py、candidate_preview.py、group_qualification.py、qualification_actions.py、qualification_ai.py、qualification_service.py、qualification_system_identity.py。包含双普通成员独立广告且各留存 24 小时的试投证据、自家账号身份映射完整性与发送前复核、入群前公开只读排序，以及同文件内经测试的 v3 复核优先级/等待/退出安全修正。未部署前端、数据库迁移或工作区其余未提交改动。

构建包：/opt/vanguard/deploy-candidates/v3-prejoin-hardening-20260923T160000Z/source.tar.gz
SHA256：379336bc83bbb67b8539fb26a9e9fbe678d65cb707d0e4f04f6d5065b38d1268

运行镜像：
- backend：local/vanguard:v3-prejoin-hardening-20260923T160000Z-backend，sha256:baa767e7bf088cae3da115e442cdda4eb4ebbd7cb21da0c568ef272890857c7d
- celery-worker：local/vanguard:v3-prejoin-hardening-20260923T160000Z-celery，sha256:3014f8d4bb2860f6ff9f284751bc98ff6a76b121b86a41b9f3b63f966cb5acab
- telegram-growth-worker：local/vanguard:v3-prejoin-hardening-20260923T160000Z-growth，sha256:ad9fd69142df8a8550541c918756ff8f03d39b5dcd1eb81666c02518d5116c4b

三个候选镜像分别基于部署前正在运行的同名服务镜像，仅叠加上述 7 个模块；镜像内文件摘要与发布清单一致。服务器源码的对应 7 个文件也已对齐；旧文件备份位于 /opt/vanguard/deploy-backups/v3-prejoin-hardening-20260923T160000Z/source-before。

## 验证

本地隔离候选后端与 Worker 测试树均为 525 passed。项目 Ruff 与 Python 编译通过；三个候选镜像在无网络模式下完成 7 文件摘要、编译与模块导入验证。候选后端在 18082 健康运行，群资格业务路由与现役服务同为未认证 401；随后停止临时容器。

生产切换后，三个目标服务均运行上述新镜像；原点和公网 /health 正常，首页 HTTP 200，群资格业务路由未认证 HTTP 401。按错误级别和 traceback 标记复核三个服务近 6 分钟日志，异常数均为 0。Celery Worker 的资格任务成功处理 0 条，广告任务因执行开关关闭而跳过。数据库只读核对：automation.auto_join_scheduler=false、automation.ad_delivery_execution=false、automation.group_qualification=true，最近 15 分钟加群请求 0、广告发送 0。生产尚未设置完整的 system_account_user_ids，试投资格会保持保守阻断；未进行真实 Telegram 加群或广告发送。30 次/日与 48 分钟间隔尚无 v3 真实投放数据验证。

原 test_search.py 全量运行 127 passed、16 failed；失败集中在既有测试 mock 缺少 reservation_key/id 或 db.get 不可 await，与本次发布调用点无关。该套未作为本次发布的绿色门槛，相关 v3 定向测试和现有自动加群主路径已通过。

## 回滚

旧的 backend/celery/growth 镜像标签与镜像 ID 保留。候选和回滚覆盖文件均保留在上述候选目录。需要回滚时先用旧镜像恢复服务：

cd /opt/vanguard
docker compose -f docker-compose.production.yml -f /opt/vanguard/deploy-candidates/v3-prejoin-hardening-20260923T160000Z/rollback.compose.yml up -d --no-build --no-deps backend celery-worker telegram-growth-worker

然后仅在服务器源码仍与本版发布清单一致时运行：

python3 /opt/vanguard/deploy-candidates/v3-prejoin-hardening-20260923T160000Z/rollback_source.py

回滚脚本会先验证现有 7 文件摘要，再恢复 5 个原文件，并删除本版新增的 2 个文件；若文件已被后续发布修改则停止。生产 Compose 原文件备份位于候选目录的 docker-compose.production.before.yml。后续如需从源码重建，应先核对服务器上其他已暂存但不属于本版的改动，不应把普通 build 当作本版的同内容重放。

## 后续只读采证补丁

2026-09-24（北京时间），生产三个 Vanguard 镜像在本版基础上仅叠加 group_qualification.py 的广告回复线程定向补证修复；当前运行镜像、备份、回滚和 #2 群 522／173 的新审核结果以 [PP-AI 动态增长续报](pp-ai-dynamic-growth-20260923.md) 18:39 UTC 小节为准。本节原镜像仍保留为回滚基底。


## 后续规则身份与租约重试补丁

2026-09-24（北京时间）又做两次仅限 Vanguard 的单模块发布：pp-ai-rule-identity-20260923T192800Z 与 pp-ai-lease-retry-20260923T195700Z。当前生产镜像、SHA、备份、回滚、群 10／2／11 的定向审核及实际动作计数，以 [PP-AI 动态增长续报](pp-ai-dynamic-growth-20260923.md) 20:01 UTC 小节为准。v3 双普通成员各留存 24 小时、双审各 95 分和原广告入口护栏未变。