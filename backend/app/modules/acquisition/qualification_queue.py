"""Persist fair turns for renewals, retries and never-collected memberships."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import case

from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import GroupQualificationAudit


def key(account_id: int) -> str:
    return f"qualification.review_cursor.{account_id}"


async def cursor(db: Any, account_id: int) -> dict:
    row = await db.get(SystemSetting, key(account_id), populate_existing=True)
    try:
        data = json.loads(row.value) if row else {}
        return {
            "last_family": int(data.get("last_family", 0)),
            "renewal_id": max(0, int(data.get("renewal_id", 0))),
            "initial_id": max(0, int(data.get("initial_id", 0))),
            "fresh_id": max(0, int(data.get("fresh_id", 0))),
        }
    except (TypeError, ValueError, AttributeError):
        return {"last_family": 0, "renewal_id": 0, "initial_id": 0, "fresh_id": 0}


def ordering(position: dict, family: Any) -> tuple:
    # Preserve the old renewal/retry cursor IDs; family 2 gets new first reads.
    # Cycle 0 -> 2 -> 1 -> 0, skipping absent/not-ready families. Deferrals
    # never consume a turn, and recurring arrivals cannot starve old retries.
    # Fresh membership/evidence gets the first turn.  The caller adds a
    # separate stale-row guard so renewals can bypass an old parked review
    # without changing the normal three-family rotation.
    turns = {0: (2, 1, 0), 2: (1, 0, 2), 1: (0, 2, 1)}
    first, second, _ = turns.get(position["last_family"], turns[0])
    previous_id = case(
        (family == 0, position["renewal_id"]),
        (family == 2, position.get("fresh_id", 0)),
        else_=position["initial_id"],
    )
    return (
        case((family == first, 0), (family == second, 1), else_=2),
        case((GroupQualificationAudit.id > previous_id, 0), else_=1),
        GroupQualificationAudit.id,
    )


async def claimed(db: Any, account_id: int, audit_id: int, family: int) -> None:
    """Called under the account claim lock, committed with the running claim."""
    data = await cursor(db, account_id)
    data["last_family"] = family
    data[{0: "renewal_id", 1: "initial_id", 2: "fresh_id"}[family]] = audit_id
    row = await db.get(SystemSetting, key(account_id))
    if row is None:
        db.add(SystemSetting(key=key(account_id), value=json.dumps(data)))
    else:
        row.value = json.dumps(data)
