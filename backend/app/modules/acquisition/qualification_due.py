"""One due-work predicate for dispatch, execution and recovery."""

from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.core.group.models import GroupAccountMembership as Member
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit as Audit


def due_clause(now: datetime) -> Any:
    timer = and_(Audit.next_retry_at.is_not(None), Audit.next_retry_at <= now)
    return or_(
        and_(Audit.state == "queued", or_(Audit.next_retry_at.is_(None), timer)),
        and_(Audit.state.in_(["running", "reviewing_ai", "waiting_ai"]), timer),
        and_(Audit.state == "completed", timer, Audit.decision.in_(
            ["unknown", "technical_wait", "observe", "wait", "reject", "allowed", "trial"]
        )),
    )


def current_clause() -> Any:
    member = aliased(Member)
    older = aliased(Audit)
    latest = select(func.max(older.id)).where(
        older.membership_id == member.id,
        older.membership_joined_at.is_not_distinct_from(member.joined_at),
        older.state != "cancelled",
    ).correlate(member).scalar_subquery()
    return exists(select(member.id).where(
        member.id == Audit.membership_id,
        member.account_id == Audit.account_id, member.group_id == Audit.group_id,
        member.joined_at.is_not_distinct_from(Audit.membership_joined_at),
        member.status.in_(["joined", "pending", "leave_failed", "rejected"]),
        Audit.id == latest, Audit.policy_version == POLICY_VERSION,
        Audit.content_scope == "text_profile",
    ))
