"""P4 history governance: dry-run reports, batched archive, cleanup and skips."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.modules.acquisition import history_governance as governance
from app.modules.acquisition.p2_models import (
    GrowthOperation,
    GrowthOperationAttempt,
    GrowthOperationEvent,
    GrowthOutbox,
)
from app.modules.acquisition.p4_models import (
    GrowthHistoryGovernanceBatch,
    GrowthOperationArchive,
    GrowthPacingDailyAccountStat,
    GrowthPacingDecision,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 22, 12, 0, 0)
EVIDENCE = hashlib.sha256(b"p4-governance").hexdigest()


@pytest.fixture(autouse=True)
async def accounts(test_db):
    test_db.add(TelegramAccount(id=1, identifier="gov-1", session_name="gov-1"))
    test_db.add(TelegramAccount(id=2, identifier="gov-2", session_name="gov-2"))
    await test_db.flush()
    return test_db


async def _terminal_unit(
    db,
    *,
    resolved_at: datetime,
    account_id: int = 1,
    target_key: str = "group:1",
    state: str = "resolved",
    outbox_state: str = "succeeded",
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    with_children: bool = True,
) -> GrowthOperation:
    """One terminal operation with its Outbox row, attempt and event chain."""
    if outbox_state != "leased":
        lease_owner = None
        lease_expires_at = None
    operation = GrowthOperation(
        kind="send_ad",
        idempotency_key=f"p2:send_ad:{target_key}",
        account_id=account_id,
        target_key=target_key,
        run_id="run-1",
        snapshot_id="snapshot-1",
        group_version="group-v1",
        rule_version="rule-v1",
        evidence_hash=EVIDENCE,
        budget_reservation_id="gov-reservation",
        payload_json='{"schema_version": 1, "payload": {}}',
        payload_hash=hashlib.sha256(b"payload").hexdigest(),
        state=state,
        state_version=3,
        provider_request_id="req-1",
        provider_receipt_json='{"ok": true}',
        attempt_count=1,
        reserved_at=resolved_at - timedelta(hours=1),
        sent_at=resolved_at - timedelta(minutes=30),
        confirmed_at=resolved_at,
        resolved_at=resolved_at,
        created_at=resolved_at - timedelta(hours=2),
        updated_at=resolved_at,
    )
    db.add(operation)
    await db.flush()
    db.add(GrowthOutbox(
        operation_id=operation.id,
        kind="send_ad",
        account_id=account_id,
        priority=10,
        payload_version=1,
        state=outbox_state,
        available_at=resolved_at,
        payload_json='{"schema_version": 1, "payload": {}}',
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        completed_at=resolved_at if outbox_state != "leased" else None,
        created_at=resolved_at,
    ))
    if with_children:
        db.add(GrowthOperationAttempt(
            operation_id=operation.id,
            attempt_no=1,
            actor_generation="actor-1",
            provider_request_id="req-1",
            outcome="sent",
            receipt_json='{"message_id": 7}',
            started_at=resolved_at - timedelta(minutes=30),
            finished_at=resolved_at - timedelta(minutes=29),
        ))
        db.add(GrowthOperationEvent(
            operation_id=operation.id,
            event_type="reserved",
            from_state=None,
            to_state="reserved",
            payload_json="{}",
            created_at=resolved_at - timedelta(hours=1),
        ))
        db.add(GrowthOperationEvent(
            operation_id=operation.id,
            event_type="state_transition",
            from_state="reserved",
            to_state="resolved",
            payload_json="{}",
            created_at=resolved_at,
        ))
    await db.flush()
    return operation


async def test_dry_run_is_idempotent(test_db):
    await _terminal_unit(test_db, resolved_at=NOW - timedelta(days=200))
    first = await governance.dry_run(test_db, NOW)
    second = await governance.dry_run(test_db, NOW)
    for report in (first["candidates"], second["candidates"]):
        assert report["operations"] == 1
        assert report["attempts"] == 1
        assert report["events"] == 2
        assert report["outbox"] == 1
    batches = (await test_db.scalars(
        select(GrowthHistoryGovernanceBatch).where(
            GrowthHistoryGovernanceBatch.mode == "dry_run"
        )
    )).all()
    assert len(batches) == 2
    for batch in batches:
        assert batch.status == "succeeded"
        assert batch.dry_run is True


async def test_active_young_and_reconciliation_are_never_candidates(test_db):
    old = NOW - timedelta(days=200)
    # Eligible: terminal, unreconciled, past the hot cutoff.
    await _terminal_unit(test_db, resolved_at=old, target_key="group:resolved")
    # Young terminal operation: still inside the 90-day hot window.
    await _terminal_unit(test_db, resolved_at=NOW - timedelta(days=1), target_key="group:young")
    # Active (non-terminal) operation, however old: never a candidate.
    await _terminal_unit(test_db, resolved_at=old, state="confirmed", target_key="group:active")
    summary = await governance.candidate_summary(test_db, NOW)
    assert summary["operations"] == 1


async def test_archive_batch_preserves_audit_chain_and_deletes_hot_rows(test_db):
    operation = await _terminal_unit(test_db, resolved_at=NOW - timedelta(days=200))
    result = await governance.archive_batch(test_db, NOW)
    assert result["archived_count"] == 1
    assert result["deleted_count"] == 1
    # The operation row is retained as a permanent idempotency tombstone.
    assert await test_db.get(GrowthOperation, operation.id) is not None
    assert (await test_db.scalars(select(GrowthOutbox))).first() is not None
    assert (await test_db.scalars(select(GrowthOperationAttempt))).first() is not None
    assert (await test_db.scalars(select(GrowthOperationEvent))).first() is None
    archive = (await test_db.scalars(select(GrowthOperationArchive))).first()
    assert archive is not None
    assert archive.operation_id == operation.id
    assert archive.idempotency_key == operation.idempotency_key
    assert archive.state_version == operation.state_version
    assert archive.evidence_hash == EVIDENCE
    assert json.loads(archive.provider_receipt_json)["ok"] is True
    events = json.loads(archive.events_json)
    assert [event["event_type"] for event in events] == ["reserved", "state_transition"]
    assert json.loads(archive.attempts_json)[0]["receipt_json"] == '{"message_id": 7}'
    assert json.loads(archive.outbox_json)["state"] == "succeeded"
    batch = await test_db.get(GrowthHistoryGovernanceBatch, archive.batch_id)
    assert batch.status == "succeeded"
    assert batch.checksum


async def test_archive_batch_retry_recovers_after_crash(test_db):
    operation = await _terminal_unit(
        test_db, resolved_at=NOW - timedelta(days=200), target_key="group:crash"
    )
    # Simulate a crash between writing the archive row and deleting hot rows.
    await governance.dry_run(test_db, NOW)
    batch_id = (await test_db.scalars(
        select(GrowthHistoryGovernanceBatch.id).where(
            GrowthHistoryGovernanceBatch.mode == "dry_run"
        )
    )).first()
    test_db.add(GrowthOperationArchive(
        operation_id=operation.id,
        batch_id=batch_id,
        kind=operation.kind,
        account_id=operation.account_id,
        target_key=operation.target_key,
        idempotency_key=operation.idempotency_key,
        snapshot_id=operation.snapshot_id,
        group_version=operation.group_version,
        rule_version=operation.rule_version,
        evidence_hash=operation.evidence_hash,
        payload_hash=operation.payload_hash,
        state=operation.state,
        state_version=operation.state_version,
        reserved_at=operation.reserved_at,
        resolved_at=operation.resolved_at,
        outbox_json="{}",
        attempts_json="[]",
        events_json="[]",
        archived_at=NOW,
    ))
    await test_db.commit()
    result = await governance.archive_batch(test_db, NOW)
    assert result["deleted_count"] == 1
    assert result["skipped"]["already_archived"] == 1
    assert await test_db.get(GrowthOperation, operation.id) is not None
    assert len((await test_db.scalars(select(GrowthOperationArchive))).all()) == 1


async def test_leased_outbox_is_skipped(test_db):
    operation = await _terminal_unit(
        test_db,
        resolved_at=NOW - timedelta(days=200),
        target_key="group:leased",
        outbox_state="leased",
        lease_owner="verifier",
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    summary = await governance.candidate_summary(test_db, NOW)
    assert summary["operations"] == 0
    result = await governance.archive_batch(test_db, NOW)
    assert result["archived_count"] == 0
    assert await test_db.get(GrowthOperation, operation.id) is not None


async def test_cleanup_expired_respects_each_retention(test_db):
    old_decision = GrowthPacingDecision(
        stage="ads", mode="shadow", window_started_at=NOW - timedelta(days=15),
        candidates_json="[]", gate_reasons_json="{}", created_at=NOW - timedelta(days=15),
    )
    young_decision = GrowthPacingDecision(
        stage="ads", mode="shadow", window_started_at=NOW - timedelta(days=2),
        candidates_json="[]", gate_reasons_json="{}", created_at=NOW - timedelta(days=2),
    )
    old_stat = GrowthPacingDailyAccountStat(
        stat_date=NOW - timedelta(days=400), account_id=1,
    )
    batch = GrowthHistoryGovernanceBatch(
        mode="archive", status="succeeded", dry_run=False,
        cutoffs_json="{}", candidates_json="{}", started_at=NOW, finished_at=NOW,
    )
    test_db.add_all([old_decision, young_decision, old_stat, batch])
    await test_db.flush()
    old_archive = GrowthOperationArchive(
        operation_id=999, batch_id=batch.id, kind="send_ad", account_id=1,
        target_key="group:x", idempotency_key="k", snapshot_id="s",
        group_version="g", rule_version="r", evidence_hash=EVIDENCE,
        payload_hash="p", state="resolved", state_version=1,
        reserved_at=NOW - timedelta(days=500), archived_at=NOW - timedelta(days=400),
        outbox_json="{}", attempts_json="[]", events_json="[]",
    )
    test_db.add(old_archive)
    await test_db.flush()
    deleted = await governance.cleanup_expired(test_db, NOW)
    assert deleted["pacing_decisions"] == 1
    assert deleted["daily_stats"] == 1
    assert deleted["archives"] == 0
    assert await test_db.get(GrowthOperationArchive, old_archive.id) is not None
    remaining = (await test_db.scalars(select(GrowthPacingDecision))).all()
    assert [row.id for row in remaining] == [young_decision.id]


@pytest.mark.parametrize("identity", ["missing", "different_key", "active", "matching"])
async def test_archive_cleanup_requires_matching_terminal_identity(test_db, identity):
    operation = None
    if identity != "missing":
        operation = await _terminal_unit(
            test_db, resolved_at=NOW - timedelta(days=500),
            state="reserved" if identity == "active" else "resolved",
        )
    batch = GrowthHistoryGovernanceBatch(
        mode="archive", status="succeeded", dry_run=False,
        cutoffs_json="{}", candidates_json="{}", started_at=NOW, finished_at=NOW,
    )
    test_db.add(batch)
    await test_db.flush()
    archive = GrowthOperationArchive(
        operation_id=operation.id if operation else 999,
        batch_id=batch.id, kind="send_ad", account_id=1, target_key="group:1",
        idempotency_key=operation.idempotency_key
        if operation and identity != "different_key" else "legacy-only-key",
        snapshot_id="snapshot-1", group_version="group-v1", rule_version="rule-v1",
        evidence_hash=EVIDENCE, payload_hash="p", state="resolved", state_version=1,
        reserved_at=NOW - timedelta(days=500), archived_at=NOW - timedelta(days=400),
        outbox_json="{}", attempts_json="[]", events_json="[]",
    )
    test_db.add(archive)
    await test_db.flush()
    expected = int(identity == "matching")
    assert (await governance.candidate_summary(test_db, NOW))["archives"] == expected
    assert (await governance.cleanup_expired(test_db, NOW))["archives"] == expected
    remaining = await test_db.scalar(select(GrowthOperationArchive.id).where(
        GrowthOperationArchive.id == archive.id,
    ))
    assert (remaining is None) == bool(expected)


async def test_refresh_daily_stats_is_idempotent(test_db):
    day = NOW - timedelta(days=1)
    decision = GrowthPacingDecision(
        stage="ads", mode="pacing", window_started_at=day,
        candidate_count=2, gated_count=1, scheduled_count=1,
        selected_account_id=1, applied=True,
        gate_reasons_json='{"gated_budget_wait": 1}',
        candidates_json=json.dumps([
            {"account_id": 1, "gate_reason": None, "score": 0.9, "rank": 1, "published": True,
             "fair_share": 0.5, "actual_share": 1.0},
            {"account_id": 2, "gate_reason": "gated_budget_wait", "score": 0.0, "rank": 0,
             "fair_share": 0.5, "actual_share": 0.0},
        ]),
        created_at=day,
    )
    test_db.add(decision)
    await test_db.flush()
    first = await governance.refresh_daily_stats(test_db, day)
    assert first["accounts"] == 2
    second = await governance.refresh_daily_stats(test_db, day)
    assert second["accounts"] == 2
    rows = (await test_db.scalars(select(GrowthPacingDailyAccountStat))).all()
    assert len(rows) == 2
    by_account = {row.account_id: row for row in rows}
    assert by_account[1].scheduled_count == 1
    assert by_account[1].gated_count == 0
    assert by_account[2].gated_count == 1
    assert by_account[1].avg_score == pytest.approx(0.9)
    assert by_account[1].actual_share_avg == pytest.approx(1.0)


async def test_run_pass_respects_flags(test_db, monkeypatch):
    operation = await _terminal_unit(test_db, resolved_at=NOW - timedelta(days=200))

    monkeypatch.setattr(settings, "P4_HISTORY_GOVERNANCE", False)
    result = await governance.run_pass(test_db, now=NOW)
    assert result["skipped"] == "history_governance_disabled"
    assert "archive" not in result
    assert await test_db.get(GrowthOperation, operation.id) is not None

    monkeypatch.setattr(settings, "P4_HISTORY_GOVERNANCE", True)
    monkeypatch.setattr(settings, "P4_HISTORY_GOVERNANCE_DRY_RUN", True)
    result = await governance.run_pass(test_db, now=NOW)
    assert result["skipped"] == "dry_run_mode"
    assert "archive" not in result
    assert await test_db.get(GrowthOperation, operation.id) is not None

    monkeypatch.setattr(settings, "P4_HISTORY_GOVERNANCE_DRY_RUN", False)
    result = await governance.run_pass(test_db, now=NOW)
    assert result["archive"]["archived_count"] == 1
    assert "cleanup" in result
    assert await test_db.get(GrowthOperation, operation.id) is not None
