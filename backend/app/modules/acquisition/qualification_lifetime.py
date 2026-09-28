"""Qualification lifetime, separate from observation and delivery windows."""
from datetime import datetime, timedelta

EVIDENCE_TTL = timedelta(hours=720)
RENEWAL_LEAD = timedelta(hours=24)


def evidence_expiry(collected_at: datetime, now: datetime) -> datetime:
    return min(collected_at, now) + EVIDENCE_TTL


def renewal_at(collected_at: datetime, now: datetime) -> datetime:
    return max(now, evidence_expiry(collected_at, now) - RENEWAL_LEAD)
