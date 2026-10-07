"""Operational wait timestamps live outside immutable qualification evidence."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.settings_models import SystemSetting


# These values describe a local scheduler wait.  They are deliberately kept
# separate from evidence maturity (two hour/24 hour observation deadlines) and
# from provider/AI backoff.  A local budget can recover early when another
# worker releases a reservation or a new window opens.
BUDGET_WAIT_REASONS = frozenset({"telegram_read_budget", "telegram_read_slice"})


async def record(db: Any, audit_id: int, reason: str, required: int, retry: datetime, now: datetime) -> None:
    key = f"qualification.wait.{audit_id}"
    row = await db.get(SystemSetting, key)
    prior = json.loads(row.value or "{}") if row else {}
    value = json.dumps({"reason": reason, "required_reads": required, "retry_at": retry.isoformat(),
                        "checked_at": now.isoformat(), "first_blocked_at": prior.get("first_blocked_at") or now.isoformat()})
    if row is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        row.value = value


async def clear(db: Any, audit_id: int) -> None:
    row = await db.get(SystemSetting, f"qualification.wait.{audit_id}")
    if row is not None:
        await db.delete(row)


async def budget_wait_ids(
    db: Any,
    account_id: int,
    *,
    now: datetime | None = None,
    limit: int = 500,
) -> set[int]:
    """Return audits whose persisted timer came from the local read budget.

    ``next_retry_at`` is an operational forecast.  Keeping its provenance in
    ``SystemSetting`` lets the dispatcher shorten stale timers after a fresh
    budget check without waking evidence observation or AI retry timers.
    Invalid/stale setting rows are ignored; the audit query below is the final
    scope guard for account and state.
    """
    from app.modules.acquisition.models import GroupQualificationAudit

    keys = (
        await db.scalars(
            select(SystemSetting).where(SystemSetting.key.like("qualification.wait.%")).limit(limit)
        )
    ).all()
    ids: set[int] = set()
    for setting in keys:
        try:
            audit_id = int(setting.key.rsplit(".", 1)[-1])
            value = json.loads(setting.value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if value.get("reason") in BUDGET_WAIT_REASONS:
            ids.add(audit_id)
    if not ids:
        return set()
    current = now or datetime.utcnow()
    rows = (
        await db.scalars(
            select(GroupQualificationAudit.id).where(
                GroupQualificationAudit.id.in_(ids),
                GroupQualificationAudit.account_id == account_id,
                GroupQualificationAudit.state.in_(["queued", "completed"]),
                GroupQualificationAudit.next_retry_at.is_not(None),
                GroupQualificationAudit.next_retry_at > current,
            )
        )
    ).all()
    return {int(value) for value in rows}
