import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.modules.guardian.campaign_runner as campaign_runner_module
import app.modules.guardian.coupon.coupon_distributor as coupon_module
from app.api.broadcasts import BroadcastRecord
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
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
from app.core.group.models import Group
from app.core.user.models import User
from app.modules.guardian.campaign_runner import ManagedGroupCampaignRunner
from app.modules.guardian.main import GuardianBot
from app.modules.guardian.models import (
    CouponDistribution,
    GroupCampaignTriggerEvent,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedBotProfile


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


async def _seed_campaign_binding(test_db, *, chat_id: int, owned: bool = False):
    suffix = str(abs(chat_id))
    bot = TelegramAccount(
        identifier=f"campaign-bot-{suffix}",
        session_name=f"campaign-bot-{suffix}",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    records = [bot]
    owner = None
    if owned:
        owner = TelegramAccount(
            identifier=f"campaign-owner-{suffix}",
            session_name=f"campaign-owner-{suffix}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        records.append(owner)
    test_db.add_all(records)
    await test_db.flush()
    if owned:
        test_db.add_all(
            [
                GuardianBotProfile(
                    account_id=bot.id,
                    bot_token=f"123456:{suffix}-campaign-token",
                    bot_user_id=881001,
                    enabled=True,
                ),
                OwnedBotProfile(
                    owner_account_id=owner.id,
                    account_id=bot.id,
                    token_ciphertext="encrypted-campaign-token",
                    bot_user_id=881001,
                    status="verified",
                    enabled=True,
                ),
            ]
        )

    group = Group(group_id=chat_id, title=f"Campaign target {suffix}")
    test_db.add(group)
    await test_db.flush()
    binding = ManagedGroupBinding(
        group_id=group.id,
        telegram_group_id=chat_id,
        bot_account_id=bot.id,
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
    )
    test_db.add(binding)
    await test_db.flush()

    asset = None
    if owned:
        asset = OwnedGroupAsset(
            internal_name=f"campaign-owned-{suffix}",
            title=f"Campaign target {suffix}",
            owner_account_id=owner.id,
            status="ready",
            telegram_chat_id=chat_id,
            core_group_id=group.id,
            managed_binding_id=binding.id,
            guardian_bot_account_id=bot.id,
            governance_status="managed",
        )
        test_db.add(asset)
    await test_db.commit()
    return asset, group, binding, bot


@pytest.mark.asyncio
async def test_managed_group_coupon_code_is_sent_to_group_once_per_batch(test_db, monkeypatch):
    _asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db, chat_id=-10070001
    )
    user = User(telegram_id=70001, username="alice")
    campaign = Campaign(
        name="group-coupon-batch",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing=CampaignTriggerTiming.IMMEDIATE,
        trigger_event=GroupCampaignTriggerEvent.USER_JOINED.value,
        target_group_ids=json.dumps([-10070001]),
        bot_account_id=bot.id,
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
        assert account_id == bot.id
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
    _asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db, chat_id=-10080001
    )
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
        bot_account_id=bot.id,
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
        assert account_id == bot.id
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
    _asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10080001,
    )
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
        bot_account_id=bot.id,
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
async def test_queued_owned_campaign_stop_blocks_tracking_and_reward(
    test_db, monkeypatch
):
    asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081009,
        owned=True,
    )
    user = User(telegram_id=81009, username="queued-member")
    campaign = Campaign(
        name="queued-owned-stop",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing=CampaignTriggerTiming.DELAYED,
        trigger_event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
        distribution_mode=CampaignDistributionMode.DELAYED,
        target_group_ids=json.dumps([asset.telegram_chat_id]),
        bot_account_id=bot.id,
        reward_policy_json=json.dumps({"coupon_batch_key": "queued-stop"}),
        enabled=True,
    )
    execution = CampaignExecution(
        campaign=campaign,
        user=user,
        group_id=asset.telegram_chat_id,
        status=CampaignExecutionStatus.PENDING,
        trigger_timing=CampaignTriggerTiming.DELAYED.value,
        trigger_event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
        distribution_mode=CampaignDistributionMode.DELAYED,
        scheduled_at=datetime(2026, 9, 10, 12, 0),
    )
    test_db.add_all([user, campaign, execution])
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    create_tracking = AsyncMock()
    grant_reward = AsyncMock()
    monkeypatch.setattr(runner, "_get_or_create_tracking", create_tracking)
    monkeypatch.setattr(runner, "_grant_reward", grant_reward)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value="governance_stop_enabled"),
    )

    result = await runner._execute_campaign(
        campaign=campaign,
        event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
        telegram_group_id=asset.telegram_chat_id,
        user=user,
        username=user.username,
        metadata={},
        execution=execution,
    )

    assert result.status == CampaignExecutionStatus.SKIPPED.value
    assert result.reason == "governance_target_not_allowed"
    create_tracking.assert_not_awaited()
    grant_reward.assert_not_awaited()
    assert await test_db.scalar(select(CampaignTracking)) is None
    assert await test_db.scalar(select(CouponDistribution)) is None


@pytest.mark.asyncio
async def test_owned_stop_blocks_coupon_batch_before_generation(
    test_db, monkeypatch
):
    asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081010,
        owned=True,
    )
    campaign = Campaign(
        name="owned-public-codes-stop",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        distribution_mode=CampaignDistributionMode.MANUAL,
        target_group_ids=json.dumps([asset.telegram_chat_id]),
        bot_account_id=bot.id,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_delivery_mode": "public_codes",
                "coupon_quantity": 2,
            }
        ),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    generate_batch = AsyncMock()
    broadcast = AsyncMock()
    monkeypatch.setattr(
        runner.coupon_distributor,
        "generate_discount_batch",
        generate_batch,
    )
    monkeypatch.setattr(runner, "_broadcast_to_groups", broadcast)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value="governance_stop_enabled"),
    )

    delivered = await runner._broadcast_campaign_to_groups(
        campaign=campaign,
        telegram_group_ids=[asset.telegram_chat_id],
        run_at=datetime(2026, 9, 10, 12, 0),
        batch_context="manual-stop",
    )

    assert delivered is False
    generate_batch.assert_not_awaited()
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_public_batch_uses_only_currently_eligible_targets(
    test_db,
    monkeypatch,
):
    owned_asset, _owned_group, _owned_binding, _owned_bot = (
        await _seed_campaign_binding(test_db, chat_id=-10081016, owned=True)
    )
    _legacy_asset, _legacy_group, _legacy_binding, _legacy_bot = (
        await _seed_campaign_binding(test_db, chat_id=-10081017)
    )
    campaign = Campaign(
        name="mixed-public-batch",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        distribution_mode=CampaignDistributionMode.MANUAL,
        target_group_ids=json.dumps([-10081016, -10081017]),
        bot_account_id=None,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_delivery_mode": "public_codes",
            }
        ),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    generate_batch = AsyncMock(
        return_value=coupon_module.CouponBatchResult(
            success=True,
            coupon_codes=["ONLY_LEGACY_CODE"],
            message="ok",
            batch_key="eligible-only",
        )
    )
    broadcast = AsyncMock(return_value=True)
    monkeypatch.setattr(
        runner.coupon_distributor,
        "generate_discount_batch",
        generate_batch,
    )
    monkeypatch.setattr(runner, "_broadcast_to_groups", broadcast)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value="governance_stop_enabled"),
    )

    delivered = await runner._broadcast_campaign_to_groups(
        campaign=campaign,
        telegram_group_ids=[owned_asset.telegram_chat_id, -10081017],
        run_at=datetime(2026, 9, 10, 12, 0),
        batch_context="manual-mixed",
    )

    assert delivered is True
    generate_batch.assert_awaited_once()
    assert broadcast.await_args.kwargs["telegram_group_ids"] == [-10081017]


@pytest.mark.asyncio
async def test_campaign_without_managed_binding_never_falls_back_to_campaign_bot(
    test_db, monkeypatch
):
    campaign = Campaign(
        name="unbound-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081001]),
        bot_account_id=81001,
        broadcast_policy_json=json.dumps({"message": "must not send"}),
        enabled=True,
    )
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock()
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081001],
    )

    assert delivered is False
    create_client.assert_not_awaited()
    record = (await test_db.execute(select(BroadcastRecord))).scalar_one()
    assert record.success_count == 0
    assert record.failed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate_reason",
    [
        "governance_feature_disabled",
        "governance_gate_backend_unavailable",
        "governance_stop_enabled",
    ],
)
async def test_owned_campaign_gate_failure_skips_without_creating_client(
    test_db, monkeypatch, gate_reason
):
    _asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081002,
        owned=True,
    )
    campaign = Campaign(
        name="owned-gated-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081002]),
        bot_account_id=bot.id,
        broadcast_policy_json=json.dumps({"message": "must not send"}),
        enabled=True,
    )
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock()
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=gate_reason),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081002],
    )

    assert delivered is False
    create_client.assert_not_awaited()
    record = (await test_db.execute(select(BroadcastRecord))).scalar_one()
    assert record.failed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_field", ["asset_core_link", "binding_status"])
async def test_owned_campaign_requires_complete_active_identity_links(
    test_db, monkeypatch, broken_field
):
    asset, _group, binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081003,
        owned=True,
    )
    if broken_field == "asset_core_link":
        asset.core_group_id = None
    else:
        binding.binding_status = ManagedGroupBindingStatus.DEGRADED
    await test_db.commit()

    campaign = Campaign(
        name="owned-broken-link-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081003]),
        bot_account_id=bot.id,
        broadcast_policy_json=json.dumps({"message": "must not send"}),
        enabled=True,
    )
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock()
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081003],
    )

    assert delivered is False
    create_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_gate_stop_does_not_block_legacy_managed_campaign(
    test_db, monkeypatch
):
    _asset, _group, _binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081004,
        owned=False,
    )
    campaign = Campaign(
        name="legacy-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081004]),
        bot_account_id=bot.id,
        broadcast_policy_json=json.dumps({"message": "legacy still sends"}),
        enabled=True,
    )
    fake_client = FakeTelegramClient()
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock(return_value=fake_client)
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value="governance_stop_enabled"),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081004],
    )

    assert delivered is True
    create_client.assert_awaited_once_with(bot.id)
    assert [message["chat_id"] for message in fake_client.messages] == [-10081004]


@pytest.mark.asyncio
async def test_mixed_campaign_skips_stopped_owned_target_only(test_db, monkeypatch):
    _asset, _owned_group, _owned_binding, owned_bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081007,
        owned=True,
    )
    _legacy_asset, _legacy_group, _legacy_binding, legacy_bot = (
        await _seed_campaign_binding(
            test_db,
            chat_id=-10081008,
            owned=False,
        )
    )
    campaign = Campaign(
        name="mixed-owned-legacy-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081007, -10081008]),
        bot_account_id=None,
        broadcast_policy_json=json.dumps({"message": "only legacy sends"}),
        enabled=True,
    )
    fake_client = FakeTelegramClient()
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock(return_value=fake_client)
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value="governance_stop_enabled"),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081007, -10081008],
    )

    assert delivered is True
    create_client.assert_awaited_once_with(legacy_bot.id)
    assert create_client.await_args.args != (owned_bot.id,)
    assert [message["chat_id"] for message in fake_client.messages] == [-10081008]
    record = (await test_db.execute(select(BroadcastRecord))).scalar_one()
    assert record.success_count == 1
    assert record.failed_count == 1


@pytest.mark.asyncio
async def test_valid_owned_campaign_uses_bound_guardian_bot(test_db, monkeypatch):
    asset, group, binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081006,
        owned=True,
    )
    campaign = Campaign(
        name="valid-owned-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081006]),
        bot_account_id=bot.id,
        broadcast_policy_json=json.dumps({"message": "owned sends safely"}),
        enabled=True,
    )
    fake_client = FakeTelegramClient()
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock(return_value=fake_client)
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081006],
    )

    assert asset.core_group_id == group.id
    assert asset.managed_binding_id == binding.id
    assert asset.guardian_bot_account_id == bot.id
    assert delivered is True
    create_client.assert_awaited_once_with(bot.id)
    assert [message["chat_id"] for message in fake_client.messages] == [-10081006]


@pytest.mark.asyncio
async def test_campaign_bot_must_match_managed_binding(test_db, monkeypatch):
    _asset, _group, _binding, _bound_bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081005,
    )
    other_bot = TelegramAccount(
        identifier="campaign-other-bot-81005",
        session_name="campaign-other-bot-81005",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add(other_bot)
    await test_db.commit()
    campaign = Campaign(
        name="mismatched-bot-campaign",
        campaign_type=CampaignType.PROMO,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        target_group_ids=json.dumps([-10081005]),
        bot_account_id=other_bot.id,
        broadcast_policy_json=json.dumps({"message": "must not send"}),
        enabled=True,
    )
    runner = ManagedGroupCampaignRunner(test_db)
    create_client = AsyncMock()
    monkeypatch.setattr(runner, "_create_guardian_client", create_client)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )

    delivered = await runner._broadcast_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081005],
    )

    assert delivered is False
    create_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_legacy_binding_blocks_tracking_and_reward(test_db, monkeypatch):
    _asset, _group, binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081011,
    )
    user = User(telegram_id=81011, username="deleted-binding-member")
    campaign = Campaign(
        name="deleted-binding-side-effects",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing=CampaignTriggerTiming.IMMEDIATE,
        trigger_event=GroupCampaignTriggerEvent.USER_JOINED.value,
        target_group_ids=json.dumps([-10081011]),
        bot_account_id=bot.id,
        enabled=True,
    )
    test_db.add_all([user, campaign])
    await test_db.commit()
    await test_db.delete(binding)
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    create_tracking = AsyncMock()
    grant_reward = AsyncMock()
    monkeypatch.setattr(runner, "_is_user_eligible", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_get_or_create_tracking", create_tracking)
    monkeypatch.setattr(runner, "_grant_reward", grant_reward)

    result = await runner._execute_campaign(
        campaign=campaign,
        event=GroupCampaignTriggerEvent.USER_JOINED,
        telegram_group_id=-10081011,
        user=user,
        username=user.username,
        metadata={},
    )

    assert result.status == CampaignExecutionStatus.SKIPPED.value
    assert result.reason == "governance_target_not_allowed"
    create_tracking.assert_not_awaited()
    grant_reward.assert_not_awaited()
    assert await test_db.scalar(select(CampaignTracking)) is None
    assert await test_db.scalar(select(CouponDistribution)) is None


@pytest.mark.asyncio
async def test_deleted_legacy_binding_blocks_public_batch_generation(
    test_db,
    monkeypatch,
):
    _asset, _group, binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081012,
    )
    campaign = Campaign(
        name="deleted-binding-public-batch",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        distribution_mode=CampaignDistributionMode.MANUAL,
        target_group_ids=json.dumps([-10081012]),
        bot_account_id=bot.id,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_delivery_mode": "public_codes",
                "coupon_quantity": 2,
            }
        ),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.delete(binding)
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    generate_batch = AsyncMock()
    monkeypatch.setattr(
        runner.coupon_distributor,
        "generate_discount_batch",
        generate_batch,
    )

    delivered = await runner._broadcast_campaign_to_groups(
        campaign=campaign,
        telegram_group_ids=[-10081012],
        run_at=datetime(2026, 9, 10, 12, 0),
        batch_context="manual-deleted-binding",
    )

    assert delivered is False
    generate_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_claim_link_is_target_bound_in_mixed_owned_legacy_campaign(
    test_db,
    monkeypatch,
):
    _owned_asset, _owned_group, owned_binding, owned_bot = (
        await _seed_campaign_binding(
            test_db,
            chat_id=-10081013,
            owned=True,
        )
    )
    _legacy_asset, _legacy_group, legacy_binding, legacy_bot = (
        await _seed_campaign_binding(
            test_db,
            chat_id=-10081014,
        )
    )
    _legacy_asset_2, _legacy_group_2, legacy_binding_2, legacy_bot_2 = (
        await _seed_campaign_binding(
            test_db,
            chat_id=-10081018,
        )
    )
    fake_sub2api = FakeSub2APIClient()
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ENABLED", True)
    monkeypatch.setattr(
        coupon_module.settings,
        "SUB2API_BASE_URL",
        "https://sub2api.example.com",
    )
    monkeypatch.setattr(coupon_module.settings, "SUB2API_ADMIN_API_KEY", "admin-test")
    monkeypatch.setattr(
        coupon_module,
        "get_sub2api_client",
        lambda **_kwargs: fake_sub2api,
    )

    campaign = Campaign(
        name="mixed-target-bound-claim",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        distribution_mode=CampaignDistributionMode.MANUAL,
        target_group_ids=json.dumps([-10081013, -10081014, -10081018]),
        bot_account_id=None,
        reward_policy_json=json.dumps(
            {
                "coupon_provider": "sub2api",
                "coupon_delivery_mode": "claim_link",
                "coupon_batch_key": "mixed-claim",
                "coupon_amount": 3,
            }
        ),
        broadcast_policy_json=json.dumps({"message": "领取：{claim_url}"}),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    clients: dict[int, FakeTelegramClient] = {}
    runner = ManagedGroupCampaignRunner(test_db)

    async def fake_create_guardian_client(account_id):
        clients.setdefault(account_id, FakeTelegramClient())
        return clients[account_id]

    async def fake_get_guardian_bot_username(account_id):
        return f"CampaignBot{account_id}"

    monkeypatch.setattr(runner, "_create_guardian_client", fake_create_guardian_client)
    monkeypatch.setattr(runner, "_get_guardian_bot_username", fake_get_guardian_bot_username)
    monkeypatch.setattr(
        campaign_runner_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value="governance_stop_enabled"),
    )

    result = await runner.trigger_manual_broadcast(campaign)

    assert result.delivered is True
    assert owned_bot.id not in clients
    assert {legacy_bot.id, legacy_bot_2.id}.issubset(clients)
    assert [
        message["chat_id"] for message in clients[legacy_bot.id].messages
    ] == [-10081014]
    assert [
        message["chat_id"] for message in clients[legacy_bot_2.id].messages
    ] == [-10081018]
    claim_url = clients[legacy_bot.id].messages[0]["reply_markup"][
        "inline_keyboard"
    ][0][0]["url"]
    claim_url_2 = clients[legacy_bot_2.id].messages[0]["reply_markup"][
        "inline_keyboard"
    ][0][0]["url"]
    assert claim_url != claim_url_2
    payload = claim_url.split("start=", 1)[1]
    payload_2 = claim_url_2.split("start=", 1)[1]
    assert len(payload) <= 64
    parsed = runner._parse_claim_payload(payload)
    assert parsed is not None
    assert parsed.telegram_group_id == -10081014
    assert parsed.managed_binding_id == legacy_binding.id
    assert parsed.managed_binding_id != owned_binding.id
    parsed_2 = runner._parse_claim_payload(payload_2)
    assert parsed_2 is not None
    assert parsed_2.telegram_group_id == -10081018
    assert parsed_2.managed_binding_id == legacy_binding_2.id

    old_ambiguous = f"vgc_{campaign.id}_manual-{result.execution_id}"
    rejected = await runner.claim_group_coupon(
        old_ambiguous,
        user_telegram_id=81014,
    )
    assert rejected == "活动当前不可领取，请稍后再试。"
    assert fake_sub2api.calls == []

    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    assert (
        await runner.claim_group_coupon(tampered, user_telegram_id=81014)
        is None
    )
    assert fake_sub2api.calls == []

    claimed = await runner.claim_group_coupon(
        payload,
        user_telegram_id=81014,
        username="legacy-member",
    )
    assert "领取成功" in claimed
    assert len(fake_sub2api.calls) == 1
    distribution = await test_db.scalar(
        select(CouponDistribution).where(
            CouponDistribution.campaign_id == campaign.id
        )
    )
    assert distribution is not None
    assert distribution.batch_key == (
        f"mixed-claim:manual-{result.execution_id}"
    )

    repeated_from_other_group = await runner.claim_group_coupon(
        payload_2,
        user_telegram_id=81014,
        username="legacy-member",
    )
    assert "你已领取过本批次优惠券" in repeated_from_other_group
    assert len(fake_sub2api.calls) == 1


@pytest.mark.asyncio
async def test_signed_claim_rejects_binding_deleted_after_link_creation(
    test_db,
    monkeypatch,
):
    _asset, _group, binding, bot = await _seed_campaign_binding(
        test_db,
        chat_id=-10081015,
    )
    campaign = Campaign(
        name="claim-binding-deleted",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_event=GroupCampaignTriggerEvent.SCHEDULED.value,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        target_group_ids=json.dumps([-10081015]),
        bot_account_id=bot.id,
        reward_policy_json=json.dumps({"coupon_provider": "sub2api"}),
        broadcast_policy_json=json.dumps({"schedule_times": ["21:00"]}),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)
    execution = CampaignExecution(
        campaign_id=campaign.id,
        status=CampaignExecutionStatus.COMPLETED,
        trigger_event=GroupCampaignTriggerEvent.SCHEDULED.value,
        distribution_mode=CampaignDistributionMode.SCHEDULED,
        last_run_at=datetime(2026, 7, 10, 13, 0),
        delivered=True,
    )
    test_db.add(execution)
    await test_db.commit()

    runner = ManagedGroupCampaignRunner(test_db)
    payload = runner._build_claim_payload(
        campaign.id,
        "scheduled-202607102100",
        telegram_group_id=-10081015,
        managed_binding_id=binding.id,
    )
    await test_db.delete(binding)
    await test_db.commit()

    rejected = await runner.claim_group_coupon(
        payload,
        user_telegram_id=81015,
        now=datetime(2026, 7, 10, 13, 1),
    )

    assert rejected == "活动当前不可领取，请稍后再试。"
    assert await test_db.scalar(select(CouponDistribution)) is None
    assert await test_db.scalar(select(User).where(User.telegram_id == 81015)) is None


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
