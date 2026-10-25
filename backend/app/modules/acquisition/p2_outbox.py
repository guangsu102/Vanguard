"""P2 durable operation and Outbox service.

This module owns persistence and state transitions only.  Telegram calls are
performed by an account Actor supplied by :func:`dispatch_outbox_once`; the
service never creates a client or calls Telegram directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.account.control_models import AccountBudgetReservation, ReconciliationTask
from app.modules.acquisition.p2_models import (
    GrowthOperation,
    GrowthOperationAttempt,
    GrowthOperationEvent,
    GrowthOutbox,
)

LEASE_SECONDS = 300
MAX_ATTEMPTS = 8
ACTIVE_OPERATION_STATES = (
    "reserved",
    "sent",
    "receipt_pending",
    "confirmed",
    "survival_pending",
)
TERMINAL_OPERATION_STATES = frozenset({"resolved", "failed", "cancelled"})
OUTBOX_RETRYABLE_STATES = frozenset({"pending", "retryable_failed"})


class P2OperationError(RuntimeError):
    """Base error for P2 operation and Outbox failures."""


async def _record_duplicate_target(kind: str) -> None:
    """P4 telemetry for a target blocked by active/unknown/reconciliation work."""
    try:
        from app.core import metrics

        await metrics.record_duplicate_target(kind)
    except Exception:
        pass


async def _record_receipt_latency(started_at: datetime | None, confirmed_at: datetime | None) -> None:
    """P4 telemetry from the external send boundary to durable confirmation."""
    if started_at is None or confirmed_at is None:
        return
    try:
        from app.core import metrics

        await metrics.record_receipt_latency(started_at=started_at, confirmed_at=confirmed_at)
    except Exception:
        pass


class P2IdempotencyConflict(P2OperationError):
    """The same key was reused with a different immutable request."""


class P2TargetBlocked(P2OperationError):
    """A target already has an active or unresolved logical operation."""


class P2StateConflict(P2OperationError):
    """An optimistic state version or fencing token no longer matches."""


class P2InvalidTransition(P2OperationError):
    """A caller attempted a transition outside the operation state graph."""


class UnknownExternalOutcome(P2OperationError):
    """The Actor may have reached Telegram but could not return a receipt."""


class RetryableOutboxError(P2OperationError):
    """The Actor did not reach the external side-effect boundary."""


class PermanentOutboxError(P2OperationError):
    """The operation cannot succeed without a new business operation."""


@dataclass(frozen=True, slots=True)
class ActorResult:
    """Small, serializable contract returned by an account Actor."""

    outcome: str
    provider_request_id: str | None = None
    receipt: Mapping[str, Any] | None = None
    error: str | None = None
    survival_due_at: datetime | None = None
    actor_generation: str | None = None


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _same_request(operation: GrowthOperation, digest: str) -> bool:
    # Historical hashes included the reservation reference. Preserve their
    # idempotency while allowing a duplicate caller to supply a fresh budget.
    if operation.payload_hash == digest:
        return True
    try:
        envelope = json.loads(operation.payload_json)
        return payload_hash({key: value for key, value in envelope.items() if key != "budget_reservation_id"}) == digest
    except (ValueError, TypeError, AttributeError):
        return False


def derive_idempotency_key(
    *,
    kind: str,
    account_id: int,
    target_key: str,
    run_id: str | None,
    campaign_id: int | None = None,
    snapshot_id: str,
    group_version: str,
    rule_version: str,
    evidence_hash: str,
) -> str:
    """Build a stable logical key from the immutable operation identity."""

    identity = {
        "kind": kind,
        "account_id": int(account_id),
        "target_key": target_key,
        "run_id": run_id,
        "campaign_id": campaign_id,
        "snapshot_id": snapshot_id,
        "group_version": group_version,
        "rule_version": rule_version,
        "evidence_hash": evidence_hash,
    }
    digest = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()
    return f"p2:{kind}:{digest[:48]}"


def _validate_snapshot(
    *,
    snapshot_id: str,
    group_version: str,
    rule_version: str,
    evidence_hash: str,
) -> None:
    values = {
        "snapshot_id": snapshot_id,
        "group_version": group_version,
        "rule_version": rule_version,
        "evidence_hash": evidence_hash,
    }
    if any(not str(value or "").strip() for value in values.values()):
        raise ValueError("p2_snapshot_fields_required")
    if len(evidence_hash) != 64:
        raise ValueError("p2_evidence_hash_invalid")


def direct_scope_error(
    *,
    account_id: int,
    group_ids: list[int],
    campaign_id: int | None,
    run_id: str | None,
) -> str | None:
    """Return a direct-production scope violation, or ``None`` when allowed."""

    from app.core.config import settings

    if not settings.P2_DIRECT_SCOPE_ENABLED:
        return None
    if not group_ids:
        return "p2_direct_scope_group_blocked"
    for group_id in group_ids:
        allowed, reason = settings.p2_direct_scope_allows(
            account_id=int(account_id),
            group_id=int(group_id),
            campaign_id=campaign_id,
            run_id=run_id,
        )
        if allowed:
            return None
        if reason != "p2_direct_scope_group_blocked":
            return reason
    return "p2_direct_scope_group_blocked"


async def _validate_p3_snapshot(
    db: Any,
    *,
    snapshot_id: str,
    group_version: str,
    rule_version: str,
    evidence_hash: str,
    account_id: int,
    target_key: str,
    campaign_id: int | None,
    run_id: str | None,
    kind: str,
) -> None:
    """Flags control snapshot generation, never the validity of an enqueue."""
    from app.modules.acquisition.qualification_snapshot import (
        QualificationSnapshotEnvelope,
        validate_envelope,
    )

    snapshot = await validate_envelope(
        db,
        QualificationSnapshotEnvelope(
            snapshot_id=snapshot_id,
            group_version=group_version,
            rule_version=rule_version,
            evidence_hash=evidence_hash,
            account_id=int(account_id),
            target_key=target_key,
            campaign_id=campaign_id,
            run_id=run_id,
        ),
    )
    try:
        evidence = json.loads(snapshot.evidence_json or "{}")
    except (TypeError, ValueError):
        evidence = {}
    if kind == "send_ad" and isinstance(evidence, Mapping) and evidence.get("authorization_scope") == "join":
        from app.modules.acquisition.qualification_snapshot import QualificationSnapshotMismatch

        raise QualificationSnapshotMismatch("qualification_snapshot_scope_mismatch")


async def enqueue_operation(
    db: Any,
    *,
    kind: str,
    account_id: int,
    target_key: str,
    snapshot_id: str,
    group_version: str,
    rule_version: str,
    evidence_hash: str,
    payload: Mapping[str, Any],
    run_id: str | None = None,
    group_id: int | None = None,
    campaign_id: int | None = None,
    budget_reservation_id: str | None = None,
    idempotency_key: str | None = None,
    priority: int = 50,
    now: datetime | None = None,
) -> tuple[GrowthOperation, GrowthOutbox, bool]:
    """Create one operation and its Outbox row in the caller's transaction.

    The boolean return value is ``True`` when an existing operation was reused.
    No commit is performed so callers can atomically commit their domain row,
    budget reservation and P2 records together.
    """

    if kind not in {"join_group", "send_ad"}:
        raise ValueError("p2_operation_kind_invalid")
    if not target_key or len(target_key) > 500:
        raise ValueError("p2_target_key_invalid")
    if not budget_reservation_id:
        raise ValueError("p2_budget_reservation_required")
    # The direct-production release fence is enforced again at the durable
    # write boundary so an alternate caller cannot bypass producer filtering.
    # Payloads may carry both the internal Group id and the Telegram id; either
    # one is sufficient to identify an explicitly approved target.
    scope_group_ids: list[int] = []
    for value in (
        payload.get("group_db_id"),
        payload.get("group_id"),
        payload.get("telegram_group_id"),
    ):
        try:
            if value is not None:
                scope_group_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not scope_group_ids:
        try:
            scope_group_ids.append(int(target_key))
        except (TypeError, ValueError):
            pass
    scope_error = direct_scope_error(
        account_id=int(account_id), group_ids=scope_group_ids,
        campaign_id=campaign_id, run_id=run_id,
    )
    if scope_error:
        raise P2OperationError(scope_error)
    _validate_snapshot(
        snapshot_id=snapshot_id,
        group_version=group_version,
        rule_version=rule_version,
        evidence_hash=evidence_hash,
    )
    now = now or datetime.utcnow()
    key = idempotency_key or derive_idempotency_key(
        kind=kind,
        account_id=account_id,
        target_key=target_key,
        run_id=run_id,
        campaign_id=campaign_id,
        snapshot_id=snapshot_id,
        group_version=group_version,
        rule_version=rule_version,
        evidence_hash=evidence_hash,
    )
    envelope = {
        "schema_version": 1,
        "kind": kind,
        "account_id": int(account_id),
        "target_key": target_key,
        "group_id": group_id,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "group_version": group_version,
        "rule_version": rule_version,
        "evidence_hash": evidence_hash,
        "budget_reservation_id": budget_reservation_id,
        "payload": dict(payload),
    }
    encoded = _canonical(envelope)
    digest = payload_hash({key: value for key, value in envelope.items() if key != "budget_reservation_id"})

    existing = await db.scalar(
        select(GrowthOperation)
        .where(GrowthOperation.idempotency_key == key)
        .with_for_update()
    )
    if existing is not None:
        if not _same_request(existing, digest):
            raise P2IdempotencyConflict("p2_idempotency_payload_mismatch")
        outbox = await db.scalar(
            select(GrowthOutbox).where(GrowthOutbox.operation_id == existing.id).with_for_update()
        )
        if outbox is None:
            outbox = GrowthOutbox(
                operation_id=existing.id,
                kind=existing.kind,
                account_id=existing.account_id,
                priority=priority,
                payload_version=1,
                state="succeeded" if existing.state in TERMINAL_OPERATION_STATES else "pending",
                payload_json=existing.payload_json,
                available_at=now,
                completed_at=now if existing.state in TERMINAL_OPERATION_STATES else None,
            )
            db.add(outbox)
            await db.flush()
        if outbox is None:
            raise P2OperationError("p2_operation_outbox_missing")
        return existing, outbox, True

    await _validate_p3_snapshot(
        db, snapshot_id=snapshot_id, group_version=group_version,
        rule_version=rule_version, evidence_hash=evidence_hash,
        account_id=account_id, target_key=target_key, campaign_id=campaign_id, run_id=run_id,
        kind=kind,
    )
    reservation = await db.get(AccountBudgetReservation, budget_reservation_id, with_for_update=True)
    if (reservation is None or reservation.account_id != account_id
            or reservation.state != "reserved" or reservation.lease_expires_at <= now):
        raise P2OperationError("p2_budget_reservation_invalid")

    from app.modules.acquisition.p4_models import GrowthOperationArchive
    archived_identity = await db.scalar(select(GrowthOperationArchive.operation_id).where(
        GrowthOperationArchive.idempotency_key == key
    ).limit(1))
    if archived_identity is not None:
        raise P2TargetBlocked(f"p2_archived_identity_requires_recovery:{archived_identity}")

    active_target = await db.scalar(
        select(GrowthOperation)
        .where(
            GrowthOperation.account_id == account_id,
            GrowthOperation.target_key == target_key,
            or_(
                GrowthOperation.state.in_(ACTIVE_OPERATION_STATES),
                GrowthOperation.reconciliation_required.is_(True),
            ),
        )
        .with_for_update()
    )
    if active_target is not None:
        await _record_duplicate_target(kind)
        raise P2TargetBlocked("p2_target_reconciliation_required")

    operation = GrowthOperation(
        kind=kind,
        idempotency_key=key,
        account_id=account_id,
        group_id=group_id,
        campaign_id=campaign_id,
        target_key=target_key,
        run_id=run_id,
        snapshot_id=snapshot_id,
        group_version=group_version,
        rule_version=rule_version,
        evidence_hash=evidence_hash,
        budget_reservation_id=budget_reservation_id,
        payload_json=encoded,
        payload_hash=digest,
        state="reserved",
        reserved_at=now,
        created_at=now,
        updated_at=now,
    )
    outbox = GrowthOutbox(
        kind=kind,
        account_id=account_id,
        priority=int(priority),
        payload_version=1,
        state="pending",
        available_at=now,
        payload_json=encoded,
        created_at=now,
    )
    operation.outbox = outbox
    try:
        async with db.begin_nested():
            # begin_nested() flushes existing changes before establishing its
            # savepoint. Add the contender only after the savepoint exists.
            db.add(operation)
            await db.flush()
    except IntegrityError as exc:
        # A concurrent request may win either the idempotency key or target
        # partial index. The caller's outer transaction remains usable.
        existing = await db.scalar(
            select(GrowthOperation)
            .where(GrowthOperation.idempotency_key == key)
            .with_for_update()
        )
        if existing is not None:
            if not _same_request(existing, digest):
                raise P2IdempotencyConflict("p2_idempotency_payload_mismatch") from exc
            existing_outbox = await db.scalar(
                select(GrowthOutbox).where(GrowthOutbox.operation_id == existing.id)
            )
            if existing_outbox is None:
                raise P2OperationError("p2_operation_outbox_missing") from exc
            return existing, existing_outbox, True
        await _record_duplicate_target(kind)
        raise P2TargetBlocked("p2_target_active_conflict") from exc
    db.add(
        GrowthOperationEvent(
            operation_id=operation.id,
            event_type="reserved",
            from_state=None,
            to_state="reserved",
            payload_json=_canonical(
                {
                    "snapshot_id": snapshot_id,
                    "group_version": group_version,
                    "rule_version": rule_version,
                    "evidence_hash": evidence_hash,
                }
            ),
            created_at=now,
        )
    )
    await db.flush()
    return operation, outbox, False


_TRANSITIONS: dict[str, set[str]] = {
    "reserved": {"sent", "failed", "cancelled"},
    "sent": {"receipt_pending", "confirmed", "failed"},
    "receipt_pending": {"confirmed", "failed"},
    "confirmed": {"survival_pending", "resolved", "failed"},
    "survival_pending": {"resolved", "failed"},
    "resolved": set(),
    "failed": set(),
    "cancelled": set(),
}


def _check_transition(operation: GrowthOperation, target: str) -> None:
    if target not in _TRANSITIONS:
        raise P2InvalidTransition("p2_state_invalid")
    if target == operation.state:
        return
    if target == "sent" and operation.attempt_count < 1:
        raise P2InvalidTransition("p2_sent_attempt_required")
    if target not in _TRANSITIONS.get(operation.state, set()):
        raise P2InvalidTransition(f"p2_transition_{operation.state}_to_{target}")
    if operation.kind == "send_ad" and target == "resolved" and operation.state != "survival_pending":
        raise P2InvalidTransition("p2_ad_survival_required")
    if operation.kind == "join_group" and target == "survival_pending":
        raise P2InvalidTransition("p2_join_survival_invalid")


async def transition_operation(
    db: Any,
    operation_id: int,
    target_state: str,
    *,
    expected_version: int | None = None,
    event_type: str = "state_transition",
    provider_request_id: str | None = None,
    provider_receipt: Mapping[str, Any] | None = None,
    survival_due_at: datetime | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> GrowthOperation:
    """Apply one checked state transition and append its audit event."""

    now = now or datetime.utcnow()
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation_id).with_for_update()
    )
    if operation is None:
        raise P2OperationError("p2_operation_missing")
    if expected_version is not None and operation.state_version != expected_version:
        raise P2StateConflict("p2_state_version_mismatch")
    _check_transition(operation, target_state)
    if target_state == operation.state:
        return operation
    if target_state == "confirmed" and not (provider_receipt or operation.provider_receipt_json):
        raise P2InvalidTransition("p2_confirmation_receipt_required")
    if target_state in TERMINAL_OPERATION_STATES and operation.reconciliation_required:
        raise P2InvalidTransition("p2_reconciliation_required")
    if target_state in {"confirmed", "resolved"} and not (
        provider_receipt or operation.provider_receipt_json
    ):
        raise P2InvalidTransition("p2_terminal_evidence_required")

    previous = operation.state
    operation.state = target_state
    operation.state_version += 1
    operation.updated_at = now
    if provider_request_id:
        operation.provider_request_id = provider_request_id
    if provider_receipt is not None:
        operation.provider_receipt_json = _canonical(dict(provider_receipt))
    if target_state == "sent":
        operation.sent_at = operation.sent_at or now
    if target_state == "confirmed":
        operation.confirmed_at = operation.confirmed_at or now
    if target_state == "survival_pending":
        operation.survival_due_at = survival_due_at
    if target_state == "resolved":
        operation.resolved_at = operation.resolved_at or now
        operation.reconciliation_required = False
    if error:
        operation.last_error = error[:500]
    db.add(
        GrowthOperationEvent(
            operation_id=operation.id,
            event_type=event_type,
            from_state=previous,
            to_state=target_state,
            payload_json=_canonical(
                {
                    "provider_request_id": provider_request_id,
                    "has_receipt": bool(provider_receipt or operation.provider_receipt_json),
                    "error": error,
                }
            ),
            created_at=now,
        )
    )
    await db.flush()
    return operation


async def record_attempt(
    db: Any,
    operation_id: int,
    *,
    actor_generation: str | None = None,
    provider_request_id: str | None = None,
    now: datetime | None = None,
) -> GrowthOperationAttempt:
    now = now or datetime.utcnow()
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation_id).with_for_update()
    )
    if operation is None:
        raise P2OperationError("p2_operation_missing")
    if operation.state in TERMINAL_OPERATION_STATES:
        raise P2StateConflict("p2_operation_terminal")
    operation.attempt_count += 1
    operation.state_version += 1
    operation.updated_at = now
    attempt = GrowthOperationAttempt(
        operation_id=operation_id,
        attempt_no=operation.attempt_count,
        actor_generation=actor_generation,
        provider_request_id=provider_request_id,
        started_at=now,
    )
    db.add(attempt)
    db.add(
        GrowthOperationEvent(
            operation_id=operation.id,
            event_type="attempt_started",
            from_state=operation.state,
            to_state=operation.state,
            payload_json=_canonical({"attempt_no": attempt.attempt_no}),
            created_at=now,
        )
    )
    await db.flush()
    return attempt


async def mark_attempt(
    db: Any,
    attempt_id: int,
    *,
    outcome: str,
    receipt: Mapping[str, Any] | None = None,
    error: str | None = None,
    provider_request_id: str | None = None,
    now: datetime | None = None,
) -> GrowthOperationAttempt:
    now = now or datetime.utcnow()
    attempt = await db.scalar(
        select(GrowthOperationAttempt)
        .where(GrowthOperationAttempt.id == attempt_id)
        .with_for_update()
    )
    if attempt is None:
        raise P2OperationError("p2_attempt_missing")
    if attempt.finished_at is not None:
        raise P2StateConflict("p2_attempt_already_finished")
    attempt.outcome = outcome[:32]
    attempt.receipt_json = _canonical(dict(receipt)) if receipt is not None else attempt.receipt_json
    attempt.error = error[:500] if error else None
    attempt.provider_request_id = provider_request_id or attempt.provider_request_id
    attempt.finished_at = now
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == attempt.operation_id).with_for_update()
    )
    if operation is not None:
        db.add(
            GrowthOperationEvent(
                operation_id=attempt.operation_id,
                event_type="attempt_finished",
                from_state=operation.state,
                to_state=operation.state,
                payload_json=_canonical(
                    {
                        "attempt_id": attempt.id,
                        "outcome": outcome,
                        "has_receipt": receipt is not None,
                        "error": error,
                    }
                ),
                created_at=now,
            )
        )
    await db.flush()
    return attempt


def reconciliation_task_key(operation_id: int) -> str:
    return f"growth:{int(operation_id)}:v1"


async def mark_reconciliation_required(
    db: Any,
    operation_id: int,
    *,
    reason: str,
    now: datetime | None = None,
    priority: int = 0,
) -> tuple[GrowthOperation, ReconciliationTask]:
    now = now or datetime.utcnow()
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation_id).with_for_update()
    )
    if operation is None:
        raise P2OperationError("p2_operation_missing")
    if operation.state in TERMINAL_OPERATION_STATES:
        raise P2StateConflict("p2_terminal_reconciliation")
    operation.reconciliation_required = True
    # Reconciliation is an operational state change even when the business
    # state remains unchanged.  Advancing the same optimistic version keeps
    # database trigger checks and stale workers from silently overwriting the
    # protection bit.
    operation.state_version += 1
    operation.last_error = reason[:500]
    operation.updated_at = now
    task = await db.scalar(
        select(ReconciliationTask)
        .where(ReconciliationTask.task_key == reconciliation_task_key(operation_id))
        .with_for_update()
    )
    if task is None:
        task = ReconciliationTask(
            task_key=reconciliation_task_key(operation_id),
            kind="growth",
            source_id=operation.id,
            account_id=operation.account_id,
            priority=int(priority),
            state="pending",
            available_at=now,
            verification_due_at=now,
            reason=reason[:500],
            first_discovered_at=now,
            age_threshold_seconds=3600,
        )
        db.add(task)
    else:
        # A live verifier owns its task lease until finish_task; do not revoke
        # it merely because another observer found the same uncertainty.
        if task.state != "leased":
            task.state = "pending"
            task.lease_owner = None
            task.lease_expires_at = None
        task.available_at = now
        task.finished_at = None
        task.last_error_code = reason[:100]
        task.published_at = None
        task.reason = reason[:500]
        task.first_discovered_at = task.first_discovered_at or now
        task.age_threshold_seconds = task.age_threshold_seconds or 3600
        task.result_json = None
    db.add(
        GrowthOperationEvent(
            operation_id=operation.id,
            event_type="reconciliation_required",
            from_state=operation.state,
            to_state=operation.state,
            payload_json=_canonical({"reason": reason}),
            created_at=now,
        )
    )
    await db.flush()
    return operation, task


async def clear_reconciliation(
    db: Any,
    operation_id: int,
    *,
    outcome: str,
    receipt: Mapping[str, Any] | None = None,
    complete_task: bool = True,
    now: datetime | None = None,
) -> GrowthOperation:
    if outcome not in {"confirmed", "failed"}:
        raise ValueError("p2_reconciliation_outcome_invalid")
    now = now or datetime.utcnow()
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation_id).with_for_update()
    )
    if operation is None:
        raise P2OperationError("p2_operation_missing")
    if not operation.reconciliation_required:
        return operation
    operation.reconciliation_required = False
    operation.state_version += 1
    operation.provider_receipt_json = (
        _canonical(dict(receipt)) if receipt is not None else operation.provider_receipt_json
    )
    operation.updated_at = now
    task = await db.scalar(
        select(ReconciliationTask)
        .where(ReconciliationTask.task_key == reconciliation_task_key(operation_id))
        .with_for_update()
    )
    if task is not None and complete_task:
        task.state = "succeeded"
        task.finished_at = now
        task.lease_owner = None
        task.lease_expires_at = None
    db.add(
        GrowthOperationEvent(
            operation_id=operation.id,
            event_type="reconciliation_cleared",
            from_state=operation.state,
            to_state=operation.state,
            payload_json=_canonical({"outcome": outcome, "has_receipt": receipt is not None}),
            created_at=now,
        )
    )
    await db.flush()
    return operation


@dataclass(frozen=True, slots=True)
class GrowthVerification:
    """Result of a read-only Telegram fact lookup.

    A verifier may return ``pending`` when Telegram has not exposed a
    definitive fact yet.  It must never return a command to send/join again;
    reconciliation is deliberately a read-only lane.
    """

    outcome: str
    receipt: Mapping[str, Any] | None = None
    evidence: Mapping[str, Any] | None = None
    error: str | None = None
    survival_due_at: datetime | None = None


def _operation_payload(operation: GrowthOperation) -> dict[str, Any]:
    try:
        envelope = json.loads(operation.payload_json or "{}")
    except (TypeError, ValueError):
        return {}
    payload = envelope.get("payload") if isinstance(envelope, dict) else {}
    return payload if isinstance(payload, dict) else {}


async def read_only_growth_verifier(
    operation: GrowthOperation,
    *,
    account_pool: Any,
) -> GrowthVerification:
    """Inspect Telegram facts without invoking a write API.

    The account Actor owns the connected client.  This adapter only uses
    ``get_entity``, ``get_permissions`` and ``get_messages`` and returns
    ``pending`` when the Actor/session is unavailable.  Deployments may inject
    a stricter verifier with the same contract into ``reconcile_growth``.
    """

    payload = _operation_payload(operation)
    target: Any = payload.get("target") or payload.get("target_key") or operation.target_key
    # Reconciliation is a read-only, force-refresh lane.  It still goes
    # through the account Actor boundary so a worker cannot bypass the
    # account lease and call a Telegram client directly.
    from app.core.account.read_cache import (
        FeatureFlags,
        TelegramReadCache,
        make_account_actor_reader,
    )

    read_cache = TelegramReadCache(
        make_account_actor_reader(account_pool),
        flags=FeatureFlags.from_env(),
    )
    try:
        entity_result = await read_cache.resolve_entity(
            int(operation.account_id), target, force_refresh=True
        )
        if entity_result.status != "fresh" or entity_result.data is None:
            return GrowthVerification("pending", error=entity_result.error or "growth_target_read_failed")
    except Exception as exc:
        return GrowthVerification("pending", error=f"growth_target_read_failed:{type(exc).__name__}")

    if operation.kind == "join_group":
        try:
            membership_result = await read_cache.get_membership(
                int(operation.account_id), target, force_refresh=True
            )
            if membership_result.status != "fresh":
                return GrowthVerification(
                    "pending", error=membership_result.error or "growth_membership_read_unsupported"
                )
            permission = membership_result.data
            if permission is None or not hasattr(permission, "has_left"):
                return GrowthVerification("pending", error="growth_membership_evidence_missing")
            if bool(getattr(permission, "has_left", False)):
                # A pending join request can legitimately have no member yet;
                # absence is therefore not a safe failure verdict.
                return GrowthVerification("pending", error="growth_join_not_confirmed")
            receipt = {
                "kind": "membership",
                "target": operation.target_key,
                "confirmed_at": datetime.utcnow().isoformat(),
            }
            return GrowthVerification("confirmed", receipt=receipt, evidence=receipt)
        except Exception as exc:
            return GrowthVerification("pending", error=f"growth_membership_read_failed:{type(exc).__name__}")

    receipt: dict[str, Any] = {}
    try:
        if operation.provider_receipt_json:
            decoded = json.loads(operation.provider_receipt_json)
            if isinstance(decoded, dict):
                receipt.update(decoded)
    except (TypeError, ValueError):
        pass
    raw_message_id = (
        receipt.get("message_id")
        or receipt.get("telegram_message_id")
        or payload.get("message_id")
    )
    try:
        message_id = int(raw_message_id)
    except (TypeError, ValueError):
        return GrowthVerification("pending", error="growth_ad_receipt_message_id_missing")
    try:
        survival_result = await read_cache.get_ad_survival(
            int(operation.account_id), target, message_id, force_refresh=True
        )
        if survival_result.status != "fresh":
            return GrowthVerification(
                "pending", error=survival_result.error or "growth_ad_message_read_failed"
            )
        message = survival_result.data
    except Exception as exc:
        return GrowthVerification("pending", error=f"growth_ad_message_read_failed:{type(exc).__name__}")
    if message is None:
        return GrowthVerification("pending", error="growth_ad_message_not_visible")
    evidence = {
        **receipt,
        "message_id": message_id,
        "target": operation.target_key,
        "observed_at": datetime.utcnow().isoformat(),
    }
    if bool(payload.get("survived") or receipt.get("survived")) or (
        operation.survival_due_at is not None
        and operation.survival_due_at <= datetime.utcnow()
    ):
        return GrowthVerification("survived", receipt=evidence, evidence=evidence)
    due = operation.survival_due_at or (datetime.utcnow() + timedelta(hours=24))
    return GrowthVerification("confirmed", receipt=evidence, evidence=evidence, survival_due_at=due)


def _active_dispatch_lease(outbox: GrowthOutbox | None, now: datetime) -> bool:
    return bool(
        outbox is not None
        and outbox.state == "leased"
        and outbox.lease_expires_at is not None
        and outbox.lease_expires_at > now
    )


async def _finish_reconciled_outbox(
    db: Any,
    operation: GrowthOperation,
    outbox: GrowthOutbox | None,
    *,
    state: str,
    now: datetime,
    error: str | None = None,
) -> None:
    """Close a locked dispatch row in the same transaction as its evidence."""
    if outbox is None or outbox.state in {"succeeded", "dead", "cancelled"}:
        # A later survival failure must not rewrite a successful send receipt.
        return
    previous = outbox.state
    outbox.state = state
    outbox.last_error = error[:500] if error else None
    outbox.provider_request_id = operation.provider_request_id or outbox.provider_request_id
    outbox.lease_owner = outbox.lease_expires_at = outbox.fencing_token = None
    outbox.completed_at = now
    db.add(GrowthOperationEvent(
        operation_id=operation.id,
        event_type="outbox_reconciled",
        from_state=operation.state,
        to_state=operation.state,
        payload_json=_canonical({"outbox_id": outbox.id, "from_state": previous, "to_state": state}),
        created_at=now,
    ))
    if operation.budget_reservation_id and state in {"succeeded", "dead", "cancelled"}:
        from app.core.account.budget_ledger import settle
        reservation = await db.get(
            AccountBudgetReservation,
            operation.budget_reservation_id,
            populate_existing=True,
        )
        consumed_reads = 1 if reservation is not None and reservation.reserved_reads >= 1 else 0
        await settle(
            db,
            operation.budget_reservation_id,
            consumed_reads if state == "succeeded" else 0,
            state="settled" if state == "succeeded" else "released",
            reason=f"p2_reconciliation_{state}",
            now=now,
        )
    await db.flush()


async def reconcile_growth_operation(
    db: Any,
    operation_id: int,
    *,
    verifier: Callable[[GrowthOperation], Awaitable[GrowthVerification | Mapping[str, Any]]],
    now: datetime | None = None,
    max_age_seconds: int | None = None,
) -> dict[str, Any]:
    """Resolve one P2 reconciliation task using a read-only verifier.

    The function records the verification attempt and evidence, then advances
    the same optimistic state machine used by the Outbox callback.  It never
    calls an external write method and never makes an unknown result retryable.
    """

    now = now or datetime.utcnow()
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation_id)
    )
    if operation is None:
        return {"state": "cancelled", "error": "p2_operation_missing"}
    if operation.state in TERMINAL_OPERATION_STATES and not operation.reconciliation_required:
        return {"state": "succeeded", "observed": True}
    if not operation.reconciliation_required and operation.state != "survival_pending":
        return {"state": "succeeded", "observed": False}

    outbox_query = select(GrowthOutbox).where(GrowthOutbox.operation_id == operation_id)
    outbox = await db.scalar(outbox_query.execution_options(populate_existing=True))
    if _active_dispatch_lease(outbox, now):
        return {"state": "pending", "error": "p2_dispatch_in_progress", "retry_after": 60}
    expected_version = operation.state_version
    task = await db.scalar(
        select(ReconciliationTask).where(
            ReconciliationTask.task_key == reconciliation_task_key(operation_id)
        )
    )
    age_start = (task.first_discovered_at or task.created_at) if task is not None else operation.created_at
    if operation.state == "survival_pending" and operation.survival_due_at:
        age_start = max(age_start, operation.survival_due_at)
        if now < operation.survival_due_at:
            return {"state": "pending", "retry_after": max(1, int((operation.survival_due_at - now).total_seconds()))}
    try:
        verification = await verifier(operation)
    except Exception as exc:
        verification = GrowthVerification(
            "pending", error=f"growth_verifier_failed:{type(exc).__name__}"
        )
    if not isinstance(verification, GrowthVerification):
        verification = GrowthVerification(**dict(verification))

    # Do not hold dispatch locks across the provider read. Lock Outbox before
    # operation, as dispatch/recovery do, and reject facts for a changed state.
    outbox = await db.scalar(outbox_query.with_for_update().execution_options(populate_existing=True))
    operation = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if operation is None:
        return {"state": "cancelled", "error": "p2_operation_missing"}
    if _active_dispatch_lease(outbox, now):
        return {"state": "pending", "error": "p2_dispatch_in_progress", "retry_after": 60}
    if operation.state_version != expected_version:
        return {"state": "pending", "error": "p2_reconciliation_state_changed", "retry_after": 60}

    attempt = await record_attempt(
        db,
        operation.id,
        actor_generation="reconciliation-read",
        now=now,
    )
    evidence = verification.evidence or verification.receipt
    if task is not None:
        task.evidence_json = _canonical(dict(evidence)) if evidence else None
        if verification.error:
            task.reason = verification.error[:500]
    await mark_attempt(
        db,
        attempt.id,
        outcome=verification.outcome,
        receipt=evidence,
        error=verification.error,
        now=now,
    )

    outcome = verification.outcome
    if outcome in {"pending", "unknown"}:
        age = max(0, int((now - age_start).total_seconds()))
        threshold = max_age_seconds or (task.age_threshold_seconds if task is not None else None) or 3600
        if age >= max(1, int(threshold)):
            if task is not None:
                task.result_json = _canonical({"outcome": "manual_required", "age_seconds": age})
            return {
                "state": "manual_required",
                "error": verification.error or "growth_reconciliation_age_threshold",
                "observed": False,
            }
        return {
            "state": "pending",
            "error": verification.error,
            "retry_after": 60,
            "observed": False,
        }

    receipt = verification.receipt or verification.evidence
    if not receipt:
        return {"state": "pending", "error": "growth_verification_evidence_missing", "retry_after": 60}
    if outcome == "failed":
        await clear_reconciliation(
            db,
            operation.id,
            outcome="failed",
            receipt=receipt,
            complete_task=False,
            now=now,
        )
        await transition_operation(
            db,
            operation.id,
            "failed",
            provider_receipt=receipt,
            error=verification.error or "growth_external_failure",
            now=now,
        )
        await _finish_reconciled_outbox(
            db, operation, outbox, state="dead", now=now,
            error=verification.error or "growth_external_failure",
        )
        if task is not None:
            task.result_json = _canonical({"outcome": "failed"})
        return {"state": "succeeded", "observed": True}

    if outcome not in {"confirmed", "survived"}:
        return {"state": "manual_required", "error": "growth_verification_outcome_invalid"}

    await clear_reconciliation(
        db,
        operation.id,
        outcome="confirmed",
        receipt=receipt,
        complete_task=False,
        now=now,
    )
    current = await db.scalar(
        select(GrowthOperation).where(GrowthOperation.id == operation.id).with_for_update()
    )
    if current is None:
        return {"state": "cancelled", "error": "p2_operation_missing"}
    if current.state == "reserved":
        await transition_operation(db, current.id, "sent", now=now)
        current = await db.scalar(select(GrowthOperation).where(GrowthOperation.id == current.id))
    if current.state == "sent":
        await transition_operation(db, current.id, "receipt_pending", now=now)
        current = await db.scalar(select(GrowthOperation).where(GrowthOperation.id == current.id))
    if current.state == "receipt_pending":
        await transition_operation(
            db, current.id, "confirmed", provider_receipt=receipt, now=now
        )
        current = await db.scalar(select(GrowthOperation).where(GrowthOperation.id == current.id))
        await _record_receipt_latency(current.sent_at or age_start, now)

    await _finish_reconciled_outbox(db, current, outbox, state="succeeded", now=now)
    if current.kind == "join_group":
        # Keep the legacy join budget projection aligned when a pending or
        # crashed Actor result is confirmed by the read-only reconciliation
        # lane.  The reservation is already settled by the Outbox helper;
        # this only closes the attempt's local state.
        try:
            payload = _operation_payload(current)
            attempt_id = payload.get("join_attempt_id")
            if attempt_id is not None:
                from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus

                attempt_row = await db.get(
                    AutoJoinAttempt, int(attempt_id), populate_existing=True
                )
                if attempt_row is not None and attempt_row.account_id == current.account_id:
                    attempt_row.request_state = "sent"
                    attempt_row.telegram_action_attempted = True
                    attempt_row.request_sent_at = attempt_row.request_sent_at or now
                    attempt_row.reservation_expires_at = None
                    attempt_row.status = DeliveryStatus.SUCCESS.value
                    attempt_row.joined_at = attempt_row.joined_at or now
                    attempt_row.reconciliation_status = "confirmed"
                    attempt_row.reconciliation_checked_at = now
                    attempt_row.reconciliation_next_at = None
                    attempt_row.reason = "p2_reconciliation_confirmed"
            from app.modules.acquisition.p2_dispatch import _legacy_join_projection

            await _legacy_join_projection(db, current, payload)
        except (TypeError, ValueError):
            # The durable operation remains authoritative even if an old
            # migration row has no legacy attempt projection.
            pass
        if current.state == "confirmed":
            await transition_operation(
                db, current.id, "resolved", provider_receipt=receipt, now=now
            )
        if task is not None:
            task.result_json = _canonical({"outcome": "resolved"})
        return {"state": "succeeded", "observed": True}

    if current.state == "confirmed":
        due = verification.survival_due_at or current.survival_due_at or now + timedelta(hours=24)
        await transition_operation(
            db,
            current.id,
            "survival_pending",
            provider_receipt=receipt,
            survival_due_at=due,
            now=now,
        )
        current = await db.scalar(select(GrowthOperation).where(GrowthOperation.id == current.id))
    if outcome == "survived" and current.state == "survival_pending":
        await transition_operation(
            db, current.id, "resolved", provider_receipt=receipt, now=now
        )
        if task is not None:
            task.result_json = _canonical({"outcome": "resolved"})
        return {"state": "succeeded", "observed": True}
    due = current.survival_due_at or now + timedelta(hours=24)
    return {
        "state": "pending",
        "retry_after": max(1, int((due - now).total_seconds())),
        "next_verification_at": due.isoformat(),
        "observed": True,
    }


async def recover_expired_outbox(
    db: Any, *, now: datetime | None = None, account_id: int | None = None, limit: int = 100
) -> int:
    """A crashed dispatcher may already have sent; expire into verification."""
    now = now or datetime.utcnow()
    query = select(GrowthOutbox).where(
        GrowthOutbox.state == "leased", GrowthOutbox.lease_expires_at <= now
    ).order_by(GrowthOutbox.lease_expires_at, GrowthOutbox.id).limit(limit).with_for_update(skip_locked=True)
    if account_id is not None:
        query = query.where(GrowthOutbox.account_id == account_id)
    rows = (await db.scalars(query.execution_options(populate_existing=True))).all()
    for row in rows:
        row.state = "reconciliation_required"
        row.lease_owner = row.fencing_token = row.lease_expires_at = None
        row.last_error = "p2_dispatch_lease_expired"
        await mark_reconciliation_required(db, row.operation_id, reason=row.last_error, now=now)
    await db.flush()
    return len(rows)


async def claim_outbox(
    db: Any,
    *,
    owner: str,
    fencing_token: str,
    account_id: int | None = None,
    lease_seconds: int = LEASE_SECONDS,
    now: datetime | None = None,
    commit: bool = True,
) -> GrowthOutbox | None:
    """Claim one ready Outbox row using a PostgreSQL-safe row lock."""

    now = now or datetime.utcnow()
    if not owner or not fencing_token:
        raise P2StateConflict("p2_dispatch_fence_required")
    recovered = await recover_expired_outbox(db, now=now, account_id=account_id)
    ready = and_(GrowthOutbox.state.in_(OUTBOX_RETRYABLE_STATES), GrowthOutbox.available_at <= now)
    query = (
        select(GrowthOutbox)
        .join(GrowthOperation, GrowthOperation.id == GrowthOutbox.operation_id)
        .where(ready)
        .where(GrowthOperation.reconciliation_required.is_(False))
        .where(GrowthOperation.state == "reserved")
        .order_by(GrowthOutbox.priority, GrowthOutbox.available_at, GrowthOutbox.id)
        .with_for_update(skip_locked=True)
    )
    if account_id is not None:
        query = query.where(GrowthOutbox.account_id == account_id)
    row = await db.scalar(query.limit(1).execution_options(populate_existing=True))
    if row is None:
        if commit and recovered:
            await db.commit()
        return None
    row.state = "leased"
    row.lease_owner = owner
    row.fencing_token = fencing_token
    row.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
    row.attempts += 1
    if commit:
        await db.commit()
    else:
        await db.flush()
    return row


async def _owned_outbox(db: Any, outbox_id: int, owner: str, fencing_token: str, now: datetime) -> GrowthOutbox:
    row = await db.scalar(select(GrowthOutbox).where(GrowthOutbox.id == outbox_id)
                          .with_for_update().execution_options(populate_existing=True))
    if (row is None or row.state != "leased" or row.lease_owner != owner
            or row.fencing_token != fencing_token or row.lease_expires_at is None
            or row.lease_expires_at <= now):
        raise P2StateConflict("p2_outbox_lease_lost")
    return row


async def finish_outbox(
    db: Any,
    outbox_id: int,
    *,
    owner: str,
    fencing_token: str,
    state: str,
    error: str | None = None,
    retry_after: int = 60,
    provider_request_id: str | None = None,
    commit: bool = True,
    now: datetime | None = None,
) -> GrowthOutbox:
    if state not in {"succeeded", "retryable_failed", "reconciliation_required", "dead", "cancelled"}:
        raise ValueError("p2_outbox_state_invalid")
    now = now or datetime.utcnow()
    row = await _owned_outbox(db, outbox_id, owner, fencing_token, now)
    if state == "retryable_failed" and row.attempts >= MAX_ATTEMPTS:
        state = "dead"
        error = error or "p2_outbox_max_attempts"
    row.state = state
    row.last_error = error[:500] if error else None
    row.provider_request_id = provider_request_id or row.provider_request_id
    row.lease_owner = None
    row.lease_expires_at = None
    row.fencing_token = None
    if state in {"succeeded", "dead", "cancelled"}:
        row.completed_at = now
    else:
        row.available_at = now + timedelta(seconds=max(1, retry_after))
    if state == "reconciliation_required":
        await mark_reconciliation_required(db, row.operation_id, reason=error or "external_outcome_unknown", now=now)
    elif state in {"succeeded", "dead", "cancelled"}:
        operation = await db.scalar(
            select(GrowthOperation).where(GrowthOperation.id == row.operation_id).with_for_update()
        )
        if operation is not None:
            if state == "dead" and operation.state not in TERMINAL_OPERATION_STATES:
                await transition_operation(
                    db,
                    operation.id,
                    "failed",
                    event_type="outbox_dead",
                    error=error or "outbox_dead",
                    now=now,
                )
            # A durable Outbox terminal result closes the corresponding P0
            # budget reservation in the same transaction. Unknown results
            # intentionally remain reserved until reconciliation confirms a
            # provider fact, preventing a duplicate dispatch from reusing it.
            if operation.budget_reservation_id:
                from app.core.account.budget_ledger import settle
                reservation = await db.get(
                    AccountBudgetReservation,
                    operation.budget_reservation_id,
                    populate_existing=True,
                )
                consumed_reads = 1 if reservation is not None and reservation.reserved_reads >= 1 else 0
                await settle(
                    db,
                    operation.budget_reservation_id,
                    consumed_reads if state == "succeeded" else 0,
                    state="settled" if state == "succeeded" else "released",
                    reason=f"p2_outbox_{state}",
                    now=now,
                )
    if commit:
        await db.commit()
    else:
        await db.flush()
    return row


async def dispatch_outbox_once(
    db: Any,
    *,
    owner: str,
    fencing_token: str,
    actor_submitter: Callable[[GrowthOperation, Mapping[str, Any], str], Awaitable[ActorResult]],
    account_id: int | None = None,
    now: datetime | None = None,
) -> GrowthOutbox | None:
    """Dispatch one row through the supplied account Actor.

    The callback is the only external execution boundary. Unknown outcomes
    become durable reconciliation tasks and are never replayed automatically.
    """

    now = now or datetime.utcnow()
    row = await claim_outbox(
        db,
        owner=owner,
        fencing_token=fencing_token,
        account_id=account_id,
        now=now,
    )
    if row is None:
        return None
    operation = await db.get(GrowthOperation, row.operation_id)
    if operation is None:
        await finish_outbox(
            db,
            row.id,
            owner=owner,
            fencing_token=fencing_token,
            state="dead",
            error="p2_operation_missing",
            now=now,
        )
        return row
    if row.account_id != operation.account_id or row.kind != operation.kind:
        await finish_outbox(
            db,
            row.id,
            owner=owner,
            fencing_token=fencing_token,
            state="dead",
            error="p2_outbox_operation_identity_mismatch",
            now=now,
        )
        return row
    envelope = json.loads(row.payload_json or "{}")
    payload = envelope.get("payload") if isinstance(envelope, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    scope_group_ids: list[int] = []
    for value in (
        payload.get("group_db_id"),
        payload.get("group_id"),
        payload.get("telegram_group_id"),
    ):
        try:
            if value is not None:
                scope_group_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not scope_group_ids:
        try:
            scope_group_ids.append(int(operation.target_key))
        except (TypeError, ValueError):
            pass
    scope_error = direct_scope_error(
        account_id=operation.account_id,
        group_ids=scope_group_ids,
        campaign_id=operation.campaign_id,
        run_id=operation.run_id,
    )
    if scope_error:
        await finish_outbox(
            db, row.id, owner=owner, fencing_token=fencing_token,
            state="dead", error=scope_error, now=now,
        )
        return row
    await _owned_outbox(db, row.id, owner, fencing_token, datetime.utcnow())
    try:
        await _validate_p3_snapshot(
            db, snapshot_id=operation.snapshot_id, group_version=operation.group_version,
            rule_version=operation.rule_version, evidence_hash=operation.evidence_hash,
            account_id=operation.account_id, target_key=operation.target_key,
            campaign_id=operation.campaign_id, run_id=operation.run_id,
            kind=operation.kind,
        )
        reservation = await db.get(AccountBudgetReservation, operation.budget_reservation_id, populate_existing=True)
        if (reservation is None or reservation.account_id != operation.account_id
                or reservation.state != "reserved" or reservation.lease_expires_at <= datetime.utcnow()):
            raise ValueError("p2_budget_reservation_invalid")
    except ValueError as exc:
        await finish_outbox(db, row.id, owner=owner, fencing_token=fencing_token, state="dead", error=str(exc))
        return row
    attempt = await record_attempt(db, operation.id, actor_generation=fencing_token, now=now)
    # The intent must survive a hard crash at any point inside actor_submitter.
    await db.commit()

    async def finish_error(exc: BaseException, state: str, outcome: str) -> None:
        finished_at = datetime.utcnow()
        try:
            await _owned_outbox(db, row.id, owner, fencing_token, finished_at)
        except P2StateConflict:
            # Expired dispatches become verification work. A replacement owner
            # alone is allowed to finish a row whose fence has changed.
            await recover_expired_outbox(db, now=finished_at, account_id=operation.account_id)
            await db.commit()
            return
        await mark_attempt(db, attempt.id, outcome=outcome, error=str(exc), now=finished_at)
        await finish_outbox(
            db, row.id, owner=owner, fencing_token=fencing_token,
            state=state, error=str(exc) or type(exc).__name__, now=finished_at,
        )

    try:
        result = await actor_submitter(operation, payload, fencing_token)
        result = result if isinstance(result, ActorResult) else ActorResult(**dict(result))
        if result.actor_generation != fencing_token:
            raise UnknownExternalOutcome("p2_actor_generation_mismatch")
        if result.outcome not in {"sent", "confirmed", "survival_pending", "resolved", "failed", "unknown"}:
            raise UnknownExternalOutcome("p2_actor_outcome_invalid")
        if result.outcome in {"confirmed", "survival_pending", "resolved"} and not result.receipt:
            raise UnknownExternalOutcome("p2_actor_receipt_missing")
        if result.receipt is not None:
            _canonical(dict(result.receipt))
    except RetryableOutboxError as exc:
        await finish_error(exc, "retryable_failed", "retryable_failed")
        return row
    except PermanentOutboxError as exc:
        await finish_error(exc, "dead", "failed")
        return row
    except asyncio.CancelledError as exc:
        await finish_error(exc, "reconciliation_required", "unknown")
        raise
    except Exception as exc:
        await finish_error(exc, "reconciliation_required", "unknown")
        return row

    result_now = datetime.utcnow()
    try:
        await _owned_outbox(db, row.id, owner, fencing_token, result_now)
    except P2StateConflict as exc:
        await finish_error(exc, "reconciliation_required", "unknown")
        return row
    await mark_attempt(
        db,
        attempt.id,
        outcome=result.outcome,
        receipt=result.receipt,
        error=result.error,
        provider_request_id=result.provider_request_id,
        now=result_now,
    )
    if result.outcome == "unknown":
        await finish_outbox(
            db,
            row.id,
            owner=owner,
            fencing_token=fencing_token,
            state="reconciliation_required",
            error=result.error or "external_outcome_unknown",
            provider_request_id=result.provider_request_id,
            now=result_now,
        )
        return row
    if result.provider_request_id:
        row.provider_request_id = result.provider_request_id
    if result.outcome == "failed":
        await transition_operation(
            db,
            operation.id,
            "failed",
            provider_request_id=result.provider_request_id,
            error=result.error,
            now=result_now,
        )
    else:
        await transition_operation(
            db,
            operation.id,
            "sent",
            provider_request_id=result.provider_request_id,
            now=result_now,
        )
        if result.outcome == "sent":
            await transition_operation(db, operation.id, "receipt_pending", now=result_now)
            await finish_outbox(
                db, row.id, owner=owner, fencing_token=fencing_token,
                state="reconciliation_required", error="p2_provider_receipt_pending", now=result_now,
            )
            return row
        if result.outcome in {"confirmed", "survival_pending", "resolved"}:
            await transition_operation(
                db,
                operation.id,
                "receipt_pending",
                provider_request_id=result.provider_request_id,
                now=result_now,
            )
            await transition_operation(
                db,
                operation.id,
                "confirmed",
                provider_request_id=result.provider_request_id,
                provider_receipt=result.receipt,
                now=result_now,
            )
            await _record_receipt_latency(attempt.started_at, result_now)
        if result.outcome in {"confirmed", "survival_pending", "resolved"} and operation.kind == "send_ad":
            await transition_operation(
                db,
                operation.id,
                "survival_pending",
                provider_request_id=result.provider_request_id,
                provider_receipt=result.receipt,
                survival_due_at=(
                    result.survival_due_at
                    or result_now + timedelta(hours=24)
                ),
                now=result_now,
            )
            # A send receipt cannot prove survival. Always schedule a separate
            # provider read at the due time, even if an Actor says resolved.
            db.add(ReconciliationTask(
                task_key=reconciliation_task_key(operation.id), kind="growth",
                source_id=operation.id, account_id=operation.account_id, priority=0,
                state="pending", available_at=operation.survival_due_at,
                verification_due_at=operation.survival_due_at,
                first_discovered_at=result_now, age_threshold_seconds=3600,
                reason="p2_survival_verification",
            ))
        if operation.kind == "join_group" and result.outcome in {"confirmed", "resolved"}:
            await transition_operation(
                db,
                operation.id,
                "resolved",
                provider_request_id=result.provider_request_id,
                provider_receipt=result.receipt,
                now=result_now,
            )
    await finish_outbox(
        db,
        row.id,
        owner=owner,
        fencing_token=fencing_token,
        state="succeeded",
        provider_request_id=result.provider_request_id,
        now=result_now,
    )
    return row
