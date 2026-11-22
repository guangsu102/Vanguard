"""Exercise the real recovery and opportunity code against a local SQLite database."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import importlib
import json
import sys
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import Base
from app.core.settings_models import SystemSetting
from app.core.account.models import AccountOutboundAttempt
from app.modules.acquisition.models import AdDeliveryLog
from tests.unit.test_ad_opportunities import due_fixture, opportunity_rows, receipt_context
from tests.unit.growth_efficiency_telemetry.conftest import register_pure_models

pytestmark = pytest.mark.asyncio


@pytest.fixture
def modules(monkeypatch):
    meter = importlib.import_module("app.modules.acquisition.ad_opportunities")
    receipts = importlib.import_module("app.core.account.send_receipts")
    monkeypatch.setitem(sys.modules, "app.modules.acquisition.ad_opportunities", meter)
    import app.modules.acquisition
    monkeypatch.setattr(app.modules.acquisition, "ad_opportunities", meter)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr("app.modules.acquisition.qualification_engine._publish_wakes", AsyncMock())
    monkeypatch.setattr("app.modules.acquisition.daily_frequency.DailyFrequencyService.note_sent", AsyncMock())
    # The business reconciliation remains the existing operation; forbid a new reservation.
    monkeypatch.setattr("app.core.account.outbound_budget.AccountOutboundBudgetService.reserve",
        AsyncMock(side_effect=AssertionError("receipt measurement must not charge again")))
    return meter, receipts


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch, modules):
    register_pure_models.__wrapped__()
    engine = create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"receipt.sqlite"))
    # Enable real outer transactions so SAVEPOINT release cannot commit a
    # measurement behind the test's explicit failed outer commit.
    @event.listens_for(engine.sync_engine, "connect")
    def sqlite_transactions(connection, _record):
        connection.isolation_level = None
    @event.listens_for(engine.sync_engine, "begin")
    def sqlite_begin(connection):
        connection.exec_driver_sql("BEGIN")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.core.database.async_session_factory", factory)
    async with factory() as db:
        yield db, factory
    await engine.dispose()


async def seed(db, meter, *, message_id=4321, status="pending", context_changes=None):
    now, member, _ = await due_fixture(db)
    await meter.collect_due(db, now)
    sent = now-timedelta(minutes=5)
    log = AdDeliveryLog(account_id=2, group_id=member.group_id,
        telegram_group_id=member.telegram_group_id, ad_campaign_id=88,
        created_at=sent, status=status, reservation_token="recovered-opportunity",
        error="send_outcome_unknown:lost_sdk_response" if status == "unknown" else None,
        qualification_context_json=receipt_context(member, **(context_changes or {})),
        survival_status="not_required", survival_stage="complete")
    attempt = AccountOutboundAttempt(account_id=2, attempt_key="ad:recovered-opportunity",
        category="ad", target_key=str(member.telegram_group_id), state="succeeded" if message_id else "unknown",
        attempted_at=sent, message_id=message_id)
    db.add_all([log, attempt])
    await db.commit()
    # The original send is old enough for the existing exact-local-receipt lane.
    return now+timedelta(minutes=11), log.id


async def stored(factory, log_id):
    async with factory() as db:
        log = await db.get(AdDeliveryLog, log_id)
        rows = await opportunity_rows(db)
        return log, json.loads(rows[0].value)


async def test_recovery_closes_existing_opportunity_after_receipt_commit_and_is_idempotent(database, modules, monkeypatch):
    db, factory = database
    meter, receipts = modules
    now, identity = await seed(db, meter)
    original = meter.observe_log_safely
    calls = []
    async def observe(measurement, log):
        assert measurement is not db
        async with factory() as witness:
            saved = await witness.get(AdDeliveryLog, identity)
            assert saved.status == "success" and saved.telegram_message_id == 4321
        calls.append(log.id)
        await original(measurement, log)
    monkeypatch.setattr(meter, "observe_log_safely", observe)
    result = await receipts.recover_missing_receipts(db, now, 2, 10, independent=True)
    assert result["receipt_ids_recovered"] == 1
    log, value = await stored(factory, identity)
    assert log.status == "success" and value["state"] == "completed"
    assert value["receipt_log_id"] == identity and value["message_id"] == 4321
    assert json.loads(log.qualification_context_json)["send_reconciliation"]["automatic_resend"] is False
    again = await receipts.recover_missing_receipts(db, now, 2, 10, independent=True)
    assert again["receipt_ids_recovered"] == 0 and calls == [identity]
    async with factory() as witness:
        assert len(await opportunity_rows(witness)) == 1


@pytest.mark.parametrize("status", ["pending", "unknown"])
async def test_unproved_pending_or_unknown_never_completes_and_keeps_unknown_fence(database, modules, status):
    db, factory = database
    meter, receipts = modules
    now, identity = await seed(db, meter, message_id=None, status=status)
    result = await receipts.recover_missing_receipts(db, now, 2, 10, independent=True)
    assert result["receipt_ids_recovered"] == 0 and result["manual_evidence_required"] == 1
    log, value = await stored(factory, identity)
    assert log.status == "failed" and log.telegram_message_id is None
    assert value["state"] == "pending"
    assert json.loads(log.qualification_context_json)["send_reconciliation"]["automatic_resend"] is False
    async with factory() as witness:
        attempt = await witness.scalar(select(AccountOutboundAttempt))
        assert attempt.state == "unknown" and attempt.message_id is None


@pytest.mark.parametrize("changes", [{"membership_joined_at": "2000-01-01T00:00:00"}, {"audit_id": None}])
async def test_exact_business_receipt_with_wrong_scope_does_not_forge_completion(database, modules, changes):
    db, factory = database
    meter, receipts = modules
    now, identity = await seed(db, meter, context_changes=changes)
    assert (await receipts.recover_missing_receipts(db, now, 2, 10, independent=True))["receipt_ids_recovered"] == 1
    log, value = await stored(factory, identity)
    assert log.status == "success" and value["state"] == "pending"


@pytest.mark.parametrize("failure", ["write", "timeout", "cancel"])
async def test_measurement_failure_or_cancel_preserves_committed_receipt_and_records_gap(database, modules, monkeypatch, failure):
    db, factory = database
    meter, receipts = modules
    now, identity = await seed(db, meter)
    marker = await db.get(SystemSetting, meter.COVERAGE_PREFIX+"2")
    coverage = json.loads(marker.value)
    historical = {"start": (now-timedelta(hours=2)).isoformat(),
        "end": (now-timedelta(hours=1)).isoformat(), "reason": "dispatcher_scan_gap"}
    coverage["gaps"].append(historical)
    marker.value = json.dumps(coverage)
    await db.commit()
    async def fail(*args):
        if failure == "write":
            raise RuntimeError("measurement only")
        if failure == "timeout":
            await asyncio.Future()
        raise asyncio.CancelledError()
    monkeypatch.setattr(meter, "observe_log_safely", fail)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await receipts.recover_missing_receipts(db, now, 2, 10, independent=True)
    else:
        assert (await receipts.recover_missing_receipts(db, now, 2, 10, independent=True))["receipt_ids_recovered"] == 1
    log, value = await stored(factory, identity)
    assert log.status == "success" and log.telegram_message_id == 4321 and value["state"] == "pending"
    async with factory() as witness:
        marker = await witness.get(SystemSetting, meter.COVERAGE_PREFIX+"2")
        gaps = json.loads(marker.value)["gaps"]
        assert historical in gaps and any(gap["reason"] == "receipt_link_failed" for gap in gaps)
        assert not (await meter.summary(witness, 2, datetime.utcnow()))["complete"]


async def test_measurement_commit_failure_and_gap_write_failure_are_visible_without_failing_receipt(database, modules, monkeypatch, capsys):
    db, factory = database
    meter, receipts = modules
    now, identity = await seed(db, meter)
    @asynccontextmanager
    async def broken():
        async with factory() as measure:
            try:
                yield measure
                raise RuntimeError("measurement commit unavailable")
            finally:
                await measure.rollback()
    monkeypatch.setattr("app.core.database.get_db_session", broken)
    result = await receipts.recover_missing_receipts(db, now, 2, 10, independent=True)
    assert result["receipt_ids_recovered"] == 1
    log, value = await stored(factory, identity)
    assert log.status == "success" and value["state"] == "pending"
    output = capsys.readouterr().out
    assert "ad_recovered_receipt_measurement_failed" in output and "gap_recorded=False" in output


async def test_disabled_cohort_opens_no_measurement_session(modules, monkeypatch):
    meter, _ = modules
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", False)
    monkeypatch.setattr("app.core.database.get_db_session", lambda: pytest.fail("disabled measurement opened database"))
    await meter.observe_committed_receipts({1: 2})


async def test_actual_task_cancellation_preserves_receipt_and_propagates_after_gap(database, modules, monkeypatch):
    db, factory = database
    meter, receipts = modules
    now, identity = await seed(db, meter)
    entered = asyncio.Event()
    async def wait(*args):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(meter, "observe_log_safely", wait)
    task = asyncio.create_task(receipts.recover_missing_receipts(db, now, 2, 10, independent=True))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    log, value = await stored(factory, identity)
    assert log.status == "success" and value["state"] == "pending"
    async with factory() as witness:
        marker = await witness.get(SystemSetting, meter.COVERAGE_PREFIX+"2")
        assert any(gap["reason"] == "receipt_link_failed" for gap in json.loads(marker.value)["gaps"])
