"""New joins must fit both outstanding reviews and one follow-up review."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select


async def review_debt(db: Any, account_id: int, now: datetime, end: datetime) -> tuple[int, int]:
    from app.core.account.models import TelegramAccount
    from app.core.account.read_work import review_estimate, sample_key
    from app.core.group.models import GroupAccountMembership as Member
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.adaptive_frequency import payload
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_events import account_key, key, pending_value
    from app.modules.acquisition.review_cost import review_shape
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    latest = select(func.max(Audit.id)).where(
        Audit.membership_id == Member.id, Audit.membership_joined_at == Member.joined_at,
        Audit.state != 'cancelled',
    ).correlate(Member).scalar_subquery()
    rows = (await db.execute(select(Member, Audit).select_from(Member).outerjoin(Audit, Audit.id == latest).where(
        Member.account_id == account_id, Member.status == 'joined', Member.left_at.is_(None),
        Member.review_status.notin_(['manual_required', 'owned_group_excluded', 'exit_pending', 'leave_failed']),
    ).limit(301))).all()
    if len(rows) > 300:
        raise ValueError('review_inventory_incomplete')
    if not rows:
        return 0, 0
    account = await db.get(TelegramAccount, account_id)
    markers = dict((await db.execute(select(SystemSetting.key, SystemSetting.value).where(
        SystemSetting.key.in_([account_key(account_id), *(key(member.id) for member, _ in rows)]),
    ))).all())
    try:
        from app.core.redis import get_redis
        cache = await get_redis()
        samples = await cache.lrange(sample_key(account_id, 'review'), 0, 199)
    except Exception:
        samples = []
    reads = count = 0
    for member, audit in rows:
        if audit is not None:
            if audit.state in {'manual_required', 'waiting_membership'}:
                continue
            if audit.state == 'completed' and audit.next_retry_at is None:
                continue
            if audit.decision in {'allowed', 'trial', 'protected'} or waiting_for_evidence(audit, now):
                continue
            if audit.next_retry_at and audit.next_retry_at > end and audit.reason != 'telegram_read_budget':
                continue
        prior = payload(audit.evidence_json) if audit else {}
        prior = prior.get('pending_collection') or prior
        change = pending_value(markers.get(key(member.id)), member.joined_at, markers.get(account_key(account_id)))
        phase, fallback = review_shape(prior, audit, member, account, change, now)
        last = prior.get('read_cost') or {}
        # The durable row also carries unfinished work if the rolling sample
        # list has rotated. A zero-read admission failure is not new evidence.
        if last.get('defer_reason') == 'telegram_read_slice' and last.get('reads', 0) > 0:
            fallback = max(fallback, min(48, int(last.get('reserved_reads') or 24) + 8))
        scope = f"{member.id}:{member.joined_at.isoformat()}" if member.joined_at else ''
        cost = review_estimate(samples, account_id, member.group_id, scope, phase=phase, fallback=fallback)
        # Unfinished initial qualification can produce a later observation.
        # Keep that cost until its real verdict identifies the next work.
        if member.review_status == 'initial_pending':
            cost += 24
        reads += cost
        count += 1
    return reads, count


async def admission_plan(db: Any, account_id: int, now: datetime, rpc: dict | None = None,
                         *, group_id: int | None = None, candidate: Any = None,
                         read_plan: Any = None) -> dict:
    from app.core.config import settings
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        from app.core.account.rpc_governor import snapshot
        state = rpc if rpc is not None else await snapshot(db, account_id, now)
        blocked = state.get("state") in {"cooldown", "unavailable"}
        return {"remaining": 0 if blocked else 1, "reason": state.get("reason") if blocked else None,
                "retry_after_seconds": state.get("retry_after_seconds", 0), "budget_enabled": False,
                "review_reads_due": None, "pending_reviews": None, "available_reads": None,
                "new_group_read_cost": None, "remaining_read_cost": None, "window_end": None,
                "execution": {"scope": "rpc_pacing", "remaining": 0 if blocked else 1}}
    from app.modules.acquisition.qualification_engine import enabled_for
    if enabled_for(account_id):
        return await _next_step_admission(db, account_id, now, rpc, group_id=group_id,
                                          candidate=candidate, read_plan=read_plan)
    from app.core.account.read_work import current
    from app.core.account.reserved_reads import (
        allocations,
        available,
        lending_caps,
        transferred_caps,
    )
    from app.core.account.rpc_governor import snapshot

    rpc = rpc if rpc is not None else await snapshot(db, account_id, now)
    if rpc.get('state') in {'cooldown', 'unavailable', 'budget_wait'}:
        return {'remaining': 0, 'reason': rpc.get('reason') or 'telegram_read_budget'}
    limits, usage = rpc['limits'], rpc['usage']
    ttl = usage['day']['ttl_seconds']
    if ttl == -1:
        return {'remaining': 0, 'reason': 'telegram_rpc_guard_unavailable'}
    end = now + timedelta(seconds=max(1, ttl) if ttl >= 0 else 86400)
    debt, pending = await review_debt(db, account_id, now, end)
    caps = transferred_caps(allocations(limits['day']), usage['survival_lent_day']['used'],
                            usage.get('sync_lent_day', {}).get('used', 0), usage.get('ad_lent_day', {}).get('used', 0))
    used = [usage[lane + '_day']['used'] for lane in ('ad', 'survival', 'critical', 'sync', 'routine')]
    caps = lending_caps(caps, used, 'critical', limits.get('survival_hold_day', -1),
                        limits.get('sync_hold_day', -1), limits.get('ad_demand_hold_day', -1))
    room = available(limits['day'], used, caps, 'critical')
    # 12 join reads + 24 first-review reads remain atomically reserved together.
    # Another 24 follow-up reads stay in the forecast, not charged as fake RPCs.
    lifecycle = 12 + 24 + 24
    work = current(account_id)
    outstanding = lifecycle
    if work is not None and work.kind == 'join' and work.claimed:
        # Final admission runs after connection/preview reads. Those spent
        # reads stay charged; require only the remainder of this same job.
        outstanding = max(0, work.limit - work.reads) + work.review_reads + 24
    from app.modules.acquisition.execution_admission import execution_plan
    # Review pressure is an account-local admission input.  The worker's
    # process-wide concurrency remains enforced when work is claimed; an old
    # backlog on account 2 must not set accounts 33/34/35 to zero.
    execution = await execution_plan(db, now, account_id=account_id)
    read_slots = max(0, room - debt) // outstanding
    slots = min(read_slots, execution["remaining"])
    reason = None if slots else (
        ('join_wait_inventory_review' if debt else 'telegram_read_budget')
        if not read_slots else execution['reason']
    )
    return {'remaining': slots, 'reason': reason,
            'retry_after_seconds': max(60, ttl) if reason == 'telegram_read_budget' else 60,
            'execution': execution,
            'review_reads_due': debt, 'pending_reviews': pending, 'available_reads': room,
            'new_group_read_cost': lifecycle, 'remaining_read_cost': outstanding, 'window_end': end.isoformat()}


async def _next_step_admission(db: Any, account_id: int, now: datetime, rpc: dict | None,
                                *, group_id: int | None, candidate: Any, read_plan: Any) -> dict:
    """Admit one target's next dependency, independent of historical debt.

    This is a forecast only. The account/target lock and existing Redis Lua
    retain the final authority over concurrent joins and actual spending.
    """
    from app.modules.acquisition.qualification_engine import initial_slots

    pending = await initial_slots(db, account_id)
    slots = max(0, 2 - pending)
    result = {"remaining": slots, "reason": None if slots else "join_initial_collection_capacity",
              "retry_after_seconds": 0 if slots else 60,
              "execution": {"remaining": slots, "pending": pending, "concurrency": 2,
                            "scope": "efficiency_first_collection", "account_id": account_id},
              "review_reads_due": 0, "pending_reviews": pending,
              "available_reads": None, "new_group_read_cost": 0,
              "remaining_read_cost": 0, "window_end": None,
              "engine_version": "efficiency_v1"}
    # Discovery has no chosen target: do not read Telegram or reserve a
    # fictitious lifecycle cost before a plan can name its missing facts.
    if not slots or group_id is None:
        return result
    from app.modules.acquisition.review_read_plan import for_join
    from app.core.account.critical_fairness import purpose_budget
    from app.core.account.read_work import minimum_wait
    from app.core.account.rpc_governor import snapshot

    plan = read_plan if read_plan is not None else await for_join(db, account_id, group_id, now, candidate=candidate)
    result.update(new_group_read_cost=plan.required_reads,
                  remaining_read_cost=plan.required_reads, plan_scope=plan.scope,
                  plan_version=plan.version, missing_dependencies=list(plan.missing_dependencies))
    if plan.ready_at is not None and plan.ready_at > now:
        return {**result, "remaining": 0, "reason": plan.reason or "join_dependency_wait",
                "retry_after_seconds": max(1, int((plan.ready_at-now).total_seconds())),
                "ready_at": plan.ready_at.isoformat()}
    budget = rpc if rpc is not None else await snapshot(db, account_id, now)
    if budget.get("state") in {"cooldown", "unavailable", "budget_wait"}:
        return {**result, "remaining": 0, "reason": budget.get("reason") or "telegram_read_budget",
                "retry_after_seconds": max(1, int(budget.get("retry_after_seconds") or 60))}
    purpose = purpose_budget(budget, "auto_join")
    room = max(0, int(purpose.get("remaining", 0)))
    result["available_reads"] = room
    if purpose.get("retry_after_seconds", 0) or room < plan.required_reads:
        delay = max(int(purpose.get("retry_after_seconds") or 0),
                    minimum_wait(budget, "auto_join", plan.required_reads) if room < plan.required_reads else 0)
        return {**result, "remaining": 0, "reason": "telegram_read_budget",
                "retry_after_seconds": max(1, delay)}
    return result
