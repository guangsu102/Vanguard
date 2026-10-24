"""Deleted SDK events remain local and preserve batch transaction boundaries."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account import event_inbox as inbox
from app.core.account.models import TelegramAccount
from app.core.worker_status import TelegramEventInbox

pytestmark = pytest.mark.asyncio


async def account(db):
    db.add(TelegramAccount(id=2, identifier="local-deleted", session_name="local-deleted"))
    await db.commit()


@pytest.mark.parametrize("p1_enabled", [False, True])
async def test_local_batch_routes_deleted_payload_without_losing_ids(test_db, monkeypatch, p1_enabled):
    """Deleted SDK payloads use ``deleted_ids`` and never enter apply_fact."""
    from app.core.config import settings
    from app.workers import telegram_worker as worker_module
    from app.workers.telegram_worker import TelegramWorker, TelegramWorkerRole

    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", p1_enabled)
    await account(test_db)
    test_db.add_all([
        TelegramEventInbox(
            account_id=2,
            event_key="local-fact-before-delete",
            kind="listener_fact",
            chat_id=-100123,
            payload_json=json.dumps({"kind": "gap", "peer": -100123, "at": 1}),
            state="pending",
            next_attempt_at=datetime.utcnow(),
        ),
        TelegramEventInbox(
            account_id=2,
            event_key="deleted-preserve-all",
            kind="deleted",
            chat_id=-100123,
            payload_json=json.dumps({
                "kind": "deleted",
                "chat_id": -100123,
                "id": None,
                "deleted_ids": [11, 12, 13],
                "sender": {},
                "date": None,
            }),
            state="pending",
            next_attempt_at=datetime.utcnow(),
        ),
        TelegramEventInbox(
            account_id=2,
            event_key="local-fact-after-delete",
            kind="listener_fact",
            chat_id=-100123,
            payload_json=json.dumps({"kind": "gap", "peer": -100123, "at": 2, "fail": True}),
            state="pending",
            next_attempt_at=datetime.utcnow(),
        ),
    ])
    await test_db.commit()

    @asynccontextmanager
    async def same_session():
        yield test_db

    observed = AsyncMock()
    applied = []

    async def apply_fact(_db, _account_id, fact):
        applied.append(fact)
        if fact.get("fail"):
            raise ValueError("bad_local_fact")

    monkeypatch.setattr(worker_module, "get_db_session", same_session)
    monkeypatch.setattr("app.core.account.listener_facts.apply_fact", apply_fact)
    monkeypatch.setattr("app.modules.acquisition.adaptive_frequency.queue_deleted_observation", observed)
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="deleted-batch")
    worker._account_pool = object()
    worker._growth_listener_sessions = {2: "listener"}

    assert await worker._process_growth_inbox_local_batch(limit=3)
    await test_db.commit()
    rows = (await test_db.scalars(select(TelegramEventInbox).order_by(TelegramEventInbox.id))).all()
    assert [row.state for row in rows] == ["completed", "completed", "pending"]
    assert [fact["kind"] for fact in applied] == ["gap", "gap"]
    observed.assert_awaited_once()
    deleted = observed.await_args.args[2]
    assert deleted.chat_id == -100123 and deleted.deleted_ids == [11, 12, 13]
    assert observed.await_args.kwargs == {"confirmed": True}


@pytest.mark.parametrize("p1_enabled", [False, True])
@pytest.mark.parametrize("rollback_batch", [False, True])
async def test_local_batch_actual_deletions_keep_savepoint_and_offline_boundaries(
    test_db, monkeypatch, p1_enabled, rollback_batch,
):
    """Exercise the real internally committing helper, including a failed fact."""
    from app.core.config import settings
    from app.workers import telegram_worker as worker_module
    from app.workers.telegram_worker import TelegramWorker, TelegramWorkerRole
    from tests.unit.test_adaptive_group_frequency import NOW, TARGET, delivery
    from tests.unit.test_event_cycle_survival import scenario

    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", p1_enabled)
    owner, _, _, state, member, first = await scenario(test_db)
    from app.modules.acquisition.models import AdCampaign
    campaign = await test_db.get(AdCampaign, first.ad_campaign_id)
    second = await delivery(test_db, owner, campaign, state, NOW + timedelta(seconds=1), status="pending")
    third = await delivery(test_db, owner, campaign, state, NOW + timedelta(seconds=2), status="pending")
    second.group_id = third.group_id = member.group_id
    await inbox.enqueue(test_db, owner.id, "deleted", Obj(
        id=900, chat_id=TARGET, deleted_ids=[first.telegram_message_id, second.telegram_message_id],
        sender=None, message=None,
    ))
    malformed = TelegramEventInbox(
        account_id=owner.id, event_key="malformed-between-deletions", kind="listener_fact",
        chat_id=TARGET, payload_json=json.dumps({"kind": "gap"}), state="pending",
        next_attempt_at=datetime.utcnow(),
    )
    typed = TelegramEventInbox(
        account_id=owner.id, event_key="typed-deletion", kind="listener_fact", chat_id=TARGET,
        payload_json=json.dumps({"kind": "deleted", "peer": TARGET, "message_id": third.telegram_message_id}),
        state="pending", next_attempt_at=datetime.utcnow(),
    )
    unknown = TelegramEventInbox(
        account_id=owner.id, event_key="unresolved-join", kind="join", chat_id=TARGET,
        payload_json="{}", state="reconciliation_required", external_attempted=True,
    )
    later = TelegramEventInbox(
        account_id=owner.id, event_key="later-message", kind="message", chat_id=TARGET,
        payload_json="{}", state="pending", next_attempt_at=datetime.utcnow(),
    )
    test_db.add_all([malformed, typed, unknown, later])
    await test_db.commit()
    original_survival_statuses = [row.survival_status for row in (first, second, third)]
    deleted = await test_db.scalar(select(TelegramEventInbox).where(TelegramEventInbox.kind == "deleted"))

    @asynccontextmanager
    async def same_session():
        yield test_db

    monkeypatch.setattr(worker_module, "get_db_session", same_session)
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="offline-deleted-batch")
    worker._account_pool = Obj(get_account_by_id=AsyncMock(side_effect=AssertionError("local facts need no client")))
    worker._growth_runtime_account_ids = {owner.id}
    assert not worker._growth_listener_sessions

    assert await worker._process_growth_inbox_once(local_only=True)
    if rollback_batch:
        await test_db.rollback()
    else:
        await test_db.commit()
    for row in (deleted, malformed, typed, unknown, later, first, second, third):
        await test_db.refresh(row)
    assert [row.state for row in (deleted, malformed, typed)] == (
        ["pending", "pending", "pending"] if rollback_batch else ["completed", "pending", "completed"]
    )
    assert [row.survival_status for row in (first, second, third)] == (
        original_survival_statuses if rollback_batch else ["deleted", "deleted", "deleted"]
    )
    if not rollback_batch:
        assert malformed.last_error == "KeyError"
        assert json.loads(malformed.payload_json) == {"kind": "gap"}
        assert all(row.claim_owner is None for row in (deleted, malformed, typed))
    assert unknown.state == "reconciliation_required" and unknown.external_attempted
    assert later.state == "pending" and later.attempts == 0
    worker._account_pool.get_account_by_id.assert_not_awaited()
