"""P1 inbox fencing, fairness, evidence preservation and fixed baseline."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account import event_inbox as inbox
from app.core.account.listener_facts import enqueue_fact
from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.core.worker_status import TelegramEventInbox
from tests.unit.test_growth_event_inbox import event

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def p1_enabled(monkeypatch):
    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", True)


async def accounts(db):
    for account_id in (2, 34):
        db.add(TelegramAccount(id=account_id, identifier=str(account_id), session_name=str(account_id)))
    await db.commit()


async def test_batch_round_robins_accounts_before_second_fact(test_db):
    await accounts(test_db)
    for account_id in (2, 34):
        for index in range(4):
            test_db.add(TelegramEventInbox(account_id=account_id, event_key=f"{account_id}:{index}",
                kind="listener_fact", payload_json="{}", state="pending"))
    await test_db.commit()
    rows = await inbox.claim_batch(test_db, [2, 34], local_only=True, after_account_id=2, limit=4)
    assert [row.account_id for row in rows] == [34, 2, 34, 2]
    assert len({row.id for row in rows}) == 4
    assert all(row.claim_owner and row.claim_lease_expires_at for row in rows)


async def test_expired_owner_cannot_finish_or_write_after_reclaim(test_db, monkeypatch):
    await accounts(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await test_db.commit()
    row = await inbox.claim(test_db, [2], owner="same-worker")
    stale_owner = row.claim_owner
    row.claim_lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    await test_db.commit()
    assert await inbox.recover_expired_leases(test_db) == {"retried": 1, "reconciled": 0}
    await test_db.commit()
    replacement = await inbox.claim(test_db, [2], owner="same-worker")
    replacement_owner = replacement.claim_owner
    assert replacement_owner != stale_owner
    await test_db.commit()
    with pytest.raises(RuntimeError, match="claim_lost"):
        await inbox.finish(test_db, row.id, owner=stale_owner)

    @asynccontextmanager
    async def session():
        yield test_db

    monkeypatch.setattr(inbox, "get_db_session", session)
    event_token = inbox.current_event_id.set(row.id)
    claim_token = inbox.current_claim_owner.set(stale_owner)
    try:
        with pytest.raises(RuntimeError, match="claim_lost"):
            await inbox.mark_external_attempt()
    finally:
        inbox.current_event_id.reset(event_token)
        inbox.current_claim_owner.reset(claim_token)
    await inbox.finish(test_db, row.id, owner=replacement_owner)
    assert row.state == "completed"
    assert row.checkpoint_stage == "completed"


async def test_expired_external_attempt_is_fenced_and_not_replayed(test_db):
    await accounts(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await test_db.commit()
    row = await inbox.claim(test_db, [2])
    old_owner = row.claim_owner
    row.external_attempted = True
    row.claim_lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    await test_db.commit()
    assert await inbox.recover_expired_leases(test_db) == {"retried": 0, "reconciled": 1}
    await test_db.commit()
    assert await inbox.claim(test_db, [2]) is None
    with pytest.raises(RuntimeError, match="claim_lost"):
        await inbox.finish(test_db, row.id, owner=old_owner)
    assert row.state == "reconciliation_required"


async def test_distinct_business_messages_and_receipts_never_coalesce(test_db):
    await accounts(test_db)
    await inbox.enqueue(test_db, 2, "message", event(1))
    await inbox.enqueue(test_db, 2, "message", event(2))
    assert not await inbox.enqueue(test_db, 2, "message", event(2))
    await test_db.commit()
    rows = list((await test_db.scalars(select(TelegramEventInbox))).all())
    assert len(rows) == 2
    assert {json.loads(row.payload_json)["id"] for row in rows} == {1, 2}
    assert all(row.coalesced_count == 0 for row in rows)


async def test_typed_gap_coalescing_keeps_identity_and_excludes_evidence(test_db):
    await accounts(test_db)
    base = {"kind": "gap", "peer": -100123, "key": "gap", "at": 299}
    await enqueue_fact(test_db, 2, base)
    await enqueue_fact(test_db, 2, {**base, "at": 301})
    await enqueue_fact(test_db, 2, {**base, "at": 301})
    for message_id in (5, 6):
        await enqueue_fact(test_db, 2, {**base, "kind": "evidence_candidate", "message_id": message_id})
    await test_db.commit()
    rows = list((await test_db.scalars(select(TelegramEventInbox).order_by(TelegramEventInbox.id))).all())
    assert len(rows) == 4
    assert rows[0].coalesced_count == 1 and json.loads(rows[0].payload_json)["at"] == 301
    assert rows[1].state == "completed"
    assert [row.state for row in rows[2:]] == ["pending", "pending"]
    assert all(row.lane == "listener_fact" for row in rows)


async def test_disabled_flag_keeps_legacy_claims_without_short_lease(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", False)
    await accounts(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await test_db.commit()
    row = await inbox.claim(test_db, [2])
    assert row.claim_owner is None and row.claim_lease_expires_at is None
    await inbox.finish(test_db, row.id)
    assert row.checkpoint_stage is None


async def test_disabled_p1_uses_independent_legacy_inbox_settings(monkeypatch):
    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", False)
    monkeypatch.setattr(settings, "P1_INBOX_CLAIM_BATCH_SIZE", 7)
    monkeypatch.setattr(settings, "P1_INBOX_CLAIM_LEASE_SECONDS", 300)
    monkeypatch.setattr(settings, "LEGACY_INBOX_CLAIM_BATCH_SIZE", 32)
    monkeypatch.setattr(settings, "LEGACY_INBOX_CLAIM_LEASE_SECONDS", 90)

    assert settings.growth_inbox_claim_batch_size == 32
    assert settings.growth_inbox_claim_lease_seconds == 90

    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", True)
    assert settings.growth_inbox_claim_batch_size == 7
    assert settings.growth_inbox_claim_lease_seconds == 300


async def test_metrics_fixed_baseline_actual_share_and_checkpoint_counter(test_db, monkeypatch):
    from app.core import metrics
    from app.core.account.control_models import AccountBudgetSnapshot

    await accounts(test_db)
    now = datetime.utcnow()
    baseline = now - timedelta(hours=1)
    monkeypatch.setattr(metrics, "inbox_baseline_at", lambda: baseline)
    for identity, received in (("old", now - timedelta(days=2)), ("new", now - timedelta(seconds=60))):
        test_db.add(TelegramEventInbox(account_id=2, event_key=identity, kind="message",
                                      payload_json="{}", state="pending", received_at=received))
    # Default business columns on a historical local fact must not misclassify it.
    test_db.add(TelegramEventInbox(account_id=2, event_key="legacy-fact", kind="listener_fact",
                                  lane="business", payload_json="{}", state="pending"))
    for lane, limit in (("ad", 35), ("survival", 15), ("critical", 25), ("sync", 20), ("routine", 5), ("listener", 2)):
        test_db.add(AccountBudgetSnapshot(account_id=2, window="hour", window_start=now,
            lane=lane, limit=limit, used=35 if lane == "sync" else 0, remaining=0,
            ttl_seconds=3600, read_factor=1, loan_json=json.dumps({"sync": 70, "sync_borrowed": 15}), snapshot_at=now))
    await test_db.commit()
    cache = SimpleNamespace(get=AsyncMock(return_value=None), zrange=AsyncMock(return_value=[]), set=AsyncMock(),
        hgetall=AsyncMock(return_value={"vanguard_qualification_checkpoint_events_total|hit": "3",
                                      "vanguard_qualification_checkpoint_events_total|miss": "1"}))
    result = await metrics.aggregate(test_db, cache)
    gauges = {(g["name"], tuple(g["labels"])): g["value"] for g in result["gauges"]}
    assert 60 <= gauges[("vanguard_queue_lag_seconds", ("inbox",))] < 120
    assert gauges[("vanguard_inbox_baseline_pending", ())] == 1
    assert gauges[("vanguard_inbox_lane_pending", ("listener_fact",))] == 1
    assert gauges[("vanguard_sync_actual_percent", ())] == 35
    assert gauges[("vanguard_sync_borrowed_reads", ())] == 15
    assert gauges[("vanguard_qualification_checkpoint_hit_ratio", ())] == 0.75
    assert ("vanguard_sync_wait_p95_seconds", ()) not in gauges
