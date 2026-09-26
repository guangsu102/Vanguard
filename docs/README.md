# Vanguard 文档索引

本页按用途区分项目文档。架构和运行状态需结合当前代码、部署配置与目标环境复核；带日期的报告只代表采集时点。

## 从这里开始

| 主题 | 文档 | 用法 |
| --- | --- | --- |
| 项目与本地启动 | [项目 README](../README.md) | 结合 Compose、依赖和环境模板 |
| 当前主线 | [主线架构](vanguard-mainline-architecture.md) | 后端、Telegram Worker、XBoard HMAC 协议 |
| 生产发布 | [部署指南](../SERVER_DEPLOY.md)、[运维手册](../OPS_GUIDE.md)、[部署清单](../deploy/deployment-checklist.md) | 迁移与开关须重新核对 |
| 开发约定 | [开发规范](开发规范文档.md)、[仓库指令](../AGENTS.md) | 结合当前代码和测试 |
| XBoard 对接 | [Vanguard API](vanguard-xboard-api.md)、[集成规范](XBOARD_INTEGRATION_SPEC.md) | 使用主线 HMAC 协议 |
| 清理检测 | [文档与无用代码审计](DOCUMENT_AUDIT_2026-09-25.md) | 候选及证据 |

## 需求与设计

- [群运营中心总方案](群运营中心-自建群治理与AI引流-实施计划方案.md)与[阶段 1](群运营中心-阶段1-自建群接入Guardian治理-需求规格.md)、[阶段 2](群运营中心-阶段2-自建群内推广账号AI与模板消息-需求规格.md)、[阶段 3](群运营中心-阶段3-账号级AI性格Persona-需求规格.md)、[阶段 5](群运营中心-阶段5-成员分类与前端整合-需求规格.md)：阶段状态以文件顶部说明及当前实现复核。
- [自建群编排 Phase 1 契约](自建群编排-Phase1-技术契约.md)、[需求确认清单](需求确认清单.md)、[开发计划](开发计划.md)：设计和计划资料，不能推断功能已上线。
- [引流 Bot 需求](Telegram引流Bot_需求文档_v5.0.md)、[守护 Bot 需求](Telegram守护Bot_需求文档_v1.0.md)、[早期技术架构](技术架构文档.md)：原始设计；当前运行分工先看主线架构。
- [NapCat OneBot](napcat-onebot.md)、[账号登录](ACCOUNT_LOGIN.md)：专项说明，接口和开关需按代码复核。
- [入群后广告准入方案](plans/2026-09-21-post-join-ad-eligibility.md)：未跟踪的工作方案，不能直接视为已交付。

## 历史与时间点材料

- [阶段 4 外部群广告规格](群运营中心-阶段4-外部群广告引流到自建群-需求规格.md)已明确废弃，禁止作为开发、迁移或验收依据；保留取消决策。
- [旧 bot-matrix API 文档](接口文档.md)仅供历史对照；[Decodo API 摘录](api接口.txt)是外部接口资料，并非 Vanguard 主线合同。
- [早期账号登录实现总结](IMPLEMENTATION_SUMMARY.md)与根目录部署、修复、验收报告是历史记录。[2026-05-24 部署报告](../DEPLOYMENT_SUCCESS.md)中的旧域名和端口不能作为当前目标。
- [评审记录](reviews/)及[运行和部署报告](reports/)按日期阅读。账号数、开关、端口、队列和发送结果都是当时快照。
- [2026-07-15 广告素材清单](production-ad-creatives-2026-07-15.md)是历史快照。

## 维护约定

新文档应写明状态、日期、适用代码或环境；废弃方案保留取消原因并在本索引注明。运行报告写采集窗口和证据来源。临时部署证据、回滚材料保持原目录。