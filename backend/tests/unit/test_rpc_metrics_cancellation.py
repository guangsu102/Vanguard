"""Real fixed-file SDK entry semantics across cancellation and delayed writes."""

import asyncio
import mmap
import struct
import threading
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telethon.tl.functions.updates import GetStateRequest

from app.core.account import read_metrics as metrics
from app.core.account import read_work, rpc_governor

T0 = 1_799_712_000


@pytest_asyncio.fixture(loop_scope="module")
async def meter(tmp_path, monkeypatch):
    clock = [T0]
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_vanguard_read_metrics_state", None)
    loop._vanguard_read_metrics_state = None
    store = metrics.FixedReadMetrics(tmp_path / "meter")
    monkeypatch.setattr(metrics, "_store", lambda: store)
    monkeypatch.setattr(metrics.time, "time", lambda: clock[0])
    monkeypatch.setattr(metrics, "start_metrics_health", lambda: None)
    monkeypatch.setattr("app.core.account.send_receipts.record_request", AsyncMock())
    work = read_work.ReadWork(34, 1, "review")
    work.measurement_stage = "phase1"
    token = read_work._current.set(work)
    try:
        yield Obj(store=store, clock=clock, work=work)
    finally:
        await metrics.stop_metrics_health()
        read_work._current.reset(token)
        loop._vanguard_read_metrics_state = previous


def client_with(original):
    client = Obj(_call=original)
    governor = Obj(account_id=34, purpose=lambda: "group_qualification", before=AsyncMock(),
                   failed=AsyncMock(), succeeded=AsyncMock())
    rpc_governor.install_governor(client, governor)
    return client


async def flush_io():
    state = metrics._state()
    if state.inflight is not None:
        await asyncio.wait_for(asyncio.shield(state.inflight), 5)
    await asyncio.sleep(0)


def full_hour(meter):
    state = metrics._state()
    pending = min((entry.at for entry in state.pending_sdk.values()), default=None)
    for second in range(90, 3601, 90):
        meter.store.add(0, "unknown", {}, at=T0 + second, source_token=state.token,
                        pending_since=pending, gap_since=state.pending_gap)
    return meter.store.snapshot(34, now=T0 + 3601, hours=1)


@pytest.mark.asyncio(loop_scope="module")
async def test_cancel_during_committed_pending_marker_never_counts_sdk_entry(meter, monkeypatch):
    committed, release = threading.Event(), threading.Event()
    add = meter.store.add

    def hold_pending(*args, **kwargs):
        add(*args, **kwargs)
        if kwargs.get("pending_since") is not None:
            committed.set()
            release.wait(5)

    monkeypatch.setattr(meter.store, "add", hold_pending)
    original = AsyncMock(return_value=Obj())
    task = asyncio.create_task(client_with(original)._call(None, GetStateRequest()))
    try:
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await flush_io()
        monkeypatch.setattr(meter.store, "add", add)
    result = full_hour(meter)
    assert original.await_count == meter.work.entered_reads == 0
    assert result["known_entered_sdk"] == 0
    assert result["actual_reads"] in (None, 0)
    assert not result["complete"]  # Cancellation during the marker leaves a conservative gap.


@pytest.mark.asyncio(loop_scope="module")
async def test_cancel_inside_sdk_counts_one_actual_entry_and_preserves_cancellation(meter):
    entered = asyncio.Event()

    async def sdk(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    original = AsyncMock(side_effect=sdk)
    task = asyncio.create_task(client_with(original)._call(None, GetStateRequest()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = full_hour(meter)
    assert original.await_count == meter.work.entered_reads == result["actual_reads"] == 1
    assert result["complete"] and result["pending_sdk_since"] is None
    assert sum(row["error"] for stage in result["by_stage"] for row in stage["rpc"]) == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_second_cancel_during_committed_entry_write_never_becomes_complete_zero(meter, monkeypatch):
    entered = asyncio.Event()
    committed, release = threading.Event(), threading.Event()
    add = meter.store.add

    async def sdk(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    def hold_entered(account, stage, increments, **kwargs):
        add(account, stage, increments, **kwargs)
        if any(index < metrics.RPC_COUNT and index % 4 == 1 for index in increments):
            committed.set()
            release.wait(5)

    monkeypatch.setattr(meter.store, "add", hold_entered)
    original = AsyncMock(side_effect=sdk)
    task = asyncio.create_task(client_with(original)._call(None, GetStateRequest()))
    try:
        await entered.wait()
        task.cancel()
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await flush_io()
        monkeypatch.setattr(meter.store, "add", add)
    result = full_hour(meter)
    assert original.await_count == meter.work.entered_reads == result["known_entered_sdk"] == 1
    assert not result["complete"] and result["actual_reads"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_failure_before_entry_write_remains_unknown_after_healthy_heartbeats(meter, monkeypatch):
    add = meter.store.add

    def fail_entered(account, stage, increments, **kwargs):
        if any(index < metrics.RPC_COUNT and index % 4 == 1 for index in increments):
            raise OSError("before durable transaction")
        add(account, stage, increments, **kwargs)

    monkeypatch.setattr(meter.store, "add", fail_entered)
    original = AsyncMock(return_value=Obj())
    await client_with(original)._call(None, GetStateRequest())
    result = full_hour(meter)
    assert original.await_count == meter.work.entered_reads == 1
    assert result["known_entered_sdk"] == 0
    assert not result["complete"] and result["actual_reads"] is None
    assert metrics._state().pending_sdk


@pytest.mark.asyncio(loop_scope="module")
async def test_marker_timeout_cannot_turn_a_late_old_transaction_into_complete_zero(meter, monkeypatch):
    started, release_io = threading.Event(), threading.Event()
    entered, release_sdk = asyncio.Event(), asyncio.Event()
    state = metrics._state()
    for second in range(0, 3591, 90):
        meter.store.add(34, "phase1", {}, at=T0 + second, source_token=state.token)
    meter.clock[0] = T0 + 3599
    add = meter.store.add

    def old_transaction(*args, **kwargs):
        started.set()
        release_io.wait(5)
        add(*args, **kwargs)

    async def sdk(*args, **kwargs):
        entered.set()
        await release_sdk.wait()
        return Obj()

    monkeypatch.setattr(meter.store, "add", old_transaction)
    old = asyncio.create_task(metrics._persist(0, "unknown", {}, start_health=False))
    assert await asyncio.to_thread(started.wait, 3)
    original = AsyncMock(side_effect=sdk)
    task = asyncio.create_task(client_with(original)._call(None, GetStateRequest()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert meter.work.entered_reads == 1
        meter.clock[0] = T0 + 3601
        await metrics._persist(0, "unknown", {}, start_health=False)
        before = meter.store.snapshot(34, now=meter.clock[0], hours=1)
        assert not before["complete"] and before["actual_reads"] is None
        release_io.set()
        await old
        await flush_io()
        monkeypatch.setattr(meter.store, "add", add)
        late = meter.store.snapshot(34, now=meter.clock[0], hours=1)
        assert late["sources_confirmed_through"] == T0 + 3599
        assert not late["complete"] and late["actual_reads"] is None
        await metrics._persist(0, "unknown", {}, start_health=False)
        after = meter.store.snapshot(34, now=meter.clock[0], hours=1)
        assert not after["complete"] and after["actual_reads"] is None
    finally:
        release_io.set()
        release_sdk.set()
        await old
        await task
    final = meter.store.snapshot(34, now=meter.clock[0], hours=1)
    assert final["known_entered_sdk"] == 1
    assert not final["complete"]


@pytest.mark.asyncio(loop_scope="module")
async def test_long_sdk_call_is_incomplete_until_recorded_in_original_hour_stage_and_purpose(meter):
    entered, release = asyncio.Event(), asyncio.Event()
    state = metrics._state()
    for second in range(0, 3591, 90):
        meter.store.add(34, "phase1", {}, at=T0 + second, source_token=state.token)
    meter.clock[0] = T0 + 3599

    async def sdk(*args, **kwargs):
        entered.set()
        await release.wait()
        return Obj()

    original = AsyncMock(side_effect=sdk)
    task = asyncio.create_task(client_with(original)._call(None, GetStateRequest()))
    await entered.wait()
    meter.clock[0] = T0 + 3601
    meter.work.measurement_stage = "phase2"
    await metrics._persist(0, "unknown", {})
    pending = meter.store.snapshot(34, now=meter.clock[0], hours=1)
    assert not pending["complete"] and pending["reason"] == "sdk_entry_pending"
    release.set()
    await task
    result = meter.store.snapshot(34, now=meter.clock[0], hours=1)
    assert result["complete"] and result["actual_reads"] == 1
    assert result["by_stage"][1]["entered_sdk"] == 1
    assert result["by_stage"][2]["entered_sdk"] == 0
    assert result["by_stage"][1]["rpc"][0]["purpose"] == "review"
    assert result["sources_confirmed_through"] == T0 + 3601


@pytest.mark.asyncio(loop_scope="module")
async def test_resolving_one_entry_does_not_clear_another_sources_pending_rpc(meter):
    first = await metrics.prepare_sdk_entry(34, ["channels.GetMessagesRequest"], purpose="group_qualification")
    metrics.enter_sdk(first)
    meter.clock[0] = T0 + 1
    second = await metrics.prepare_sdk_entry(35, ["channels.GetMessagesRequest"], purpose="authorization_renewal")
    metrics.enter_sdk(second)
    await metrics.finish_sdk_entry(second, "success")
    assert list(metrics._state().pending_sdk.values()) == [first]
    pending = full_hour(meter)
    assert not pending["complete"] and pending["pending_sdk_since"] == T0
    await metrics.finish_sdk_entry(first, "success")
    result = full_hour(meter)
    assert result["complete"] and result["actual_reads"] == 1
    assert not metrics._state().pending_sdk


def test_source_expiry_preserves_old_pending_hour_without_resizing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics.time, "time", lambda: T0)
    store = metrics.FixedReadMetrics(tmp_path / "expiry")
    store.add(34, "phase1", {}, at=T0, source_token=1, pending_since=T0)
    # A recent heartbeat must not erase the earlier unresolved SDK boundary.
    store.add(0, "unknown", {}, at=T0 + 90, source_token=1, pending_since=T0)
    expired = store.snapshot(34, now=T0 + 3601, hours=1)
    assert not expired["complete"] and expired["actual_reads"] is None
    assert expired["gap_hours"] == [T0]
    assert (store.directory / metrics.FILENAME).stat().st_size == metrics.FILE_BYTES


def test_legacy_header_fences_old_semantics_and_cutover_only_advances(tmp_path, monkeypatch):
    clock = [T0]
    monkeypatch.setattr(metrics.time, "time", lambda: clock[0])
    store = metrics.FixedReadMetrics(tmp_path / "legacy")
    store.add(34, "phase1", {1: 2}, at=T0, source_token=1)
    for second in range(90, 3601, 90):
        store.add(0, "unknown", {}, at=T0 + second, source_token=1)
    assert store.snapshot(34, now=T0 + 3601, hours=1)["actual_reads"] == 2
    # Existing v1 files contain zero in the formerly reserved header field.
    with open(store.directory / metrics.FILENAME, "r+b") as handle:
        with mmap.mmap(handle.fileno(), metrics.FILE_BYTES) as data:
            struct.pack_into("<d", data, metrics.SEMANTICS_AT, 0)
            data.flush()
    clock[0] = T0 + 3601
    legacy = store.snapshot(34, now=clock[0], hours=1)
    assert legacy["known_entered_sdk"] == 2  # Historical aggregates are untouched.
    assert not legacy["complete"] and legacy["actual_reads"] is None
    assert legacy["reason"] == "window_before_semantics"
    assert legacy["measurement_version"] == "sdk_entry_v2"
    assert legacy["measurement_semantics_started_at"] == clock[0]
    final_cutover = store.mark_semantics_cutover(T0 + 3691)
    assert final_cutover == T0 + 3691
    assert store.mark_semantics_cutover(T0 + 3600) == final_cutover
    for second in range(3690, 10801, 90):
        clock[0] = T0 + second
        store.add(0, "unknown", {}, at=clock[0], source_token=1)
    store.add(34, "phase1", {1: 5}, at=T0 + 7201, source_at=T0 + 10800, source_token=1)
    full = store.snapshot(34, now=T0 + 10801, hours=1)
    assert full["complete"] and full["actual_reads"] == 5
    assert full["measurement_semantics_started_at"] == final_cutover
    assert (store.directory / metrics.FILENAME).stat().st_size == metrics.FILE_BYTES
    with pytest.raises(ValueError, match="invalid_measurement_cutover"):
        store.mark_semantics_cutover(float("nan"))


@pytest.mark.asyncio(loop_scope="module")
async def test_fixed_cache_gate_enums_persist_with_work_without_unbounded_labels(meter):
    assert metrics.mark_history_cache_gate("ingest")
    assert metrics.mark_history_cache_gate("hit")
    assert not metrics.mark_history_cache_gate("message-private-body")
    await metrics.record_work(meter.work)
    result = full_hour(meter)["by_stage"][1]["history_cache_gates"]
    assert result["ingest"] == result["hit"] == 1
    assert set(result) == set(metrics.CACHE_GATE_REASONS)
    assert sum(result.values()) == 2
