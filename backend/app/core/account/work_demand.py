"""Let blocked business dependencies use idle reservations, with bounded holds."""

from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, or_, select


async def _join_demand_snapshot(db: Any, account_id: int, now: datetime) -> dict[str, Any]:
    """Return the cached join work that can justify an idle join hold.

    A preview-pending row is demand for a future read, but it is not an
    executable join.  Treating every row in the discovery queue as executable
    strands the review lane behind a permanent join floor.  This helper only
    reads the bounded local inventory; Telegram is never contacted here.
    """
    from telethon.tl.types import PeerChannel, PeerChat

    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition import candidate_inventory
    from app.modules.acquisition.models import AutoJoinAttempt
    from app.modules.acquisition.qualification_identity import peer_identity
    from app.modules.acquisition.qualification_join_gate import qualification_join_gate

    rows = list(
        (
            await db.scalars(
                select(Group)
                .where(
                    Group.status.in_(["pending_join", "join_failed", "cooling_down"]),
                    Group.username.is_not(None),
                )
                .order_by(Group.id)
                .limit(301)
            )
        ).all()
    )
    candidate_rows = rows[:300]
    candidate_ids = [group.id for group in candidate_rows]
    joined_ids = set(
        (
            await db.scalars(
                select(GroupAccountMembership.group_id)
                .where(
                    GroupAccountMembership.group_id.in_(candidate_ids),
                    GroupAccountMembership.status == "joined",
                )
                .distinct()
            )
        ).all()
    ) if candidate_ids else set()
    facts = await candidate_inventory.load(db, account_id, candidate_ids)
    retry_at = await candidate_inventory.account_retry_at(db, account_id)
    retry_wait = bool(retry_at and retry_at > now)

    counts = {
        "total": 0,
        "preview_pending": 0,
        "identity_pending": 0,
        "excluded": 0,
        "ready": 0,
    }
    executable = False
    for group in candidate_rows:
        if group.id in joined_ids:
            continue
        counts["total"] += 1
        fact = facts.get(group.id, {})
        if not candidate_inventory.fresh(fact, group, account_id, now):
            counts["preview_pending"] += 1
            if (peer_identity(group.group_id) or (None, None))[1] is None:
                counts["identity_pending"] += 1
            continue
        if fact.get("preview_exclusion"):
            counts["excluded"] += 1
            continue
        if not candidate_inventory.ready(fact):
            counts["preview_pending"] += 1
            if fact.get("namespace") not in {"chat", "channel"}:
                counts["identity_pending"] += 1
            continue
        identity = peer_identity(group.group_id, namespace=fact.get("namespace"))
        if identity is None or identity[1] is None:
            counts["identity_pending"] += 1
            counts["preview_pending"] += 1
            continue
        # Use the same local gate as capacity_snapshot().  Keep scanning the
        # bounded sample after finding a runnable candidate so the operational
        # counts describe the full sample instead of whichever row happened to
        # be first.
        entity = PeerChannel(identity[0]) if identity[1] == "channel" else PeerChat(identity[0])
        if await qualification_join_gate(db, entity):
            counts["excluded"] += 1
            continue
        counts["ready"] += 1
        executable = True

    # The snapshot is produced immediately before this helper by the governor.
    # It may represent an in-flight join even when the candidate inventory is
    # temporarily empty, so never release that reservation's budget.
    active_attempt = await db.scalar(
        select(func.count())
        .select_from(AutoJoinAttempt)
        .where(
            AutoJoinAttempt.account_id == account_id,
            AutoJoinAttempt.request_state == "reserved",
            AutoJoinAttempt.reservation_expires_at.is_not(None),
            AutoJoinAttempt.reservation_expires_at > now,
        )
    )
    active_request_reservation = bool(active_attempt)

    return {
        **counts,
        "sample_complete": len(rows) <= 300,
        "account_retry_at": retry_at.isoformat() if retry_at else None,
        "retry_wait": retry_wait,
        # account_retry_at only gates the next preview attempt.  A fresh,
        # ready candidate remains real join demand even while another preview
        # is waiting for its retry deadline.
        "executable": bool(executable),
        "active_request_reservation": active_request_reservation,
    }


async def add_work_demand(db: Any, account_id: int, now: datetime, limits: dict, budget: dict) -> None:
    from app.core.account.models import AccountOperationConfig
    from app.core.account.reserved_reads import allocations, available, transferred_caps
    from app.core.group.models import GroupAccountMembership as Member
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.models import AdDeliveryScheduleState as Schedule
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_events import account_key, key, pending_value
    from app.modules.acquisition.qualification_lifetime import authorization_current

    config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
    if config is None or not config.enabled:
        return
    if not config.auto_ads_enabled or not config.dynamic_capacity_enabled:
        return
    latest = select(func.max(Audit.id)).where(
        Audit.membership_id == Member.id, Audit.membership_joined_at == Member.joined_at,
        Audit.state != 'cancelled',
    ).correlate(Member).scalar_subquery()
    schedules = select(Schedule.id).where(
        Schedule.account_id == account_id, Schedule.group_id == Member.group_id,
    ).correlate(Member)
    due_schedule = schedules.where(
        Schedule.status != 'paused',
        or_(Schedule.next_due_at <= now, Schedule.last_reason == 'qualification_event_pending'),
        or_(Schedule.status != 'sending', Schedule.lease_expires_at.is_(None), Schedule.lease_expires_at <= now),
    )
    rows = (await db.execute(select(Member, Audit).select_from(Member).join(Audit, Audit.id == latest).where(
        Member.account_id == account_id, Member.status == 'joined', Member.left_at.is_(None),
        Member.review_status == 'approved', Member.ad_status == 'active',
        Audit.decision.in_(['allowed', 'trial']),
        or_(~exists(schedules), exists(due_schedule)),
    ).limit(301))).all()
    if not rows or len(rows) > 300:
        return
    markers = dict((await db.execute(select(SystemSetting.key, SystemSetting.value).where(
        SystemSetting.key.in_([account_key(account_id), *(key(member.id) for member, _ in rows)]),
    ))).all())
    blocked = 0
    for member, audit in rows:
        if not authorization_current(audit, now) or (member.ad_pause_until and member.ad_pause_until > now):
            continue
        if not pending_value(markers.get(key(member.id)), member.joined_at, markers.get(account_key(account_id))):
            return  # An available, due target keeps the ordinary refresh cap.
        blocked += 1
    if not blocked:
        return
    rooms, waits = [budget['usage']['minute']['remaining']], [budget['retry_after_seconds']]
    for window, duration in (('hour', 3600), ('day', 86400)):
        caps = allocations(limits[window])
        # A renewal is a send dependency. Still retain 20% of the ad base plus
        # all flex for delivery, and charge every actual repair read normally.
        hold = caps[4] + (caps[0] + 4) // 5
        limits['ad_refresh_hold_' + window] = hold
        used = [budget['usage'][lane + '_' + window]['used'] for lane in ('ad', 'survival', 'critical', 'sync', 'routine')]
        adjusted = transferred_caps(caps, budget['usage']['survival_lent_' + window]['used'],
                                    budget['usage'].get('sync_lent_' + window, {}).get('used', 0),
                                    budget['usage'].get('ad_lent_' + window, {}).get('used', 0))
        room = max(0, available(limits[window], used, adjusted, 'ad') - hold)
        rooms.append(room)
        if not room:
            ttl = budget['usage'][window]['ttl_seconds']
            waits.append(max(1, ttl) if ttl >= 0 else duration)
    budget['lanes']['ad_refresh'] = {'remaining': min(rooms), 'retry_after_seconds': max(waits)}
    budget['ad_dependency_recovery'] = {'blocked_grants': blocked, 'send_reserve_percent': 20}
