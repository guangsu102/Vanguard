"""Bounded off-thread diagnostics; no database/network/business clients."""
import asyncio
from datetime import datetime
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.acquisition import review_diagnostics as diag
from app.modules.acquisition import qualification_sla_runtime as runtime


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(diag, "_store", diag._store)
    monkeypatch.setattr(runtime, "_diagnostic_pid", runtime.os.getpid())
    monkeypatch.setattr(runtime, "_diagnostic_last_at", 0.0)
    monkeypatch.setattr(runtime, "_diagnostic_thread", None)
    monkeypatch.setattr(runtime, "_diagnostic_schedule_lock", threading.Lock())
    monkeypatch.setattr(runtime, "_staged_count", 0)
    monkeypatch.setattr(runtime, "_unconfigured_losses", {})
    monkeypatch.setattr(runtime, "_global_loss_since", None)
    yield
    thread = runtime._diagnostic_thread
    if isinstance(thread, threading.Thread):
        thread.join(2)
        assert not thread.is_alive()


def schedule(error=None):
    runtime._schedule_write_diagnostic(error or RuntimeError("secret-never-persist"),
        role="growth", stage=32, phase=5, batch_size=1)


def finish():
    thread = runtime._diagnostic_thread
    assert thread is not None and thread.daemon
    thread.join(2)
    assert not thread.is_alive()


def test_real_fixed_ring_contains_numeric_codes_and_no_error_arguments(tmp_path):
    store = diag.configure(tmp_path)
    values = {"__name__": "app.modules.acquisition.qualification_sla_runtime"}
    exec("def _write():\n    raise RuntimeError('DSN-secret-SQL-body')", values)
    try:
        values["_write"]()
    except RuntimeError as error:
        error.orig = SimpleNamespace(sqlstate="57014")
        schedule(error)
    finish()
    assert store.path.stat().st_size == 2 * 1024 * 1024
    assert b"DSN-secret-SQL-body" not in store.path.read_bytes()
    rows = store.snapshot()
    assert len(rows) == 1 and rows[0]["event"] == "review_metrics_manager_health_failed"
    assert rows[0]["reason"] == "RuntimeError"
    counts = rows[0]["counts"]
    assert counts["phase_code"] == 5 and counts["role_code"] == 2
    assert counts["sqlstate_code"] == int("57014", 36)
    assert counts["frame_0_module_code"] == 201 and counts["frame_0_function_code"] == 202
    assert all(type(value) is int for value in counts.values())


def test_sixty_second_process_limit_precedes_thread_start(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    append = Mock()
    monkeypatch.setattr(diag, "append", append)
    schedule(); finish()
    clock[0] = 69.9
    schedule()
    assert append.call_count == 1
    clock[0] = 70.0
    schedule(); finish()
    assert append.call_count == 2


@pytest.mark.asyncio
async def test_slow_disk_never_blocks_loop_or_accumulates_thread_or_queue(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    clock = [10.0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    def blocked(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(diag, "append", blocked)
    try:
        schedule()
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.001)
        assert entered.is_set()
        original = runtime._diagnostic_thread
        clock[0] = 80.0
        for _ in range(20):
            schedule()
        marker = []
        await asyncio.sleep(0)
        marker.append("loop_progressed")
        assert marker and runtime._diagnostic_thread is original and len(calls) == 1
    finally:
        release.set()
        finish()


def test_diagnostic_disk_error_is_optional_and_has_no_recursive_logging(monkeypatch):
    append = Mock(side_effect=OSError("disk-path-secret"))
    monkeypatch.setattr(diag, "append", append)
    schedule(); finish()
    assert append.call_count == 1


def test_concurrent_callers_can_start_only_one_daemon_writer(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    barrier = threading.Barrier(11)
    calls = []
    def blocked(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(diag, "append", blocked)
    def caller():
        barrier.wait()
        schedule()
    callers = [threading.Thread(target=caller) for _ in range(10)]
    try:
        for thread in callers:
            thread.start()
        barrier.wait()
        for thread in callers:
            thread.join(1)
            assert not thread.is_alive()
        assert entered.wait(1) and len(calls) == 1
    finally:
        release.set()
        finish()


def test_fork_reset_does_not_touch_inherited_thread_state(monkeypatch):
    class InheritedThread:
        def is_alive(self):
            raise AssertionError("inherited thread must not be touched")
    monkeypatch.setattr(runtime, "_diagnostic_pid", -1)
    monkeypatch.setattr(runtime, "_diagnostic_thread", InheritedThread())
    append = Mock()
    monkeypatch.setattr(diag, "append", append)
    schedule(); finish()
    assert append.call_count == 1


def test_unknown_error_and_sqlstate_cannot_add_arbitrary_strings(monkeypatch):
    append = Mock()
    monkeypatch.setattr(diag, "append", append)
    class SecretError(Exception):
        pass
    error = SecretError("SQL and credentials")
    error.sqlstate = "password"
    schedule(error); finish()
    data = append.call_args.kwargs
    assert data["reason"] == "other" and "sqlstate_code" not in data["counts"]
    assert all(type(value) is int for value in data["counts"].values())


@pytest.mark.asyncio
async def test_timeout_preserves_numeric_cause_frame_at_cancelled_await(monkeypatch):
    append = Mock()
    monkeypatch.setattr(diag, "append", append)
    values = {"__name__": "app.modules.acquisition.qualification_sla_runtime", "asyncio": asyncio}
    exec("async def _census():\n    await asyncio.Event().wait()", values)
    try:
        async with asyncio.timeout(.01):
            await values["_census"]()
    except TimeoutError as error:
        schedule(error)
    finish()
    data = append.call_args.kwargs
    assert data["reason"] == "TimeoutError"
    assert data["counts"]["frame_0_module_code"] == 201
    assert data["counts"]["frame_0_function_code"] == 203


@pytest.mark.asyncio
async def test_retry_exhaustion_diagnoses_once_after_writer_and_preserves_loss(monkeypatch):
    producer = runtime.Producer(lambda: None, role="celery")
    captured, calls = [], []
    monkeypatch.setattr(runtime, "_schedule_write_diagnostic", lambda error, **kw: captured.append(kw))
    async def failing(batch):
        calls.append(batch)
        producer._write_phase = 6
        if len(calls) <= 2:
            if len(calls) == 2:
                producer.stopping = True
            raise RuntimeError("not logged")
    monkeypatch.setattr(producer, "_write", failing)
    producer.offer("ready", {"account_id": 33})
    await producer.run()
    assert len(calls) == 3 and captured == [{"role": "celery", "stage": 33, "phase": 6, "batch_size": 1}]
    assert 33 in producer.lost and producer.queue.empty()


@pytest.mark.asyncio
async def test_successful_retry_never_creates_failure_diagnostic(monkeypatch):
    producer = runtime.Producer(lambda: None, role="celery")
    captured, calls = [], []
    monkeypatch.setattr(runtime, "_schedule_write_diagnostic", lambda error, **kw: captured.append(kw))
    async def retry(batch):
        calls.append(batch)
        if len(calls) == 1:
            raise RuntimeError("retry only")
        producer.stopping = True
    monkeypatch.setattr(producer, "_write", retry)
    producer.offer("ready", {"account_id": 33})
    await producer.run()
    assert len(calls) == 3 and captured == [] and producer.lost == {}


@pytest.mark.asyncio
async def test_census_failure_diagnoses_without_clearing_pending_or_inventing_business_loss(monkeypatch):
    monkeypatch.setenv("QUALIFICATION_SLA_CENSUS_ROLE", "growth")
    producer = runtime.Producer(lambda: None, role="growth", expected_source_counts={"growth": 1})
    captured, calls = [], []
    monkeypatch.setattr(runtime, "_schedule_write_diagnostic", lambda error, **kw: captured.append(kw))
    async def failing(batch):
        calls.append(batch)
        producer._write_phase = 5
        producer.census_pending, producer._census_queued = True, False
        if len(calls) <= 2:
            if len(calls) == 2:
                producer.stopping = True
            raise TimeoutError("not logged")
    monkeypatch.setattr(producer, "_write", failing)
    producer.offer("census", {})
    await producer.run()
    assert captured == [{"role": "growth", "stage": 32, "phase": 5, "batch_size": 1}]
    assert producer.census_pending and not producer._census_queued and producer.lost == {}


@pytest.mark.asyncio
async def test_cancellation_propagates_without_failure_diagnostic(monkeypatch):
    producer = runtime.Producer(lambda: None, role="celery")
    captured = []
    monkeypatch.setattr(runtime, "_schedule_write_diagnostic", lambda error, **kw: captured.append(kw))
    async def cancelled(batch):
        raise asyncio.CancelledError()
    monkeypatch.setattr(producer, "_write", cancelled)
    producer.offer("ready", {"account_id": 33})
    with pytest.raises(asyncio.CancelledError):
        await producer.run()
    assert captured == [] and 33 in producer.lost


@pytest.mark.asyncio
async def test_heartbeat_failure_diagnoses_and_keeps_existing_loss_behavior(monkeypatch):
    producer = runtime.Producer(lambda: None, role="celery")
    producer.accounts.add(33)
    captured, calls = [], []
    monkeypatch.setattr(runtime, "_schedule_write_diagnostic", lambda error, **kw: captured.append(kw))
    async def failing(batch):
        calls.append(batch)
        producer._write_phase = 7
        producer.stopping = True
        if len(calls) == 1:
            raise RuntimeError("not logged")
    monkeypatch.setattr(producer, "_write", failing)
    await producer.run()
    assert captured == [{"role": "celery", "stage": 31, "phase": 7, "batch_size": 0}]
    assert 33 in producer.lost


def test_legacy_reader_append_keeps_new_numeric_diagnostic_frame(tmp_path, monkeypatch):
    # R22 _rows validates event/reason only; unknown numeric count keys are kept.
    # Retain the exact deployed enum to model a not-yet-updated shared-ring reader.
    legacy_events = frozenset({"review_history_cache_gates", "review_evidence_handoff_failed",
        "review_evidence_maintenance_failed", "review_collection_error", "review_metrics_manager_health_failed"})
    assert diag.EVENTS == legacy_events
    store = diag.configure(tmp_path)
    schedule(); finish()
    before = store.snapshot()
    assert len(before) == 1
    assert before[0]["counts"]["phase_code"] == 5
    assert before[0]["counts"]["role_code"] == 2
    # A legacy append runs _rows(clear=True), which formerly erased the new event.
    monkeypatch.setattr(diag, "EVENTS", legacy_events)
    assert store.append("review_collection_error", counts={"count": 1})
    after = store.snapshot()
    assert len(after) == 2 and after[0] == before[0]
    assert after[0]["event"] == "review_metrics_manager_health_failed"
    assert store.path.stat().st_size == 2 * 1024 * 1024
