"""Forecast already admitted advertising before lending its idle read share."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select


def projected_hold(sends: int, renewals: int, delivery_cost: int, renewal_cost: int) -> int:
    work = sends * delivery_cost + renewals * renewal_cost
    # Two deliveries and a newly changed qualification can arrive between
    # forecasts. Loans also retain a 20% retry allowance for scheduled work.
    return work + max(2 * delivery_cost + renewal_cost, math.ceil(work / 5))


def add_refresh_demand_headroom(limits: dict, budget: dict, demand: dict) -> None:
    """Let blocked grants spend their own repair capacity, retaining due sends.

    A fixed percentage of the original window is not remaining delivery work.
    A complete forecast protects available and future sends together. One ready
    target must not prevent repairs for the other scheduled targets.
    """
    from app.core.account.read_work import DEFAULT_RENEWAL_READS
    from app.core.account.reserved_reads import (
        ad_send_hold,
        allocations,
        available,
        transferred_caps,
    )

    renewals = max(demand[window]['renewals'] for window in ('hour', 'day'))
    if not renewals and not budget.get('ad_dependency_recovery', {}).get('blocked_grants'):
        return
    rooms, waits, holds = [budget['usage']['minute']['remaining']], [budget['retry_after_seconds']], {}
    constrained = {}
    for window, duration in (('hour', 3600), ('day', 86400)):
        usage = budget['usage']
        base = allocations(limits[window])
        caps = transferred_caps(base, usage['survival_lent_' + window]['used'],
                                usage.get('sync_lent_' + window, {}).get('used', 0),
                                usage.get('ad_lent_' + window, {}).get('used', 0))
        used = [usage[lane + '_' + window]['used'] for lane in ('ad', 'survival', 'critical', 'sync', 'routine')]
        delivery_work = demand[window]['sends'] * demand['delivery_cost']
        # Keep at least two extra deliveries, or a 20% buffer when larger.
        # The renewal's full reservation is held separately by the atomic owner.
        forecast_hold = delivery_work + max(2 * demand['delivery_cost'], math.ceil(delivery_work / 5))
        old_hold = limits.get('ad_refresh_hold_' + window, ad_send_hold(base[0], base[4]))
        holds[window] = min(old_hold, forecast_hold)
        remaining = available(limits[window], used, caps, 'ad')
        if demand[window]['renewals'] or budget.get('ad_dependency_recovery', {}).get('blocked_grants'):
            # An unfunded future delivery plan must not consume the reads
            # needed to make its own targets deliverable. Admit one complete
            # renewal while retaining two sends and 20% of real headroom.
            renewal_reads = max(DEFAULT_RENEWAL_READS, demand['renewal_cost'])
            send_floor = max(2 * demand['delivery_cost'], math.ceil(remaining / 5))
            feasible_hold = max(send_floor, remaining - renewal_reads)
            if holds[window] > feasible_hold:
                holds[window] = feasible_hold
                constrained[window] = {'available_reads': remaining, 'renewal_reads': renewal_reads,
                                       'forecast_delivery_hold': forecast_hold, 'send_floor': send_floor}
        limits['ad_refresh_hold_' + window] = holds[window]
        room = max(0, remaining - holds[window])
        rooms.append(room)
        if not room:
            ttl = usage[window]['ttl_seconds']
            waits.append(max(1, ttl) if ttl >= 0 else duration)
    recovery = budget.setdefault('ad_dependency_recovery', {})
    recovery.update(delivery_holds=holds, forecast_renewals=renewals, reserve_policy='remaining_deliveries')
    if constrained:
        recovery['unfunded_delivery_plan'] = constrained
    budget['lanes']['ad_refresh'] = {'remaining': min(rooms), 'retry_after_seconds': max(waits)}


async def ad_demand(db: Any, account_id: int, now: datetime, budget: dict) -> dict:
    from app.core.account.models import AccountOperationConfig, TelegramAccount
    from app.core.account.outbound_budget import effective_capacity_limits
    from app.core.account.read_work import DEFAULT_RENEWAL_READS
    from app.core.group.models import GroupAccountMembership as Member
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.ad_output_plan import mixed_resource_capacity
    from app.modules.acquisition.adaptive_frequency import canonical, payload, rule_quota
    from app.modules.acquisition.models import AdDeliveryScheduleState as Schedule
    from app.modules.acquisition.models import GroupAdFrequency
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_events import account_key, key, pending_value
    from app.modules.acquisition.qualification_lifetime import authorization_current
    from app.modules.acquisition.read_costs import operation_costs

    config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
    if not (config and config.enabled and config.dynamic_capacity_enabled
            and config.auto_ads_enabled and config.adaptive_ads_enabled):
        return {}
    account = await db.get(TelegramAccount, account_id)
    if account is None:
        return {}
    latest = select(func.max(Audit.id)).where(
        Audit.membership_id == Member.id, Audit.membership_joined_at == Member.joined_at,
        Audit.state != 'cancelled',
    ).correlate(Member).scalar_subquery()
    rows = (await db.execute(select(Member, Audit).select_from(Member).outerjoin(Audit, Audit.id == latest).where(
        Member.account_id == account_id, Member.status == 'joined', Member.left_at.is_(None),
        Member.review_status == 'approved', Member.ad_status == 'active',
    ).limit(301))).all()
    if len(rows) > 300 or any(audit is None or audit.decision not in {'allowed', 'trial'} for _, audit in rows):
        return {}
    markers = dict((await db.execute(select(SystemSetting.key, SystemSetting.value).where(
        SystemSetting.key.in_([account_key(account_id), *(key(member.id) for member, _ in rows)]),
    ))).all())
    peers = {canonical(member.telegram_group_id, payload(audit.evidence_json)) for member, audit in rows}
    if None in peers:
        return {}
    frequencies = {row.telegram_group_id: row for row in (await db.scalars(select(GroupAdFrequency).where(
        GroupAdFrequency.telegram_group_id.in_(peers),
    ))).all()} if peers else {}
    schedules = (await db.execute(select(Schedule.group_id, Schedule.next_due_at, Schedule.status,
        Schedule.lease_expires_at).where(Schedule.account_id == account_id).limit(3001))).all()
    if len(schedules) > 3000 or any(s.status == 'sending' and s.lease_expires_at and s.lease_expires_at > now for s in schedules):
        return {}
    by_group: dict[int, list] = {}
    for schedule in schedules:
        by_group.setdefault(schedule.group_id, []).append(schedule)
    capacity = await effective_capacity_limits(db, account, config, now)
    costs = await operation_costs(account_id)
    costs = {'survival_read_cost': 10, 'daily_review_read_cost': 10, **costs}
    delivery = max(1, costs['delivery_read_cost'])
    renewal = max(1, costs.get("renewal_read_cost", DEFAULT_RENEWAL_READS))
    # Match the sending planner's sustainable pace, including groups whose
    # existing grants need repair. Configured outbound ceilings are not plans.
    inventory = {'mature_usable': 0, 'mature_slots_24h': 0, 'probe_slots_24h': 0}
    for member, audit in rows:
        state = frequencies.get(canonical(member.telegram_group_id, payload(audit.evidence_json)))
        quota = min(state.quota if state else 1, rule_quota(payload(audit.evidence_json)))
        if state and state.mature:
            inventory['mature_usable'] += 1
            inventory['mature_slots_24h'] += quota
        else:
            inventory['probe_slots_24h'] += quota
    read_limits = {name: item['limit'] for name, item in budget['usage'].items() if 'limit' in item}
    planned = mixed_resource_capacity(capacity['ad'], read_limits, inventory, costs) if read_limits else capacity['ad']
    interval = max(1, capacity.get('ad_interval_seconds', 600), math.ceil(86400 / max(1, planned)))
    result = {}
    for window, duration in (('hour', 3600), ('day', 86400)):
        ttl = budget['usage'][window]['ttl_seconds']
        if ttl == -1:
            return {}
        horizon = min(duration, max(1, ttl)) if ttl >= 0 else duration
        end = now + timedelta(seconds=horizon)
        sends = repairs = 0
        for member, audit in rows:
            peer = canonical(member.telegram_group_id, payload(audit.evidence_json))
            state = frequencies.get(peer)
            if state and state.status != 'active':
                continue
            group_schedules = by_group.get(member.group_id, [])
            dates = [s.next_due_at for s in group_schedules if s.status != 'paused']
            if group_schedules and not dates:
                continue
            due = max(now, min(dates, default=now), member.ad_pause_until or now,
                      state.pause_until if state and state.pause_until else now)
            if due > end:
                continue
            quota = min(state.quota if state else 1, rule_quota(payload(audit.evidence_json)))
            group_interval = max(interval, math.ceil(86400 / max(1, quota)))
            sends += 1 + int((end - due).total_seconds() // group_interval)
            repairs += int(not authorization_current(audit, now) or bool(pending_value(
                markers.get(key(member.id)), member.joined_at, markers.get(account_key(account_id)))))
        sends = min(sends, 1 + horizon // interval, max(0, planned))
        repairs = min(repairs, sends)
        result[window] = {'sends': sends, 'renewals': repairs, 'horizon_seconds': horizon,
                          'hold': projected_hold(sends, repairs, delivery, renewal)}
    return {**result, 'delivery_cost': delivery, 'renewal_cost': renewal}


async def add_ad_demand_headroom(db: Any, account_id: int, now: datetime, limits: dict, budget: dict) -> None:
    from app.core.account.reserved_reads import (
        allocations,
        available,
        lending_caps,
        transferred_caps,
    )

    demand = await ad_demand(db, account_id, now, budget)
    if not demand:
        return
    add_refresh_demand_headroom(limits, budget, demand)
    rooms, waits, loans = [budget['usage']['minute']['remaining']], [budget['retry_after_seconds']], {}
    for window, duration in (('hour', 3600), ('day', 86400)):
        usage = budget['usage']
        limits['ad_demand_hold_' + window] = demand[window]['hold']
        caps = transferred_caps(allocations(limits[window]), usage['survival_lent_' + window]['used'],
                                usage.get('sync_lent_' + window, {}).get('used', 0),
                                usage.get('ad_lent_' + window, {}).get('used', 0))
        used = [usage[lane + '_' + window]['used'] for lane in ('ad', 'survival', 'critical', 'sync', 'routine')]
        adjusted = lending_caps(caps, used, 'critical', limits.get('survival_hold_' + window, -1),
                                limits.get('sync_hold_' + window, -1), demand[window]['hold'])
        room = available(limits[window], used, adjusted, 'critical')
        rooms.append(room)
        loans[window] = caps[0] - adjusted[0]
        if not room:
            ttl = usage[window]['ttl_seconds']
            waits.append(max(1, ttl) if ttl >= 0 else duration)
    budget['ad_demand_lending'] = {'forecast': demand, 'available': loans}
    budget['lanes']['critical'] = {'remaining': min(rooms), 'retry_after_seconds': max(waits)}
