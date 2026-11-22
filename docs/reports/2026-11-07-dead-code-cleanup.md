# 无用代码清理验收（2026-11-07）

## 实际变更

以清理开始时的工作区为基线，删除 37 个文件，修改 21 个文件；新增 23 行、删除 6585 行，净减少 6562 行（不含本报告）。未提交或部署。

- 删除已由 README 标为历史参考的独立 bot-matrix 源码、专属测试和配置，以及 Dockerfile.bot；同步移除开发/生产 Compose 的 legacy Bot 服务、CI 镜像构建和部署打包脚本中的旧引用。
- 删除无人引用且硬编码旧开发机路径、旧域名的 package.sh，以及前端构建范围之外的根目录 src/auto-imports.d.ts。
- 删除三个无运行入口、无导入、无测试引用的后端模块：core/network/proxy_provider.py、modules/acquisition/candidate_preview_guard.py、modules/acquisition/state_guards.py。现用代理供应商客户端和审核、幂等逻辑保留。
- 删除 23 个无调用符号（包括两份未被端点使用的 Pydantic schema、私有包装方法），并清理相应的无用登录会话目录初始化、未使用导入和局部变量。
- 删除养号映射中被后续同名项覆盖的重复 ad_probe 键，清理两个无插值 f-string，并修正局部循环变量遮蔽导入。
- 更新 README、AGENTS.md 和 CLAUDE.md，指向当前后端 Telegram worker。

## 验证

| 检查 | 结果 |
| --- | --- |
| 后端 Python AST 解析 | 通过 |
| Ruff / Pyflakes 全部 F 类规则 | 通过 |
| Compose 和 CI YAML 解析 | 通过 |
| 清理前后隔离导入 API 合同对比 | 345 条路径、94 张已注册表元数据完全一致 |
| 前端类型检查 | 通过 |
| 前端测试 | 64 个测试文件、263 项通过 |
| 前端生产构建 | 通过；保留现有大 chunk 提示 |
| 后端完整 unit 回归尝试 | 1226 项通过、5 项失败后按 maxfail=5 停止；其余未执行 |
| 后端针对性回归（13 个测试文件） | 242 项通过、1 项失败 |
| 清理前代码隔离复跑 | 上述 6 项失败均复现，未归因于本次清理 |
| 清理范围外原有已跟踪文件哈希核对 | 完全一致 |

后端现存失败涉及读取预算归属（4 项）、审核源覆盖记录（1 项）和停止状态下调度票据释放（1 项）。未删除测试，也未把失败业务逻辑当作废弃代码删除。环境中补齐了测试所需 lupa 和 fakeredis；依赖声明没有改动。

## 判定边界

检查覆盖 backend/app、frontend/src、历史 bot-matrix、现用构建和部署引用；私有方法结合 AST 与代码、测试、运维脚本的符号引用检查，删除后进行了二次扫描。框架回调、ORM 注册、数据库迁移、可配置功能、鉴权及实际被调用的安全/预算/审核流程不因默认关闭或静态工具未命中而删除。此结论不声称已穷尽动态运行时的所有冗余。

没有连接生产服务器，没有修改共享 PostgreSQL、Redis、生产认证配置或 Telegram session 数据。原有未提交工作保留；构建自动修改的前端声明文件已按清理前哈希恢复。

## 本地审计证据

- 修改前文件、独立清理补丁和逐项清单：`.tmp/dead-code-cleanup-20261107-000929/`
- 全量测试日志：`.tmp/cleanup-backend-tests.log`
- 针对性测试日志：`.tmp/cleanup-targeted-tests.log`
- 清理前失败复现：`.tmp/cleanup-baseline-tests.log`、`.tmp/cleanup-baseline-p5.log`
- 前端构建日志：`.tmp/cleanup-frontend-build.log`

## 删除的无调用符号

- `backend/app/workers/telegram_worker.py`：`_is_runtime_capable_account`
- `backend/app/workers/telegram_worker.py`：`_resolve_entity_name_from_client`
- `backend/app/api/broadcasts.py`：`BroadcastResponse`
- `backend/app/api/accounts.py`：`ImportSessionRequest`
- `backend/app/modules/acquisition/automation.py`：`_business_stage_limit_multiplier`
- `backend/app/modules/acquisition/automation.py`：`_account_risk_limit_multiplier`
- `backend/app/modules/acquisition/automation.py`：`_recent_group_conversion_count`
- `backend/app/modules/acquisition/automation.py`：`_mark_search_keyword_used`
- `backend/app/modules/acquisition/automation.py`：`_is_already_joined`
- `backend/app/modules/acquisition/automation.py`：`_wait_after_join_verification_action`
- `backend/app/modules/acquisition/automation.py`：`_creative_pool_for_binding_no_generate`
- `backend/app/modules/acquisition/automation.py`：`_ad_time_window_multiplier`
- `backend/app/modules/acquisition/automation.py`：`_count_successful_ads`
- `backend/app/modules/acquisition/automation.py`：`_get_ad_delivery_last_sent_at`
- `backend/app/modules/acquisition/automation.py`：`_is_scheduled_time_due`
- `backend/app/modules/acquisition/automation.py`：`_is_ad_delivery_stop_error`
- `backend/app/modules/acquisition/automation.py`：`_block_group_ads_and_leave`
- `backend/app/modules/acquisition/automation.py`：`_ad_survival_error_is_retryable`
- `backend/app/modules/acquisition/auto_reply/reply_engine.py`：`_ai_enabled`
- `backend/app/modules/acquisition/p5_rollout.py`：`_window_deltas`
- `backend/app/modules/acquisition/private_msg/private_handler.py`：`_ensure_client`
- `backend/app/core/account/auth_helper.py`：`_get_session_path`
- `backend/app/modules/acquisition/automation.py`：`_joined_membership_account_id`
