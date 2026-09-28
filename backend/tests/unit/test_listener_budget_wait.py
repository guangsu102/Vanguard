import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl import types
from telethon.tl.functions.updates import GetChannelDifferenceRequest, GetDifferenceRequest
from telethon._updates.session import SessionState, ChannelState

from app.core.account.listener_budget_wait import can_receive_while_waiting, receive_while_waiting
from app.core.account.rpc_governor import RpcDeferred, install_governor

pytestmark = pytest.mark.asyncio


def client_case():
    c = TelegramClient(MemorySession(), 1, 'offline', sequential_updates=True)
    c.is_connected = Mock(return_value=True)
    c.disconnect = AsyncMock()
    c._vanguard_durable_checkpoint = True
    c._message_box.load(SessionState(99, 1, False, 10, 0, int(datetime.now(timezone.utc).timestamp()), 0, None),
                        [ChannelState(11, 10), ChannelState(22, 10)])
    c._message_box.try_begin_get_diff(11, 'test missing channel update')
    c._mb_entity_cache.set_self_user(99, False, 0)
    c._preprocess_updates = AsyncMock(side_effect=lambda items, users, chats: items)
    c._dispatch_update = AsyncMock()
    return c


def envelope(channel, pts, message_id):
    now = datetime.now(timezone.utc)
    msg = types.Message(id=message_id, peer_id=types.PeerChannel(channel), from_id=types.PeerUser(44),
                        date=now, message='offline')
    return types.Updates([types.UpdateNewChannelMessage(msg, pts=pts, pts_count=1)], [], [], now, seq=0)


def request():
    return GetChannelDifferenceRequest(types.InputChannel(11, 1), types.ChannelMessagesFilterEmpty(), 10, 100)


async def test_unrelated_pushes_flow_but_gap_channel_cursor_and_request_stay_unchanged():
    c = client_case()
    req = request()
    await c._updates_queue.put(envelope(11, 11, 101))
    await c._updates_queue.put(envelope(22, 11, 201))
    await c._updates_queue.put(envelope(22, 11, 201))  # duplicate transport delivery
    c.is_connected.side_effect = [True, True, True, False]
    await receive_while_waiting(c, RpcDeferred('telegram_read_budget', 3600))
    c._dispatch_update.assert_awaited_once()
    assert c._dispatch_update.await_args.args[0].message.id == 201
    assert c._message_box.map[11].pts == req.pts == 10
    assert c._message_box.map[22].pts == 11
    assert 11 in c._message_box.getting_diff_for
    assert c._updates_queue.qsize() == 0
    c.disconnect.assert_not_awaited()


async def test_governor_resumes_same_channel_request_after_draining_push(monkeypatch):
    from app.core.account import listener_budget_wait as module
    c = client_case()
    req = request()
    await c._updates_queue.put(envelope(22, 11, 201))
    # Deterministic wake after processing one envelope; no clock sleep/network.
    async def drain(client, error):
        client.is_connected.side_effect = [True, False]
        await receive_while_waiting(client, error)
        client.is_connected.side_effect = None
        client.is_connected.return_value = True
    monkeypatch.setattr(module, 'receive_while_waiting', drain)
    original = AsyncMock(return_value=types.updates.ChannelDifferenceEmpty(pts=10, final=True))
    c._call = original
    governor = Obj(before=AsyncMock(side_effect=[RpcDeferred('telegram_read_budget', 100), None]),
                   failed=AsyncMock(), succeeded=AsyncMock())
    install_governor(c, governor)
    c._updates_handle = asyncio.current_task()
    result = await asyncio.wait_for(c._call(None, req), timeout=2)
    assert result.pts == 10 and c._message_box.map[11].pts == 10
    original.assert_awaited_once_with(None, req, ordered=False, flood_sleep_threshold=0)
    assert c._dispatch_update.await_count == 1 and c._vanguard_sync_wait is None


async def test_global_difference_and_non_durable_clients_do_not_use_channel_wait_path():
    c = client_case()
    assert can_receive_while_waiting(c, [request()])
    assert not can_receive_while_waiting(c, [GetDifferenceRequest(10, datetime.now(timezone.utc), 0)])
    c._vanguard_durable_checkpoint = False
    assert not can_receive_while_waiting(c, [request()])


async def test_cancelled_budget_wait_does_not_advance_gap_or_send_network():
    c = client_case()
    task = asyncio.create_task(receive_while_waiting(c, RpcDeferred('telegram_read_budget', 3600)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c._message_box.map[11].pts == 10
    c._dispatch_update.assert_not_awaited()


async def test_new_gap_remains_sdk_owned_without_dispatching_out_of_order():
    c = client_case()
    await c._updates_queue.put(envelope(22, 12, 202))
    await c._updates_queue.put(envelope(22, 11, 201))
    c.is_connected.side_effect = [True, True, False]
    await receive_while_waiting(c, RpcDeferred('telegram_read_budget', 3600))
    assert [a.args[0].message.id for a in c._dispatch_update.await_args_list] == [201, 202]
    assert c._message_box.map[22].pts == 12
    assert c._message_box.map[11].pts == 10

async def test_budget_wait_pushes_are_persisted_by_real_sdk_callbacks(monkeypatch, test_db, tmp_path):
    from contextlib import asynccontextmanager
    from sqlalchemy import select
    from app.core.account.listener_checkpoint import ListenerSession, install_listener_checkpoint
    from app.core.account.models import TelegramAccount
    from app.core.worker_status import TelegramEventInbox, TelegramWorkerRole
    from app.workers import telegram_worker as worker_module
    test_db.add(TelegramAccount(id=2, identifier='budget-inbox', session_name='budget-inbox'))
    await test_db.commit()
    @asynccontextmanager
    async def db_session():
        yield test_db
        await test_db.commit()
    monkeypatch.setattr(worker_module, 'get_db_session', db_session)
    session = ListenerSession(str(tmp_path/'budget-inbox'))
    c = TelegramClient(session, 1, 'offline', sequential_updates=True)
    c.is_connected = Mock(return_value=False)
    c._call = AsyncMock(side_effect=AssertionError('network forbidden'))
    c._message_box.load(SessionState(99, 1, False, 10, 0, int(datetime.now(timezone.utc).timestamp()), 0, None),
                        [ChannelState(11, 10), ChannelState(22, 10)])
    c._message_box.try_begin_get_diff(11, 'missing')
    c._mb_entity_cache.set_self_user(99, False, 0)
    install_listener_checkpoint(c)
    c.is_connected.side_effect = [True, True, False]
    worker = worker_module.TelegramWorker(TelegramWorkerRole.GROWTH_USER)
    worker._attach_growth_event_handlers(Obj(client=c, account_id=2, session_name='budget-inbox'))
    await c._updates_queue.put(envelope(11, 11, 101))
    await c._updates_queue.put(envelope(22, 11, 201))
    await receive_while_waiting(c, RpcDeferred('telegram_read_budget', 600))
    rows = list((await test_db.scalars(select(TelegramEventInbox))).all())
    assert len(rows) == 1 and rows[0].state == 'pending' and rows[0].chat_id == -1000000000022
    assert session.get_update_state(11).pts == 10 and session.get_update_state(22).pts == 11
    assert session.pending_updates == 0
    c._call.assert_not_awaited()
    session.close()


async def test_unsupported_sdk_uses_existing_safe_pause(monkeypatch):
    from app.core.account import listener_budget_wait as module
    c = client_case()
    monkeypatch.setattr(module, 'telethon_version', 'future-version')
    assert not can_receive_while_waiting(c, [request()])

async def test_business_read_does_not_clear_native_sync_wait_state():
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    c = client_case()
    waiting = {'started': 123.0, 'reason': 'telegram_read_budget', 'resume_at': 'later'}
    c._vanguard_sync_wait = waiting
    c._call = AsyncMock(return_value=Obj())
    install_governor(c, Obj(before=AsyncMock(), failed=AsyncMock(), succeeded=AsyncMock()))
    await c._call(None, ResolveUsernameRequest('offline'))
    assert c._vanguard_sync_wait is waiting
