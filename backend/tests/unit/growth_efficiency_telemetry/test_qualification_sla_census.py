"""Real SQLite keyset pages and commit/rollback boundaries for the census."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, Integer, String, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "acquisition"
sys.path.insert(0, str(ROOT.parents[2]))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


runtime = load("sla_census_runtime", "qualification_sla_runtime.py")
event.remove(Session, "after_commit", runtime.after_commit)
event.remove(Session, "after_soft_rollback", runtime.after_soft_rollback)
ledger = load("sla_census_ledger", "qualification_sla.py")
NOW = datetime(2026, 11, 6, 12)


class SourceBase(DeclarativeBase):
    pass


class Work(SourceBase):
    __tablename__ = "census_work_fixture"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String, default="READY")
    family: Mapped[str] = mapped_column(String, default="qualification")
    scope_key: Mapped[str] = mapped_column(String)
    ready_at: Mapped[datetime] = mapped_column(DateTime)


@pytest_asyncio.fixture
async def fixture(monkeypatch):
    import app.modules.acquisition as package
    from app.core.settings_models import SystemSetting
    monkeypatch.setenv("QUALIFICATION_SLA_ENABLED", "true")
    monkeypatch.setenv("QUALIFICATION_SLA_CENSUS_ROLE", "growth")
    monkeypatch.setattr(runtime, "_now", lambda: NOW)
    monkeypatch.setattr(runtime, "_staged_count", 0)
    monkeypatch.setattr(runtime, "_global_loss_since", None)
    monkeypatch.setattr(runtime, "_unconfigured_losses", {})
    cohort, fail_commit = [{33}], [False]
    costs = SimpleNamespace(observe_work=AsyncMock(), reconcile_pending=AsyncMock())
    for name, module in {"qualification_sla": ledger, "qualification_cost": costs,
        "models": SimpleNamespace(QualificationWork=Work),
        "qualification_engine": SimpleNamespace(enabled_account_ids=lambda: set(cohort[0]))}.items():
        monkeypatch.setitem(sys.modules, "app.modules.acquisition."+name, module)
        monkeypatch.setattr(package, name, module, raising=False)
    class ControlledSession(AsyncSession):
        async def commit(self):
            if fail_commit[0]:
                fail_commit[0] = False
                raise RuntimeError("injected measurement commit failure")
            await super().commit()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SourceBase.metadata.create_all)
            await connection.run_sync(lambda sync: SystemSetting.__table__.create(sync))
        factory = async_sessionmaker(engine, class_=ControlledSession, expire_on_commit=False, autoflush=False)
        async def registry():
            return {"complete": True, "sources": [], "observed_at": NOW.isoformat()}
        def producer(role="growth", **kwargs):
            return runtime.Producer(factory, role=role, expected_source_counts={"growth": 1, "celery:main": 18},
                source_provider=registry, **kwargs)
        yield SimpleNamespace(factory=factory, producer=producer, costs=costs,
            cohort=cohort, fail_commit=fail_commit)
    finally:
        await engine.dispose()


async def add_work(fixture, count, *, first=1, account=33):
    async with fixture.factory() as db:
        db.add_all([Work(id=value, account_id=account, scope_key="scope-"+str(value),
            ready_at=NOW-timedelta(minutes=5)) for value in range(first, first+count)])
        await db.commit()


async def page(producer):
    if producer.queue.empty():
        assert producer.offer("census", {})
    item = producer.queue.get_nowait()
    try:
        await producer._write([item])
    finally:
        producer.queue.task_done()


async def state(fixture, account=33):
    from app.core.settings_models import SystemSetting
    async with fixture.factory() as db:
        rows = (await db.scalars(select(SystemSetting))).all()
        episodes = [json.loads(row.value) for row in rows if row.key.startswith(ledger.PREFIX)]
        cursors = [json.loads(row.value) for row in rows if row.key.startswith(runtime.CENSUS_PREFIX)]
        marker = next((json.loads(row.value) for row in rows if row.key == ledger.COVERAGE_PREFIX+str(account)), {})
        return episodes, cursors, marker


@pytest.mark.asyncio
async def test_multiple_real_sql_pages_only_publish_last_scan_after_the_complete_pass(fixture):
    count = runtime.CENSUS_PAGE_SIZE*2+3
    await add_work(fixture, count)
    producer = fixture.producer()
    for expected in range(4, count, 4):
        await page(producer)
        episodes, cursors, marker = await state(fixture)
        assert len(episodes) == expected and len(cursors) == 1
        assert producer.census_at is None and "last_scan_at" not in marker
        assert producer.census_pending and producer.queue.qsize() == 1
    await page(producer)
    episodes, cursors, marker = await state(fixture)
    assert len(episodes) == count and cursors == []
    assert producer.census_at == NOW and marker["last_scan_at"] == NOW.isoformat()
    assert not producer.census_pending and producer.queue.empty()
    fixture.costs.reconcile_pending.assert_awaited_once_with(ANY, {33}, NOW, limit=16)


@pytest.mark.asyncio
async def test_failed_commit_never_advances_durable_or_memory_cursor(fixture):
    await add_work(fixture, 35)
    producer = fixture.producer()
    await page(producer)
    before = dict(producer.census_cursor)
    fixture.fail_commit[0] = True
    with pytest.raises(RuntimeError, match="commit failure"):
        await page(producer)
    episodes, cursors, marker = await state(fixture)
    assert len(episodes) == 4 and cursors[0]["after_id"] == 4
    assert producer.census_cursor == before and producer.census_at is None
    assert "last_scan_at" not in marker
    await page(producer)
    assert producer.census_cursor["after_id"] == 8


@pytest.mark.asyncio
async def test_owner_refreshes_completed_census_without_a_dispatcher_in_its_process(fixture, monkeypatch):
    """Celery dispatch requests cannot wake the different growth owner loop."""
    await add_work(fixture, 1)
    clock = [NOW]
    monkeypatch.setattr(runtime, "_now", lambda: clock[0])
    producer = fixture.producer()
    observed_new_work = asyncio.Event()
    async def observed(db, event, now):
        if event["work_id"] == 2:
            observed_new_work.set()
    fixture.costs.observe_work.side_effect = observed
    assert producer.offer("census", {})
    task = asyncio.create_task(producer.run())
    try:
        await asyncio.wait_for(producer.queue.join(), 1)
        assert producer.census_at == NOW and not producer.census_pending
        await add_work(fixture, 1, first=2)
        clock[0] = NOW+timedelta(seconds=21)
        # No request_census call or business event is supplied to this process.
        await asyncio.wait_for(observed_new_work.wait(), 1)
        await asyncio.wait_for(producer.queue.join(), 1)
        _, cursors, marker = await state(fixture)
        assert marker["last_scan_at"] == clock[0].isoformat()
        assert cursors == [] and producer.census_at == clock[0]
    finally:
        producer.stopping = True
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_fresh_owner_process_resumes_durable_page_and_cohort_change_reopens_at_low_ids(fixture):
    await add_work(fixture, 1, account=34)
    await add_work(fixture, 34, first=2)
    first = fixture.producer()
    await page(first)
    replacement = fixture.producer()
    await page(replacement)
    assert replacement.census_cursor["after_id"] == 9
    fixture.cohort[0] = {33, 34}
    await page(replacement)
    episodes, cursors, marker = await state(fixture, account=34)
    assert any(row["work_id"] == 1 for row in episodes)
    assert replacement.census_cursor["cohort"] == [33, 34]
    assert replacement.census_cursor["after_id"] == 4
    assert len(cursors) == 1 and "last_scan_at" not in marker
    assert any(gap["reason"] == "census_cohort_changed" for gap in marker["gaps"])


@pytest.mark.asyncio
async def test_single_long_lived_owner_avoids_prefork_scans_and_event_pending_can_quiesce(fixture, monkeypatch):
    await add_work(fixture, 35)
    owner, child = fixture.producer(), fixture.producer("celery:main")
    assert owner.census_owner and not child.census_owner
    for _ in range(100):
        assert child.offer("census", {}) is False
    assert child.queue.empty() and not child.census_pending
    for _ in range(9):
        await page(owner)
        assert child.queue.empty()
    assert owner.queue.empty() and not owner.census_pending
    monkeypatch.setattr(runtime, "_producer", child)
    assert runtime.process_source_status()["pending_count"] == 0
    monkeypatch.setattr(runtime, "_producer", owner)
    assert runtime.process_source_status()["pending_count"] == 0
    _, _, marker = await state(fixture)
    assert marker["last_scan_at"] == NOW.isoformat()
    assert fixture.costs.observe_work.await_count == 35


@pytest.mark.asyncio
async def test_work_cap_retains_a_gap_and_never_claims_a_zero_or_complete_sample(fixture, monkeypatch):
    monkeypatch.setattr(runtime, "CENSUS_WORK_LIMIT", 17)
    await add_work(fixture, 18)
    producer = fixture.producer()
    for _ in range(5):
        await page(producer)
    episodes, cursors, marker = await state(fixture)
    assert len(episodes) == 17 and cursors == []
    assert marker["denominator_loss"] and "last_scan_at" not in marker
    assert any(gap["reason"] == "census_truncated" for gap in marker["gaps"])
    assert producer.census_cursor["phase"] == "capped" and producer.queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["census", "ready"])
async def test_stopping_only_durable_census_does_not_fabricate_transport_event_loss(fixture, monkeypatch, kind):
    monkeypatch.setattr(runtime, "WRITE_TIMEOUT", .01)
    producer = fixture.producer()
    entered = asyncio.Event()
    async def stalled(batch):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(producer, "_write", stalled)
    monkeypatch.setattr(runtime, "_producer", producer)
    producer.accounts.add(33)
    assert producer.offer(kind, {} if kind == "census" else {"account_id": 33})
    producer.task = asyncio.create_task(producer.run())
    await asyncio.wait_for(entered.wait(), 1)
    await runtime.stop_producer()
    assert (33 in runtime._unconfigured_losses) is (kind == "ready")


def test_owner_must_be_a_single_explicitly_counted_long_lived_source(monkeypatch):
    monkeypatch.setenv("QUALIFICATION_SLA_CENSUS_ROLE", "growth")
    assert not runtime.Producer(lambda: None, role="growth", expected_source_counts={"growth": 18}).census_owner
    monkeypatch.delenv("QUALIFICATION_SLA_CENSUS_ROLE")
    assert not runtime.Producer(lambda: None, role="growth", expected_source_counts={"growth": 1}).census_owner


@pytest.mark.asyncio
async def test_database_retires_only_confirmed_clean_sources_without_readding_registry_tombstones(fixture):
    from app.core.settings_models import SystemSetting
    last = NOW-timedelta(seconds=61)
    clean = {"role": "celery:main", "generation": "clean", "started_at": (NOW-timedelta(hours=1)).isoformat(),
        "last_seen_at": last.isoformat(), "closed_at": last.isoformat(), "confirmed_through": last.isoformat(),
        "clean_closed": True, "status_valid": True, "pending_count": 0,
        "lost_since": None, "history_loss": False, "gaps": []}
    dirty = {**clean, "generation": "unconfirmed", "gaps": [{"start": (last-timedelta(seconds=1)).isoformat(),
        "end": last.isoformat(), "reason": "producer_transport_gap"}]}
    keys = [runtime.SOURCE_PREFIX+item["role"]+"."+item["generation"] for item in (clean, dirty)]
    async with fixture.factory() as db:
        db.add_all([SystemSetting(key=key, value=json.dumps(item)) for key, item in zip(keys, (clean, dirty))])
        await db.commit()
    producer = fixture.producer()
    producer.accounts.add(33)
    async def registry():
        return {"complete": True, "sources": [clean, dirty]}
    producer.source_provider = registry
    await producer._write([])
    async with fixture.factory() as db:
        assert await db.get(SystemSetting, keys[0]) is None
        assert await db.get(SystemSetting, keys[1]) is not None
