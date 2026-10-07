import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from telethon.tl import types

from app.core.account import event_inbox
from app.core.account.listener_checkpoint import ListenerSession
from app.core.account.listener_facts import enqueue_fact
from app.core.account.listener_interest import project
from app.core.account.listener_journal import JournalQueue
from app.core.worker_status import TelegramEventInbox
from tests.unit.test_bounded_listener import process
from tests.unit.test_growth_event_inbox import account
from tests.unit.test_listener_journal import client, event

PEER = -(10**12 + 22)


def policy(**extra):
    return {
        "selective": True,
        "loaded_at": time.monotonic(),
        "ordinary_required": False,
        "owned_chats": set(),
        "ads": {PEER: {7}},
        "groups": {PEER: {"joined_at": "2026-01-01T00:00:00"}},
        "verification": {},
        "account_id": 2,
        "self_id": 99,
        **extra,
    }


def chatter(mid, channel=22):
    value = event(mid, mid + 10)
    value.updates[0].message.peer_id = types.PeerChannel(channel)
    value.updates[0].message.reply_to = types.MessageReplyHeader(reply_to_msg_id=1)
    value.updates[0].message.message = "unrelated bot reply " * 500
    value.users = [types.User(id=44, bot=True, bot_info_version=1)]
    return value


def deletion(ids, channel=22):
    return types.UpdateShort(
        types.UpdateDeleteChannelMessages(channel, ids, 123, 1), datetime.now(UTC)
    )


@pytest.mark.asyncio
async def test_ten_thousand_bot_replies_do_not_persist_or_accumulate_on_a_gap(tmp_path):
    c = client(tmp_path)
    q = c.session.journal
    q.policy = policy
    c._message_box.try_begin_get_diff(22, "real gap")
    for mid in range(1, 10001):
        q.put_nowait(chatter(mid))
        await process(c, q.get_nowait())
    assert q.snapshot()["states"] == {}
    assert q.snapshot()["business_facts_pending"] == 0
    assert q.observed == {}
    c.session.close()


@pytest.mark.asyncio
async def test_protocol_memory_overflow_is_bounded_and_never_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(JournalQueue, "PAGE_SIZE", 10)
    c = client(tmp_path)
    q = c.session.journal
    q.policy = policy
    for mid in range(1, 1001):
        value = chatter(mid, 22 + mid % 30)
        value.seq = mid  # Cannot combine distinct account sequence envelopes.
        q.put_nowait(value)
    assert q.qsize() == 10
    assert len(q.protocol_recovery) <= 11
    assert q.snapshot()["states"] == {}
    assert c._message_box.seq == 0 and c._message_box.map[22].pts == 10
    q.put_nowait(deletion([7]))
    assert len(q.facts()) == 1  # Critical evidence survives even a full protocol buffer.
    c.session.close()
    reopened = ListenerSession(str(tmp_path / "listener"))
    restored = JournalQueue(reopened)
    assert json.loads(restored.facts()[0][1])["message_id"] == 7
    reopened.close()


@pytest.mark.asyncio
async def test_projection_migrates_old_chatter_and_preserves_deletion_before_release(tmp_path):
    c = client(tmp_path)
    q = c.session.journal
    for item in [chatter(1), deletion([7])]:
        q.put_nowait(item)
        q.get_nowait()
    q.reconciliation_required()
    q.policy = policy
    assert q.reconcile_batch() == 2
    assert q.snapshot()["states"] == {}
    assert json.loads(q.facts()[0][1])["message_id"] == 7
    c.session.close()


@pytest.mark.asyncio
async def test_pending_history_is_projected_even_when_sdk_cannot_read(tmp_path, monkeypatch):
    monkeypatch.setattr(JournalQueue, "PAGE_SIZE", 10)
    c = client(tmp_path)
    q = c.session.journal
    for mid in range(1, 51):
        q.put_nowait(chatter(mid))
    q.put_nowait(deletion([7]))
    assert q.qsize() == 10
    q.policy = policy
    assert q.reconcile_batch() == 51
    assert q.snapshot()["states"] == {}
    assert q.queued == set()
    assert len(q.facts()) == 1
    # Queued protocol entries still work after their raw rows were released.
    while not q.empty():
        await process(c, q.get_nowait())
    assert q.observed == {}
    c.session.close()


@pytest.mark.asyncio
async def test_fact_ack_cannot_remove_newer_merge_and_unknown_deletions_are_bounded(tmp_path):
    c = client(tmp_path)
    q = c.session.journal
    q.policy = policy
    q.put_nowait(deletion([100]))
    rows = q.facts()
    for start in range(200, 3000, 100):
        q.put_nowait(deletion(list(range(start, start + 100))))
    q.acknowledge_facts(rows)
    facts = q.facts()
    assert len(facts) == 1
    data = json.loads(facts[0][1])
    assert len(data["ids"]) == 512 and data["overflow"] is True
    assert q.snapshot()["states"] == {}
    c.session.close()


def test_verification_only_keeps_a_recent_active_group_cue_and_never_body():
    item = chatter(1)
    m = item.updates[0].message
    m.message = "请完成验证 captcha"
    joined = (datetime.utcnow() - timedelta(minutes=2)).isoformat()
    projected, ignored, facts = project(item, policy(verification={PEER: {"joined_at": joined}}))
    assert ignored == [0] and len(facts) == 1 and facts[0]["kind"] == "verification"
    assert projected.updates[0].message.message == ""
    assert project(item, policy())[2] == []
    m.date = datetime.now(UTC) - timedelta(hours=1)
    assert project(item, policy(verification={PEER: {"joined_at": joined}}))[2] == []


def test_private_owned_and_enabled_consumers_preserved_unrelated_replies_discarded():
    for kind in ("private", "owned", "enabled", "stale"):
        item = chatter(1)
        p = policy()
        if kind == "private":
            item.updates[0].message.peer_id = types.PeerUser(44)
        if kind == "owned":
            p["owned_chats"] = {PEER}
        if kind == "enabled":
            p["ordinary_required"] = True
        if kind == "stale":
            p["loaded_at"] -= 100
        assert project(item, p)[1] == [], kind
    assert project(chatter(1), policy())[1] == [0]


def test_missing_channel_identity_does_not_match_channel_message_id():
    item = types.UpdateShort(types.UpdateDeleteMessages([7], 10, 1), datetime.now(UTC))
    assert project(item, policy())[2] == []
    facts = project(item, policy(ads={-22: {7}}))[2]
    assert len(facts) == 1 and facts[0]["peer"] == -22


def test_channel_sync_gap_is_not_an_extra_mature_survival_check():
    item = types.UpdateShort(types.UpdateChannelTooLong(22), datetime.now(UTC))
    facts = project(item, policy())[2]
    assert len(facts) == 1 and facts[0]["kind"] == "gap"
    assert "message_id" not in facts[0]  # Not an ad deletion or survival observation.


def test_own_historical_join_does_not_become_a_negative_membership_event():
    joined = datetime.now(UTC) - timedelta(days=4)
    message = types.MessageService(
        id=88,
        peer_id=types.PeerChannel(22),
        from_id=types.PeerUser(99),
        date=joined,
        action=types.MessageActionChatAddUser([99]),
    )
    item = types.UpdateShort(types.UpdateNewChannelMessage(message, 123, 1), datetime.now(UTC))
    facts = project(item, policy())[2]
    assert facts[0]["kind"] == "verification" and facts[0]["at"] == joined.timestamp()
    message.action = types.MessageActionChatDeleteUser(99)
    assert project(item, policy())[2][0]["kind"] == "membership"


@pytest.mark.asyncio
async def test_local_facts_deduplicate_retry_and_bypass_unknown_external_actions(test_db):
    await account(test_db)
    test_db.add(
        TelegramEventInbox(
            account_id=2,
            event_key="old",
            kind="message",
            chat_id=PEER,
            state="reconciliation_required",
            payload_json="{}",
        )
    )
    await test_db.commit()
    fact = {
        "kind": "deleted",
        "key": f"deleted:{PEER}:7",
        "peer": PEER,
        "message_id": 7,
        "at": time.time(),
    }
    await enqueue_fact(test_db, 2, fact)
    await enqueue_fact(test_db, 2, fact)
    await test_db.commit()
    row = await event_inbox.claim(test_db, [2])
    assert row.kind == "listener_fact"
    await event_inbox.finish(test_db, row.id, RuntimeError("transient local failure"))
    await test_db.commit()
    assert row.state == "pending"
    row.state = "processing"
    await test_db.commit()
    await event_inbox.recover_interrupted(test_db)
    await test_db.commit()
    await test_db.refresh(row)
    assert row.state == "pending"
    all_rows = (await test_db.scalars(select(TelegramEventInbox))).all()
    assert (
        len(all_rows) == 2
        and next(r for r in all_rows if r.event_key == "old").state == "reconciliation_required"
    )


@pytest.mark.asyncio
async def test_only_our_deleted_ad_is_recorded_once_without_another_read(test_db):
    from app.core.account.listener_facts import apply_fact
    from tests.unit.test_adaptive_group_frequency import NOW, TARGET, delivery, setup

    a, _, campaign, _, state = await setup(test_db, mature=True)
    log = await delivery(test_db, a, campaign, state, NOW - timedelta(days=2))
    await test_db.commit()
    fact = {
        "kind": "deletion_candidates",
        "peer": TARGET,
        "ids": [log.telegram_message_id + 1],
        "at": time.time(),
    }
    await apply_fact(test_db, a.id, fact)
    assert log.survival_status == "survived"
    fact["ids"] = [log.telegram_message_id]
    await apply_fact(test_db, a.id, fact)
    assert log.survival_status == "deleted" and log.survival_stage == "complete"
    assert log.survival_check_due_at is None and log.survival_claim_token is None
    version = log.survival_version
    await test_db.commit()
    await apply_fact(test_db, a.id, fact)
    assert log.survival_claim_token is None and log.survival_version == version
