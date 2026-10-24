# 广告资格复核修复线上发布

用户要求立即部署，已于 **2026-10-12 06:34:02（北京时间）** 在 `oracle4c24g` 完成发布；06:35 完成账号状态采样，06:36 完成服务与公网验收。

## 发布范围

基于当前生产两套镜像增量构建，发布六个源文件：

- `app/core/account/demand_lending.py`：受事件阻挡的群只计复核需求；没有可发送目标时，释放一次完整复核所需的真实读取余量。
- `app/modules/acquisition/qualification_events.py`：普通重连 UUID 不再制造复核；严格识别并退休旧合成缺口；保留真实事件和执行退避。
- `app/modules/acquisition/qualification_recovery.py`：将上述旧记录修复接入现有恢复扫描。
- `app/modules/acquisition/group_qualification.py`：身份查询能力不足且缺证时停止无效历史翻页。
- `app/modules/acquisition/ad_warmup.py`：0 天政策不再被旧缓存的未来暖机期限阻挡。
- `app/workers/telegram_worker.py`：连接实例更换不再生成账号全群缺口。

`qualification_service.py` 和 `review_waits.py` 经逐文件比较已与本地版本一致，无需重复替换。其他本地修改均未包含在本次增量包中。

通过 `/opt/shared-postgres/stacks/vanguard/compose.json` 更新 backend、resource-search-worker、telegram-guardian-worker、celery-beat、celery-worker、telegram-growth-worker-survival-continuity 的镜像，均使用 `--no-deps --no-build --pull never`。没有数据库迁移，也没有手工批量批准资格、清零预算或追加广告发送。

## 验收

- 两套候选镜像分别通过无网络烟测：旧重连事件兼容修复、真实事件保留、0 天暖机即时判定、23 次余量能够容纳 16 次复核且计数不变。
- 六个服务正常运行，重启计数均为 0，启动日志 traceback 为 0；完整 Python 源码哈希与“原镜像 + 六文件增量”一致。
- Env、Cmd、Entrypoint 与发布前相同；运行时 Compose 仅改变上述六个服务的镜像。
- 后端和公网 `/health` 返回 HTTP 200、healthy；前端首页 HTTP 200。
- Growth、Guardian 心跳 online，last_error 均为空，采样时心跳距现在分别约 24、10 秒。
- 共享 PG18、共享网关、Vanguard Redis 和前端均 healthy；其他 36 个容器的 ID、镜像和启动时间与发布前一致。
- 原广告暖机政策仍为 0 天，账号 33、34、35 的普通审核及 16 次广告授权复核只读准入均通过。

## 账号当前情况

以下为北京时间 **06:35:04** 的实际状态，不将发布前已发生的恢复归因于本次修复：

| 账号 | qualified / usable | 有效原授权 | ad_refresh 剩余 | 当前具体等待 |
| --- | --- | --- | ---: | --- |
| 33 | 1 / 0 | 704 | 96 | 704 的 `frequency_daily_review_due`，需完成广告留存日核验 |
| 34 | 3 / 2 | 794、738、586 | 106 | 794 同为日核验待完成；738、586 当前处于 24 小时发送频率上限 |
| 35 | 3 / 3 | 577、581、635 | 96 | 当前按 24 小时发送频率等待 |

704、794、738、586 的原审核均 completed/trial，授权有效，无待处理资格事件。发布前后尝试次数分别保持 22、141、14、5，重启未给这些有效授权增加一次重连复核。

账号 34 在发布前已有两次成功发送：2026-10-11 21:31:07 对 738、21:46:24 对 586。当前频率对应下一次发送时间分别为 **2026-10-12 21:31:07、21:46:24（北京时间）**，仍需经过实际发送时的检查。账号 33 当前尚不能因原授权有效就直接发送，其广告留存日核验仍未完成。

## 记录

生产私有备份与回退计划位于 `/opt/vanguard/backups/qualification-refresh/20261011t223244z`。脱敏发布与验收文件保存在 `deploy-artifacts/qualification-refresh-20261012/`：`manifest.json`、`release.diff`、`release-result.json`、`production-verification.json`、`runtime-before.json`、`runtime-after.json`。

发布前 272 项相关测试通过、28 项跳过；本次未重新声明全量测试通过。未提交或推送 Git。
