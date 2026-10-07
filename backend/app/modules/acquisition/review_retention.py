"""Retire obsolete scheduling state while preserving historical evidence."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased

from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.models import GroupQualificationAudit


async def retire_obsolete_reviews(db: Any, *, limit: int = 500) -> int:
    newer = aliased(GroupQualificationAudit)
    superseded = exists(select(newer.id).where(
        newer.membership_id == GroupQualificationAudit.membership_id,
        newer.id > GroupQualificationAudit.id,
        newer.state != "cancelled",
    ))
    terminal_membership = GroupAccountMembership.status.in_(
        ["left", "banned", "rejected", "leave_failed", "account_lost"]
    )
    actionable = or_(
        GroupQualificationAudit.state.in_(["queued", "waiting_ai", "reviewing_ai", "manual_required"]),
        (GroupQualificationAudit.state == "completed") & GroupQualificationAudit.next_retry_at.is_not(None),
        (GroupQualificationAudit.state == "waiting_membership") & terminal_membership,
    )
    rows = (await db.scalars(select(GroupQualificationAudit).join(
        GroupAccountMembership, GroupAccountMembership.id == GroupQualificationAudit.membership_id,
    ).where(actionable, or_(
        superseded,
        terminal_membership,
        (GroupQualificationAudit.decision == "protected") & (GroupAccountMembership.review_status == "owned_group_excluded"),
        (GroupQualificationAudit.reason == "join_approval_pending") & (GroupAccountMembership.status == "pending"),
    )).order_by(GroupQualificationAudit.id).limit(max(1, min(limit, 500)))
        .with_for_update(of=GroupQualificationAudit, skip_locked=True))).all()
    for row in rows:
        member = await db.get(GroupAccountMembership, row.membership_id)
        latest = await db.scalar(select(GroupQualificationAudit.id).where(
            GroupQualificationAudit.membership_id == row.membership_id,
            GroupQualificationAudit.state != "cancelled",
        ).order_by(GroupQualificationAudit.id.desc()).limit(1))
        if row.decision in {"allowed", "trial"}:
            # Older grants still support continuity; retire their timer only.
            row.state = "completed"
        elif latest != row.id:
            row.state, row.reason = "cancelled", "superseded_by_newer_review"
        elif member.status in {"left", "banned", "rejected", "leave_failed", "account_lost"} or member.joined_at != row.membership_joined_at:
            row.state, row.reason = "cancelled", "membership_or_scope_changed"
        elif row.decision == "protected":
            row.state = "completed"
        else:
            row.state = "waiting_membership"
        row.next_retry_at = None
    await db.flush()
    return len(rows)

def waits_for_new_evidence(snapshot: dict, now: datetime) -> bool:
    """Inspect persisted evidence without counting another review attempt."""
    from app.modules.acquisition.evidence_progress import date, evidence_maturity, progress_retry

    if snapshot.get("decision") != "observe" or evidence_maturity(snapshot, now) or progress_retry(snapshot, now):
        return False
    reason = snapshot.get("reason")
    if reason == "group_rules_ai_provider_content_rejected":
        return int(snapshot.get("unchanged_rejection_count", 0)) >= 3
    started = date(snapshot.get("observation_started_at"))
    return bool(
        int(snapshot.get("ai_review_failures", 0)) >= 3
        or (started is not None and now >= started + timedelta(hours=24)
            and reason in {"qualification_evidence_incomplete", "rules_coverage_incomplete"})
    )
