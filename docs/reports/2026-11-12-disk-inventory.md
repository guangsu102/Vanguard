# oracle4c24g 磁盘盘点（2026-11-12，只读采集 + 低风险清理已执行）

## 0.2 清理后复查（2026-11-12 晚）

**当前：119G / 64%，可用 68G。清理后无异常回涨。**

| 位置 | 占用 | 状态 |
|---|---|---|
| /opt/sub2api-dr | 55G | pgdata 52G（稳定）+ releases 2.7G（22 次发布产物）+ backups 0 |
| /var/lib/docker | 48G | overlay2 ~38G（53 个镜像 29.2GB）+ 卷 8.6G + 容器日志 1.9G |
| /opt/vanguard | 5.8G | 备份 2.9G（7 份轮转生效）+ 其余 ~2.9G |
| leostudio / codex-eval 等 | ~4G | |

**新发现——telegram_event_inbox 历史积累**：表 5.1GB / 580 万行（completed 只增不删）。每日事件量在删除过滤上线前为 16~48 万条/天（11-09 峰值 47.9 万），上线后骤降至 ~1900 条/天（过滤生效 99.5%）。5.1G 是**历史存量**而非增长中风险；增加 completed 行按时间保留策略（如保留 7 天 + 定期清理 + vacuum）可回收 ~4G 并根治。

**剩余增长源（按风险排序，均为非 Vanguard 侧）**：
1. Sub2API 发布 dump（每次 +4G，无轮转——已在附录分析，待 Sub2API 方修）
2. Sub2API 容器日志无轮转：postgres 已 1.2G、v0219 363M，持续增长（daemon.json 待维护窗口设置）
3. Sub2API releases 目录 2.7G（22 次发布产物累积）+ build cache（每次构建重新产生，当前 113MB）

**Vanguard 侧待办**：event_inbox completed 保留策略（代码修复，下轮实现）。
**一次性可选清理**：Docker 匿名卷 3.3G（112 个）、退出容器持有镜像 11.6G（sub2api 回滚资产，对方决定）、build cache 113M。



紧急度：根分区 196G，清理前已用 177G（95%）、仅剩 9.6G；比昨日诊断（172G/93%）一天内增长约 5G。
若 Sub2API 保持 11-11 的发布 dump 节奏（单日 3 份 × ~3.7G），**剩余空间约 1 天内耗尽**。

## 0. 已执行的低风险清理（2026-11-12）

| 动作 | 结果 |
|---|---|
| 终止 10 个挂起的 `docker logs` 读取进程（11d/28d/20d 三批弃置巡检） | **dockerd CPU 160%+ → 0.0%**，忙循环根除 |
| 截断 2 个已退出容器（v0216/v0218-e16fa）日志 | ~0.27G |
| Docker 悬空镜像 + build cache | 1.7G |
| vanguard scheduled-backups 轮转（22 份 → 7 份） | 3.2G（6.1G → 2.9G） |
| `runtime_backup.py` 补丁：清理无 manifest 的中断残缺目录（>2 天） | 防止再次累积 |
| **Sub2API 发布 dump 全部清空（17 份，经服务器操作者确认）** | **42G**（2026-11-12 07:19 执行，执行前确认无进行中 dump，事后确认 PG/vanguard 服务均 healthy） |
| **df 对比** | **177G/95% → 130G/70%，可用 9.6G → 57G** |

> ⚠️ 注意：Sub2API 发布流程仍在每次发布前生成 ~3.7G dump 且无自动轮转，清空后若无留存策略，空间会以相同速度回涨。需 Sub2API 方在发布流程中加入轮转（建议保留最近 2-3 份）。

## 附：Sub2API dump 产生原因分析（2026-11-12）

机制溯源：`/opt/sub2api-dr/releases/<release-id>/release.sh` 的 `backup_production()` 函数。

1. **每次发布前强制全量备份**：`pg_dump`（custom 格式，compress=1，实测单份 3~3.7G）+ `app-data.tar.gz` + Redis RDB + nginx/配置副本。质量本身不错：dump 经 `pg_restore -l` 校验，完成写 `backup-complete-at.txt` 标记，中断的移入 `.failed-*`。
2. **触发频率 = 发布频率**：09-29 以来 22 次发布，且加速——10-27~10-29 三天 6 次；11-11 一天 3 次（plaza-cards/plaza-scroll/gemini-carbon-catalog，间隔约 3.5 小时）+ 次日凌晨 v0221。`-rN` 轮次命名表明是 AI 辅助的快速迭代发布。
3. **脚本内无任何留存/轮转逻辑**（grep rotate/prune/delete 为空）——每次发布 +~4G，只进不出，6 周积累 17 份 = 42G，增速与发布节奏成正比。
4. **唯一的保护是下限**：发布前检查剩余空间须 >6GB，低于则中止发布——是"磁盘满时发布失败"的熔断，不是防增长。

结论：**良性意图（发布前全量备份）× 无限留存 × 高频发布**。修复属于 Sub2API 发布管线：① 加保留策略（保留最近 2~3 份，rotate 掉更旧的）；② 或改用 PG WAL 归档实现 PITR，把单次 3.7G 降为增量；③ 或 dump 落地后即推送离机存储再删除本地副本。

## 0.1 第二轮：未使用镜像清理（2026-11-12）

| 动作 | 结果 |
|---|---|
| 删除 171 个无容器引用的镜像（保留 `local/vanguard:writable-v2-20261107-r8` 作回滚 + sub2api 3 个旧镜像不动） | 镜像 224 → 53，0 失败 |
| 镜像库总量 | 42.27GB → 29.21GB（实际释放 ~11G，名义 207GB 因层共享被高估） |
| **df 累计** | **177G/95% → 119G/64%，可用 9.6G → 68G** |

剩余说明：`docker system df` 仍显示 ~11.6GB "reclaimable"，对应 9 个已退出容器（sub2api 旧版本等）持有的镜像——属各服务自己的回滚容器，未动。当前 backend 运行镜像为 10-16 的 `ad-receipt-repair-*`（今日修复为 docker-cp 注入），下次正常重建镜像后此差异消除。

注：vanguard compose 本身已带日志轮转（10m×3）；daemon.json 未设置，缺口在 sub2api 侧容器（其 postgres 日志已 1.2G），设置 daemon.json 需重启 docker 影响全部容器，须在维护窗口进行。



## 一、总量构成（互不重复相加）

| 位置 | 占用 | 说明 |
|---|---|---|
| /opt | 110G | 见下 |
| /var | 64G | 其中 /var/lib/docker 62G（`du -x` 核实） |
| /usr + /root + /var/log 等 | ~4G | 系统与日志 |
| 合计 | 177G | 与 df 一致 |

注：不带 `-x` 的 du 会把 docker 内部重复计数（/var/lib/docker/overlay2 显示 82G 是假象），以 `-x` 结果为准。

## 二、/opt（110G）

| 目录 | 占用 | 构成 |
|---|---|---|
| /opt/sub2api-dr | 96G | pgdata 52G（base 50G + WAL 2.1G）+ **backups 42G（17 份发布前 dump）** + 发布目录 ~2.6G |
| /opt/vanguard | 9.0G | scheduled-backups **6.1G（23 份每日备份，无轮转）** + backups 0.9G + data/backend/sessions 等 ~1.7G |
| /opt/leostudio-oracle4c24g | 2.2G | 发布目录 |
| /opt/codex-eval | 1.3G | 评估产物 |
| 其他（digital-goods/shadowfleet/turb-gpt 等） | ~1G | |

**昨日 → 今日增量 ~5G**：sub2api backups +4.3G（11-11 新增 pre-plaza-cards / pre-plaza-scroll / pre-gemini-carbon-catalog 三份 dump，每份 3~3.7G）+ vanguard 每日备份 +0.3G + pgdata base 小幅增长。

## 三、/var/lib/docker（62G，`du -x` 口径）

| 项 | 占用 | 明细 |
|---|---|---|
| overlay2（镜像层） | ~51G | 240 个镜像、49 个被引用；`docker system df` 可回收 **31.22GB（73%）** |
| volumes | 8.6G | **126 个卷仅 12 个在用**；大项：vanguard_napcat_qq_data 4.1G（业务数据，保留）、vanguard_vanguard-prometheus 1.1G、若干 200-300M 匿名卷 |
| containers（容器日志） | 2.2G | 最大：sub2api-dr-postgres **1.2G**、sub2api-dr-app-v0219（运行中）363M、v0216（已退出）137M、v0218-e16fa（已退出）129M |
| build cache | 166M | 全部可回收 |

## 四、dockerd CPU 忙循环的根因（已实锤）

`dockerd` 长期 160%+ CPU、99.4% 采样在日志尾部扫描路径，直接原因是**挂起的 `docker logs` 客户端**：

```
PID 1614088 / 1614126  已挂起 11 天 18 小时  → docker logs sub2api-dr-app-v0214-56cf75c-20261007（已退出容器）
PID 1617701 / 1617774  已挂起 11 天 18 小时  → 同上（另一轮巡检 bash 循环）
PID 2755075            已挂起 1 天 23 小时   → docker logs oracle-shadowfleet-api
```

这些是此前人工/脚本巡检留下的 bash 循环，正在对已退出容器日志做无限 tail，dockerd 为其持续扫描日志尾部。
另有 3 个已删除容器（9-06、10-08 时代）的日志句柄仍被 dockerd 持有。

处置（均为低风险，不影响任何容器运行）：
1. kill 上述 5 个 `docker logs` 客户端进程及其父 bash；
2. `truncate -s 0` 两个已退出容器（v0216/v0218-e16fa）的 json.log，释放 ~270M；
3. 设置 daemon.json 日志轮转（`max-size: 20m, max-file: 3`），对新建容器生效；存量容器在下次重建时自然带上；
4. sub2api-dr-postgres 的 1.2G 日志可用 `truncate -s 0` 截断（不动文件句柄，不影响运行中的 PG）——属日志管理，建议与 Sub2API 方知会后执行。

## 五、可释放空间汇总（按风险从低到高）

| 动作 | 可释放 | 风险 | 前置确认 |
|---|---|---|---|
| 杀 5 个挂起的 docker logs 客户端 | 0（止住 CPU 空转） | 极低 | 无需（只影响读取方） |
| 截断 2 个已退出容器日志 | ~0.3G | 极低 | 无需 |
| vanguard scheduled-backups 轮转（保留最近 7 份） | ~4.8G | 低 | 备份脚本本身可加自动轮转 |
| Docker 悬空镜像 + build cache（`docker image prune -f`） | ~数 GB 内 | 低 | 无需 |
| Docker 未使用镜像（保守：仅清 30 天未引用且非回滚保留） | ~25-31G | 中 | 需逐个核对 49 个运行容器的镜像与回滚保留清单 |
| Docker 匿名未使用卷（112 个未挂载） | ~2-3G | 中 | 逐个确认无挂载引用 |
| sub2api 旧发布 dump（17 份 → 保留最近 3 份） | **~31G** | 中 | **需 Sub2API 方确认留存策略**（非 Vanguard 服务，只建议不代删） |
| 合计 | **~65-70G** | | 95% → 约 60% |

## 六、结构性建议

1. **Sub2API 发布流程加 dump 轮转**（当前每发布一次 +3.7G，一天 3 次即可吃满剩余空间）——这是磁盘增长主因，Vanguard 侧无法代管。
2. **vanguard scheduled-backups 加自动轮转**（保留 7 天，代码/脚本层面可改，避免 23 份无限累积）。
3. **daemon.json 日志轮转**一次性治本（所有新容器），配合 vanguard 栈下次 `up -d` 重建生效。
4. dockerd 挂起日志客户端是巡检脚本未用 `--tail` 退出机制/超时导致，巡检方式需修正。

## 七、不做的事

- 共享 PostgreSQL（sub2api-dr-postgres）容器与 pgdata 52G 业务数据：不动。
- NapCat QQ 数据卷 4.1G：业务数据，不动。
- Prometheus TSDB 1.1G：可按指标保留策略另行评估，本次不动。
- PG WAL 2.1G：正常范围，不动。
