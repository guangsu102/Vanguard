"""Optional cache IO cannot block protocol ingestion or discard admitted proof."""

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import types

from app.modules.acquisition import review_evidence_handoff as handoff
from app.modules.acquisition import review_evidence_ingest as ingest
from app.modules.acquisition import review_read_runtime as runtime
from app.modules.acquisition.review_evidence_store import ReviewEvidenceStore
from tests.unit.test_listener_journal import client as listener_client
from tests.unit.test_review_evidence_store import config as small_config
from tests.unit.test_review_read_runtime import PEER, envelope, interest


@pytest.fixture
async def worker(tmp_path, monkeypatch):
    store = ReviewEvidenceStore(small_config.__wrapped__(tmp_path))
    monkeypatch.setattr(runtime, "store_for", lambda _account: store)
    worker = await ingest.start_ingest_worker(store=store)
    assert worker is not None
    try:
        yield worker
    finally:
        await ingest.stop_ingest_worker()


@pytest.mark.asyncio
async def test_preinitialized_worker_serializes_disk_io_off_receiver_and_fences_pending_cache(worker, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = worker.store.capture_sync
    thread_ids = []

    def blocked(**kwargs):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(worker.store, "capture_sync", blocked)
    item, policy = envelope(), interest()
    assert worker.store.path.is_file()
    refs, gaps = ingest.submit_envelope(item, policy)
    assert refs[PEER, 200] and not gaps
    assert not ingest.cache_safe(2)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        assert worker.inflight and thread_ids == [thread_ids[0]]
        assert thread_ids[0] != threading.get_ident()
    finally:
        release.set()
    await ingest.drain_ingest()
    assert ingest.cache_safe(2)
    hit = await worker.store.get_cache(2, PEER, refs[PEER, 200]["key"])
    assert hit.ok and hit.data["body"] == item.updates[0].message.message


@pytest.mark.asyncio
async def test_queue_overflow_is_bounded_durable_business_gap_without_sdk_progress(worker, tmp_path):
    worker.max_items = 1
    session_path = tmp_path / "session"
    session_path.mkdir()
    client = listener_client(session_path)
    queue = client.session.journal
    queue.policy = lambda: interest(evidence_needed=True)
    first = envelope(text="优惠出售套餐联系 https://offer.example",
                     posted=datetime.now(UTC) - timedelta(hours=1))
    try:
        queue.put_nowait(first)
        queue.put_nowait(envelope(201, text="警告，请删除这条广告"))
        assert len(worker.pending) == 1
        assert worker.pending_bytes <= worker.max_bytes
        facts = [json.loads(raw) for _, raw in queue.facts()]
        assert any(fact["kind"] == "gap" and fact.get("reason") == "optional_evidence_ingest_gap"
                   for fact in facts)
        assert client._message_box.map[22].pts == 10
        assert not ingest.cache_safe(2)
        await ingest.drain_ingest()
        rows = (await worker.store.list_observations(account_id=2)).data["observations"]
        assert len(rows) == 1
        assert "gap" in rows[0]["control"]["facts"]
        kept = await worker.store.get_observation(rows[0]["handle"])
        assert first.updates[0].message.message in json.dumps(kept.data, ensure_ascii=False)
        assert not ingest.cache_safe(2)
    finally:
        client.session.close()


@pytest.mark.asyncio
async def test_overflow_only_gap_is_committed_before_fully_projected_message_returns(worker, tmp_path):
    worker.max_items = 0
    session_path = tmp_path / "session"
    session_path.mkdir()
    client = listener_client(session_path)
    client.session.journal.policy = interest
    item = envelope(text="收到")
    item.updates[0].message.reply_to = types.MessageReplyHeader(reply_to_msg_id=100)
    try:
        client.session.journal.put_nowait(item)
        # A second connection cannot see an uncommitted session transaction.
        with sqlite3.connect(client.session.filename) as persisted:
            rows = persisted.execute("SELECT value FROM vanguard_listener_facts").fetchall()
        facts = [json.loads(raw) for (raw,) in rows]
        assert len(facts) == 1 and facts[0]["kind"] == "gap"
        assert client._message_box.map[22].pts == 10
        await ingest.drain_ingest()
    finally:
        client.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["single", "total"])
async def test_single_item_and_total_byte_caps_reject_before_any_store_admission(worker, limit):
    if limit == "single":
        worker.max_item_bytes = 10
    else:
        worker.max_bytes = 10
    refs, gaps = ingest.submit_envelope(envelope(), interest(evidence_needed=True))
    assert not refs and gaps
    assert worker.pending_bytes == 0 and not worker.pending
    await ingest.drain_ingest()
    assert not (await worker.store.list_observations()).data["observations"]


@pytest.mark.asyncio
async def test_new_admission_capacity_rejection_keeps_existing_protection_unchanged(worker, monkeypatch):
    policy = interest(evidence_needed=True)
    posted = datetime.now(UTC) - timedelta(hours=1)
    ingest.submit_envelope(envelope(text="优惠出售套餐联系 https://one.example", posted=posted), policy)
    await ingest.drain_ingest()
    old = (await worker.store.list_observations()).data["observations"][0]
    monkeypatch.setattr(worker.store, "admit_observation_sync",
                        lambda **_: Obj(ok=False, reason="protected_capacity"))
    ingest.submit_envelope(envelope(201, text="优惠出售套餐联系 https://two.example", posted=posted), policy)
    await ingest.drain_ingest()
    rows = (await worker.store.list_observations()).data["observations"]
    assert len(rows) == 1 and rows[0]["handle"] == old["handle"]
    assert not rows[0]["control"]["facts"]
    assert ingest.cache_safe(2)


@pytest.mark.asyncio
async def test_disabled_admission_still_records_edits_on_existing_protected_version(worker, monkeypatch):
    policy = interest(evidence_needed=True)
    posted = datetime.now(UTC) - timedelta(hours=1)
    ingest.submit_envelope(envelope(text="优惠出售套餐联系 https://old.example", posted=posted), policy)
    await ingest.drain_ingest()
    old = (await worker.store.list_observations()).data["observations"][0]
    monkeypatch.setattr(runtime, "store_for", lambda _account: None)
    refs, gaps = ingest.submit_envelope(
        envelope(text="优惠出售套餐联系 https://new.example", posted=posted,
                 edited=datetime.now(UTC), edit=True), policy,
    )
    assert not refs and not gaps
    await ingest.drain_ingest()
    rows = (await worker.store.list_observations()).data["observations"]
    assert len(rows) == 1
    preserved = await worker.store.get_observation(old["handle"])
    assert preserved.data["control"]["facts"].get("edited")
    assert preserved.data["control"]["state"] == "invalidated"


@pytest.mark.asyncio
async def test_restart_keeps_preexisting_payload_and_marks_inmemory_coverage_unknown(worker):
    ingest.submit_envelope(
        envelope(text="优惠出售套餐联系 https://offer.example", posted=datetime.now(UTC) - timedelta(hours=1)),
        interest(evidence_needed=True),
    )
    await ingest.drain_ingest()
    old = (await worker.store.list_observations()).data["observations"][0]
    await ingest.stop_ingest_worker()
    restarted = await ingest.start_ingest_worker(store=worker.store)
    assert restarted is not None
    kept = await worker.store.get_observation(old["handle"])
    assert kept.ok and kept.data["control"]["facts"].get("gap")
    assert kept.data["records"]


@pytest.mark.asyncio
async def test_preinitialization_failure_never_enables_ingress_or_creates_a_background_task(monkeypatch):
    store = Obj(initialize=AsyncMock(return_value=Obj(ok=False, reason="disk_unavailable")))
    assert await ingest.start_ingest_worker(store=store) is None
    assert not ingest.cache_safe(2)
    assert ingest.submit_envelope(envelope(), interest()) == ({}, [])


@pytest.mark.asyncio
async def test_concurrent_start_reuses_one_serial_writer_and_shutdown_drains_it(worker):
    first, second = await asyncio.gather(
        ingest.start_ingest_worker(store=worker.store),
        ingest.start_ingest_worker(store=worker.store),
    )
    assert first is second is worker
    ingest.submit_envelope(envelope(), interest())
    await ingest.stop_ingest_worker()
    assert not worker.pending and not worker.account_pending
    assert worker.task.done() and worker.maintenance_task.done()


@pytest.mark.asyncio
async def test_maintenance_remains_independent_when_admission_is_disabled(worker, monkeypatch):
    monkeypatch.setattr(runtime, "store_for", lambda _account: None)
    monkeypatch.setattr(ingest, "MAINTENANCE_SECONDS", 0.01)
    done = asyncio.Event()

    async def maintain(*, account_id):
        assert account_id is None
        done.set()

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintain)
    # Restart only the independent maintenance task so it observes the new clock.
    worker.maintenance_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker.maintenance_task
    worker.maintenance_task = asyncio.create_task(worker._maintain())
    await asyncio.wait_for(done.wait(), timeout=1)
    assert not worker.pending and not worker.inflight


@pytest.mark.asyncio
async def test_real_store_flag_off_starts_existing_ledger_and_keeps_control_ingress(tmp_path, monkeypatch):
    store = ReviewEvidenceStore(small_config.__wrapped__(tmp_path))
    policy = interest(evidence_needed=True)
    posted = datetime.now(UTC)-timedelta(hours=1)
    first = envelope(text="优惠出售套餐联系 https://offer.example", posted=posted)
    runtime.capture_envelope(first, policy, store=store)
    rows = store.list_observations_sync(account_id=2).data["observations"]
    assert len(rows) == 1
    monkeypatch.setattr(runtime, "_store", store)
    monkeypatch.setattr(runtime, "_store_path", str(store.directory))
    monkeypatch.setattr(runtime.settings, "REVIEW_EVIDENCE_STORE_PATH", str(store.directory))
    monkeypatch.setattr(runtime.settings, "REVIEW_EVIDENCE_STORE_ENABLED", False)
    assert runtime.store_for(2) is None
    restarted = await ingest.start_ingest_worker()
    try:
        assert restarted is not None and restarted.maintenance_task is not None
        changed = envelope(text="新优惠出售套餐联系 https://changed.example", posted=posted,
                           edited=datetime.now(UTC), edit=True)
        refs, gaps = ingest.submit_envelope(changed, policy)
        assert not refs and not gaps
        await ingest.drain_ingest()
        retained = store.get_observation_sync(rows[0]["handle"]).data
        assert retained["control"]["facts"].get("edited") and retained["records"]
        assert len(store.list_observations_sync().data["observations"]) == 1
        assert store.stats_sync().data["ordinary_records"] == 0
    finally:
        await ingest.stop_ingest_worker()
