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
spec = importlib.util.spec_from_file_location("qualification_cost_draft", ROOT / "qualification_cost.py")
cost = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cost
spec.loader.exec_module(cost)
T0 = datetime(2026, 11, 5, 18)
NOW = T0+timedelta(minutes=5)


def event(token="slice-a", **changes):
    return {"account_id": 33, "work_id": 1, "revision": 1, "membership_id": 7,
        "membership_incarnation": "2026-11-01T00:00:00", "scope_hash": "scope-a",
        "slice_token": token, "slice_kind": "review", "phase": "first", "complexity": "complex",
        "slice_started_at": T0.isoformat(), "slice_finished_at": (T0+timedelta(seconds=10)).isoformat(),
        "sdk_entered": 2, "ledger_id": "read:"+token, "reservation_not_taken": False, "failed": False, **changes}


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.settings_models import SystemSetting
    from app.core.account.control_models import AccountBudgetReservation
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SystemSetting.__table__.create(sync))
        await connection.run_sync(lambda sync: AccountBudgetReservation.__table__.create(sync))
    async with async_sessionmaker(engine, autoflush=False, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def reservation(db, token="slice-a", *, state="settled", reason="work_complete", reads=3):
    from app.core.account.control_models import AccountBudgetReservation
    db.add(AccountBudgetReservation(reservation_id="read:"+token, account_id=33,
        workload="review", lane="critical", window_start=T0.replace(hour=0), reserved_reads=8,
        consumed_reads=reads, state=state, lease_expires_at=NOW, created_at=T0,
        settled_at=T0+timedelta(seconds=10), reason=reason))
    await db.flush()


async def output(db, *, complete=True):
    return await cost.summary(db, 33, NOW, coverage_complete=complete)


@pytest.mark.asyncio
async def test_unique_tokens_accumulate_cross_slice_without_double_charging(db):
    await reservation(db)
    await reservation(db, "slice-b", reads=1)
    for item in [event(), event("slice-b", sdk_entered=1), event()]:
        await cost.record_started(db, item, NOW)
        await cost.record_finished(db, item, NOW)
    result = await output(db)
    row = result["tasks"][0]
    assert row["slices"] == 2 and row["sdk_entered"] == 3 and row["budget_charged"] == 4
    assert result["matched_task_savings"] is None and result["net_savings"] is None


@pytest.mark.asyncio
async def test_started_without_end_is_unknown_and_keeps_pending_work(db):
    await cost.record_started(db, event(), NOW)
    row = (await output(db))["tasks"][0]
    assert row["open_slices"] == 1 and row["sdk_entered"] is None and row["budget_charged"] is None
    assert not row["cost_complete"] and not row["terminal"]


@pytest.mark.asyncio
async def test_end_without_start_does_not_invent_missing_begin_evidence(db):
    await reservation(db)
    await cost.record_finished(db, event(), NOW)
    row = (await output(db))["tasks"][0]
    assert row["sdk_entered"] is None and row["known_sdk_entered"] == 2
    assert row["budget_charged"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state,reason", [("settled", "consumption_evidence_missing"), ("reserved", None)])
async def test_unknown_or_unsettled_budget_never_becomes_zero(db, state, reason):
    await reservation(db, state=state, reason=reason, reads=0)
    await cost.record_started(db, event(), NOW)
    await cost.record_finished(db, event(), NOW)
    row = (await output(db))["tasks"][0]
    assert row["sdk_entered"] == 2 and row["budget_charged"] is None and row["unknown_budget_slices"] == 1


@pytest.mark.asyncio
async def test_real_zero_without_any_reservation_is_explicit(db):
    item = event(sdk_entered=0, ledger_id=None, reservation_not_taken=True)
    await cost.record_started(db, item, NOW)
    await cost.record_finished(db, item, NOW)
    row = (await output(db))["tasks"][0]
    assert row["sdk_entered"] == 0 and row["budget_charged"] == 0


@pytest.mark.asyncio
async def test_missing_ledger_after_claim_is_unknown(db):
    item = event(sdk_entered=0, ledger_id=None)
    await cost.record_started(db, item, NOW)
    await cost.record_finished(db, item, NOW)
    assert (await output(db))["tasks"][0]["budget_charged"] is None


@pytest.mark.asyncio
async def test_failed_slice_cost_stays_in_whole_work(db):
    await reservation(db)
    item = event(failed=True)
    await cost.record_started(db, item, NOW)
    await cost.record_finished(db, item, NOW)
    row = (await output(db))["tasks"][0]
    assert row["failed_slices"] == 1 and row["sdk_entered"] == 2 and row["budget_charged"] == 3


@pytest.mark.asyncio
async def test_same_token_cannot_be_reassigned_to_new_revision_or_incarnation(db):
    await reservation(db)
    await cost.record_started(db, event(), NOW)
    await cost.record_finished(db, event(revision=2, membership_incarnation="2026-11-02T00:00:00"), NOW)
    result = await output(db)
    assert not result["coverage_complete"]
    assert all(row["sdk_entered"] is None for row in result["tasks"])


@pytest.mark.asyncio
async def test_capacity_does_not_delete_pending_or_invent_zero(db, monkeypatch):
    from app.core.settings_models import SystemSetting
    monkeypatch.setattr(cost, "MAX_ROWS", 2)
    await cost.record_started(db, event(), NOW)
    await cost.record_started(db, event("slice-b"), NOW)
    assert await db.get(SystemSetting, cost.slice_key(event())) is not None
    assert await db.get(SystemSetting, cost.slice_key(event("slice-b"))) is None
    assert not (await output(db))["coverage_complete"]


@pytest.mark.asyncio
async def test_source_gap_hides_exact_costs_but_preserves_known_observations(db):
    await reservation(db)
    await cost.record_started(db, event(), NOW)
    await cost.record_finished(db, event(), NOW)
    row = (await output(db, complete=False))["tasks"][0]
    assert row["sdk_entered"] is None and row["budget_charged"] is None
    assert row["known_sdk_entered"] == 2


@pytest.mark.asyncio
async def test_summary_is_read_only_and_does_not_flush_callers_changes(db, monkeypatch):
    from unittest.mock import AsyncMock
    await reservation(db)
    await cost.record_started(db, event(), NOW)
    await db.commit()
    monkeypatch.setattr(db, "flush", AsyncMock(side_effect=AssertionError("summary flushed")))
    monkeypatch.setattr(db, "commit", AsyncMock(side_effect=AssertionError("summary committed")))
    await output(db)


@pytest.mark.asyncio
async def test_eventual_existing_settlement_is_reconciled_without_new_charge(db):
    from app.core.account.control_models import AccountBudgetReservation
    await reservation(db, state="reserved", reads=0)
    await cost.record_started(db, event(), NOW)
    await cost.record_finished(db, event(), NOW)
    assert (await output(db))["tasks"][0]["budget_charged"] is None
    existing = await db.get(AccountBudgetReservation, "read:slice-a")
    existing.state, existing.consumed_reads, existing.reason = "settled", 3, "work_complete"
    await db.flush()
    await cost.reconcile_pending(db, {33}, NOW)
    row = (await output(db))["tasks"][0]
    assert row["budget_charged"] == 3 and row["sdk_entered"] == 2
    assert existing.reservation_id == "read:slice-a"


@pytest.mark.asyncio
async def test_slice_complexity_never_invents_whole_work_demand(db):
    await reservation(db)
    item = event(complexity="simple")
    await cost.record_started(db, item, NOW)
    await cost.record_finished(db, item, NOW)
    row = (await output(db))["tasks"][0]
    assert row["slice_complexities"] == ["simple"]
    assert row["task_demand"]["task_complexity"] == "unknown"
    assert "whole_work" in row["task_demand"]["complexity_source"]


@pytest.mark.asyncio
async def test_cost_report_cannot_promote_missing_source_or_sdk_proof(db, monkeypatch):
    from unittest.mock import AsyncMock
    await reservation(db)
    await cost.record_started(db, event(), NOW)
    await cost.record_finished(db, event(), NOW)
    await db.commit()
    monkeypatch.setattr(db, "flush", AsyncMock(side_effect=AssertionError("report flushed")))
    monkeypatch.setattr(db, "commit", AsyncMock(side_effect=AssertionError("report committed")))
    result = await cost.cost_report(db, 33, NOW, since=T0)
    assert result["coverage_complete"] is False
    assert "source_inventory_unverified" in result["incomplete_reasons"]
    assert "sdk_coverage_unverified" in result["incomplete_reasons"]
    assert result["tasks"][0]["sdk_entered"] is None
    assert result["tasks"][0]["known_sdk_entered"] == 2


@pytest.mark.asyncio
async def test_never_started_work_remains_in_cost_cohort_with_unknown_cost(db):
    await cost.observe_work(db, event(), NOW)
    row = (await output(db))["tasks"][0]
    assert row["slices"] == 0 and row["sdk_entered"] is None
    assert row["budget_charged"] is None and row["cost_complete"] is False


@pytest.mark.asyncio
async def test_whole_work_cost_report_requires_real_transport_and_sdk_watermarks(db):
    from app.core.settings_models import SystemSetting
    await reservation(db)
    await cost.record_started(db, event(), T0+timedelta(seconds=1))
    await cost.record_finished(db, event(), NOW)
    task = await db.get(SystemSetting, cost.task_key(event()))
    value = json.loads(task.value)
    value.update(terminal=True, business_state="DONE", terminal_at=(T0+timedelta(seconds=11)).isoformat())
    task.value = json.dumps(value)
    db.add(SystemSetting(key=cost.SLA_COVERAGE_PREFIX+"33", value=json.dumps({
        "started_at": T0.isoformat(), "source_confirmed_through": NOW.isoformat(),
        "source_observed_at": NOW.isoformat(), "expected_source_counts": {"backend": 1},
        "source_inventory_verified": True, "source_pending_events": 0, "gaps": [],
        "transaction_frontier_verified": True})))
    await db.commit()
    sdk = {"account_id": 33, "source": "fixed_read_metrics_v1", "measurement_version": "sdk_entry_v2", "complete": True,
        "window_start": T0.isoformat(), "window_end": NOW.isoformat(), "sources_confirmed_through": NOW.isoformat()}
    result = await cost.cost_report(db, 33, NOW, since=T0, sdk_coverage=sdk)
    assert result["coverage_complete"] is True and result["incomplete_reasons"] == []
    assert result["tasks"][0]["cost_complete"] is True
    assert result["tasks"][0]["sdk_entered"] == 2 and result["tasks"][0]["budget_charged"] == 3
    sdk["complete"] = False
    result = await cost.cost_report(db, 33, NOW, since=T0, sdk_coverage=sdk)
    assert result["tasks"][0]["sdk_entered"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source_account", [None, 34, True])
async def test_global_or_foreign_account_sdk_snapshot_cannot_prove_costs(db, source_account):
    await cost.observe_work(db, event(), NOW)
    sdk = {"account_id": source_account, "source": "fixed_read_metrics_v1", "measurement_version": "sdk_entry_v2", "complete": True,
        "window_start": T0.isoformat(), "window_end": NOW.isoformat(), "sources_confirmed_through": NOW.isoformat()}
    result = await cost.cost_report(db, 33, NOW, since=T0, sdk_coverage=sdk)
    assert "sdk_coverage_unverified" in result["incomplete_reasons"]
    assert result["tasks"][0]["sdk_entered"] is None


@pytest.mark.asyncio
async def test_api_summary_truncates_only_payload_and_preserves_total_pending_cohort(db):
    for number in range(25):
        await cost.observe_work(db, event(work_id=number+1), NOW)
    result = await cost.cost_report_summary(db, 33, NOW, since=T0, limit=1000)
    assert result["returned_tasks"] == 20 and result["total_tasks"] == 25
    assert result["tasks_truncated"] is True
    assert result["aggregate"]["pending_work"] == 25
    assert result["aggregate"]["sdk_entered"] is None
    assert "gaps" not in result["source_coverage"]


@pytest.mark.asyncio
async def test_settlement_pages_advance_durably_without_starving_later_tokens(db, monkeypatch):
    for number in range(35):
        token = "pending-"+str(number)
        await reservation(db, token, state="reserved", reads=0)
        await cost.record_started(db, event(token), NOW)
        await cost.record_finished(db, event(token), NOW)
    await db.commit()
    seen = []
    async def settled(db, item):
        seen.append(item["token_hash"])
        return True, 1, "existing_budget_ledger"
    monkeypatch.setattr(cost, "_budget", settled)
    for expected in (16, 32, 35):
        await cost.reconcile_pending(db, {33}, NOW, limit=10000)
        await db.commit()
        assert len(seen) == expected
    assert len(set(seen)) == 35


@pytest.mark.asyncio
async def test_settlement_cursor_does_not_advance_when_its_transaction_rolls_back(db, monkeypatch):
    from app.core.settings_models import SystemSetting
    await reservation(db, state="reserved", reads=0)
    await cost.record_started(db, event(), NOW)
    await cost.record_finished(db, event(), NOW)
    await db.commit()
    async def settled(db, item):
        return True, 1, "existing_budget_ledger"
    monkeypatch.setattr(cost, "_budget", settled)
    await cost.reconcile_pending(db, {33}, NOW, limit=1)
    assert await db.get(SystemSetting, cost.RECONCILE_CURSOR) is not None
    await db.rollback()
    assert await db.get(SystemSetting, cost.RECONCILE_CURSOR) is None
    await cost.reconcile_pending(db, {33}, NOW, limit=1)
    assert (await output(db))["tasks"][0]["budget_charged"] == 1
