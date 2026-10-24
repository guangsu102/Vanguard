# 首轮广告日核验漏选修复发布

发布时间：北京时间 **2026-10-12 07:36:36**。本次只替换 `app/modules/acquisition/daily_frequency.py`，通过 `/opt/shared-postgres/stacks/vanguard/compose.json` 的六个现有应用服务增量更新。没有数据库迁移、人工批准资格、清零预算或主动发送广告。

## 修复内容

原调度在指定账号时通过 `group_ad_frequency_event -> ad_delivery_log.account_id` 找群。一个群在首轮日核验前没有事件，因此即使有成功发送回执、日核验已经到期，也永远不会进入候选。

现在候选查询以到期的 `GroupAdFrequency` 为主，并以该账号在当前周期内的真实成功发送回执发现首轮群；支持 canonical Telegram peer ID 与原始 ID 别名。`DailyFrequencyService.claim()` 仍然按周期最后一条广告重新确认账号、群身份、授权配置和租约，所以候选发现不会绕过既有安全校验。账号量子超过首批候选时，查询按到期时间和群键稳定翻页，避免其他账号的最后发送记录占满批次。

## 验证

- 145 项相关单元测试通过，其中新增的首轮日核验、多账号最后发送者、身份别名、读取额度重试、消息缺失和批次翻页场景 8 项全部通过；修复前新增场景中 6 项稳定复现漏选。
- Ruff 检查通过，离线 smoke 通过。
- 线上 PostgreSQL READ ONLY 预检：账号 33 选出 `-1001310519636`（704），账号 34 选出 `-1001395018123`（794），查询执行约 0.1ms。
- 发布六个服务均运行，backend healthy，公网 `/health` 返回 200；源代码哈希、Env/Cmd/Entrypoint 均校验一致；36 个非目标容器保持原 ID、镜像和启动时间。共享 PG18、网关、Redis、前端均保持 healthy。

## 线上结果

北京时间 **07:40:24**，33/704 首轮日核验成功：广告仍存在、账号仍在群内且可发言，周期从 `quota=1 / mature=false` 提升为 `quota=2 / mature=true`。

北京时间 **07:41:01**，账号 33 对704产生新成功广告回执 `telegram_message_id=56176148`，说明修复后的调度已实际恢复投放。

北京时间 **07:42:31**，34/794 首轮日核验成功，同样提升为 `quota=2 / mature=true`。该群仍有真实来源的 `gap`、`rules` 资格事件，所以资格门禁继续阻挡，不能把日核验成功误报为该群已经可投放。

最终采样（北京时间 **07:43:30**）：账号 33 `qualified=1 / usable=1`，704 的 gate 为 `ready`；账号 34 `qualified=2 / usable=2`，738、586 等待每日频率，794 仍为真实资格事件阻挡；账号 35 `qualified=3 / usable=3`，三个群等待每日频率，但 `ad_refresh` 本轮已用尽，预计按额度窗口恢复。暖机策略仍为 0 天。账号 2 继续有成功广告，账号 4 自动广告关闭。

## 文件

- 代码：[daily_frequency.py](/E:/project/Vanguard/backend/app/modules/acquisition/daily_frequency.py)
- 回归测试：[test_daily_frequency_account_dispatch.py](/E:/project/Vanguard/backend/tests/unit/test_daily_frequency_account_dispatch.py)
- 发布证据目录：[daily-frequency-20261012](/E:/project/Vanguard/deploy-artifacts/daily-frequency-20261012)
