import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.modules.acquisition.automation as automation_module
from app.api.automation import AdCampaignCreate, create_ad_campaign
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group, GroupLevel
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import (
    AdCampaign,
    AdDeliveryLog,
    AdSendMode,
    DeliveryStatus,
)


@pytest.mark.asyncio
async def test_create_scheduled_campaign_normalizes_and_validates_target_groups(test_db):
    first_group = Group(group_id=-100900001, title="First target", level=GroupLevel.A)
    second_group = Group(group_id=-100900002, title="Second target", level=GroupLevel.B)
    test_db.add_all([first_group, second_group])
    await test_db.commit()

    response = await create_ad_campaign(
        AdCampaignCreate(
            name="targeted scheduled soft ads",
            send_mode=AdSendMode.SCHEDULED.value,
            target_group_ids=[second_group.id, first_group.id, second_group.id],
            scheduled_times=["9:05", "09:05", "21:30"],
        ),
        db=test_db,
    )

    assert response["data"]["target_group_ids"] == [second_group.id, first_group.id]
    assert response["data"]["scheduled_times"] == ["09:05", "21:30"]


@pytest.mark.asyncio
async def test_create_campaign_rejects_unknown_target_group(test_db):
    with pytest.raises(HTTPException) as exc_info:
        await create_ad_campaign(
            AdCampaignCreate(
                name="unknown target",
                target_group_ids=[999999],
            ),
            db=test_db,
        )

    assert exc_info.value.status_code == 400
    assert "Target groups not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_scheduled_campaign_requires_valid_daily_time(test_db):
    with pytest.raises(HTTPException) as missing_exc:
        await create_ad_campaign(
            AdCampaignCreate(name="missing time", send_mode=AdSendMode.SCHEDULED.value),
            db=test_db,
        )
    assert missing_exc.value.status_code == 400

    with pytest.raises(HTTPException) as invalid_exc:
        await create_ad_campaign(
            AdCampaignCreate(
                name="invalid time",
                send_mode=AdSendMode.SCHEDULED.value,
                scheduled_times=["25:00"],
            ),
            db=test_db,
        )
    assert invalid_exc.value.status_code == 400
    assert "Invalid scheduled time" in invalid_exc.value.detail


def test_scheduled_slot_uses_configured_timezone_and_handles_midnight():
    service = AcquisitionAutomationService(None)
    campaign = AdCampaign(
        name="timezone schedule",
        send_mode=AdSendMode.SCHEDULED.value,
        scheduled_times=json.dumps(["09:00", "00:02"]),
    )

    morning_slot = service._scheduled_slot_start(
        campaign,
        datetime(2026, 7, 14, 1, 3),
        timezone_offset_hours=8,
    )
    midnight_slot = service._scheduled_slot_start(
        campaign,
        datetime(2026, 7, 14, 16, 0),
        timezone_offset_hours=8,
    )

    assert morning_slot == datetime(2026, 7, 14, 1, 0)
    assert midnight_slot == datetime(2026, 7, 14, 16, 2)


@pytest.mark.asyncio
async def test_non_targeted_group_is_rejected_before_delivery_checks(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    campaign = AdCampaign(
        id=91,
        name="specific group only",
        target_group_ids=json.dumps([101]),
    )
    binding = SimpleNamespace(account_id=7)
    membership = SimpleNamespace(
        telegram_group_id=-100123,
        group=SimpleNamespace(id=202, status="active", level=SimpleNamespace(value="A")),
    )

    monkeypatch.setattr(service, "_ad_recent_inflight_delivery_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_recent_undeliverable_failure_reason", AsyncMock(return_value=None))

    reason = await service._ad_skip_reason(
        binding,
        campaign,
        SimpleNamespace(id=1),
        membership,
    )

    assert reason == "group_not_targeted"


@pytest.mark.asyncio
async def test_scheduled_time_is_enforced_when_dynamic_capacity_is_enabled(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    campaign = AdCampaign(
        id=92,
        name="capacity still respects schedule",
        send_mode=AdSendMode.SCHEDULED.value,
        scheduled_times=json.dumps(["09:00"]),
        target_group_levels=json.dumps(["A"]),
    )
    binding = SimpleNamespace(account_id=7)
    membership = SimpleNamespace(
        telegram_group_id=-100456,
        group=SimpleNamespace(id=303, status="active", level=SimpleNamespace(value="A")),
    )

    monkeypatch.setattr(automation_module, "_now", lambda: datetime(2026, 7, 14, 5, 0))
    monkeypatch.setattr(
        automation_module,
        "get_ad_capacity_settings",
        AsyncMock(return_value={"enabled": True, "timezone_offset_hours": 8}),
    )
    monkeypatch.setattr(service, "_ad_recent_inflight_delivery_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_recent_undeliverable_failure_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_ad_warmup_skip_reason", AsyncMock(return_value=None))
    dynamic_limit = AsyncMock(return_value=10)
    monkeypatch.setattr(service, "_ad_dynamic_daily_limit", dynamic_limit)

    reason = await service._ad_skip_reason(
        binding,
        campaign,
        SimpleNamespace(id=1),
        membership,
    )

    assert reason == "scheduled_time_not_due"
    dynamic_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_interval_is_enforced_when_dynamic_capacity_is_enabled(test_db, monkeypatch):
    now = datetime(2026, 7, 15, 4, 0)
    account = TelegramAccount(
        phone="+15550008801",
        identifier="+15550008801",
        session_name="target_group_interval",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=-10088001, title="Interval target", level=GroupLevel.A, status="active")
    campaign = AdCampaign(
        name="ten minute target",
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=10,
        target_group_ids="[]",
        target_group_levels=json.dumps(["A"]),
        max_sends_per_group_per_day=0,
        max_sends_per_account_per_day=0,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status=DeliveryStatus.SUCCESS.value,
            sent_at=now - timedelta(minutes=5),
        )
    )
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    binding = SimpleNamespace(account_id=account.id)
    membership = SimpleNamespace(
        telegram_group_id=group.group_id,
        group=group,
        joined_at=now - timedelta(days=1),
    )
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    monkeypatch.setattr(
        automation_module,
        "get_ad_capacity_settings",
        AsyncMock(return_value={"enabled": True, "timezone_offset_hours": 8}),
    )
    monkeypatch.setattr(service, "_ad_recent_inflight_delivery_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_recent_undeliverable_failure_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_ad_warmup_skip_reason", AsyncMock(return_value=None))

    reason = await service._ad_skip_reason(
        binding,
        campaign,
        SimpleNamespace(id=1),
        membership,
    )

    assert reason == "interval_not_due"
