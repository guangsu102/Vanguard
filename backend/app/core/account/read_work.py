"""Bounded read work: reserve before connecting, charge only actual RPCs."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

LEASE_SECONDS = 480
DEFAULT_REVIEW_READS = 24
DEFAULT_JOIN_READS = 12
DEFAULT_RENEWAL_READS = 16
RETENTION_SECONDS = 7 * 86400


def key(account_id: int) -> str:
    return f"vanguard:rpc:work:{account_id}"


def sample_key(account_id: int, kind: str) -> str:
    return f"vanguard:rpc:work_cost:{account_id}:{kind}"


@dataclass
class ReadWork:
    account_id: int
    group_id: int
    kind: str
    scope: str = ""
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    claimed: bool = False
    handed_off: bool = False
    closed: bool = False
    limit: int = 0
    review_reads: int = DEFAULT_REVIEW_READS
    phase: str = "full"
    requested_reads: int = DEFAULT_REVIEW_READS
    planned_reads: int | None = None
    reads: int = 0
    entered_reads: int = 0
    outcome: str = "interrupted"
    defer_reason: str | None = None
    methods: Counter = field(default_factory=Counter)
    failures: Counter = field(default_factory=Counter)
    ledger_id: str | None = None
    started: float = field(default_factory=time.monotonic)
    budget_workload: str | None = None
    budget_claim: Any = None
    budget_bind_failed: bool = False

    def summary(self) -> dict[str, Any]:
        from app.modules.acquisition.read_costs import COST_VERSION
        return {
            "path": COST_VERSION,
            "version": 1,
            "reads": self.reads,
            "entered_sdk_reads": self.entered_reads,
            "measurement_stage": getattr(self, "measurement_stage", "unknown"),
            "reserved_reads": self.limit,
            "methods": dict(self.methods),
            "failures": dict(self.failures),
            "kind": self.kind,
            "budget_workload": self.budget_workload,
            "phase": self.phase,
            "outcome": self.outcome,
            "defer_reason": self.defer_reason,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
        }


_current: ContextVar[ReadWork | None] = ContextVar("critical_read_work", default=None)


def current(account_id: int | None = None) -> ReadWork | None:
    work = _current.get()
    return (
        work
        if work is not None
        and not work.closed
        and (account_id is None or work.account_id == account_id)
        else None
    )


def lane_for(work: ReadWork) -> str:
    return {"renewal": "ad", "ad_reconciliation": "ad", "survival": "survival"}.get(work.kind, "critical")


def budget_kind(work: ReadWork, purpose: str = "") -> str:
    """Classify spending without changing the work's evidence/cost identity."""
    if work.budget_workload in {"join", "review"}:
        return work.budget_workload
    if work.kind in {"join", "join_reconciliation"}:
        return "join"
    from app.core.account.critical_fairness import workload

    return workload(purpose)


def charged(account_id: int, methods: list[str]) -> None:
    work = current(account_id)
    if work is not None:
        from app.core.account.rpc_governor import READ_PREFIXES

        reads = [method for method in methods if method.split(".")[-1].startswith(READ_PREFIXES)]
        work.reads += len(reads)
        work.methods.update(reads)


def failed(account_id: int, methods: list[str], exc: Exception) -> None:
    work = current(account_id)
    if work is not None:
        work.failures[type(exc).__name__] += 1


def estimate(samples: list[str], fallback: int, now: float, *, minimum: int | None = None, phase: str | None = None) -> int:
    completed = []
    for raw in samples:
        try:
            item = json.loads(raw)
            if (
                type(item.get("reads")) is int
                and 0 < item["reads"] <= 64
                and now - RETENTION_SECONDS <= item["at"] <= now
                and item.get("outcome")
                in {"allowed", "trial", "observe", "wait", "reject", "joined"}
                and (phase is None or item.get('phase', 'full') == phase)
            ):
                completed.append(item["reads"])
        except (ValueError, TypeError, KeyError):
            continue
    if len(completed) < 20:
        return fallback
    completed.sort()
    return min(48, max(fallback if minimum is None else minimum, completed[math.ceil(len(completed) * 0.9) - 1] + 2))


async def review_required(account_id: int, group_id: int, scope: str, *, phase: str = 'full',
                          fallback: int = DEFAULT_REVIEW_READS, cache: Any = None,
                          planned_reads: int | None = None) -> int:
    fallback = min(48, max(1, planned_reads)) if planned_reads is not None else fallback
    try:
        if cache is None:
            from app.core.redis import get_redis
            cache = await get_redis()
        samples = await cache.lrange(sample_key(account_id, 'review'), 0, 199)
        return review_estimate(samples, account_id, group_id, scope, phase=phase, fallback=fallback,
                               planned_reads=planned_reads)
    except Exception:
        return fallback


def review_estimate(samples: list[str], account_id: int, group_id: int, scope: str, *,
                    phase: str = 'full', fallback: int = DEFAULT_REVIEW_READS,
                    planned_reads: int | None = None) -> int:
    """Use the same membership cost for forecasts, preflight and atomic claims."""
    minimum = min(48, max(1, planned_reads)) if planned_reads is not None else (12 if phase == 'exit' else 16)
    if planned_reads is not None:
        fallback = minimum
    required = estimate(samples, fallback, time.time(), minimum=minimum, phase=phase)
    return resumed_estimate(samples, ReadWork(account_id, group_id, 'review', scope), required)


def resumed_estimate(samples: list[str], work: ReadWork, default: int) -> int:
    """Grow only this membership's slice when its last pass actually hit the cap."""
    for raw in samples:
        try:
            item = json.loads(raw)
            if item.get("group_id") != work.group_id or item.get("scope", "") != work.scope:
                continue
            if item.get("defer_reason") == "telegram_read_slice" and item.get("reads", 0) > 0:
                cap, step = (24, 4) if work.kind == "join" else (48, 8)
                return max(default, min(cap, int(item["reserved_reads"]) + step))
            break
        except (ValueError, TypeError, KeyError):
            continue
    return default


def minimum_wait(budget: dict, purpose: str, required: int) -> int:
    from app.core.config import settings
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        return 0
    from app.core.account.critical_fairness import other_hold, workload
    from app.core.account.reserved_reads import (
        ad_send_hold,
        allocations,
        available,
        lending_caps,
        transferred_caps,
    )

    waits = []
    usage, limits = budget["usage"], budget["limits"]
    if usage["minute"]["remaining"] < required:
        waits.append(max(1, usage["minute"]["ttl_seconds"]))
    for window, duration in (("hour", 3600), ("day", 86400)):
        base = allocations(limits[window])
        used = [
            usage[f"{lane}_{window}"]["used"]
            for lane in ("ad", "survival", "critical", "sync", "routine")
        ]
        caps = transferred_caps(
            base,
            usage[f"survival_lent_{window}"]["used"],
            usage.get(f"sync_lent_{window}", {}).get("used", 0),
            usage.get(f"ad_lent_{window}", {}).get("used", 0),
            usage.get(f"sync_borrowed_ad_{window}", {}).get("used", 0),
            usage.get(f"sync_borrowed_survival_{window}", {}).get("used", 0),
        )
        lane = 'ad' if purpose == 'ad_qualification_refresh' else 'critical'
        listener_floor = (usage.get(f"listener_{window}", {}).get("remaining", 0)
                          if limits.get("growth_efficiency_v1") else 0)
        caps = lending_caps(
            caps,
            used,
            lane,
            limits.get(f"survival_hold_{window}", -1),
            limits.get(f"sync_hold_{window}", -1),
            limits.get(f"ad_demand_hold_{window}", -1),
            survival_floor=listener_floor,
        )
        hold = other_hold(
            base[2],
            used[2],
            usage[f"critical_join_{window}"]["used"],
            usage[f"critical_review_{window}"]["used"],
            workload(purpose),
            limits.get(f'join_idle_hold_{window}', -1),
            efficiency=bool(limits.get("growth_efficiency_v1")),
        )
        if lane == 'ad':
            hold = limits.get(f'ad_refresh_hold_{window}', ad_send_hold(base[0], base[4]))
        if available(limits[window], used, caps, lane, survival_floor=listener_floor) - hold < required:
            ttl = usage[window]["ttl_seconds"]
            waits.append(max(1, ttl) if ttl > 0 else duration)
    return max(waits, default=60)


async def initial_pending(db: Any, account_id: int, group_id: int) -> tuple[int, datetime] | None:
    """Report initial work only when account-local execution capacity is full."""
    from app.modules.acquisition.qualification_engine import enabled_for
    if enabled_for(account_id):
        # New work owns explicit first-collection slots. Historical verdicts
        # and future evidence waits cannot veto unrelated joins.
        return None
    from app.modules.acquisition.execution_admission import execution_plan

    if (await execution_plan(db, datetime.utcnow(), account_id=account_id))["remaining"] > 0:
        return None
    from sqlalchemy import func, select

    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.models import GroupQualificationAudit

    latest = (
        select(func.max(GroupQualificationAudit.id))
        .where(
            GroupQualificationAudit.membership_id == GroupAccountMembership.id,
            GroupQualificationAudit.membership_joined_at == GroupAccountMembership.joined_at,
            GroupQualificationAudit.state != "cancelled",
        )
        .correlate(GroupAccountMembership)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(GroupQualificationAudit, GroupAccountMembership)
            .join(
                GroupAccountMembership,
                GroupAccountMembership.id == GroupQualificationAudit.membership_id,
            )
            .where(
                GroupQualificationAudit.account_id == account_id,
                GroupQualificationAudit.group_id != group_id,
                GroupQualificationAudit.batch_id.like("join-attempt:%"),
                GroupQualificationAudit.id == latest,
                GroupQualificationAudit.decision.in_(["unknown", "technical_wait"]),
                GroupQualificationAudit.state != "cancelled",
                GroupAccountMembership.status == "joined",
                GroupAccountMembership.review_status.notin_(
                    ["exit_pending", "leave_failed", "manual_required", "owned_group_excluded"]
                ),
            )
            .order_by(GroupQualificationAudit.id.desc())
            .limit(1)
        )
    ).all()
    seen = set()
    for row, member in rows:
        if member.id in seen:
            continue
        seen.add(member.id)
        if row.membership_joined_at == member.joined_at and row.decision in {
            "unknown",
            "technical_wait",
        }:
            # Completion can arrive before a forecast budget deadline. Recheck
            # locally without freezing the join schedule for hours.
            return member.group_id, datetime.utcnow() + timedelta(seconds=60)
    return None


async def ensure(db: Any, account_id: int, limits: dict, cache: Any) -> None:
    from app.core.config import settings
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        return
    """Called by the real RPC governor; fake/offline collectors spend no budget."""
    work = current(account_id)
    if work is None:
        return
    from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
    from app.core.account.rpc_governor import RpcDeferred
    from app.modules.acquisition.qualification_engine import enabled_for

    efficiency = enabled_for(account_id)
    initial_join_review = efficiency and work.kind == "review" and work.budget_workload == "join"
    if initial_join_review:
        from app.modules.acquisition.qualification_engine import (
            initial_join_budget_current,
            initial_join_claim_current,
        )

        try:
            valid = await initial_join_budget_current(
                db, work.budget_claim, account_id, work.group_id, work_token=work.token,
            )
        except Exception as exc:
            raise RpcDeferred("telegram_rpc_guard_unavailable", 60) from exc
        if not valid:
            if work.claimed:
                raise RpcDeferred("qualification_work_claim_changed", 60)
            try:
                claim_current = await initial_join_claim_current(
                    db, work.budget_claim, account_id, work.group_id,
                )
            except Exception as exc:
                raise RpcDeferred("telegram_rpc_guard_unavailable", 60) from exc
            if not claim_current:
                raise RpcDeferred("qualification_work_claim_changed", 60)
            # A prior nonzero/unknown slice consumed the protected first step.
            # The still-current work may continue under the ordinary review
            # budget, without inheriting the old token's join entitlement.
            work.budget_workload, work.budget_claim = None, None
            initial_join_review = False
    if work.claimed:
        return

    efficiency_join = work.kind == "join" and efficiency
    join_confirmation = work.kind == "join_reconciliation" and efficiency
    next_join_plan = None
    if efficiency_join:
        from app.modules.acquisition.review_read_plan import for_join
        from app.modules.acquisition.growth_admission import admission_plan

        next_join_plan = await for_join(db, account_id, work.group_id, datetime.utcnow())
        admission = await admission_plan(db, account_id, datetime.utcnow(), group_id=work.group_id,
                                         read_plan=next_join_plan)
        if admission["remaining"] <= 0:
            raise RpcDeferred(admission["reason"], admission.get("retry_after_seconds") or 60)

    if work.kind == "join" and not efficiency_join:
        from sqlalchemy import select

        from app.core.account.models import AccountOperationConfig
        config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
        if config is not None and config.dynamic_capacity_enabled:
            from app.modules.acquisition.growth_admission import admission_plan
            from app.modules.acquisition.join_budget import is_hard_join_admission_blocker

            admission = await admission_plan(db, account_id, datetime.utcnow())
            # Execution capacity is a scheduling signal.  It must remain
            # visible in admission telemetry, but it cannot veto the join's
            # own bounded read reservation: the durable review watermark and
            # the RPC budget below are the safety gates.  Keeping this check
            # aligned with JoinRequestBudgetService avoids reintroducing the
            # old hard gate at the Telegram preflight layer.
            if is_hard_join_admission_blocker(admission['reason']):
                raise RpcDeferred(admission['reason'], admission.get('retry_after_seconds', 60))
    try:
        bounded_join_work = initial_join_review or join_confirmation
        samples = [] if efficiency_join or bounded_join_work else await cache.lrange(sample_key(account_id, work.kind), 0, 199)
        if efficiency_join:
            # Reserve just this target's next bounded dependency. Initial
            # qualification later owns its own plan and checkpoint, rather
            # than a 24-read handoff coupled to this join reservation.
            work.limit = next_join_plan.required_reads
            work.planned_reads = work.limit
            work.phase = next_join_plan.operation
            work.review_reads = 0
        elif bounded_join_work:
            # The current new membership / original join request owns this
            # bounded step. Historical review samples cannot inflate it.
            if type(work.planned_reads) is not int or not 1 <= work.planned_reads <= 8:
                raise RpcDeferred("telegram_rpc_guard_unavailable", 60)
            work.limit = work.planned_reads
            work.review_reads = 0
        else:
            work.limit = estimate(
                samples,
                {"join": DEFAULT_JOIN_READS, "join_reconciliation": 8, "survival": 8, "ad_reconciliation": 8}.get(work.kind, DEFAULT_REVIEW_READS),
                time.time(),
            )
            if work.kind == "join":
                work.limit = min(24, work.limit)
            work.limit = resumed_estimate(samples, work, work.limit)
        if work.kind == 'review' and not initial_join_review:
            work.limit = await review_required(account_id, work.group_id, work.scope, phase=work.phase,
                                               fallback=work.requested_reads, cache=cache,
                                               planned_reads=work.planned_reads)
        if work.kind == 'renewal':
            work.limit = (estimate(samples, min(48, max(1, work.planned_reads)), time.time(),
                                   minimum=min(48, max(1, work.planned_reads)), phase=work.phase)
                          if work.planned_reads is not None else DEFAULT_RENEWAL_READS)
        if work.kind == "join" and not efficiency_join:
            review_samples = await cache.lrange(sample_key(account_id, "review"), 0, 199)
            work.review_reads = estimate(review_samples, DEFAULT_REVIEW_READS, time.time())
        reserve = work.limit + (work.review_reads if work.kind == "join" else 0)
        keys, args = reservation_args(
            account_id,
            limits,
            lane_for(work),
            reserve,
            0,
            workload=budget_kind(work) if efficiency else ("join" if work.kind in {"join", "join_reconciliation"} else "review"),
            work_token=work.token,
            work_mode="reserve",
            work_group=work.group_id,
            work_scope=work.scope,
            work_kind=work.kind,
            ad_refresh=work.kind == 'renewal',
        )
        delay = int(await cache.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args))
        if delay:
            raise RpcDeferred("telegram_read_budget", max(1, math.ceil(delay / 1000)))
        remaining = int(await cache.hget(key(account_id), "remaining") or 0)
        if work.kind == "review":
            work.limit = min(work.limit, remaining)
        from app.core.account.budget_ledger import reserve_read_work
        try:
            await reserve_read_work(db, cache, work, remaining)
            if initial_join_review:
                from app.modules.acquisition.qualification_engine import bind_initial_join_read_work

                if not await bind_initial_join_read_work(
                    db, work.budget_claim, account_id, work.group_id, work.token, work.limit,
                ):
                    raise RpcDeferred("telegram_rpc_guard_unavailable", 60)
        except BaseException:
            work.budget_bind_failed = True
            # Includes cancellation after Redis succeeded and PG failed. Only
            # this generation's reservation is released; spent quotas stay.
            await cache.eval(
                "if redis.call('HGET',KEYS[1],'token')==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                1, key(account_id), work.token,
            )
            raise
        work.claimed = True
        from app.modules.acquisition import qualification_sla_runtime
        qualification_sla_runtime.budget_step_entered(account_id, work)
    except RpcDeferred:
        raise
    except Exception as exc:
        raise RpcDeferred("telegram_rpc_guard_unavailable", 60) from exc


async def handoff(work: ReadWork, scope: str) -> None:
    """Retain only the first-review share after a confirmed, committed join."""
    if not work.claimed or work.review_reads <= 0:
        return
    from app.core.redis import get_redis

    cache = await get_redis()
    saved = await cache.eval(
        """
        if redis.call('HGET',KEYS[1],'token')~=ARGV[1] then return 0 end
        local remaining=tonumber(redis.call('HGET',KEYS[1],'remaining') or '0')
        remaining=math.min(remaining,tonumber(ARGV[2]))
        for w=1,2 do
          local survival=math.min(remaining,tonumber(redis.call('HGET',KEYS[1],'survival_'..w) or '0'))
          local sync=math.min(remaining-survival,tonumber(redis.call('HGET',KEYS[1],'sync_'..w) or '0'))
          local ad=math.min(remaining-survival-sync,tonumber(redis.call('HGET',KEYS[1],'ad_'..w) or '0'))
          redis.call('HSET',KEYS[1],'survival_'..w,survival,'sync_'..w,sync,'ad_'..w,ad)
        end
        redis.call('HSET',KEYS[1],'remaining',remaining,
          'kind','review','scope',ARGV[3],'token','pending')
        return 1
    """,
        1,
        key(work.account_id),
        work.token,
        work.review_reads,
        scope,
    )
    work.handed_off = bool(saved)


async def release(work: ReadWork) -> None:
    if not work.claimed or work.handed_off:
        return
    from app.core.redis import get_redis

    try:
        async with asyncio.timeout(2):
            cache = await get_redis()
            released = await cache.eval(
                "if redis.call('HGET',KEYS[1],'token')==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                1,
                key(work.account_id),
                work.token,
            )
            work.claimed = False
            if released:
                from app.core.scheduler.growth_dispatch import wake_account
                await wake_account(work.account_id, stages=("ads", "join", "review"),
                                   reason="budget_settled", client=cache)
    except Exception:
        pass  # Original lease expiry remains the fallback.


async def reservation_snapshot(cache: Any, account_id: int) -> dict:
    """Operational visibility, never an authorization to skip the atomic claim."""
    try:
        item = await cache.hgetall(key(account_id))
        ttl = await cache.ttl(key(account_id))
        if not item or ttl <= 0:
            return {}
        return {
            "group_id": int(item["group"]),
            "scope": item.get("scope", ""),
            "kind": item["kind"],
            "remaining": int(item["remaining"]),
            "pending": item["token"] == "pending",
            "ttl_seconds": ttl,
        }
    except Exception:
        return {}


@asynccontextmanager
async def operation(account_id: int, group_id: int, kind: str, *, scope: str = "") -> AsyncIterator[ReadWork]:
    work = ReadWork(account_id, group_id, kind, scope)
    from app.modules.acquisition import qualification_sla_runtime
    qualification_sla_runtime.slice_started(work)
    token = _current.set(work)
    failed_work = False
    try:
        yield work
    except BaseException:
        failed_work = True
        raise
    finally:
        work.closed = True
        _current.reset(token)
        from app.core.account.budget_ledger import finish_read_work
        await finish_read_work(work)
        if work.kind == "review" and work.budget_workload == "join" and work.budget_claim is not None:
            from app.modules.acquisition.qualification_engine import finish_initial_join_read_work

            await finish_initial_join_read_work(work, failed=failed_work)
        qualification_sla_runtime.slice_finished(work, failed=failed_work)
        from app.core.account.read_metrics import record_work
        await record_work(work, failed=failed_work)
        if work.claimed or work.reads:
            try:
                from app.core.redis import get_redis

                async with asyncio.timeout(2):
                    cache = await get_redis()
                    released = False
                    if work.claimed and not work.handed_off:
                        released = await cache.eval(
                            "if redis.call('HGET',KEYS[1],'token')==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                            1,
                            key(account_id),
                            work.token,
                        )
                    if work.reads:
                        row = {
                            **work.summary(),
                            "at": time.time(),
                            "group_id": group_id,
                            "scope": work.scope,
                        }
                        async with cache.pipeline(transaction=True) as pipe:
                            pipe.lpush(sample_key(account_id, work.kind), json.dumps(row))
                            pipe.ltrim(sample_key(account_id, work.kind), 0, 199)
                            pipe.expire(sample_key(account_id, work.kind), RETENTION_SECONDS)
                            await pipe.execute()
                    if released:
                        from app.core.scheduler.growth_dispatch import wake_account
                        await wake_account(account_id, stages=("ads", "join", "review"),
                                           reason="budget_settled", client=cache)
            except Exception:
                # A crashed/failed cleanup expires; never reset spent counters.
                pass
