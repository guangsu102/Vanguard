"""Actual hook/transport boundaries without touching a frozen release or host."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as Obj
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "acquisition"
spec = importlib.util.spec_from_file_location("sla_draft_runtime", ROOT / "qualification_sla_runtime.py")
runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)
NOW = datetime(2026, 11, 5, 18)


class Capture:
    def __init__(self):
        self.events = []
        self.lost = {}
    def offer(self, kind, event):
        self.events.append((kind, event))
        return True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("QUALIFICATION_SLA_ENABLED", "true")
    # This module loads a second copy to exercise hook boundaries. Keep its
    # session outbox separate from the canonical runtime imported by other tests.
    monkeypatch.setattr(runtime, "STAGED", "qualification.sla.test_alias.staged")
    monkeypatch.setattr(runtime, "_producer", Capture())
    monkeypatch.setattr(runtime, "_staged_count", 0)
    monkeypatch.setattr(runtime, "actor_account", lambda: 33)
    monkeypatch.setattr(runtime, "_now", lambda: NOW)
    token = runtime._binding.set(None)
    yield
    runtime._binding.reset(token)


def work(**changes):
    return Obj(**({"account_id": 33, "id": 1, "revision": 1, "ready_at": NOW-timedelta(minutes=2),
        "family": "qualification", "scope_key": "scope", "state": "READY"} | changes))


def bind():
    return runtime.bind_claim(Obj(sla_episode=runtime.reference(work()), token="claim"), 33)


def test_claim_only_does_not_count_actual_step():
    token = bind()
    try:
        assert runtime._producer.events == []
    finally:
        runtime.reset_binding(token)


def test_actual_budget_step_has_owner_and_funding_without_upgrading_origin():
    token = bind()
    try:
        runtime.observe_health(None, NOW)
        assert runtime.budget_step_entered(33, Obj(account_id=33, claimed=True, limit=8,
            scope="membership", kind="review", phase="first"))
        kind, event = runtime._producer.events[0]
        assert kind == "started" and event["budget_granted"]
        assert event["health_verified"] is False
        assert "eligibility" not in event
        assert event["ready_at"] == (NOW-timedelta(minutes=2)).isoformat()
    finally:
        runtime.reset_binding(token)


@pytest.mark.parametrize("claimed,limit", [(False, 8), (True, 0)])
def test_unfunded_or_implicit_zero_is_not_actual_step(claimed, limit):
    token = bind()
    try:
        assert not runtime.budget_step_entered(33, Obj(account_id=33, claimed=claimed, limit=limit,
            scope="membership", kind="review", phase="first"))
        assert not runtime._producer.events
    finally:
        runtime.reset_binding(token)


def test_foreign_actor_and_detached_provider_do_not_start(monkeypatch):
    token = bind()
    try:
        monkeypatch.setattr(runtime, "actor_account", lambda: None)
        assert not runtime.budget_step_entered(33, Obj(account_id=33, claimed=True, limit=8,
            scope="membership", kind="review", phase="first"))
        assert not runtime._producer.events
    finally:
        runtime.reset_binding(token)


@pytest.mark.parametrize("state", ["READY", "WAIT_BUDGET", "WAIT_MATURITY"])
def test_persistent_due_time_survives_offline_maturity(state):
    original_due = NOW-timedelta(hours=3)
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        with session.begin():
            runtime.stage_ready(session, work(state=state, ready_at=original_due))
        _, event = runtime._producer.events[0]
        assert event["ready_at"] == original_due.isoformat()
        assert event["origin_state"] == state


@pytest.mark.parametrize("state", ["WAIT_PROVIDER", "WAIT_EVENT", "DONE", "STALE"])
def test_nonready_dependency_timer_cannot_create_episode(state):
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        with session.begin():
            runtime.stage_ready(session, work(state=state))
        assert runtime.reference(work(state=state)) is None
        assert not runtime._producer.events


def test_savepoint_rollback_discards_only_inner_events():
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        with session.begin():
            runtime.stage_ready(session, work(id=1))
            try:
                with session.begin_nested():
                    runtime.stage_ready(session, work(id=2))
                    raise ValueError("rollback nested")
            except ValueError:
                pass
        assert [event["work_id"] for _, event in runtime._producer.events] == [1]


def test_savepoint_commit_waits_for_outer_business_commit():
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        with session.begin():
            with session.begin_nested():
                runtime.stage_ready(session, work())
            assert not runtime._producer.events
        assert len(runtime._producer.events) == 1


def test_outer_rollback_never_publishes_ready():
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        with pytest.raises(ValueError):
            with session.begin():
                runtime.stage_ready(session, work())
                raise ValueError("rollback")
        assert not runtime._producer.events


def test_queue_is_bounded_and_overflow_remains_unknown():
    producer = runtime.Producer(lambda: None, role="growth")
    for _ in range(runtime.QUEUE_LIMIT):
        assert producer.offer("ready", runtime.reference(work()))
    assert not producer.offer("ready", runtime.reference(work()))
    assert producer.queue.qsize() == runtime.QUEUE_LIMIT
    assert producer.lost == {33: NOW}


@pytest.mark.asyncio
async def test_blocked_measurement_database_cannot_block_business_actor(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def blocked_write(self, batch):
        entered.set()
        await release.wait()
    monkeypatch.setattr(runtime.Producer, "_write", blocked_write)
    producer = runtime.Producer(lambda: None, role="growth")
    monkeypatch.setattr(runtime, "_producer", producer)
    task = asyncio.create_task(producer.run())
    producer.task = task
    try:
        assert runtime.emit("ready", runtime.reference(work()))
        await asyncio.wait_for(entered.wait(), 1)
        async def business_actor():
            token = bind()
            try:
                runtime.observe_health(True, NOW)
                assert runtime.budget_step_entered(33, Obj(account_id=33, claimed=True, limit=8,
                    scope="membership", kind="review", phase="first"))
                return "business advanced"
            finally:
                runtime.reset_binding(token)
        assert await asyncio.wait_for(business_actor(), 0.1) == "business advanced"
        assert not release.is_set()
    finally:
        release.set()
        producer.stopping = True
        await asyncio.wait_for(task, 1)


def test_disabled_hooks_do_not_stage_or_record_losses(monkeypatch):
    monkeypatch.delenv("QUALIFICATION_SLA_ENABLED", raising=False)
    monkeypatch.setattr(runtime, "_producer", None)
    monkeypatch.setattr(runtime, "_unconfigured_losses", {})
    with Session(create_engine("sqlite://")) as session:
        with session.begin():
            runtime.stage_ready(session, work())
        assert runtime.STAGED not in session.info
    assert runtime.emit("ready", runtime.reference(work())) is False
    assert runtime._unconfigured_losses == {}


def test_status_counts_staged_queued_and_inflight_events_without_io(monkeypatch):
    producer = runtime.Producer(lambda: None, role="growth")
    monkeypatch.setattr(runtime, "_producer", producer)
    monkeypatch.setattr(runtime, "_unconfigured_losses", {})
    producer.offer("ready", runtime.reference(work()))
    producer.in_flight = 2
    with Session(create_engine("sqlite://")) as session:
        with session.begin():
            runtime.stage_ready(session, work())
            assert runtime.process_source_status()["pending_count"] == 4
        assert runtime.process_source_status()["pending_count"] == 4  # staged moved into queue
    producer.lost[33] = NOW
    assert runtime.process_source_status()["lost_since"] == NOW.isoformat()


def test_global_staged_limit_prevents_many_session_overflow(monkeypatch):
    monkeypatch.setattr(runtime, "_staged_count", runtime.QUEUE_LIMIT)
    with Session(create_engine("sqlite://")) as session:
        with session.begin():
            runtime.stage_ready(session, work())
        assert runtime._producer.lost == {33: NOW}
