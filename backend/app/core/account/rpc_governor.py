"""Shared read-RPC budget and durable Telegram cooldown, with no message retries.

These are local conservative budgets, not Telegram's undocumented thresholds.
Only RPC names are recorded: never request arguments, entities or message bodies.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.core.settings_models import SystemSetting

logger = structlog.get_logger()
PREFIX = "telegram.rpc.account."
BACKGROUND = {"join_candidate_preview", "group_metadata_sync", "auto_join_search", "search"}
READ_PREFIXES = ("Get", "Search", "Resolve", "CheckChatInvite")


class RpcDeferred(RuntimeError):
    """Local scheduling outcome; intentionally has no `seconds` attribute."""

    def __init__(self, reason: str, retry_after_seconds: int):
        self.reason = reason
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(f"{reason}: retry_after_seconds={self.retry_after_seconds}")


def parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def limits_for(state: dict, now: datetime) -> dict[str, int]:
    end = parse_date(state.get("pause_until"))
    factor = float(state.get("read_factor", 1.0))
    # Recover gradually only after a successful read in this recovery epoch.
    success = parse_date(state.get("last_success_at"))
    if end and success and success > end and now > end:
        factor = min(1.0, factor + 0.1 * int((now - end).total_seconds() // 86400))
    return {
        "minute": max(6, int(120 * factor)),
        "hour": max(30, int(240 * factor)),
        "day": max(120, int(2400 * factor)),
        "background_hour": max(10, int(60 * factor)),
        "background_day": max(40, int(480 * factor)),
    }


async def load_state(db: Any, account_id: int) -> dict:
    row = await db.get(SystemSetting, PREFIX + str(account_id))
    return json.loads(row.value) if row else {}


async def record_flood(
    db: Any, account_id: int, seconds: int, method: str, purpose: str, now: datetime | None = None
) -> dict:
    now = now or datetime.utcnow()
    key = PREFIX + str(account_id)
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == key).with_for_update())
    state = json.loads(row.value) if row else {}
    old_end = parse_date(state.get("pause_until"))
    end = max(old_end or now, now + timedelta(seconds=max(1, seconds) + 60))
    # Multiple observations during the same cooldown must not repeatedly halve it.
    if not old_end or old_end <= now:
        current = limits_for(state, now)["hour"] / 240
        state["read_factor"] = max(0.125, current / 2)
    state.update(
        pause_until=end.isoformat(),
        last_flood_at=now.isoformat(),
        method=method,
        purpose=purpose,
        wait_seconds=seconds,
    )
    if row is None:
        row = SystemSetting(key=key, description="Shared Telegram read cooldown and recovery")
        db.add(row)
    row.value = json.dumps(state)
    await db.flush()
    logger.warning(
        "telegram_rpc_flood_wait",
        account_id=account_id,
        method=method,
        purpose=purpose,
        wait_seconds=seconds,
        pause_until=end.isoformat(),
    )
    return state


async def read_budget_state(account_id: int, limits: dict, now: datetime) -> dict:
    """Atomic, read-only usage snapshot; never reserves or resets a budget."""
    from app.core.redis import get_redis
    windows = [("minute", 60), ("hour", 3600), ("day", 86400),
               ("background_hour", 3600), ("background_day", 86400)]
    cache = await get_redis()
    keys = [f"vanguard:rpc:{account_id}:{name}" for name, _ in windows]
    values = await cache.eval(
        "local r={} for _,k in ipairs(KEYS) do "
        "table.insert(r,tonumber(redis.call('GET',k) or '0')); "
        "table.insert(r,redis.call('TTL',k)); end return r", len(keys), *keys)
    usage, blocked, delay = {}, [], 0
    for i, (name, duration) in enumerate(windows):
        used, ttl = int(values[2*i]), int(values[2*i+1])
        exhausted = used >= limits[name]
        # A missing expiry on an exhausted counter is unavailable, never ready.
        wait = (max(1, ttl) if ttl >= 0 else duration) if exhausted else 0
        usage[name] = {"used": used, "limit": limits[name], "remaining": max(0, limits[name]-used),
                       "retry_after_seconds": wait, "ttl_seconds": ttl}
        if exhausted and not name.startswith("background_"):
            blocked.append(name); delay = max(delay, wait)
    emergency = max(0, int(await cache.ttl(f"vanguard:rpc:cooldown:{account_id}")))
    return {"usage": usage, "blocked_windows": blocked, "retry_after_seconds": max(delay, emergency),
            "emergency_cooldown_seconds": emergency}


def deferred_error(error: Exception | str) -> tuple[str, int] | None:
    """Recognize only pre-RPC scheduling outcomes, never unknown send outcomes."""
    if isinstance(error, RpcDeferred):
        return error.reason, error.retry_after_seconds
    match = re.fullmatch(r"(?:unknown:)?(telegram_read_budget|telegram_rpc_cooldown|telegram_rpc_guard_unavailable): retry_after_seconds=(\d+)", str(error))
    return (match[1], max(1, int(match[2]))) if match else None


async def snapshot(db: Any, account_id: int, now: datetime) -> dict:
    state = await load_state(db, account_id)
    end = parse_date(state.get("pause_until"))
    limits = limits_for(state, now)
    result = {**state, "state": "recovering" if limits["hour"] < 240 else "ready",
              "limits": limits, "reason": None, "resume_at": None}
    try:
        budget = await read_budget_state(account_id, limits, now)
        result.update(budget)
        delay = budget["retry_after_seconds"]
        if delay:
            result.update(state="budget_wait", reason="telegram_read_budget",
                          resume_at=(now + timedelta(seconds=delay + 1)).isoformat())
        if budget["emergency_cooldown_seconds"]:
            result.update(state="cooldown", reason="telegram_rpc_cooldown")
    except Exception:
        result.update(state="unavailable", reason="telegram_rpc_guard_unavailable",
                      retry_after_seconds=60, resume_at=(now + timedelta(seconds=60)).isoformat())
    if end and end > now:
        resume = max(end, parse_date(result.get("resume_at")) or end)
        result.update(state="cooldown", reason="telegram_rpc_cooldown",
                      resume_at=resume.isoformat(), retry_after_seconds=math.ceil((resume-now).total_seconds()))
    return result


async def check_read_ready(db: Any, account_id: int, now: datetime | None = None) -> dict:
    result = await snapshot(db, account_id, now or datetime.utcnow())
    if result["state"] in {"cooldown", "budget_wait", "unavailable"}:
        raise RpcDeferred(result["reason"], result["retry_after_seconds"])
    return result


# Fixed rolling windows start at the first reservation, not a wall-clock boundary.
# All windows are checked BEFORE any increment, atomically across every worker.
BUDGET_LUA = """
local delay = 0
for i,key in ipairs(KEYS) do
  local used = tonumber(redis.call('GET', key) or '0')
  if used + tonumber(ARGV[1]) > tonumber(ARGV[2*i]) then
    local ttl = redis.call('TTL', key)
    if ttl < 1 then ttl = tonumber(ARGV[2*i+1]) end
    delay = math.max(delay, ttl)
  end
end
if delay > 0 then return delay end
for i,key in ipairs(KEYS) do
  local count = redis.call('INCRBY', key, ARGV[1])
  if count == tonumber(ARGV[1]) then redis.call('EXPIRE', key, ARGV[2*i+1]) end
end
return 0
"""


class RpcGovernor:
    def __init__(self, account_id: int, purpose: Any, *, budget_reads: bool = True):
        self.account_id, self.purpose, self.budget_reads = account_id, purpose, budget_reads

    async def before(self, methods: list[str]) -> None:
        from app.core.database import get_db_session
        from app.core.redis import get_redis

        now = datetime.utcnow()
        try:
            async with get_db_session() as db:
                state = await load_state(db, self.account_id)
            until = parse_date(state.get("pause_until"))
            if until and until > now:
                raise RpcDeferred("telegram_rpc_cooldown", math.ceil((until - now).total_seconds()))
            redis = await get_redis()
            emergency = int(await redis.ttl(f"vanguard:rpc:cooldown:{self.account_id}"))
            if emergency > 0:
                raise RpcDeferred("telegram_rpc_cooldown", emergency)
            reads = sum(method.split(".")[-1].startswith(READ_PREFIXES) for method in methods)
            if not reads or not self.budget_reads:
                return
            limits = limits_for(state, now)
            windows = [("minute", 60), ("hour", 3600), ("day", 86400)]
            if self.purpose() in BACKGROUND:
                windows += [("background_hour", 3600), ("background_day", 86400)]
            keys = [f"vanguard:rpc:{self.account_id}:{name}" for name, _ in windows]
            args = [reads]
            for name, ttl in windows:
                args.extend([limits[name], ttl])
            redis = await get_redis()
            delay = int(await redis.eval(BUDGET_LUA, len(keys), *keys, *args))
            if delay:
                raise RpcDeferred("telegram_read_budget", delay)
            # Bounded telemetry; count attempts, including failures, never request content.
            key = f"vanguard:rpc:counts:{self.account_id}:{now:%Y%m%d}"
            async with redis.pipeline(transaction=True) as pipe:
                for method in methods:
                    pipe.hincrby(key, method, 1)
                pipe.expire(key, 7 * 86400)
                await pipe.execute()
        except RpcDeferred:
            raise
        except Exception as exc:
            logger.error(
                "telegram_rpc_guard_unavailable",
                account_id=self.account_id,
                error_type=type(exc).__name__,
            )
            raise RpcDeferred("telegram_rpc_guard_unavailable", 60) from exc

    async def failed(self, methods: list[str], exc: Exception) -> None:
        # Write floods keep the existing action-specific feedback. Read floods
        # block every workflow sharing this identity until Telegram's deadline.
        if "FloodWait" not in type(exc).__name__ or not any(
            method.split(".")[-1].startswith(READ_PREFIXES) for method in methods
        ):
            return
        from app.core.database import get_db_session
        from app.core.redis import get_redis

        cached = False
        try:
            redis = await get_redis()
            await redis.eval(
                "local t=redis.call('TTL',KEYS[1]);if t<tonumber(ARGV[1]) then "
                "redis.call('SET',KEYS[1],'1','EX',ARGV[1]) end;return 1",
                1,
                f"vanguard:rpc:cooldown:{self.account_id}",
                int(exc.seconds) + 60,
            )
            cached = True
        except Exception:
            logger.error("telegram_rpc_emergency_cache_failed", account_id=self.account_id)
        try:
            async with get_db_session() as db:
                await record_flood(
                    db, self.account_id, int(exc.seconds), ",".join(methods), self.purpose()
                )
            exc._vanguard_read_flood = True
        except Exception:
            logger.error("telegram_rpc_cooldown_persistence_failed", account_id=self.account_id)
            # Preserve the original Telegram error so existing callers also retain
            # its deadline. A Redis-only checkpoint still blocks every worker.
            exc._vanguard_read_flood = cached

    async def succeeded(self) -> None:
        from app.core.database import get_db_session

        async with get_db_session() as db:
            row = await db.scalar(
                select(SystemSetting)
                .where(SystemSetting.key == PREFIX + str(self.account_id))
                .with_for_update()
            )
            state = json.loads(row.value) if row else {}
            end = parse_date(state.get("pause_until"))
            if (
                end
                and end <= datetime.utcnow()
                and not (parse_date(state.get("last_success_at")) or datetime.min) > end
            ):
                state["last_success_at"] = datetime.utcnow().isoformat()
                row.value = json.dumps(state)


def install_governor(client: Any, governor: RpcGovernor) -> None:
    original = client._call

    async def guarded(
        sender: Any, request: Any, ordered: bool = False, flood_sleep_threshold: int | None = None
    ) -> Any:
        requests = request if isinstance(request, (list, tuple)) else [request]
        methods = [
            type(item).__module__.rsplit(".", 1)[-1] + "." + type(item).__name__
            for item in requests
        ]
        await governor.before(methods)
        try:
            result = await original(sender, request, ordered=ordered, flood_sleep_threshold=0)
        except Exception as exc:
            await governor.failed(methods, exc)
            raise
        # Never turn a completed write into an error because telemetry failed.
        try:
            await governor.succeeded()
        except Exception as exc:
            logger.warning("telegram_rpc_success_checkpoint_failed", error_type=type(exc).__name__)
        return result

    client._call = guarded
    client._vanguard_governor = governor
