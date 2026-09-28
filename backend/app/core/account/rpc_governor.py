"""Shared read-RPC budget and durable Telegram cooldown, with no message retries.

These are local conservative budgets, not Telegram's undocumented thresholds.
Only RPC names are recorded: never request arguments, entities or message bodies.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.core.account.rpc_budget_policy import (
    AD_PURPOSES,
    LANE_SHARES,
    LANES,
    WINDOWS,
    read_lane,
    window_plan,
)
from app.core.settings_models import SystemSetting

logger = structlog.get_logger()
PREFIX = "telegram.rpc.account."

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
    limits = {
        "minute": max(6, int(120 * factor)),
        "hour": max(30, int(240 * factor)),
        "day": max(120, int(2400 * factor)),
    }
    for lane, share in LANE_SHARES.items():
        for window in ("hour", "day"):
            # Round the protected allocation up to whole RPCs so recovering
            # accounts never reserve less than the requested 35 percent.
            reserve_rounding = 99 if lane in {"ad", "survival"} else 0
            limits[f"{lane}_{window}"] = max(1, (limits[window] * share + reserve_rounding) // 100)
    for window in ("hour", "day"):
        limits[f"non_ad_{window}"] = limits[window] - limits[f"ad_{window}"]
        limits[f"non_survival_{window}"] = limits[f"non_ad_{window}"] - limits[f"survival_{window}"]
    return limits



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
    windows = WINDOWS
    cache = await get_redis()
    keys = [f"vanguard:rpc:{account_id}:{name}" for name, _ in windows]
    values = await cache.eval(
        "local r={} for _,k in ipairs(KEYS) do "
        "table.insert(r,tonumber(redis.call('GET',k) or '0')); "
        "table.insert(r,redis.call('TTL',k)); end return r", len(keys), *keys)
    raw = {name: (int(values[2*i]), int(values[2*i+1])) for i, (name, _) in enumerate(windows)}
    # Virtual non-ad consumption retains all existing shared usage. Historical
    # reads before ad accounting existed are conservatively treated as non-ad.
    for window in ("hour", "day"):
        total, ttl = raw[window]
        ad_used = min(total, raw[f"ad_{window}"][0]) if ttl != -2 else 0
        raw[f"ad_{window}"] = (ad_used, ttl)
        raw[f"non_ad_{window}"] = (total - ad_used, ttl)
        survival_used = min(total - ad_used, raw[f"survival_{window}"][0]) if ttl != -2 else 0
        raw[f"survival_{window}"] = (survival_used, ttl)
        raw[f"non_survival_{window}"] = (total - ad_used - survival_used, ttl)
    usage, blocked, delay = {}, [], 0
    for name, duration in windows:
        used, ttl = raw[name]
        exhausted = used >= limits[name]
        # A missing expiry on an exhausted counter is unavailable, never ready.
        wait = (max(1, ttl) if ttl >= 0 else duration) if exhausted else 0
        usage[name] = {"used": used, "limit": limits[name], "remaining": max(0, limits[name]-used),
                       "retry_after_seconds": wait, "ttl_seconds": ttl}
        if exhausted and name in {"minute", "hour", "day"}:
            blocked.append(name)
            delay = max(delay, wait)
    emergency = max(0, int(await cache.ttl(f"vanguard:rpc:cooldown:{account_id}")))
    lanes = {}
    for lane in LANES:
        waits, remaining = [], []
        for name, duration, cap in window_plan(limits, lane):
            item = usage[name]
            remaining.append(max(0, cap - item["used"]))
            if item["used"] >= cap:
                waits.append(max(1, item["ttl_seconds"]) if item["ttl_seconds"] >= 0 else duration)
        lanes[lane] = {"remaining": min(remaining), "retry_after_seconds": max([emergency, *waits])}
    return {"usage": usage, "blocked_windows": blocked, "retry_after_seconds": max(delay, emergency),
            "emergency_cooldown_seconds": emergency, "lanes": lanes}


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
    for lane in result.get("lanes", {}).values():
        delay = max(lane["retry_after_seconds"], result.get("retry_after_seconds", 0))
        lane["retry_after_seconds"] = delay
        lane["resume_at"] = (now + timedelta(seconds=delay + 1)).isoformat() if delay else None
    return result


async def check_read_ready(
    db: Any, account_id: int, now: datetime | None = None, *, purpose: str = "ad_delivery",
    requires_bootstrap: bool = False,
) -> dict:
    result = await snapshot(db, account_id, now or datetime.utcnow())
    required_lanes = {read_lane([], purpose)}
    if requires_bootstrap:
        # A short connection authorizes via GetState before collecting evidence.
        # Reuse the RPC policy so critical purposes retain their existing lane.
        required_lanes.add(read_lane(["updates.GetStateRequest"], purpose))
    delay = max(
        result.get("lanes", {}).get(lane, {}).get("retry_after_seconds", 0)
        for lane in required_lanes
    )
    if delay and result["state"] not in {"cooldown", "unavailable"}:
        raise RpcDeferred("telegram_read_budget", delay)
    if result["state"] in {"cooldown", "budget_wait", "unavailable"}:
        raise RpcDeferred(result["reason"], result["retry_after_seconds"])
    return result


# Fixed rolling windows start at the first reservation, not a wall-clock boundary.
# All windows are checked BEFORE any increment, atomically across every worker.
BUDGET_LUA = """
local delay = 0
-- Shared counters remain the original first three windows. The last five keys
-- are ad-hour/day, survival-hour/day lookups and pacing. No migration reset.
local windows = #KEYS - 5
local function protected(parent)
  local total = tonumber(redis.call('GET', KEYS[parent]) or '0')
  local offset = parent == 2 and 0 or 1
  local ad = math.min(total, tonumber(redis.call('GET', KEYS[windows+1+offset]) or '0'))
  local survival = math.min(total-ad, tonumber(redis.call('GET', KEYS[windows+3+offset]) or '0'))
  return total, ad, survival
end
local function parent_for(key)
  if string.match(key, '_hour$') then return 2 end
  return 3
end
local function virtual(key)
  return string.match(key, ':non_ad_') or string.match(key, ':non_survival_')
end
local function aligned(key)
  return virtual(key) or string.match(key, ':ad_') or string.match(key, ':survival_')
end
local function used_for(i)
  local key = KEYS[i]
  if aligned(key) then
    local total, ad, survival = protected(parent_for(key))
    if string.match(key, ':non_ad_') then return total-ad end
    if string.match(key, ':non_survival_') then return total-ad-survival end
    if string.match(key, ':survival_') then return survival end
    return ad
  end
  return tonumber(redis.call('GET',key) or '0')
end
for i=1,windows do
  if used_for(i)+tonumber(ARGV[1]) > tonumber(ARGV[2*i+1]) then
    local key=KEYS[i]
    local ttl=redis.call('TTL', aligned(key) and KEYS[parent_for(key)] or key)
    if ttl < 1 then ttl=tonumber(ARGV[2*i+2]) end
    delay=math.max(delay,ttl)
  end
end
if delay > 0 then return delay*1000 end
local pace=tonumber(ARGV[2])
local t=redis.call('TIME')
local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
if pace > 0 then
  local due=tonumber(redis.call('GET',KEYS[#KEYS]) or '0')
  local burst=tonumber(ARGV[3+windows*2]) or 1
  local eligible=due-math.max(0,burst-tonumber(ARGV[1]))*pace
  if eligible > now then return -(eligible-now) end
end
for i=1,windows do
  if not virtual(KEYS[i]) then
    local count=redis.call('INCRBY',KEYS[i],ARGV[1])
    if count == tonumber(ARGV[1]) then
      redis.call('EXPIRE',KEYS[i],ARGV[2*i+2])
      if i == 2 or i == 3 then
        local offset=i == 2 and 0 or 1
        redis.call('DEL',KEYS[windows+1+offset],KEYS[windows+3+offset])
      end
    end
    if aligned(KEYS[i]) then
      local ttl=redis.call('PTTL',KEYS[parent_for(KEYS[i])])
      if ttl > 0 then redis.call('PEXPIRE',KEYS[i],ttl) end
    end
  end
end
if pace > 0 then
  local due=math.max(now,tonumber(redis.call('GET',KEYS[#KEYS]) or '0'))+pace*tonumber(ARGV[1])
  redis.call('SET',KEYS[#KEYS],due,'PX',due-now+60000)
end
return 0
"""


class RpcGovernor:
    def __init__(self, account_id: int, purpose: Any, *, budget_reads: bool = True):
        self.account_id, self.purpose, self.budget_reads = account_id, purpose, budget_reads

    async def before(self, methods: list[str], *, sync: bool = False) -> None:
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
            purpose = str(self.purpose())
            from app.core.account.event_inbox import current_event_id
            if current_event_id.get() is not None and not sync and purpose not in (AD_PURPOSES | {"ad_survival_check"}):
                purpose = "growth_event"
            lane = "sync" if sync else read_lane(methods, purpose)
            windows = window_plan(limits, lane)
            keys = [f"vanguard:rpc:{self.account_id}:{name}" for name, _, _ in windows]
            keys.extend(f"vanguard:rpc:{self.account_id}:ad_{window}" for window in ("hour", "day"))
            keys.extend(f"vanguard:rpc:{self.account_id}:survival_{window}" for window in ("hour", "day"))
            keys.append(f"vanguard:rpc:{self.account_id}:{'sync_pace' if lane == 'sync' else 'scan_pace'}")
            # A four-read burst permits bootstrap and short differences. The
            # sustained sync rate fits its day share, not merely its hour peak.
            pace = (math.ceil(86400000 / limits["sync_day"]) if lane == "sync"
                    else {"routine": 1500, "background": 3000}.get(lane, 0))
            args = [reads, pace]
            for _name, ttl, cap in windows:
                args.extend([cap, ttl])
            args.append(4 if lane == "sync" else 1)
            redis = await get_redis()
            while True:
                delay = int(await redis.eval(BUDGET_LUA, len(keys), *keys, *args))
                if delay >= 0:
                    break
                if lane == "sync":
                    raise RpcDeferred("telegram_read_budget", math.ceil(-delay / 1000))
                # A paced wait never reserves budget or sends an RPC. Shared
                # Redis time coordinates scans in different worker processes.
                from app.core.account.read_timeout import paced_sleep
                await paced_sleep(min(3.0, -delay / 1000))
                async with get_db_session() as db:
                    fresh = await load_state(db, self.account_id)
                until = parse_date(fresh.get("pause_until"))
                emergency = int(await redis.ttl(f"vanguard:rpc:cooldown:{self.account_id}"))
                wait = max(emergency, math.ceil((until-datetime.utcnow()).total_seconds()) if until else 0)
                if wait > 0:
                    raise RpcDeferred("telegram_rpc_cooldown", wait)
            if delay:
                raise RpcDeferred("telegram_read_budget", math.ceil(delay / 1000))
            from app.modules.acquisition.read_costs import charge_reads
            charge_reads(self.account_id, reads, lane)
            # Bounded telemetry; count attempts, including failures, never request content.
            key = f"vanguard:rpc:counts:{self.account_id}:{now:%Y%m%d}"
            async with redis.pipeline(transaction=True) as pipe:
                for method in methods:
                    pipe.hincrby(key, method, 1)
                pipe.expire(key, 7 * 86400)
                usage_key = f"vanguard:rpc:usage:{self.account_id}:{now:%Y%m%d}"
                purpose = purpose if re.fullmatch(r"[a-z_]{1,64}", purpose) else "other"
                for method in methods:
                    pipe.hincrby(usage_key, f"{lane}|{purpose}|{method}", 1)
                pipe.expire(usage_key, 7 * 86400)
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
        internal_sync = bool(methods) and all(method.split(".")[-1].startswith(READ_PREFIXES) for method in methods)
        internal_sync = internal_sync and asyncio.current_task() is getattr(client, "_updates_handle", None)
        while True:
            try:
                if internal_sync:
                    await governor.before(methods, sync=True)
                else:
                    await governor.before(methods)
                break
            except RpcDeferred as exc:
                if not internal_sync:
                    raise
                from app.core.account.listener_budget_wait import (
                    can_receive_while_waiting, can_wait_global, wait_global,
                    receive_while_waiting,
                )
                if exc.reason == "telegram_read_budget" and can_receive_while_waiting(client, requests):
                    await receive_while_waiting(client, exc)
                    continue
                if exc.reason == "telegram_read_budget" and can_wait_global(client, requests):
                    await wait_global(client, exc)
                    continue
                pause = getattr(client, "_vanguard_listener_pause", None)
                if pause is not None:
                    # A separate task disconnects and cancels this consumer.
                    # The client/session and queued updates remain for catch-up.
                    pause.request(exc)
                    await asyncio.Future()
                # This is a pre-RPC wait, never a resend. Cancellation from disconnect()
                # propagates, and each wake rechecks shared limits and Telegram cooldown.
                await asyncio.sleep(min(60, exc.retry_after_seconds + 1))
        if internal_sync:
            client._vanguard_sync_wait = None
        try:
            if any(not method.split(".")[-1].startswith(READ_PREFIXES) for method in methods):
                from app.core.account.event_inbox import mark_external_attempt
                await mark_external_attempt()
            result = await original(sender, request, ordered=ordered, flood_sleep_threshold=0)
        except Exception as exc:
            await governor.failed(methods, exc)
            raise
        if internal_sync:
            if type(result).__name__ in {"DifferenceTooLong", "ChannelDifferenceTooLong"}:
                journal = getattr(getattr(client, 'session', None), 'journal', None)
                if journal is not None:
                    journal.reconciliation_required()
            from app.core.account.listener_budget_wait import record_sync_result
            await record_sync_result(governor, requests, result)
        # Never turn a completed write into an error because telemetry failed.
        try:
            await governor.succeeded()
        except Exception as exc:
            logger.warning("telegram_rpc_success_checkpoint_failed", error_type=type(exc).__name__)
        return result

    client._call = guarded
    client._vanguard_governor = governor
