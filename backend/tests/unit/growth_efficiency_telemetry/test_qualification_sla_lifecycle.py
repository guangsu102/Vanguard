"""Idle prefork inventory and loop-local reuse of application resources."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

HERE = Path(__file__).resolve().parents[3] / "app" / "modules" / "acquisition"
spec = importlib.util.spec_from_file_location("qualification_sla_lifecycle_draft", HERE / "qualification_sla_lifecycle.py")
life = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = life
spec.loader.exec_module(life)
NOW = datetime(2026, 11, 6, 10, 0)


class DeferredThread:
    """Hold the real target for deterministic idle heartbeat execution."""
    def __init__(self, *, target, name, daemon):
        self.target, self.name, self.daemon = target, name, daemon
        self.started = False
    def start(self):
        self.started = True
    def join(self, timeout):
        assert timeout <= .5
    def is_alive(self):
        return False


def idle_status(**changes):
    return {"pending_count": 0, "loop_active": False, "lost_since": None,
        "confirmed_through": NOW.isoformat(), "status_valid": True, **changes}


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv("QUALIFICATION_SLA_ENABLED", "true")
    monkeypatch.setenv("QUALIFICATION_SLA_EXPECTED_SOURCES", '{"backend":1,"celery:main":2}')
    monkeypatch.delenv("QUALIFICATION_SLA_SOURCE_ROLE", raising=False)
    monkeypatch.delenv("QUALIFICATION_SLA_ROLE", raising=False)
    monkeypatch.setattr(life, "_process", None)
    monkeypatch.setattr(life, "_loop", None)
    monkeypatch.setattr(life, "_lifecycle_failure_since", None)
    monkeypatch.setattr(life, "_lifecycle_failure_pid", None)
    monkeypatch.setattr(life, "_now", lambda: NOW)
    monkeypatch.setattr(life.threading, "Thread", DeferredThread)
    runtime = SimpleNamespace(start_producer=Mock(), stop_producer=AsyncMock(),
        request_census=Mock(), process_source_status=Mock(side_effect=idle_status))
    monkeypatch.setattr(life, "_runtime_module", lambda: runtime)
    monkeypatch.setattr(life, "_database_factory", lambda: object())
    return runtime


@pytest.mark.asyncio
async def test_default_disabled_does_not_construct_a_client_or_touch_application_resources(environment, monkeypatch):
    monkeypatch.delenv("QUALIFICATION_SLA_ENABLED", raising=False)
    forbidden = Mock(side_effect=AssertionError("disabled measurement touched resources"))
    monkeypatch.setattr(life, "_new_client", forbidden)
    monkeypatch.setattr(life, "_database_factory", forbidden)
    assert life.declare_process("backend") is None
    assert await life.start("backend") is False
    await life.stop(close_process=True)
    assert life.source_identity() is None
    forbidden.assert_not_called()


@pytest.mark.parametrize("raw, expected", [
    ('{"backend":1,"celery:main":2}', {"backend": 1, "celery:main": 2}),
    ('', None), ('{}', None), ('[]', None), ('{"celery":true}', None),
    ('{"celery":0}', None), ('{"CELERY":1}', None), ('{"backend":513}', None),
    ('{"backend":300,"celery:main":300}', None),
])
def test_expected_sources_are_an_explicit_validated_manifest(environment, monkeypatch, raw, expected):
    monkeypatch.setenv("QUALIFICATION_SLA_EXPECTED_SOURCES", raw)
    assert life.get_expected_sources() == expected


def test_idle_child_declares_once_and_fork_gets_new_identity_without_parent_resource_reuse(environment, monkeypatch):
    pid = [100]
    monkeypatch.setattr(life.os, "getpid", lambda: pid[0])
    monkeypatch.setenv("QUALIFICATION_SLA_SOURCE_ROLE", "celery:main")
    original = life.declare_process()
    original_source = life._process
    assert original_source.thread.started and original_source.thread.daemon
    assert original_source.thread.name == "qualification-sla-source"
    assert life.declare_process() == original
    pid[0] = 101
    replacement = life.declare_process()
    assert replacement["generation"] != original["generation"]
    assert replacement["role"] == "celery:main" and replacement["pid"] == 101
    assert not original_source.halt.is_set()
    assert environment.start_producer.call_count == 0  # Idle child needs no DB loop.


def test_idle_heartbeat_executes_and_closes_its_own_client_without_a_database(environment, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("idle source used database"))
    monkeypatch.setattr(life, "_database_factory", forbidden)
    identity = life.declare_process("celery:main")
    source = life._process
    rows = []
    def publish(*args):
        rows.append(json.loads(args[5]))
        source.halt.set()
        return 1
    client = SimpleNamespace(eval=Mock(side_effect=publish), close=Mock())
    monkeypatch.setattr(life, "_new_client", Mock(return_value=client))
    source.thread.target()
    assert len(rows) == 2
    assert rows[0]["generation"] == rows[1]["generation"] == identity["generation"]
    assert not rows[0]["clean_closed"] and rows[1]["clean_closed"]
    assert rows[0]["confirmed_through"] == NOW.isoformat()
    client.close.assert_called_once()
    forbidden.assert_not_called()


@pytest.mark.parametrize("status, clean", [
    (idle_status(), True), (idle_status(pending_count=1), False),
    (idle_status(loop_active=True), False), (idle_status(lost_since=(NOW-timedelta(seconds=1)).isoformat()), False),
])
def test_clean_process_close_cannot_hide_pending_active_or_lost_measurement(environment, status, clean):
    life.declare_process("backend", status_provider=lambda: status)
    assert life._process.snapshot(closing=True)["clean_closed"] is clean


def test_two_asyncio_run_tasks_reuse_process_identity_but_not_loop_producer(environment, monkeypatch):
    loops, identities = [], []
    def started(*args, **kwargs):
        loops.append(asyncio.get_running_loop())
        identities.append(kwargs["source_identity"])
        assert args[0] is life.existing_sessions
        assert kwargs["source_provider"] is life.source_provider
    environment.start_producer.side_effect = started
    async def once():
        assert await life.start("celery:main")
        assert await life.start("celery:main")
        await life.stop()
        assert life._loop is None
    asyncio.run(once())
    asyncio.run(once())
    assert loops[0] is not loops[1] and all(loop.is_closed() for loop in loops)
    assert identities[0] == identities[1]
    assert environment.start_producer.call_count == 2
    assert environment.stop_producer.await_count == 2
    assert life.source_identity() == identities[0]  # Still an idle child.


@pytest.mark.asyncio
async def test_existing_sessions_borrows_only_the_current_application_factory(environment, monkeypatch):
    sentinel, calls = object(), []
    @asynccontextmanager
    async def existing():
        calls.append("session_open")
        yield sentinel
        calls.append("session_close")
    monkeypatch.setattr(life, "_database_factory", lambda: existing)
    async with life.existing_sessions() as db:
        assert db is sentinel
    assert calls == ["session_open", "session_close"]


@pytest.mark.asyncio
async def test_initialization_may_follow_start_without_measurement_initializing_a_pool(environment, monkeypatch):
    factory = [None]
    monkeypatch.setattr(life, "_database_factory", lambda: factory[0])
    assert await life.start("celery:main")
    environment.start_producer.assert_not_called()
    factory[0] = object()
    await asyncio.sleep(.06)
    environment.start_producer.assert_called_once()
    environment.request_census.assert_called_once()
    await life.stop()
    assert not any(task.get_name() == "qualification-sla-bootstrap" for task in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_stop_cancels_delayed_initialization_before_loop_closes(environment, monkeypatch):
    monkeypatch.setattr(life, "_database_factory", lambda: None)
    assert await life.start("celery:main")
    bootstrap = life._loop.bootstrap
    await life.stop()
    assert bootstrap.done() and life._loop is None
    environment.start_producer.assert_not_called()


@pytest.mark.asyncio
async def test_database_fallback_only_starts_a_predeclared_process(environment):
    assert await life.start_current_process() is False
    environment.start_producer.assert_not_called()
    life.declare_process("backend")
    assert await life.start_current_process()
    assert environment.start_producer.call_args.kwargs["role"] == "backend"
    await life.stop()


@pytest.mark.asyncio
async def test_redis_then_database_cleanup_drains_once_while_both_resources_still_exist(environment):
    resources = {"redis": True, "database": True}
    events = []
    async def drain():
        assert resources == {"redis": True, "database": True}
        events.append("measurement_drained")
    environment.stop_producer.side_effect = drain
    assert await life.start("celery:main")
    await life.stop()  # First line of close_redis.
    resources["redis"] = False
    events.append("redis_closed")
    await life.stop()  # First line of close_db, already drained.
    resources["database"] = False
    events.append("database_closed")
    assert events == ["measurement_drained", "redis_closed", "database_closed"]
    assert environment.stop_producer.await_count == 1
    assert life.source_identity() is not None  # Prefork process remains idle.


@pytest.mark.asyncio
async def test_source_snapshot_never_initializes_global_redis_when_it_is_missing(environment, monkeypatch):
    monkeypatch.setattr(life, "_current_redis", AsyncMock(side_effect=RuntimeError("not initialized")))
    forbidden = Mock(side_effect=AssertionError("snapshot created a resource"))
    monkeypatch.setattr(life, "_new_client", forbidden)
    monkeypatch.setattr(life, "_database_factory", forbidden)
    result = await life.source_provider()
    assert not result["complete"] and result["reason"] == "source_registry_unavailable"
    forbidden.assert_not_called()


def test_missing_old_loop_cleanup_is_detected_instead_of_reusing_a_closed_loop(environment):
    asyncio.run(life.start("celery:main"))
    original = life._loop
    assert original.loop.is_closed()
    assert asyncio.run(life.start("celery:main")) is False
    assert environment.start_producer.call_count == 1
    assert life._process.fatal_since is not None


def source_row(generation="a", **changes):
    return {"role": "backend", "pid": 100, "generation": generation,
        "started_at": (NOW-timedelta(hours=1)).isoformat(), "last_seen_at": NOW.isoformat(),
        "status_valid": True, "pending_count": 0, "loop_active": False,
        "lost_since": None, "confirmed_through": (NOW-timedelta(seconds=20)).isoformat(),
        "clean_closed": False, **changes}


@pytest.mark.asyncio
async def test_registry_snapshot_is_bounded_and_returns_the_minimum_confirmed_active_watermark(environment, monkeypatch):
    rows = [source_row(), source_row("b", role="celery:main", confirmed_through=(NOW-timedelta(seconds=30)).isoformat()),
        source_row("retired", clean_closed=True, confirmed_through=(NOW-timedelta(hours=1)).isoformat())]
    cache = SimpleNamespace(eval=AsyncMock(return_value=[1, [json.dumps(row) for row in rows]]))
    monkeypatch.setattr(life, "_current_redis", AsyncMock(return_value=cache))
    result = await life.source_provider()
    assert result["complete"] and len(result["sources"]) == 3
    assert result["min_confirmed_through"] == (NOW-timedelta(seconds=30)).isoformat()
    assert cache.eval.call_args.args[-1] == life.MAX_SOURCES


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"last_seen_at": (NOW+timedelta(microseconds=1)).isoformat()},
    {"confirmed_through": (NOW+timedelta(microseconds=1)).isoformat()},
    {"status_valid": False}, {"pending_count": True}, {"confirmed_through": None},
])
async def test_registry_snapshot_rejects_future_heartbeat_or_incomplete_source_proof(environment, monkeypatch, changes):
    cache = SimpleNamespace(eval=AsyncMock(return_value=[1, [json.dumps(source_row(**changes))]]))
    monkeypatch.setattr(life, "_current_redis", AsyncMock(return_value=cache))
    result = await life.source_provider()
    assert not result["complete"]
    assert result.get("min_confirmed_through") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [[0, "source_registry_capacity_exceeded"], [1, ["bad-json"]],
    [1, [json.dumps(source_row())]*513]])
async def test_registry_errors_or_capacity_cannot_make_a_complete_inventory(environment, monkeypatch, result):
    cache = SimpleNamespace(eval=AsyncMock(return_value=result))
    monkeypatch.setattr(life, "_current_redis", AsyncMock(return_value=cache))
    assert not (await life.source_provider())["complete"]


def test_transport_recovery_publishes_its_bounded_gap_before_clearing_open_failure(environment):
    life.declare_process("backend")
    source = life._process
    original = NOW-timedelta(seconds=5)
    source.failure_since = original
    failing = SimpleNamespace(eval=Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        source.pulse(failing)
    assert source.failure_since == original and source.gaps == []
    client = SimpleNamespace(eval=Mock(return_value=1))
    assert source.pulse(client)
    sent = json.loads(client.eval.call_args.args[5])
    assert sent["gaps"] == [{"start": original.isoformat(), "end": NOW.isoformat(),
        "reason": "source_heartbeat_transport_failed"}]
    assert sent["lost_since"] is None and source.failure_since is None
    assert source.gaps == sent["gaps"]
    assert source.pulse(client)
    assert json.loads(client.eval.call_args.args[5])["gaps"] == sent["gaps"]


def test_transport_gap_capacity_loss_is_sticky_instead_of_silently_dropping_history(environment):
    life.declare_process("backend")
    source = life._process
    source.gaps = [{"start": (NOW-timedelta(seconds=10)).isoformat(),
        "end": (NOW-timedelta(seconds=9)).isoformat(), "reason": "source_heartbeat_transport_failed"}]*life.MAX_SOURCE_GAPS
    source.failure_since = NOW-timedelta(seconds=1)
    client = SimpleNamespace(eval=Mock(return_value=1))
    assert source.pulse(client)
    value = json.loads(client.eval.call_args.args[5])
    assert len(value["gaps"]) == life.MAX_SOURCE_GAPS
    assert value["history_loss"] and not value["status_valid"]
    assert source.history_loss and source.snapshot()["history_loss"]


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [True, False])
async def test_historical_invalid_status_stays_evidence_without_blocking_current_inventory(environment, monkeypatch, closed):
    old = source_row("old", clean_closed=closed, status_valid=False, confirmed_through=None,
        last_seen_at=(NOW-timedelta(seconds=61)).isoformat(), pending_count=True)
    cache = SimpleNamespace(eval=AsyncMock(return_value=[1, [json.dumps(source_row()), json.dumps(old)]]))
    monkeypatch.setattr(life, "_current_redis", AsyncMock(return_value=cache))
    result = await life.source_provider()
    assert result["complete"] and len(result["sources"]) == 2
    assert result["sources"][1] == old
    assert result["min_confirmed_through"] == (NOW-timedelta(seconds=20)).isoformat()


def test_real_celery_signal_registration_is_idempotent_and_registers_idle_children(environment, monkeypatch):
    # Use real Signal instances without invoking the application receivers that
    # may already be registered during collection by other integration tests.
    import celery.signals as signals
    monkeypatch.setattr(signals, "worker_process_init", type(signals.worker_process_init)())
    monkeypatch.setattr(signals, "worker_process_shutdown", type(signals.worker_process_shutdown)())
    from celery.signals import worker_process_init, worker_process_shutdown
    life.install_celery_signals()
    life.install_celery_signals()
    try:
        assert sum(key[0] == "qualification-sla-source-init-v1" for key, receiver in worker_process_init.receivers) == 1
        assert sum(key[0] == "qualification-sla-source-shutdown-v1" for key, receiver in worker_process_shutdown.receivers) == 1
        monkeypatch.setenv("QUALIFICATION_SLA_SOURCE_ROLE", "celery:main")
        worker_process_init.send(sender=object())
        assert life.source_identity()["role"] == "celery:main"
        environment.start_producer.assert_not_called()
        worker_process_shutdown.send(sender=object())
        assert life.source_identity() is None
    finally:
        worker_process_init.disconnect(dispatch_uid="qualification-sla-source-init-v1")
        worker_process_shutdown.disconnect(dispatch_uid="qualification-sla-source-shutdown-v1")


@pytest.mark.asyncio
async def test_thread_start_failure_never_escapes_business_startup_or_allows_a_false_restart(environment, monkeypatch):
    class BrokenThread(DeferredThread):
        def start(self):
            raise RuntimeError("cannot start measurement heartbeat")
    monkeypatch.setattr(life.threading, "Thread", BrokenThread)
    assert life.declare_process("backend") is None
    assert life._lifecycle_failure_since is not None
    assert await life.start("backend") is False
    environment.start_producer.assert_not_called()


def test_thread_join_failure_never_escapes_business_shutdown(environment):
    life.declare_process("backend")
    life._process.thread.join = Mock(side_effect=RuntimeError("cannot join heartbeat"))
    assert life.process_shutdown() is None
    assert life._lifecycle_failure_since is not None
    assert life._process.fatal_since is not None


def test_signal_install_failure_does_not_break_celery_configuration(environment, monkeypatch):
    from celery.signals import worker_process_init
    monkeypatch.setattr(worker_process_init, "connect", Mock(side_effect=RuntimeError("signal failed")))
    assert life.install_celery_signals() is None
    assert life._lifecycle_failure_since is not None


@pytest.mark.asyncio
async def test_lifecycle_failure_is_visible_even_if_other_registry_sources_look_healthy(environment, monkeypatch):
    life._mark_fatal()
    cache = SimpleNamespace(eval=AsyncMock(return_value=[1, [json.dumps(source_row())]]))
    monkeypatch.setattr(life, "_current_redis", AsyncMock(return_value=cache))
    result = await life.source_provider()
    assert not result["complete"] and result["reason"] == "local_source_lifecycle_failure"
    assert len(result["sources"]) == 1


def registry_item(generation, **changes):
    last = NOW-timedelta(seconds=61)
    return source_row(generation, clean_closed=True, closed_at=last.isoformat(),
        last_seen_at=last.isoformat(), confirmed_through=last.isoformat(),
        last_seen_epoch=last.replace(tzinfo=timezone.utc).timestamp(), gaps=[], history_loss=False, **changes)


def test_real_redis_lua_retires_clean_closed_sources_after_sixty_seconds(environment, monkeypatch):
    from fakeredis import FakeRedis
    monkeypatch.setattr(life, "MAX_SOURCES", 2)
    life.declare_process("backend")
    cache = FakeRedis(decode_responses=True)
    cache.hset(life.REGISTRY_KEY, mapping={"retired": json.dumps(registry_item("retired")),
        "active": json.dumps(source_row("active", last_seen_epoch=NOW.replace(tzinfo=timezone.utc).timestamp()))})
    assert life._process.pulse(cache)
    assert cache.hlen(life.REGISTRY_KEY) == 2
    assert not cache.hexists(life.REGISTRY_KEY, "retired")
    assert cache.hexists(life.REGISTRY_KEY, "active")
    assert cache.get(life.REGISTRY_ERROR_KEY) is None


@pytest.mark.parametrize("changes", [{"gaps": [{"start": "x", "end": "y"}]},
    {"pending_count": 1}, {"lost_since": NOW.isoformat()}, {"history_loss": True},
    {"clean_closed": False}, {"status_valid": False}])
def test_real_redis_lua_never_retires_unconfirmed_or_abnormal_sources_early(environment, monkeypatch, changes):
    from fakeredis import FakeRedis
    monkeypatch.setattr(life, "MAX_SOURCES", 2)
    life.declare_process("backend")
    cache = FakeRedis(decode_responses=True)
    item = registry_item("protected")
    item.update(changes)
    cache.hset(life.REGISTRY_KEY, mapping={"protected": json.dumps(item),
        "active": json.dumps(source_row("active", last_seen_epoch=NOW.replace(tzinfo=timezone.utc).timestamp()))})
    assert not life._process.pulse(cache)
    assert cache.hexists(life.REGISTRY_KEY, "protected") and cache.hlen(life.REGISTRY_KEY) == 2
    assert cache.get(life.REGISTRY_ERROR_KEY) == "source_registry_capacity_exceeded"
