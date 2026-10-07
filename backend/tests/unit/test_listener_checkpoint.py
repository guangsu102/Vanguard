import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types.updates import State
from app.core.account.listener_checkpoint import ListenerSession, install_listener_checkpoint


def state(pts):
    return State(pts=pts, qts=0, date=datetime.now(timezone.utc), seq=pts, unread_count=0)


def test_cursor_is_not_committed_mid_batch_or_on_disconnect(tmp_path):
    path=str(tmp_path / 'listener')
    session=ListenerSession(path)
    session.set_update_state(0,state(10));session.save()
    session.pending_updates=2
    session.set_update_state(0,state(20));session.save()
    session.close()
    reopened=ListenerSession(path)
    assert reopened.get_update_state(0).pts==10
    reopened.close()


@pytest.mark.asyncio
async def test_checkpoint_advances_only_after_all_sdk_callbacks_persist(tmp_path):
    session=ListenerSession(str(tmp_path/'listener'))
    session.set_update_state(0,state(10));session.save()
    client=Obj(session=session, _preprocess_updates=AsyncMock(side_effect=lambda u,users,chats:u),
               _dispatch_update=AsyncMock(), _save_states_and_entities=AsyncMock())
    install_listener_checkpoint(client)
    batch=await client._preprocess_updates([1,2],[],[])
    session.set_update_state(0,state(20))
    await client._dispatch_update(batch[0]);session.save()
    assert session.get_update_state(0).pts==10
    await client._dispatch_update(batch[1])
    assert session.get_update_state(0).pts==20
    client._save_states_and_entities.assert_awaited_once()
    session.close()


@pytest.mark.asyncio
async def test_cancelled_ingestion_never_advances_cursor(tmp_path):
    session=ListenerSession(str(tmp_path/'listener'))
    session.set_update_state(0,state(10));session.save()
    client=Obj(session=session,_preprocess_updates=AsyncMock(side_effect=lambda u,users,chats:u),
               _dispatch_update=AsyncMock(side_effect=asyncio.CancelledError),
               _save_states_and_entities=AsyncMock())
    install_listener_checkpoint(client)
    await client._preprocess_updates([1],[],[])
    session.set_update_state(0,state(20))
    with pytest.raises(asyncio.CancelledError):await client._dispatch_update(1)
    session.save()
    assert session.get_update_state(0).pts==10
    session.close()


@pytest.mark.asyncio
async def test_recovered_updates_wait_until_ingestion_handler_is_attached(tmp_path):
    session=ListenerSession(str(tmp_path/'listener'))
    client=Obj(session=session,_preprocess_updates=AsyncMock(side_effect=lambda u,users,chats:u),
               _dispatch_update=AsyncMock(),_save_states_and_entities=AsyncMock(),
               add_event_handler=lambda *args: None)
    install_listener_checkpoint(client)
    await client._preprocess_updates([1],[],[])
    task=asyncio.create_task(client._dispatch_update(1))
    await asyncio.sleep(0)
    assert not task.done() and session.pending_updates==1
    client._vanguard_ingest_ready.set()
    await task
    assert session.pending_updates==0
    session.close()

@pytest.mark.asyncio
async def test_real_sdk_events_reach_inbox_without_network(monkeypatch, test_db, tmp_path):
    from contextlib import asynccontextmanager
    from sqlalchemy import select
    from telethon import TelegramClient
    from telethon.tl import types
    from app.core.account.models import TelegramAccount
    from app.core.worker_status import TelegramEventInbox, TelegramWorkerRole
    from app.workers import telegram_worker as module
    test_db.add(TelegramAccount(id=2,identifier="sdk-inbox",session_name="sdk-inbox"))
    await test_db.commit()
    @asynccontextmanager
    async def db_session():
        yield test_db
        await test_db.commit()
    monkeypatch.setattr(module,"get_db_session",db_session)
    session=ListenerSession(str(tmp_path/'sdk'))
    client=TelegramClient(session,1,'offline-test-placeholder',sequential_updates=True)
    client._call=AsyncMock(side_effect=AssertionError('network forbidden'))
    client._mb_entity_cache.set_self_user(9999001,False,0)
    client._save_states_and_entities=AsyncMock()
    install_listener_checkpoint(client)
    worker=module.TelegramWorker(TelegramWorkerRole.GROWTH_USER)
    worker._attach_growth_event_handlers(Obj(client=client,account_id=2,session_name='sdk-inbox'))
    now=datetime.now(timezone.utc)
    msg=types.Message(id=101,peer_id=types.PeerUser(44),from_id=types.PeerUser(44),date=now,message='offline sample')
    join=types.MessageService(id=103,peer_id=types.PeerChat(55),from_id=types.PeerUser(44),date=now,action=types.MessageActionChatAddUser(users=[66]))
    updates=[types.UpdateNewMessage(msg,pts=1,pts_count=1),types.UpdateNewMessage(join,pts=2,pts_count=1),types.UpdateDeleteMessages(messages=[101],pts=3,pts_count=1)]
    users=[types.User(id=44,first_name='Offline',access_hash=1)]
    batch=await client._preprocess_updates(updates,users,[])
    assert session.pending_updates==3
    for update in batch:await asyncio.wait_for(client._dispatch_update(update),timeout=2)
    rows=list((await test_db.scalars(select(TelegramEventInbox))).all())
    assert len(rows)==3 and {r.kind for r in rows}=={'message','join','deleted'}
    for update in await client._preprocess_updates([updates[0]],users,[]):await client._dispatch_update(update)
    assert len(list((await test_db.scalars(select(TelegramEventInbox))).all()))==3
    assert session.pending_updates==0
    client._call.assert_not_awaited()
    session.close()
