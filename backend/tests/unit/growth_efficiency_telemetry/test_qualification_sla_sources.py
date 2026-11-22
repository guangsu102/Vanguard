from __future__ import annotations
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "acquisition"
sys.path.insert(0, str(ROOT.parents[2]))


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


runtime = module("sla_source_runtime", "qualification_sla_runtime.py")
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session
sa_event.remove(Session, "after_commit", runtime.after_commit)
sa_event.remove(Session, "after_soft_rollback", runtime.after_soft_rollback)
ledger = module("sla_source_ledger", "qualification_sla.py")
NOW = datetime(2026, 11, 5, 19)


@pytest_asyncio.fixture
async def db(monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.settings_models import SystemSetting
    monkeypatch.setitem(sys.modules, "app.modules.acquisition.qualification_sla", ledger)
    monkeypatch.setattr(runtime, "_now", lambda: NOW)
    monkeypatch.setattr(runtime, "_staged_count", 0)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SystemSetting.__table__.create(sync))
    async with async_sessionmaker(engine, autoflush=False, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def source(**changes):
    return {"role": "celery", "generation": "idle-child", "pid": 42,
        "started_at": (NOW-timedelta(hours=1)).isoformat(), "last_seen_at": (NOW-timedelta(seconds=5)).isoformat(),
        "confirmed_through": (NOW-timedelta(seconds=5)).isoformat(), "pending_count": 0,
        "clean_closed": False, "lost_since": None, **changes}


def producer(items, *, complete=True):
    async def registry():
        return {"complete": complete, "sources": items, "observed_at": NOW.isoformat()}
    value = runtime.Producer(lambda: None, role="backend", expected_source_counts={"backend": 1, "celery": 1},
        source_provider=registry, source_identity={"generation": "backend-current"})
    value.accounts.add(33)
    return value


async def coverage(db):
    from app.core.settings_models import SystemSetting
    await db.flush()
    return json.loads((await db.get(SystemSetting, ledger.COVERAGE_PREFIX+"33")).value)


@pytest.mark.asyncio
async def test_idle_registered_child_counts_without_sending_any_work(db):
    await producer([source()])._source_coverage(db, NOW)
    result = await coverage(db)
    assert result["source_inventory_verified"] is True
    assert result["transaction_frontier_verified"] is False
    assert result["active_source_counts"] == {"backend": 1, "celery": 1}
    assert result["source_pending_events"] == 0
    assert result["source_confirmed_through"] == source()["confirmed_through"]


@pytest.mark.asyncio
async def test_runtime_replaces_a_persisted_verified_transaction_frontier_with_false(db):
    from app.core.settings_models import SystemSetting
    db.add(SystemSetting(key=ledger.COVERAGE_PREFIX+"33", value=json.dumps({
        "started_at": (NOW-timedelta(hours=1)).isoformat(), "last_scan_at": NOW.isoformat(),
        "gaps": [], "transaction_frontier_verified": True})))
    await db.commit()
    await producer([source()])._source_coverage(db, NOW)
    result = await coverage(db)
    assert result["source_inventory_verified"] is True
    assert result["transaction_frontier_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [[], [source(last_seen_at=(NOW+timedelta(seconds=1)).isoformat())],
    [source(last_seen_at=(NOW-timedelta(seconds=61)).isoformat())],
    [source(), source(generation="unlisted-extra-child")]])
async def test_missing_future_stale_or_extra_source_cannot_prove_inventory(db, items):
    await producer(items)._source_coverage(db, NOW)
    assert (await coverage(db))["source_inventory_verified"] is False


@pytest.mark.asyncio
async def test_pending_source_events_remain_visible_even_if_inventory_is_complete(db):
    await producer([source(pending_count=2)])._source_coverage(db, NOW)
    result = await coverage(db)
    assert result["source_inventory_verified"] is True
    assert result["source_pending_events"] == 2


@pytest.mark.asyncio
async def test_unverified_registry_cannot_be_upgraded_from_minimal_pid_count(db):
    await producer([source()], complete=False)._source_coverage(db, NOW)
    assert (await coverage(db))["complete_override"] is False


@pytest.mark.asyncio
async def test_inventory_loss_gap_survives_recovery(db):
    value = producer([])
    await value._source_coverage(db, NOW-timedelta(seconds=10))
    await db.flush()
    async def recovered():
        return {"complete": True, "sources": [source()], "observed_at": NOW.isoformat()}
    value.source_provider = recovered
    await value._source_coverage(db, NOW)
    result = await coverage(db)
    assert result["source_inventory_verified"] is True
    assert any(item["reason"] == "source_inventory_missing" for item in result["gaps"])


@pytest.mark.asyncio
async def test_end_of_task_loop_does_not_close_the_idle_prefork_process(db):
    value = producer([source()])
    await value._source_coverage(db, NOW, closing=True)
    result = await coverage(db)
    assert result["source_inventory_verified"] is True
    assert result["active_source_counts"] == {"backend": 1, "celery": 1}


@pytest.mark.asyncio
async def test_own_registry_recovered_transport_gap_is_retained(db):
    value = producer([source(), source(role="backend", generation="backend-current", gaps=[{
        "start": (NOW-timedelta(seconds=40)).isoformat(), "end": (NOW-timedelta(seconds=20)).isoformat(),
        "reason": "source_redis_unavailable"}])])
    await value._source_coverage(db, NOW)
    result = await coverage(db)
    assert any(item["reason"] == "producer_transport_gap" for item in result["gaps"])
