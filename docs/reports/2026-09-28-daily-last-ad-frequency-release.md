# 单群 24 小时周期频率策略发布

已提交、推送并部署。北京时间 2026-09-28 21:49:44 完成生产切换；业务核验时间为 21:51:56。

## Git 与生产版本

| 项目 | 值 |
| --- | --- |
| Git 分支 | `codex/daily-group-frequency` |
| 已上线前置修复的基线提交 | `02ce244d88f41b39e76dcee37964da4d0c44c78c` |
| 本次功能提交及生产应用 SHA | `adb25f5d25d8a5151048e9fe31c706127f63a95c` |
| 目标 | `oracle4c24g:/opt/vanguard` |
| 后端镜像 | `local/vanguard:daily-frequency-c06c313a29f9` |
| 后端镜像 ID | `sha256:00df39737ef9799e087aaf34f5e25e59bc6693790ad8e8b4ecdb695b7684fd88` |
| 前端镜像 | `local/vanguard:daily-frequency-c06c313a29f9-frontend` |
| 前端镜像 ID | `sha256:e583a4b185bd95fc96fb947225f1523b6a51dc0ac25e304d4bc0f8c1ebe2b286` |
| 源码包 SHA256 | `c06c313a29f9378b70299fe4ce38232bd58a963ea93696e7029c7173a76eadfa` |
| 前端产物 SHA256 | `755de2fd23b473baf2712d14140916107169a16d75d2590d53eb1b7f870ed611` |
| 后端 / 前端监听 | `127.0.0.1:18080` / `127.0.0.1:13000` |
| 网站 | `https://vanguard.pipenai.xyz` |

使用独立的受管理工作区制作提交，原工作区的并发修改保留。前置基线与上线源码快照逐文件核对，本次功能源自已推送的 Git 提交。

## 生效规则

- 首次实际成功发送起算 24 小时。周期结束时复核该周期最后一条成功广告；首次广告单独存活 24 小时即可升级。
- 存活时按 `1 → 2 → 4 → 8 → 16 → 30` 调整单群额度。后续周期最后一条消息可以不足 24 小时。
- 确认消息消失后额度减半，最低为 1；已经为 1 时再次确认消失，进入已有退群流程。受保护群、权限和退群结果未决等原有约束继续生效。
- 读取失败、预算等待或发送结果未决时保持额度，等待复核。连续两个安全读取确认消息缺失，间隔至少 120 秒，才执行减额。
- 单条消息原有存活检查继续记录证据；每天的额度调整由共享群状态、周期、租约和回调令牌约束，防止并发重复升级。

## 验证结果

- 在生产相同依赖环境中完成 242 项相关回归；前端类型检查、构建和定向 Ruff 检查通过。
- 隔离的生产 PostgreSQL 副本应用 `064_daily_group_frequency.sql`，新增 7 个字段和到期索引；再次执行跳过已应用迁移。迁移记录保存在生产使用的 `schema_migration_history`。
- 真实 PostgreSQL 验证了完整额度路径、周期最后一条消息仅 12 小时仍可复核、8 减至 4、最低额度进入退群、未知读取保持额度、两个并发复核只产生一个领取，以及旧回调失效。
- 旧生产镜像可读取新增字段后的数据库，保留原合格群资格，可进行应用回滚。
- 六个后端角色的运行源码均与发布清单一致；各角色重启计数为 0。公网健康、首页、入口资源为 200，未登录访问容量 API 为 401，116 个前端产物文件哈希一致。
- 发布后日志抽检未发现 traceback、critical、监听致命错误、暂停错误或 PostgreSQL 死锁。
- 生产保留 30 个合格群；核验时可用群为 27，身份阻塞为 1。账号 3 的隔离状态保留。
- 业务开关摘要仍为 `ed4b9784551f5852ad86d2a1cd4253683b9398d547e0c09e83fe153fa820c479`。未决发送、重复回执和成功缺少消息 ID 均为 0；累计成功 109 条，最近成功日志为 768。
- 任务切换前活动及预留任务均为 0；原始监听队列与检查点摘要在切换前后相同，4 个冻结监听容器保持原 ID 及暂停状态。
- 隔离验收容器、网络及临时环境文件已经清理，生产备份和候选镜像保留。部署范围为 Vanguard 的六个后端角色与前端。

业务验证边界：截至 21:51:56，生产已加载 `daily_last_message` 策略，尚未产生 `daily_*` 周期调整事件；真实下一次到期复核仍待执行。完整增长路径已通过隔离 PostgreSQL 验证。

## 备份与回滚

- 回滚镜像：`local/vanguard:established-ad-96bb13461be6`，前端对应 `-frontend` 标签。
- 备份目录：`/opt/vanguard/deploy-backups/daily-frequency-c06c313a29f9`，包含数据库、配置、源码和原始监听队列备份。
- 数据库备份 SHA256：`41fcf72620190fbed40b82dbe82ce07da5ad1b5305fb342d9af06ff03c52d353`。
- 应用回滚入口：

```powershell
ssh oracle4c24g "python3 /opt/vanguard/deploy-candidates/daily-frequency-c06c313a29f9/release_ops.py rollback"
```

回滚先检查监听和任务状态并排空消费者，再恢复应用镜像及文件；保留生产数据库、新增列、发送记录和预算。监听已经联网或存在在途操作时，脚本会停止切换，需先完成相应交接。

## 发布证据

本地目录：`deploy-artifacts/daily-last-ad-frequency-20260928/`。

- `candidate.json`、`source-manifest.json`：已提交源码、功能范围和构建清单。
- `validation-state.json`、`regression.log`：生产依赖环境中的回归结果。
- `candidate-checks.log`、`rollback-compatibility.json`、`migration-idempotence.log`：真实 PostgreSQL 与应用回滚兼容验证。
- `candidate-ready.json`、`cutover-complete.json`、`production-migration.log`：备份、迁移与切换结果。
- `verification.json`、`postcheck.json`、`worker-check.json`、`telegram-heartbeats.json`：代码、路由、日志、业务和 worker 核验。
- `cleanup-launch.log`：隔离验收资源清理结果。

验收准备时修正了迁移命令的 Python 模块路径；发布后只读统计脚本修正了事件字段名称。这两项均为发布工具修正，应用源码保持已推送的功能提交。
