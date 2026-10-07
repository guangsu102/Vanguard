"""Real SDK account recovery stays frozen while independent channels progress."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from telethon._updates.messagebox import ENTRY_ACCOUNT, PossibleGap
from telethon.tl import types

from app.core.account.listener_budget_wait import wait_global
from app.core.account.listener_checkpoint import ListenerSession
from app.core.account.listener_journal import JournalQueue
from app.core.account.rpc_governor import RpcDeferred
from tests.unit.test_listener_journal import client, event
from tests.unit.test_selective_listener import policy, chatter, deletion

pytestmark = pytest.mark.asyncio


def private():
    value = event()
    value.updates = [types.UpdateNewMessage(types.Message(id=123, peer_id=types.PeerUser(44),
        from_id=types.PeerUser(44), date=datetime.now(UTC), message='keep private'), pts=11, pts_count=1)]
    return value


async def test_channel_progress_preserves_global_request_cursors_and_private_replay(tmp_path):
    c = client(tmp_path);q = c.session.journal
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, 'account gap')
    c._message_box.seq = 7
    before = (c._message_box.map[ENTRY_ACCOUNT].pts, c._message_box.seq, c._message_box.date)
    pending_gap = PossibleGap(deadline=999999999, updates=[private().updates[0]])
    c._message_box.possible_gaps[ENTRY_ACCOUNT] = pending_gap
    q.put_nowait(private())
    seq_envelope = event(2, 12);seq_envelope.seq = 8
    q.put_nowait(seq_envelope)
    channel = event();channel.date += timedelta(hours=3)
    q.put_nowait(channel)
    c.is_connected = Mock(side_effect=[True, False]);c._call = AsyncMock(side_effect=AssertionError('no RPC'))
    await wait_global(c, RpcDeferred('telegram_read_budget', 3600))
    assert (c._message_box.map[ENTRY_ACCOUNT].pts, c._message_box.seq, c._message_box.date) == before
    assert c._message_box.map[22].pts == 11
    assert c._message_box.possible_gaps[ENTRY_ACCOUNT] is pending_gap and len(pending_gap.updates) == 1
    assert q.snapshot()['states'] == {'checkpointed': 1, 'pending': 2}
    assert c.session.get_update_state(0).pts == 10 and c.session.get_update_state(0).seq == 7
    c._call.assert_not_awaited();c.session.close()
    reopened = ListenerSession(str(tmp_path/'listener'));restored = JournalQueue(reopened)
    assert restored.qsize() == 2
    assert isinstance(restored.get_nowait().updates[0], types.UpdateNewMessage)
    assert restored.get_nowait().seq == 8
    reopened.close()


async def test_full_protocol_queue_compacts_without_losing_critical_facts_or_advancing_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(JournalQueue, 'PAGE_SIZE', 10)
    c = client(tmp_path);q = c.session.journal;q.policy = policy
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, 'account gap')
    for mid in range(1, 101):
        value = chatter(mid);value.seq = mid
        q.put_nowait(value)
    q.put_nowait(deletion([7]))
    c.is_connected = Mock(side_effect=[True, False])
    await wait_global(c, RpcDeferred('telegram_read_budget', 3600))
    assert q.qsize() <= 1
    assert c._message_box.seq == 0 and c._message_box.map[22].pts == 10
    assert c._message_box.map[ENTRY_ACCOUNT].pts == 10
    assert json.loads(q.facts()[0][1])['message_id'] == 7
    c.session.close()
    reopened = ListenerSession(str(tmp_path/'listener'))
    assert len(JournalQueue(reopened).facts()) == 1
    reopened.close()


async def test_cancelled_channel_dispatch_replays_from_old_durable_cursor(tmp_path):
    c = client(tmp_path, AsyncMock(side_effect=asyncio.CancelledError))
    await c._save_states_and_entities();c.session.save()
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, 'account gap')
    c.session.journal.put_nowait(event());c.is_connected = Mock(return_value=True)
    with pytest.raises(asyncio.CancelledError):
        await wait_global(c, RpcDeferred('telegram_read_budget', 3600))
    c.session.close()
    reopened = ListenerSession(str(tmp_path/'listener'));q = JournalQueue(reopened)
    assert reopened.get_update_state(22).pts == 10
    assert q.qsize() == 1 and q.get_nowait().updates[0].message.id == 1
    reopened.close()


async def test_channel_with_its_own_gap_retains_raw_until_real_difference(tmp_path):
    c = client(tmp_path);q = c.session.journal
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, 'account gap')
    c._message_box.try_begin_get_diff(22, 'channel gap')
    q.put_nowait(event());c.is_connected = Mock(side_effect=[True, False])
    await wait_global(c, RpcDeferred('telegram_read_budget', 3600))
    assert c._message_box.map[22].pts == 10
    assert q.snapshot()['states'] == {'pending': 1}
    c.session.close()

async def test_mixed_account_channel_envelope_stays_queued_whole(tmp_path):
    c = client(tmp_path);q = c.session.journal
    c._message_box.try_begin_get_diff(ENTRY_ACCOUNT, 'account gap')
    mixed = event();mixed.updates.append(private().updates[0]);q.put_nowait(mixed)
    c.is_connected = Mock(side_effect=[True, False])
    await wait_global(c, RpcDeferred('telegram_read_budget', 0.01))
    assert q.qsize() == 1 and q.snapshot()['states'] == {'pending': 1}
    assert c._message_box.map[22].pts == c._message_box.map[ENTRY_ACCOUNT].pts == 10
    c.session.close()
