"""Exercise the real advertising callers with their read-only session facade."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError

from app.core.account import rpc_governor as rpc
from app.core.account.budget_forecast import apply_forecast
from app.core.account.models import AccountOperationConfig
from app.core.settings_models import SystemSetting
from app.modules.acquisition.ad_output_plan import ad_output_plan
from app.modules.acquisition.ad_pacing import account_pacing_deadline
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.capacity import capacity_snapshot
from app.modules.acquisition.capacity_reads import CapacityReads
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    GroupAdFrequency,
)
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("caller", ["pacing", "output", "capacity"])
@pytest.mark.parametrize("forecast_fails", [False, True])
async def test_advertising_entry_points_preserve_real_session_and_budget(
    test_db,
    monkeypatch,
    caller,
    forecast_fails,
):
    account, group, _, audit = await setup(test_db)
    now = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    account.registered_at = now - timedelta(days=365)
    audit.checked_at, audit.expires_at = now, now + timedelta(days=1)
    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_ads_enabled=True,
        auto_join_enabled=True,
        dynamic_capacity_enabled=True,
        adaptive_ads_enabled=True,
        operation_mode="growth",
    )
    campaign = AdCampaign(name="forecast-boundary", enabled=True, delivery_policy="growth")
    creative = AdCreative(
        name="profile", content="Details in profile", creative_type="text", enabled=True
    )
    test_db.add_all([config, campaign, creative])
    await test_db.flush()
    test_db.add(
        AccountAdBinding(
            account_id=account.id, ad_campaign_id=campaign.id, creative_id=creative.id, enabled=True
        )
    )
    await test_db.commit()
    cache = mock_usage(monkeypatch, {})
    cache.lrange = AsyncMock(return_value=[])
    cache.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        AcquisitionAutomationService,
        "_get_ad_delivery_cooldown_until",
        AsyncMock(return_value=None),
    )
    before = await rpc.snapshot(test_db, account.id, now)
    assert before["state"] == "ready" and not before.get("forecast_errors")
    if forecast_fails:

        async def broken(db, account_id, checked_at, limits, budget):
            limits["day"] = 999999
            await db.scalar(
                select(SystemSetting.value).where(SystemSetting.key == "before.failure")
            )
            raise ValueError("optional_forecast_failed")

        monkeypatch.setattr("app.core.account.survival_lending.add_idle_survival_headroom", broken)
    # This write must survive the forecast's savepoint and remain uncommitted.
    test_db.add(SystemSetting(key="business.pending", value="preserved"))
    commits = []

    def outer_commit(session):
        if not session.in_nested_transaction():
            commits.append(True)

    event.listen(test_db.sync_session, "after_commit", outer_commit)
    try:
        if caller == "pacing":
            result = await account_pacing_deadline(test_db, config, now, now - timedelta(minutes=1))
            assert result is not None and result > now
        elif caller == "output":
            result = await ad_output_plan(test_db, account, config, now)
            assert result["sustainable_ads_24h"] > 0
        else:
            result = await capacity_snapshot(test_db, account.id, now)
            assert result["enabled"] is True
            assert "telegram_rpc_guard_unavailable" not in result["blockers"]
        assert not commits and test_db.in_transaction() and test_db.is_active
        after = await rpc.snapshot(CapacityReads(test_db), account.id, now)
        assert after["state"] == "ready" and after["usage"] == before["usage"]
        assert after["limits"]["day"] == before["limits"]["day"]
        assert bool(after.get("forecast_errors")) == forecast_fails
        await test_db.commit()
        assert commits == [True]
        assert (await test_db.get(SystemSetting, "business.pending")).value == "preserved"
    finally:
        event.remove(test_db.sync_session, "after_commit", outer_commit)


async def test_forecast_savepoint_retains_cache_until_rollback_and_enforces_read_only(test_db):
    test_db.add(SystemSetting(key="projection", value="original"))
    await test_db.commit()
    reader = CapacityReads(test_db)
    statement = select(SystemSetting.value).where(SystemSetting.key == "projection")
    assert await reader.scalar(statement) == "original"
    original = await reader.get(SystemSetting, "projection")
    cached = len(reader.results)
    savepoint = await reader.begin_nested()
    for operation in (
        reader.execute(update(SystemSetting).values(value="forbidden")),
        reader.execute(select(SystemSetting).with_for_update()),
        reader.get(SystemSetting, "projection", with_for_update=True),
        reader.flush(),
        reader.commit(),
    ):
        with pytest.raises(RuntimeError):
            await operation
    with pytest.raises(RuntimeError):
        reader.add(SystemSetting(key="forbidden", value="write"))
    await savepoint.commit()
    assert len(reader.results) == cached and reader.objects
    # The projection's caller can still write through its original session.
    await test_db.execute(
        update(SystemSetting).where(SystemSetting.key == "projection").values(value="new")
    )
    assert await reader.scalar(statement) == "original"
    savepoint = await reader.begin_nested()
    await savepoint.rollback()
    assert not reader.results and not reader.objects
    assert reader.is_active and test_db.in_transaction()
    assert await reader.scalar(statement) == "new"
    assert (await reader.get(SystemSetting, "projection")) is original


async def test_facade_does_not_swallow_failed_business_flush(test_db):
    test_db.add(GroupAdFrequency(telegram_group_id=42, quota=0))
    reader = CapacityReads(test_db)
    forecast = AsyncMock()
    with pytest.raises(IntegrityError):
        await apply_forecast(reader, 2, datetime.utcnow(), "invalid_business", forecast, {}, {})
    assert not reader.is_active
    forecast.assert_not_awaited()
    await test_db.rollback()


async def test_facade_reports_budget_state_failure_without_a_secondary_attribute_error(
    test_db, monkeypatch
):
    monkeypatch.setattr(
        rpc, "read_budget_state", AsyncMock(side_effect=ConnectionError("cache_unavailable"))
    )
    state = await rpc.snapshot(CapacityReads(test_db), 2, datetime.utcnow())
    assert state["state"] == "unavailable" and state["reason"] == "telegram_rpc_guard_unavailable"
    assert test_db.is_active
