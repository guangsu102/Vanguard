"""Bounded transaction-local telemetry; no persistent completion markers."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from typing import Any

from sqlalchemy import event, inspect, select
from sqlalchemy.util.concurrency import await_only, in_greenlet

PENDING_KEY = "vanguard_review_completions_v2"
INSTALLED_KEY = "vanguard_review_completion_hooks_v2"
MAX_PENDING = 64
COMPLETE_OUTCOMES = frozenset({"allowed", "trial", "observe", "wait", "reject", "protected"})


def _gap(at: float | None = None) -> None:
    try:
        from app.core.account.read_metrics import _fault, _state
        _fault(_state(), time.time() if at is None else at)
    except Exception:
        pass


async def _prior(db: Any, row: Any, field: str) -> Any:
    """Read a scalar without autoflush; telemetry SQL has its own savepoint.

    A successful audit lock follows the existing audit->membership order and
    lasts through the outer transaction. No business value is written here.
    """
    from app.modules.acquisition.models import GroupQualificationAudit
    with db.no_autoflush:
        connection = await db.connection()
        async with connection.begin_nested():
            result = await connection.execute(select(getattr(GroupQualificationAudit, field)).where(
                GroupQualificationAudit.id == row.id,
            ).with_for_update(of=GroupQualificationAudit, nowait=True))
            return result.one()


async def _publish(pending: list[dict]) -> None:
    from app.core.account import read_metrics as metrics
    groups = {}
    for item in pending:
        row = item["row"]
        if inspect(row).was_deleted or not row.id or row.state == "cancelled" or row.decision != item["decision"]:
            continue
        if item["kind"] == "renewal" and row.state != "completed":
            continue
        if item["proof"] == "review" and row.checked_at != item["checked_at"]:
            continue
        if item["proof"] == "event" and not item.get("consumed"):
            _gap()
            continue
        if item["proof"] == "identity":
            if json.loads(row.evidence_json or "{}").get("system_identity_fingerprint") != item["identity"]:
                _gap()
                continue
        offset = metrics.RPC_COUNT + (item["kind"] == "renewal") * metrics.WORK_WIDTH
        increments = groups.setdefault((item["account_id"], item["stage"]), Counter())
        outcome = item["decision"] if item["decision"] in metrics.OUTCOMES else "other"
        increments[offset + 8 + metrics.OUTCOMES.index(outcome)] += 1
        if outcome in COMPLETE_OUTCOMES:
            increments[offset + 54] += 1
            increments[offset + 55] += item["elapsed_ms"]
            increments[offset + 35 + metrics.COMPLEXITIES.index(item["complexity"]) * 5] += 1
    async with asyncio.timeout(2):
        for (account_id, stage), increments in groups.items():
            await metrics._persist(account_id, stage, increments)


def _committed(session: Any) -> None:
    if session.in_nested_transaction():
        return
    pending = session.info.pop(PENDING_KEY, [])
    if not pending:
        return
    try:
        if not in_greenlet():
            _gap()
            return
        await_only(_publish(pending))
    except (Exception, asyncio.CancelledError):
        _gap()


def _rolled_back(session: Any, *args: Any) -> None:
    pending = session.info.pop(PENDING_KEY, [])
    if pending and session.in_nested_transaction():
        _gap()


def confirm_event_consumed(db: Any, member: Any, value: dict | None) -> None:
    """Called only after acknowledge's locked compare-and-clear succeeded."""
    try:
        _confirm_event_consumed(db, member, value)
    except Exception:
        _gap()


def _confirm_event_consumed(db: Any, member: Any, value: dict | None) -> None:
    from app.modules.acquisition.qualification_events import recovery_event_key
    identity = recovery_event_key(value)
    if identity is None:
        return
    session = getattr(db, "sync_session", None)
    for item in (session.info.get(PENDING_KEY, []) if session is not None else []):
        if item["proof"] == "event" and item["membership_id"] == member.id and item["event_key"] == identity:
            item["consumed"] = True


async def stage_completion(db: Any, row: Any, work: Any, *, kind: str,
                           event_value: dict | None = None,
                           identity_change: tuple[Any, str] | None = None) -> bool:
    """Stage one proven business transition without changing its outcome."""
    if kind not in {"review", "renewal"} or row is None:
        return False
    sync = getattr(db, "sync_session", None)
    if sync is None or not isinstance(getattr(sync, "info", None), dict):
        _gap()
        return False
    from app.core.account.read_metrics import COMPLEXITIES, stage_for
    pending = sync.info.setdefault(PENDING_KEY, [])
    try:
        extra = {}
        if kind == "review":
            if row.checked_at is None:
                _gap()
                return False
            if not inspect(row).pending:
                previous = await _prior(db, row, "checked_at")
                if previous[0] == row.checked_at:
                    _gap()
                    return False
            proof = "review"
        elif event_value is not None:
            from app.modules.acquisition.qualification_events import recovery_event_key
            identity = recovery_event_key(event_value)
            if not identity or not row.membership_id:
                _gap()
                return False
            proof = "event"
            extra = {"event_key": identity, "membership_id": row.membership_id, "consumed": False}
        elif identity_change is not None:
            old, new = identity_change
            if old == new or not isinstance(new, str) or not new:
                return False
            previous = await _prior(db, row, "evidence_json")
            if json.loads(previous[0] or "{}").get("system_identity_fingerprint") != old:
                _gap()
                return False
            proof = "identity"
            extra = {"identity": new}
        else:
            return False
        if len(pending) >= MAX_PENDING:
            _gap()
            return False
        complexity = getattr(work, "measurement_complexity", "unknown")
        item = {"row": row, "kind": kind, "proof": proof, "decision": row.decision,
            "account_id": row.account_id, "checked_at": row.checked_at,
            "stage": getattr(work, "measurement_stage", None) or stage_for(row.account_id),
            "complexity": complexity if complexity in COMPLEXITIES else "unknown",
            "elapsed_ms": max(0, round((time.monotonic()-work.started)*1000)) if work else 0, **extra}
        pending[:] = [old for old in pending if not (old["row"] is row and old["kind"] == kind)]
        pending.append(item)
        if not sync.info.get(INSTALLED_KEY):
            event.listen(sync, "after_commit", _committed)
            event.listen(sync, "after_rollback", _rolled_back)
            sync.info[INSTALLED_KEY] = True
        return True
    except Exception:
        _gap()
        return False
