"""Decision equivalence and bounded recovery for the V5 collection repair."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.modules.acquisition.group_qualification import EvidenceCollector, trial_evidence
from app.modules.acquisition.qualification_read_context import QualificationReadContext
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, ReadOnlyClient, message

AD = "优惠服务出售欢迎联系 https://one.example"


def progress(*, offset=500, complete=False, end=NOW):
    return {"collected_at": NOW.isoformat(), "collection_progress": {"history_cursors": [
        {"window_end": (end - timedelta(days=index)).isoformat(),
         "offset_id": offset, "complete": complete} for index in range(3)
    ]}}


@pytest.mark.asyncio
async def test_cursor_is_restored_before_any_head_read_and_completed_segments_are_skipped():
    client = ReadOnlyClient([message(900), message(400, age=30)])
    collector = EvidenceCollector(client, enable_history_resume=True)
    collector.previous = progress()
    collector.previous["collection_progress"]["history_cursors"][0]["complete"] = True
    snapshot = await collector.collect(ENTITY, now=NOW)
    history = [kw for kind, kw in client.calls if kind == "messages" and "filter" not in kw]
    assert history and all(kw["offset_id"] == 500 for kw in history)
    assert all(kw["offset_date"].replace(tzinfo=None) < NOW for kw in history)
    assert not snapshot["history_complete"]
    assert snapshot["collection_progress"]["history_resume_refresh_required"]
    assert "resumed_history_hint" in snapshot["unknowns"]


@pytest.mark.asyncio
async def test_resumed_coverage_gap_has_one_fresh_fallback_and_can_finish():
    history = [message(900), message(400, age=30), message(200, age=54)]
    first = EvidenceCollector(ReadOnlyClient(history), enable_history_resume=True)
    first.previous = progress(complete=True)
    partial = await first.collect(ENTITY, now=NOW)
    assert not partial["history_complete"] and partial["sample_count"] == 0
    second_client = ReadOnlyClient(history)
    second = EvidenceCollector(second_client, enable_history_resume=True)
    second.previous = partial
    finished = await second.collect(ENTITY, now=NOW)
    assert finished["history_complete"]
    assert finished["valid_messages"] == 3
    assert finished["collection_progress"]["history_resume_fallback"]
    assert not finished["collection_progress"]["history_resume_refresh_required"]
    assert "resumed_history_hint" not in finished["unknowns"]
    calls = [kw for kind, kw in second_client.calls if kind == "messages" and "filter" not in kw]
    assert all(kw["offset_id"] == 0 for kw in calls)


@pytest.mark.asyncio
async def test_moving_window_reads_only_new_head_then_uses_already_loaded_tail():
    client = ReadOnlyClient([message(900, age=0.1), message(400, age=30)])
    collector = EvidenceCollector(client, enable_history_resume=True)
    collector.previous = progress(end=NOW - timedelta(hours=1))
    snapshot = await collector.collect(ENTITY, now=NOW)
    calls = [kw for kind, kw in client.calls if kind == "messages" and "filter" not in kw]
    assert [kw["offset_id"] for kw in calls[:3]] == [0, 0, 0]
    assert all(kw["offset_id"] == 500 for kw in calls[3:])
    assert snapshot["sample_count"] < 100
    assert not snapshot["history_complete"]


@pytest.mark.asyncio
async def test_invalid_or_future_cursor_cannot_skip_fresh_history():
    client = ReadOnlyClient([message(900)])
    collector = EvidenceCollector(client, enable_history_resume=True)
    collector.previous = progress(end=NOW + timedelta(seconds=1))
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert snapshot["history_complete"]
    assert snapshot["sample_count"] >= 1
    assert not snapshot["collection_progress"]["history_resume_refresh_required"]


@pytest.mark.asyncio
async def test_related_authors_first_can_skip_chat_only_after_sufficient_live_trial():
    history = [message(100, age=30, text=AD, sender=42, replies=Obj(replies=0)),
               *[message(200 + i, age=1, sender=100 + i) for i in range(20)]]
    client = ReadOnlyClient(history)
    # Targeted warning verification reuses the exact current root response.
    client.get_messages = AsyncMock(side_effect=lambda entity, ids: next(item for item in history if item.id == ids))
    collector = EvidenceCollector(client, enable_on_demand_identity=True)
    snapshot = await collector.collect(ENTITY, now=NOW)
    assert trial_evidence(snapshot["evidence"])
    assert client.permission_calls == ["me", 42]
    assert snapshot["valid_messages"] == 1
    assert snapshot["valid_messages_complete"] is False
    assert "chat_identity_not_evaluated" in snapshot["unknowns"]
    assert not snapshot["collection_progress"]["pending_message_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["no_ad", "young", "warning", "small", "online_unknown", "old"])
async def test_negative_or_unknown_branch_retains_complete_chat_identity_path(condition):
    ad = message(100, age=60 if condition == "old" else 1 if condition == "young" else 30,
                 text=AD, sender=42, replies=Obj(replies=1 if condition == "warning" else 0))
    history = [message(200, sender=2)] if condition == "no_ad" else [ad, message(200, sender=2)]
    client = ReadOnlyClient(history, count=49 if condition == "small" else 50)
    if condition == "online_unknown":
        client.full.online_count = None
    if condition == "warning":
        original = client.iter_messages

        async def items(entity, **kwargs):
            if "reply_to" in kwargs:
                yield message(101, text="警告删除广告", sender=7)
            else:
                async for item in original(entity, **kwargs):
                    yield item

        client.iter_messages = items
    client.get_messages = AsyncMock(return_value=ad)
    snapshot = await EvidenceCollector(client, enable_on_demand_identity=True).collect(ENTITY, now=NOW)
    assert 2 in client.permission_calls
    assert "chat_identity_not_evaluated" not in snapshot["unknowns"]
    assert snapshot.get("valid_messages_complete") is not False


@pytest.mark.asyncio
async def test_candidate_slice_does_not_fall_through_into_full_history():
    client = ReadOnlyClient([message(100)])
    snapshot = await EvidenceCollector(client, enable_fast_path=True, candidate_only=True).collect(ENTITY, now=NOW)
    assert snapshot["read_plan_requires_full"]
    assert snapshot["local_rpc_defer_reason"] == "qualification_candidate_full_required"
    assert not [kw for kind, kw in client.calls if kind == "messages"]
    assert not trial_evidence(snapshot["evidence"])


@pytest.mark.asyncio
async def test_unified_fact_reads_are_singleflight_and_scope_parameters_are_exact():
    client = ReadOnlyClient()
    client.get_entity = AsyncMock(return_value=ENTITY)
    client.get_me = AsyncMock(return_value=Obj(id=7))
    context = QualificationReadContext()
    first, second = await asyncio.gather(context.get_entity(client, "name"), context.get_entity(client, "name"))
    assert first is second
    client.get_entity.assert_awaited_once()
    await context.get_me(client)
    await context.get_me(client)
    client.get_me.assert_awaited_once()
    full = await context.full_info(client, ENTITY)
    assert await context.full_info(client, ENTITY) is full
    await context.permissions(client, ENTITY, "me", full_chat=full.full_chat)
    await context.permissions(client, ENTITY, "me", full_chat=full.full_chat)
    assert client.permission_calls == ["me"]
    await context.messages(client, ENTITY, limit=1)
    await context.messages(client, ENTITY, limit=1)
    await context.messages(client, ENTITY, limit=2)
    assert len([kw for kind, kw in client.calls if kind == "messages"]) == 2


@pytest.mark.asyncio
async def test_exact_failure_backoff_expires_and_never_lends_a_success(monkeypatch):
    from app.modules.acquisition import qualification_read_context as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    client = Obj(get_entity=AsyncMock(side_effect=[ValueError("missing"), ENTITY]))
    context = QualificationReadContext()
    for _ in range(2):
        with pytest.raises(ValueError, match="missing"):
            await context.get_entity(client, "same")
    assert client.get_entity.await_count == 1
    clock[0] += 61
    assert await context.get_entity(client, "same") is ENTITY
    assert client.get_entity.await_count == 2


@pytest.mark.asyncio
async def test_disabling_or_expiring_or_new_sender_generation_never_reuses_rights(monkeypatch):
    from app.modules.acquisition import qualification_read_context as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    client = ReadOnlyClient()
    context = QualificationReadContext(ttl_seconds=10)
    await context.permissions(client, ENTITY, "me")
    clock[0] += 11
    await context.permissions(client, ENTITY, "me")
    client._sender = Obj()
    await context.permissions(client, ENTITY, "me")
    disabled = QualificationReadContext(enabled=False)
    await disabled.permissions(client, ENTITY, "me")
    await disabled.permissions(client, ENTITY, "me")
    assert client.permission_calls == ["me"] * 5


@pytest.mark.asyncio
async def test_pushed_message_seed_is_never_promoted_to_a_mature_live_root():
    pushed, live = message(100, age=30, text=AD), message(100, age=30, text=AD)
    pushed._vanguard_event_evidence = True
    client = Obj(get_messages=AsyncMock(return_value=live))
    context = QualificationReadContext()
    context.remember_messages(client, ENTITY, [pushed])
    assert (await context.get_messages(client, ENTITY, [100]))[0] is pushed
    assert await context.get_message(client, ENTITY, 100, require_live=True) is live
    assert await context.get_message(client, ENTITY, 100, require_live=True) is live
    client.get_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_id_scalar_and_batch_share_exact_rpc_failure_without_retry():
    client = Obj(get_messages=AsyncMock(side_effect=ValueError("unavailable")))
    context = QualificationReadContext()
    with pytest.raises(ValueError, match="unavailable"):
        await context.get_messages(client, ENTITY, [100], require_live=True)
    with pytest.raises(ValueError, match="unavailable"):
        await context.get_message(client, ENTITY, 100, require_live=True)
    client.get_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_context_hits_never_invent_avoided_network_counts(monkeypatch):
    from unittest.mock import Mock

    from app.core.account import read_metrics

    avoided, hits = Mock(), Mock()
    monkeypatch.setattr(read_metrics, "mark_avoided", avoided)
    monkeypatch.setattr(read_metrics, "mark_optimization", hits)
    # get_me is commonly satisfied entirely by the SDK's local session. Even
    # an exact successful repeated operation does not establish one saved RPC.
    client = Obj(get_me=AsyncMock(return_value=Obj(id=1)),
                 get_messages=AsyncMock(return_value=[message(100), message(200)]))
    context = QualificationReadContext()
    await context.get_me(client)
    await context.get_me(client)
    await context.get_messages(client, ENTITY, [100, 200])
    await context.get_messages(client, ENTITY, [100, 200])
    await context.get_messages(client, ENTITY, [100])
    await context.get_message(client, ENTITY, 200)
    assert hits.call_count == 4
    avoided.assert_not_called()
    client.get_me.assert_awaited_once()
    client.get_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_reuse_is_a_backoff_hit_without_predicted_savings(monkeypatch):
    from unittest.mock import Mock

    from app.core.account import read_metrics

    avoided, hits = Mock(), Mock()
    monkeypatch.setattr(read_metrics, "mark_avoided", avoided)
    monkeypatch.setattr(read_metrics, "mark_optimization", hits)
    client = Obj(get_entity=AsyncMock(side_effect=ValueError("local lookup failed")))
    context = QualificationReadContext()
    for _ in range(2):
        with pytest.raises(ValueError):
            await context.get_entity(client, "same")
    hits.assert_called_once_with("failure_backoff", "used")
    avoided.assert_not_called()


def test_business_stage_flags_cannot_be_overridden_by_explicit_collector_booleans(monkeypatch):
    monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled", lambda *_: False)
    collector = EvidenceCollector(ReadOnlyClient(), account_id=34,
                                  enable_history_resume=True, enable_on_demand_identity=True)
    assert not collector.read_context.enabled
    assert not collector.enable_history_resume
    assert not collector.enable_on_demand_identity

