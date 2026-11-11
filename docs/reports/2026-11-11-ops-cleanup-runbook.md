# 服务器侧清理与运维操作清单（oracle4c24g）

依据 2026-11-11 21:45—22:07 只读诊断（[findings.json](../../tmp/system-diagnosis-20261111/findings.json)）。
所有删除类操作先列清单核对，再执行；**共享 PostgreSQL（sub2api-dr-postgres）与 Sub2API 运行栈一律不动**。

## A. dockerd 日志忙循环（CPU 优先项）

现象：dockerd 约 161%—180% CPU，99.4% 采样时间在日志尾部扫描路径；持有 3 个已删除容器日志的读取句柄，且有多个挂起的 `docker logs` 客户端。

```bash
# 1. 找出挂起的 docker logs 客户端与对应容器（只读）
ps -eo pid,etime,cmd | grep "[d]ocker logs"
ls -l /proc/$(pidof dockerd)/fd 2>/dev/null | grep "deleted.*log" | head

# 2. 终止陈旧的 docker logs 客户端进程（逐个确认后 kill <pid>）
# 3. 设置全局日志轮转（daemon.json 持久化 + 现有容器需重建才生效）
sudo tee /etc/docker/daemon.json.new <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" }
}
EOF
# 与现有 daemon.json 合并后再覆盖（不要直接覆盖，先 cat 现文件）
cat /etc/docker/daemon.json 2>/dev/null
sudo systemctl restart docker   # 重启会短暂影响全部容器，选择低峰执行
```

注意：
- `log-opts` 只对**新建/重建**的容器生效；vanguard 生产栈重建时自然带上。
- 单个容器临时清理：`truncate -s 0 /var/lib/docker/containers/<id>/<id>-json.log`（不清配置，只截断）。
- 当前容器日志约 2.1 GiB，截断大文件日志可立即缓解 dockerd 扫描负担。

## B. Docker 镜像清理（约 30.6 GB 可回收，先核对保留清单）

```bash
# 1. 悬空镜像（无标签、未被引用）：安全
docker image prune -f

# 2. 列出未使用的镜像（236 个镜像中仅 48 个被引用）——先核对回滚保留清单
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | sort
docker images -f "dangling=false" --filter "until=168h" --format '{{.Repository}}:{{.Tag}}'

# 3. 与当前运行版本比对后，再执行全量回收（谨慎：保留最近 1-2 个可回滚版本）
docker image prune -a -f --filter "until=168h"
```

保留原则：vanguard-backend / vanguard-frontend 当前运行镜像 + 其前一个版本；Sub2API 栈的全部运行镜像；其余构建缓存与历史版本可清。

## C. Sub2API 历史发布备份（37.7 GiB，13 份发布前 dump）

```bash
ls -lh /opt/sub2api-dr/*.sql* /opt/sub2api-dr/backups/ 2>/dev/null
# 保留最近 2 份；更早的迁移到对象存储或删除（先与 Sub2API 负责人确认留存策略）
```

## D. vanguard 定时备份（5.8 GiB）

```bash
ls -lht /opt/vanguard/backups/ | head -20
# 按留存策略裁剪：保留最近 7 天每日 + 4 周每周即可；确认备份脚本自身的轮转配置是否失效
```

## E. 队列残留对账（3 条已外发、结果未知的事件，挡住 78 条同群业务事件）

```bash
# 定位（只读）
docker exec vanguard-backend python - <<'PY'
import asyncio
from sqlalchemy import select
from app.core.database import get_db_session
from app.core.worker_status import TelegramEventInbox
async def main():
    async with get_db_session() as db:
        rows = (await db.scalars(select(TelegramEventInbox).where(
            TelegramEventInbox.state == "reconciliation_required",
            TelegramEventInbox.external_attempted.is_(True),
        ).order_by(TelegramEventInbox.id).limit(20))).all()
        for r in rows:
            print(r.id, r.account_id, r.kind, r.last_error, r.started_at)
asyncio.run(main())
PY
# 人工核实 Telegram 侧实际效果后，用 resolve_reconciliation 关闭（禁止直接重放）
```

## F. 明确不做的事

- 不停止/重建 `sub2api-dr-postgres`，不动 `/opt/shared-postgres/` 运行栈。
- Docker 卷中的 NapCat 数据（约 4.08 GiB）是业务数据，不按缓存清理。
- PG WAL（2.08 GiB）不是大头，不调整 checkpoint 配置。
- 不清空 15 万条待处理删除通知：代码修复后由批量通道自然消化，其中可能混有未落库发送的删除证据。

## 建议顺序

1. A（dockerd 忙循环）——CPU 压力直接来源。
2. C + D（备份裁剪）——低风险释放 ~40 GiB。
3. B（镜像回收）——先核对保留清单，释放 ~30 GB。
4. E（3 条对账）——恢复 78 条被挡业务事件。
5. 部署本轮代码修复后，观察积压消化与权限失败频率。
