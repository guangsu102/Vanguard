"""Real database boundaries for inbox replay, ordering and unknown outbound outcomes."""
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace as Obj

import pytest
from sqlalchemy import select

from app.core.account import event_inbox as inbox
from app.core.account.models import TelegramAccount
from app.core.account.rpc_governor import RpcDeferred

pytestmark = pytest.mark.asyncio


def event(message_id=1, chat_id=-100123):
    return Obj(id=message_id, chat_id=chat_id, sender_id=44, raw_text="ordinary incoming text",
               sender=Obj(id=44, first_name="Sample", username=None, last_name=None, bot=False),
               message=Obj(id=message_id, date=datetime.utcnow(), media=None), is_private=False)


async def account(db):
    db.add(TelegramAccount(id=2, identifier="inbox", session_name="inbox"))
    await db.commit()


async def test_duplicate_delivery_is_deduplicated_and_payload_restores_without_rpc(test_db):
    await account(test_db)
    incoming = event()
    assert await inbox.enqueue(test_db, 2, "message", incoming)
    assert not await inbox.enqueue(test_db, 2, "message", incoming)
    await test_db.commit()
    row = await inbox.claim(test_db, [2])
    restored = inbox.restore_event(json.loads(row.payload_json), None)
    assert restored.raw_text == incoming.raw_text and restored.message.date == incoming.message.date
    assert (await restored.get_sender()).id == 44
    await inbox.finish(test_db, row.id)
    assert row.state == "completed" and row.payload_json == "{}"
    assert not await inbox.enqueue(test_db, 2, "message", incoming)


async def test_budget_deferral_blocks_same_peer_but_other_peer_can_progress(test_db):
    await account(test_db)
    for message_id, chat in [(1, -100123), (2, -100123), (3, -100456)]:
        await inbox.enqueue(test_db, 2, "message", event(message_id, chat))
    await test_db.commit()
    first = await inbox.claim(test_db, [2])
    await inbox.finish(test_db, first.id, RpcDeferred("telegram_read_budget", 600))
    await test_db.commit()
    second = await inbox.claim(test_db, [2])
    assert second.chat_id == -100456
    assert first.state == "pending" and first.payload_json != "{}"


async def test_external_attempt_never_retries_even_for_later_budget_error(test_db):
    await account(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await test_db.commit()
    row = await inbox.claim(test_db, [2])
    row.external_attempted = True
    await inbox.finish(test_db, row.id, RpcDeferred("telegram_read_budget", 600))
    assert row.state == "reconciliation_required"
    assert await inbox.claim(test_db, [2]) is None


async def test_restart_resumes_pending_but_preserves_interrupted_claim_for_reconciliation(test_db):
    await account(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await inbox.enqueue(test_db, 2, "message", event(2, -100456))
    await test_db.commit()
    row = await inbox.claim(test_db, [2])
    await test_db.commit()
    assert await inbox.recover_interrupted(test_db) == 1
    await test_db.commit()
    assert row.state == "reconciliation_required"
    next_row = await inbox.claim(test_db, [2])
    assert next_row.chat_id == -100456


async def test_unknown_business_failure_is_retained_not_acknowledged(test_db):
    await account(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await test_db.commit()
    row = await inbox.claim(test_db, [2])
    await inbox.finish(test_db, row.id, TimeoutError())
    assert row.state == "reconciliation_required" and row.payload_json != "{}"
    stats = await inbox.inbox_snapshot(test_db)
    assert stats["states"]["reconciliation_required"] == 1


@pytest.mark.parametrize('local_only', [True, None])
async def test_local_facts_progress_while_unknown_chat_send_keeps_peer_order(test_db, local_only):
    from app.core.worker_status import TelegramEventInbox

    await account(test_db)
    await inbox.enqueue(test_db, 2, 'message', event())
    await test_db.commit()
    first = await inbox.claim(test_db, [2], local_only=False)
    first.external_attempted = True
    await inbox.finish(test_db, first.id, TimeoutError())
    await inbox.enqueue(test_db, 2, 'message', event(2))
    fact = TelegramEventInbox(account_id=2, event_key='local-permission', kind='listener_fact',
        chat_id=-100123, payload_json='{}', state='pending', next_attempt_at=datetime.utcnow())
    test_db.add(fact)
    await test_db.commit()
    claimed = await inbox.claim(test_db, [2], local_account_ids=[2], local_only=local_only)
    assert claimed.id == fact.id
    await inbox.finish(test_db, fact.id)
    await test_db.commit()
    assert first.state == 'reconciliation_required'
    assert await inbox.claim(test_db, [2], local_only=False) is None


async def test_local_fact_claim_batch_keeps_durable_order_and_claims_once(test_db):
    from app.core.worker_status import TelegramEventInbox

    await account(test_db)
    for index in range(4):
        test_db.add(TelegramEventInbox(
            account_id=2,
            event_key=f"fact-{index}",
            kind="listener_fact",
            chat_id=-100000 - index,
            payload_json=json.dumps({"kind": "gap", "peer": -100000 - index, "at": 1}),
            state="pending",
            next_attempt_at=datetime.utcnow(),
        ))
    await test_db.commit()

    rows = await inbox.claim_batch(test_db, [2], local_only=True, limit=3)
    assert [row.event_key for row in rows] == ["fact-0", "fact-1", "fact-2"]
    assert all(row.state == "processing" and row.attempts == 1 for row in rows)
    await test_db.commit()

    again = await inbox.claim_batch(test_db, [2], local_only=True, limit=3)
    assert [row.event_key for row in again] == ["fact-3"]


async def test_local_fact_batch_savepoint_keeps_good_facts_when_one_fails(test_db, monkeypatch):
    from app.core.worker_status import TelegramEventInbox
    from app.workers import telegram_worker as worker_module
    from app.workers.telegram_worker import TelegramWorker, TelegramWorkerRole

    await account(test_db)
    for index in range(3):
        test_db.add(TelegramEventInbox(
            account_id=2,
            event_key=f"savepoint-{index}",
            kind="listener_fact",
            chat_id=-200000 - index,
            payload_json=json.dumps({
                "kind": "gap", "peer": -200000 - index, "at": 1,
                "fail": index == 1,
            }),
            state="pending",
            next_attempt_at=datetime.utcnow(),
        ))
    await test_db.commit()

    @asynccontextmanager
    async def same_session():
        yield test_db

    async def apply_fact(db, account_id, fact):
        if fact.get("fail"):
            raise ValueError("bad_local_fact")

    monkeypatch.setattr(worker_module, "get_db_session", same_session)
    monkeypatch.setattr("app.core.account.listener_facts.apply_fact", apply_fact)
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="batch-test")
    worker._account_pool = object()
    worker._growth_listener_sessions = {2: "listener"}

    assert await worker._process_growth_inbox_local_batch(limit=3)
    await test_db.commit()
    rows = (await test_db.scalars(select(TelegramEventInbox).order_by(TelegramEventInbox.id))).all()
    assert [row.state for row in rows] == ["completed", "pending", "completed"]
    assert rows[0].payload_json.startswith('{"audit"')
    assert rows[1].last_error == "ValueError"
