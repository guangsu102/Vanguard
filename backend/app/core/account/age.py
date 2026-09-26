"""Auditable age lower bounds, independent of operational account health."""
from __future__ import annotations
import json
from datetime import UTC, datetime
from typing import Any


def utc_naive(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError, OverflowError):
        return None


def age_attestation_payload(account: Any) -> dict[str, Any]:
    raw = getattr(account, 'age_attestation_json', None)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def age_evidence(account: Any, now: datetime) -> dict[str, Any]:
    now = utc_naive(now)
    if account is None or now is None:
        return {'minimum_age_days': None, 'source': 'unknown', 'conflict': False}
    raw_registration = getattr(account, 'registered_at', None)
    registered = utc_naive(raw_registration) if raw_registration is not None else None
    if raw_registration is not None and (registered is None or registered > now):
        return {'minimum_age_days': None, 'source': 'invalid_registration', 'conflict': True}
    statement = age_attestation_payload(account)
    confirmed = utc_naive(statement.get('confirmed_at'))
    minimum = statement.get('minimum_age_days')
    valid = (
        type(minimum) is int and 0 <= minimum <= 36500 and confirmed is not None
        and confirmed <= now and not statement.get('revoked_at')
        and statement.get('source') == 'owner_confirmation'
        and type(statement.get('confirmed_by')) is int and statement['confirmed_by'] > 0
        and type(statement.get('version')) is int and statement['version'] >= 1
    )
    if registered is not None and valid and (confirmed - registered).total_seconds() < minimum * 86400:
        return {'minimum_age_days': None, 'source': 'conflicting_evidence', 'conflict': True}
    if registered is not None:
        return {'minimum_age_days': (now - registered).days, 'source': 'registered_at', 'conflict': False}
    if valid:
        return {'minimum_age_days': minimum + (now - confirmed).days, 'source': 'owner_confirmation', 'conflict': False}
    return {'minimum_age_days': None, 'source': 'unknown', 'conflict': False}


def verified_age_days(account: Any, now: datetime) -> int | None:
    return age_evidence(account, now)['minimum_age_days']


def account_age_eligibility_reason(account: Any, now: datetime, min_age_days: int = 180) -> str | None:
    evidence = age_evidence(account, now)
    if evidence['conflict']:
        return 'account_age_evidence_conflict'
    if evidence['minimum_age_days'] is None or evidence['minimum_age_days'] < min_age_days:
        return 'account_age_not_verified_six_months'
    return None
