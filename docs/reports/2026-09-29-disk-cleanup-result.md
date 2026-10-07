# 第一批磁盘清理结果（2026-09-29）

已按用户“同意开始清理”的授权，在 `oracle4c24g` 执行原清单第一批缓存清理。执行时间为北京时间 15:52:40–15:53:17，15:55:27 完成保留文件复核。未执行第二批镜像、归档或备份清理。

| 指标 | 清理前 | 清理完成后 |
|---|---:|---:|
| 根盘使用率 | 91% | **79%** |
| 可用空间 | 18.19 GiB | **39.48 GiB** |
| 可用空间净增 | — | **21.29 GiB** |
| 容器数量 | 57，运行 51 | 57，运行 51 |
| Docker 镜像 ID 数（含中间镜像） | 765 | 765 |

15:55 的复核仍为 79%，可用约 39.47 GiB；少量变化来自运行期间的正常磁盘活动。净释放量采用同一次执行前后的文件系统可用空间差值，不将 Docker 逻辑容量与目录体积简单累加。

## 已完成项目

- 删除报告列明的 8 处 Trivy 重复缓存，文件分配空间共 **12.06 GiB**。逐个确认精确路径、真实路径边界、文件类型、无符号链接、无硬链接、24 小时内无文件修改、无打开句柄和容器挂载。
- 保留中央缓存 `/opt/airport-growth-multi-agent/tooling/trivy/0.74.0/cache`。其文件列表、大小、mtime 和 inode 与清理前一致。
- 执行 `apt-get clean`，清理下载缓存；未卸载软件。复核 `/var/cache/apt` 仅剩约 28 KiB。
- 通过本机 `default` 构建器清理 **183 条超过 24 小时未使用的私有构建缓存**。Docker 报告 `8.487 GB`（约 7.90 GiB）逻辑空间；947 条构建缓存降至 764 条，符合本次条件的剩余记录为 0。

执行的构建缓存命令为：

```bash
docker buildx prune --builder default --filter until=24h --filter 'private=""' --force
```

本机 buildx 版本为 0.13.1。该版本的布尔字段以空值匹配，因此先用 `du` 预览并与 Docker API 的 224 条私有缓存逐 ID 比对，确认精确一致后才执行。清理完成后再次验证：删除的 183 条 ID 恰好等于执行前符合时间和私有条件的集合；没有清理 Shared=true 或 InUse=true 的记录。筛选行为核对了 [buildx 0.13.1 源码](https://github.com/docker/buildx/blob/v0.13.1/commands/prune.go) 和 [BuildKit 筛选字段实现](https://github.com/moby/buildkit/blob/v0.13.1/cache/manager.go)。

## 验证结果

- 全部 57 个容器的 ID、镜像 ID、运行状态、启动时间、重启次数、健康状态及挂载内容保持一致；没有新增、删除或重启容器。Docker 挂载数组的返回顺序会变化，比较时按内容归一化。
- 全部 765 个镜像 ID 均保留，标签未变化。没有执行 image prune、container prune、volume prune 或 system prune。
- 66 个机场项目镜像 tar 归档的路径和文件大小逐项一致，未删除任何归档。
- Sub2API 数据库和备份、Vanguard data/sessions/部署备份与候选目录、LeoStudio 在用浏览器目录均存在，未列入删除范围。
- Vanguard 后端 `127.0.0.1:18080/health` 返回 200 / healthy，前端 `127.0.0.1:13000/` 返回 200。
- 从本机访问 `https://vanguard.pipenai.xyz/health`，清理前后均返回 200。服务器自身请求此公网入口清理前后均为 403，此路径差异在操作前已存在。
- 本次只验证运行状态与 HTTP 健康，没有通过额外 Telegram 发消息或真实支付请求做业务测试。

## 保留范围与后续

第二批候选仍按原清单保留：25.57 GiB 镜像归档、旧镜像候选、12.75 GiB Sub2API 备份，以及 Vanguard 旧备份与候选包。它们需要按项目回滚版本和备份保留策略筛选，不计入本次清理成果。

数据库、WAL、账号会话、业务上传数据、运行挂载、回滚镜像与备份均未清理。被删除的是可再生成缓存，无需生产回滚；下次相关构建或扫描可能重新下载缓存。

执行与复核证据保存在 `deploy-artifacts/disk-audit-20260929-1424/`：

- `cleanup-preflight.json`：只读基线及筛选预览。
- `cleanup-execute.json`：执行事件、每阶段 df、精确命令、前后全部容器和镜像快照。
- `cleanup-summary.json`：归一化差异比较，`checks_passed=true`。
- `protected_verification.json`：保留目录与 66 个镜像归档复核。

原始盘点见 [磁盘清理分析](./2026-09-29-disk-cleanup-analysis.md)。
