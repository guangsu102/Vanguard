"""Evidence-based three-day send restrictions shared by review and survival checks."""

from datetime import UTC, datetime, timedelta
from typing import Any

LONG_RESTRICTION = timedelta(days=3)
MAX_OBSERVATION_GAP = timedelta(hours=26)


def as_time(value: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(value, datetime):
            return None
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    except (TypeError, ValueError):
        return None


def restriction_facts(blocked_rights: list[Any], now: datetime) -> dict[str, Any]:
    expiries = [as_time(getattr(rights, "until_date", None)) for rights in blocked_rights]
    permanent = bool(blocked_rights) and any(
        end is None or end <= datetime(1970, 1, 2) or end >= now + timedelta(days=366)
        for end in expiries
    )
    until = max((end for end in expiries if end is not None), default=None)
    facts: dict[str, Any] = {
        "restriction_evidence": "telegram_banned_rights" if blocked_rights else None,
        "permanent_send_restriction_verified": permanent,
    }
    if until:
        facts["restriction_until"] = until.isoformat()
        if now < until and not permanent:
            facts["temporary_until"] = until.isoformat()
    return facts


def confirmed_denial(snapshot: dict, now: datetime) -> bool:
    permission = snapshot.get("permissions") or {}
    if (
        snapshot.get("technical_errors")
        or permission.get("member") is not True
        or permission.get("can_send_text") is not False
        or permission.get("verification_required") is True
        or permission.get("verification_pending") is True
        or any(permission.get(key) for key in ("newcomer_until", "slowmode_until"))
    ):
        return False
    until = as_time(permission.get("temporary_until"))
    return until is None or until > now


def track_restriction(
    snapshot: dict, previous: dict, now: datetime, *, last_sent_at: datetime | None = None
) -> None:
    """An unknown read, a membership change (caller), or a successful send breaks a streak."""
    permission = snapshot.get("permissions") or {}
    permission.pop("send_restriction_since", None)
    permission.pop("send_restriction_observed_at", None)
    if not confirmed_denial(snapshot, now):
        return
    old = previous.get("permissions") or {}
    since = as_time(old.get("send_restriction_since"))
    last = as_time(old.get("send_restriction_observed_at"))
    if (
        since is None or last is None or not since <= last <= now
        or now - last > MAX_OBSERVATION_GAP
        or not confirmed_denial(previous, last)
        or (last_sent_at is not None and last_sent_at >= since)
    ):
        since = now
    permission["send_restriction_since"] = since.isoformat()
    permission["send_restriction_observed_at"] = now.isoformat()


def verified_long_restriction(snapshot: dict, now: datetime) -> bool:
    if not confirmed_denial(snapshot, now):
        return False
    permission = snapshot.get("permissions") or {}
    until = as_time(permission.get("restriction_until"))
    if permission.get("restriction_evidence") == "telegram_banned_rights" and (
        permission.get("permanent_send_restriction_verified") is True
        or (until is not None and until > now + LONG_RESTRICTION)
    ):
        return True
    since = as_time(permission.get("send_restriction_since"))
    last = as_time(permission.get("send_restriction_observed_at"))
    return bool(
        since and last and since + LONG_RESTRICTION < last
        and timedelta(0) <= now - last <= timedelta(minutes=5)
    )
