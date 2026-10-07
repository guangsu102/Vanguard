"""Shared read-RPC budget and durable Telegram cooldown, with no message retries.

These are local conservative budgets, not Telegram's undocumented thresholds.
Only RPC names are recorded: never request arguments, entities or message bodies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.core.account.rpc_budget_policy import (
    AD_PURPOSES,
    LANES,
    LISTENER_CONTROL_METHODS,
    WINDOWS,
    read_lane,
)
from app.core.account.session_gate import AccountSessionGate
from app.core.account.session_guard import is_wrong_session_error
from app.core.config import settings
from app.core.settings_models import SystemSetting

logger = structlog.get_logger()
PREFIX = "telegram.rpc.account."
_SESSION_GATE = AccountSessionGate()

READ_PREFIXES = ("Get", "Search", "Resolve", "CheckChatInvite")


class RpcDeferred(RuntimeError):
    """Local scheduling outcome; intentionally has no `seconds` attribute."""

    def __init__(self, reason: str, retry_after_seconds: int):
        self.reason = reason
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(f"{reason}: retry_after_seconds={self.retry_after_seconds}")


def parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def recovery_factor(state: dict, now: datetime) -> float:
    """Account recovery is independent of the configured read allowance."""
    end = parse_date(state.get("pause_until"))
    factor = float(state.get("read_factor", 1.0))
    # Recover gradually only after a successful read in this recovery epoch.
    success = parse_date(state.get("last_success_at"))
    if end and success and success > end and now > end:
        factor = min(1.0, factor + 0.1 * int((now - end).total_seconds() // 86400))
    return factor


def limits_for(state: dict, now: datetime) -> dict[str, int]:
    factor = recovery_factor(state, now)
    percent = settings.TELEGRAM_READ_BUDGET_PERCENT
    limits = {
        name: max(1, max(floor, int(base * factor)) * percent // 100)
        for name, base, floor in (("minute", 120, 6), ("hour", 240, 30), ("day", 2400, 120))
    }
    from app.core.account.reserved_reads import allocations, listener_reserve
    for window in ("hour", "day"):
        ad, survival, joining, sync, flex = allocations(limits[window])
        for name, value in {"ad": ad, "survival": survival, "critical": joining,
                            "sync": sync, "routine": flex, "background": flex,
                            "join_reserved": joining, "sync_reserved": sync,
                            "flex_reserved": flex}.items():
            limits[f"{name}_{window}"] = value
        limits[f"non_ad_{window}"] = limits[window] - ad
        limits[f"non_survival_{window}"] = limits[window] - ad - survival
        limits[f"listener_{window}"] = listener_reserve(limits[window])
        limits[f"survival_lent_{window}"] = survival
        limits[f"sync_lent_{window}"] = sync
        limits[f"ad_lent_{window}"] = ad
        limits[f"critical_join_{window}"] = joining
        limits[f"critical_review_{window}"] = joining
        limits[f"ad_pool_{window}"] = ad + flex
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
        current = recovery_factor(state, now)
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
    from app.core.account.reserved_reads import (
        COUNTERS,
        LANE_INDEX,
        ad_send_hold,
        allocations,
        available,
        classified,
        transferred_caps,
    )
    lane_windows = {lane: [] for lane in LANES}
    lane_windows["ad_refresh"] = []
    guaranteed = {}
    legacy_attribution = {}
    for window in ("hour", "day"):
        total, ttl = raw[window]
        caps = allocations(limits[window])
        measured = [raw[f"{name}_{window}"][0] if ttl != -2 else 0 for name in COUNTERS]
        used = classified(total, measured, caps)
        legacy_attribution[window] = {"unclassified": max(0, total-sum(measured)),
                                      "assigned": dict(zip(COUNTERS, [max(0,a-b) for a,b in zip(used,measured, strict=True)], strict=True))}
        lent = raw[f"survival_lent_{window}"][0] if ttl != -2 else 0
        sync_lent = raw[f"sync_lent_{window}"][0] if ttl != -2 else 0
        ad_lent = raw[f"ad_lent_{window}"][0] if ttl != -2 else 0
        caps = transferred_caps(caps, lent, sync_lent, ad_lent)
        raw[f"survival_lent_{window}"] = (lent, ttl)
        raw[f"sync_lent_{window}"] = (sync_lent, ttl)
        raw[f"ad_lent_{window}"] = (ad_lent, ttl)
        control_used = raw[f"listener_{window}"][0] if ttl != -2 else 0
        control_room = max(0, limits[f"listener_{window}"]-control_used)
        raw[f"listener_{window}"] = (control_used, ttl)
        for kind in ("join", "review"):
            name = f"critical_{kind}_{window}"
            raw[name] = (raw[name][0] if ttl != -2 else 0, ttl)
        duration = 3600 if window == "hour" else 86400
        safety_room = available(limits[window], used, caps, "survival")
        lane_windows["listener"].append((min(control_room, safety_room), ttl, duration))
        for lane, index in LANE_INDEX.items():
            raw[f"{lane}_{window}"] = (used[index], ttl)
            guaranteed[f"{lane}_{window}"] = caps[index]
            room = available(limits[window], used, caps, lane)
            if lane == "survival":
                room = max(0, room-control_room)
            lane_windows[lane].append((room, ttl, duration))
            if lane == "ad":
                lane_windows["ad_refresh"].append((max(0, room-ad_send_hold(caps[0], caps[4])), ttl, duration))
        raw[f"non_ad_{window}"] = (total-used[0], ttl)
        raw[f"non_survival_{window}"] = (total-used[0]-used[1], ttl)
        raw[f"ad_pool_{window}"] = (used[0] + max(0, used[4]), ttl)
    usage, blocked, delay = {}, [], 0
    for name, duration in windows:
        used, ttl = raw[name]
        cap = limits[name]
        exhausted = used >= cap
        wait = (max(1, ttl) if ttl >= 0 else duration) if exhausted else 0
        usage[name] = {"used": used, "limit": cap, "remaining": max(0, cap-used),
                       "retry_after_seconds": wait, "ttl_seconds": ttl}
        if name in guaranteed:
            usage[name]["guaranteed_limit"] = guaranteed[name]
        if exhausted and name in {"minute", "hour", "day"}:
            blocked.append(name)
            delay = max(delay, wait)
    emergency = max(0, int(await cache.ttl(f"vanguard:rpc:cooldown:{account_id}")))
    lanes = {}
    for lane, items in lane_windows.items():
        waits, remaining = [], [usage["minute"]["remaining"]]
        for room, ttl, duration in items:
            remaining.append(room)
            if room <= 0:
                waits.append(max(1, ttl) if ttl >= 0 else duration)
        lanes[lane] = {"remaining": min(remaining), "retry_after_seconds": max([emergency, delay, *waits])}
    from app.core.account.read_work import reservation_snapshot
    reservation = await reservation_snapshot(cache, account_id)
    return {"usage": usage, "work_reservation": reservation, "blocked_windows": blocked, "retry_after_seconds": max(delay, emergency),
            "emergency_cooldown_seconds": emergency, "lanes": lanes, "reservation_policy": "ad35_survival15_join25_sync20_flex5",
            "listener_reserve_policy": "survival_includes_listener2_min3", "legacy_attribution": legacy_attribution}



def deferred_error(error: Exception | str) -> tuple[str, int] | None:
    """Recognize only pre-RPC scheduling outcomes, never unknown send outcomes."""
    if isinstance(error, RpcDeferred):
        return error.reason, error.retry_after_seconds
    from app.core.account.outbound_budget import OutboundBudgetBlocked

    if isinstance(error, OutboundBudgetBlocked):
        # The outbound budget raises before any Telegram RPC is issued; a
        # local pacing/quota rejection must never be recorded as a failed or
        # unknown-outcome send.
        return error.reason, max(1, int(error.retry_after_seconds or 60))
    match = re.fullmatch(
        r"(?:unknown:)?(telegram_read_budget|telegram_rpc_cooldown"
        r"|telegram_rpc_guard_unavailable|outbound_[a-z_]+)"
        r": retry_after_seconds=(\d+)",
        str(error),
    )
    return (match[1], max(1, int(match[2]))) if match else None


async def snapshot(db: Any, account_id: int, now: datetime) -> dict:
    state = await load_state(db, account_id)
    end = parse_date(state.get("pause_until"))
    limits = limits_for(state, now)
    factor = recovery_factor(state, now)
    result = {**state, "state": "recovering" if factor < 1.0 else "ready",
              "budget_percent": settings.TELEGRAM_READ_BUDGET_PERCENT,
              "effective_read_factor": factor,
              "limits": limits, "reason": None, "resume_at": None}
    stage = "read_budget_state"
    try:
        budget = await read_budget_state(account_id, limits, now)
        from app.core.account.budget_forecast import apply_forecast
        from app.core.account.demand_lending import add_ad_demand_headroom
        from app.core.account.join_demand_lending import add_join_demand_headroom
        from app.core.account.survival_lending import add_idle_survival_headroom
        from app.core.account.sync_lending import add_idle_sync_headroom
        from app.core.account.work_demand import add_work_demand
        for stage, forecast in (
            ("survival_lending", add_idle_survival_headroom),
            ("sync_lending", add_idle_sync_headroom),
            ("work_demand", add_work_demand),
            ("ad_demand_lending", add_ad_demand_headroom),
            ("join_demand_lending", add_join_demand_headroom),
        ):
            await apply_forecast(db, account_id, now, stage, forecast, limits, budget)
        stage = "workload_budgets"
        from app.core.account.critical_fairness import add_workload_budgets
        add_workload_budgets(budget, limits)
        result.update(budget)
        delay = budget["retry_after_seconds"]
        if delay:
            result.update(state="budget_wait", reason="telegram_read_budget",
                          resume_at=(now + timedelta(seconds=delay + 1)).isoformat())
        if budget["emergency_cooldown_seconds"]:
            result.update(state="cooldown", reason="telegram_rpc_cooldown")
    except Exception as exc:
        logger.error(
            "telegram_read_snapshot_failed", account_id=account_id,
            stage=stage, error_type=type(exc).__name__, exc_info=True,
        )
        if not db.is_active:
            raise
        result.update(state="unavailable", reason="telegram_rpc_guard_unavailable",
                      retry_after_seconds=60, resume_at=(now + timedelta(seconds=60)).isoformat())
    if end and end > now:
        resume = max(end, parse_date(result.get("resume_at")) or end)
        result.update(state="cooldown", reason="telegram_rpc_cooldown",
                      resume_at=resume.isoformat(), retry_after_seconds=math.ceil((resume-now).total_seconds()))
    for lane in [*result.get("lanes", {}).values(), *result.get("critical_workloads", {}).values()]:
        delay = max(lane["retry_after_seconds"], result.get("retry_after_seconds", 0))
        lane["retry_after_seconds"] = delay
        lane["resume_at"] = (now + timedelta(seconds=delay + 1)).isoformat() if delay else None
    return result


async def check_read_ready(
    db: Any, account_id: int, now: datetime | None = None, *, purpose: str = "ad_delivery",
    requires_bootstrap: bool = False,
    minimum_reads: int = 1,
    work_group_id: int | None = None, work_scope: str = "",
) -> dict:
    result = await snapshot(db, account_id, now or datetime.utcnow())
    from app.core.account.critical_fairness import purpose_budget
    required_lanes = {read_lane([], purpose)}
    held = result.get("work_reservation") or {}
    reserved = (work_group_id is not None and held.get("pending") is True
                and held.get("kind") == "review" and held.get("group_id") == work_group_id
                and held.get("scope") == work_scope and held.get("remaining", 0) >= minimum_reads)
    if requires_bootstrap:
        # A short connection authorizes via GetState before collecting evidence.
        # Reuse the RPC policy so critical purposes retain their existing lane.
        required_lanes.add(read_lane(["updates.GetStateRequest"], purpose))
    if reserved:
        required_lanes.discard("critical")
    delay = max((
        result.get("lanes", {}).get(lane, {}).get("retry_after_seconds", 0)
        for lane in required_lanes
    ), default=0)
    if not reserved:
        delay = max(delay, purpose_budget(result, purpose).get("retry_after_seconds", 0))
    if delay and result["state"] not in {"cooldown", "unavailable"}:
        raise RpcDeferred("telegram_read_budget", delay)
    if result["state"] in {"cooldown", "budget_wait", "unavailable"}:
        raise RpcDeferred(result["reason"], result["retry_after_seconds"])
    if not reserved and minimum_reads > 1 and purpose_budget(result, purpose).get("remaining", 0) < minimum_reads:
        from app.core.account.read_work import minimum_wait
        raise RpcDeferred("telegram_read_budget", minimum_wait(result, purpose, minimum_reads))
    return result


async def check_dispatch_ready(db: Any, account_id: int, now: datetime | None = None) -> dict:
    """Check shared cooldowns without presuming that a send requires a read.

    Every actual RPC still enters RpcGovernor.before; reads retain the same Lua
    budget and writes retain the same account cooldown and outbound ledger.
    """
    result = await snapshot(db, account_id, now or datetime.utcnow())
    if result["state"] in {"cooldown", "unavailable"} or (
        result["state"] == "budget_wait" and result.get("reason") != "telegram_read_budget"
    ):
        raise RpcDeferred(result["reason"], result["retry_after_seconds"])
    return result


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
            if not self.budget_reads:
                return
            limits = limits_for(state, now)
            purpose = str(self.purpose())
            from app.core.account.event_inbox import current_event_id
            if current_event_id.get() is not None and not sync and purpose not in (AD_PURPOSES | {"ad_survival_check"}):
                purpose = "growth_event"
            control = all(method in LISTENER_CONTROL_METHODS for method in methods)
            lane = ("listener" if control else "sync") if sync else read_lane(methods, purpose)
            if (not sync and getattr(self, "bootstrapping", False)
                    and all(m in LISTENER_CONTROL_METHODS | {"updates.GetDifferenceRequest"} for m in methods)):
                # SDK _on_login needs one initial difference after GetState.
                # Background catch-up remains on the separate sync lane.
                lane = "listener"
            from app.core.account import read_work
            work = read_work.current(self.account_id) if not sync and (
                lane == 'critical' or purpose == 'ad_qualification_refresh'
            ) else None
            if not reads and work is None and purpose != "join_candidate_preview":
                return
            if lane == "critical" or work is not None or purpose == 'ad_qualification_refresh':
                # Recompute before each actual read. The Lua allocator retains
                # all totals/TTLs and applies the buffer to current counters.
                async with get_db_session() as db:
                    current = await snapshot(db, self.account_id, now)
                    if current["state"] not in {"cooldown", "unavailable", "budget_wait"} and work is not None:
                        await read_work.ensure(db, self.account_id, current["limits"], redis)
                    elif current["state"] not in {"cooldown", "unavailable", "budget_wait"} and purpose == "join_candidate_preview":
                        from app.core.account.critical_fairness import purpose_budget
                        # Preview must leave enough to join and perform a first
                        # review. Stop before connecting if a new join is unfinished.
                        pending = await read_work.initial_pending(db, self.account_id, 0)
                        if pending:
                            raise RpcDeferred("join_initial_review_pending", max(60, math.ceil((pending[1] - now).total_seconds())))
                        required = read_work.DEFAULT_JOIN_READS + read_work.DEFAULT_REVIEW_READS + reads
                        if purpose_budget(current, purpose).get("remaining", 0) < required:
                            raise RpcDeferred("telegram_read_budget", read_work.minimum_wait(current, purpose, required))
                if current["state"] in {"cooldown", "unavailable", "budget_wait"}:
                    raise RpcDeferred(current["reason"], current["retry_after_seconds"])
                limits = current["limits"]
            if work is not None and work.reads + reads > work.limit:
                work.defer_reason = "telegram_read_slice"
                raise RpcDeferred("telegram_read_slice", 60)
            if not reads:
                return
            # Finish an outstanding difference before live traffic outgrows it.
            # This only changes smoothing; minute/hour/day and the protected
            # advertising/survival pools still bound every actual RPC.
            catchup = lane == "sync" and getattr(self, "catchup", False)
            pace = (1000 if catchup
                    else math.ceil(86400000 / limits["sync_day"]) if lane == "sync"
                    else {"routine": 1500, "background": 3000}.get(lane, 0))
            if lane == "sync" and getattr(self, "bootstrapping", False):
                # A connection must finish get_me/GetState/GetDifference together.
                # Pacing each restart can otherwise consume one token forever.
                # Hour/day/minute counters and platform cooldowns still apply.
                pace = 0
            from app.core.account.critical_fairness import workload
            from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
            keys, args = reservation_args(self.account_id, limits, lane, reads, pace,
                                           workload=workload(purpose) if lane == "critical" else None,
                                           ad_refresh=purpose == "ad_qualification_refresh",
                                           sync_catchup=catchup and pace > 0)
            redis = await get_redis()
            while True:
                delay = int(await redis.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args))
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
            if delay > 0 and lane == "sync" and sync and all(
                method in {"updates.GetDifferenceRequest", "updates.GetChannelDifferenceRequest"}
                for method in methods
            ):
                # Protocol recovery shares the existing bounded listener reserve.
                # It never creates tokens or borrows an admitted review/send slice.
                recovery_keys, recovery_args = reservation_args(self.account_id, limits, "listener", reads, 0)
                # Keep two hourly/four daily control RPCs for connection health.
                recovery_args[20] = max(0, recovery_args[20] - 2)
                recovery_args[21] = max(0, recovery_args[21] - 4)
                recovery_delay = int(await redis.eval(RESERVED_BUDGET_LUA, len(recovery_keys),
                                                     *recovery_keys, *recovery_args))
                if recovery_delay == 0:
                    lane, delay = "listener", 0
                elif recovery_delay > 0:
                    delay = min(delay, recovery_delay)
                if delay > 0 and getattr(self, "completing", False):
                    from app.core.account.sync_completion import reserve
                    async with get_db_session() as db:
                        completion_budget = await snapshot(db, self.account_id, datetime.utcnow())
                    completion_delay = await reserve(redis, self.account_id, completion_budget, reads)
                    if completion_delay == 0:
                        lane, delay = "survival", 0
                    else:
                        delay = min(delay, completion_delay)
            if delay:
                raise RpcDeferred("telegram_read_budget", math.ceil(delay / 1000))
            from app.modules.acquisition.read_costs import charge_reads
            charge_reads(self.account_id, reads, lane)
            if not sync:
                read_work.charged(self.account_id, methods)
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
                exc_info=True,
            )
            raise RpcDeferred("telegram_rpc_guard_unavailable", 60) from exc

    async def failed(self, methods: list[str], exc: Exception) -> None:
        from app.core.account import read_work
        read_work.failed(self.account_id, methods, exc)
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
        if getattr(client, "_vanguard_session_quarantined", False):
            raise RuntimeError(
                f"telegram session quarantined for account {getattr(governor, 'account_id', None)}; replay forbidden"
            )
        methods = [
            type(item).__module__.rsplit(".", 1)[-1] + "." + type(item).__name__
            for item in requests
        ]
        internal_sync = bool(methods) and all(method.split(".")[-1].startswith(READ_PREFIXES) for method in methods)
        internal_sync = internal_sync and asyncio.current_task() is getattr(client, "_updates_handle", None)
        while True:
            try:
                if internal_sync:
                    from app.core.account.sync_completion import continuing
                    governor.completing = continuing(client, requests)
                    # Persisted backlog or an unfinished server slice needs a
                    # short catch-up burst. A normal idle listener stays paced.
                    queue = getattr(client, "_updates_queue", None)
                    governor.catchup = bool(
                        getattr(client, "_vanguard_durable_checkpoint", False)
                        and ((queue is not None and queue.qsize() >= 500)
                             or governor.completing)
                    )
                    await governor.before(methods, sync=True)
                else:
                    await governor.before(methods)
                break
            except RpcDeferred as exc:
                if not internal_sync:
                    raise
                from app.core.account.listener_budget_wait import (
                    can_receive_while_waiting,
                    can_wait_global,
                    receive_while_waiting,
                    wait_global,
                )
                if exc.reason == "telegram_read_budget" and can_receive_while_waiting(client, requests):
                    await receive_while_waiting(client, exc)
                    continue
                if exc.reason == "telegram_read_budget" and can_wait_global(client, requests):
                    await wait_global(client, exc)
                    continue
                if exc.reason == "telegram_read_budget" and all(m in LISTENER_CONTROL_METHODS for m in methods):
                    # Local control-budget waits must not tear down a push transport.
                    # Ingress remains durable; SDK cursors and this request stay frozen.
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
            from app.core.account.send_receipts import record_request
            await record_request(getattr(governor, "account_id", None), requests)
            # One Telegram auth key must never have concurrent RPCs in this
            # process.  The operation is executed exactly once; a wrong
            # session response is surfaced and quarantines the client rather
            # than being replayed against a new connection.
            async with _SESSION_GATE.operation(getattr(governor, "account_id", "unknown")):
                result = await original(sender, request, ordered=ordered, flood_sleep_threshold=0)
            await record_request(getattr(governor, "account_id", None), requests, result)
        except Exception as exc:
            await governor.failed(methods, exc)
            if is_wrong_session_error(exc):
                client._vanguard_session_quarantined = True
                guard = getattr(client, "_vanguard_session_guard", None)
                if guard is not None:
                    with contextlib.suppress(Exception):
                        await guard._quarantine(exc)
                with contextlib.suppress(Exception):
                    if client.is_connected():
                        await client.disconnect()
                logger.error(
                    "telegram_session_quarantined_no_replay",
                    account_id=getattr(governor, "account_id", None),
                    methods=methods,
                )
            raise
        if internal_sync:
            client._vanguard_difference_continuation = type(result).__name__ == "DifferenceSlice"
            from app.core.account.sync_completion import record
            record(client, requests, result)
            if type(result).__name__ in {"DifferenceTooLong", "ChannelDifferenceTooLong"}:
                journal = getattr(getattr(client, 'session', None), 'journal', None)
                if journal is not None:
                    journal.reconciliation_required(requests[0])
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
