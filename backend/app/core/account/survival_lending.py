"""Lend only idle survival capacity to critical reads within the same windows."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig
from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency


async def survival_holds(
    db: Any, account_id: int, now: datetime, limits: dict, budget: dict
) -> dict[str, int]:
    """Remaining reads to retain, including scheduled work and a 5% buffer.

    Active readers and actionable unknown outcomes keep the full reservation.
    A parked receipt without a message ID is reconciled locally without RPCs.
    No counters or expiries are reset; only the critical lane can borrow.
    """
    config = await db.scalar(
        select(AccountOperationConfig).where(
            AccountOperationConfig.account_id == account_id,
        )
    )
    if not (
        config
        and config.enabled
        and config.auto_ads_enabled
        and config.adaptive_ads_enabled
        and config.dynamic_capacity_enabled
    ):
        return {}
    pending = (
        await db.execute(
            select(
                AdDeliveryLog.id,
                AdDeliveryLog.status,
                AdDeliveryLog.created_at,
                AdDeliveryLog.telegram_message_id,
                AdDeliveryLog.survival_status,
                AdDeliveryLog.survival_stage,
                AdDeliveryLog.error,
                AdDeliveryLog.survival_error,
                AdDeliveryLog.survival_check_due_at,
                AdDeliveryLog.survival_claim_token,
                AdDeliveryLog.survival_claim_expires_at,
                AdDeliveryLog.qualification_context_json,
            )
            .where(
                AdDeliveryLog.account_id == account_id,
                (AdDeliveryLog.survival_status == "pending")
                | AdDeliveryLog.status.in_(
                    ["pending", "sending", "unknown", "reconciliation_required"]
                )
                | AdDeliveryLog.error.ilike("%send_outcome_unknown%"),
            )
            .limit(301)
        )
    ).all()
    if len(pending) > 300:
        return {}
    from app.core.account.send_receipts import parked_receipt

    parked = set()
    for row in pending:
        # The survival worker cannot query a receipt without a message ID.
        # Keep recent/in-flight sends, claimed readers and legacy checkpoints
        # fully protected. This does not resolve the receipt or release sends.
        if not parked_receipt(row, now):
            return {}
        parked.add(row.id)
    peers = list(
        (
            await db.scalars(
                select(GroupAccountMembership.telegram_group_id)
                .where(
                    GroupAccountMembership.account_id == account_id,
                    GroupAccountMembership.status == "joined",
                    GroupAccountMembership.left_at.is_(None),
                )
                .limit(301)
            )
        ).all()
    )
    if len(peers) > 300:
        return {}
    keys = set()
    for peer in peers:
        if peer is None:
            return {}
        keys.update([peer] if peer < 0 else [-peer, -(10**12 + peer)])
    states = (
        list(
            (
                await db.scalars(
                    select(GroupAdFrequency).where(
                        GroupAdFrequency.telegram_group_id.in_(keys),
                        GroupAdFrequency.status == "active",
                    )
                )
            ).all()
        )
        if keys
        else []
    )
    if any(
        (s.daily_review_error and s.daily_review_error != "qualification_delivery_reconciliation_required")
        or (s.daily_review_expires_at and s.daily_review_expires_at > now)
        for s in states
    ):
        return {}
    # Legacy rows can lack a stored deadline. Resolve their actual cycle before
    # lending, rather than treating a missing field as an empty workload.
    from app.modules.acquisition.daily_frequency import DailyFrequencyService

    deadlines = []
    daily = DailyFrequencyService(db)
    for state in states:
        plan = None
        reconciliation = state.daily_review_error == "qualification_delivery_reconciliation_required"
        if reconciliation or state.daily_review_due_at is None:
            logs = await daily.frequency.logs(state.telegram_group_id)
            retained = [log for log in logs if log.id not in parked]
            # Prove that only our parked receipts caused this particular hold.
            # Other accounts, ambiguous identities and addressable unknown
            # receipts must still disable lending.
            if reconciliation and len(retained) == len(logs):
                return {}
            plan = await daily.plan(state, logs=retained)
            if plan.reason:
                return {}
        if state.daily_review_due_at:
            deadlines.append(state.daily_review_due_at)
        else:
            if plan and plan.due_at:
                deadlines.append(plan.due_at)
    # Include idle states conservatively when their old deadline is still set.
    from app.modules.acquisition.read_costs import operation_costs

    cost = max(10, (await operation_costs(account_id))["daily_review_read_cost"])
    result = {}
    for window, duration in (("hour", 3600), ("day", 86400)):
        usage = budget["usage"][window]
        ttl = usage["ttl_seconds"]
        if ttl == -1:  # Missing expiry is an unknown budget state, not headroom.
            return {}
        end = now + timedelta(seconds=ttl if ttl > 0 else duration)
        # Leave a short boundary margin; the next RPC recomputes the demand.
        due = sum(due_at <= end + timedelta(minutes=2) for due_at in deadlines)
        from app.core.account.reserved_reads import listener_reserve
        # The contingency covers the remaining window, not a second full day.
        # Listener control keeps its entire unspent allowance, even near expiry.
        remaining = min(duration, max(1, ttl)) if ttl >= 0 else duration
        control = max(0, listener_reserve(limits[window])
                      - budget['usage'].get('listener_' + window, {}).get('used', 0))
        buffer = max(cost, math.ceil(limits[window] * .05 * remaining / duration), control)
        result[window] = due * cost + buffer
    return result


async def add_idle_survival_headroom(
    db: Any, account_id: int, now: datetime, limits: dict, budget: dict
) -> None:
    """Enrich a snapshot; failure leaves all hard reservations intact."""
    from app.core.account.reserved_reads import (
        allocations,
        available,
        lending_caps,
        transferred_caps,
    )

    holds = await survival_holds(db, account_id, now, limits, budget)
    if not holds:
        return
    rooms, delays, loans = [], [], {}
    for window, duration in (("hour", 3600), ("day", 86400)):
        # The lane view already includes conservative classification of legacy
        # usage. Raw join_reserved/flex counters alone can undercount old spend.
        used = [
            budget["usage"][f"{lane}_{window}"]["used"]
            for lane in ("ad", "survival", "critical", "sync", "routine")
        ]
        caps = transferred_caps(
            allocations(limits[window]), budget["usage"][f"survival_lent_{window}"]["used"],
            budget["usage"].get(f"sync_lent_{window}", {}).get("used", 0),
            budget["usage"].get(f"ad_lent_{window}", {}).get("used", 0)
        )
        adjusted = lending_caps(caps, used, "critical", holds[window])
        room = available(limits[window], used, adjusted, "critical")
        rooms.append(room)
        if room <= 0:
            ttl = budget["usage"][window]["ttl_seconds"]
            delays.append(max(1, ttl) if ttl >= 0 else duration)
        loans[window] = caps[1] - adjusted[1]
    budget["survival_lending"] = {"holds": holds, "available": loans}
    limits["survival_hold_hour"], limits["survival_hold_day"] = holds["hour"], holds["day"]
    budget["lanes"]["critical"] = {
        "remaining": min(budget["usage"]["minute"]["remaining"], *rooms),
        "retry_after_seconds": max(budget["retry_after_seconds"], *delays, 0),
    }
