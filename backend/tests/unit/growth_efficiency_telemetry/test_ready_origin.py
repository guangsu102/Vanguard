"""Necessary R21 integration tests only: local SQLite, real R20 hook bodies."""
import ast
from contextvars import ContextVar
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, String, Text, event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.modules.acquisition import qualification_ready_origin as origin
OUT = Path(__file__).resolve().parents[3] / 'app'

NOW = datetime(2026, 11, 7, 1, 0, 0)


class Base(DeclarativeBase):
    pass


class Work(Base):
    __tablename__ = "qualification_work"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer)
    membership_id = Column(Integer)
    group_id = Column(Integer)
    membership_joined_at = Column(DateTime)
    revision = Column(Integer)
    scope_key = Column(String)
    family = Column(String)
    kind = Column(String)
    policy_version = Column(String)
    profile_fingerprint = Column(String)
    state = Column(String)
    ready_at = Column(DateTime)
    checkpoint_json = Column(Text)
    progress_json = Column(Text)
    claim_token = Column(String)


class Setting(Base):
    __tablename__ = "system_setting"
    key = Column(String(120), primary_key=True)
    value = Column(Text)


def work_at(at=NOW, **changes):
    values = dict(id=1, account_id=33, membership_id=60, group_id=40,
        membership_joined_at=NOW-timedelta(days=1), revision=1, scope_key="scope",
        family="qualification", kind="first", policy_version="policy", profile_fingerprint="profile",
        state="READY", ready_at=at, checkpoint_json="{}", claim_token=None,
        progress_json=json.dumps({"read_plan": {"phase": "first", "required_reads": 8}}))
    return Work(**{**values, **changes})


def hooks():
    names = {"reference", "stage", "stage_ready", "after_commit", "after_soft_rollback",
        "bind_claim", "observe_health", "_start_event", "budget_step_entered", "current_admission"}
    tree = ast.parse((OUT/"modules/acquisition/qualification_sla_runtime.py").read_text(encoding="utf-8"))
    selected = ast.Module([node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names], type_ignores=[])
    emitted = []
    def emit(kind, value):
        emitted.append((kind, value))
        return True
    namespace = dict(ready_origin=origin, Any=Any, datetime=datetime, _staged_count=0, QUEUE_LIMIT=256,
        STAGED="test.r21.staged", _best_effort=lambda *args: lambda function: function,
        _now=lambda: NOW, _iso=origin._iso, _hash=origin._hash,
        _binding=ContextVar("r21_test_binding", default=None), actor_account=lambda: 33, enabled=lambda: True,
        emit=emit, _lost=lambda *args: pytest.fail("unexpected measurement overflow"))
    exec(compile(selected, "real-r20-candidate-runtime-hooks", "exec"), namespace)
    return SimpleNamespace(**namespace), emitted


@pytest.fixture(autouse=True)
def epoch(monkeypatch):
    monkeypatch.setenv(origin.ENV, json.dumps({"id": "future-r21-test", "started_at": (NOW-timedelta(seconds=1)).isoformat(), "accounts": [33]}))


@pytest_asyncio.fixture
async def db(monkeypatch):
    class BusinessSession(Session):
        pass
    runtime, emitted = hooks()
    event.listen(BusinessSession, "after_commit", runtime.after_commit)
    event.listen(BusinessSession, "after_soft_rollback", runtime.after_soft_rollback)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setitem(sys.modules, "app.core.settings_models", SimpleNamespace(SystemSetting=Setting))
    try:
        yield engine, BusinessSession, runtime, emitted
    finally:
        event.remove(BusinessSession, "after_commit", runtime.after_commit)
        event.remove(BusinessSession, "after_soft_rollback", runtime.after_soft_rollback)
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_work_update_and_checkpoint_commit_or_rollback_together(db):
    engine, cls, runtime, emitted = db
    writes = []
    def observe_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(("INSERT INTO qualification_work", "UPDATE qualification_work")):
            writes.append(statement.split()[0])
    event.listen(engine.sync_engine, "before_cursor_execute", observe_sql)
    async with AsyncSession(engine, sync_session_class=cls, expire_on_commit=False) as session:
        work = work_at(id=None)
        session.add(work)
        runtime.stage_ready(session, work)
        assert origin.NAMESPACE in json.loads(work.checkpoint_json)
        await session.flush()
        await session.rollback()
    assert not emitted
    async with AsyncSession(engine, sync_session_class=cls, expire_on_commit=False) as session:
        work = work_at(id=None)
        session.add(work)
        runtime.stage_ready(session, work)
        checkpoint = work.checkpoint_json
        await session.flush()
        await session.commit()
    async with AsyncSession(engine) as session:
        saved = await session.get(Work, 1)
        assert origin.NAMESPACE in json.loads(saved.checkpoint_json)
        assert saved.checkpoint_json == checkpoint
        pending = json.loads(saved.checkpoint_json)[origin.NAMESPACE]
        assert pending["identity"]["work_id"] is None and pending["locator_at_origin"] == "pending_insert"
    assert len(emitted) == 1 and emitted[0][1]["origin_unknown"] is True
    assert "_ready_origin_work" not in emitted[0][1]
    bound = emitted[0][1]["ready_origin_epoch"]
    assert bound["row_binding"]["work_id"] == 1 and bound["identity"]["work_id"] is None
    assert bound["origin_id"] == pending["origin_id"] and bound["ready_at"] == pending["ready_at"]
    assert writes == ["INSERT", "INSERT"]  # No telemetry-only UPDATE after either initial INSERT.
    async with AsyncSession(engine, sync_session_class=cls, expire_on_commit=False) as session:
        saved = await session.get(Work, 1)
        saved.state, saved.ready_at = "WAIT_BUDGET", NOW+timedelta(seconds=1)
        runtime.stage_ready(session, saved)
        await session.flush()
        await session.commit()
    assert writes == ["INSERT", "INSERT", "UPDATE"]
    event.remove(engine.sync_engine, "before_cursor_execute", observe_sql)


@pytest.mark.asyncio
async def test_outer_final_state_suppresses_inner_enqueue_ready(db):
    engine, cls, runtime, emitted = db
    async with AsyncSession(engine, sync_session_class=cls, expire_on_commit=False) as session:
        work = work_at()
        session.add(work)
        await session.flush()
        runtime.stage_ready(session, work)
        work.state, work.ready_at = "DONE", None
        runtime.stage_ready(session, work)
        await session.commit()
    assert emitted == []


def test_maturity_due_is_preserved_and_late_first_observation_stays_unknown():
    runtime, _ = hooks()
    due = NOW+timedelta(hours=1)
    work = work_at(due, state="WAIT_MATURITY")
    runtime.stage_ready(SimpleNamespace(info={}, get_nested_transaction=lambda: None, get_transaction=lambda: None), work)
    first = json.loads(work.checkpoint_json)[origin.NAMESPACE]
    assert first["ready_at"] == due.isoformat() and first["origin_reason"] == "scheduled_due_not_observed"
    work.state, work.claim_token = "RUNNING", "claim"
    assert origin.capture_claim(work, runtime.reference(work), due+timedelta(seconds=30), health=True)
    current = json.loads(work.checkpoint_json)[origin.NAMESPACE]
    assert current["ready_at"] == due.isoformat()
    assert current["due_observation_reason"] == "due_first_observed_late"
    assert current["eligibility"] == {}


def test_actual_funded_hook_is_wired_but_never_upgrades_earlier_origin():
    runtime, emitted = hooks()
    work = work_at(claim_token="claim")
    assert origin.capture_ready(work, {}, NOW)
    assert origin.capture_claim(work, {}, NOW, health=True)
    reference = runtime.reference(work)
    runtime.bind_claim(SimpleNamespace(sla_episode=reference, token="claim"), 33)
    runtime.observe_health(True, NOW)
    read = SimpleNamespace(claimed=True, account_id=33, limit=8, scope="member:60", kind="review", phase="first")
    assert runtime.budget_step_entered(33, read)
    observed = runtime.current_admission()
    assert observed["budget_granted"] and observed["required_reads"] == 8
    assert origin.capture_admission(work, observed)
    current = json.loads(work.checkpoint_json)[origin.NAMESPACE]
    assert current["actual_admission"]["source"] == "existing_budget_step_entered"
    assert current["origin_reason"] == "origin_decision_missing" and current["eligibility"] == {}
    assert emitted[0][0] == "started"


def test_claim_scope_and_incarnation_mismatch_rejected_and_checkpoint_merge_bounded():
    runtime, _ = hooks()
    work = work_at(claim_token="claim")
    origin.capture_ready(work, {}, NOW)
    old = work.checkpoint_json
    work.scope_key = "changed"
    assert not origin.capture_claim(work, {}, NOW, health=True)
    assert work.checkpoint_json == old
    work.scope_key = "scope"
    work.membership_joined_at += timedelta(seconds=1)
    assert not origin.capture_claim(work, {}, NOW, health=True)
    merged = origin.preserve_checkpoint(old, json.dumps({"version": 1, "stages": {"collection": {"revision": "new"}}}))
    assert json.loads(merged)["stages"]["collection"]["revision"] == "new"
    assert json.loads(merged)[origin.NAMESPACE] == json.loads(old)[origin.NAMESPACE]
    work.membership_joined_at -= timedelta(seconds=1)
    work.ready_at += timedelta(seconds=1)
    assert origin.capture_ready(work, {}, NOW+timedelta(seconds=1))
    assert json.loads(work.checkpoint_json)[origin.NAMESPACE]["overwritten_count"] == 1
    assert len(json.dumps(json.loads(work.checkpoint_json)[origin.NAMESPACE]).encode()) <= origin.LIMIT
    new = work_at(id=None)
    assert origin.capture_ready(new, {}, NOW)
    original = json.loads(new.checkpoint_json)[origin.NAMESPACE]
    new.id = 99
    assert origin.capture_ready(new, {}, NOW)
    assert json.loads(new.checkpoint_json)[origin.NAMESPACE]["origin_id"] == original["origin_id"]
    assert runtime.reference(new)["ready_origin_epoch"]["row_binding"]["work_id"] == 99
    new.group_id += 1
    assert "ready_origin_epoch" not in runtime.reference(new)


@pytest.mark.asyncio
async def test_existing_real_r20_resolver_writer_replay_keeps_same_episode(db, monkeypatch):
    engine, _, runtime, _ = db
    monkeypatch.setitem(sys.modules, "app.modules.acquisition.qualification_ready_origin", origin)
    spec = importlib.util.spec_from_file_location("r21_candidate_real_ledger", OUT/"modules/acquisition/qualification_sla.py")
    ledger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ledger)
    work = work_at()
    origin.capture_ready(work, {}, NOW)
    item = runtime.reference(work)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for _ in range(2):
            canonical = await ledger.resolve_event(session, "ready", item, NOW)
            recorded = await ledger.record_ready(session, canonical, NOW)
            assert recorded["recorded"]
            assert await origin.annotate_existing(session, canonical, recorded["episode_key"], Setting)
        await session.commit()
    async with AsyncSession(engine) as session:
        rows = list((await session.scalars(select(Setting).where(Setting.key.startswith(ledger.PREFIX+"33.")))).all())
        assert len(rows) == 1
        stored = json.loads(rows[0].value)
        assert stored["eligibility"]["state"] == "unknown"
        assert stored["ready_origin_epoch"]["epoch"]["id"] == "future-r21-test"
        report = origin.cohort_report([stored], "future-r21-test", window_start=NOW, window_end=NOW)
        assert report["origin_counts"]["unknown"] == 1
        assert report["overall_strict_rate"] is None and report["population_coverage_complete"] is False
        full_report = await ledger.report(session, 33, NOW+timedelta(seconds=3), since=NOW-timedelta(seconds=1))
        assert full_report["ready_origin_cohort"]["observed_cohort_count"] == 1
        assert full_report["strict_rate"] is None and not session.new and not session.dirty
        outside = await ledger.report(session, 33, NOW+timedelta(seconds=3), since=NOW+timedelta(seconds=1))
        assert outside["ready_origin_cohort"]["observed_cohort_count"] == 0
        malformed = {**stored, "ready_origin_epoch": {**stored["ready_origin_epoch"], "ready_at": "bad"}}
        invalid = origin.cohort_report([malformed], "future-r21-test", window_start=NOW, window_end=NOW)
        assert invalid["invalid_checkpoint_rows"] == 1 and invalid["cohort_rate"] is None
        malformed_states = [{**stored, "eligibility": {"state": state}} for state in ([], {})]
        unknown = origin.cohort_report(malformed_states, "future-r21-test", window_start=NOW, window_end=NOW)
        assert unknown["origin_counts"]["unknown"] == 2 and unknown["cohort_rate"] is None


def test_old_epoch_is_never_backfilled_and_business_measurement_failures_are_optional(monkeypatch):
    work = work_at(NOW-timedelta(hours=1))
    assert origin.capture_ready(work, {}, NOW) is False and work.checkpoint_json == "{}"
    migrated = work_at(kind="migration")
    assert origin.capture_ready(migrated, {}, NOW) is False and migrated.checkpoint_json == "{}"
    work = work_at()
    assert origin.capture_ready(work, {}, NOW)
    monkeypatch.setenv(origin.ENV, json.dumps({"id": "other-epoch", "started_at": NOW.isoformat(), "accounts": [33]}))
    runtime, _ = hooks()
    assert "ready_origin_epoch" not in runtime.reference(work)
    broken = SimpleNamespace()
    assert origin.capture_ready(broken, {}, NOW) is False
    assert origin.capture_claim(broken, {}, NOW, health=True) is False
    minimal = SimpleNamespace(account_id=33, id=9, revision=1, ready_at=NOW,
        family="qualification", scope_key="minimal", state="READY")
    monkeypatch.delenv(origin.ENV)
    legacy = runtime.reference(minimal)
    assert legacy is not None and legacy["work_id"] == 9 and "origin_unknown" not in legacy
    assert origin.final_reference(minimal, legacy)["work_id"] == 9
    monkeypatch.setenv(origin.ENV, json.dumps({"id": "future-r21-test", "started_at": NOW.isoformat(), "accounts": [33]}))
    unknown = runtime.reference(minimal)
    assert unknown is not None and unknown["origin_unknown"] and unknown["eligibility"] == {}
    assert origin.final_reference(minimal, unknown)["work_id"] == 9
    fake_session = SimpleNamespace(info={}, get_nested_transaction=lambda: None, get_transaction=lambda: "t")
    runtime.stage_ready(fake_session, minimal)
    staged = fake_session.info["test.r21.staged"]["t"][0][1]
    assert staged["origin_unknown"] and staged["eligibility"] == {}


def test_patch_has_real_callsite_sources_and_no_extra_business_await_or_new_writer():
    engine = (OUT/"modules/acquisition/qualification_engine.py").read_text(encoding="utf-8")
    runtime = (OUT/"modules/acquisition/qualification_sla_runtime.py").read_text(encoding="utf-8")
    ledger = (OUT/"modules/acquisition/qualification_sla.py").read_text(encoding="utf-8")
    assert "ready_origin.capture_claim(work, sla_episode" in engine
    assert engine.count("ready_origin.capture_admission(work, sla_measure.current_admission())") == 3
    assert "sla_measure.stage_ready(service.db, work)" in engine
    assert "ready_origin_admission" in runtime and "work.claimed" in runtime
    assert "await ready_origin.annotate_existing(db, canonical" in runtime
    assert "await ready_origin" not in engine
    assert "    sla_measure.stage_ready(db, work)\n    await db.flush()\n" in engine
    assert 'result["ready_origin_cohort"] = ready_origin.cohort_report(' in ledger
    candidate = Path(origin.__file__).read_text(encoding="utf-8")
    for forbidden in ("create_async_engine", "sessionmaker", "get_redis", "db.commit(", "db.rollback("):
        assert forbidden not in candidate
