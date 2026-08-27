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
    AccountAdBindingCreate,
    AccountAdBindingUpdate,
    AdCampaignCreate,
    _apply_operation_mode_transition_side_effects,
    _validate_ad_only_binding_scope,
    create_account_ad_binding,
    create_account_ad_bindings_batch,
    create_ad_campaign,
    update_account_ad_binding,
)
from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdDeliveryPolicy,
    AdSendMode,
    DeliveryStatus,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
)


@pytest.mark.asyncio
async def test_operation_mode_transition_requires_force_and_disables_incompatible_bindings(
    test_db,
):
    account = TelegramAccount(
        identifier="mode-transition-account",
        session_name="mode-transition-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(
        account=account,
        operation_mode=AccountOperationMode.GROWTH.value,
    )
    campaign = AdCampaign(
        name="growth campaign before role transition",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
    )
    creative = AdCreative(
        name="transition creative",
        content="transition ad",
        enabled=True,
    )
    test_db.add_all([account, config, campaign, creative])
    await test_db.flush()
    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        creative_id=creative.id,
        enabled=True,
    )
    test_db.add(binding)
    await test_db.commit()

    with pytest.raises(
        HTTPException, match="operation_mode_transition_requires_force"
    ):
        await _apply_operation_mode_transition_side_effects(
            config,
            {"operation_mode": AccountOperationMode.AD_ONLY.value},
            test_db,
        )

    assert binding.enabled is True
    await _apply_operation_mode_transition_side_effects(
        config,
        {
            "operation_mode": AccountOperationMode.AD_ONLY.value,
            "force_transition": True,
        },
        test_db,
    )
    assert binding.enabled is False


@pytest.mark.asyncio
async def test_ad_only_account_with_owned_group_cannot_switch_back_to_growth(
    test_db,
):
    account = TelegramAccount(
        identifier="owned-group-ad-only-account",
        session_name="owned-group-ad-only-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(
        account=account,
        operation_mode=AccountOperationMode.AD_ONLY.value,
    )
    test_db.add_all([account, config])
    await test_db.flush()
    group = Group(
        group_id=-100900000777,
        title="Owned ad-only group",
        level=GroupLevel.A,
        ad_delivery_account_id=account.id,
    )
    test_db.add(group)
    await test_db.commit()

    with pytest.raises(
        HTTPException, match="operation_mode_transition_blocked"
    ):
        await _apply_operation_mode_transition_side_effects(
            config,
            {
                "operation_mode": AccountOperationMode.GROWTH.value,
                "force_transition": True,
            },
            test_db,
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


@pytest.mark.asyncio
async def test_binding_create_and_enable_reject_unavailable_accounts(test_db):
    campaign = AdCampaign(name="unavailable account binding")
    creative = AdCreative(name="Unavailable account creative", content="test", enabled=True)
    banned_account = TelegramAccount(
        phone="+15550008801",
        identifier="+15550008801",
        session_name="single_banned_ad_binding",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.BANNED,
        is_active=True,
    )
    errored_account = TelegramAccount(
        phone="+15550008802",
        identifier="+15550008802",
        session_name="single_error_ad_binding",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ERROR,
        is_active=True,
    )
    inactive_account = TelegramAccount(
        phone="+15550008803",
        identifier="+15550008803",
        session_name="single_inactive_ad_binding",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=False,
    )
    test_db.add_all([campaign, creative, banned_account, errored_account, inactive_account])
    await test_db.flush()

    rejected = (
        (banned_account, "Banned account cannot be bound"),
        (errored_account, "Errored account cannot be bound"),
        (inactive_account, "Inactive account cannot be bound"),
    )
    for account, expected_detail in rejected:
        with pytest.raises(HTTPException) as exc:
            await create_account_ad_binding(
                AccountAdBindingCreate(
                    account_id=account.id,
                    ad_campaign_id=campaign.id,
                    creative_id=creative.id,
                ),
                db=test_db,
            )
        assert exc.value.status_code == 409
        assert expected_detail in exc.value.detail

    disabled_binding = AccountAdBinding(
        account_id=banned_account.id,
        ad_campaign_id=campaign.id,
        creative_id=creative.id,
        enabled=False,
    )
    test_db.add(disabled_binding)
    await test_db.flush()

    with pytest.raises(HTTPException) as enable_exc:
        await update_account_ad_binding(
            disabled_binding.id,
            AccountAdBindingUpdate(enabled=True),
            db=test_db,
        )
    assert enable_exc.value.status_code == 409
    assert "Banned account cannot be bound" in enable_exc.value.detail


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
async def test_ad_only_scheduled_time_is_enforced_without_growth_gates(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    campaign = AdCampaign(
        id=92,
        name="ad-only schedule",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.SCHEDULED.value,
        scheduled_times=json.dumps(["09:00"]),
        target_group_ids=json.dumps([303]),
    )
    binding = SimpleNamespace(account_id=7)
    membership = SimpleNamespace(
        telegram_group_id=-100456,
        join_method="manual_link_join",
        ad_pause_until=None,
        ad_status="active",
        group=SimpleNamespace(
            id=303,
            group_id=-100456,
            status="active",
            level=SimpleNamespace(value="A"),
            ad_delivery_account_id=7,
        ),
    )

    monkeypatch.setattr(automation_module, "_now", lambda: datetime(2026, 7, 14, 5, 0))
    monkeypatch.setattr(
        automation_module,
        "get_ad_capacity_settings",
        AsyncMock(return_value={"enabled": True, "timezone_offset_hours": 8}),
    )
    monkeypatch.setattr(service, "_ad_recent_inflight_delivery_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_recent_undeliverable_failure_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "_get_account_operation_config",
        AsyncMock(
            return_value=SimpleNamespace(
                account_id=7,
                operation_mode=AccountOperationMode.AD_ONLY.value,
                enabled=True,
                auto_ads_enabled=True,
                quiet_hours_start=None,
                quiet_hours_end=None,
            )
        ),
    )
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service,
        "_get_or_create_group_ad_profile",
        AsyncMock(
            return_value=SimpleNamespace(
                ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                ad_policy_confidence=100,
                ad_policy_expires_at=None,
                paused_until=None,
                ad_tier=GroupAdTier.STABLE.value,
            )
        ),
    )
    warmup_check = AsyncMock(return_value="warming")
    monkeypatch.setattr(service, "_ad_warmup_skip_reason", warmup_check)
    growth_health = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_growth_ad_health_allowed", growth_health)

    reason = await service._ad_skip_reason(
        binding,
        campaign,
        SimpleNamespace(id=1),
        membership,
    )

    assert reason == "scheduled_time_not_due"
    growth_health.assert_not_awaited()
    warmup_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_required_group_is_blocked_before_writable_probe(test_db, monkeypatch):
    now = datetime(2026, 7, 14, 5, 0)
    group = Group(group_id=-100457, title="Approval only", level=GroupLevel.A, status="active")
    test_db.add(group)
    await test_db.flush()
    test_db.add(
        GroupAdProfile(
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_policy_mode=GroupAdPolicyMode.APPROVAL_REQUIRED.value,
            ad_policy_confidence=100,
            ad_policy_source="manual",
            ad_policy_verified_at=now - timedelta(days=1),
            ad_policy_expires_at=now + timedelta(days=30),
            ad_tier=GroupAdTier.OBSERVING.value,
            daily_capacity=0,
        )
    )
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    campaign = AdCampaign(
        id=93,
        name="approval blocked",
        send_mode=AdSendMode.INTERVAL.value,
        target_group_levels=json.dumps(["A"]),
    )
    binding = SimpleNamespace(account_id=7)
    membership = SimpleNamespace(telegram_group_id=group.group_id, group=group)
    warmup_check = AsyncMock(return_value=None)
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    monkeypatch.setattr(service, "_ad_recent_inflight_delivery_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_recent_undeliverable_failure_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_ad_warmup_skip_reason", warmup_check)

    reason = await service._ad_skip_reason(
        binding,
        campaign,
        SimpleNamespace(id=1),
        membership,
    )

    assert reason == "group_ad_approval_required"
    warmup_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_ad_only_group_interval_is_enforced_without_growth_gates(test_db, monkeypatch):
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
        name="ten minute ad-only target",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=10,
        target_group_ids="[]",
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    campaign.target_group_ids = json.dumps([group.id])
    group.ad_delivery_account_id = account.id
    test_db.add_all(
        [
            GroupAdProfile(
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                ad_policy_confidence=100,
                ad_policy_source="manual",
                ad_policy_verified_at=now - timedelta(days=1),
                ad_policy_expires_at=now + timedelta(days=30),
                ad_tier=GroupAdTier.STABLE.value,
                daily_capacity=0,
            ),
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.SUCCESS.value,
                sent_at=now - timedelta(minutes=5),
            ),
        ]
    )
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    binding = SimpleNamespace(account_id=account.id)
    membership = SimpleNamespace(
        telegram_group_id=group.group_id,
        group=group,
        join_method="manual_link_join",
        joined_at=now - timedelta(days=1),
        ad_pause_until=None,
        ad_status="active",
    )
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    monkeypatch.setattr(
        automation_module,
        "get_ad_capacity_settings",
        AsyncMock(return_value={"enabled": True, "timezone_offset_hours": 8}),
    )
    monkeypatch.setattr(service, "_ad_recent_inflight_delivery_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_recent_undeliverable_failure_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "_get_account_operation_config",
        AsyncMock(
            return_value=SimpleNamespace(
                account_id=account.id,
                operation_mode=AccountOperationMode.AD_ONLY.value,
                enabled=True,
                auto_ads_enabled=True,
                quiet_hours_start=None,
                quiet_hours_end=None,
            )
        ),
    )
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    warmup_check = AsyncMock(return_value="warming")
    monkeypatch.setattr(service, "_ad_warmup_skip_reason", warmup_check)

    reason = await service._ad_skip_reason(
        binding,
        campaign,
        SimpleNamespace(id=1),
        membership,
    )

    assert reason == "interval_not_due"
    warmup_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_handed_over_group_rejects_previous_growth_account(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    campaign = AdCampaign(id=94, name="legacy growth campaign", target_group_ids=json.dumps([404]))
    binding = SimpleNamespace(account_id=7)
    membership = SimpleNamespace(
        telegram_group_id=-100404,
        join_method="keyword_auto_join",
        group=SimpleNamespace(
            id=404,
            status="active",
            level=SimpleNamespace(value="A"),
            ad_delivery_account_id=9,
        ),
    )
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))

    reason = await service._ad_skip_reason(binding, campaign, None, membership)

    assert reason == "group_reserved_for_ad_only"


@pytest.mark.asyncio
async def test_ad_only_account_rejects_level_based_campaign(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    campaign = AdCampaign(
        id=95,
        name="level campaign",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        target_group_ids="[]",
    )
    binding = SimpleNamespace(account_id=9)
    membership = SimpleNamespace(
        telegram_group_id=-100405,
        join_method="manual_link_join",
        group=SimpleNamespace(
            id=405,
            status="active",
            level=SimpleNamespace(value="A"),
            ad_delivery_account_id=9,
        ),
    )
    config = SimpleNamespace(operation_mode=AccountOperationMode.AD_ONLY.value)
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=config))

    reason = await service._ad_skip_reason(binding, campaign, None, membership)

    assert reason == "ad_only_requires_explicit_groups"


@pytest.mark.asyncio
async def test_ad_only_binding_requires_manual_takeover(test_db):
    account = TelegramAccount(
        identifier="manual-binding-account",
        session_name="manual-binding-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=-100406, title="Manual binding group", level=GroupLevel.A)
    campaign = AdCampaign(
        name="manual binding campaign",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=180,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    campaign.target_group_ids = json.dumps([group.id])
    test_db.add_all(
        [
            AccountOperationConfig(
                account_id=account.id,
                operation_mode=AccountOperationMode.AD_ONLY.value,
            ),
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=account.id,
                status="joined",
                join_method="keyword_auto_join",
            ),
        ]
    )
    await test_db.commit()

    with pytest.raises(HTTPException, match="must manually join and own"):
        await _validate_ad_only_binding_scope([account.id], campaign.id, test_db)

    membership = (
        await test_db.execute(
            select(GroupAccountMembership).where(GroupAccountMembership.account_id == account.id)
        )
    ).scalar_one()
    membership.join_method = "manual_link_join"
    group.ad_delivery_account_id = account.id
    test_db.add(
        GroupAdProfile(
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
            ad_policy_confidence=100,
            ad_policy_source="manual",
            ad_policy_verified_at=datetime.utcnow(),
            ad_policy_expires_at=datetime.utcnow() + timedelta(days=30),
            ad_tier=GroupAdTier.STABLE.value,
            daily_capacity=0,
        )
    )
    await test_db.commit()

    await _validate_ad_only_binding_scope([account.id], campaign.id, test_db)
