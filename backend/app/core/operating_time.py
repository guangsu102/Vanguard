"""Operating-day helpers for UTC-naive database timestamps."""

from datetime import UTC, date, datetime, timedelta

DEFAULT_OPERATING_TIMEZONE_OFFSET_HOURS = 8


def operating_day_start(
    now: datetime | None = None,
    *,
    timezone_offset_hours: int = DEFAULT_OPERATING_TIMEZONE_OFFSET_HOURS,
) -> datetime:
    """Return the UTC-naive start of the local operating day."""
    current = now or datetime.now(UTC).replace(tzinfo=None)
    offset = timedelta(hours=timezone_offset_hours)
    local_now = current + offset
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0) - offset


def operating_date(
    now: datetime | None = None,
    *,
    timezone_offset_hours: int = DEFAULT_OPERATING_TIMEZONE_OFFSET_HOURS,
) -> date:
    """Return the local operating date for a UTC-naive timestamp."""
    current = now or datetime.now(UTC).replace(tzinfo=None)
    return (current + timedelta(hours=timezone_offset_hours)).date()
