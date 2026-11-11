"""Redis coordination for growth operations; no pre-RPC database writes."""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

PREFIX = "vanguard:growth:v2:"
TAKE = """
local time = redis.call('TIME')
local now = tonumber(time[1])
local offset = tonumber(ARGV[6] or '0')
local day = math.floor((now + offset) / 86400)
local used = 0
if tonumber(redis.call('HGET', KEYS[1], 'day') or '-1') == day then
 used = tonumber(redis.call('HGET', KEYS[1], 'used') or '0')
end
used = math.max(used, tonumber(ARGV[7] or '0'))
if used >= tonumber(ARGV[1]) then return {0, (day+1)*86400-offset-now, 'account_daily_cap'} end
local due = tonumber(redis.call('GET', KEYS[2]) or '0')
if due > now then return {0, due-now, 'account_cooldown'} end
if KEYS[3] then
 local shared = tonumber(redis.call('GET', KEYS[3]) or '0')
 if shared > now then return {0, shared-now, 'group_cooldown'} end
end
if KEYS[4] then
 local total=tonumber(redis.call('GET',KEYS[4]) or '0')+tonumber(ARGV[5])
 if total>=tonumber(ARGV[4]) then return {0,86400, 'account_total_cap'} end
end
redis.call('HSET', KEYS[1], 'day', day, 'used', used+1)
redis.call('EXPIREAT', KEYS[1], (day+2)*86400-offset)
redis.call('SET', KEYS[2], now+tonumber(ARGV[2]), 'EX', ARGV[2])
if KEYS[3] then redis.call('SET', KEYS[3], now+tonumber(ARGV[3]), 'EX', ARGV[3]) end
if KEYS[4] then redis.call('INCR',KEYS[4]);redis.call('EXPIRE',KEYS[4],172800) end
return {1, now+tonumber(ARGV[2])}
"""
RENEW = """
if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('EXPIRE',KEYS[1],ARGV[2])
"""
RELEASE = """
if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end
return 0
"""
REVERT = """
local time = redis.call('TIME')
local now = tonumber(time[1])
local offset = tonumber(ARGV[1] or '0')
local day = math.floor((now + offset) / 86400)
if tonumber(redis.call('HGET', KEYS[1], 'day') or '-1') == day then
 local used = tonumber(redis.call('HGET', KEYS[1], 'used') or '0')
 if used > 0 then
  redis.call('HSET', KEYS[1], 'used', used - 1)
 end
end
if KEYS[2] then redis.call('DEL', KEYS[2]) end
if KEYS[3] then
 local total = tonumber(redis.call('GET', KEYS[3]) or '0')
 if total > 0 then redis.call('DECR', KEYS[3]) end
end
return 1
"""


class Deferred(RuntimeError):
    def __init__(self, reason: str, seconds: int = 60):
        self.reason, self.retry_after_seconds = reason, max(1, seconds)
        super().__init__(reason)


async def take_join(redis, account_id: int, *, recovering: bool = False, durable_used: int = 0) -> datetime:
    try:
        ok, value = await redis.eval(TAKE, 2, PREFIX + f"join:{account_id}:count",
                                    PREFIX + f"join:{account_id}:due", 30, 7200 if recovering else 2880,
                                    0, 0, 0, 0, durable_used)
    except Exception as exc:
        raise Deferred("redis_unavailable", 30) from exc
    if not ok:
        raise Deferred("join_cooldown_or_daily_cap", int(value))
    return datetime.fromtimestamp(int(value), UTC).replace(tzinfo=None)


async def take_ad(redis, account_id: int, group_key: int, *, cap: int,
                  account_interval: int, group_interval: int, total_cap: int | None = None,
                  durable_used: int = 0, durable_ads: int = 0) -> None:
    from app.core.operating_time import operating_date
    shared = f"risk:account:{account_id}:daily:outbound_message:{operating_date():%Y%m%d}"
    try:
        res = await redis.eval(TAKE, 4, PREFIX + f"ads:{account_id}:count",
            PREFIX + f"ads:{account_id}:due", PREFIX + f"group:{group_key}:due",
            shared, max(0, cap), max(1, account_interval), max(1, group_interval),
            max(0, total_cap if total_cap is not None else cap), durable_used, 28800, durable_ads)
    except Exception as exc:
        raise Deferred("redis_unavailable", 30) from exc
    ok, value = res[0], res[1]
    reason = res[2] if len(res) > 2 else "ad_cooldown_or_daily_cap"
    if not ok:
        raise Deferred(reason, int(value))


async def revert_ad(redis, account_id: int) -> None:
    """Rollback ad cooldown and count when sending failed before or during RPC."""
    from app.core.operating_time import operating_date
    shared = f"risk:account:{account_id}:daily:outbound_message:{operating_date():%Y%m%d}"
    try:
        await redis.eval(REVERT, 3, PREFIX + f"ads:{account_id}:count",
            PREFIX + f"ads:{account_id}:due", shared, 28800)
    except Exception:
        pass


async def durable_outbound_used(db, account_id: int, now: datetime) -> int:
    """Existing non-growth intents retain their original counting semantics."""
    from datetime import timedelta

    from app.core.account.outbound_budget import AccountOutboundBudgetService
    from app.core.operating_time import operating_day_start
    service = AccountOutboundBudgetService(db)
    rows = await service._attempts(account_id, now)
    return max(sum(service._counted(row, start, now) for row in rows)
               for start in (operating_day_start(now), now - timedelta(days=1)))


@asynccontextmanager
async def lease(redis, name: str, *, ttl: int = 120):
    """A renewable token lock. Losing it cancels the operation, never replays it."""
    key, token = PREFIX + "lock:" + name, uuid4().hex
    try:
        acquired = await redis.set(key, token, nx=True, ex=ttl)
    except Exception as exc:
        raise Deferred("redis_unavailable", 30) from exc
    if not acquired:
        raise Deferred("operation_inflight", 15)
    parent = asyncio.current_task()
    lost = False

    async def renew():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(ttl / 3)
                if not await redis.eval(RENEW, 1, key, token, ttl):
                    raise RuntimeError("lock_lost")
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            parent.cancel()

    task = asyncio.create_task(renew())
    try:
        yield token
    except asyncio.CancelledError:
        if lost:
            raise Deferred("operation_lock_lost", ttl) from None
        raise
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(Exception):
            await redis.eval(RELEASE, 1, key, token)
