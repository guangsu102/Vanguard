# oracle4c24g 磁盘清理盘点（2026-09-29）

更新：第一批已于 15:53 完成，根盘降至 79%，净释放约 21.29 GiB。详见 [清理执行结果](./2026-09-29-disk-cleanup-result.md)。以下保留原始盘点快照，第二批仍未执行。

本轮仅执行只读盘点。没有删除文件、清理 Docker、停止容器、调整数据库或修改生产配置。范围为 oracle4c24g 整台主机，包含 Vanguard 及同机其他项目。采集时间为北京时间 14:26–14:41，目录扫描和数据库统计不是同一事务快照。容量统一使用 GiB（1024³ 字节）。

根盘总量 **195.70 GiB**，已用 **167.40 GiB**，可用 **18.30 GiB**，`df` 使用率 **91%**；inode 使用率 18%。优先处理重复扫描缓存和闲置构建缓存，第一批估算约 **20.17 GiB**；完成后预计可用约 **38.46 GiB**，使用率约 **80%**。这是待执行估算，最终以清理后 `df` 为准。

## 空间分布

| 路径 | 占用 GiB | 内容与判断 |
|---|---:|---|
| `/var/lib/docker` | 64.78 | 镜像、构建缓存、容器日志和卷；不能直接删除内部目录 |
| `/opt/sub2api-dr` | 48.23 | 生产数据库 33.28、备份 12.75、发布目录 2.02 |
| `/opt/airport-growth-multi-agent` | 40.68 | evidence 28.07、candidates 9.77、tooling 2.78 |
| `/opt/leostudio-oracle4c24g` | 5.04 | releases 3.68、uploads 1.12；运行程序和浏览器有目录挂载 |
| `/opt/vanguard` | 3.34 | deploy-backups 1.82、deploy-candidates 0.90；含生产 data 和 sessions |
| `/opt/vanguard.file-backups` | 0.90 | 旧整目录备份，按保留策略复核 |
| `/opt/vanguard-deploy-backups` | 0.42 | 另一处部署备份，按保留策略复核 |
| `/var/log` | 0.81 | 主要是 systemd journal |
| `/var/cache` | 0.30 | 主要是 apt 下载缓存 |

表中路径互不包含；后续细分容量已包含在对应父目录中，不能重复累加。`/opt` 合计 99.36 GiB，`/var` 合计 66.01 GiB。Docker overlay2 为 59.73 GiB，里面也有构建层，不能再把镜像逻辑体积叠加进去。

## 第一批：低风险、可再生成的缓存

| 项目 | 估算 GiB | 已核实情况 |
|---|---:|---|
| 8 处 evidence 内的 Trivy 缓存副本 | 12.06 | 无打开句柄、mmap、进程工作目录或容器挂载；文件无硬链接 |
| 超过 24 小时未使用的私有 BuildKit 缓存 | 7.90 | 180 条，InUse=false、Shared=false；清理前重新检查构建任务 |
| apt 已下载安装包缓存 | 0.21 | `/var/cache/apt/archives`；可重新下载 |
| 合计 | **20.17** | 不包含旧镜像、备份、数据库、日志清理 |

保留 `/opt/airport-growth-multi-agent/tooling/trivy/0.74.0/cache` 中央缓存约 2.78 GiB。全部 9 处缓存为 14.84 GiB，但第一批只计 8 处重复副本。清除缓存会增加下次构建或扫描的下载时间。应通过 Docker 管理构建缓存，不手工删除 `/var/lib/docker` 下的层或元数据。

第一批 Trivy 精确路径：

| 路径 | GiB |
|---|---:|
| `/opt/airport-growth-multi-agent/evidence/14676fa926f7845cda7943eda8c544a0499ac319/tool-cache/trivy` | 1.335 |
| `/opt/airport-growth-multi-agent/evidence/2d5054c95bb8cd9328f9b4ab13c2647f1ee5043f/tool-cache/trivy` | 1.317 |
| `/opt/airport-growth-multi-agent/evidence/72675cd8702c025fc1949131dc1a7678a77eee6a/tool-cache/trivy` | 1.317 |
| `/opt/airport-growth-multi-agent/evidence/account-login-20260926/trivy-cache` | 2.781 |
| `/opt/airport-growth-multi-agent/evidence/af0858ce6e670db8570655b0d84037242568ce50/tool-cache/trivy` | 1.317 |
| `/opt/airport-growth-multi-agent/evidence/b7aa1071bf43aeebef974780ab3ae9b068eb7f22/tool-cache/trivy` | 1.335 |
| `/opt/airport-growth-multi-agent/evidence/bbd0862e1c50f42614e35ac9906cb66c9c71b46e/tool-cache/trivy` | 1.336 |
| `/opt/airport-growth-multi-agent/evidence/d83f3ebe5232bdc8b4a3c586fd1c80122d883a10/tool-cache/trivy` | 1.317 |

这些目录只含漏洞库和扫描缓存；其父目录中的扫描报告、发布证据、配置备份和镜像归档继续保留。执行前需重新验证未被正在进行的扫描使用。

## 第二批：有较大空间，但需确定回滚和备份保留范围

| 项目 | 当前体积 / 候选体积 GiB | 处理建议 |
|---|---:|---|
| airport 镜像 tar 归档，共 66 个 | 25.57 | 全部核对了 manifest 和配置摘要，对应镜像均仍存在 Docker；归档无容器挂载。确认离线回滚用途后归档或删除选定 tar，保留 JSON/报告 |
| 155 个无标签、未发现引用、创建超过 24 小时的镜像 | 3.21 独占层估算 | 逐 ID 复核后清理；无标签不等于无用，另有 3 个无标签镜像正在被容器引用 |
| 55 个有标签、未发现引用的旧镜像 | 6.62 独占层估算 | 按项目确认回滚和后续构建需求，再逐 ID 清理 |
| `/opt/sub2api-dr/backups` | 12.75 总量 | 保留当前上线前备份及关键计费/迁移基线；旧备份先验证可恢复和异地副本，不把总量视为可删量 |
| Vanguard 两处旧备份加当前 deploy-backups、deploy-candidates | 4.04 总量 | 按版本保留当前、上一个可回滚版本和待发布候选；其他旧包再精简 |
| LeoStudio uploads | 1.12 总量 | 发布包上传暂存可逐项复核；不要与 releases 中的运行浏览器目录混淆 |

镜像分类已交叉比对全部运行/停止容器、有限深度的发布/回滚/Compose 文本引用，以及 tar 内的镜像摘要。未发现引用只是候选证据，不代表扫描范围以外绝无引用。镜像独占层、共享层和 BuildKit 会相互影响，第二批镜像数字不能作为与第一批直接相加的保证值。

66 个归档：evidence 下 42 个、15.99 GiB；candidates 下 24 个、9.58 GiB。其中 9 个归档对应仍被容器引用的镜像，约 3.18 GiB；其余 57 个约 22.39 GiB。即使文件本身不参与运行，也可能承担离线回滚用途。禁止同时删除某版本的 Docker 镜像和唯一离线备份。

重点旧镜像候选：

| 标签 | 独占层 GiB |
|---|---:|
| `local/pay-gpt-upgrade:s8-80587f4f2524` | 2.79 |
| `mcr.microsoft.com/playwright:v1.62.1-noble` | 2.60 |
| `vanguard-admin-init:latest` | 0.40 |
| `postgres:16` | 0.35 |
| `node:20-alpine` | 0.13 |
| `local/sub2api:0.2.1-standby-arm64-3b5475168b75 / local/sub2api:prod-candidate-3b5475168b75-arm64` | 0.12 |

Sub2API 还有一份名称含 `.failed-20260925T155701Z` 的旧备份目录，约 1.01 GiB，且随后有对应正常目录。它优先进入重复备份复核，但本轮未验证完整恢复，不能仅凭名称判定无效并删除。

## 数据库：不能按普通文件清理

`/opt/sub2api-dr/pgdata` 占 33.28 GiB，主要是数据文件约 17.20 GiB，加 WAL 约 16.06 GiB。连接实际 PostgreSQL 18 的 55432 端口，只查询目录统计，设置了只读事务和 15 秒超时。

WAL 的主要保留原因已经确认：`wal_keep_size=16384 MB`，即主动为复制保留 16 GiB；`archive_mode=off`；复制槽 `xd_primary` 活跃，快照中的保留滞后和下游 replay lag 都为 0，状态为 streaming。当前不是失联复制槽导致的堆积。`max_slot_wal_keep_size=-1` 也值得在后续容量策略中审视，但本轮不改复制设置。

`wal_keep_size` 指定的是保留 WAL 的最低量；若将其缩小，会减少下游断连后的追赶窗口，需结合 WAL 产生速度和容灾要求评估，不能直接删除 `pg_wal`。见 [PostgreSQL 18 官方说明](https://www.postgresql.org/docs/18/runtime-config-replication.html#GUC-WAL-KEEP-SIZE)。

业务库内部的大表（含索引）：

| 表 | 总体积 GiB | 索引 GiB | 后续判断 |
|---|---:|---:|---|
| `leostudio_oracle.outbox_events` | 4.60 | 2.19 | 需按状态、时间、消费完成情况制定保留策略 |
| `leostudio_oracle.browser_profile_leases` | 4.52 | 3.44 | 需按状态、时间、消费完成情况制定保留策略 |
| `public.ops_system_logs` | 2.91 | 1.36 | 需按状态、时间、消费完成情况制定保留策略 |
| `leostudio_oracle.browser_profile_work_claims` | 2.91 | 1.44 | 需按状态、时间、消费完成情况制定保留策略 |
| `public.usage_logs` | 1.02 | 0.64 | 计费或用量数据，不能按日志垃圾清理 |
| `public.ops_error_logs` | 0.27 | 0.15 | 需按状态、时间、消费完成情况制定保留策略 |
| `public.usage_billing_dedup` | 0.24 | 0.10 | 计费或用量数据，不能按日志垃圾清理 |

前三个 LeoStudio 表（outbox_events、browser_profile_leases、browser_profile_work_claims）合计约 12.02 GiB，说明 Sub2API 数据目录中还承载其他业务 schema。不能把整个目录或整个数据库视为单一项目的可删除日志。此次未扫描业务记录验证过期范围，未将表容量算作可回收空间。

## 日志、数据卷与应保留项目

- Docker 容器日志及容器目录合计约 1.27 GiB；其中 sub2api-dr-postgres 的 JSON 日志约 0.84 GiB，未设置 max-size/max-file。后续应补充日志轮转；本轮未截断活动日志。
- systemd journal 合计约 0.79 GiB，可按保留时间或容量精简，但收益小于缓存，先保留本次排障证据。
- 约 0.30 GiB 已删除日志仍被 Sub2API 进程持有；重复删除路径无效，要由进程关闭句柄才能释放。为此单独重启生产服务收益很低。
- `/tmp` 占用约 1.40 GiB，但它是 tmpfs 内存盘；清理它不会释放根盘空间。
- 51 个运行中容器、6 个停止容器均纳入保护引用；46 个镜像 ID 被容器引用。至少 13 个 Sub2API 历史/当前应用版本仍在运行，不能按版本旧直接当垃圾清理。
- `vanguard-qq-onebot-worker` 虽已停止，关联的 `vanguard_napcat_qq_data` 卷仍占约 2.30 GiB，包含账号运行数据，不列入垃圾。若干未引用匿名卷还有约 0.58 GiB，未确认数据用途，暂不做 volume prune。
- 保留所有数据库、Redis 数据、Telegram/QQ sessions、上传业务文件、凭据、TLS 密钥以及当前和上一个可用回滚版本。
- LeoStudio 当前版本 `/opt/leostudio-oracle4c24g/releases/dev-4712b3544f9b` 的程序与 Playwright 浏览器被容器挂载，不能因为浏览器体积大而当缓存删除。
- Vanguard 当前 backend 镜像为 `local/vanguard:frequency-schedule-d3939fce49ad`，上一个候选回滚为 `local/vanguard:selective-listener-6f07023f57a4`；保留相关镜像、部署配置及数据库备份。

## 建议顺序与长期控制

1. 先处理本报告第一批精确缓存清单，预计释放约 20.17 GiB；每类执行后检查 df、容器状态与业务健康。
2. 再确定 airport 的镜像归档保留版本，优先清理重复 tar；不要直接删除 evidence 或 candidates 整棵目录。
3. 再按镜像 ID 和项目回滚范围精简旧镜像，以及建立数据库备份保留清单。
4. 后续把 Trivy 数据库缓存集中存放，发布证据仅保留报告、摘要与必要镜像；设置构建缓存上限、Docker 日志轮转和备份保留策略。
5. 数据表生命周期及 WAL 容灾保留单独评估，不并入临时文件清理。

## 可复核证据

所有证据位于 `deploy-artifacts/disk-audit-20260929-1424/`：

- `filesystem.json`：df、inode、目录、日志、已删除但仍打开文件。
- `docker_inventory.json`：全部容器/镜像/卷/构建缓存及挂载；未保存 Env 或秘密。
- `details.json`：精确缓存路径、归档与备份容量。
- `cache_verification.json`：缓存去重分配空间、硬链接、打开句柄、mmap 和挂载复核。
- `archive_verification.json`：66 个 tar 的镜像摘要及 Docker 存在性。
- `database_sizes.json`：数据库只读目录统计、复制及 WAL 配置。
- `images.csv`、`cleanup-candidates.json`：镜像完整 ID、标签、分类、保留引用。
- `cleanup-plan.json`：第一批精确路径与预检查条件；状态为 proposal_only_no_deletion_performed。

实际可释放空间受后续构建、写入和 Docker 共享层影响；此次没有执行清理，所有释放量均为候选估算。
