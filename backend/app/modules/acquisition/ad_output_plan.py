"""Read-only advertising capacity and admission control for new group work."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.modules.acquisition.models import AdDeliveryLog


def resource_capacity(ad_limit: int, read_limits: dict[str, int], *, delivery_cost: int = 12, survival_cost: int = 30) -> int:
    """Conservative whole-day planning, never permission to send or reset a budget.

    Advertising and survival have independent reservations. Keep 20 percent of
    each for retries and reconciliation; sync/maintenance use their own remainder.
    This projects a fresh whole day and never changes current budget counters.
    """
    return max(0, min(
        ad_limit,
        read_limits.get("ad_day", 0) * 4 // 5 // max(1, delivery_cost),
        read_limits.get("survival_day", 0) * 4 // 5 // max(1, survival_cost),
    ))


def expansion_plan(
    capacity: int,
    inventory: dict[str, int],
    *,
    ad_due: int,
    survival_due: int,
    unresolved: int,
    unavailable_reason: str | None,
    material_count: int,
) -> dict[str, Any]:
    usable = inventory.get("usable", 0)
    slots = inventory.get("slots_24h", 0)
    backlog = inventory.get("active_backlog", 0)
    # One new group starts with one slot/day. Existing higher-frequency groups
    # already cover those slots and must not create an artificial group deficit.
    target = min(300, usable + max(0, 2 * capacity - slots)) if capacity else 0
    deficit = max(0, target - usable - backlog)
    reason = unavailable_reason
    if reason is None:
        if not material_count:
            reason = "join_ad_material_missing"
        elif unresolved:
            reason = "join_wait_ad_reconciliation"
        elif survival_due:
            reason = "join_wait_ad_survival"
        elif ad_due:
            reason = "join_wait_ad_delivery"
        elif not capacity:
            reason = "join_ad_capacity_unavailable"
        elif not deficit and target > usable:
            reason = "join_wait_inventory_review"
        elif not deficit:
            reason = "join_inventory_target_met"
    return {
        "sustainable_ads_24h": capacity,
        "planned_ads_24h": min(capacity, slots),
        "available_slots_24h": slots,
        "usable_groups": usable,
        "target_groups": target,
        "group_deficit": deficit,
        "qualified_group_deficit": max(0, target - usable),
        "pending_review_groups": backlog,
        "ad_due": ad_due,
        "survival_due": survival_due,
        "unresolved_sends": unresolved,
        "join_blocker": reason,
        "read_cost_source": "conservative_estimate",
        "delivery_read_cost": 12,
        "survival_read_cost": 30,
    }


async def output_metrics(db: Any, account_id: int, now: datetime) -> dict[str, Any]:
    """One send cohort; checkpoints never turn unknowns into failures."""
    log = AdDeliveryLog
    rows = await db.execute(
        select(
            func.count(log.id).label("sent_24h"),
            func.count(log.id).filter(log.survived_one_hour_at.is_not(None)).label("confirmed_1h"),
            func.count(log.id).filter(log.survival_status == "deleted").label("deleted"),
            func.count(log.id)
            .filter(log.survival_status.in_(["pending", "checking", "check_failed"]))
            .label("unresolved"),
        ).where(
            log.account_id == account_id,
            log.status == "success",
            log.telegram_message_id.is_not(None),
            log.sent_at > now - timedelta(hours=24),
        )
    )
    metrics = dict(rows.mappings().one())
    # Completed age cohort is shown separately from the last 24h sends.
    rows = await db.execute(
        select(
            func.count(log.id).label("matured_sends_72h"),
            func.count(log.id)
            .filter(log.survived_twenty_four_hour_at.is_not(None))
            .label("confirmed_24h"),
            func.count(log.id)
            .filter(log.survival_status.in_(["pending", "checking", "check_failed"]))
            .label("matured_unresolved"),
        ).where(
            log.account_id == account_id,
            log.status == "success",
            log.telegram_message_id.is_not(None),
            log.sent_at > now - timedelta(hours=72),
            log.sent_at <= now - timedelta(hours=24),
        )
    )
    metrics.update(dict(rows.mappings().one()))
    from app.modules.acquisition.survival_schedule import backlog
    metrics.update(await backlog(db, account_id, now))
    return metrics


async def ad_output_plan(
    db: Any,
    account: Any,
    config: Any,
    now: datetime,
    *,
    inventory: dict[str, int] | None = None,
    rpc: dict | None = None,
    limits: dict | None = None,
    workload: dict | None = None,
) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.account.outbound_budget import effective_capacity_limits
    from app.core.account.rpc_governor import snapshot
    from app.modules.acquisition.capacity import executable_inventory, inventory_snapshot
    from app.modules.acquisition.capacity_reads import CapacityReads

    if isinstance(db, AsyncSession):
        db = CapacityReads(db)
    account_id = account.id
    inventory = (
        inventory if inventory is not None else await inventory_snapshot(db, account_id, now)
    )
    rpc = rpc if rpc is not None else await snapshot(db, account_id, now)
    limits = (
        limits if limits is not None else await effective_capacity_limits(db, account, config, now)
    )
    workload = (
        workload
        if workload is not None
        else await executable_inventory(db, account_id, config, now, include_candidates=False)
    )
    from app.core.automation_settings import get_ad_capacity_settings
    from app.modules.acquisition.survival_schedule import overdue_clause
    capacity = await get_ad_capacity_settings(db)
    log = AdDeliveryLog
    rows = await db.execute(
        select(
            func.count(log.id)
            .filter(
                log.status == "success",
                log.survival_status == "pending",
                overdue_clause(now, capacity),
            )
            .label("survival_due"),
            func.count(log.id)
            .filter(log.status.in_(["pending", "unknown", "sending", "reconciliation_required"]))
            .label("unresolved"),
        ).where(log.account_id == account_id)
    )
    pending = rows.mappings().one()
    reason = None
    if str(getattr(account.risk_level, "value", account.risk_level)) == "quarantined":
        reason = "account_risk_quarantined"
    elif not config.enabled or not config.auto_ads_enabled or limits.get("ad", 0) <= 0:
        reason = "join_ad_account_unavailable"
    elif rpc.get("state") in {"budget_wait", "cooldown", "unavailable"}:
        reason = rpc.get("reason") or "join_ad_capacity_unavailable"
    elif any(
        workload.get("blocker_counts", {}).get(code)
        for code in (
            "qualification_disabled",
            "ad_delivery_paused",
            "qualification_rollout_paused",
            "account_quiet_hours",
            "ad_time_window_blocked",
        )
    ):
        reason = "join_ad_delivery_paused"
    from app.modules.acquisition.read_costs import operation_costs
    costs = await operation_costs(account_id)
    plan = expansion_plan(
        resource_capacity(limits.get("ad", 0), rpc.get("limits", {}),
                          delivery_cost=costs["delivery_read_cost"],
                          survival_cost=costs["survival_read_cost"]),
        inventory,
        ad_due=workload.get("ad_targets", 0),
        survival_due=pending["survival_due"],
        unresolved=pending["unresolved"],
        unavailable_reason=reason,
        material_count=workload.get("material_count", 0),
    )
    return {**plan, **costs}


def delivery_priority(
    *,
    due: datetime,
    last_sent: datetime | None,
    survived: int,
    deleted: int,
    now: datetime,
    identity: int,
) -> tuple:
    # Age waiting work into the highest class so new groups cannot starve.
    base = 0 if survived and not deleted else 1 if last_sent is None else 2
    rank = max(0, base - max(0, (now - due).days))
    return rank, due, last_sent or datetime.min, identity


async def prioritize_deliveries(
    db: Any, members: list[Any], campaign_id: int | None, now: datetime
) -> list[Any]:
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.models import AdDeliveryScheduleState

    if not members:
        return members
    account_id = members[0].account_id
    ids = [member.id for member in members]
    log = AdDeliveryLog
    rows = (
        await db.execute(
            select(
                GroupAccountMembership.id,
                func.max(log.sent_at).label("last_sent"),
                func.count(log.id)
                .filter(log.survived_twenty_four_hour_at.is_not(None))
                .label("survived"),
                func.count(log.id).filter(log.survival_status == "deleted").label("deleted"),
            )
            .outerjoin(
                log,
                (log.account_id == GroupAccountMembership.account_id)
                & (log.group_id == GroupAccountMembership.group_id)
                & (log.status == "success")
                & log.telegram_message_id.is_not(None)
                & (log.sent_at >= GroupAccountMembership.joined_at)
                & (log.sent_at >= now - timedelta(days=7)),
            )
            .where(GroupAccountMembership.id.in_(ids))
            .group_by(GroupAccountMembership.id)
        )
    ).all()
    history = {row.id: row for row in rows}
    query = select(AdDeliveryScheduleState.group_id, AdDeliveryScheduleState.next_due_at).where(
        AdDeliveryScheduleState.account_id == account_id
    )
    if campaign_id is not None:
        query = query.where(AdDeliveryScheduleState.campaign_id == campaign_id)
    due = {}
    for row in (await db.execute(query)).all():
        due[row.group_id] = min(due.get(row.group_id, row.next_due_at), row.next_due_at)

    def key(member: Any) -> tuple:
        row = history[member.id]
        return delivery_priority(
            due=due.get(member.group_id, member.joined_at or now),
            last_sent=row.last_sent,
            survived=row.survived,
            deleted=row.deleted,
            now=now,
            identity=member.id,
        )

    return sorted(members, key=key)
