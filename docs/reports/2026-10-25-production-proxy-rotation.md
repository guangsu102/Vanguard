# 生产代理轮换记录

执行时间：2026-10-25 05:43–05:49 UTC  
范围：Vanguard 生产业务库、Vanguard Redis 策略键和常驻账号池

## 变更

- 删除原有 5 条失效静态代理记录（`isp-2.evomi.com:12346`）。
- 写入 10 条新的 TrustSource 动态住宅代理，统一为 `http`、`US`、`residential`、`trustsource`、`is_active=true`。
- 分配关系：账号 2 → 代理 ID 16，账号 4 → 17，账号 33 → 18，账号 34 → 19，账号 35 → 20。
- 代理 ID 21–25 保留为未绑定备用代理。
- 账号 5、31、32 保持 `dynamic` 且没有静态代理绑定。

数据库事务提交后，向 Redis 发布了 5 个账号的代理策略 generation，并由常驻 worker 消费。共享 PostgreSQL、共享 Redis 服务和 Telegram 发送开关均未停止或重建；P2/P3/P4/P5 flags 未修改。

## 核验

- 生产只读查询：代理总数 `10`，新代理主机数量 `10`，旧主机存在性 `false`。
- 10 个新代理的 HTTP 与 SOCKS5 Telegram API 探针均返回 `200`。
- Redis 策略状态：账号 2/4/33/34/35 分别指向代理 ID 16/17/18/19/20。
- `https://vanguard.pipenai.xyz/health` 返回 `{"status":"healthy","version":"1.0.0"}`。
- 代理策略广播时间点之后，growth worker 与 celery worker 没有新的 `static_proxy_unavailable` 或 `database_proxy_policy_snapshot_stale` 日志。

代理凭据只在执行时通过 stdin 传入生产容器，未写入本报告、Git 或终端输出。
