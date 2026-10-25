"""P2 Outbox and operation state-machine tests."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.account.control_models import AccountBudgetReservation, ReconciliationTask
from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.core.group.models import Group
from app.modules.acquisition.models import AdCampaign
from app.modules.acquisition.p2_models import (
    GrowthOperation,
    GrowthOperationAttempt,
    GrowthOperationEvent,
    GrowthOutbox,
)
from app.modules.acquisition.p2_outbox import (
    ActorResult,
    P2IdempotencyConflict,
    P2InvalidTransition,
    P2StateConflict,
    P2TargetBlocked,
    UnknownExternalOutcome,
    claim_outbox,
    direct_scope_error,
    dispatch_outbox_once,
    enqueue_operation,
    mark_reconciliation_required,
    reconcile_growth_operation,
    record_attempt,
    transition_operation,
)
from app.modules.acquisition.qualification_snapshot import (
    QualificationSnapshot,
    QualificationSnapshotMismatch,
    create_or_get_snapshot,
)

EVIDENCE_HASH = hashlib.sha256(b"p2-evidence").hexdigest()


def test_direct_scope_error_fails_closed_for_durable_and_dispatch_boundaries(monkeypatch):
    monkeypatch.setattr(settings, "P2_DIRECT_SCOPE_ENABLED", True)
    monkeypatch.setattr(settings, "P2_DIRECT_SCOPE_ACCOUNT_IDS", "33,35")
    monkeypatch.setattr(settings, "P2_DIRECT_SCOPE_GROUP_IDS", "231,178")
    monkeypatch.setattr(settings, "P2_DIRECT_SCOPE_CAMPAIGN_ID", 1)
    monkeypatch.setattr(settings, "P2_DIRECT_SCOPE_RUN_ID", "p6-direct-20261025")

    assert direct_scope_error(
        account_id=33,
        group_ids=[231, 1226537673],
        campaign_id=1,
        run_id="p6-direct-20261025",
    ) is None
    assert direct_scope_error(
        account_id=2,
        group_ids=[231],
        campaign_id=1,
        run_id="p6-direct-20261025",
    ) == "p2_direct_scope_account_blocked"
    assert direct_scope_error(
        account_id=33,
        group_ids=[999],
        campaign_id=1,
        run_id="p6-direct-20261025",
    ) == "p2_direct_scope_group_blocked"
    assert direct_scope_error(
        account_id=33,
        group_ids=[231],
        campaign_id=1,
        run_id="wrong-run",
    ) == "p2_direct_scope_run_id_mismatch"


async def _fixtures(db):
    account = TelegramAccount(id=1, identifier="p2-account", session_name="p2-account")
    group = Group(id=1, group_id=-100123456, title="p2")
    campaign = AdCampaign(id=1, name="p2-campaign")
    reservation = AccountBudgetReservation(
        reservation_id="p2-reservation",
        account_id=1,
        workload="growth",
        lane="ad",
        window_start=datetime.utcnow(),
        reserved_reads=1,
        consumed_reads=0,
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.add_all([account, group, campaign, reservation])
    await db.flush()
    return account, group, campaign, reservation


@pytest_asyncio.fixture(autouse=True)
async def p2_fixtures(test_db):
    await _fixtures(test_db)


async def _enqueue(db, *, kind="join_group", payload=None, target="group:1"):
    snapshot_id = "snapshot:" + target
    if await db.get(QualificationSnapshot, snapshot_id) is None:
        db.add(QualificationSnapshot(
            snapshot_id=snapshot_id, target_key=target, group_version="group-v1",
            rule_version="rule-v1", evidence_hash=EVIDENCE_HASH, evidence_json="{}",
            verdict="allowed", source_fact_version="group-v1",
        ))
        await db.flush()
    # A budget reservation is consumed by exactly one logical operation.  The
    # default join fixture keeps the historical id used by the explicit P3
    # validation tests; every other target/kind gets its own active row so a
    # prior dispatch cannot accidentally be reused by a later operation.
    reservation_id = (
        "p2-reservation"
        if kind == "join_group" and target == "group:1"
        else f"p2-reservation:{kind}:{target}"[:160]
    )
    if await db.get(AccountBudgetReservation, reservation_id) is None:
        db.add(AccountBudgetReservation(
            reservation_id=reservation_id,
            account_id=1,
            workload="growth",
            lane="ad",
            window_start=datetime.utcnow(),
            reserved_reads=1,
            consumed_reads=0,
            lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
        ))
        await db.flush()
    return await enqueue_operation(
        db,
        kind=kind,
        account_id=1,
        group_id=1,
        campaign_id=1 if kind == "send_ad" else None,
        target_key=target,
        run_id="run-1",
        snapshot_id=snapshot_id,
        group_version="group-v1",
        rule_version="rule-v1",
        evidence_hash=EVIDENCE_HASH,
        budget_reservation_id=reservation_id,
        payload=payload or {"target": target},
        priority=10,
    )


@pytest.mark.asyncio
async def test_idempotent_enqueue_reuses_operation_and_rejects_payload_conflict(test_db):
    operation, outbox, reused = await _enqueue(test_db)
    assert reused is False
    duplicate, duplicate_outbox, reused = await _enqueue(test_db)
    assert reused is True
    assert duplicate.id == operation.id
    assert duplicate_outbox.id == outbox.id
    assert await test_db.scalar(select(func.count(GrowthOperation.id))) == 1
    assert await test_db.scalar(select(func.count(GrowthOutbox.id))) == 1

    with pytest.raises(P2IdempotencyConflict):
        await enqueue_operation(
            test_db,
            kind="join_group",
            account_id=1,
            target_key="different-target",
            snapshot_id="snapshot-1",
            group_version="group-v1",
            rule_version="rule-v1",
            evidence_hash=EVIDENCE_HASH,
            budget_reservation_id="p2-reservation",
            payload={"target": "different-target"},
            idempotency_key=operation.idempotency_key,
        )


@pytest.mark.asyncio
async def test_ad_cannot_resolve_without_survival(test_db):
    operation, _, _ = await _enqueue(test_db, kind="send_ad", payload={"target": "group:1"})
    await record_attempt(test_db, operation.id, actor_generation="test")
    await transition_operation(test_db, operation.id, "sent")
    await transition_operation(test_db, operation.id, "receipt_pending")
    await transition_operation(test_db, operation.id, "confirmed", provider_receipt={"message_id": 7})
    with pytest.raises(P2InvalidTransition):
        await transition_operation(
            test_db, operation.id, "resolved", provider_receipt={"message_id": 7}
        )
    await transition_operation(
        test_db,
        operation.id,
        "survival_pending",
        provider_receipt={"message_id": 7},
        survival_due_at=datetime.utcnow() + timedelta(hours=1),
    )
    await transition_operation(test_db, operation.id, "resolved", provider_receipt={"message_id": 7})
    current = await test_db.get(GrowthOperation, operation.id)
    assert current.state == "resolved"


@pytest.mark.asyncio
async def test_active_target_is_blocked_across_join_and_ad_kinds(test_db):
    await _enqueue(test_db, target="same-target")
    with pytest.raises(P2TargetBlocked):
        await _enqueue(test_db, kind="send_ad", target="same-target")


@pytest.mark.asyncio
async def test_sent_requires_attempt_and_snapshot_identity_is_immutable(test_db):
    operation, _, _ = await _enqueue(test_db)
    with pytest.raises(P2InvalidTransition):
        await transition_operation(test_db, operation.id, "sent")
    operation.snapshot_id = "rewritten"
    with pytest.raises(ValueError, match="identity_immutable"):
        await test_db.flush()


@pytest.mark.asyncio
async def test_p3_flag_validates_immutable_snapshot_before_enqueue(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P3_QUALIFICATION_SNAPSHOT", True)
    snapshot = await create_or_get_snapshot(
        test_db,
        target_key="group:p3",
        group_version="1",
        rule_version="rule-v1",
        evidence={"qualified": True},
        verdict="qualified",
        source_fact_version="1",
    )
    operation, _, reused = await enqueue_operation(
        test_db,
        kind="join_group",
        account_id=1,
        target_key="group:p3",
        snapshot_id=snapshot.snapshot_id,
        group_version=snapshot.group_version,
        rule_version=snapshot.rule_version,
        evidence_hash=snapshot.evidence_hash,
        budget_reservation_id="p2-reservation",
        payload={"target": "group:p3"},
    )
    assert operation.snapshot_id == snapshot.snapshot_id
    assert reused is False

    with pytest.raises(QualificationSnapshotMismatch):
        await enqueue_operation(
            test_db,
            kind="send_ad",
            account_id=1,
            target_key="group:p3-other",
            snapshot_id=snapshot.snapshot_id,
            group_version=snapshot.group_version,
            rule_version=snapshot.rule_version,
            evidence_hash=hashlib.sha256(b"different").hexdigest(),
            budget_reservation_id="p2-reservation",
            payload={"target": "group:p3-other"},
        )


@pytest.mark.asyncio
async def test_unknown_outcome_is_reconciliation_only_and_not_replayed(test_db):
    operation, outbox, _ = await _enqueue(test_db)
    calls = 0

    async def actor(_operation, _payload, _fence):
        nonlocal calls
        calls += 1
        raise UnknownExternalOutcome("provider_timeout")

    await dispatch_outbox_once(
        test_db,
        owner="worker-1",
        fencing_token="generation-1",
        actor_submitter=actor,
        now=datetime.utcnow(),
    )
    await test_db.refresh(operation)
    await test_db.refresh(outbox)
    assert calls == 1
    assert outbox.state == "reconciliation_required"
    assert operation.reconciliation_required is True
    assert await test_db.scalar(select(func.count(GrowthOperationAttempt.id))) == 1
    assert await test_db.scalar(select(func.count(ReconciliationTask.id))) == 1
    assert await dispatch_outbox_once(
        test_db,
        owner="worker-1",
        fencing_token="generation-1",
        actor_submitter=actor,
    ) is None
    assert calls == 1


@pytest.mark.asyncio
async def test_reconciliation_verifier_resolves_join_without_write_callback(test_db):
    operation, outbox, _ = await _enqueue(test_db)
    await mark_reconciliation_required(test_db, operation.id, reason="unknown")

    async def verifier(current):
        assert current.id == operation.id
        return {"outcome": "confirmed", "receipt": {"membership": True}}

    result = await reconcile_growth_operation(test_db, operation.id, verifier=verifier)
    await test_db.refresh(operation)
    assert result["state"] == "succeeded"
    assert operation.state == "resolved"
    assert operation.reconciliation_required is False
    await test_db.refresh(outbox)
    assert outbox.state == "succeeded"
    assert outbox.completed_at is not None
    assert await test_db.scalar(select(func.count(GrowthOperationEvent.id))) >= 3


@pytest.mark.asyncio
async def test_reconciliation_pending_becomes_manual_after_age_threshold(test_db):
    operation, _, _ = await _enqueue(test_db)
    await mark_reconciliation_required(test_db, operation.id, reason="unknown")
    task = await test_db.scalar(
        select(ReconciliationTask).where(ReconciliationTask.source_id == operation.id)
    )
    task.created_at = datetime.utcnow() - timedelta(hours=2)
    task.first_discovered_at = task.created_at
    await test_db.flush()

    result = await reconcile_growth_operation(
        test_db,
        operation.id,
        verifier=lambda _current: _pending_verification(),
        max_age_seconds=60,
    )
    assert result["state"] == "manual_required"
    current = await test_db.get(GrowthOperation, operation.id)
    assert current.reconciliation_required is True


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, operation_state, outbox_state", [
    ("confirmed", "resolved", "succeeded"),
    ("failed", "failed", "dead"),
])
async def test_unknown_dispatch_reconciliation_finishes_outbox_atomically(
    test_db, outcome, operation_state, outbox_state,
):
    operation, outbox, _ = await _enqueue(test_db)

    async def actor(_operation, _payload, _fence):
        raise UnknownExternalOutcome("provider_timeout")

    await dispatch_outbox_once(
        test_db, owner="sender", fencing_token="generation", actor_submitter=actor,
    )
    task = await test_db.scalar(select(ReconciliationTask).where(
        ReconciliationTask.source_id == operation.id,
    ))
    task.state = "leased"
    task.lease_owner = "reconciler"
    task.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    await test_db.flush()

    async def verifier(_operation):
        return {"outcome": outcome, "receipt": {"membership": outcome == "confirmed"}}

    result = await reconcile_growth_operation(test_db, operation.id, verifier=verifier)
    await test_db.commit()
    await test_db.refresh(operation)
    await test_db.refresh(outbox)
    await test_db.refresh(task)
    assert result["state"] == "succeeded"
    assert operation.state == operation_state
    assert not operation.reconciliation_required
    assert outbox.state == outbox_state
    assert outbox.completed_at is not None
    assert outbox.lease_owner is outbox.lease_expires_at is outbox.fencing_token is None
    # The reconciliation worker owns its separate CAS finish/ACK step.
    assert task.state == "leased"
    assert task.lease_owner == "reconciler"
    assert await claim_outbox(test_db, owner="retry", fencing_token="new") is None


@pytest.mark.asyncio
async def test_reconciliation_defers_live_dispatch_lease_then_closes_expired_lease(test_db):
    operation, outbox, _ = await _enqueue(test_db)
    now = datetime.utcnow()
    await claim_outbox(test_db, owner="sender", fencing_token="generation", now=now)
    await mark_reconciliation_required(test_db, operation.id, reason="concurrent_check", now=now)
    calls = []

    async def verifier(current):
        calls.append(current.id)
        return {"outcome": "confirmed", "receipt": {"membership": True}}

    result = await reconcile_growth_operation(test_db, operation.id, verifier=verifier, now=now)
    assert result["state"] == "pending"
    assert calls == []
    assert outbox.state == "leased"
    assert outbox.lease_owner == "sender"
    assert await test_db.scalar(select(func.count(GrowthOperationAttempt.id))) == 0

    result = await reconcile_growth_operation(
        test_db, operation.id, verifier=verifier, now=now + timedelta(minutes=6),
    )
    assert result["state"] == "succeeded"
    await test_db.refresh(outbox)
    assert outbox.state == "succeeded"
    assert outbox.lease_owner is outbox.lease_expires_at is outbox.fencing_token is None


async def _pending_verification():
    from app.modules.acquisition.p2_outbox import GrowthVerification

    return GrowthVerification("pending", error="still_unknown")


@pytest.mark.asyncio
async def test_fencing_token_blocks_stale_outbox_worker(test_db):
    operation, outbox, _ = await _enqueue(test_db)
    await test_db.commit()
    claimed = await claim_outbox(
        test_db,
        owner="worker-1",
        fencing_token="generation-1",
        commit=False,
    )
    assert claimed is not None
    with pytest.raises(P2StateConflict):
        from app.modules.acquisition.p2_outbox import finish_outbox

        await finish_outbox(
            test_db,
            outbox.id,
            owner="worker-2",
            fencing_token="generation-2",
            state="succeeded",
            commit=False,
        )


@pytest.mark.asyncio
async def test_expired_dispatch_is_reconciled_without_resending(test_db):
    operation, outbox, _ = await _enqueue(test_db)
    now = datetime.utcnow()
    await claim_outbox(test_db, owner="crashed", fencing_token="old", now=now)
    await record_attempt(test_db, operation.id, actor_generation="old")
    await test_db.commit()
    assert await claim_outbox(
        test_db, owner="replacement", fencing_token="new", now=now + timedelta(minutes=6)
    ) is None
    await test_db.refresh(operation)
    await test_db.refresh(outbox)
    assert operation.reconciliation_required
    assert outbox.state == "reconciliation_required"
    assert outbox.attempts == 1
    assert outbox.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "invalid_result", "old_generation", "cancelled"])
async def test_uncertain_actor_response_never_becomes_retryable(test_db, failure):
    operation, outbox, _ = await _enqueue(test_db)

    async def actor(_operation, _payload, fence):
        if failure == "timeout":
            raise TimeoutError("provider timeout")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        if failure == "invalid_result":
            return {"unrecognized": True}
        return ActorResult("confirmed", receipt={"membership": True}, actor_generation="old")

    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await dispatch_outbox_once(test_db, owner="worker", fencing_token="current", actor_submitter=actor)
    else:
        await dispatch_outbox_once(test_db, owner="worker", fencing_token="current", actor_submitter=actor)
    await test_db.refresh(operation)
    await test_db.refresh(outbox)
    assert operation.reconciliation_required
    assert outbox.state == "reconciliation_required"
    assert operation.state == "reserved"
    assert await claim_outbox(test_db, owner="next", fencing_token="next") is None


@pytest.mark.asyncio
async def test_confirmed_join_resolves_and_ad_waits_for_survival(test_db):
    join, _, _ = await _enqueue(test_db)

    async def actor(operation, _payload, fence):
        return ActorResult("confirmed", receipt={"membership": True, "message_id": 42}, actor_generation=fence)

    await dispatch_outbox_once(test_db, owner="worker", fencing_token="fence", actor_submitter=actor)
    await test_db.refresh(join)
    assert join.state == "resolved"
    ad, _, _ = await _enqueue(test_db, kind="send_ad", target="ad-target")
    await dispatch_outbox_once(test_db, owner="worker", fencing_token="fence", actor_submitter=actor)
    await test_db.refresh(ad)
    assert ad.state == "survival_pending"
    assert await test_db.scalar(select(ReconciliationTask).where(ReconciliationTask.source_id == ad.id)) is not None

    async def verifier(_operation):
        return {"outcome": "survived", "receipt": {"message_id": 42, "survived": True}}

    result = await reconcile_growth_operation(test_db, ad.id, verifier=verifier, now=ad.survival_due_at)
    await test_db.refresh(ad)
    assert result["state"] == "succeeded"
    assert ad.state == "resolved"


@pytest.mark.asyncio
async def test_confirmed_reconciliation_ad_does_not_skip_next_survival(test_db):
    ad, outbox, _ = await _enqueue(test_db, kind="send_ad")
    await mark_reconciliation_required(test_db, ad.id, reason="unknown")
    due = datetime.utcnow() + timedelta(hours=24)

    async def confirmed(_operation):
        return {"outcome": "confirmed", "receipt": {"message_id": 42}, "survival_due_at": due}

    assert (await reconcile_growth_operation(test_db, ad.id, verifier=confirmed))["state"] == "pending"
    await test_db.refresh(outbox)
    assert outbox.state == "succeeded"

    async def survived(_operation):
        return {"outcome": "survived", "receipt": {"message_id": 42, "survived": True}}

    assert (await reconcile_growth_operation(test_db, ad.id, verifier=survived, now=due))["state"] == "succeeded"
    await test_db.refresh(ad)
    assert ad.state == "resolved"
