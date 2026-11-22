"""Real fixed-file metering: bounds, recovery, zero work and distinct RPC stages."""

import mmap
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account import read_metrics as metrics
from app.core.account import read_work

T0 = 1_799_712_000  # exact UTC hour


@pytest.fixture(autouse=True)
async def isolated_measurement_loop_state():
    """Independent stores cannot inherit another test's producer failure state.

    Production retains that state across calls to the same persistent source;
    this module uses a new temporary file (or a failing mock) for each test.
    The production-source harness may reuse one event loop for the whole suite.
    """
    import asyncio

    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_vanguard_read_metrics_state", None)
    loop._vanguard_read_metrics_state = None
    try:
        yield
    finally:
        await metrics.stop_metrics_health()
        loop._vanguard_read_metrics_state = previous


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics.time, "time", lambda: T0)
    value = metrics.FixedReadMetrics(tmp_path / "metrics")
    monkeypatch.setattr(metrics, "_store", lambda: value)
    return value


def test_fixed_allocation_temporal_coverage_and_real_zero(store, monkeypatch):
    store.add(34, "phase1", {0: 2, 1: 1}, at=T0)
    initial = store.snapshot(34, now=T0 + 1800, hours=1)
    assert initial["actual_reads"] is None and not initial["complete"]
    full = store.snapshot(34, now=T0 + 3600, hours=1)
    assert full["complete"] and full["actual_reads"] == 1
    zero = store.snapshot(34, now=T0 + 7200, hours=1)
    assert zero["complete"] and zero["actual_reads"] == 0
    assert (store.directory / metrics.FILENAME).stat().st_size == metrics.FILE_BYTES
    assert metrics.FILE_BYTES <= 16 * 1024 * 1024
    assert metrics.FILE_BYTES + 537_006_080 < 1024 * 1024 * 1024
    assert [p.name for p in store.directory.iterdir()] == [metrics.FILENAME]


def test_ring_expiration_and_bounded_accounts(store):
    for account_id in range(1, 20):
        store.add(account_id, "phase2", {1: 1}, at=T0)
    assert not store.snapshot(19, now=T0 + 3600, hours=1)["available"]
    summary = store.snapshot(now=T0 + 3600, hours=1)
    assert summary["actual_reads"] == 19 and summary["overflow_events"] == 11
    store.add(1, "phase2", {1: 2}, at=T0 + metrics.HOURS * 3600)
    assert store.snapshot(now=T0 + (metrics.HOURS + 1) * 3600)["actual_reads"] == 2
    assert (store.directory / metrics.FILENAME).stat().st_size == metrics.FILE_BYTES


def test_concurrent_writers_and_interrupted_update_are_not_silent_zero(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.add(34, "phase1", {1: 1}, at=T0), range(20)))
    assert store.snapshot(34, now=T0 + 3600, hours=1)["actual_reads"] == 20
    with open(store.directory / metrics.FILENAME, "r+b") as handle:
        with mmap.mmap(handle.fileno(), metrics.FILE_BYTES) as data:
            metrics._put(data, 24, T0 // 3600 + 1)
            data.flush()
    recovered = store.snapshot(34, now=T0 + 3600, hours=1)
    assert recovered["actual_reads"] is None
    assert recovered["known_entered_sdk"] == 20
    assert recovered["gap_hours"] == [T0]


def test_directory_cannot_accumulate_logs_or_copies(store):
    store.add(34, "phase1", {1: 1})
    (store.directory / "unexpected.log").write_text("x")
    with pytest.raises(ValueError, match="dedicated"):
        store.add(34, "phase1", {1: 1})


def test_dirty_recovery_marks_all_intermediate_hours_and_idle_is_not_account_overflow(store, monkeypatch):
    for account in range(1, metrics.ACCOUNT_SLOTS + 1):
        store.add(account, "phase1", {1: 1}, at=T0)
    store.add(0, "unknown", {}, at=T0)
    assert store.snapshot(now=T0 + 3600, hours=1)["overflow_events"] == 0
    with open(store.directory / metrics.FILENAME, "r+b") as handle:
        with mmap.mmap(handle.fileno(), metrics.FILE_BYTES) as data:
            metrics._put(data, 24, T0 // 3600 + 1)
            data.flush()
    monkeypatch.setattr(metrics.time, "time", lambda: T0 + 3 * 3600 + 1)
    recovered = store.snapshot(now=T0 + 3 * 3600 + 1, hours=3)
    assert recovered["actual_reads"] is None
    assert recovered["gap_hours"] == [T0, T0 + 3600, T0 + 7200]


@pytest.mark.asyncio
async def test_zero_read_work_and_mutually_exclusive_attribution(store, monkeypatch):
    monkeypatch.setattr("app.core.account.budget_ledger.finish_read_work", AsyncMock())
    async with read_work.operation(34, 40, "review") as work:
        metrics.set_work_plan("phase1", phase="continuation", complexity="simple")
        work.outcome = "trial"
        assert metrics.mark_avoided("same_work", ("GetMessages", 1))
        assert not metrics.mark_avoided("push", ("GetMessages", 1))
        assert metrics.mark_optimization("fast_path", "fallback", extra_reads=2)
    await metrics.stop_metrics_health()
    result = store.snapshot(34, now=T0 + 3600, hours=1)["by_stage"][1]
    review = result["work"][0]
    assert review["finished_slices"] == 1 and review["zero_read_slices"] == 1
    assert review["completed_slices"] == 1 and review["completed_decisions"] == 0 and review["outcomes"]["trial"] == 0
    assert review["phases"]["continuation"]["slices"] == 1
    assert result["optimizations"]["same_work"]["avoided_reads"] == 1
    assert result["optimizations"]["push"]["avoided_reads"] == 0
    assert result["optimizations"]["fast_path"]["fallback"] == 1 and result["extra_reads"] == 2


@pytest.mark.asyncio
async def test_attempted_entered_errors_and_committed_decisions_are_separate(store):
    methods = ["channels.GetMessagesRequest"]
    async with read_work.operation(34, 40, "review") as work:
        metrics.set_work_plan("phase2", complexity="complex")
        await metrics.record_rpc(34, methods * 3, "attempted", purpose="group_qualification")
        await metrics.record_rpc(34, methods * 2, "entered_sdk", purpose="group_qualification")
        await metrics.record_rpc(34, methods, "success", purpose="group_qualification")
        await metrics.record_rpc(34, methods, "error", purpose="group_qualification", flood=True)
        assert work.entered_reads == 2
    await metrics.record_committed_review(34, "trial", stage="phase2", elapsed_seconds=1.2)
    await metrics.stop_metrics_health()
    result = store.snapshot(34, now=T0 + 3600, hours=1)["by_stage"][2]
    assert result["attempted"] == 3 and result["entered_sdk"] == 2
    assert result["rpc"][0]["error"] == 1 and result["flood_errors"] == 1
    assert result["scheduler_commits"]["decisions"] == 1
    assert result["scheduler_commits"]["elapsed_ms"] == 1200


def test_planned_read_floor_uses_only_matching_phase():
    import json
    samples = [json.dumps({"reads": 40, "at": T0, "phase": "full", "outcome": "trial"})] * 30
    assert read_work.review_estimate(samples, 34, 40, "", phase="permissions_only", planned_reads=6) == 6
    samples += [json.dumps({"reads": 7, "at": T0, "phase": "permissions_only", "outcome": "trial"})] * 20
    # Real clock cannot be substituted globally outside the fixture.
    current = __import__("time").time()
    samples = [row.replace(str(T0), str(current)) for row in samples]
    assert read_work.review_estimate(samples, 34, 40, "", phase="permissions_only", planned_reads=6) == 9


@pytest.mark.asyncio
async def test_optional_metrics_failure_never_changes_business_result(monkeypatch):
    monkeypatch.setattr(metrics, "_store", lambda: Obj(add=lambda *a: (_ for _ in ()).throw(OSError())))
    await metrics.record_rpc(34, ["channels.GetMessagesRequest"], "attempted")
    await metrics.stop_metrics_health()


@pytest.mark.asyncio
async def test_wrapper_counts_pre_sdk_failure_separately_and_keeps_sdk_success(store, monkeypatch):
    from telethon.tl.functions.updates import GetStateRequest

    from app.core.account.rpc_governor import install_governor

    original = AsyncMock(return_value=Obj())
    client = Obj(_call=original)
    governor = Obj(account_id=34, purpose=lambda: "group_qualification", before=AsyncMock(),
                   failed=AsyncMock(), succeeded=AsyncMock())
    install_governor(client, governor)
    monkeypatch.setattr("app.core.account.send_receipts.record_request", AsyncMock(side_effect=RuntimeError("precondition")))
    with pytest.raises(RuntimeError, match="precondition"):
        await client._call(None, GetStateRequest())
    original.assert_not_awaited()
    await metrics.stop_metrics_health()
    first = store.snapshot(34, now=T0 + 3600, hours=1)
    assert first["attempted_reads"] == 1 and first["actual_reads"] == 0
    monkeypatch.setattr("app.core.account.send_receipts.record_request", AsyncMock(side_effect=[None, RuntimeError("receipt")]))
    with pytest.raises(RuntimeError, match="receipt"):
        await client._call(None, GetStateRequest())
    assert original.await_count == 1
    await metrics.stop_metrics_health()
    second = store.snapshot(34, now=T0 + 3600, hours=1)
    assert second["attempted_reads"] == 2 and second["actual_reads"] == 1
    assert sum(row["success"] for stage in second["by_stage"] for row in stage["rpc"]) == 1


@pytest.mark.asyncio
async def test_real_ledger_reservation_settlement_and_missing_evidence(test_db, monkeypatch):
    from app.core.account import budget_ledger
    from app.core.account.control_models import AccountBudgetReservation
    from app.core.config import settings

    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    cache = Obj(ttl=AsyncMock(return_value=120), eval=AsyncMock(return_value=1),
                get=AsyncMock(return_value="4"))
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=cache))

    @asynccontextmanager
    async def session():
        yield test_db
        await test_db.commit()

    monkeypatch.setattr("app.core.database.get_db_session", session)
    work = read_work.ReadWork(34, 40, "review", reads=4)
    await budget_ledger.reserve_read_work(test_db, cache, work, 12)
    assert (await test_db.get(AccountBudgetReservation, work.ledger_id)).state == "reserved"
    await budget_ledger.finish_read_work(work)
    await budget_ledger.finish_read_work(work)
    row = await test_db.get(AccountBudgetReservation, work.ledger_id, populate_existing=True)
    assert row.state == "settled" and row.consumed_reads == 4
    unknown = read_work.ReadWork(34, 41, "review", reads=3)
    await budget_ledger.reserve_read_work(test_db, cache, unknown, 12)
    cache.get.return_value = None
    await budget_ledger.finish_read_work(unknown)
    row = await test_db.get(AccountBudgetReservation, unknown.ledger_id, populate_existing=True)
    assert row.reason == "consumption_evidence_missing"


@pytest.mark.asyncio
async def test_explicit_renewal_plan_controls_reservation_without_fixed_sixteen(monkeypatch):
    from app.core.account import rpc_governor

    reserve = AsyncMock()
    monkeypatch.setattr("app.core.account.budget_ledger.reserve_read_work", reserve)
    cache = Obj(lrange=AsyncMock(return_value=[]), eval=AsyncMock(return_value=0), hget=AsyncMock(return_value="6"))
    work = read_work.ReadWork(34, 40, "renewal", planned_reads=6, phase="permissions_only")
    token = read_work._current.set(work)
    try:
        await read_work.ensure(Obj(), 34, rpc_governor.limits_for({}, __import__("datetime").datetime.utcnow()), cache)
    finally:
        read_work._current.reset(token)
    assert work.limit == 6 and work.claimed
    assert reserve.await_args.args[-1] == 6


def test_source_heartbeat_proves_idle_zero_and_crash_marks_unknown(store):
    store.add(34, "phase1", {}, at=T0, source_token=101)
    for second in range(90, 3601, 90):
        store.add(0, "unknown", {}, at=T0 + second, source_token=101)
    healthy = store.snapshot(34, now=T0 + 3601, hours=1)
    assert healthy["complete"] and healthy["actual_reads"] == 0
    crashed = store.snapshot(34, now=T0 + 7200, hours=1)
    assert crashed["actual_reads"] is None and crashed["gap_hours"]


@pytest.mark.asyncio
async def test_failure_before_dirty_marker_is_preserved_after_recovery(store, monkeypatch):
    clock = [T0]
    monkeypatch.setattr(metrics.time, "time", lambda: clock[0])
    state = metrics._state()
    for second in range(0, 3591, 90):
        store.add(34, "phase1", {}, at=T0 + second, source_token=state.token)
    clock[0] = T0 + 3599
    monkeypatch.setattr(metrics, "_store", lambda: Obj(add=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("before marker"))))
    await metrics.record_rpc(34, ["channels.GetMessagesRequest"], "attempted")
    assert state.pending_gap == T0 + 3599
    monkeypatch.setattr(metrics, "_store", lambda: store)
    clock[0] = T0 + 3601
    await metrics._persist(34, "phase1", {1: 1})
    await metrics.stop_metrics_health()
    result = store.snapshot(34, now=T0 + 3602, hours=1)
    assert result["actual_reads"] is None and result["gap_hours"] == [T0]


@pytest.mark.asyncio
async def test_slow_io_is_single_flight_and_waiting_business_is_bounded(store, monkeypatch):
    import asyncio

    release = threading.Event()
    started = threading.Event()
    active = [0, 0]
    original = store.add

    def slow(*args, **kwargs):
        active[0] += 1
        active[1] = max(active)
        started.set()
        release.wait(3)
        try:
            original(*args, **kwargs)
        finally:
            active[0] -= 1

    monkeypatch.setattr(store, "add", slow)
    first = asyncio.create_task(metrics._persist(34, "phase1", {1: 1}, start_health=False))
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    await metrics._persist(34, "phase1", {1: 1}, start_health=False)
    assert active[1] == 1 and metrics._state().pending_gap is not None
    release.set()
    await first
    monkeypatch.setattr(store, "add", original)
    await metrics.stop_metrics_health()
    result = store.snapshot(34, now=T0 + 3600, hours=1)
    assert result["actual_reads"] is None


def test_missing_role_and_late_role_start_never_imply_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics.time, "time", lambda: T0)
    roles = {name: metrics.FixedReadMetrics(tmp_path / "roles", role=name, expected_roles=metrics.ROLES)
             for name in metrics.ROLES}
    roles["backend"].add(34, "phase1", {}, at=T0, source_token=1)
    result = roles["backend"].snapshot(34, now=T0 + 3600, hours=1)
    assert result["actual_reads"] is None and {"growth", "celery"} <= set(result["missing_roles"])
    for second in range(3600, 7201, 90):
        for token, value in enumerate(roles.values(), start=1):
            value.add(34, "phase1", {}, at=T0 + second, source_token=token)
    late = roles["backend"].snapshot(34, now=T0 + 3601, hours=1)
    assert late["actual_reads"] is None
    result = roles["backend"].snapshot(34, now=T0 + 7201, hours=1)
    assert result["actual_reads"] is None  # Registration after an expired source marks that hour unknown.
    for second in range(7290, 10801, 90):
        for token, value in enumerate(roles.values(), start=1):
            value.add(34, "phase1", {}, at=T0 + second, source_token=token)
    result = roles["backend"].snapshot(34, now=T0 + 10801, hours=1)
    assert result["actual_reads"] == 0 and result["complete"]


@pytest.mark.asyncio
async def test_cancelling_health_during_lock_acquisition_keeps_the_permit(store):
    import asyncio

    state = metrics._state()
    await state.semaphore.acquire()
    metrics.start_metrics_health()
    await asyncio.sleep(0)
    state.semaphore.release()
    await asyncio.sleep(0)
    await metrics.stop_metrics_health()
    assert state.semaphore._value == 1
    assert state.inflight is None and state.pending_gap is None
    store.add(34, "phase1", {1: 1}, at=T0)
    assert store.snapshot(34, now=T0 + 3600, hours=1)["actual_reads"] == 1
