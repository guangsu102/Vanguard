# oracle4c24g 共享 PostgreSQL（2026-09-30）

Oracle 上的业务共用一个 PostgreSQL 18 主实例 `sub2api-dr-postgres`，每个业务保留独立数据库和账号。这里的“整合”是共享实例，不是把业务表混放到一个数据库。

| 业务 | 数据库 | 应用账号 | 连接数上限 |
| --- | --- | --- | ---: |
| Sub2API | sub2api | 原账号保留 | 原连接配置保留 |
| Airport 当前登录环境 | airport_growth_oidc_uat | airport_oidc_app | 4 |
| Airport X 验收环境 | airport_growth_x_acceptance | airport_x_app | 4 |
| Digital Goods | digital_goods | digital_goods_app | 8 |
| XBoard / ShadowFleet | xboard | xboard_app | 35 |
| Vanguard | vanguard | vanguard_app | 25 |

应用地址为 `oracle-shared-postgres:5432`。这是现有各应用 Docker 网络内的 TCP 网关别名，网关容器 `oracle-shared-postgres-gateway` 不发布宿主机端口；网关转发到主实例的 `10.77.0.1:55432`。主实例仍使用 host 网络。网关独立出口网络是 `oracle-shared-postgres-egress`（172.30.250.0/28），出口地址 172.30.250.2，使用 PostgreSQL SCRAM 密码认证。

主实例当前是主库；远端 10.77.0.3 经 `xd_primary` 槽执行异步物理复制。新增数据库和角色随 WAL 复制。应用仍连接 Oracle 主库，网关没有自动主从切换能力。故障切换需由共享数据库维护者统一处理，并校验备库 HBA 和应用路由；物理复制不能代替历史备份。

容器内存和内存加 swap 上限均为 8 GiB。在线参数：effective_cache_size=6GB、work_mem=8MB、maintenance_work_mem=256MB、autovacuum_work_mem=128MB、max_parallel_maintenance_workers=1。为保持 Sub2API 连续服务，本次没有重启或重建它的应用/数据库容器；shared_buffers 仍为 128MB，max_connections 仍为 100。连接池应受上表限额约束，不能给每个进程都配置大池。需要重启的进一步调优需另安排维护窗口。

## 部署边界

- 单个项目不能停止、重建或执行 `down` 操作影响共享数据库/网关，也不能清理共享 PG 数据卷。
- Oracle 常规发布不启动旧独立 PG。旧容器和旧数据卷已按用户要求永久删除，不再保留旧实例回滚。
- Redis 本次没有合并，继续使用各项目现有实例、认证、队列和持久化策略。
- 不在 Git 中保存真实密码、生产 `.env`、数据库 dump 或运行时 Compose。真实配置仅在服务器 root 可读文件中。
- 运行时配置以 `/opt/shared-postgres/stacks/<项目名>/compose.json` 为准。文件固定了切换时的实际镜像、挂载、环境变量和网络；正常操作只指定应用服务并加 `--no-deps`。新发布应先构建并核对新镜像，再明确更新相应服务的镜像及必要配置，不能用旧 Compose 全栈覆盖生产。
- 本仓库共享 PG 覆盖文件供新版本发布合并使用，需要 Docker Compose >=2.24.4（使用 `!override`/`!reset`）。实际主机 2.26.1 已支持。先运行 `config --quiet`，再针对明确的应用服务执行 `up -d --no-deps`。
- 旧实例和迁移回滚 dump 已删除，不能回启旧数据库。故障恢复使用正式生产备份，必须仅针对受影响业务并保护其他共享业务。

迁移一致性记录位于 `/opt/shared-postgres/20260930-consolidation/`，仅 root 可读。SHA-256 和逐表行数/序列比对记录保留；最终/演练 dump、旧配置和运行快照已在后续清理中删除。Vanguard 的每日备份脚本已改用共享主库；恢复演练在无网络的临时 PG18 容器中执行，不在共享实例创建/删除演练库。

## Vanguard 配置

运行时文件：`/opt/shared-postgres/stacks/vanguard/compose.json`。

```bash
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json config --quiet
docker compose -f /opt/shared-postgres/stacks/vanguard/compose.json up -d --no-deps backend
```

当前六个数据库写入服务为 backend、celery-worker、celery-beat、resource-search-worker、telegram-growth-worker-survival-continuity、telegram-guardian-worker。生产 growth 服务名与本仓库基线的 telegram-growth-worker 有差异，发布必须先核对现有服务名。

`DATABASE_URL` 使用 `postgresql+asyncpg://vanguard_app:<密码>@oracle-shared-postgres:5432/vanguard`；`DATABASE_POOL_SIZE=2`、`DATABASE_MAX_OVERFLOW=1`。不要把 URL 放进日志。

新版本使用 `docker-compose.production.yml` 加 `docker-compose.oracle-shared-postgres.yml`；通过私有 env 文件提供 `SHARED_POSTGRES_URL`（asyncpg 格式）。指定 `--env-file` 使 Compose 插值读取该变量。示例：

```bash
docker compose --env-file /opt/vanguard/.env.production -f docker-compose.production.yml -f docker-compose.oracle-shared-postgres.yml config --quiet
```

备份 `/opt/vanguard/scripts/ops/runtime_backup.py` 只备份 vanguard 库（用户 vanguard_app、端口 55432），保留独立 Redis 备份。监控 `/opt/vanguard/scripts/ops/runtime_monitor.py` 检查共享 PG 和网关。`vanguard-backup.timer` 和 `vanguard-runtime-monitor.timer` 继续运行。手工恢复演练：`python3 /opt/vanguard/scripts/ops/runtime_backup.py --restore-drill`。

## 本次验收记录

2026-09-30 22:15（北京时间）验收完成：五个业务库共 195 张表、140 个序列在停写切换时与源库一致。六个应用的原生数据库客户端均确认连接各自目标库；两个 Sub2API 公共入口 GET 返回 200。Sub2API 应用及 PostgreSQL 容器未重启/重建，21:27 至 22:15 的 572 次健康探测无失败。远端 PG 从库已回放验收 WAL 位点，Redis 主从在线。

五套发布 Compose 组合已在主机 2.26.1 验证：默认服务不包含旧独立 PG，连接指向共享网关。每个服务器运行时目录另有 root-only `database.env`，供新版本叠加共享 PG 覆盖文件时作为额外 `--env-file` 读取；仍需保留项目原有私有环境文件。不要打印/提交该文件或运行时 Compose。

Vanguard 每日备份及监控定时器已恢复，实际完成从共享库备份和无网络临时 PG18 恢复演练。五个旧 PG 容器及原数据卷、最终迁移 dump 已在后续清理中永久删除。Git 未提交或推送。

## 不保留回滚的清理结果

2026-09-30 用户明确要求删除无用资源且不再需要回滚。服务器已删除五个已迁移旧 PG 容器及原数据、废弃 Keycloak/旧 OIDC 环境、其他确认停用容器、无引用数据卷及空网络。运行时 Compose 已移除旧数据库服务。

迁移用最终/演练 dump、旧环境配置副本，以及历史发布回滚包已删除。当前共享 PG、Redis/MySQL/SQLite 业务数据、当前运行镜像、正常生产备份和正在发布流程使用的资料保留。不要按本文早期迁移时的“停止保留”状态恢复旧容器；后续状态以本节为准。

## WAL 保留与追加清理（2026-09-30）

共享主库已通过 ALTER SYSTEM 持久化 `wal_keep_size=2GB` 并在线 reload，替代此前的 16GB 最低保留量。`xd_primary` 物理复制槽仍为 active，备库 10.77.0.3 继续 streaming；`max_slot_wal_keep_size=-1` 保留原值，复制槽继续按备库进度保留必要 WAL。不可手动删除 pg_wal 文件或因单个项目部署移除共享复制槽。应继续监测复制延迟和槽保留量。

正常检查点已将 WAL 目录从约 16.11 GiB 回收到约 2.13 GiB，期间没有重启或重建 Sub2API、PostgreSQL。容器 8 GiB 上限继续生效，没有待重启参数。

已永久删除旧的 /tmp 构建目录、LeoStudio 历史上传部署包以及 pip/APT 缓存；系统 journal 已清理超过 7 天的归档。/tmp 是 tmpfs，此部分释放约 1.25 GiB 内存临时存储，不计入系统盘释放量。当前发布目录和正常生产备份继续使用。服务器操作记录位于 /opt/shared-postgres/20260930-extra-cleanup/。
