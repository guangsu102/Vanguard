"""Small, side-effect free guards for acquisition task state transitions.

The acquisition workers have several different persistence models (qualification,
join approval and outbound delivery), but they all need the same invariants:
terminal rows must not be scheduled again and a logical operation must have one
stable idempotency key.  Keeping these rules in a dependency-free module makes
it possible for each worker to apply them without opening a database session or
performing another Telegram read.

This module deliberately does not perform I/O.  Callers should apply the
returned field updates in the transaction which owns the row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


# These names are shared by qualification, join and outbound reconciliation.
# A caller may add a local terminal state, but must never schedule one of these.
TERMINAL_STATUSES = frozenset(
    {
        "passed",
        "failed",
        "completed",
        "sent",
        "rejected",
        "excluded",
        "terminal_failed",
        "failed_reconciliation",
        "cancelled",
        "cancelled_inactive_account",
        "manual_review",
    }
)

RETRYABLE_STATUSES = frozenset(
    {"pending", "deferred_budget", "awaiting_external_approval"}
)


def is_terminal(status: str | None) -> bool:
    """Return whether *status* is a terminal state.

    Unknown statuses are intentionally treated as non-terminal so that a new
    state cannot silently skip its required transition/cleanup code.
    """

    return bool(status and status in TERMINAL_STATUSES)


def terminal_fields(status: str, *, reason: str | None = None) -> dict[str, Any]:
    """Build the fields that must be written whenever a row becomes terminal.

    In particular, ``next_retry_at`` is cleared.  Leaving that column set is
    the source of the ``completed + next_retry_at`` phantom queue entries.
    ``locked_until`` and ``lease_owner`` are included when present in a model;
    SQLAlchemy callers can safely filter the mapping to model columns.
    """

    if not is_terminal(status):
        raise ValueError(f"not a terminal acquisition status: {status!r}")
    fields: dict[str, Any] = {
        "status": status,
        "next_retry_at": None,
        "locked_until": None,
        "lease_owner": None,
    }
    if reason is not None:
        fields["failure_reason"] = reason
    return fields


def is_due(
    status: str | None,
    next_retry_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a row is eligible for a new scheduler pass.

    Only explicitly retryable states are due.  A missing retry timestamp is
    not interpreted as immediately due: this prevents an event replay from
    turning an in-flight row into an unbounded retry loop.
    """

    if status not in RETRYABLE_STATUSES or next_retry_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    retry_at = next_retry_at
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return retry_at <= current


def reuse_existing(
    existing: Mapping[str, Any] | None,
    *,
    incoming_operation_key: str,
) -> bool:
    """Return True when a caller must reuse a persisted result.

    A logical operation is never re-read/re-reviewed when an existing row owns
    the same operation key, regardless of its status.  New evidence versions
    must have a new operation key; a terminal row from an older evidence
    version must not suppress a different logical operation.  Lease recovery
    retains the operation key and reuses its recorded reads/results.
    """

    if not existing:
        return False
    return existing.get("operation_key") == incoming_operation_key


def reconciliation_allowed(
    attempt_count: int | None,
    *,
    max_attempts: int,
) -> bool:
    """Bound read-only reconciliation attempts without permitting resend."""

    if max_attempts < 1:
        return False
    return max(attempt_count or 0, 0) < max_attempts


def normalize_ai_outcome(value: object) -> str:
    """Map an AI qualification response to the two-state contract.

    The worker must not persist ``unknown`` (or any other third state): an
    absent, malformed, timed-out or ambiguous response is a failed review.
    This function does not call the model and therefore cannot trigger a
    second recognition pass.
    """

    if value is True:
        return "passed"
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"pass", "passed", "approve", "approved", "通过"}:
            return "passed"
    return "failed"

