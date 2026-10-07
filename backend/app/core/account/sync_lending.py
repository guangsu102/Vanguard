"""Lend bounded sync headroom only while a fresh listener is caught up."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.worker_status import TelegramWorkerStatus


def caught_up(item: dict) -> bool:
    journal = item.get("raw_journal") or {}
    return bool(
        item.get("state") == "connected"
        and item.get("connected") is True
        and item.get("checkpoint_scope") == "durable_session_and_inbox"
        and item.get("update_queue_size") == 0
        and item.get("update_handler_tasks") == 0
        and not item.get("sync_wait_reason")
        and not item.get("pause_error")
        and not item.get("read_wait_seconds")
        and journal.get("available") is True
        and not journal.get("storage_error")
        and isinstance(journal.get("states"), dict)
        and isinstance(journal.get("queues"), dict)
        and not journal.get("pressure")
        and (journal.get("checkpoint") or {}).get("count", 0) > 0
        and all(
            not (journal.get("states") or {}).get(name, 0)
            for name in ("pending", "reconciliation_required")
        )
        and all(not entry.get("count") for entry in (journal.get("queues") or {}).values())
    )


def day_blocked_listener(item: dict, limits: dict, budget: dict) -> bool:
    """An exhausted day must not strand every unused hourly sync reservation.

    Keep half the hourly share even here: new capacity/usage can change the
    wait. Never lend the daily sync share or infer recovery from a connected
    socket alone. The listener must be durably checkpointing its input.
    """
    from app.core.account.reserved_reads import allocations, available, transferred_caps

    journal = item.get('raw_journal') or {}
    if not (
        item.get('state') == 'connected_wait' and item.get('connected') is True
        and item.get('sync_wait_reason') == 'telegram_read_budget'
        and item.get('sync_wait_scope') == 'account'
        and item.get('checkpoint_scope') == 'durable_session_and_inbox'
        and not item.get('pause_error') and item.get('update_handler_tasks') == 0
        and journal.get('available') is True and not journal.get('storage_error')
        and not journal.get('pressure')
        and (journal.get('checkpoint') or {}).get('count', 0) > 0
        and isinstance(journal.get('states'), dict) and isinstance(journal.get('queues'), dict)
        and not journal['states'].get('reconciliation_required', 0)
        and not (journal['queues'].get('business_facts') or {}).get('count')
        and all(entry.get('count', 0) <= 4 and entry.get('bytes', 0) <= 4096
                for name, entry in journal['queues'].items() if name != 'business_facts')
    ):
        return False
    usage = budget['usage']
    hour_ttl, day_ttl = (usage[w]['ttl_seconds'] for w in ('hour', 'day'))
    # A missing hourly counter starts a new full hour on the first read.
    hour_horizon = 3600 if hour_ttl == -2 else hour_ttl
    if hour_horizon <= 0 or day_ttl <= hour_horizon + 60:
        return False
    caps = transferred_caps(allocations(limits['day']), usage['survival_lent_day']['used'],
                            usage['sync_lent_day']['used'], usage.get('ad_lent_day', {}).get('used', 0))
    used = [usage[lane + '_day']['used'] for lane in ('ad', 'survival', 'critical', 'sync', 'routine')]
    return used[3] >= caps[3] and available(limits['day'], used, caps, 'sync') == 0


async def add_idle_sync_headroom(
    db: Any,
    account_id: int,
    now: datetime,
    limits: dict,
    budget: dict,
) -> None:
    """Keep at least half of sync's base allocation and projected remaining load.

    Missing, stale, paused or backlogged listeners never enable a new loan.
    A checkpointing listener blocked by its daily share can lend only bounded
    hourly headroom; it keeps its daily share and half the hourly allocation.
    Spent loans are accounted until the existing window expires, and an admitted
    work reservation retains its small loan even when later demand increases.
    """
    from app.core.account.reserved_reads import (
        allocations,
        available,
        lending_caps,
        transferred_caps,
    )

    rows = list(
        (
            await db.scalars(
                select(TelegramWorkerStatus).where(
                    TelegramWorkerStatus.role == "growth_user_worker",
                    TelegramWorkerStatus.status == "online",
                    TelegramWorkerStatus.last_heartbeat_at >= now - timedelta(seconds=90),
                    TelegramWorkerStatus.last_heartbeat_at <= now + timedelta(seconds=5),
                )
            )
        ).all()
    )
    listeners = [
        item
        for row in rows
        for item in json.loads(row.metadata_json or "{}")
        .get("runtime", {})
        .get("listeners", [])
        if item.get("account_id") == account_id
    ]
    if len(listeners) != 1:
        return
    idle = caught_up(listeners[0])
    day_blocked = not idle and day_blocked_listener(listeners[0], limits, budget)
    if not idle and not day_blocked:
        return
    holds, rooms, loans, waits = {}, [], {}, []
    for window, duration in (("hour", 3600), ("day", 86400)):
        usage = budget["usage"]
        ttl = usage[window]["ttl_seconds"]
        if ttl == -1:
            return
        base = allocations(limits[window])
        used = [
            usage[f"{lane}_{window}"]["used"]
            for lane in ("ad", "survival", "critical", "sync", "routine")
        ]
        remaining = min(duration, max(0, ttl)) if ttl >= 0 else duration
        elapsed = max(60, duration - remaining)
        projected = math.ceil(used[3] * remaining / elapsed * 1.5) + 8
        holds[window] = max(math.ceil(base[3] / 2), projected)
        if day_blocked and window == 'day':
            # Include the daily limit in the resulting lane budget, while
            # leaving this donor fully protected without proof of idleness.
            holds[window] = -1
        caps = transferred_caps(
            base, usage[f"survival_lent_{window}"]["used"], usage[f"sync_lent_{window}"]["used"],
            usage.get(f"ad_lent_{window}", {}).get("used", 0)
        )
        adjusted = lending_caps(
            caps, used, "critical", limits.get(f"survival_hold_{window}", -1), holds[window]
        )
        room = available(limits[window], used, adjusted, "critical")
        rooms.append(room)
        loans[window] = caps[3] - adjusted[3]
        if room <= 0:
            waits.append(max(1, ttl) if ttl >= 0 else duration)
    limits.update({f"sync_hold_{window}": value for window, value in holds.items()})
    budget["sync_lending"] = {"holds": holds, "available": loans, "listener_caught_up": idle,
                              "reason": "sync_day_exhausted" if day_blocked else "listener_caught_up"}
    budget["lanes"]["critical"] = {
        "remaining": min(budget["usage"]["minute"]["remaining"], *rooms),
        "retry_after_seconds": max(budget["retry_after_seconds"], *waits, 0),
    }
