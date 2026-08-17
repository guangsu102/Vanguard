import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.modules.acquisition.automation as automation_module
from app.api.automation import (
    AccountAdBindingBatchCreate,
    AdCampaignCreate,
    create_account_ad_bindings_batch,
    create_ad_campaign,
)
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdSendMode,
    DeliveryStatus,
)


@pytest.mark.asyncio
async def test_create_scheduled_campaign_normalizes_and_validates_target_groups(test_db):
    first_group = Group(group_id=-100900001, title="First target", level=GroupLevel.A)
    second_group = Group(group_id=-100900002, title="Second target", level=GroupLevel.B)
    account = TelegramAccount(
        phone='+15550009001',
        identifier='+15550009001',
        session_name='target_group_campaign',
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add_all([first_group, second_group, account])
    await test_db.flush()
    test_db.add_all(
        [
            GroupAccountMembership(
                group_id=first_group.id,
                telegram_group_id=first_group.group_id,
                account_id=account.id,
                status='joined',
            ),
            GroupAccountMembership(
                group_id=second_group.id,
                telegram_group_id=second_group.group_id,
                account_id=account.id,
                status='joined',
            ),
        ]
    )
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
async def test_create_campaign_rejects_group_with_only_left_membership(test_db):
    account = TelegramAccount(
        phone='+15550009002',
        identifier='+15550009002',
        session_name='left_target_group',
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=-100900003,
        title='Left target',
        level=GroupLevel.A,
        status='active',
    )
    test_db.add_all([account, group])
    await test_db.flush()
    test_db.add(
        GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status='left',
            left_at=datetime.utcnow(),
        )
    )
    await test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await create_ad_campaign(
            AdCampaignCreate(name='left target campaign', target_group_ids=[group.id]),
            db=test_db,
        )

    assert exc_info.value.status_code == 400
    assert 'not currently joined' in exc_info.value.detail


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


@pytest.mark.asyncio
async def test_batch_binding_supports_multiple_accounts_and_legacy_account_id(test_db):
    accounts = [
        TelegramAccount(
            phone=f"+1555000890{index}",
            identifier=f"+1555000890{index}",
            session_name=f"multi_ad_binding_{index}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        for index in range(1, 4)
    ]
    campaign = AdCampaign(name="multi-account ad binding")
    creatives = [
        AdCreative(name="Multi creative 1", content="first", enabled=True),
        AdCreative(name="Multi creative 2", content="second", enabled=True),
    ]
    test_db.add_all([*accounts, campaign, *creatives])
    await test_db.flush()

    response = await create_account_ad_bindings_batch(
        AccountAdBindingBatchCreate(
            account_ids=[accounts[0].id, accounts[1].id, accounts[0].id],
            ad_campaign_id=campaign.id,
            creative_ids=[creatives[0].id, creatives[1].id, creatives[0].id],
            priority=7,
        ),
        db=test_db,
    )

    assert len(response["data"]) == 4
    assert {
        (item["account_id"], item["creative_id"]) for item in response["data"]
    } == {
        (accounts[0].id, creatives[0].id),
        (accounts[0].id, creatives[1].id),
        (accounts[1].id, creatives[0].id),
        (accounts[1].id, creatives[1].id),
    }
    assert {item["priority"] for item in response["data"]} == {7}

    duplicate_response = await create_account_ad_bindings_batch(
        AccountAdBindingBatchCreate(
            account_ids=[accounts[0].id, accounts[1].id],
            ad_campaign_id=campaign.id,
            creative_ids=[creatives[0].id, creatives[1].id],
        ),
        db=test_db,
    )
    assert duplicate_response["data"] == []

    legacy_response = await create_account_ad_bindings_batch(
        AccountAdBindingBatchCreate(
            account_id=accounts[2].id,
            ad_campaign_id=campaign.id,
            creative_ids=[creatives[0].id, creatives[1].id],
        ),
        db=test_db,
    )
    assert len(legacy_response["data"]) == 2

    banned_account = TelegramAccount(
        phone="+1555000899",
        identifier="+1555000899",
        session_name="banned_ad_binding",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.BANNED,
        is_active=False,
    )
    test_db.add(banned_account)
    await test_db.flush()
    with pytest.raises(HTTPException) as banned_exc:
        await create_account_ad_bindings_batch(
            AccountAdBindingBatchCreate(
                account_id=banned_account.id,
                ad_campaign_id=campaign.id,
                creative_ids=[creatives[0].id],
            ),
            db=test_db,
        )
    assert banned_exc.value.status_code == 409
    assert "Banned account cannot be bound" in banned_exc.value.detail

    binding_rows = (
        await test_db.execute(
            select(AccountAdBinding).where(AccountAdBinding.ad_campaign_id == campaign.id)
        )
    ).scalars().all()
    assert len(binding_rows) == 6
    assert {binding.account_id for binding in binding_rows} == {account.id for account in accounts}


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
