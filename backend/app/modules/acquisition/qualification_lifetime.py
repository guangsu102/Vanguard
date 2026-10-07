"""Evidence freshness is distinct from event-maintained advertising permission."""
import json
from datetime import datetime, timedelta
from typing import Any

EVIDENCE_TTL = timedelta(hours=720)


def evidence_expiry(collected_at: datetime, now: datetime) -> datetime:
    return min(collected_at, now) + EVIDENCE_TTL


def renewal_at(collected_at: datetime, now: datetime) -> None:
    """A granted permission is reconsidered on change, never just because it aged."""
    return None


def authorization_current(row: Any, now: datetime) -> bool:
    """Only an explicitly adopted event grant has no time-only expiry."""
    if not (row and row.decision in {"allowed", "trial"}
            and row.checked_at is not None and row.checked_at <= now):
        return False
    try:
        snapshot = json.loads(row.evidence_json or "{}")
        if snapshot.get("invalidated_at") or snapshot.get("protected"):
            return False
        if snapshot.get("authorization_mode") == "events":
            return True
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(row.expires_at and row.expires_at > now and row.checked_at >= now - EVIDENCE_TTL)
