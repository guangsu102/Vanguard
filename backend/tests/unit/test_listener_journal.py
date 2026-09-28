import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon._updates.session import ChannelState, SessionState
from telethon.tl import types

from app.core.account.listener_checkpoint import ListenerSession, install_listener_checkpoint
from app.core.account.listener_journal import JournalQueue
from app.core.account.rpc_governor import RpcDeferred, install_governor


def event(mid=1, pts=11):
    now = datetime.now(UTC)
    msg = types.Message(
        id=mid,
        peer_id=types.PeerChannel(22),
        from_id=types.PeerUser(44),
        date=now,
        message="offline",
    )
    return types.Updates(
        [types.UpdateNewChannelMessage(msg, pts=pts, pts_count=1)], [], [], now, seq=0
    )


def client(tmp_path, dispatch=None):
    session = ListenerSession(str(tmp_path / "listener"))
    c = TelegramClient(session, 1, "offline", sequential_updates=True)
    c._message_box.load(
        SessionState(99, 1, False, 10, 0, int(datetime.now(UTC).timestamp()), 0, None),
        [ChannelState(22, 10)],
    )
    c._mb_entity_cache.set_self_user(99, False, 0)
    c._dispatch_update = dispatch or AsyncMock()
    install_listener_checkpoint(c)
    c._vanguard_ingest_ready.set()
    return c


@pytest.mark.asyncio
async def test_raw_queue_restarts_without_advancing_unprocessed_cursor(tmp_path):
    c = client(tmp_path)
    await c._save_states_and_entities()
    c.session.save()
    c._updates_queue.put_nowait(event())
    c._updates_queue.get_nowait()  # crash after take but before ordered ingestion
    c.session.close()
    reopened = ListenerSession(str(tmp_path / "listener"))
    q = JournalQueue(reopened)
    assert q.qsize() == 1 and q.get_nowait().updates[0].message.id == 1
    assert reopened.get_update_state(22).pts == 10
    reopened.close()


@pytest.mark.asyncio
async def test_raw_ack_and_cursor_follow_successful_batch(tmp_path):
    c = client(tmp_path)
    c._updates_queue.put_nowait(event())
    envelope = c._updates_queue.get_nowait()
    result = []
    users, chats = c._message_box.process_updates(envelope, c._mb_entity_cache, result)
    for update in await c._preprocess_updates(result, users, chats):
        await c._dispatch_update(update)
    assert c.session.get_update_state(22).pts == 11
    assert c.session.journal.snapshot()["states"] == {"checkpointed": 1}
    c.session.close()
    reopened = ListenerSession(str(tmp_path / "listener"))
    assert JournalQueue(reopened).qsize() == 0
    reopened.close()


@pytest.mark.asyncio
async def test_cancelled_batch_replays_raw_with_old_checkpoint(tmp_path):
    c = client(tmp_path)
    await c._save_states_and_entities()
    c.session.save()
    c._updates_queue.put_nowait(event())
    result = []
    users, chats = c._message_box.process_updates(
        c._updates_queue.get_nowait(), c._mb_entity_cache, result
    )
    await c._preprocess_updates(result, users, chats)
    await c._save_states_and_entities()
    c.session.close()
    reopened = ListenerSession(str(tmp_path / "listener"))
    assert reopened.get_update_state(22).pts == 10
    assert JournalQueue(reopened).qsize() == 1
    reopened.close()


@pytest.mark.asyncio
async def test_empty_global_difference_commits_and_close_flushes(tmp_path):
    c = client(tmp_path)
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, "offline")
    now = datetime.now(UTC)
    result = c._message_box.apply_difference(
        types.updates.DifferenceEmpty(date=now, seq=7), c._mb_entity_cache
    )
    await c._preprocess_updates(*result)
    assert c.session.get_update_state(0).seq == 7
    c._message_box.seq = 8
    await c._save_states_and_entities()
    c.session.close()  # actual SDK shutdown path, no preceding save()
    reopened = ListenerSession(str(tmp_path / "listener"))
    assert reopened.get_update_state(0).seq == 8
    reopened.close()


@pytest.mark.asyncio
async def test_gap_raw_remains_pending_until_sdk_reconciles(tmp_path):
    c = client(tmp_path)
    c._message_box.try_begin_get_diff(22, "offline gap")
    c._updates_queue.put_nowait(event())
    c._updates_queue.get_nowait()
    await c._preprocess_updates([], [], [])
    assert c.session.journal.snapshot()["states"] == {"pending": 1}
    c._updates_queue.put_nowait(event(2, 12))
    c.session.journal.reconciliation_required()
    await c._preprocess_updates([], [], [])
    assert c.session.journal.snapshot()["states"] == {"reconciliation_required": 1, "pending": 1}
    c.session.close()


@pytest.mark.asyncio
async def test_native_global_wait_preserves_request_and_drains_after_difference(
    monkeypatch, tmp_path
):
    from app.core.account import listener_budget_wait as module

    connected = True
    calls = []

    async def dispatch(update):
        nonlocal connected
        if isinstance(update, types.UpdateNewChannelMessage):
            calls.append(update.message.id)
            connected = False

    c = client(tmp_path, dispatch)
    c._authorized = True
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, "offline catch-up")
    c.is_connected = Mock(side_effect=lambda: connected)
    now = datetime.now(UTC)
    replies = [
        types.updates.DifferenceSlice([], [], [], [], [], types.updates.State(11, 0, now, 1, 0)),
        types.updates.DifferenceEmpty(now, 2),
    ]
    c._call = AsyncMock(side_effect=replies)
    network = c._call
    guard = Obj(
        before=AsyncMock(side_effect=[None, RpcDeferred("telegram_read_budget", 60), None]),
        failed=AsyncMock(),
        succeeded=AsyncMock(),
    )

    async def wait(client, error):
        assert client.session.get_update_state(0).pts == 11
        client._updates_queue.put_nowait(event())
        assert client._message_box.seq == 1 and client._message_box.map[ENTRY_ACCOUNT].pts == 11

    monkeypatch.setattr(module, "wait_global", wait)
    c._vanguard_listener_pause = Obj(
        request=Mock(side_effect=AssertionError("must not disconnect"))
    )
    install_governor(c, guard)
    c._updates_handle = asyncio.create_task(c._update_loop())
    await asyncio.wait_for(c._updates_handle, 2)
    assert calls == [1] and network.await_count == 2
    assert network.await_args_list[1].args[1].pts == 11
    assert c.session.get_update_state(0).seq == 2
    assert c.session.journal.snapshot()["states"] == {"checkpointed": 1}
    c.session.close()
