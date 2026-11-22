"""History reuse exercises the real ingest worker and durable SDK journal."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj

import pytest
import pytest_asyncio
from telethon._updates.session import ChannelState, SessionState

from app.core.account import session_exec
from app.modules.acquisition import review_evidence_ingest as ingest
from app.modules.acquisition import review_history_progress as pages
from app.modules.acquisition.group_qualification import EvidenceCollector
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, ReadOnlyClient, message
from tests.unit.test_listener_journal import client as listener_client
from tests.unit.test_session_exec import FakeRedis


@pytest_asyncio.fixture(loop_scope="module")
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled",
                        lambda _account, stage: stage == "resume")
    now = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    scope = {"membership_id": 91, "joined_at": (now-timedelta(days=9)).isoformat(),
             "event": {}, "policy": "test-v3", "profile": "p1", "system_identity": [9]}
    store = ReviewEvidenceStore(EvidenceStoreConfig(directory=tmp_path / "pages", enabled=True,
        protected_slots=1, protected_slot_bytes=64*1024, protected_control_bytes=4096,
        protected_records_per_slot=8, protected_record_limit=8, slots_per_account=1,
        slots_per_account_group=1, ordinary_pages=1, ordinary_page_bytes=1024*1024,
        physical_limit_bytes=4*1024*1024))
    worker = await ingest.start_ingest_worker(store=store)
    assert worker is not None
    sdk = listener_client(tmp_path)
    sdk._message_box.load(
        SessionState(99, 1, False, 10, 0, int(now.replace(tzinfo=UTC).timestamp()), 0, None),
        [ChannelState(42, 10)],
    )
    history = [message(100, age=1), message(50, age=30), message(10, age=54)]
    for item in history:
        item.date += now-NOW
    client = ReadOnlyClient(history)
    client.session, client._message_box, client._sender = sdk.session, sdk._message_box, sdk._sender

    def collector(previous=None):
        value = EvidenceCollector(client, account_id=34, history_scope=scope,
                                  history_store=store, enable_on_demand_identity=False)
        if previous:
            value.previous = previous
        return value

    try:
        yield Obj(now=now, scope=scope, store=store, worker=worker, sdk=sdk,
                  client=client, collector=collector, group=pages.group_key(ENTITY))
    finally:
        await ingest.stop_ingest_worker()
        sdk.session.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_owner_continuation_uses_real_ingest_and_journal_without_extra_history(runtime):
    assert ingest.cache_safe(34)
    assert ingest.cache_state(34)["reason"] == "ready"
    broker = FakeRedis()
    seen = []

    async def execute(account_id, stage):
        assert account_id == 34
        assert stage == "review"
        seen.append((account_id, stage))
        first = await runtime.collector().collect(ENTITY, now=runtime.now)
        assert runtime.store.stats_sync().data["lifecycle_counters"]["cache_writes"] > 0
        runtime.client.calls.clear()
        second = await runtime.collector(first).collect(ENTITY, now=runtime.now)
        history = [kw for kind, kw in runtime.client.calls
                   if kind == "messages" and "filter" not in kw]
        assert not history
        assert second["history_cached_count"] == 3
        assert runtime.store.stats_sync().data["lifecycle_counters"]["cache_hits"] > 0
        return {"processed": 1}, 30

    server = asyncio.create_task(session_exec.serve_requests(
        broker, owns=lambda account: account == 34, execute_stage=execute,
        poll_interval=0.001, max_concurrency=1,
    ))
    try:
        # Exercise the production owner acceptance and callback path itself;
        # a foreign account must not reach the collector or borrow this client.
        assert await session_exec.forward(35, "review", client=broker, timeout=5) == {"error": "not_owner"}
        assert seen == []
        reply = await session_exec.forward(34, "review", client=broker, timeout=5)
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert reply == {"result": {"processed": 1}, "interval": 30}
    assert seen == [(34, "review")]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("condition,reason", [
    ("controls", "ingest_controls_pending"), ("pending", "ingest_account_pending"),
    ("gap", "sdk_gap"), ("fact", "fact_scope_changed"),
    ("watermark", "sdk_watermark_unavailable"),
])
async def test_actual_owner_runtime_fails_closed_and_reports_fixed_reason(runtime, condition, reason):
    before = pages.diagnostics().get(reason, 0)
    if condition == "controls":
        runtime.worker.controls_pending = True
    elif condition == "pending":
        runtime.worker.account_pending[34] = 1
    elif condition == "gap":
        runtime.sdk._message_box.possible_gaps[42] = True
    elif condition == "fact":
        cursor = runtime.sdk.session._cursor()
        cursor.execute("INSERT INTO vanguard_listener_facts VALUES(?,?)", (
            "rules:target", '{"kind":"rules","peer":-1000000000042}'))
        cursor.close()
    else:
        runtime.sdk._message_box.map.pop(42)
    assert pages.fingerprint(runtime.client, 34, runtime.group, runtime.scope) is None
    assert pages.diagnostics()[reason] == before + 1
    # Restore synthetic waiting flags so shutdown does not invent a control job.
    runtime.worker.controls_pending = False
    runtime.worker.account_pending.clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_plain_short_connection_keeps_fresh_fallback_and_records_sdk_gate(runtime):
    runtime.client.session = Obj()
    before = pages.diagnostics().get("sdk_state_unavailable", 0)
    snapshot = await runtime.collector().collect(ENTITY, now=runtime.now)
    assert snapshot["history_complete"]
    assert snapshot.get("history_cached_count", 0) == 0
    assert runtime.store.stats_sync().data["lifecycle_counters"]["cache_writes"] == 0
    assert pages.diagnostics()["sdk_state_unavailable"] > before


@pytest.mark.asyncio(loop_scope="module")
async def test_real_worker_shutdown_and_reconnect_never_lend_old_generation(runtime):
    identity = pages.fingerprint(runtime.client, 34, runtime.group, runtime.scope)
    assert identity is not None
    runtime.client._sender = Obj(_connection=Obj())
    assert pages.fingerprint(runtime.client, 34, runtime.group, runtime.scope) != identity
    await ingest.stop_ingest_worker()
    assert ingest.cache_state(34) == {"safe": False, "reason": "ingest_unavailable"}
    assert pages.fingerprint(runtime.client, 34, runtime.group, runtime.scope) is None
