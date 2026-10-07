"""Spread useful advertising across the remaining read-budget window."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any


def pacing_deadline(rpc: dict, *, capacity: int, delivery_cost: int,
                    last_sent: datetime | None, now: datetime) -> datetime | None:
    limits, usage = rpc.get("limits", {}), rpc.get("usage", {})
    day = usage.get("day", {})
    ad = usage.get("ad_day", {})
    if not limits.get("ad_day") or capacity <= 0:
        return None
    # Keep 20% for unsuccessful reads/reconciliation, just as the output model.
    lent = usage.get('ad_lent_day', {}).get('used', 0)
    allocation = max(0, limits["ad_day"] - lent)
    remaining = max(0, allocation - ad.get('used', 0))
    forecast = (rpc.get('ad_demand_lending', {}).get('forecast') or {}).get('day')
    if forecast and remaining >= forecast['hold']:
        # Every scheduled send/repair and its retry buffer are already funded.
        # Do not spread this plan across a second whole-day quota or charge a
        # new buffer against historical spend.
        return last_sent + timedelta(seconds=math.ceil(86400 / capacity)) if last_sent else None
    reads = (remaining * 4 // 5 if lent or forecast
             else max(0, allocation * 4 // 5 - ad.get("used", 0)))
    slots = reads // max(1, delivery_cost)
    ttl = day.get("ttl_seconds", -2)
    horizon = max(1, ttl) if ttl >= 0 else 86400
    if slots == 0:
        return now + timedelta(seconds=horizon + 1)
    interval = max(math.ceil(86400 / capacity), math.ceil(horizon / slots))
    return last_sent + timedelta(seconds=interval) if last_sent else None


async def account_pacing_deadline(db: Any, config: Any, now: datetime, last_sent: datetime | None) -> datetime | None:
    if getattr(config, "dynamic_capacity_enabled", False) is not True:
        return None
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.account.models import TelegramAccount
    from app.core.account.outbound_budget import effective_capacity_limits
    from app.core.account.rpc_governor import snapshot
    from app.modules.acquisition.ad_output_plan import mixed_resource_capacity
    from app.modules.acquisition.capacity import inventory_snapshot
    from app.modules.acquisition.capacity_reads import CapacityReads
    from app.modules.acquisition.read_costs import operation_costs

    reader = CapacityReads(db) if isinstance(db, AsyncSession) else db
    rpc = await snapshot(reader, config.account_id, now)
    if not rpc.get("limits", {}).get("ad_day"):
        return None
    costs = await operation_costs(config.account_id)
    inventory = await inventory_snapshot(reader, config.account_id, now)
    account = await reader.get(TelegramAccount, config.account_id)
    limits = await effective_capacity_limits(reader, account, config, now)
    capacity = mixed_resource_capacity(limits["ad"], rpc["limits"], inventory, costs)
    return pacing_deadline(rpc, capacity=capacity, delivery_cost=costs["delivery_read_cost"],
                           last_sent=last_sent, now=now)
