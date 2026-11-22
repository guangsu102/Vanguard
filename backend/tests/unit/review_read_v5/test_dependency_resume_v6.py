"""Unrelated account traffic cannot expire proved channel raw facts."""
from datetime import UTC

import pytest
from telethon._updates.messagebox import ENTRY_ACCOUNT

from app.modules.acquisition import review_history_progress as pages
from tests.unit.test_group_qualification_sampling import ENTITY
from tests.unit.test_review_history_progress import env as env, history_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['seq', 'account_pts', 'age'])
async def test_proved_channel_facts_survive_irrelevant_account_changes(env, monkeypatch, change):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    if change == 'seq':
        client._message_box.seq += 1
    elif change == 'account_pts':
        client._message_box.map[ENTRY_ACCOUNT].pts += 1
    else:
        instant = env.now.replace(tzinfo=UTC).timestamp()+301
        monkeypatch.setattr(pages.time, 'time', lambda: instant)
    client.calls.clear()
    client.permission_calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert not history_calls(client)
    assert second['history_cached_count'] == 3
    assert second['history_source_oldest_at'] == env.now.isoformat()
    assert client.permission_calls == ['me', 1]


@pytest.mark.asyncio
async def test_long_page_ttl_never_extends_original_message_version_age(env, monkeypatch):
    client = env.client([env.msg(100, age=71.99)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    instant = env.now.replace(tzinfo=UTC).timestamp()+60
    monkeypatch.setattr(pages.time, 'time', lambda: instant)
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert history_calls(client)  # A 72-hour-old raw version expired in the fixed store.
    assert second['history_source_oldest_at'] == env.now.isoformat()
