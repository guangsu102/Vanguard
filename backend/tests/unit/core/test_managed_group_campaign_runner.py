import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.modules.guardian.coupon.coupon_distributor as coupon_module
from app.api.broadcasts import BroadcastRecord
from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignExecution,
    CampaignExecutionStatus,
    CampaignScope,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.user.models import User
from app.modules.guardian.campaign_runner import ManagedGroupCampaignRunner
from app.modules.guardian.main import GuardianBot
from app.modules.guardian.models import CouponDistribution, GroupCampaignTriggerEvent


class FakeTelegramClient:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, message, parse_mode="Markdown", **_kwargs):
        self.messages.append(
            {
                "chat_id": chat_id,
                "message": message,
                "parse_mode": parse_mode,
                "reply_markup": _kwargs.get("reply_markup"),
            }
        )
        return type("Message", (), {"message_id": 1})()

    async def close(self):
        return None


class FakeSub2APICode:
    def __init__(self, code: str):
        self.code = code


class FakeSub2APIClient:
    def __init__(self):
        self.calls = []

    async def generate_redeem_codes(self, **kwargs):
        self.calls.append(kwargs)
        count = int(kwargs.get("count") or 1)
        call_number = len(self.calls)
        return [FakeSub2APICode(f"CLAIM_CODE_{call_number}_{index}") for index in range(1, count + 1)]


@pytest.mark.asyncio
async def test_managed_group_coupon_code_is_sent_to_group_once_per_batch(test_db, monkeypatch):
    user = User(telegram_id=70001, username="alice")
    campaign = Campaign(
        name="group-coupon-batch",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing=CampaignTriggerTiming.IMMEDIATE,
        trigger_event=GroupCampaignTriggerEvent.USER_JOINED.value,
        target_group_ids=json.dumps([-10070001]),
        bot_account_id=99,
        reward_policy_json=json.dumps({"coupon_batch_key": "batch-a"}),
        broadcast_policy_json=json.dumps({"message": "{user_display} 的专属码：{coupon_code}"}),
        eligibility_policy_json=json.dumps({"once_per_user": True}),
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.refresh(campaign)

    fake_client = FakeTelegramClient()
    runner = ManagedGroupCampaignRunner(test_db)

    async def fake_create_guardian_client(account_id):
        assert account_id == 99
        return fake_client

    monkeypatch.setattr(runner, "_create_guardian_client", fake_create_guardian_client)

    first = await runner.trigger_for_event(
        event=GroupCampaignTriggerEvent.USER_JOINED,
        telegram_group_id=-10070001,
        user_telegram_id=70001,
        username="alice",
    )
    second = await runner.trigger_for_event(
        event=GroupCampaignTriggerEvent.USER_JOINED,
        telegram_group_id=-10070001,
        user_telegram_id=70001,
        username="alice",
    )

    assert first[0].reward_granted is True
    assert first[0].delivered is True
    assert second[0].status == "skipped"
    assert second[0].reason == "eligibility_not_met"
    assert len(fake_client.messages) == 1
    assert fake_client.messages[0]["chat_id"] == -10070001
    assert "@alice 的专属码：DISCOUNT_" in fake_client.messages[0]["message"]

    distributions = (
        await test_db.execute(select(CouponDistribution).where(CouponDistribution.campaign_id == campaign.id))
    ).scalars().all()
    assert len(distributions) == 1
    assert distributions[0].batch_key == "batch-a"


@pytest.mark.asyncio
async def test_scheduled_group_sub2api_coupon_claim_link_is_sent_without_public_codes(test_db, monkeypatch):
    fake_sub2api = FakeSub2APIClient()
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ENABLED", True)
    monkeypatch.setattr(coupon_module.settings, "SUB2API_BASE_URL", "https://sub2api.example.com")
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(coupon_module, "get_sub2api_client", lambda **_kwargs: fake_sub2api)

    campaign = Campaign(
        name="scheduled-group-sub2api",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing=CampaignTriggerTiming.SCHEDULED,
        trigger_event=GroupCampaignTriggerEvent.SCHEDULED.value,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        target_group_ids=json.dumps([-10080001]),
        bot_account_id=88,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_amount": 3,
                "coupon_quantity": 2,
                "coupon_type": "balance",
                "coupon_batch_key": "evening",
            }
        ),
        broadcast_policy_json=json.dumps(
            {
                "schedule_times": ["21:00"],
                "message": "本次兑换码：{coupon_code}\n有效期：{validity_hours}小时",
            }
        ),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    fake_client = FakeTelegramClient()
    runner = ManagedGroupCampaignRunner(test_db)

    async def fake_create_guardian_client(account_id):
        assert account_id == 88
        return fake_client

    monkeypatch.setattr(runner, "_create_guardian_client", fake_create_guardian_client)

    async def fake_get_guardian_bot_username(_account_id):
        return "PipenAIBot"

    monkeypatch.setattr(runner, "_get_guardian_bot_username", fake_get_guardian_bot_username)

    result = await runner.process_scheduled_campaigns(now=datetime(2026, 7, 10, 13, 0))
    repeated = await runner.process_scheduled_campaigns(now=datetime(2026, 7, 10, 13, 0, 30))

    assert result["processed"] == 1
    assert result["broadcasted"] == 1
    assert repeated["skipped"] == 1
    assert len(fake_client.messages) == 1
    assert fake_client.messages[0]["chat_id"] == -10080001
    assert fake_client.messages[0]["parse_mode"] == ""
    assert "{coupon_code}" not in fake_client.messages[0]["message"]
    assert "CLAIM_CODE" not in fake_client.messages[0]["message"]
    assert "https://t.me/PipenAIBot?start=" in fake_client.messages[0]["message"]
    assert "每位用户每个批次仅可领取一次" in fake_client.messages[0]["message"]
    assert fake_client.messages[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "领取优惠券"
    assert fake_sub2api.calls == []

    execution = (
        await test_db.execute(select(CampaignExecution).where(CampaignExecution.campaign_id == campaign.id))
    ).scalar_one()
    assert execution.status == CampaignExecutionStatus.COMPLETED
    assert execution.last_run_at == datetime(2026, 7, 10, 13, 0)

    broadcast = (await test_db.execute(select(BroadcastRecord))).scalar_one()
    assert broadcast.status == "completed"
    assert broadcast.success_count == 1
    assert broadcast.failed_count == 0


@pytest.mark.asyncio
async def test_group_coupon_claim_generates_one_code_once_per_user_batch(test_db, monkeypatch):
    fake_sub2api = FakeSub2APIClient()
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ENABLED", True)
    monkeypatch.setattr(coupon_module.settings, "SUB2API_BASE_URL", "https://sub2api.example.com")
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(coupon_module, "get_sub2api_client", lambda **_kwargs: fake_sub2api)

    campaign = Campaign(
        name="claim-once-group-sub2api",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing=CampaignTriggerTiming.SCHEDULED,
        trigger_event=GroupCampaignTriggerEvent.SCHEDULED.value,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        target_group_ids=json.dumps([-10080001]),
        bot_account_id=88,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_amount": 3,
                "coupon_quantity": 5,
                "coupon_type": "balance",
                "coupon_batch_key": "evening",
            }
        ),
        broadcast_policy_json=json.dumps({"schedule_times": ["21:00"]}),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    execution = CampaignExecution(
        campaign_id=campaign.id,
        status=CampaignExecutionStatus.COMPLETED,
        trigger_timing=CampaignTriggerTiming.SCHEDULED.value,
        trigger_event=GroupCampaignTriggerEvent.SCHEDULED.value,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        scheduled_at=datetime(2026, 7, 10, 13, 0),
        executed_at=datetime(2026, 7, 10, 13, 0, 5),
        last_run_at=datetime(2026, 7, 10, 13, 0, 5),
        delivered=True,
    )
    test_db.add(execution)
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    payload = f"vgc_{campaign.id}_scheduled-202607102100"

    first = await runner.claim_group_coupon(
        payload,
        user_telegram_id=70002,
        username="bob",
        now=datetime(2026, 7, 10, 13, 1),
    )
    second = await runner.claim_group_coupon(
        payload,
        user_telegram_id=70002,
        username="bob",
        now=datetime(2026, 7, 10, 13, 2),
    )

    assert "领取成功" in first
    assert "CLAIM_CODE_1_1" in first
    assert "你已领取过本批次优惠券" in second
    assert "CLAIM_CODE_1_1" in second
    assert len(fake_sub2api.calls) == 1
    assert fake_sub2api.calls[0]["count"] == 1
    raw_identity = (
        f"{campaign.id}\x1fuser\x1fevening:scheduled-202607102100\x1f70002"
    )
    expected_digest = hashlib.sha256(raw_identity.encode()).hexdigest()
    assert fake_sub2api.calls[0]["idempotency_key"] == (
        f"vanguard-coupon-{campaign.id}-{expected_digest}"
    )

    distributions = (
        await test_db.execute(select(CouponDistribution).where(CouponDistribution.campaign_id == campaign.id))
    ).scalars().all()
    assert len(distributions) == 1
    assert distributions[0].coupon_code == "CLAIM_CODE_1_1"
    assert distributions[0].batch_key == "evening:scheduled-202607102100"


@pytest.mark.asyncio
async def test_guardian_private_start_dispatches_coupon_claim_response():
    class FakeClaimRunner:
        def __init__(self):
            self.calls = []

        async def claim_group_coupon(self, payload, *, user_telegram_id, username=None, now=None):
            self.calls.append(
                {
                    "payload": payload,
                    "user_telegram_id": user_telegram_id,
                    "username": username,
                    "now": now,
                }
            )
            return "领取成功\n兑换码：PRIVATE_CODE"

    fake_client = FakeTelegramClient()
    fake_runner = FakeClaimRunner()
    bot = GuardianBot.__new__(GuardianBot)
    bot._telegram_client = fake_client
    bot._context = SimpleNamespace(campaign_runner=fake_runner)
    bot.logger = SimpleNamespace(warning=lambda *_args, **_kwargs: None, error=lambda *_args, **_kwargs: None)

    processed = await bot.handle_message(
        message_id=10,
        chat_id=70003,
        user_id=70003,
        username="carol",
        text="/start vgc_1_manual-2",
    )

    assert processed is True
    assert fake_runner.calls == [
        {
            "payload": "/start vgc_1_manual-2",
            "user_telegram_id": 70003,
            "username": "carol",
            "now": None,
        }
    ]
    assert fake_client.messages[0]["chat_id"] == 70003
    assert fake_client.messages[0]["parse_mode"] == ""
    assert "PRIVATE_CODE" in fake_client.messages[0]["message"]
