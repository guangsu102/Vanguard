from datetime import datetime, timedelta

import pytest

from app.api.stats import (
    _beijing_day_bounds,
    get_dashboard,
    get_stats_funnel,
    get_stats_sources,
)
from app.core.account.models import (
    AccountRiskDailyStat,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group, GroupLevel
from app.core.user.models import User, UserState
from app.modules.acquisition.models import (
    AcquisitionMessage,
    AcquisitionTracking,
    AdCampaign,
    AdDeliveryLog,
    DeliveryStatus,
    MessageType,
)


@pytest.mark.asyncio
async def test_funnel_uses_acquisition_tracking(test_db):
    now = datetime.utcnow()
    test_db.add_all(
        [
            AcquisitionTracking(
                tracking_code="stats_ref_1",
                source_type="telegram_group",
                click_at=now,
                registered_at=now,
                converted_at=now,
                converted=True,
                created_at=now,
            ),
            AcquisitionTracking(
                tracking_code="stats_ref_2",
                source_type="private_reply",
                click_at=now,
                created_at=now,
            ),
        ]
    )
    await test_db.commit()

    response = await get_stats_funnel(
        start_date=(now - timedelta(minutes=1)).isoformat(),
        end_date=(now + timedelta(minutes=1)).isoformat(),
        db=test_db,
    )

    by_stage = {item["stage"]: item["count"] for item in response["data"]}
    assert by_stage == {
        "Tracking": 2,
        "Clicked": 2,
        "Registered": 1,
        "Activated": 1,
    }


@pytest.mark.asyncio
async def test_sources_use_acquisition_source_type(test_db):
    now = datetime.utcnow()
    test_db.add_all(
        [
            AcquisitionTracking(tracking_code="source_ref_1", source_type="telegram_group", created_at=now),
            AcquisitionTracking(tracking_code="source_ref_2", source_type="telegram_group", created_at=now),
            AcquisitionTracking(tracking_code="source_ref_3", source_type="private_reply", created_at=now),
        ]
    )
    await test_db.commit()

    response = await get_stats_sources(
        start_date=(now - timedelta(minutes=1)).isoformat(),
        end_date=(now + timedelta(minutes=1)).isoformat(),
        db=test_db,
    )

    by_source = {item["source"]: item["count"] for item in response["data"]}
    assert by_source["telegram_group"] == 2
    assert by_source["private_reply"] == 1


@pytest.mark.asyncio
async def test_dashboard_contract_and_daily_messages_use_real_delivery_records(test_db):
    day_start, _ = _beijing_day_bounds()
    sent_at = day_start + timedelta(hours=1)
    account = TelegramAccount(
        identifier="stats-dashboard-account",
        session_name="stats-dashboard-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940001,
        title="Dashboard group",
        member_count=321,
        level=GroupLevel.A,
        status="active",
    )
    user = User(
        telegram_id=940001,
        state=UserState.ACTIVE,
        created_at=sent_at,
        updated_at=sent_at,
    )
    campaign = AdCampaign(
        name="Dashboard delivery",
        enabled=True,
        status="active",
        delivery_policy="growth",
    )
    tracking = AcquisitionTracking(
        tracking_code="dashboard_tracking",
        source_type="telegram_group",
        registered_at=sent_at,
        converted_at=sent_at,
        converted=True,
        created_at=sent_at,
    )
    test_db.add_all([account, group, user, campaign, tracking])
    await test_db.flush()
    test_db.add_all(
        [
            AcquisitionMessage(
                account_id=account.id,
                group_id=group.group_id,
                content="normal outgoing message",
                message_type=MessageType.INTERACTION.value,
                message_id=1001,
                sent_at=sent_at,
            ),
            AcquisitionMessage(
                account_id=account.id,
                group_id=group.group_id,
                core_group_id=group.id,
                content="recorded probe",
                message_type=MessageType.INTERACTION.value,
                message_id=1003,
                message_purpose="ad_probe",
                content_category="community",
                sent_at=sent_at,
            ),
            AccountRiskDailyStat(
                account_id=account.id,
                stat_date=(sent_at + timedelta(hours=8)).date(),
                action="ad_probe",
                status="success",
                target_type="group",
                count=3,
                first_seen_at=sent_at,
                last_seen_at=sent_at,
            ),
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.SUCCESS.value,
                telegram_message_id=1002,
                sent_at=sent_at,
                created_at=sent_at,
            ),
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.FAILED.value,
                sent_at=sent_at,
                created_at=sent_at,
            ),
        ]
    )
    await test_db.commit()

    response = await get_dashboard(test_db)
    data = response["data"]

    assert data["totalAccounts"] == 1
    assert data["onlineAccounts"] == 1
    assert data["totalGroups"] == 1
    assert data["totalUsers"] == 1
    assert data["activeUsers"] == 1
    assert data["dailyRegistered"] == 1
    assert data["dailyConverted"] == 1
    assert data["dailyMessages"] == 5
    assert data["conversionRate"] == 100.0
    assert len(data["weeklyTrend"]) == 7
    assert data["accountDistribution"] == [{"status": "online", "count": 1}]
    assert data["topGroups"] == [
        {"id": group.id, "title": group.title, "memberCount": 321}
    ]
    assert "total_accounts" not in data
    assert "daily_messages" not in data
