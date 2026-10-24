"""Bound new joins by each account's first-review capacity.

The review workers still have a global concurrency limit, but a backlog owned
by one account must not consume another account's join admission.  The caller
passes the account whose next join is being planned; omitting it is retained
for operational aggregate reporting only.
"""

import math
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

REVIEW_CONCURRENCY = 2
FIRST_REVIEW_HORIZON = 120
# Includes network variation and scheduler handoff; execution samples can raise it.
SERVICE_SECONDS = 60


def available_slots(pending: int, service_seconds: float = SERVICE_SECONDS) -> int:
    return max(0, math.floor(FIRST_REVIEW_HORIZON * REVIEW_CONCURRENCY /
                             max(SERVICE_SECONDS, service_seconds)) - pending)


async def execution_plan(
    db: Any,
    now: datetime,
    account_id: int | None = None,
) -> dict:
    from app.core.group.models import GroupAccountMembership as Member
    from app.core.scheduler.growth_dispatch import eligible_accounts
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    latest = select(func.max(Audit.id)).where(
        Audit.membership_id == Member.id, Audit.membership_joined_at == Member.joined_at,
        Audit.state != "cancelled",
    ).correlate(Member).scalar_subquery()
    account_scope = (
        Member.account_id == account_id
        if account_id is not None
        else Member.account_id.in_(eligible_accounts(now))
    )
    rows = (await db.execute(select(Member, Audit).outerjoin(Audit, Audit.id == latest).where(
        account_scope, Member.status == "joined",
        Member.left_at.is_(None),
        Member.review_status.notin_(["approved", "manual_required", "owned_group_excluded", "exit_pending"]),
    ).limit(501))).all()
    if len(rows) > 500:
        return {"remaining": 0, "reason": "review_inventory_incomplete",
                "scope": "account" if account_id is not None else "global", "account_id": account_id}
    pending, seconds = 0, SERVICE_SECONDS
    for _member, audit in rows:
        if audit is not None:
            # Terminal decisions and observations waiting for a future event do
            # not occupy the executable first-review lane.  A retry is pressure
            # only once its retry time is due. A running review still occupies
            # capacity while its worker lease is valid.
            if waiting_for_evidence(audit, now) or audit.state in {"manual_required", "cancelled"}:
                continue
            if audit.state in {"completed", "waiting_ai", "waiting_membership"} and audit.next_retry_at is None:
                continue
            if audit.state not in {"running", "reviewing_ai"} and audit.next_retry_at and audit.next_retry_at > now:
                continue
            from app.modules.acquisition.adaptive_frequency import payload
            measured = (payload(audit.evidence_json).get("read_cost") or {}).get("elapsed_seconds", 0)
            if isinstance(measured, (int, float)) and math.isfinite(measured):
                seconds = max(seconds, min(240, measured))
        pending += 1
    slots = available_slots(pending, seconds)
    return {"remaining": slots, "reason": None if slots else "join_wait_execution_capacity",
            "pending": pending, "service_seconds": seconds, "concurrency": REVIEW_CONCURRENCY,
            "horizon_seconds": FIRST_REVIEW_HORIZON,
            "scope": "account" if account_id is not None else "global", "account_id": account_id}
