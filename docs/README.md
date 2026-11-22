# Vanguard 文档索引

当前架构与接口以主线文档和代码为准；部署以目标服务器的运行时 Compose 为准。带日期的报告只代表采集时点，不代表当前状态。

## 开发与运维

| 主题 | 文档 |
| --- | --- |
| 项目与本地启动 | [项目 README](../README.md) |
| 运行架构 | [主线架构](vanguard-mainline-architecture.md) |
| 生产发布 | [Oracle US 部署指南](../SERVER_DEPLOY.md) |
| 部署历史 | [XD 部署记录（主机已到期）](XD_DEPLOYMENT.md)、[早期 Oracle 共享 PostgreSQL 记录](ORACLE_SHARED_POSTGRES.md) |
| 日常运维 | [运维手册](../OPS_GUIDE.md)、[故障排查](../TROUBLESHOOTING.md)、[部署清单](../deploy/deployment-checklist.md) |
| 开发约定 | [开发规范](开发规范文档.md)、[仓库指令](../AGENTS.md) |
| 管理员与 Telegram 登录 | [认证说明](../AUTH_GUIDE.md)、[账号登录](ACCOUNT_LOGIN.md) |
| XBoard 对接 | [Vanguard API](vanguard-xboard-api.md)、[集成规范](XBOARD_INTEGRATION_SPEC.md) |
| QQ 接入 | [NapCat OneBot](napcat-onebot.md)、[QQ 增长自动化](qq-growth-automation.md) |
| 监控与发布控制 | [P0 监控手册](P0_MONITORING_RUNBOOK.md)、[P5 发布手册](P5_ROLLOUT_RUNBOOK.md)、[P0–P6 影响审计](P0_P6_IMPACT_AUDIT.md) |

## 需求与设计

- [群运营中心总方案](群运营中心-自建群治理与AI引流-实施计划方案.md)及[阶段 1](群运营中心-阶段1-自建群接入Guardian治理-需求规格.md)、[阶段 2](群运营中心-阶段2-自建群内推广账号AI与模板消息-需求规格.md)、[阶段 3](群运营中心-阶段3-账号级AI性格Persona-需求规格.md)、[阶段 5](群运营中心-阶段5-成员分类与前端整合-需求规格.md)：交付状态以正文、代码和验收证据核对。
- [自建群编排契约](自建群编排-Phase1-技术契约.md)、[需求确认清单](需求确认清单.md)、[开发计划](开发计划.md)：保留需求依据，历史计划不能推断功能已上线。
- [引流 Bot 需求](Telegram引流Bot_需求文档_v5.0.md)、[守护 Bot 需求](Telegram守护Bot_需求文档_v1.0.md)：原始产品需求，当前运行分工见主线架构。
- [专项方案目录](plans/)：实施计划与灰度手册，需分别核对状态。

## 已取消范围

群运营中心阶段 4 已于 2026-09-11 取消独立开发，旧规格正文已删除。外部群广告引流由现有素材 `link_url` / `{{link_url}}` 满足；不得恢复原方案的 `052/053/054` SQL、`038/039/040` Alembic 迁移及阶段 4 API、Worker、状态机或开关。未来如需链接轮换、目标失效联动或按自建群归因，须另行界定需求。自建群内部 AI/模板消息不受该取消决定影响。

## 验证与历史证据

- [运行和部署报告](reports/)保留故障分析、发布结果和可追溯证据；历史账号数、开关和镜像不能作为当前配置使用。
- [最近代码清理验收](reports/2026-11-07-dead-code-cleanup.md)记录删除范围和已有测试失败。
- [Decodo API 摘录](api接口.txt)属于外部接口资料，不是 Vanguard 的接口合同。

维护文档时优先更新已有指南；被替代的操作说明和重复快照可删除，必要的取消决定与约束保留在对应主文档中。生产配置与临时审计证据不写入公共文档。
