import asyncio
import time
from types import SimpleNamespace as Obj
from datetime import datetime, UTC

import pytest
from telethon.tl import types
from telethon.tl.functions.updates import GetChannelDifferenceRequest
from telethon._updates.messagebox import State, ENTRY_ACCOUNT

from app.core.account.listener_journal import JournalQueue
from app.core.account.listener_protocol import compact, dependencies, satisfied
from app.core.account.listener_checkpoint import ListenerSession
from tests.unit.test_listener_journal import client, event


def policy():
    return {'loaded_at': time.monotonic(), 'ordinary_required': False, 'owned_chats': set()}


def ordinary(mid=1, pts=11, channel=22):
    value = event(mid, pts)
    value.updates[0].message.peer_id = types.PeerChannel(channel)
    value.users = [types.User(id=44, bot=False)]
    value.updates[0].message.message = 'irrelevant ' * 1000
    return value


async def process(c, envelope):
    result = []
    users, chats = c._message_box.process_updates(envelope, c._mb_entity_cache, result)
    for update in await c._preprocess_updates(result, users, chats):
        await c._dispatch_update(update)


@pytest.mark.asyncio
async def test_unrelated_channel_gap_does_not_retain_completed_event(tmp_path):
    c = client(tmp_path)
    c._message_box.map[44] = State(10, asyncio.get_running_loop().time()+900)
    c._message_box.try_begin_get_diff(44, 'unrelated channel')
    c._updates_queue.put_nowait(event())
    await process(c, c._updates_queue.get_nowait())
    assert c.session.journal.snapshot()['states'] == {'checkpointed': 1}
    assert c.session.get_update_state(22).pts == 11
    c.session.close()


@pytest.mark.asyncio
async def test_mixed_envelope_waits_for_all_channels(tmp_path):
    c = client(tmp_path)
    c._message_box.map[44] = State(10, asyncio.get_running_loop().time()+900)
    mixed = event()
    second = event(2, 12).updates[0]
    second.message.peer_id = types.PeerChannel(44)
    mixed.updates.append(second)
    c._message_box.try_begin_get_diff(44, 'real gap')
    c._updates_queue.put_nowait(mixed)
    await process(c, c._updates_queue.get_nowait())
    assert c.session.journal.snapshot()['states']['pending'] == 1
    c.session.close()


@pytest.mark.asyncio
async def test_channel_too_long_only_quarantines_its_dependencies(tmp_path):
    c = client(tmp_path)
    q = c.session.journal
    for channel in (22, 44):
        q.put_nowait(ordinary(channel=channel)); q.get_nowait()
    q.reconciliation_required(GetChannelDifferenceRequest(types.InputChannel(22, 0), types.ChannelMessagesFilterEmpty(), 10, 100))
    assert q.snapshot()['states'] == {'pending': 1, 'reconciliation_required': 1}
    c.session.close()


@pytest.mark.asyncio
async def test_ordinary_disk_growth_bounded_and_restart_orders_compacted_pts(tmp_path):
    c = client(tmp_path)
    q = c.session.journal
    q.policy = policy
    for mid in range(1, 2001):
        q.put_nowait(ordinary(mid, mid+10))
    assert q.qsize() == 1
    assert q.snapshot()['states'] == {'pending': 1}
    size = q.session._cursor().execute('SELECT sum(length(payload)) FROM vanguard_raw_updates').fetchone()[0]
    assert size < 1000
    c.session.close()
    c = client(tmp_path)
    item = c._updates_queue.get_nowait()
    assert item.updates[0].pts_count == 2000
    await process(c, item)
    assert c.session.get_update_state(22).pts == 2010
    assert c.session.journal.snapshot()['states'] == {'checkpointed': 1}
    c.session.close()


@pytest.mark.asyncio
async def test_ignored_chatter_during_gap_coalesces_without_hiding_critical_event(tmp_path):
    c = client(tmp_path)
    q = c.session.journal; q.policy = policy
    c._message_box.try_begin_get_diff(22, 'gap')
    for mid in range(1, 101):
        q.put_nowait(ordinary(mid, mid+10))
        await process(c, q.get_nowait())
    assert q.snapshot()['states'] == {'pending': 1}
    deletion = types.UpdateShort(types.UpdateDeleteChannelMessages(22, [7], 111, 1), datetime.now(UTC))
    q.put_nowait(deletion)
    await process(c, q.get_nowait())
    for mid in range(102, 202):
        q.put_nowait(ordinary(mid, mid+10))
        await process(c, q.get_nowait())
    assert q.snapshot()['states'] == {'pending': 3}
    c.session.close()


def test_private_bot_reply_owned_unknown_and_enabled_consumers_are_preserved():
    base = ordinary()
    for kind in ('private', 'bot', 'reply', 'mentioned', 'owned', 'unknown', 'enabled', 'stale'):
        item = ordinary(); p = policy(); m = item.updates[0].message
        if kind == 'private': m.peer_id = types.PeerUser(44)
        if kind == 'bot': item.users[0].bot = True
        if kind == 'reply': m.reply_to = types.MessageReplyHeader(reply_to_msg_id=1)
        if kind == 'mentioned': m.mentioned = True
        if kind == 'owned': p['owned_chats'] = {-1000000000022}
        if kind == 'unknown': item.users = []
        if kind == 'enabled': p['ordinary_required'] = True
        if kind == 'stale': p['loaded_at'] -= 100
        assert compact(item, p)[1] == [], kind
    assert compact(base, policy())[1] == [0]


@pytest.mark.asyncio
async def test_active_push_renews_sdk_deadline_without_suppressing_gap(tmp_path):
    c = client(tmp_path)
    c._message_box.map[22].deadline = asyncio.get_running_loop().time() - 1
    await process(c, event())
    assert c._message_box.map[22].deadline > asyncio.get_running_loop().time() + 800
    await process(c, event(3, 13))
    assert 22 in c._message_box.possible_gaps
    c.session.close()


@pytest.mark.asyncio
async def test_channel_difference_is_fair_and_final_too_long_stops_loop(tmp_path):
    c = client(tmp_path)
    box = c._message_box
    box.map[44] = State(10, asyncio.get_running_loop().time()+900)
    for channel in (22, 44):
        box.try_begin_get_diff(channel, 'gap')
    cache = Obj(self_bot=False, get=lambda entry: Obj(id=entry, hash=0), extend=lambda *args: None)
    requests = [box.get_channel_difference(cache) for _ in range(3)]
    assert [r.channel.channel_id for r in requests] == [22,44,22]
    difference = types.updates.ChannelDifferenceTooLong(dialog=Obj(pts=100), messages=[], chats=[], users=[], final=True, timeout=600)
    assert box.apply_channel_difference(requests[0], difference, cache) == ([],[],[])
    assert 22 not in box.getting_diff_for and 44 in box.getting_diff_for
    assert box.map[22].pts == 100
    assert box.map[22].deadline > asyncio.get_running_loop().time() + 590
    c.session.close()


@pytest.mark.asyncio
async def test_three_hundred_idle_channels_do_not_poll_but_actual_gaps_and_account_do(tmp_path):
    c = client(tmp_path)
    box = c._message_box
    now = asyncio.get_running_loop().time()
    for entry in range(1000,1300):
        box.map[entry] = State(10, now-1)
    box.map[ENTRY_ACCOUNT].deadline = now-1
    box.next_deadline = ENTRY_ACCOUNT
    before = {entry:state.pts for entry,state in box.map.items()}
    box.check_deadlines()
    assert box.getting_diff_for == {ENTRY_ACCOUNT}
    assert {entry:state.pts for entry,state in box.map.items()} == before
    box.try_begin_get_diff(1000, 'server TooLong')
    box.check_deadlines()
    assert 1000 in box.getting_diff_for
    c.session.close()
