"""Atomic history facts preserve dependencies and the original freshness bound."""
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon._updates.messagebox import ENTRY_ACCOUNT

from app.modules.acquisition import review_history_progress as pages
from app.modules.acquisition.group_qualification import EvidenceCollector, trial_evidence
from app.modules.acquisition.qualification_read_context import QualificationReadContext
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, ReadOnlyClient, message


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled", lambda *_: True)
    monkeypatch.setattr("app.modules.acquisition.review_evidence_ingest.cache_safe", lambda *_: True)
    now = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    scope = {"membership_id": 91, "joined_at": (now-timedelta(days=9)).isoformat(),
             "event": {"v": 2}, "policy": "test-v3", "profile": "p1", "system_identity": [9]}
    store = ReviewEvidenceStore(EvidenceStoreConfig(directory=tmp_path / "pages", enabled=True,
        protected_slots=1, protected_slot_bytes=64*1024, protected_control_bytes=4096,
        protected_records_per_slot=8, protected_record_limit=8, slots_per_account=1,
        slots_per_account_group=1, ordinary_pages=1, ordinary_page_bytes=1024*1024,
        physical_limit_bytes=4*1024*1024))
    assert store.initialize_sync().ok

    def client(history):
        result = ReadOnlyClient(history)
        result._message_box = Obj(seq=10, map={ENTRY_ACCOUNT: Obj(pts=20), 42: Obj(pts=30)},
                                  getting_diff_for=set(), possible_gaps={})
        result.session = Obj(journal=Obj(facts=lambda **_: []))
        return result

    def msg(number, **kwargs):
        result = message(number, **kwargs)
        result.date += now-NOW
        return result

    def collector(client, previous=None, **kwargs):
        result = EvidenceCollector(client, account_id=34, history_scope=scope,
                                   history_store=store, enable_on_demand_identity=False, **kwargs)
        if previous:
            result.previous = copy.deepcopy(previous)
            # Exercise the history package, independently of targeted identity
            # progress (covered by the existing evidence progress suite).
            result.previous["collection_progress"]["pending_message_ids"] = []
            result.previous["collection_progress"]["not_evaluated_message_ids"] = []
        return result

    return Obj(now=now, scope=scope, store=store, client=client, msg=msg, collector=collector)


def history_calls(client):
    return [kw for kind, kw in client.calls if kind == "messages" and "filter" not in kw]


@pytest.mark.asyncio
async def test_complete_package_eliminates_history_calls_and_rechecks_roles(env):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    assert first["history_complete"]
    assert all(row.get("page_key") not in {None, "unavailable"}
               for row in first["collection_progress"]["history_cursors"])
    client.calls.clear()
    client.permission_calls.clear()
    client.admins.add(1)
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_complete"] and not history_calls(client)
    assert second["history_cached_count"] == 3
    assert second["sample_count"] == 0 and second["unique_sample_count"] == 3
    assert second["valid_messages"] == 3
    assert second["roles"]["1"] == "admin"
    assert client.permission_calls == ["me", 1]
    assert second["history_source_oldest_at"] == env.now.isoformat()
    assert not second["collection_progress"]["history_resume_refresh_required"]
    assert "resumed_history_hint" not in second["unknowns"]


@pytest.mark.asyncio
async def test_partial_package_restores_prefix_and_finishes_tail(env):
    history = [env.msg(1000*(3-day)-i, age=24*day+1+i/100, sender=day+1)
               for day in range(3) for i in range(120)]
    client = env.client(history)
    first = await env.collector(client, identity_limit=1, priority_identities=0).collect(ENTITY, now=env.now)
    assert first["sample_count"] == 300 and not first["history_complete"]
    offsets = [row["offset_id"] for row in first["collection_progress"]["history_cursors"]]
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert [call["offset_id"] for call in history_calls(client)] == offsets
    assert second["history_complete"] and second["unique_sample_count"] == 360
    assert second["history_cached_count"] == 300
    assert second["valid_messages"] == 360
    assert "history_coverage_incomplete" not in second["unknowns"]


@pytest.mark.asyncio
async def test_moving_window_reads_new_heads_only_and_never_renews_old_freshness(env, monkeypatch):
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    client.history.insert(0, env.msg(110, age=-0.01))
    # A real incoming update would change PTS and correctly force fresh. This
    # fixed-watermark fixture isolates the date-window merge algorithm.
    instant = (env.now+timedelta(seconds=60)).replace(tzinfo=UTC).timestamp()
    monkeypatch.setattr(pages.time, "time", lambda: instant)
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now+timedelta(seconds=60))
    assert second["history_complete"] and second["unique_sample_count"] == 4
    assert len(history_calls(client)) == 3
    assert all(call["offset_id"] == 0 for call in history_calls(client))
    assert second["history_source_oldest_at"] == env.now.isoformat()
    for cursor in second["collection_progress"]["history_cursors"]:
        row = await env.store.load_history_page(34, pages.group_key(ENTITY), key=cursor["page_key"])
        assert row.ok
        assert row.data["metadata"]["observed_at"] == env.now.replace(tzinfo=UTC).timestamp()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["scope", "joined", "event", "pts", "seq", "generation", "gap", "pending", "age", "record", "ingest"])
async def test_any_changed_dependency_or_missing_record_uses_fresh_reader(env, monkeypatch, change):
    # The legacy exact-fingerprint path keeps its original five-minute fence.
    # Dependency-aware reuse has separate transition and TTL tests.
    monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled",
                        lambda _account, stage: stage != "dependency_resume")
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    if change == "scope":
        env.scope["profile"] = "changed"
    elif change == "joined":
        env.scope["joined_at"] = env.now.isoformat()
    elif change == "event":
        env.scope["event"] = {"v": 3}
    elif change == "pts":
        client._message_box.map[42].pts += 1
    elif change == "seq":
        client._message_box.seq += 1
    elif change == "generation":
        client._sender = Obj(_connection=Obj())
    elif change == "gap":
        client._message_box.possible_gaps[42] = True
    elif change == "pending":
        client.session.journal.facts = lambda **_: [("event", '{"kind":"rules","peer":-1000000000042}')]
    elif change == "age":
        instant = env.now.replace(tzinfo=UTC).timestamp() + 301
        monkeypatch.setattr(pages.time, "time", lambda: instant)
    elif change == "record":
        assert env.store.invalidate_messages_sync(account_id=34, group_id=pages.group_key(ENTITY), message_ids=[100]).ok
    elif change == "ingest":
        monkeypatch.setattr("app.modules.acquisition.review_evidence_ingest.cache_safe", lambda *_: False)
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_complete"]
    assert history_calls(client) and history_calls(client)[0]["offset_id"] == 0
    assert second["sample_count"] > 0
    if change != "record":
        assert second["history_cached_count"] == 0
    assert "resumed_history_hint" not in second["unknowns"]


@pytest.mark.asyncio
async def test_oversized_package_cannot_skip_history_on_next_pass(env):
    client = env.client([env.msg(100, text="x"*(16*1024+1))])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    assert first["collection_progress"]["history_cursors"][0]["page_key"] == "unavailable"
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["sample_count"] > 0
    assert history_calls(client)[0]["offset_id"] == 0


@pytest.mark.asyncio
async def test_whole_bundle_over_one_mib_is_rejected_without_partial_coverage(env):
    client = env.client([env.msg(300-i, text="x"*(16*1024)) for i in range(100)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    assert first["collection_progress"]["history_cursors"][0]["page_key"] == "unavailable"
    client.calls.clear()
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert second["history_complete"]
    assert second["sample_count"] == 100 and second["unique_sample_count"] == 100
    assert history_calls(client)[0]["offset_id"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "edited", "warned"])
async def test_restored_ad_is_revalidated_live_and_changed_root_does_not_authorize(env, change):
    ad = env.msg(100, age=30, text="优惠出售服务欢迎联系 https://offer.example", replies=Obj(replies=0))
    client = env.client([ad])
    client.get_messages = AsyncMock(return_value=ad)
    first = await env.collector(client).collect(ENTITY, now=env.now)
    assert trial_evidence(first["evidence"])
    client.get_messages.reset_mock(return_value=True)
    if change == "deleted":
        client.get_messages.return_value = None
    elif change == "edited":
        client.get_messages.return_value = env.msg(100, age=30, text="更改后的优惠出售服务联系 https://other.example",
                                                  edit_date=env.now, replies=Obj(replies=0))
    elif change == "warned":
        ad.replies = Obj(replies=1)
        client.get_messages.return_value = ad
        original = client.iter_messages

        async def current_messages(entity, **kwargs):
            if "reply_to" in kwargs:
                yield env.msg(101, text="警告删除广告", sender=9)
            else:
                async for item in original(entity, **kwargs):
                    yield item

        client.iter_messages = current_messages
    second = await env.collector(client, first).collect(ENTITY, now=env.now)
    assert not trial_evidence(second["evidence"])
    if change == "deleted":
        assert "advertisement_no_longer_accessible" in second["unknowns"]
    elif change == "edited":
        assert "advertisement_content_changed" in second["unknowns"]
    else:
        assert any(item.get("warning_reply_ids") for item in second["evidence"])
    client.get_messages.assert_awaited()


@pytest.mark.asyncio
async def test_history_seed_is_never_a_live_root_or_identity_cache(env):
    record = pages._record(env.msg(100, age=30, replies=Obj(replies=0)))
    restored = pages._message(record)
    assert restored.sender is None
    client = Obj(get_messages=AsyncMock(return_value=None))
    context = QualificationReadContext()
    context.remember_messages(client, ENTITY, [restored])
    assert (await context.get_messages(client, ENTITY, [100]))[0] is restored
    assert await context.get_message(client, ENTITY, 100, require_live=True) is None
    client.get_messages.assert_awaited_once()
