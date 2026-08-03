import hashlib
import json
from datetime import datetime

import pytest
from sqlalchemy import select

import app.modules.guardian.coupon.coupon_distributor as coupon_module
from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignExecution,
    CampaignExecutionStatus,
    CampaignScope,
    CampaignTracking,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.campaign.runner import CampaignRunner
from app.core.user.models import User, UserState
from app.modules.guardian.coupon.coupon_distributor import DistributeResult
from app.modules.guardian.models import CouponDistribution


class FakeSub2APICode:
    code = "TG-SUB2API-10"


class FakeSub2APIClient:
    def __init__(self):
        self.calls = []

    async def generate_redeem_codes(self, **kwargs):
        self.calls.append(kwargs)
        return [FakeSub2APICode()]


@pytest.mark.asyncio
async def test_delayed_registration_creates_pending_execution(test_db):
    user = User(telegram_id=10001, username="alice", state=UserState.PENDING)
    campaign = Campaign(
        name="delayed-registration",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.DELAYED,
        distribution_mode=CampaignDistributionMode.DELAYED,
        broadcast_policy_json=json.dumps({"delay_minutes": 15}),
        eligibility_policy_json=json.dumps({"once_per_user": True}),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(user)
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    results = await runner.trigger_for_registration(user, occurred_at=datetime(2026, 6, 2, 10, 0))

    assert len(results) == 1
    assert results[0].status == CampaignExecutionStatus.PENDING.value

    execution = (
        await test_db.execute(select(CampaignExecution).where(CampaignExecution.campaign_id == campaign.id))
    ).scalar_one()
    assert execution.user_id == user.id
    assert execution.scheduled_at == datetime(2026, 6, 2, 10, 15)
    assert execution.status == CampaignExecutionStatus.PENDING

    tracking = (
        await test_db.execute(select(CampaignTracking).where(CampaignTracking.campaign_name == campaign.name))
    ).scalar_one()
    assert tracking.registered_at == datetime(2026, 6, 2, 10, 0)


@pytest.mark.asyncio
async def test_manual_global_campaign_executes_without_user_id(test_db):
    users = [
        User(telegram_id=20001, username="alice", state=UserState.PENDING),
        User(telegram_id=20002, username="bob", state=UserState.ACTIVE),
    ]
    campaign = Campaign(
        name="manual-global",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.MANUAL,
        distribution_mode=CampaignDistributionMode.WELCOME,
        eligibility_policy_json=json.dumps({"once_per_user": True}),
        enabled=True,
    )
    test_db.add_all([*users, campaign])
    await test_db.commit()
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    result = await runner.trigger_campaign(campaign=campaign, manual=True)

    assert isinstance(result, list)
    assert len(result) == 2
    assert all(item.reward_granted for item in result)

    distributions = (
        await test_db.execute(select(CouponDistribution).where(CouponDistribution.campaign_id == campaign.id))
    ).scalars().all()
    assert len(distributions) == 2


@pytest.mark.asyncio
async def test_process_due_delayed_campaign_executes_pending(test_db):
    user = User(telegram_id=30001, username="carol", state=UserState.PENDING)
    campaign = Campaign(
        name="due-delayed",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.DELAYED,
        distribution_mode=CampaignDistributionMode.DELAYED,
        broadcast_policy_json=json.dumps({"delay_minutes": 5}),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(user)
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    await runner.trigger_campaign(campaign=campaign, user=user, now=datetime(2026, 6, 2, 10, 0))

    result = await runner.process_due_campaigns(now=datetime(2026, 6, 2, 10, 6))

    assert result["processed"] == 1
    assert result["rewarded"] == 1
    execution = (
        await test_db.execute(select(CampaignExecution).where(CampaignExecution.campaign_id == campaign.id))
    ).scalar_one()
    assert execution.status == CampaignExecutionStatus.COMPLETED
    assert execution.executed_at == datetime(2026, 6, 2, 10, 6)


@pytest.mark.asyncio
async def test_scheduled_campaign_records_last_run_outside_policy_json(test_db):
    user = User(telegram_id=40001, username="dave", state=UserState.PENDING)
    campaign = Campaign(
        name="scheduled-global",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.SCHEDULED,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        broadcast_policy_json=json.dumps({"schedule_times": ["10:00"]}),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    first = await runner.process_due_campaigns(now=datetime(2026, 6, 2, 10, 0))
    second = await runner.process_due_campaigns(now=datetime(2026, 6, 2, 10, 0, 30))

    assert first["processed"] == 1
    assert second["skipped"] == 1
    await test_db.refresh(campaign)
    assert "last_run_at" not in json.loads(campaign.broadcast_policy_json)

    schedule_state = (
        await test_db.execute(
            select(CampaignExecution).where(
                CampaignExecution.campaign_id == campaign.id,
                CampaignExecution.user_id.is_(None),
            )
        )
    ).scalar_one()
    assert schedule_state.last_run_at == datetime(2026, 6, 2, 10, 0)


@pytest.mark.asyncio
async def test_timed_worker_ignores_legacy_distribution_mode_for_global_campaigns(test_db):
    user = User(telegram_id=50001, username="erin", state=UserState.PENDING)
    campaign = Campaign(
        name="legacy-scheduled-mode",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.AFTER_REGISTER,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        broadcast_policy_json=json.dumps({"schedule_times": ["10:00"]}),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    result = await runner.process_due_campaigns(now=datetime(2026, 6, 2, 10, 0))

    assert result["processed"] == 0
    assert result["rewarded"] == 0


@pytest.mark.asyncio
async def test_sub2api_coupon_campaign_generates_redeem_code(test_db, monkeypatch):
    fake_client = FakeSub2APIClient()
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ENABLED", True)
    monkeypatch.setattr(coupon_module.settings, "SUB2API_BASE_URL", "https://sub2api.example.com")
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(coupon_module, "get_sub2api_client", lambda **_kwargs: fake_client)

    user = User(telegram_id=60001, username="fred", state=UserState.PENDING)
    campaign = Campaign(
        name="sub2api-coupon",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.IMMEDIATE,
        distribution_mode=CampaignDistributionMode.WELCOME,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_amount": 10,
                "coupon_quantity": 3,
                "coupon_type": "balance",
            }
        ),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(user)
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    result = await runner.trigger_campaign(campaign=campaign, user=user, now=datetime(2026, 6, 2, 10, 0))

    assert result.reward_granted is True
    assert fake_client.calls[0]["count"] == 1
    assert fake_client.calls[0]["code_type"] == "balance"
    assert fake_client.calls[0]["value"] == 10
    assert fake_client.calls[0]["expires_in_days"] == 7
    raw_identity = f"{campaign.id}\x1fuser\x1f{campaign.id}\x1f{user.telegram_id}"
    expected_digest = hashlib.sha256(raw_identity.encode()).hexdigest()
    assert fake_client.calls[0]["idempotency_key"] == (
        f"vanguard-coupon-{campaign.id}-{expected_digest}"
    )

    distribution = (
        await test_db.execute(select(CouponDistribution).where(CouponDistribution.campaign_id == campaign.id))
    ).scalar_one()
    assert distribution.coupon_code == "TG-SUB2API-10"


@pytest.mark.asyncio
async def test_sub2api_coupon_batch_quota_is_enforced(test_db, monkeypatch):
    fake_client = FakeSub2APIClient()
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ENABLED", True)
    monkeypatch.setattr(coupon_module.settings, "SUB2API_BASE_URL", "https://sub2api.example.com")
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(coupon_module, "get_sub2api_client", lambda **_kwargs: fake_client)

    users = [
        User(telegram_id=61001, username="one", state=UserState.PENDING),
        User(telegram_id=61002, username="two", state=UserState.PENDING),
    ]
    campaign = Campaign(
        name="limited-sub2api-coupon",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.MANUAL,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_amount": 5,
                "coupon_quantity": 1,
                "coupon_type": "balance",
                "coupon_batch_key": "中文批次也可用",
            }
        ),
        enabled=True,
    )
    test_db.add_all([*users, campaign])
    await test_db.commit()
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    first = await runner.trigger_campaign(campaign=campaign, user=users[0], manual=True)
    second = await runner.trigger_campaign(campaign=campaign, user=users[1], manual=True)

    assert first.reward_granted is True
    assert second.reward_granted is False
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["idempotency_key"].isascii()
    assert len(fake_client.calls[0]["idempotency_key"]) <= 128


@pytest.mark.asyncio
async def test_global_coupon_message_contains_existing_code_on_retry(test_db, monkeypatch):
    fake_client = FakeSub2APIClient()
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ENABLED", True)
    monkeypatch.setattr(coupon_module.settings, "SUB2API_BASE_URL", "https://sub2api.example.com")
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(coupon_module, "get_sub2api_client", lambda **_kwargs: fake_client)

    user = User(telegram_id=62001, username="retry", state=UserState.PENDING)
    campaign = Campaign(
        name="retry-coupon",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.IMMEDIATE,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_amount": 8,
                "coupon_quantity": 2,
            }
        ),
        broadcast_policy_json=json.dumps({"message": "你的专属码：{coupon_code}"}),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(user)
    await test_db.refresh(campaign)

    runner = CampaignRunner(test_db)
    first = await runner.coupon_distributor.distribute_discount(
        user_id=user.id,
        campaign_id=campaign.id,
        telegram_id=user.telegram_id,
    )
    repeated = await runner.coupon_distributor.distribute_discount(
        user_id=user.id,
        campaign_id=campaign.id,
        telegram_id=user.telegram_id,
    )
    message = runner._resolve_message(campaign, user=user, reward_result=repeated)

    assert first.success is True
    assert repeated.success is True
    assert repeated.coupon_code == "TG-SUB2API-10"
    assert len(fake_client.calls) == 1
    assert message == "你的专属码：TG-SUB2API-10"


def test_global_coupon_message_has_safe_default(test_db):
    runner = CampaignRunner(test_db)
    campaign = Campaign(name="默认发券", validity_hours=48)
    result = DistributeResult(
        success=True,
        coupon_code="SAFE-CODE",
        trial_hours=None,
        traffic_gb=None,
        message="ok",
        batch_key="batch-1",
    )

    message = runner._resolve_message(campaign, reward_result=result)

    assert "SAFE-CODE" in message
    assert "48小时" in message
