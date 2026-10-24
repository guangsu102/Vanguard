"""Auditable, recoverable history governance for the P2 growth family.

Retention defaults (all overridable via settings):

* active operations, ``unknown`` outcomes and ``reconciliation_required``
  rows are retained until resolved — never archived, never deleted;
* terminal operations keep 90 hot days, then their event chains are archived
  and compacted; operation identities, Outbox rows and attempts are retained;
* archived audit evidence and the state-transition event chain keep 370 days;
* pacing decisions and derived queue statistics keep 14 days;
* per-account daily fairness aggregates keep 370 days.

Every pass first writes a dry-run candidate report.  Archiving is a single
transaction per batch and idempotent per operation (the archive row is keyed
by the original operation ID), so a failed batch is safely retried.  The
module never triggers Telegram operations and never touches P2 state that is
not already terminal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, distinct, exists, func, select

from app.core.config import settings
from app.modules.acquisition.p2_models import (
    GrowthOperation,
    GrowthOperationAttempt,
    GrowthOperationEvent,
    GrowthOutbox,
)
from app.modules.acquisition.p2_outbox import TERMINAL_OPERATION_STATES
from app.modules.acquisition.p4_models import (
    GrowthHistoryGovernanceBatch,
    GrowthOperationArchive,
    GrowthPacingDailyAccountStat,
    GrowthPacingDecision,
)

CLEANUP_DELETE_BATCH = 5000


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def retention_days() -> dict[str, int]:
    return {
        "hot": max(1, settings.P4_HISTORY_HOT_RETENTION_DAYS),
        "audit": max(1, settings.P4_HISTORY_AUDIT_RETENTION_DAYS),
        "recent": max(1, settings.P4_PACING_DECISION_RETENTION_DAYS),
    }


def cutoffs(now: datetime) -> dict[str, datetime]:
    days = retention_days()
    return {
        "hot_cutoff": now - timedelta(days=days["hot"]),
        "audit_cutoff": now - timedelta(days=days["audit"]),
        "recent_cutoff": now - timedelta(days=days["recent"]),
    }


def _terminal_age_column():
    """Resolved operations age from resolution; failures from last update."""
    return func.coalesce(GrowthOperation.resolved_at, GrowthOperation.updated_at)


def eligible_operation_criterion(now: datetime):
    """Terminal, unreconciled operations past the hot retention cutoff."""
    return (
        GrowthOperation.state.in_(TERMINAL_OPERATION_STATES),
        GrowthOperation.reconciliation_required.is_(False),
        GrowthOperation.archived_at.is_(None),
        _terminal_age_column() <= cutoffs(now)["hot_cutoff"],
    )


def eligible_operation_ids(db: Any, now: datetime, *, limit: int | None = None):
    """Archive candidates; an operation with an active Outbox lease never is.

    Terminal operations should not have leased Outbox rows, but a crashed
    verifier or manual repair can leave one, so the anti-join is a hard
    safety net instead of an assumption.
    """
    leased_outbox = exists(
        select(GrowthOutbox.id).where(
            GrowthOutbox.operation_id == GrowthOperation.id,
            GrowthOutbox.state == "leased",
            GrowthOutbox.lease_expires_at > now,
        )
    )
    query = select(GrowthOperation.id).where(
        *eligible_operation_criterion(now),
        ~leased_outbox,
    )
    if limit is not None:
        query = query.limit(limit)
    return query


def expired_archive_criterion(now: datetime):
    """Expire evidence only when the matching permanent identity survives.

    Older archive batches removed the original operation. Such an archive
    remains the only duplicate-send guard and must outlive audit retention.
    """
    retained_identity = exists(select(GrowthOperation.id).where(
        GrowthOperation.id == GrowthOperationArchive.operation_id,
        GrowthOperation.idempotency_key == GrowthOperationArchive.idempotency_key,
        GrowthOperation.state.in_(TERMINAL_OPERATION_STATES),
        GrowthOperation.reconciliation_required.is_(False),
    ))
    return (
        (GrowthOperationArchive.archived_at < cutoffs(now)["audit_cutoff"])
        & retained_identity
    )


async def candidate_summary(db: Any, now: datetime) -> dict[str, Any]:
    """Dry-run candidate report by type, time range and count.

    Deterministic and idempotent: repeated dry-runs over unchanged data
    produce identical reports.  Unknown, reconciliation, active operations,
    unexpired evidence and active leases are never candidates.
    """
    marks = cutoffs(now)
    operation_ids = [
        int(row) for row in (await db.scalars(eligible_operation_ids(db, now))).all()
    ]
    counts: dict[str, int] = {}
    oldest_eligible: datetime | None = None
    if operation_ids:
        # DISTINCT is required: the three outer joins fan out, and a plain
        # COUNT would multiply attempts by events by Outbox rows.
        attempts, events, outbox = (
            await db.execute(
                select(
                    func.count(distinct(GrowthOperationAttempt.id)),
                    func.count(distinct(GrowthOperationEvent.id)),
                    func.count(distinct(GrowthOutbox.id)),
                )
                .select_from(GrowthOperation)
                .outerjoin(
                    GrowthOperationAttempt,
                    GrowthOperationAttempt.operation_id == GrowthOperation.id,
                )
                .outerjoin(
                    GrowthOperationEvent,
                    GrowthOperationEvent.operation_id == GrowthOperation.id,
                )
                .outerjoin(GrowthOutbox, GrowthOutbox.operation_id == GrowthOperation.id)
                .where(GrowthOperation.id.in_(operation_ids))
            )
        ).one()
        oldest_eligible = await db.scalar(
            select(func.min(_terminal_age_column())).where(
                GrowthOperation.id.in_(operation_ids)
            )
        )
        counts.update({
            "operations": len(operation_ids),
            "attempts": int(attempts or 0),
            "events": int(events or 0),
            "outbox": int(outbox or 0),
        })
    else:
        counts.update({"operations": 0, "attempts": 0, "events": 0, "outbox": 0})
    counts["pacing_decisions"] = int(
        (await db.scalar(
            select(func.count(GrowthPacingDecision.id)).where(
                GrowthPacingDecision.created_at < marks["recent_cutoff"]
            )
        ))
        or 0
    )
    counts["daily_stats"] = int(
        (await db.scalar(
            select(func.count(GrowthPacingDailyAccountStat.id)).where(
                GrowthPacingDailyAccountStat.stat_date < marks["audit_cutoff"]
            )
        ))
        or 0
    )
    counts["archives"] = int(
        (await db.scalar(
            select(func.count(GrowthOperationArchive.id)).where(
                expired_archive_criterion(now)
            )
        ))
        or 0
    )
    return {
        **counts,
        "oldest_eligible_age_seconds": max(
            0.0, (now - oldest_eligible).total_seconds() if oldest_eligible else 0.0
        ),
    }


async def _new_batch(
    db: Any,
    *,
    mode: str,
    status: str,
    dry_run: bool,
    now: datetime,
    **fields: Any,
) -> GrowthHistoryGovernanceBatch:
    batch = GrowthHistoryGovernanceBatch(
        mode=mode,
        status=status,
        dry_run=dry_run,
        cutoffs_json=_canonical(
            {
                "retention_days": retention_days(),
                "cutoffs": {key: value.isoformat() for key, value in cutoffs(now).items()},
            }
        ),
        **fields,
    )
    db.add(batch)
    await db.flush()
    return batch


async def dry_run(db: Any, now: datetime) -> dict[str, Any]:
    """Write one dry-run candidate report; safe to run repeatedly."""
    from app.core import metrics

    summary = await candidate_summary(db, now)
    batch = await _new_batch(
        db,
        mode="dry_run",
        status="succeeded",
        dry_run=True,
        now=now,
        candidates_json=_canonical(summary),
        skipped_json=_canonical(
            {
                "reason": "active_or_unresolved_always_skipped",
                "active_operation_states": [
                    "reserved", "sent", "receipt_pending", "confirmed", "survival_pending",
                ],
                "reconciliation_required": True,
                "leased_outbox": True,
                "unexpired_evidence": True,
            }
        ),
    )
    await db.commit()
    try:
        await metrics.record_history_batch("dry_run", "succeeded")
    except Exception:
        pass
    return {"batch_id": batch.id, "candidates": summary}


async def _archive_one_operation(
    db: Any,
    operation: GrowthOperation,
    batch_id: int,
    now: datetime,
) -> str:
    """Archive one terminal operation unit and compact its hot event chain.

    Returns ``"archived"`` (new archive row), ``"deleted_only"`` (an earlier
    crash already wrote the archive row) or ``"skipped"`` when the terminal
    recheck failed or an active lease reappeared under the row lock.
    """
    if (
        operation.state not in TERMINAL_OPERATION_STATES
        or operation.reconciliation_required
        or (operation.resolved_at or operation.updated_at) > cutoffs(now)["hot_cutoff"]
    ):
        return "skipped"
    outbox = await db.scalar(
        select(GrowthOutbox).where(GrowthOutbox.operation_id == operation.id)
    )
    if outbox is not None and (
        outbox.state == "leased"
        and outbox.lease_expires_at is not None
        and outbox.lease_expires_at > now
    ):
        return "skipped"
    attempts = (await db.scalars(
        select(GrowthOperationAttempt)
        .where(GrowthOperationAttempt.operation_id == operation.id)
        .order_by(GrowthOperationAttempt.attempt_no)
    )).all()
    events = (await db.scalars(
        select(GrowthOperationEvent)
        .where(GrowthOperationEvent.operation_id == operation.id)
        .order_by(GrowthOperationEvent.id)
    )).all()
    existing = await db.scalar(
        select(GrowthOperationArchive).where(
            GrowthOperationArchive.operation_id == operation.id
        )
    )
    operation.archived_at = now
    operation.state_version += 1
    await db.flush()
    if existing is None:
        db.add(
            GrowthOperationArchive(
                operation_id=operation.id,
                batch_id=batch_id,
                kind=operation.kind,
                account_id=operation.account_id,
                group_id=operation.group_id,
                campaign_id=operation.campaign_id,
                target_key=operation.target_key,
                idempotency_key=operation.idempotency_key,
                snapshot_id=operation.snapshot_id,
                group_version=operation.group_version,
                rule_version=operation.rule_version,
                evidence_hash=operation.evidence_hash,
                budget_reservation_id=operation.budget_reservation_id,
                payload_hash=operation.payload_hash,
                state=operation.state,
                state_version=operation.state_version,
                reconciliation_required=operation.reconciliation_required,
                provider_request_id=operation.provider_request_id,
                provider_receipt_json=operation.provider_receipt_json,
                attempt_count=operation.attempt_count,
                reserved_at=operation.reserved_at,
                sent_at=operation.sent_at,
                confirmed_at=operation.confirmed_at,
                survival_due_at=operation.survival_due_at,
                resolved_at=operation.resolved_at,
                outbox_json=_canonical(
                    {
                        "state": outbox.state if outbox else None,
                        "attempts": outbox.attempts if outbox else None,
                        "priority": outbox.priority if outbox else None,
                        "provider_request_id": (
                            outbox.provider_request_id if outbox else None
                        ),
                        "completed_at": _iso(outbox.completed_at) if outbox else None,
                    }
                ),
                attempts_json=_canonical(
                    [
                        {
                            "attempt_no": attempt.attempt_no,
                            "actor_generation": attempt.actor_generation,
                            "provider_request_id": attempt.provider_request_id,
                            "outcome": attempt.outcome,
                            "receipt_json": attempt.receipt_json,
                            "error": attempt.error,
                            "started_at": _iso(attempt.started_at),
                            "finished_at": _iso(attempt.finished_at),
                        }
                        for attempt in attempts
                    ]
                ),
                events_json=_canonical(
                    [
                        {
                            "event_type": event.event_type,
                            "from_state": event.from_state,
                            "to_state": event.to_state,
                            "payload_json": event.payload_json,
                            "created_at": _iso(event.created_at),
                        }
                        for event in events
                    ]
                ),
                archived_at=now,
            )
        )
    # Keep the terminal GrowthOperation, terminal Outbox and all attempts
    # as permanent idempotency/fencing tombstones. Only the verbose
    # event chain is compacted after it has been copied to the archive.
    for child in events:
        await db.delete(child)
    return "deleted_only" if existing is not None else "archived"


async def archive_batch(db: Any, now: datetime, *, batch_size: int | None = None) -> dict[str, Any]:
    """Archive one bounded batch, retaining operation identities and attempts.

    The whole batch is one transaction: a failure rolls back every partial
    archive and records a failed batch, and the next pass simply retries.  If
    a crash left an archive row without event compaction, the retry sees the
    existing archive row and only compacts the event chain.
    """
    from app.core import metrics

    size = max(1, batch_size or settings.P4_HISTORY_ARCHIVE_BATCH_SIZE)
    # Both the terminal recheck and the active-lease safety net run again
    # under the row lock; every hold-back lands in ``recheck_failed``.
    skipped = {"recheck_failed": 0, "already_archived": 0, "missing": 0}
    try:
        operation_ids = [
            int(row)
            for row in (
                await db.scalars(eligible_operation_ids(db, now, limit=size))
            ).all()
        ]
        batch = await _new_batch(
            db, mode="archive", status="running", dry_run=False, now=now
        )
        archived: list[int] = []
        deleted: list[int] = []
        for operation_id in operation_ids:
            operation = await db.scalar(
                select(GrowthOperation)
                .where(GrowthOperation.id == operation_id)
                .with_for_update(skip_locked=True)
            )
            if operation is None:
                skipped["missing"] += 1
                continue
            outcome = await _archive_one_operation(db, operation, batch.id, now)
            if outcome == "archived":
                archived.append(operation_id)
                deleted.append(operation_id)
            elif outcome == "deleted_only":
                skipped["already_archived"] += 1
                deleted.append(operation_id)
            else:
                skipped["recheck_failed"] += 1
        checksum = hashlib.sha256(
            _canonical({"archived": sorted(archived), "deleted": sorted(deleted)}).encode("utf-8")
        ).hexdigest()
        batch.status = "succeeded"
        batch.archived_count = len(archived)
        batch.deleted_count = len(deleted)
        batch.skipped_json = _canonical(skipped)
        batch.checksum = checksum
        batch.candidates_json = _canonical(
            {"operations": len(operation_ids), "skipped": skipped}
        )
        batch.finished_at = now
        await db.commit()
        try:
            await metrics.record_history_batch("archive", "succeeded")
        except Exception:
            pass
        return {
            "batch_id": batch.id,
            "archived_count": len(archived),
            "deleted_count": len(deleted),
            "skipped": skipped,
        }
    except Exception as exc:
        await db.rollback()
        error = str(exc)
        try:
            failure = await _new_batch(
                db,
                mode="archive",
                status="failed",
                dry_run=False,
                now=now,
                error=error[:1000],
                candidates_json=_canonical({"error": error[:400]}),
                finished_at=datetime.utcnow(),
            )
            await db.commit()
            try:
                await metrics.record_history_batch("archive", "failed")
            except Exception:
                pass
            return {"batch_id": failure.id, "error": error}
        except Exception:
            return {"error": error}


async def _delete_in_batches(db: Any, model: Any, criterion: Any, *, limit: int) -> int:
    """Bounded bulk deletes so one cleanup pass never holds a huge statement."""
    total = 0
    while True:
        ids = [
            int(row)
            for row in (
                await db.scalars(select(model.id).where(criterion).limit(limit))
            ).all()
        ]
        if not ids:
            return total
        await db.execute(
            delete(model)
            .where(model.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        total += len(ids)
        if len(ids) < limit:
            return total


async def cleanup_expired(db: Any, now: datetime) -> dict[str, Any]:
    """Delete expired short-retention and audit rows; nothing else."""
    from app.core import metrics

    marks = cutoffs(now)
    deleted = {
        "pacing_decisions": await _delete_in_batches(
            db, GrowthPacingDecision,
            GrowthPacingDecision.created_at < marks["recent_cutoff"],
            limit=CLEANUP_DELETE_BATCH,
        ),
        "daily_stats": await _delete_in_batches(
            db, GrowthPacingDailyAccountStat,
            GrowthPacingDailyAccountStat.stat_date < marks["audit_cutoff"],
            limit=CLEANUP_DELETE_BATCH,
        ),
        "archives": await _delete_in_batches(
            db, GrowthOperationArchive,
            expired_archive_criterion(now),
            limit=CLEANUP_DELETE_BATCH,
        ),
    }
    if not any(deleted.values()):
        return deleted
    batch = await _new_batch(
        db,
        mode="cleanup",
        status="succeeded",
        dry_run=False,
        now=now,
        candidates_json=_canonical(deleted),
        archived_count=0,
        deleted_count=sum(deleted.values()),
    )
    await db.commit()
    try:
        await metrics.record_history_batch("cleanup", "succeeded")
    except Exception:
        pass
    return {"batch_id": batch.id, **deleted}


async def refresh_daily_stats(db: Any, day: datetime) -> dict[str, int]:
    """Derive one day of per-account fairness aggregates from decisions.

    Recomputing the whole day makes the pass idempotent: a rerun replaces the
    previous aggregate instead of double counting.
    """
    from app.modules.acquisition.models import AdDeliveryLog

    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    decisions = (await db.scalars(
        select(GrowthPacingDecision).where(
            GrowthPacingDecision.stage == "ads",
            GrowthPacingDecision.created_at >= day_start,
            GrowthPacingDecision.created_at < day_end,
        )
    )).all()
    accounts: dict[int, dict[str, Any]] = {}
    for decision in decisions:
        try:
            candidates = json.loads(decision.candidates_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            account_id = candidate.get("account_id")
            if not isinstance(account_id, int) or account_id <= 0:
                continue
            entry = accounts.setdefault(account_id, {
                "decision_count": 0, "gated_count": 0, "scheduled_count": 0,
                "score_sum": 0.0, "score_count": 0,
                "fair_share_sum": 0.0, "actual_share_sum": 0.0,
            })
            entry["decision_count"] += 1
            if candidate.get("published") is True:
                entry["scheduled_count"] += 1
            if candidate.get("gate_reason"):
                entry["gated_count"] += 1
            elif candidate.get("rank"):
                entry["score_sum"] += float(candidate.get("score") or 0)
                entry["score_count"] += 1
                entry["fair_share_sum"] += float(candidate.get("fair_share") or 0)
                entry["actual_share_sum"] += float(candidate.get("actual_share") or 0)
    success_rows = (await db.execute(
        select(AdDeliveryLog.account_id, func.count(AdDeliveryLog.id)).where(
            AdDeliveryLog.status == "success",
            AdDeliveryLog.telegram_message_id.is_not(None),
            AdDeliveryLog.sent_at >= day_start,
            AdDeliveryLog.sent_at < day_end,
        ).group_by(AdDeliveryLog.account_id)
    )).all()
    success_counts = {int(account_id): int(count) for account_id, count in success_rows}
    existing = (await db.scalars(
        select(GrowthPacingDailyAccountStat).where(
            GrowthPacingDailyAccountStat.stat_date == day_start
        )
    )).all()
    by_account = {int(row.account_id): row for row in existing}
    written = 0
    for account_id, entry in accounts.items():
        score_count = max(1, entry["score_count"])
        values = {
            "decision_count": entry["decision_count"],
            "candidate_count": entry["decision_count"],
            "gated_count": entry["gated_count"],
            "scheduled_count": entry["scheduled_count"],
            "avg_score": round(entry["score_sum"] / score_count, 6),
            "fair_share_avg": round(entry["fair_share_sum"] / score_count, 6),
            "actual_share_avg": round(entry["actual_share_sum"] / score_count, 6),
            "ad_success_count": success_counts.get(account_id, 0),
        }
        row = by_account.get(account_id)
        if row is None:
            db.add(
                GrowthPacingDailyAccountStat(
                    stat_date=day_start, account_id=account_id, **values
                )
            )
        else:
            for field, value in values.items():
                setattr(row, field, value)
        written += 1
    await db.commit()
    return {"accounts": written, "decisions": len(decisions)}


async def run_pass(db: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """One scheduled governance pass: report first, then act within flags."""
    now = now or datetime.utcnow()
    result: dict[str, Any] = {"dry_run": await dry_run(db, now)}
    if not settings.P4_HISTORY_GOVERNANCE:
        result["skipped"] = "history_governance_disabled"
        return result
    result["daily_stats"] = await refresh_daily_stats(db, now - timedelta(days=1))
    if settings.P4_HISTORY_GOVERNANCE_DRY_RUN:
        result["skipped"] = "dry_run_mode"
        return result
    result["archive"] = await archive_batch(db, now)
    result["cleanup"] = await cleanup_expired(db, now)
    return result
