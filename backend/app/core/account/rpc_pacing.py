"""Account-wide RPC pacing, independent of workflow budgets."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime

from app.core.config import settings

PACE = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local due = tonumber(redis.call('GET', KEYS[1]) or '0')
if due > now then return due - now end
redis.call('SET', KEYS[1], now + tonumber(ARGV[1]), 'PX', ARGV[1])
return 0
"""


async def restore_cooldown(redis, account_id: int) -> None:
    """Rehydrate once per Redis lifetime/day, before allowing SDK access."""
    marker = f"vanguard:rpc:cooldown_loaded:{account_id}"
    if await redis.exists(marker):
        return
    from app.core.account.rpc_governor import load_state, parse_date
    from app.core.database import get_db_session

    async with get_db_session() as db:
        state = await load_state(db, account_id)
    until = parse_date(state.get("pause_until"))
    seconds = max(0, math.ceil((until - datetime.utcnow()).total_seconds())) if until else 0
    await redis.eval(
        "local ttl=redis.call('TTL',KEYS[1]);if tonumber(ARGV[1])>ttl and tonumber(ARGV[1])>0 then "
        "redis.call('SET',KEYS[1],'1','EX',ARGV[1]) end;redis.call('SET',KEYS[2],'1','EX',86400);return 1",
        2, f"vanguard:rpc:cooldown:{account_id}", marker, seconds,
    )


async def pace(account_id: int, *, reads: int = 1) -> None:
    """Called inside the session RPC gate, immediately before SDK entry.

    Redis failure is a pre-RPC failure. Cancellation never issues the request.
    A batch occupies one interval per read, so batching cannot evade pacing.
    """
    from app.core.account.rpc_governor import RpcDeferred
    from app.core.redis import get_redis

    try:
        redis = await get_redis()
        await restore_cooldown(redis, account_id)
        while True:
            cooldown = await redis.ttl(f"vanguard:rpc:cooldown:{account_id}")
            if cooldown > 0:
                raise RpcDeferred("telegram_rpc_cooldown", cooldown)
            if not reads:
                return
            delay = int(await redis.eval(
                PACE, 1, f"vanguard:rpc:pace:{account_id}",
                max(1, int(settings.TELEGRAM_RPC_MIN_INTERVAL_SECONDS * 1000)) * reads,
            ))
            if delay <= 0:
                return
            await asyncio.sleep(min(delay / 1000, 1.0))
    except RpcDeferred:
        raise
    except Exception as exc:
        raise RpcDeferred("telegram_rpc_guard_unavailable", 30) from exc
