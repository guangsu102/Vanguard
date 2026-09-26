# 文档与无用代码静态审计（2026-09-25）

## 范围与方法

仅检查本地工作树，不查询生产、不运行 Telegram 业务操作。仓库已有大量未提交的后端、前端、测试与 docs/reports 改动，本次保持原样。“无引用”只是清理候选，不等于安全可删；动态导入、显式部署 profile、人工脚本和历史回滚会产生静态扫描盲区。

- 审计开始时 Markdown 盘点：根目录、docs、frontend、bot-matrix、deploy 共 65 份，其中 docs 42 份；不包含 deploy-artifacts 证据。本次新增两份 Markdown 后，docs 为 44 份。
- docs 内 Markdown 哈希比对没有内容完全相同的文件。相对 Markdown 链接检查为 0 个断链；报告中的 E:/...:行号 本机定位链接未计入，也不具跨机器可移植性。
- 对 backend/app 与 bot-matrix/src 运行 Ruff F401、F811、F841；对 frontend/src/components 的 23 个 Vue 组件扫描运行源码中的组件名引用，排除测试和生成声明。
- 核对后端路由、前端路由、两个 Compose 的 bot profile、Vite 声明输出路径和主线架构说明。

## 结果与清理候选

| 优先级 | 候选 | 证据 | 处理 |
| --- | --- | --- | --- |
| 高 | [阶段 4 需求规格](群运营中心-阶段4-外部群广告引流到自建群-需求规格.md) | 开头明确已废弃，禁止据此开发或建迁移 | 仅保留取消决策，从当前待办和验收依据排除 |
| 高 | [旧部署成功报告](../DEPLOYMENT_SUCCESS.md)、[全面分析报告](../PROJECT_ANALYSIS_REPORT.md)、[最终复核报告](../FINAL_VERIFICATION_REPORT.md) | 均为 2026-05-24 附近快照；旧部署报告使用 api.rensw.xyz 和旧端口，分析报告称文档仅 9 份 | 作为历史资料，不用其中的完成度或地址指导发布 |
| 中 | [旧 bot API 文档](接口文档.md)及 bot-matrix | API 文档和[主线架构](vanguard-mainline-architecture.md)明确标为 legacy；开发 Compose 的 bot 用 legacy-bot profile，生产 Compose 用 legacy-bot-matrix profile | 确认无显式启用环境、外部调用及回滚依赖后再评估移除 |
| 中 | [根目录旧生成声明](../src/auto-imports.d.ts) | 当前 Vite 配置生成到 frontend/src/auto-imports.d.ts 与 frontend/src/components.d.ts；根目录文件未见配置引用 | 后续验证类型检查和构建后可移除 |
| 中 | [早期技术架构](技术架构文档.md)、[开发计划](开发计划.md)、[Decodo 摘录](api接口.txt) | 分别是早期设计、计划及外部厂商资料 | 与主线架构及当前接口合同分开保留 |
| 低 | 一次性 scripts/codex_*.py 与旧入口 deploy-to-xd.sh | 部分脚本仍被文档或测试引用，旧入口提供迁移提示 | 逐个核对人工调用、测试及回滚，不整目录删除 |

## 代码检测明细

- Ruff F401（未使用导入）和 F811（重复定义）为 0；F841 报 1 项：[acquisition/automation.py](../backend/app/modules/acquisition/automation.py) 第 2676 行的 started_at 已赋值但未使用。此文件已有未提交修改，本次未改动。
- 前端 23 个 components/**/*.vue 均有运行源码中的组件名引用。扫描只覆盖静态名称，不能证明页面在生产可达；前端路由文件列出主要页面入口。
- bot-matrix 是 legacy，但可通过显式 Compose profile 启动，不能认定整棵目录为无用代码。主线 Telegram 执行入口是 backend/app/workers/telegram_worker.py。
- 根目录 src/auto-imports.d.ts 是生成声明候选；frontend/src 中当前生成声明不能一起清理。

## 检测边界

本次没有安装或运行 Vulture，也没有动态覆盖率数据。Ruff F 系列和名称引用扫描不能证明函数、类或模块在所有部署模式中不可达。本次未删除文件、未提交或推送、未部署。实际删除前要确认运行 profile、外部调用和回滚依赖，并执行对应类型检查、构建与回归测试。