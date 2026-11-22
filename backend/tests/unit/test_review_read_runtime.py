"""Ingress capture and exact push reuse never become live survival evidence."""

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import pytest
from telethon import types

from app.modules.acquisition import review_read_runtime as runtime
from app.modules.acquisition.review_evidence_store import ReviewEvidenceStore
from tests.unit.test_listener_journal import client as listener_client
from tests.unit.test_review_evidence_store import config as original_store_config
from tests.unit.test_selective_listener import PEER, policy


@pytest.fixture
async def store(tmp_path, monkeypatch):
    from app.modules.acquisition.review_evidence_ingest import (
        start_ingest_worker,
        stop_ingest_worker,
    )

    value = ReviewEvidenceStore(original_store_config.__wrapped__(tmp_path))
    assert value.initialize_sync().ok
    monkeypatch.setattr(runtime, "store_for", lambda _account_id: value)
    await start_ingest_worker(store=value)
    try:
        yield value
    finally:
        await stop_ingest_worker()


def envelope(mid=200, *, text="本群禁止刷屏，规则转述", posted=None, edited=None, edit=False):
    now = datetime.now(UTC).replace(microsecond=0)
    message = types.Message(
        id=mid,
        peer_id=types.PeerChannel(22),
        from_id=types.PeerUser(44),
        date=posted or now - timedelta(seconds=1),
        edit_date=edited,
        message=text,
    )
    update = (types.UpdateEditChannelMessage if edit else types.UpdateNewChannelMessage)(
        message,
        pts=11,
        pts_count=1,
    )
    return types.Updates([update], [types.User(id=44, bot=False)], [], now, seq=0)


def interest(*, stale=False, evidence_needed=False):
    return policy(
        loaded_at=time.monotonic() - (100 if stale else 0),
        groups={
            PEER: {
                "joined_at": (datetime.utcnow() - timedelta(days=2)).isoformat(),
                "membership_id": 60,
                "watched_evidence": [],
                "evidence_needed": evidence_needed,
            }
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_path", [False, True])
async def test_body_is_captured_before_selective_and_protocol_compaction(tmp_path, store, protocol_path):
    from app.modules.acquisition.review_evidence_ingest import drain_ingest

    client = listener_client(tmp_path)
    queue = client.session.journal
    current = interest()
    current["selective"] = not protocol_path
    queue.policy = lambda: current
    item = envelope()
    body = item.updates[0].message.message
    queue.put_nowait(item)
    await drain_ingest()
    version = runtime.content_version(
        body, sender_id=44, date=item.updates[0].message.date, edit_date=None
    )
    hit = await store.get_cache(2, PEER, f"message:200:{version}")
    assert hit.ok and hit.data["body"] == body
    projected = queue.get_nowait()
    assert projected.updates[0].message.message == ""
    assert client._message_box.map[22].pts == 10
    if not protocol_path:
        fact = json.loads(queue.facts()[0][1])
        assert fact["evidence_refs"]["200"]["version"] == version
    client.session.close()


@pytest.mark.asyncio
async def test_exact_recent_event_cache_requires_scope_sdk_continuity_and_no_pending_fact(store):
    item = envelope()
    current = interest()
    refs = runtime.capture_envelope(item, current)
    ref = refs[PEER, 200]
    box = Obj(getting_diff_for=set(), possible_gaps={}, map={22: Obj(pts=11)}, seq=0)
    journal = Obj(facts=Mock(return_value=[]))
    client = Obj(_message_box=box, session=Obj(journal=journal))

    async def fetch(**changes):
        args = {
            "account_id": 2,
            "group_id": PEER,
            "joined_at": current["groups"][PEER]["joined_at"],
            "refs": {"200": ref},
            "client": client,
        }
        return await runtime.cached_event_messages(**(args | changes))

    messages = await fetch()
    assert len(messages) == 1 and messages[0].message == item.updates[0].message.message
    assert messages[0]._vanguard_event_evidence is True
    assert await fetch(account_id=33) == []
    assert await fetch(group_id=PEER - 1) == []
    assert await fetch(joined_at=datetime.utcnow()) == []
    assert await fetch(refs={"200": {**ref, "version": "other-version"}}) == []
    box.getting_diff_for.add(22)
    assert await fetch() == []
    box.getting_diff_for.clear()
    box.map[22].pts = 10
    assert await fetch() == []
    box.map[22].pts = 11
    journal.facts.return_value = [("newer", json.dumps({"peer": PEER, "kind": "rules"}))]
    assert await fetch() == []


@pytest.mark.asyncio
async def test_cache_older_than_ninety_seconds_is_not_current_event_evidence(store):
    item = envelope(posted=datetime.now(UTC) - timedelta(seconds=91))
    current = interest()
    ref = runtime.capture_envelope(item, current)[PEER, 200]
    client = Obj(
        _message_box=Obj(getting_diff_for=set(), possible_gaps={}, map={22: Obj(pts=11)}, seq=0),
        session=Obj(journal=Obj(facts=lambda **_: [])),
    )
    assert (
        await runtime.cached_event_messages(
            account_id=2,
            group_id=PEER,
            joined_at=current["groups"][PEER]["joined_at"],
            refs={"200": ref},
            client=client,
        )
        == []
    )


@pytest.mark.asyncio
async def test_truncated_pending_fact_scan_is_a_miss_even_if_visible_page_is_unrelated(store):
    item = envelope()
    current = interest()
    ref = runtime.capture_envelope(item, current)[PEER, 200]
    facts = [(str(index), json.dumps({"peer": PEER - 1, "kind": "rules"})) for index in range(1001)]
    journal = Obj(facts=Mock(side_effect=lambda limit: facts[:limit]))
    client = Obj(
        _message_box=Obj(getting_diff_for=set(), possible_gaps={}, map={22: Obj(pts=11)}, seq=0),
        session=Obj(journal=journal),
    )
    assert (
        await runtime.cached_event_messages(
            account_id=2,
            group_id=PEER,
            joined_at=current["groups"][PEER]["joined_at"],
            refs={"200": ref},
            client=client,
        )
        == []
    )
    journal.facts.assert_called_once_with(limit=1001)


@pytest.mark.asyncio
async def test_same_second_conflicting_refs_fall_back_to_live_and_cannot_be_acknowledged_as_old(
    tmp_path, store
):
    from app.modules.acquisition.review_evidence_ingest import drain_ingest

    client = listener_client(tmp_path)
    queue = client.session.journal
    current = interest()
    queue.policy = lambda: current
    now = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
    queue.put_nowait(envelope(posted=now))
    first = queue.facts()
    queue.put_nowait(envelope(text="本群禁止广告，已修改规则", posted=now, edited=now, edit=True))
    await drain_ingest()
    current_fact = json.loads(queue.facts()[0][1])
    assert not current_fact.get("evidence_refs")
    assert current_fact.get("version_conflict")
    queue.acknowledge_facts(first)
    assert queue.facts()
    client.session.close()


@pytest.mark.asyncio
async def test_admitted_version_payload_survives_cache_pressure_and_local_cleanup(store):
    current = interest(evidence_needed=True)
    item = envelope(
        text="优惠出售套餐服务联系 https://offer.example",
        posted=datetime.now(UTC) - timedelta(hours=1),
    )
    runtime.capture_envelope(item, current)
    rows = (await store.list_observations(account_id=2, group_id=PEER)).data["observations"]
    assert len(rows) == 1
    row = rows[0]
    ready = row["control"]["ready_at"]
    for index in range(8):
        assert store.capture_sync(
            account_id=2, group_id=PEER - 1 - index, key=f"pressure-{index}", body="x"
        ).ok
    swept = await store.sweep(now=ready - 1)
    assert swept.ok
    protected = await store.get_observation(row["handle"])
    assert protected.ok and protected.data["control"]["state"] == "observing"
    assert item.updates[0].message.message in json.dumps(protected.data, ensure_ascii=False)
    assert not (await store.prepare_handoff(row["handle"], "verified", now=ready - 1)).ok


@pytest.mark.asyncio
async def test_real_edit_invalidates_old_protected_version_and_starts_new_observation(store):
    current = interest(evidence_needed=True)
    now = datetime.now(UTC) - timedelta(seconds=1)
    item = envelope(text="优惠出售套餐联系 https://offer.example", posted=now - timedelta(hours=2))
    runtime.capture_envelope(item, current)
    first = (await store.list_observations(account_id=2, group_id=PEER)).data["observations"][0]
    changed = envelope(
        text="新优惠出售套餐联系 https://offer.example",
        posted=item.updates[0].message.date,
        edited=now,
        edit=True,
    )
    runtime.capture_envelope(changed, current)
    old = await store.get_observation(first["handle"])
    assert old.data["control"]["facts"].get("edited")
    rows = (await store.list_observations(account_id=2, group_id=PEER)).data["observations"]
    observing = [row["control"] for row in rows if row["control"]["state"] == "observing"]
    assert len(observing) == 1
    assert observing[0]["version"] != first["control"]["version"]
    assert observing[0]["ready_at"] == pytest.approx(now.timestamp() + 24 * 3600)


@pytest.mark.asyncio
async def test_initial_rule_and_later_reply_are_protected_with_candidate(store):
    current = interest(evidence_needed=True)
    rule = envelope(198, text="本群禁止广告，请遵守群规")
    runtime.capture_envelope(rule, current)
    advert = envelope(200, text="优惠出售套餐联系 https://offer.example",
                      posted=datetime.now(UTC) - timedelta(hours=1))
    runtime.capture_envelope(advert, current)
    rows = (await store.list_observations(account_id=2, group_id=PEER)).data["observations"]
    assert len(rows) == 1
    reply = envelope(201, text="请按照规则联系管理人员")
    reply.updates[0].message.reply_to = types.MessageReplyHeader(reply_to_msg_id=200)
    runtime.capture_envelope(reply, current)
    kept = (await store.get_observation(rows[0]["handle"])).data
    bodies = {record["body"] for record in kept["records"]}
    assert rule.updates[0].message.message in bodies
    assert advert.updates[0].message.message in bodies
    assert reply.updates[0].message.message in bodies
    for index in range(8):
        store.capture_sync(account_id=2, group_id=PEER-index-1, key=str(index), body="pressure")
    assert (await store.get_observation(rows[0]["handle"])).data["records"] == kept["records"]


@pytest.mark.asyncio
async def test_exact_hint_seed_is_account_scoped_and_edit_removes_old_version(store):
    current = interest()
    item = envelope()
    runtime.capture_envelope(item, current)
    client = Obj(_message_box=Obj(getting_diff_for=set(), possible_gaps={}, map={22: Obj(pts=11)}, seq=0),
                 session=Obj(journal=Obj(facts=lambda **_: [])))
    args = {"account_id": 2, "group_id": PEER, "joined_at": current["groups"][PEER]["joined_at"],
                "message_ids": [200], "client": client}
    seeded = await runtime.cached_hint_messages(**args)
    assert len(seeded) == 1 and seeded[0]._vanguard_event_evidence
    assert not await runtime.cached_hint_messages(**(args | {"account_id": 33}))
    old_ref = runtime.capture_envelope(item, current)[PEER, 200]
    changed = envelope(text="本群允许特定广告，规则已修改", edited=item.date, edit=True)
    runtime.capture_envelope(changed, current)
    assert not await runtime.cached_event_messages(**{
        key: value for key, value in args.items() if key != "message_ids"
    }, refs={"200": old_ref})
    latest = await runtime.cached_hint_messages(**args)
    assert len(latest) == 1 and latest[0].message == changed.updates[0].message.message


@pytest.mark.asyncio
async def test_consumed_delete_cannot_resurrect_push_but_keeps_protected_body(store):
    current = interest(evidence_needed=True)
    item = envelope(text="优惠出售套餐联系 https://offer.example")
    runtime.capture_envelope(item, current)
    row = (await store.list_observations(account_id=2, group_id=PEER)).data["observations"][0]
    deleted = types.Updates([types.UpdateDeleteChannelMessages(channel_id=22, messages=[200],
        pts=12, pts_count=1)], [], [], datetime.now(UTC), seq=0)
    runtime.capture_envelope(deleted, current)
    client = Obj(_message_box=Obj(getting_diff_for=set(), possible_gaps={}, map={22: Obj(pts=12)}, seq=0),
                 session=Obj(journal=Obj(facts=lambda **_: [])))
    assert not await runtime.cached_hint_messages(account_id=2, group_id=PEER,
        joined_at=current["groups"][PEER]["joined_at"], message_ids=[200], client=client)
    assert (await store.get_observation(row["handle"])).data["records"][0]["body"]


@pytest.mark.asyncio
async def test_mature_candidate_lookup_survives_disabled_admission(store, monkeypatch):
    joined = datetime.utcnow() - timedelta(days=5)
    now = datetime.utcnow()
    first = await store.admit_observation(account_id=2, group_id=PEER, message_id=210,
        version="mature", joined_at=joined, date=now-timedelta(hours=25),
        records=[{"key": "root", "body": "old"}])
    assert first.ok
    second = await store.admit_observation(account_id=2, group_id=PEER, message_id=211,
        version="young", joined_at=joined, date=now-timedelta(hours=1),
        records=[{"key": "young", "body": "new"}])
    assert second.ok
    monkeypatch.setattr(runtime, "store_for", lambda _: None)
    monkeypatch.setattr(runtime, "maintenance_store", lambda _: store)
    member = Obj(account_id=2, joined_at=joined, telegram_group_id=PEER)
    assert await runtime.candidate_ids(member, now, mature_only=True) == [210]
    assert await runtime.candidate_ids(member, now) == [210, 211]


@pytest.mark.asyncio
async def test_live_candidate_admits_only_its_required_reply_bundle(store):
    now = datetime.utcnow()
    date = (now - timedelta(hours=1)).isoformat()
    member = Obj(id=60, account_id=2, joined_at=now-timedelta(days=5), telegram_group_id=PEER)
    root = {"source": "recent_promotional_message", "message_id": 210,
            "sender_id": 44, "sender_role": "ordinary", "text": "优惠出售套餐联系 https://offer.example",
            "date": date, "edited_at": None, "age_hours": 1, "accessible": True,
            "warning_reply_ids": [], "warning_search_complete": True}
    snapshot = {"collected_at": now.isoformat(), "evidence": [root, {
        "source": "full_about", "text": "本群请遵守规则", "date": None}],
        "evidence_dependencies": [
            {"source": "warning_context", "message_id": 211, "reply_to_message_id": 210,
             "text": "必要回复", "date": date},
            {"source": "warning_context", "message_id": 999, "reply_to_message_id": 998,
             "text": "另一个候选的回复", "date": date}]}
    await runtime.retain_candidates(member, snapshot, group_id=PEER)
    rows = (await store.list_observations(account_id=2, group_id=PEER)).data["observations"]
    assert len(rows) == 1
    records = (await store.get_observation(rows[0]["handle"])).data["records"]
    bodies = {record["body"] for record in records}
    assert bodies == {root["text"], "本群请遵守规则", "必要回复"}
    assert all("text" not in record["metadata"].get("evidence", {}) for record in records)


@pytest.mark.parametrize("allowlist, expected", [
    ("34,bad", False), ("bad,34", False), ("", False), ("33,35", False), ("34,35", True),
])
def test_runtime_and_stage_gate_share_fail_closed_allowlist(monkeypatch, allowlist, expected):
    from app.modules.acquisition import review_read_flags as flags

    monkeypatch.setattr(runtime.settings, "REVIEW_READ_OPTIMIZATION_ENABLED", True)
    monkeypatch.setattr(runtime.settings, "REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS", allowlist)
    assert runtime.enabled_for(34) is expected
    assert runtime.enabled_for(34) == flags.enabled_for(34)


@pytest.mark.asyncio
async def test_bad_allowlist_stops_admission_without_stranding_existing_protection(tmp_path, monkeypatch):
    value = ReviewEvidenceStore(original_store_config.__wrapped__(tmp_path))
    now = datetime.utcnow()
    joined = now-timedelta(days=3)
    admitted = await value.admit_observation(account_id=34, group_id=PEER, joined_at=joined,
        message_id=210, version="v1", date=now-timedelta(hours=25), records=[{"key": "root", "body": "old"}])
    assert admitted.ok
    monkeypatch.setattr(runtime, "_store", value)
    monkeypatch.setattr(runtime, "_store_path", str(value.directory))
    monkeypatch.setattr(runtime.settings, "REVIEW_EVIDENCE_STORE_PATH", str(value.directory))
    monkeypatch.setattr(runtime.settings, "REVIEW_EVIDENCE_STORE_ENABLED", True)
    monkeypatch.setattr(runtime.settings, "REVIEW_READ_OPTIMIZATION_ENABLED", True)
    monkeypatch.setattr(runtime.settings, "REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS", "34,bad")
    assert runtime.store_for(34) is None
    assert runtime.maintenance_store(34) is value
    member = Obj(account_id=34, telegram_group_id=PEER, joined_at=joined)
    assert await runtime.candidate_ids(member, now, mature_only=True) == [210]
    assert value.get_observation_sync(admitted.handle).data["records"][0]["body"] == "old"
