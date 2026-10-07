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
        Member.review_status.notin_(['manual_required', 'owned_group_excluded']),
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


async def admission_plan(db: Any, account_id: int, now: datetime, rpc: dict | None = None) -> dict:
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
    execution = await execution_plan(db, now)
    read_slots = max(0, room - debt) // outstanding
    slots = min(read_slots, execution["remaining"])
    return {'remaining': slots, 'reason': None if slots else ('join_wait_inventory_review' if not read_slots else execution['reason']),
            'execution': execution,
            'review_reads_due': debt, 'pending_reviews': pending, 'available_reads': room,
            'new_group_read_cost': lifecycle, 'remaining_read_cost': outstanding, 'window_end': end.isoformat()}
