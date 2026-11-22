"""Bounded writer batches retain SDK uncertainty across cancellation and failures."""
import asyncio
import threading
import struct

import pytest
import pytest_asyncio

from app.core.account import read_metrics as metrics

T0 = 1_799_712_000


@pytest.mark.parametrize("scope,expected", [("33", True), ("33, 34", True), ("34", False),
    ("", False), ("33,all", False), ("33,", False), ("-33", False), ("33 34", False)])
def test_new_engine_stage_and_batching_use_its_independent_strict_allowlist(monkeypatch, scope, expected):
    from app.core.config import settings
    from app.modules.acquisition.review_read_flags import measurement_stage
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", scope)
    monkeypatch.setattr(settings, "REVIEW_READ_OPTIMIZATION_ENABLED", False)
    monkeypatch.setattr(settings, "REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS", "34")
    assert metrics._batching_enabled(33) is expected
    assert measurement_stage(33) == ("phase2" if expected else "baseline")


@pytest_asyncio.fixture
async def meter(tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    prior = getattr(loop, '_vanguard_read_metrics_state', None)
    loop._vanguard_read_metrics_state = None
    clock = [T0]
    store = metrics.FixedReadMetrics(tmp_path / 'metrics')
    monkeypatch.setattr(metrics, '_store', lambda: store)
    monkeypatch.setattr(metrics, '_batching_enabled', lambda account_id: account_id > 0)
    monkeypatch.setattr(metrics, 'start_metrics_health', lambda: None)
    monkeypatch.setattr(metrics.time, 'time', lambda: clock[0])
    yield store, clock
    await metrics.stop_metrics_health()
    loop._vanguard_read_metrics_state = prior


def full_hour(store):
    state = metrics._state()
    for second in range(90, 3601, 90):
        store.add(0, 'unknown', {}, at=T0 + second, source_token=state.token,
                  gap_since=state.pending_gap,
                  pending_since=min((entry.at for entry in state.pending_sdk.values()), default=None))
    return store.snapshot(34, now=T0 + 3601, hours=1)


@pytest.mark.asyncio
async def test_concurrent_telemetry_commits_one_bounded_batch_without_lost_counts(meter, monkeypatch):
    store, _ = meter
    calls = []
    original = store.add_batch
    def write(rows, **kwargs):
        calls.append(len(rows))
        return original(rows, **kwargs)
    monkeypatch.setattr(store, 'add_batch', write)
    await asyncio.gather(*(metrics._persist(34, 'phase2', {1: 1}) for _ in range(20)))
    assert calls == [20]
    result = full_hour(store)
    assert result['complete'] and result['actual_reads'] == 20
    assert result['diagnostics']['source_lifecycle']['registered'] == 1
    assert (store.directory / metrics.FILENAME).stat().st_size == metrics.FILE_BYTES


@pytest.mark.asyncio
async def test_batched_pending_boundary_stays_unknown_until_all_entries_are_resolved(meter):
    store, _ = meter
    first, second = await asyncio.gather(*(
        metrics.prepare_sdk_entry(34, ['channels.GetMessagesRequest'], purpose='group_qualification')
        for _ in range(2)))
    metrics.enter_sdk(first)
    metrics.enter_sdk(second)
    await metrics.finish_sdk_entry(first, 'success')
    pending = full_hour(store)
    assert not pending['complete'] and pending['pending_sdk_since'] == T0
    await metrics.finish_sdk_entry(second, 'success')
    result = full_hour(store)
    assert result['complete'] and result['actual_reads'] == 2
    assert not metrics._state().pending_sdk


@pytest.mark.asyncio
async def test_caller_cancel_does_not_cancel_writer_or_claim_complete_zero(meter, monkeypatch):
    store, _ = meter
    original = store.add_batch
    entered, release = threading.Event(), threading.Event()
    def delayed(rows, **kwargs):
        entered.set()
        release.wait(4)
        original(rows, **kwargs)
    monkeypatch.setattr(store, 'add_batch', delayed)
    task = asyncio.create_task(metrics._persist(34, 'phase2', {1: 1}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.shield(metrics._state().batch_runner)
        monkeypatch.setattr(store, 'add_batch', original)
    await metrics._persist(0, 'unknown', {})
    result = full_hour(store)
    assert result['known_entered_sdk'] == 1 and result['actual_reads'] is None
    assert result['diagnostics']['gap_reasons']['cancelled_pending'] >= 1


@pytest.mark.asyncio
async def test_repeated_writer_cancellation_keeps_io_permit_until_disk_finishes(meter, monkeypatch):
    store, _ = meter
    original = store.add_batch
    entered, release = threading.Event(), threading.Event()
    def delayed(rows, **kwargs):
        entered.set()
        release.wait(4)
        original(rows, **kwargs)
    monkeypatch.setattr(store, 'add_batch', delayed)
    caller = asyncio.create_task(metrics._persist(34, 'phase2', {1: 1}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        state = metrics._state()
        disk = state.inflight
        state.batch_runner.cancel()
        state.batch_runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await state.batch_runner
        assert state.semaphore.locked()
        assert not disk.done() and not caller.done()
    finally:
        release.set()
        await asyncio.shield(disk)
        await caller
        monkeypatch.setattr(store, 'add_batch', original)
    assert not metrics._state().semaphore.locked()
    await metrics._persist(0, 'unknown', {})
    result = full_hour(store)
    assert result['known_entered_sdk'] == 1 and result['actual_reads'] is None


@pytest.mark.asyncio
async def test_failed_sdk_confirmation_keeps_pending_and_reports_fixed_reason(meter, monkeypatch):
    store, _ = meter
    entry = await metrics.prepare_sdk_entry(34, ['channels.GetMessagesRequest'])
    metrics.enter_sdk(entry)
    original = store.add_batch
    def fail(*args, **kwargs):
        raise OSError('private path must not become a label')
    monkeypatch.setattr(store, 'add_batch', fail)
    await metrics.finish_sdk_entry(entry, 'success')
    assert id(entry) in metrics._state().pending_sdk
    monkeypatch.setattr(store, 'add_batch', original)
    await metrics._persist(0, 'unknown', {})
    result = full_hour(store)
    assert not result['complete'] and result['actual_reads'] is None
    assert result['diagnostics']['gap_reasons']['io_error'] >= 1
    assert set(result['diagnostics']['gap_reasons']) == set(metrics.GAP_REASONS)


@pytest.mark.asyncio
async def test_queue_overflow_is_bounded_and_incomplete_instead_of_silent_loss(meter):
    store, _ = meter
    await asyncio.gather(*(metrics._persist(34, 'phase2', {1: 1})
                           for _ in range(metrics.BATCH_QUEUE_LIMIT + 1)))
    await metrics._persist(0, 'unknown', {})
    result = full_hour(store)
    assert result['known_entered_sdk'] == metrics.BATCH_QUEUE_LIMIT
    assert not result['complete'] and result['actual_reads'] is None
    assert result['diagnostics']['gap_reasons']['batch_overflow'] == 1
    assert not metrics._state().batch_queue


@pytest.mark.asyncio
async def test_clean_close_and_expired_source_have_distinct_lifecycle_counters(meter):
    store, _ = meter
    store.add(34, 'phase2', {}, at=T0, source_token=11)
    store.close_source(11)
    store.add(34, 'phase2', {}, at=T0, source_token=22)
    result = store.snapshot(34, now=T0 + 3601, hours=1)
    lifecycle = result['diagnostics']['source_lifecycle']
    assert lifecycle['registered'] == 2 and lifecycle['closed'] == 1 and lifecycle['expired'] == 1
    assert result['diagnostics']['gap_reasons']['source_expired'] == 1
    assert not result['complete']


@pytest.mark.asyncio
async def test_six_roles_preserve_original_timestamps_counts_and_fixed_file(meter):
    store, _ = meter
    expected = metrics.ROLES
    for index, role in enumerate(expected):
        writer = metrics.FixedReadMetrics(store.directory, role=role, expected_roles=expected)
        writer.add(34, 'phase2', {1: 1}, at=T0 + index, source_token=100 + index)
    reader = metrics.FixedReadMetrics(store.directory, expected_roles=expected)
    result = reader.snapshot(34, now=T0 + 30, hours=1)
    assert result['active_roles'] == dict.fromkeys(expected, 1)
    assert result['missing_roles'] == []
    assert not result['source_capacity_exceeded'] and result['source_slots_limit'] == 32
    assert result['diagnostics']['source_lifecycle']['registered'] == 6
    assert not any(result['diagnostics']['gap_reasons'].values())
    with store._open() as data:
        assert [struct.unpack_from('<d', data, metrics.ROLE_AT + index * 8)[0]
                for index in range(3)] == [T0, T0 + 1, T0 + 2]
        assert [struct.unpack_from('<d', data, metrics.EXTRA_ROLE_AT + index * 8)[0]
                for index in range(3)] == [T0 + 3, T0 + 4, T0 + 5]
        offset = metrics.HEADER_BYTES + (T0 // 3600 % metrics.HOURS) * metrics.HOUR_BYTES
        offset += metrics.STAGES.index('phase2') * metrics.METRICS * 8 + 8
        assert metrics._u64(data, offset) == 6
    assert (store.directory / metrics.FILENAME).stat().st_size == metrics.FILE_BYTES


@pytest.mark.asyncio
async def test_missing_extra_role_never_claims_complete_coverage(meter):
    store, _ = meter
    for index, role in enumerate(metrics.ROLES[:3]):
        writer = metrics.FixedReadMetrics(store.directory, role=role)
        writer.add(34, 'phase2', {}, at=T0, source_token=100 + index)
    result = metrics.FixedReadMetrics(store.directory, expected_roles=metrics.ROLES).snapshot(34, now=T0 + 30)
    assert not result['complete'] and result['actual_reads'] is None
    assert result['reason'] == 'expected_source_roles_missing'
    assert result['missing_roles'] == ['reconciliation', 'resource', 'guardian']
