"""Only recoverable near-term reviews count as incoming trial inventory."""

from datetime import datetime, timedelta
from typing import Any


def scheduled_observation(row: Any, now: datetime) -> bool:
    """Known observation backoffs are future work; due work becomes pressure.

    Budget/lease failures and unassessed joins always remain pressure. Keeping
    scheduled observations as incoming inventory bounds replacement admission.
    """
    return bool(
        row.state in {"completed", "queued", "waiting_ai"}
        and row.decision in {"observe", "wait"}
        and row.checked_at is not None
        and row.next_retry_at is not None and row.next_retry_at > now
    )


def waiting_for_evidence(row: Any, now: datetime) -> bool:
    """Completed observations can wait without occupying executable capacity.

    A queued callback/manual request, real maturity deadline or unfinished
    identity work remains pressure. Technical failures also remain pressure.
    """
    from app.modules.acquisition.adaptive_frequency import payload
    from app.modules.acquisition.review_retention import waits_for_new_evidence

def waiting_for_evidence(row: Any, now: datetime) -> bool:
    """Completed observations can wait without occupying executable capacity.

    A queued callback/manual request, real maturity deadline or unfinished
    identity work remains pressure. Technical failures also remain pressure.
    """
    from app.modules.acquisition.adaptive_frequency import payload
    from app.modules.acquisition.review_retention import waits_for_new_evidence

    snapshot = payload(row.evidence_json)
    if (row.decision == "reject"
            and row.state == "completed"
            and not snapshot.get("collection_needs_refresh")
            and row.next_retry_at is None):
        # A completed reject with no retry timer schedules no future review
        # work, whatever produced the verdict: the AI fail-closed path, the
        # standard reject writeback, or a terminal restriction fact.  Only a
        # pending collection refresh keeps such a row in the capacity plan;
        # recovery paths (continuity restore, evidence events) create new
        # audits with their own pressure.  Requiring an ai_final marker here
        # left every non-AI terminal reject counting as pending review forever
        # and silently consumed join admission capacity.
        return True
    if row.decision != "observe":
        return False
    if (row.state in {"queued", "completed"}
            and snapshot.get("collection_needs_refresh") == "ordinary_ad_hint"
            and row.next_retry_at and row.next_retry_at > now):
        return True
    if row.state != "completed" or snapshot.get("collection_needs_refresh"):
        return False
    return waits_for_new_evidence({**snapshot, "decision": row.decision, "reason": row.reason}, now)


def stalled_review(row: Any, now: datetime) -> bool:
    return bool(
        getattr(row, "decision", None) == "technical_wait"
        and getattr(row, "reason", None)
        in {
            "telegram_read_budget",
            "AccountOperationLeaseBusy",
            "AccountOperationLeaseUnavailable",
            "account_unavailable",
        }
        and getattr(row, "created_at", None)
        and row.created_at <= now - timedelta(hours=24)
    )
