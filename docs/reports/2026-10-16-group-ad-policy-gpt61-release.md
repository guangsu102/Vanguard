# 群规 AI 审核模型路由修复（2026-10-16）

## 根因

生产全局配置固定了 `LLM_MODEL_OVERRIDE=gpt-6.1-sol`，但 `automation.ad_capacity` 仍保存 `glm-5.3-flash`，群规审核又读取了 Ark Coding endpoint。全局 override 在请求前把显式模型替换成 `gpt-6.1-sol`，而 Ark Coding endpoint 不支持该模型，因此持续返回 `UnsupportedModel` 404；旧镜像中的失败日志可以复现这一点。

## 修复

- 群规审核默认使用共享 OpenAI-compatible endpoint，并与生产全局模型统一为 `gpt-6.1-sol`。
- 旧 Ark 专用 endpoint 改为显式 opt-in：只有 `GROUP_AD_POLICY_DEDICATED_ENABLED=true` 才会使用；专用客户端不会再接受全局模型 override。
- 生产启动时，显式启用专用 endpoint 必须同时提供 key 和 URL，避免半配置静默降级。
- 迁移 `066_unify_group_ad_policy_model.sql` 将已知旧值（含 snake_case 和 camelCase 配置键）统一为 `gpt-6.1-sol`，保留管理员自定义模型。
- 前端、API、运行时默认值同步更新，新增模型 override 和专用路由回归测试。

## 生产发布

- 目标：`oracle4c24g`，canonical Compose：`/opt/shared-postgres/stacks/vanguard/compose.json`。
- 后端镜像：`local/vanguard:ai-policy-gpt61-20261016T1625Z`；前端镜像：`local/vanguard:ai-policy-gpt61-20261016T1625Z-frontend`。
- 更新 backend、celery-worker、celery-beat、resource-search-worker、telegram-growth-worker-survival-continuity、telegram-guardian-worker 和 frontend；六个后端服务显式设置 `GROUP_AD_POLICY_DEDICATED_ENABLED=false`。
- 已执行迁移 066；数据库中的 `automation.ad_capacity.ad_policy_ai_model` 已为 `gpt-6.1-sol`，AI 审核开关仍为启用。
- 共享 PostgreSQL、共享网关和 Vanguard Redis 未重建；发布前后这些关键容器 ID 一致。
- 服务器端回退文件保存在 `/opt/vanguard.file-backups/ai-policy-gpt61-20261016-1625`。

## 验收

- 本地群规审核相关后端测试：104 passed；前端默认配置测试：4 passed；目标 Python 编译检查通过。
- 六个新容器逐一确认：专用路由关闭、endpoint 为 `https://pipenai.xyz/v1`、effective model 为 `gpt-6.1-sol`。
- 真实 provider 烟测返回 `OK`。
- 使用完整群规审核路径运行合成规则证据，返回 `approval_required`、置信度 98、`decision_source=gpt-6.1-sol`。
- 发布后约 20 分钟无 `UnsupportedModel`、`group_ad_policy_ai_failed` 或 traceback；公网 `/health` 返回 `{"status":"healthy","version":"1.0.0"}`。
- 前端新容器已通过 HTTP 首页和容器 healthcheck，所有 Vanguard 应用容器重启计数为 0。
