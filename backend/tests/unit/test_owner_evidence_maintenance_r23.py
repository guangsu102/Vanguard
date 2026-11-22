"""Owner review quanta never wait for the optional durable evidence handoff."""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.core.account import read_work, session_exec
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition import review_evidence_handoff as handoff
from app.modules.acquisition import review_evidence_ingest as ingest
from app.modules.acquisition.review_batch import current_batch


async def as_owner(call, account_id=33):
    async def execute(_account, _stage):
        return await call()
    return await session_exec._execute_in_actor(execute, account_id, "review")


@pytest_asyncio.fixture(autouse=True)
async def reset_scheduler(monkeypatch):
    await handoff.stop_owner_maintenance()
    monkeypatch.setattr(handoff, "_state", None)
    handoff.resume_owner_maintenance()
    monkeypatch.setattr(engine, "enabled_for", lambda account_id: account_id == 33)
    yield
    await handoff.stop_owner_maintenance()


@pytest.mark.asyncio
async def test_slow_maintenance_does_not_hold_review_and_inherits_no_business_context(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    caller = ContextVar("caller_db_lease", default=None)
    observed = []

    async def maintenance(**_kwargs):
        observed.append((session_exec.current_actor_account(), read_work.current(),
                         current_batch.get(), caller.get()))
        entered.set()
        await release.wait()

    async def reviews(_service, **_kwargs):
        return {"processed": 1}

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(qualification, "_run_reviews", reviews)
    read_token = read_work._current.set(SimpleNamespace(account_id=33))
    caller_token = caller.set(object())
    try:
        result = await asyncio.wait_for(as_owner(lambda: qualification.run_reviews(
            SimpleNamespace(account_pool=None), account_id=33)), 1)
        assert result == {"processed": 1}
        await asyncio.wait_for(entered.wait(), 1)
        assert not release.is_set()
        assert observed == [(None, None, None, None)]
    finally:
        read_work._current.reset(read_token)
        caller.reset(caller_token)
        release.set()


@pytest.mark.asyncio
async def test_non_owner_never_creates_a_background_task(monkeypatch):
    called = []

    async def maintenance(**_kwargs):
        called.append(True)

    async def reviews(_service, **_kwargs):
        return {"processed": 0, "reason": "session_owner_required"}

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(qualification, "_run_reviews", reviews)
    result = await qualification.run_reviews(SimpleNamespace(account_pool=None), account_id=33)
    assert result["reason"] == "session_owner_required"
    assert handoff._state is None and called == []


@pytest.mark.parametrize("account_id", [None, 2, 34])
@pytest.mark.asyncio
async def test_other_accounts_preserve_synchronous_maintenance(monkeypatch, account_id):
    entered, release = asyncio.Event(), asyncio.Event()
    reviewed = []

    async def maintenance(**kwargs):
        assert kwargs == {"account_id": account_id}
        entered.set()
        await release.wait()

    async def reviews(_service, **_kwargs):
        reviewed.append(True)
        return {"processed": 1}

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(qualification, "_run_reviews", reviews)
    task = asyncio.create_task(qualification.run_reviews(SimpleNamespace(account_pool=None), account_id=account_id))
    await asyncio.wait_for(entered.wait(), 1)
    assert not task.done() and reviewed == [] and handoff._state is None
    release.set()
    assert await task == {"processed": 1}


@pytest.mark.asyncio
async def test_disabled_new_engine_keeps_old_account_33_path(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "enabled_for", lambda _account: False)

    async def maintenance(**kwargs):
        calls.append(kwargs)

    async def reviews(_service, **_kwargs):
        return {"processed": 0}

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(qualification, "_run_reviews", reviews)
    await qualification.run_reviews(SimpleNamespace(account_pool=None), account_id=33)
    assert calls == [{"account_id": 33}] and handoff._state is None


@pytest.mark.asyncio
async def test_parallel_requests_are_single_flight_and_later_fact_is_not_lost(monkeypatch):
    entered, finish_first, paused, next_pass, second = (asyncio.Event() for _ in range(5))
    facts, snapshots = ["old"], []
    active = maximum = 0

    async def maintenance(**_kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        snapshots.append(tuple(facts))
        try:
            if len(snapshots) == 1:
                entered.set()
                await finish_first.wait()
            else:
                second.set()
        finally:
            active -= 1

    async def pause(seconds):
        assert 0 < seconds <= 300
        paused.set()
        await next_pass.wait()

    async def request_many():
        assert all(handoff.request_owner_maintenance(33) for _ in range(10))

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(handoff, "_pause", pause)
    await as_owner(request_many)
    await asyncio.wait_for(entered.wait(), 1)
    original = handoff._state.task
    facts.append("new-control")
    await as_owner(request_many)
    assert handoff._state.task is original
    finish_first.set()
    await asyncio.wait_for(paused.wait(), 1)
    assert snapshots == [("old",)]
    next_pass.set()
    await asyncio.wait_for(second.wait(), 1)
    await original
    assert snapshots == [("old",), ("old", "new-control")] and maximum == 1
    assert handoff._state.task is None and not handoff._state.pending


@pytest.mark.asyncio
async def test_finished_request_does_not_rescan_before_five_minute_cadence(monkeypatch):
    paused, release = asyncio.Event(), asyncio.Event()
    calls, waits = [], []

    async def maintenance(**_kwargs):
        calls.append(True)

    async def pause(seconds):
        waits.append(seconds)
        paused.set()
        await release.wait()

    async def request():
        assert handoff.request_owner_maintenance(33)

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(handoff, "_pause", pause)
    await as_owner(request)
    first = handoff._state.task
    await first
    await as_owner(request)
    await asyncio.wait_for(paused.wait(), 1)
    assert len(calls) == 1 and 299 <= waits[0] <= 300
    release.set()
    await handoff._state.task
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_exception_keeps_retry_cue_without_creating_retry_stream(monkeypatch):
    calls = []

    async def maintenance(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("injected local failure")

    async def request():
        return handoff.request_owner_maintenance(33)

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    assert await as_owner(request)
    first = handoff._state.task
    await first
    assert len(calls) == 1 and handoff._state.task is None and handoff._state.pending
    handoff._state.next_at = 0
    assert await as_owner(request)
    await handoff._state.task
    assert len(calls) == 2 and not handoff._state.pending


@pytest.mark.asyncio
async def test_cancel_retains_pending_and_shutdown_blocks_new_requests(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup = []

    async def maintenance(**_kwargs):
        entered.set()
        try:
            await release.wait()
        finally:
            cleanup.append("lease_cleanup_only")

    async def request():
        return handoff.request_owner_maintenance(33)

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    assert await as_owner(request)
    await asyncio.wait_for(entered.wait(), 1)
    await handoff.stop_owner_maintenance()
    assert handoff._state.pending and handoff._state.task is None
    assert cleanup == ["lease_cleanup_only"]
    assert not await as_owner(request)
    handoff.resume_owner_maintenance()
    handoff._state.next_at = 0
    release.set()
    assert await as_owner(request)
    await handoff._state.task
    assert len(cleanup) == 2


@pytest.mark.asyncio
async def test_cancel_during_cadence_wait_keeps_later_request(monkeypatch):
    paused = asyncio.Event()

    async def maintenance(**_kwargs):
        return {}

    async def pause(_seconds):
        paused.set()
        await asyncio.Event().wait()

    async def request():
        return handoff.request_owner_maintenance(33)

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(handoff, "_pause", pause)
    await as_owner(request)
    await handoff._state.task
    await as_owner(request)
    await asyncio.wait_for(paused.wait(), 1)
    await handoff.stop_owner_maintenance()
    assert handoff._state.pending and handoff._state.task is None


@pytest.mark.asyncio
async def test_owner_identity_and_engine_scope_are_checked_before_state_creation(monkeypatch):
    async def request():
        return handoff.request_owner_maintenance(33)

    assert not await as_owner(request, account_id=34)
    assert handoff._state is None
    monkeypatch.setattr(engine, "enabled_for", lambda _account: False)
    assert not await as_owner(request)
    assert handoff._state is None


@pytest.mark.asyncio
async def test_ingest_lifecycle_stops_background_even_without_ingest_worker(monkeypatch):
    entered = asyncio.Event()

    async def maintenance(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def request():
        return handoff.request_owner_maintenance(33)

    async def start(_store):
        return SimpleNamespace()

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(ingest, "_worker", None)
    monkeypatch.setattr(ingest, "_start", start)
    await as_owner(request)
    await asyncio.wait_for(entered.wait(), 1)
    await ingest.stop_ingest_worker()
    assert handoff._state.task is None and handoff._stopping
    await ingest.start_ingest_worker()
    assert not handoff._stopping


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_background_failure_or_cancel_keeps_real_protected_payload(tmp_path, monkeypatch, cancel):
    from app.modules.acquisition import review_read_runtime as runtime
    from app.modules.acquisition.review_evidence_store import ReviewEvidenceStore
    from tests.unit.test_review_evidence_store import config as small_config

    store = ReviewEvidenceStore(small_config.__wrapped__(tmp_path))
    assert store.initialize_sync().ok
    now = datetime.utcnow()
    admitted = store.admit_observation_sync(
        account_id=33, group_id=-1001234567, message_id=102, version="v1",
        joined_at=now-timedelta(days=3), date=now-timedelta(hours=32),
        records=[{"key": "retained", "body": "protected original material"}],
        now=now-timedelta(hours=31),
    )
    assert admitted.ok
    entered = asyncio.Event()

    async def transfer(_store, _handle, _now):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise OSError("injected handoff failure before commit")

    async def request():
        return handoff.request_owner_maintenance(33)

    monkeypatch.setattr(runtime, "maintenance_store", lambda _account=None: store)
    monkeypatch.setattr(handoff, "_transfer", transfer)
    await as_owner(request)
    task = handoff._state.task
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        await handoff.stop_owner_maintenance()
    else:
        await task
    retained = store.get_observation_sync(admitted.handle)
    assert retained.ok and retained.data["records"][0]["body"] == "protected original material"
    assert handoff._state.task is None


@pytest.mark.asyncio
async def test_owner_stage_cancellation_does_not_cancel_lifecycle_owned_maintenance(monkeypatch):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def maintenance(**_kwargs):
        entered.set()
        await release.wait()
        assert session_exec.current_actor_account() is None
        finished.set()

    async def reviews(_service, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(handoff, "maintain_evidence_store", maintenance)
    monkeypatch.setattr(qualification, "_run_reviews", reviews)
    parent = asyncio.create_task(as_owner(lambda: qualification.run_reviews(
        SimpleNamespace(account_pool=None), account_id=33)))
    await asyncio.wait_for(entered.wait(), 1)
    task = handoff._state.task
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert not task.cancelled() and not task.done()
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await task
