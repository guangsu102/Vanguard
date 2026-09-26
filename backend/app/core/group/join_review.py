"""Shared state helpers for the fixed post-join review lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.group.models import GroupAccountMembership


JOIN_REVIEW_INITIAL = "initial_pending"
JOIN_REVIEW_TWO_HOUR = "review_2h"
JOIN_REVIEW_FINAL = "final_pending"
JOIN_REVIEW_APPROVED = "approved"
JOIN_REVIEW_EXIT_PENDING = "exit_pending"
JOIN_REVIEW_LEAVE_FAILED = "leave_failed"
JOIN_REVIEW_MANUAL_REQUIRED = "manual_required"
JOIN_REVIEW_LEFT = "left"
JOIN_REVIEW_OWNED_EXCLUDED = "owned_group_excluded"

JOIN_REVIEW_TWO_HOUR_SECONDS = 2 * 60 * 60
JOIN_REVIEW_DEADLINE_SECONDS = 24 * 60 * 60
JOIN_LEAVE_RETRY_SECONDS = 2 * 60 * 60
JOIN_LEAVE_RETRY_WINDOW_SECONDS = 48 * 60 * 60


def exclude_owned_group_review(membership: GroupAccountMembership) -> None:
    """Keep owned memberships out of external promotion without claiming a leave."""
    membership.review_status = JOIN_REVIEW_OWNED_EXCLUDED
    membership.review_next_at = None
    membership.leave_retry_at = None
    membership.ad_status = "blocked"
    membership.warmup_status = "blocked"
    membership.probe_status = "skipped"


def reset_join_review(membership: GroupAccountMembership, now: datetime) -> None:
    """Start a new fixed review window after a new or renewed membership."""

    membership.review_status = JOIN_REVIEW_INITIAL
    membership.review_started_at = now
    membership.review_next_at = now + timedelta(seconds=JOIN_REVIEW_TWO_HOUR_SECONDS)
    membership.review_deadline_at = now + timedelta(seconds=JOIN_REVIEW_DEADLINE_SECONDS)
    membership.review_attempts = 0
    membership.leave_requested_at = None
    membership.leave_confirmed_at = None
    membership.leave_retry_at = None
    membership.leave_attempts = 0
    membership.leave_error = None


def approve_join_review(membership: GroupAccountMembership, now: datetime) -> None:
    """Record an explicit permission decision for an existing membership."""

    if membership.review_started_at is None:
        membership.review_started_at = membership.joined_at or now
    if membership.review_deadline_at is None:
        membership.review_deadline_at = membership.review_started_at + timedelta(
            seconds=JOIN_REVIEW_DEADLINE_SECONDS
        )
    membership.review_status = JOIN_REVIEW_APPROVED
    membership.review_next_at = None
    membership.leave_requested_at = None
    membership.leave_confirmed_at = None
    membership.leave_retry_at = None
    membership.leave_error = None
