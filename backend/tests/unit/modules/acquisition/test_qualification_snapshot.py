"""P3 immutable qualification snapshot tests."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.modules.acquisition.qualification_snapshot import (
    QualificationSnapshot,
    QualificationSnapshotMismatch,
    QualificationSnapshotNotEligible,
    bump_fact_version,
    canonical_json,
    create_or_get_snapshot,
    current_fact_version,
    derive_evidence_hash,
    to_envelope,
    validate_envelope,
)


@pytest.mark.asyncio
async def test_canonical_evidence_hash_ignores_runtime_timestamps():
    first = {"permissions": {"member": True}, "checked_at": "2026-01-01T00:00:00"}
    second = {"checked_at": "2026-01-02T00:00:00", "permissions": {"member": True}}
    assert canonical_json(first) != canonical_json(second)
    assert derive_evidence_hash(first) == derive_evidence_hash(second)
    assert len(derive_evidence_hash(first)) == 64


def test_evidence_hash_excludes_account_and_review_runtime_fields():
    first = {
        "permissions": {"member": True},
        "account_id": 2,
        "audit_id": 10,
        "read_cost": {"rpc": 3},
        "qualification_exit_history": ["old"],
    }
    second = {
        "permissions": {"member": True},
        "account_id": 34,
        "audit_id": 99,
        "read_cost": {"rpc": 20},
        "qualification_exit_history": ["different"],
    }
    assert derive_evidence_hash(first) == derive_evidence_hash(second)


@pytest.mark.asyncio
async def test_snapshot_is_deduplicated_and_envelope_validated(test_db):
    evidence = {"permissions": {"member": True}, "quality": "qualified"}
    first = await create_or_get_snapshot(
        test_db,
        target_key="group:-1001",
        group_version="17",
        rule_version="rule-v2",
        evidence=evidence,
        verdict="qualified",
        source_fact_version="17",
    )
    second = await create_or_get_snapshot(
        test_db,
        target_key="group:-1001",
        group_version="17",
        rule_version="rule-v2",
        evidence=evidence,
        verdict="qualified",
        source_fact_version="17",
    )
    assert second.snapshot_id == first.snapshot_id
    assert await test_db.scalar(select(QualificationSnapshot).where(QualificationSnapshot.snapshot_id == first.snapshot_id)) is first

    envelope = to_envelope(first, account_id=7, run_id="run-1")
    assert envelope.account_id == 7
    assert (await validate_envelope(test_db, envelope)).snapshot_id == first.snapshot_id

    with pytest.raises(QualificationSnapshotMismatch):
        await validate_envelope(
            test_db,
            envelope.__class__(
                snapshot_id=envelope.snapshot_id,
                group_version=envelope.group_version,
                rule_version=envelope.rule_version,
                evidence_hash=hashlib.sha256(b"other").hexdigest(),
                account_id=envelope.account_id,
                target_key=envelope.target_key,
                run_id=envelope.run_id,
            ),
        )


@pytest.mark.asyncio
async def test_unknown_verdict_cannot_create_snapshot(test_db):
    with pytest.raises(QualificationSnapshotNotEligible):
        await create_or_get_snapshot(
            test_db,
            target_key="group:-1002",
            group_version="1",
            rule_version="rule-v1",
            evidence={"unknowns": ["flood_wait"]},
            verdict="unknown",
            source_fact_version="1",
        )


@pytest.mark.asyncio
async def test_snapshot_orm_rejects_identity_mutation(test_db):
    snapshot = await create_or_get_snapshot(
        test_db,
        target_key="group:-1003",
        group_version="1",
        rule_version="rule-v1",
        evidence={"ok": True},
        verdict="qualified",
        source_fact_version="1",
    )
    snapshot.evidence_json = "{\"changed\":true}"
    with pytest.raises(ValueError, match="qualification_snapshot_immutable"):
        await test_db.flush()


@pytest.mark.asyncio
async def test_fact_version_is_monotonic_and_transactional(test_db):
    assert await current_fact_version(test_db, "group:-1004") == "0"
    assert await bump_fact_version(test_db, "group:-1004") == "1"
    assert await bump_fact_version(test_db, "group:-1004") == "2"
    assert await current_fact_version(test_db, "group:-1004") == "2"


@pytest.mark.asyncio
async def test_same_evidence_with_new_runtime_fields_reuses_snapshot(test_db):
    common = {"target_key": "group:reuse", "group_version": "1", "rule_version": "r1",
              "verdict": "allowed", "source_fact_version": "1"}
    first = await create_or_get_snapshot(test_db, evidence={"ok": True, "checked_at": "yesterday", "account_id": 2}, **common)
    second = await create_or_get_snapshot(test_db, evidence={"account_id": 34, "checked_at": "today", "ok": True}, **common)
    assert first.snapshot_id == second.snapshot_id


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["rejected", "observe", "legacy"])
async def test_non_eligible_snapshot_cannot_authorize_outbox(test_db, verdict):
    snapshot = await create_or_get_snapshot(test_db, target_key="group:reject", group_version="1",
        rule_version="r1", verdict=verdict, source_fact_version="1", evidence={"decision": verdict})
    with pytest.raises(QualificationSnapshotNotEligible):
        to_envelope(snapshot, account_id=1)


@pytest.mark.asyncio
async def test_new_fact_invalidates_qualification_envelope(test_db):
    await bump_fact_version(test_db, "group:changed")
    snapshot = await create_or_get_snapshot(test_db, target_key="group:changed", group_version="1",
        rule_version="r1", verdict="allowed", source_fact_version="1", evidence={"ok": True})
    envelope = to_envelope(snapshot, account_id=1)
    await bump_fact_version(test_db, "group:changed")
    with pytest.raises(QualificationSnapshotMismatch, match="fact_version_stale"):
        await validate_envelope(test_db, envelope)
