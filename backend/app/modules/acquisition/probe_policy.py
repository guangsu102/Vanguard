"""Write-probe capacity and time calculations; no Telegram or state changes."""

from datetime import datetime, timedelta
from typing import Any


def write_probe_limit(capacity: dict[str, Any], account: Any, now: datetime) -> int:
    configured = max(0, min(10, int(capacity.get("max_new_ad_groups_per_day", 2))))
    if configured <= 2:
        return configured
    level = str(
        getattr(getattr(account, "risk_level", None), "value", getattr(account, "risk_level", ""))
    )
    healthy = (
        level == "normal"
        and not any(
            isinstance(value, datetime) and value > now
            for value in (
                getattr(account, "risk_pause_until", None),
                getattr(account, "risk_recovery_until", None),
            )
        )
    )
    return configured if healthy else 2


def probe_day_start(now: datetime, capacity: dict[str, Any]) -> datetime:
    offset = timedelta(hours=int(capacity.get("timezone_offset_hours", 8)))
    local = now + offset
    start = local.replace(
        hour=int(capacity.get("window_start_hour", 9)), minute=0, second=0, microsecond=0
    )
    return (start if local >= start else start - timedelta(days=1)) - offset


def in_ad_window_at(now: datetime, capacity: dict[str, Any]) -> datetime:
    offset = timedelta(hours=int(capacity.get("timezone_offset_hours", 8)))
    local = now + offset
    start, end = int(capacity.get("window_start_hour", 9)), int(capacity.get("window_end_hour", 2))
    allowed = start == end or (
        start <= local.hour < end if start < end else local.hour >= start or local.hour < end
    )
    if allowed:
        return now
    next_start = local.replace(hour=start, minute=0, second=0, microsecond=0)
    if next_start < local:
        next_start += timedelta(days=1)
    return next_start - offset
