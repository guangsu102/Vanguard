"""Isolated attestation checks: no application services or network."""
import asyncio
from contextlib import contextmanager
import subprocess
import hashlib
import importlib.util
import math
import os
import struct
import sys
import types
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[3] / "app/modules/acquisition/review_diagnostics.py"


@pytest.fixture
def diag(monkeypatch):
    spec = importlib.util.spec_from_file_location("isolated_heartbeat", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "install_logging_filters", lambda: None)
    return module


def set_role(monkeypatch, role):
    fake = types.ModuleType("app.core.config")
    fake.settings = types.SimpleNamespace(REVIEW_READ_METRICS_ROLE=role)
    monkeypatch.setitem(sys.modules, "app.core.config", fake)


def state(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, path.stat().st_size


async def start_stop(diag):
    await diag.start_maintenance()
    await diag.stop_maintenance()


def test_missing_status_creates_nothing(diag, tmp_path):
    directory = tmp_path / "absent" / "diagnostics"
    result = diag.read_maintenance_status(directory)
    assert result == {"available": False, "reason": "diagnostic_file_missing",
                      "roles": {"backend": 0.0, "growth": 0.0, "celery": 0.0}}
    assert not directory.parent.exists()


def test_ordinary_operations_do_not_attest(diag, tmp_path):
    store = diag.configure(tmp_path)
    assert store.append("review_collection_error", counts={"count": 1})
    store.snapshot()
    assert store.sweep()
    before = state(store.path)
    assert diag.read_maintenance_status(tmp_path)["roles"] == dict.fromkeys(diag.MAINTENANCE_OFFSETS, 0.0)
    assert state(store.path) == before
    assert before[2] == 2 * 1024 * 1024


def test_status_uses_rdonly_without_mutating_operations(diag, tmp_path, monkeypatch):
    store = diag.configure(tmp_path)
    calls, original_open = [], os.open

    def observe(path, flags, *args, **kwargs):
        calls.append(flags)
        return original_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("status attempted mutation")

    monkeypatch.setattr(os, "open", observe)
    for name in ("_open", "_rows", "_write"):
        monkeypatch.setattr(diag.DiagnosticStore, name, forbidden)
    monkeypatch.setattr(os, "fsync", forbidden)
    before = state(store.path)
    assert diag.read_maintenance_status(tmp_path)["available"]
    assert calls and all((flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)) == 0 for flags in calls)
    assert state(store.path) == before


@pytest.mark.parametrize("role", ["backend", "growth", "celery"])
def test_start_attests_own_role_only_and_duplicate_start_does_not(diag, tmp_path, monkeypatch, role):
    set_role(monkeypatch, role)
    clock = [1800000000.0]
    monkeypatch.setattr(diag.time, "time", lambda: clock[0])
    store = diag.configure(tmp_path)
    assert store.append("review_collection_error", counts={"count": 1})
    before = store.path.read_bytes()

    async def exercise():
        await diag.start_maintenance()
        try:
            assert diag.read_maintenance_status(tmp_path)["roles"] == {
                item: clock[0] if item == role else 0.0 for item in diag.MAINTENANCE_OFFSETS}
            task = diag._task
            assert task is not None and not task.done()
            after, offset = store.path.read_bytes(), diag.MAINTENANCE_OFFSETS[role]
            assert after[:offset] == before[:offset]
            assert after[offset + 8:] == before[offset + 8:]
            clock[0] += 1
            await diag.start_maintenance()
            assert diag._task is task
            assert diag.read_maintenance_status(tmp_path)["roles"][role] == clock[0] - 1
        finally:
            await diag.stop_maintenance()
        assert diag._task is None

    asyncio.run(exercise())


def test_three_roles_survive_append_snapshot_and_configure(diag, tmp_path, monkeypatch):
    store = diag.configure(tmp_path)
    for index, role in enumerate(diag.MAINTENANCE_OFFSETS):
        set_role(monkeypatch, role)
        monkeypatch.setattr(diag.time, "time", lambda index=index: 1800000000.0 + index)
        asyncio.run(start_stop(diag))
    expected = {role: 1800000000.0 + index for index, role in enumerate(diag.MAINTENANCE_OFFSETS)}
    assert diag.read_maintenance_status(tmp_path)["roles"] == expected
    store.append("review_collection_error", counts={"count": 2})
    store.snapshot()
    diag.sweep()
    diag.configure(tmp_path)
    assert diag.read_maintenance_status(tmp_path)["roles"] == expected


def test_periodic_loop_uses_240_seconds_and_sweeps_before_attesting(diag, tmp_path, monkeypatch):
    set_role(monkeypatch, "growth")
    store = diag.configure(tmp_path)
    clock, waits = [1800000000.0], []
    monkeypatch.setattr(diag.time, "time", lambda: clock[0])

    async def exercise():
        gate, second_wait = asyncio.Event(), asyncio.Event()

        async def fake_sleep(delay):
            waits.append(delay)
            if len(waits) == 1:
                await gate.wait()
            else:
                second_wait.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(diag.asyncio, "sleep", fake_sleep)
        await diag.start_maintenance()
        try:
            assert diag.read_maintenance_status(tmp_path)["roles"]["growth"] == clock[0]
            clock[0] += 240
            gate.set()
            await asyncio.wait_for(second_wait.wait(), 5)
            assert diag.read_maintenance_status(tmp_path)["roles"]["growth"] == clock[0]
            assert waits == [240, 240]
            assert store.path.stat().st_size == diag.FILE_BYTES
        finally:
            await diag.stop_maintenance()

    asyncio.run(exercise())


def test_failed_sweep_keeps_previous_heartbeat(diag, tmp_path, monkeypatch):
    set_role(monkeypatch, "backend")
    store = diag.configure(tmp_path)
    monkeypatch.setattr(diag.time, "time", lambda: 1800000000.0)
    asyncio.run(start_stop(diag))
    before = state(store.path)

    def failed_rows(*args, **kwargs):
        raise OSError("injected sweep failure")

    monkeypatch.setattr(store, "_rows", failed_rows)
    monkeypatch.setattr(diag.time, "time", lambda: 1800000240.0)
    asyncio.run(start_stop(diag))
    assert diag.read_maintenance_status(tmp_path)["roles"]["backend"] == 1800000000.0
    assert state(store.path) == before


def test_failed_stamp_sync_restores_previous(diag, tmp_path, monkeypatch):
    store = diag.configure(tmp_path)
    monkeypatch.setattr(diag.time, "time", lambda: 1800000000.0)
    assert store._maintenance_sweep("celery")
    original_fsync, calls = os.fsync, [0]

    def fail_stamp_sync(fd):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("injected stamp fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_stamp_sync)
    monkeypatch.setattr(diag.time, "time", lambda: 1800000240.0)
    assert not store._maintenance_sweep("celery")
    assert diag.read_maintenance_status(tmp_path)["roles"]["celery"] == 1800000000.0


@pytest.mark.parametrize("role", ["", "unknown"])
def test_unknown_role_cleans_but_never_claims_identity(diag, tmp_path, monkeypatch, role):
    set_role(monkeypatch, role)
    store = diag.configure(tmp_path)
    assert store.append("review_collection_error", now=1)
    asyncio.run(start_stop(diag))
    assert store.snapshot() == []
    assert diag.read_maintenance_status(tmp_path)["roles"] == dict.fromkeys(diag.MAINTENANCE_OFFSETS, 0.0)


def test_startup_erases_expired_body_and_preserves_fresh_body(diag, tmp_path, monkeypatch):
    set_role(monkeypatch, "backend")
    store, instant = diag.configure(tmp_path), 1800000000.0
    assert store.append("review_collection_error", counts={"count": 1}, now=instant - diag.RETENTION_SECONDS + 1)
    assert store.append("review_collection_error", counts={"count": 2}, now=instant)
    monkeypatch.setattr(diag.time, "time", lambda: instant + 2)
    asyncio.run(start_stop(diag))
    blob = store.path.read_bytes()
    assert blob[diag.HEADER_BYTES:diag.HEADER_BYTES + diag.SLOT_BYTES] == bytes(diag.SLOT_BYTES)
    assert store.snapshot()[0]["counts"] == {"count": 2}
    assert struct.unpack("<Q", blob[8:16])[0] == 2
    assert len(blob) == diag.FILE_BYTES


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0, 253402300800.0])
def test_invalid_heartbeat_is_unavailable_without_repair(diag, tmp_path, value):
    store = diag.configure(tmp_path)
    with store.path.open("r+b") as handle:
        handle.seek(32)
        handle.write(struct.pack("<d", value))
    before = state(store.path)
    assert not diag.read_maintenance_status(tmp_path)["available"]
    assert state(store.path) == before


@pytest.mark.parametrize("fault", ["header", "size", "busy"])
def test_damaged_or_busy_file_is_unavailable_without_repair(diag, tmp_path, fault):
    store, locked_fd = diag.configure(tmp_path), None
    if fault == "header":
        with store.path.open("r+b") as handle:
            handle.write(b"BROKEN01")
    elif fault == "size":
        with store.path.open("r+b") as handle:
            handle.truncate(128)
    before = state(store.path)
    if fault == "busy":
        locked_fd = store._open()
    try:
        assert not diag.read_maintenance_status(tmp_path)["available"]
    finally:
        if locked_fd is not None:
            os.close(locked_fd)
    assert state(store.path) == before


def test_readonly_does_not_clear_expired_body_or_other_files(diag, tmp_path):
    store = diag.configure(tmp_path)
    assert store.append("review_collection_error", counts={"count": 3}, now=1)
    other = tmp_path / "unrelated.keep"
    other.write_text("do not sweep")
    before = state(store.path), state(other)
    assert diag.read_maintenance_status(tmp_path)["available"]
    assert (state(store.path), state(other)) == before
    assert any(store.path.read_bytes()[diag.HEADER_BYTES:])


# Cross-process lock contention regressions; preserve successful-only attestation.
@contextmanager
def held_by_another_process(path):
    program = r'''
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('locked', flush=True)
sys.stdin.readline()
os.close(fd)
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", program, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "locked"

    def release():
        if child.poll() is None:
            child.stdin.write("release\n")
            child.stdin.flush()
        assert child.wait(timeout=5) == 0

    try:
        yield release
    finally:
        release()


def test_startup_lock_failure_retries_in_5_then_returns_to_240(diag, tmp_path, monkeypatch):
    set_role(monkeypatch, "growth")
    store = diag.configure(tmp_path)
    clock, waits = [1800000000.0], []
    monkeypatch.setattr(diag.time, "time", lambda: clock[0])
    assert store._maintenance_sweep("growth")
    before = state(store.path)
    clock[0] += 240

    async def exercise(release):
        first, gate, second = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sleep(delay):
            waits.append(delay)
            if len(waits) == 1:
                first.set()
                await gate.wait()
            else:
                second.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(diag.asyncio, "sleep", sleep)
        await diag.start_maintenance()
        try:
            await asyncio.wait_for(first.wait(), 5)
            assert waits == [5]
            task = diag._task
            await diag.start_maintenance()
            assert diag._task is task
            release()
            assert state(store.path) == before
            clock[0] += 5
            gate.set()
            await asyncio.wait_for(second.wait(), 5)
            assert waits == [5, 240]
            assert diag.read_maintenance_status(tmp_path)["roles"] == {
                "backend": 0.0, "growth": clock[0], "celery": 0.0}
        finally:
            await diag.stop_maintenance()
        assert diag._task is None

    with held_by_another_process(store.path) as release:
        asyncio.run(exercise(release))


def test_periodic_lock_failure_does_not_attest_and_retries_in_5(diag, tmp_path, monkeypatch):
    set_role(monkeypatch, "backend")
    store = diag.configure(tmp_path)
    clock, waits = [1800000000.0], []
    monkeypatch.setattr(diag.time, "time", lambda: clock[0])

    async def exercise():
        events = [asyncio.Event() for _ in range(3)]
        gates = [asyncio.Event() for _ in range(3)]

        async def sleep(delay):
            index = len(waits)
            waits.append(delay)
            events[index].set()
            await gates[index].wait()

        monkeypatch.setattr(diag.asyncio, "sleep", sleep)
        await diag.start_maintenance()
        try:
            await asyncio.wait_for(events[0].wait(), 5)
            assert waits == [240]
            before = state(store.path)
            with held_by_another_process(store.path) as release:
                clock[0] += 240
                gates[0].set()
                await asyncio.wait_for(events[1].wait(), 5)
                assert waits == [240, 5]
                release()
                assert state(store.path) == before
                clock[0] += 5
                gates[1].set()
                await asyncio.wait_for(events[2].wait(), 5)
            assert waits == [240, 5, 240]
            assert diag.read_maintenance_status(tmp_path)["roles"] == {
                "backend": clock[0], "growth": 0.0, "celery": 0.0}
        finally:
            await diag.stop_maintenance()

    asyncio.run(exercise())


def test_stop_during_failed_retry_cancels_without_attesting(diag, tmp_path, monkeypatch):
    set_role(monkeypatch, "celery")
    store = diag.configure(tmp_path)
    before = state(store.path)

    async def exercise():
        sleeping = asyncio.Event()
        waits = []

        async def sleep(delay):
            waits.append(delay)
            sleeping.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(diag.asyncio, "sleep", sleep)
        await diag.start_maintenance()
        try:
            await asyncio.wait_for(sleeping.wait(), 5)
            assert waits == [5]
        finally:
            await diag.stop_maintenance()
        assert diag._task is None

    with held_by_another_process(store.path):
        asyncio.run(exercise())
    assert state(store.path) == before

