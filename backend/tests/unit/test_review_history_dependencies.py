"""Only fully explained, unaffected history pages cross an event/PTS change."""
from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from telethon.tl import types

from app.modules.acquisition import review_history_dependencies as deps
from app.modules.acquisition import review_history_progress as pages
from app.modules.acquisition.review_collection_progress import for_event
from tests.unit.test_group_qualification_sampling import ENTITY
from tests.unit.test_review_history_progress import env as env
from tests.unit.test_review_history_progress import history_calls


def observed(client, update, pts):
    deps.observe_envelope(client.session.journal, update, {"account_id": 34})
    client._message_box.map[42].pts = pts


def edit(env, number=50, age=30, *, pts=31, count=1):
    msg = types.Message(id=number, peer_id=types.PeerChannel(42),
                        from_id=types.PeerUser(1), date=(env.now-timedelta(hours=age)).replace(tzinfo=UTC),
                        message="changed", edit_date=env.now.replace(tzinfo=UTC))
    return types.UpdateEditChannelMessage(msg, pts=pts, pts_count=count)


@pytest.mark.asyncio
async def test_exact_rule_event_only_refreshes_the_day_containing_its_message(env):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    first["account_id"], first["collection_event"] = 34, env.scope["event"]
    change = {"joined_at": env.scope["joined_at"], "kinds": ["rules"],
              "message_ids": [50], "token": "changed-rule"}
    env.scope["event"] = change
    previous = for_event(first, change)
    assert previous["collection_progress"]["history_cursors"]
    client.calls.clear()
    second = await env.collector(client, previous).collect(ENTITY, now=env.now)
    assert second["history_complete"] and second["history_cached_count"] == 2
    assert len(history_calls(client)) == 1 and second["sample_count"] == 2
    assert second["history_source_oldest_at"] == env.now.isoformat()


@pytest.mark.asyncio
async def test_current_permission_event_preserves_raw_pages_but_rechecks_roles(env):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    env.scope["event"] = {"joined_at": env.scope["joined_at"], "kinds": ["membership"],
                          "message_ids": [], "token": "current-permissions"}
    client.calls.clear()
    client.permission_calls.clear()
    client.admins.add(1)
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert not history_calls(client) and second["history_cached_count"] == 3
    assert second["roles"]["1"] == "admin" and client.permission_calls == ["me", 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["edit", "delete", "new"])
async def test_contiguous_channel_transition_invalidates_only_affected_page(env, kind):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    if kind == "delete":
        update = types.UpdateDeleteChannelMessages(42, [50], pts=31, pts_count=1)
        client.history = [message for message in client.history if message.id != 50]
    elif kind == "new":
        update = edit(env, number=60)
        update = types.UpdateNewChannelMessage(update.message, pts=31, pts_count=1)
        client.history.insert(1, env.msg(60, age=30))
    else:
        update = edit(env)
        client.history[1] = env.msg(50, age=30, text="changed")
    observed(client, update, 31)
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_complete"] and second["history_cached_count"] == 2
    assert len(history_calls(client)) == 1
    assert second["unique_sample_count"] == (2 if kind == "delete" else 4 if kind == "new" else 3)
    assert second["history_source_oldest_at"] == env.now.isoformat()


@pytest.mark.asyncio
async def test_new_reply_in_another_day_also_invalidates_the_roots_page(env):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    changed = edit(env, number=110, age=1)
    changed.message.reply_to = types.MessageReplyHeader(reply_to_msg_id=50)
    client.history.insert(0, env.msg(110, age=1))
    observed(client, types.UpdateNewChannelMessage(changed.message, pts=31, pts_count=1), 31)
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_complete"] and second["history_cached_count"] == 1
    assert len(history_calls(client)) == 2


@pytest.mark.asyncio
async def test_unrelated_channel_update_preserves_target_pages(env):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    deps.observe_envelope(client.session.journal,
                          types.UpdateDeleteChannelMessages(43, [50], pts=31, pts_count=1),
                          {"account_id": 34})
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_complete"] and second["history_cached_count"] == 3
    assert not history_calls(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unexplained", "gap", "noncontiguous", "pending", "overflow", "unknown", "disabled"])
async def test_unknown_or_incomplete_transition_reverts_to_original_reader(env, monkeypatch, failure):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    if failure == "unexplained":
        client._message_box.map[42].pts = 31
    elif failure == "gap":
        observed(client, types.UpdateChannelTooLong(42, pts=31), 31)
    elif failure == "noncontiguous":
        observed(client, edit(env, number=200, age=-1, pts=32), 32)
    elif failure == "account":
        from telethon._updates.messagebox import ENTRY_ACCOUNT
        client._message_box.map[ENTRY_ACCOUNT].pts += 1
    elif failure == "seq":
        client._message_box.seq += 1
    elif failure == "pending":
        deps.observe_envelope(client.session.journal, edit(env, number=200, age=-1), {"account_id": 34})
    elif failure == "overflow":
        for index in range(deps.MAX_CHANGES+1):
            observed(client, edit(env, number=200+index, age=-1, pts=31+index), 31+index)
    elif failure == "unknown":
        observed(client, types.UpdateChannel(42), 30)
    else:
        monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled",
                            lambda _account, stage: stage != "dependency_resume")
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert len(history_calls(client)) == 3
    assert second["history_cached_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    {"kinds": ["gap"], "message_ids": []},
    {"kinds": ["unknown"], "message_ids": [50]},
    {"kinds": ["rules"], "message_ids": []},
    {"kinds": ["rules"], "message_ids": [50], "unscoped_rules_token": "unknown-range"},
    {"kinds": ["membership"], "message_ids": [], "account_generation": "gap:new"},
])
async def test_unknown_event_does_not_restore_any_pages(env, event):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    env.scope["event"] = {**event, "joined_at": env.scope["joined_at"], "token": "new"}
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert len(history_calls(client)) == 3 and second["history_cached_count"] == 0


def test_tracker_is_bounded_and_does_not_keep_bodies(env):
    client = env.client([])
    journal = client.session.journal
    for channel in range(1, deps.MAX_CHANNELS+2):
        deps.observe_envelope(journal, types.UpdateDeleteChannelMessages(channel, [1], pts=1, pts_count=1),
                              {"account_id": 34})
    tracker = deps.tracker(journal)
    assert len(tracker.channels) == deps.MAX_CHANNELS and 1 not in tracker.channels
    assert all(len(row.changes) <= deps.MAX_CHANGES for row in tracker.channels.values())
    assert all(not hasattr(change, "body") for row in tracker.channels.values() for change in row.changes)
    for channel in range(1, 22):
        for number in range(1, deps.MAX_CHANGES+1):
            deps.observe_envelope(journal, types.UpdateDeleteChannelMessages(channel, [number], pts=number, pts_count=1),
                                  {"account_id": 34})
    assert sum(len(row.changes) for row in tracker.channels.values()) <= deps.MAX_TOTAL_CHANGES


def test_duplicate_replay_does_not_renew_or_duplicate_transition(env):
    client = env.client([])
    update = edit(env, number=200, age=-1)
    observed(client, update, 31)
    first = deps.state(client.session.journal, 42, 31)
    deps.observe_envelope(client.session.journal, update, {"account_id": 34})
    assert deps.state(client.session.journal, 42, 31) == first


def test_journal_observes_before_body_projection_and_replays_without_double_counting(tmp_path, monkeypatch):
    import time

    from app.core.account.listener_checkpoint import ListenerSession
    from app.core.account.listener_journal import JournalQueue
    from tests.unit.test_listener_journal import event

    monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled", lambda *_: True)
    session = ListenerSession(str(tmp_path / "history-chain"))
    queue = JournalQueue(session)
    queue.policy = lambda: {"account_id": 34, "loaded_at": time.monotonic(),
                            "ordinary_required": False, "owned_chats": set()}
    original = event()
    queue.put_nowait(original)
    stored = deps.state(queue, 22, 11)
    assert stored is not None and stored["revision"] == 1
    queue.get_nowait()
    assert deps.state(queue, 22, 11) == stored
    queue.recover()
    assert deps.state(queue, 22, 11)["epoch"] != stored["epoch"]
    session.close()


@pytest.mark.asyncio
async def test_old_page_age_is_not_renewed_by_unrelated_updates(env, monkeypatch):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    observed(client, edit(env, number=200, age=-1), 31)
    instant = env.now.replace(tzinfo=UTC).timestamp()+72*3600+1
    monkeypatch.setattr(pages.time, "time", lambda: instant)
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_cached_count"] == 0 and len(history_calls(client)) == 3
