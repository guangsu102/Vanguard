import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import UniqueConstraint, select
from sqlalchemy.dialects import postgresql

import app.modules.acquisition.automation as automation_module
from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard, RiskBudget
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import (
    AD_DELIVERY_THROTTLE_KEY_PREFIX,
    AcquisitionAutomationService,
)
from app.modules.acquisition.models import (
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdDeliveryPolicy,
    AdDeliveryScheduleState,
    AdScheduleStatus,
    AdSendMode,
    DeliveryStatus,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
)
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements


@pytest.mark.asyncio
async def test_growth_campaign_account_daily_quota_reserves_and_releases(test_db):
    account = TelegramAccount(
        identifier="growth-account-quota",
        session_name="growth-account-quota",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    groups = [
        Group(group_id=951001 + index, title=f"Quota group {index}", level=GroupLevel.A)
        for index in range(2)
    ]
    campaign = AdCampaign(
        name="Account daily quota",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
        max_sends_per_account_per_day=1,
        max_sends_per_group_per_day=10,
    )
    creative = AdCreative(name="Quota creative", content="quota", enabled=True)
    test_db.add_all([account, *groups, campaign, creative])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    first, first_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=groups[0],
        creative=creative,
    )
    assert first is not None
    assert first_reason is None

    blocked, blocked_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=groups[1],
        creative=creative,
    )
    assert blocked is None
    assert blocked_reason == "campaign_account_daily_limit"

    await service._finalize_ad_delivery_log(
        first,
        DeliveryStatus.FAILED,
        error="send failed before Telegram completion",
    )
    released, released_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=groups[1],
        creative=creative,
    )
    assert released is not None
    assert released_reason is None


@pytest.mark.asyncio
async def test_growth_campaign_group_daily_quota_excludes_legacy_unreserved_probe_logs(test_db):
    accounts = [
        TelegramAccount(
            identifier=f"growth-group-quota-{index}",
            session_name=f"growth-group-quota-{index}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        for index in range(2)
    ]
    group = Group(group_id=951010, title="Group daily quota", level=GroupLevel.A)
    campaign = AdCampaign(
        name="Group daily quota",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
        max_sends_per_account_per_day=10,
        max_sends_per_group_per_day=1,
    )
    creative = AdCreative(name="Group quota creative", content="quota", enabled=True)
    test_db.add_all([*accounts, group, campaign, creative])
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=accounts[0].id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            creative_id=None,
            status=DeliveryStatus.PENDING.value,
        )
    )
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    first, first_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=accounts[0].id,
        group=group,
        creative=creative,
    )
    assert first is not None
    assert first_reason is None

    blocked, blocked_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=accounts[1].id,
        group=group,
        creative=creative,
    )
    assert blocked is None
    assert blocked_reason == "campaign_group_daily_limit"


@pytest.mark.asyncio
async def test_growth_campaign_daily_quota_reserves_pending_probe_without_creative(test_db):
    account = TelegramAccount(
        identifier="growth-pending-probe-quota",
        session_name="growth-pending-probe-quota",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    other_account = TelegramAccount(
        identifier="growth-pending-probe-group-quota",
        session_name="growth-pending-probe-group-quota",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    groups = [
        Group(group_id=951015 + index, title=f"Pending probe group {index}", level=GroupLevel.A)
        for index in range(2)
    ]
    campaign = AdCampaign(
        name="Pending probe daily quota",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
        max_sends_per_account_per_day=1,
        max_sends_per_group_per_day=1,
    )
    creative = AdCreative(name="Post-probe creative", content="quota", enabled=True)
    test_db.add_all([account, other_account, *groups, campaign, creative])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    probe, probe_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=groups[0],
        creative=None,
    )

    assert probe is not None
    assert probe_reason is None
    assert probe.creative_id is None
    assert probe.reservation_token
    assert probe.status == DeliveryStatus.PENDING.value

    blocked, blocked_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=groups[1],
        creative=creative,
    )
    assert blocked is None
    assert blocked_reason == "campaign_account_daily_limit"

    group_blocked, group_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=other_account.id,
        group=groups[0],
        creative=creative,
    )
    assert group_blocked is None
    assert group_reason == "campaign_group_daily_limit"

    await service._finalize_ad_delivery_log(
        probe,
        DeliveryStatus.SKIPPED,
        error="probe deferred before Telegram send",
    )
    released, released_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=groups[1],
        creative=creative,
    )
    assert released is not None
    assert released_reason is None


@pytest.mark.asyncio
async def test_growth_campaign_daily_quota_counts_successful_probe_logs(test_db):
    accounts = [
        TelegramAccount(
            identifier=f"growth-successful-probe-quota-{index}",
            session_name=f"growth-successful-probe-quota-{index}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        for index in range(2)
    ]
    groups = [
        Group(
            group_id=951020 + index,
            title=f"Successful probe quota group {index}",
            level=GroupLevel.A,
        )
        for index in range(2)
    ]
    campaign = AdCampaign(
        name="Successful probe daily quota",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
        max_sends_per_account_per_day=1,
        max_sends_per_group_per_day=1,
    )
    creative = AdCreative(name="Post-probe creative", content="quota", enabled=True)
    test_db.add_all([*accounts, *groups, campaign, creative])
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=accounts[0].id,
            group_id=groups[0].id,
            telegram_group_id=groups[0].group_id,
            ad_campaign_id=campaign.id,
            creative_id=None,
            status=DeliveryStatus.SUCCESS.value,
            sent_at=datetime.utcnow(),
        )
    )
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    account_blocked, account_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=accounts[0].id,
        group=groups[1],
        creative=creative,
    )
    group_blocked, group_reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=accounts[1].id,
        group=groups[0],
        creative=creative,
    )

    assert account_blocked is None
    assert account_reason == "campaign_account_daily_limit"
    assert group_blocked is None
    assert group_reason == "campaign_group_daily_limit"


@pytest.mark.asyncio
async def test_growth_campaign_daily_quota_resets_on_beijing_day(
    test_db,
    monkeypatch,
):
    fixed_now = datetime(2026, 8, 29, 0, 30, 0)
    monkeypatch.setattr(automation_module, "_now", lambda: fixed_now)
    account = TelegramAccount(
        identifier="growth-day-rollover",
        session_name="growth-day-rollover",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=951020, title="Rollover quota", level=GroupLevel.A)
    campaign = AdCampaign(
        name="Rollover quota",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
        max_sends_per_account_per_day=1,
        max_sends_per_group_per_day=1,
    )
    creative = AdCreative(name="Rollover creative", content="quota", enabled=True)
    test_db.add_all([account, group, campaign, creative])
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            creative_id=creative.id,
            status=DeliveryStatus.SUCCESS.value,
            sent_at=datetime(2026, 8, 28, 15, 59, 0),
        )
    )
    await test_db.commit()

    reserved, reason = await AcquisitionAutomationService(
        test_db
    )._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=group,
        creative=creative,
    )

    assert reserved is not None
    assert reason is None


class AtomicBudgetRedis:
    def __init__(self):
        self.values: dict[str, int | str] = {}
        self.lock = asyncio.Lock()

    async def eval(self, _script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        action_limit, outbound_limit, cooldown_seconds, now, _ttl = (int(value) for value in argv)
        async with self.lock:
            action_count = int(self.values.get(keys[0], 0))
            outbound_count = int(self.values.get(keys[1], 0))
            cooldown_until = int(self.values.get(keys[2], 0))
            if cooldown_until > now:
                return [0, 1, cooldown_until - now]
            if action_limit > 0 and action_count >= action_limit:
                return [0, 2, 0]
            if outbound_limit > 0 and outbound_count >= outbound_limit:
                return [0, 3, 0]
            if action_limit > 0:
                self.values[keys[0]] = action_count + 1
            if outbound_limit > 0:
                self.values[keys[1]] = outbound_count + 1
            if cooldown_seconds > 0:
                self.values[keys[2]] = now + cooldown_seconds
            return [1, 0, 0]


class AtomicBudgetCache:
    def __init__(self):
        self.client = AtomicBudgetRedis()


class WorkerLockRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, _script, numkeys, *args):
        assert numkeys == 1
        key, token = args
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


@pytest.mark.asyncio
async def test_atomic_outbound_reservation_allows_exactly_three_of_twenty(
    test_db,
    monkeypatch,
):
    cache = AtomicBudgetCache()
    guard = AccountRiskGuard(test_db, cache=cache)
    monkeypatch.setattr(
        guard,
        "_outbound_message_hard_cap",
        AsyncMock(return_value=3),
    )

    async def reserve():
        return await guard._reserve_budget(
            71,
            AccountRiskAction.AD_DELIVERY,
            RiskBudget(daily_limit=999, cooldown_seconds=0),
            {"account_outbound_message_hard_cap_default": 30},
        )

    results = await asyncio.gather(*(reserve() for _ in range(20)))

    assert sum(1 for allowed, _, _ in results if allowed) == 3
    assert (
        sum(
            1
            for allowed, reason, _ in results
            if not allowed and reason == "account_outbound_message_hard_cap"
        )
        == 17
    )
    outbound_values = [
        int(value)
        for key, value in cache.client.values.items()
        if ":daily:outbound_message:" in key
    ]
    assert outbound_values == [3]


@pytest.mark.asyncio
async def test_cooldown_precheck_does_not_consume_action_or_outbound_count(
    test_db,
    monkeypatch,
):
    cache = AtomicBudgetCache()
    guard = AccountRiskGuard(test_db, cache=cache)
    monkeypatch.setattr(
        guard,
        "_outbound_message_hard_cap",
        AsyncMock(return_value=3),
    )
    cooldown_key = "risk:account:72:cooldown:group_message"
    cache.client.values[cooldown_key] = int(datetime.utcnow().timestamp()) + 600

    allowed, reason, _ = await guard._reserve_budget(
        72,
        AccountRiskAction.GROUP_MESSAGE,
        RiskBudget(daily_limit=4, cooldown_seconds=3600),
        {"account_outbound_message_hard_cap_default": 30},
    )

    assert allowed is False
    assert reason == "group_message_cooldown"
    assert all(":daily:" not in key for key in cache.client.values)


@pytest.mark.asyncio
async def test_account_worker_lock_is_released_only_by_matching_token(test_db):
    service = AcquisitionAutomationService(test_db)
    redis = WorkerLockRedis()
    service._new_ad_delivery_redis_client = AsyncMock(return_value=redis)
    service._close_ad_delivery_redis_client = AsyncMock()

    token = await service._claim_ad_account_worker_lock(73, lease_seconds=300)
    assert token is not None
    key = f"{AD_DELIVERY_THROTTLE_KEY_PREFIX}:73:worker_lock"

    redis.values[key] = "new-owner-token"
    await service._release_ad_account_worker_lock(73, token)
    assert redis.values[key] == "new-owner-token"

    await service._release_ad_account_worker_lock(73, "new-owner-token")
    assert key not in redis.values


@pytest.mark.asyncio
async def test_growth_group_cooldown_is_global_but_ad_only_uses_campaign_frequency(
    test_db,
    monkeypatch,
):
    now = datetime(2026, 8, 26, 5, 0, 0)
    account = TelegramAccount(
        identifier="growth-cooldown-account",
        session_name="growth-cooldown-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    other_account = TelegramAccount(
        identifier="other-growth-account",
        session_name="other-growth-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(
        account=account,
        operation_mode=AccountOperationMode.GROWTH.value,
        enabled=True,
        auto_ads_enabled=True,
    )
    group = Group(
        group_id=940001,
        title="Global cooldown group",
        level=GroupLevel.A,
        status="active",
    )
    growth_campaign = AdCampaign(
        name="Growth cooldown campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
        send_mode=AdSendMode.INTERVAL.value,
        target_group_levels=json.dumps(["A"]),
        interval_minutes=5,
    )
    other_campaign = AdCampaign(
        name="Other growth campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
    )
    test_db.add_all([account, other_account, config, group, growth_campaign, other_campaign])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
        warmup_status="ad_eligible",
        probe_status="success",
        ad_status="active",
        first_ad_allowed_at=now - timedelta(days=2),
        ad_eligible_after=now - timedelta(days=1),
    )
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=100,
        ad_tier=GroupAdTier.STABLE.value,
    )
    prior_delivery = AdDeliveryLog(
        account_id=other_account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=other_campaign.id,
        status=DeliveryStatus.SUCCESS.value,
        sent_at=now - timedelta(hours=1),
    )
    test_db.add_all([membership, profile, prior_delivery])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    monkeypatch.setattr(
        automation_module,
        "get_ad_capacity_settings",
        AsyncMock(
            return_value={
                "enabled": True,
                "timezone_offset_hours": 8,
                "window_start_hour": 9,
                "window_end_hour": 2,
            }
        ),
    )
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_execution_settings",
        AsyncMock(return_value={"growth_group_global_cooldown_seconds": 86400}),
    )
    service._ad_recent_undeliverable_failure_reason = AsyncMock(return_value=None)
    service._ad_account_risk_skip_reason = AsyncMock(return_value=None)
    service._group_can_receive_ads = AsyncMock(return_value=True)
    service._ad_warmup_skip_reason = AsyncMock(return_value=None)
    service._ad_account_throttle_skip_reason = AsyncMock(return_value=None)

    growth_binding = SimpleNamespace(
        account_id=account.id,
        campaign=growth_campaign,
    )
    assert (
        await service._ad_skip_reason(
            growth_binding,
            growth_campaign,
            None,
            membership,
        )
        == "growth_group_global_cooldown"
    )

    prior_delivery.status = DeliveryStatus.PENDING.value
    prior_delivery.sent_at = None
    await test_db.commit()
    assert (
        await service._ad_skip_reason(
            growth_binding,
            growth_campaign,
            None,
            membership,
        )
        == "group_delivery_inflight"
    )

    ad_only_campaign = AdCampaign(
        name="Dedicated frequency campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        target_group_ids=json.dumps([group.id]),
        interval_minutes=30,
    )
    test_db.add(ad_only_campaign)
    config.operation_mode = AccountOperationMode.AD_ONLY.value
    group.ad_delivery_account_id = account.id
    membership.join_method = "manual_link_join"
    await test_db.commit()

    ad_only_binding = SimpleNamespace(
        account_id=account.id,
        campaign=ad_only_campaign,
    )
    assert (
        await service._ad_skip_reason(
            ad_only_binding,
            ad_only_campaign,
            None,
            membership,
        )
        is None
    )
    service._ad_account_throttle_skip_reason.assert_not_awaited()


@pytest.mark.asyncio
async def test_ad_only_next_due_uses_exact_campaign_interval(test_db, monkeypatch):
    now = datetime(2026, 8, 26, 5, 0, 0)
    campaign = AdCampaign(
        name="Exact interval campaign",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=37,
    )
    service = AcquisitionAutomationService(test_db)
    throttle = AsyncMock()
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_throttle_settings",
        throttle,
    )

    due_at = await service._next_ad_schedule_due_at(campaign, now)

    assert due_at == now + timedelta(minutes=37)
    throttle.assert_not_awaited()


@pytest.mark.asyncio
async def test_ad_only_group_mute_uses_configured_backoff_sequence(
    test_db,
    monkeypatch,
):
    now = datetime(2026, 8, 29, 1, 0, 0)
    account = TelegramAccount(
        identifier="ad-only-muted-account",
        session_name="ad-only-muted-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940012,
        title="Temporarily muted group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Muted group campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=30,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    state = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        next_due_at=now,
        status=AdScheduleStatus.SENDING.value,
        lock_token="mute-0",
    )
    test_db.add(state)
    await test_db.commit()
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    service = AcquisitionAutomationService(test_db)
    error = f"{automation_module.AD_GROUP_CONTROL_ERROR_PREFIX}ChatWriteForbiddenError"

    expected_backoffs = (40, 160, 640, 320, 160)
    for attempt, expected_minutes in enumerate(expected_backoffs, start=1):
        test_db.add(
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.FAILED.value,
                error=error,
            )
        )
        state.status = AdScheduleStatus.SENDING.value
        state.lock_token = f"mute-{attempt}"
        await test_db.commit()

        await service._finish_ad_schedule_state(
            state.id,
            state.lock_token,
            campaign=campaign,
            succeeded=False,
            reason=error,
            completed_at=now,
        )

        await test_db.refresh(state)
        assert state.status == AdScheduleStatus.RETRY.value
        assert state.next_due_at == now + timedelta(minutes=expected_minutes)
        assert state.last_reason.startswith(f"ad_only_group_control_backoff:{expected_minutes}m:")

    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status=DeliveryStatus.FAILED.value,
            error=error,
        )
    )
    state.status = AdScheduleStatus.SENDING.value
    state.lock_token = "mute-6"
    await test_db.commit()

    await service._finish_ad_schedule_state(
        state.id,
        state.lock_token,
        campaign=campaign,
        succeeded=False,
        reason=error,
        completed_at=now,
    )

    await test_db.refresh(state)
    assert state.status == AdScheduleStatus.PAUSED.value
    assert state.next_due_at == now
    assert state.last_reason.startswith(
        f"{automation_module.AD_ONLY_GROUP_CONTROL_PAUSED_REASON_PREFIX}:6:"
    )

    sibling_campaign = AdCampaign(
        name="Muted group sibling campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=30,
    )
    test_db.add(sibling_campaign)
    await test_db.commit()
    state_id, token, reason = await service._claim_ad_schedule_state(
        campaign=sibling_campaign,
        account_id=account.id,
        membership=SimpleNamespace(
            group_id=group.id,
            telegram_group_id=group.group_id,
        ),
        lease_seconds=60,
    )
    assert state_id == state.id
    assert token is None
    assert reason == state.last_reason


@pytest.mark.asyncio
async def test_ad_only_group_mute_manual_resume_resets_failure_chain(
    test_db,
    monkeypatch,
):
    now = datetime(2026, 8, 29, 2, 30, 0)
    account = TelegramAccount(
        identifier="ad-only-manual-resume",
        session_name="ad-only-manual-resume",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940015,
        title="Manually reopened group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Manual resume campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=30,
    )
    sibling_campaign = AdCampaign(
        name="Manual resume sibling campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=30,
    )
    test_db.add_all([account, group, campaign, sibling_campaign])
    await test_db.flush()
    error = f"{automation_module.AD_GROUP_CONTROL_ERROR_PREFIX}ChatWriteForbiddenError"
    for _ in range(6):
        test_db.add(
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.FAILED.value,
                error=error,
            )
        )
    state = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        next_due_at=now,
        status=AdScheduleStatus.PAUSED.value,
        last_reason=(f"{automation_module.AD_ONLY_GROUP_CONTROL_PAUSED_REASON_PREFIX}:6:{error}"),
    )
    sibling_state = AdDeliveryScheduleState(
        campaign_id=sibling_campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        next_due_at=now,
        status=AdScheduleStatus.PAUSED.value,
        last_reason=(f"{automation_module.AD_ONLY_GROUP_CONTROL_PAUSED_REASON_PREFIX}:6:{error}"),
    )
    test_db.add_all([state, sibling_state])
    await test_db.commit()
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    service = AcquisitionAutomationService(test_db)

    resumed = await service.resume_ad_only_group_control_schedule(
        state.id,
        resumed_by_user_id=42,
    )

    assert resumed.status == AdScheduleStatus.IDLE.value
    assert resumed.next_due_at == now
    assert resumed.last_reason == automation_module.AD_ONLY_GROUP_CONTROL_MANUAL_RESUME_REASON
    await test_db.refresh(sibling_state)
    assert sibling_state.status == AdScheduleStatus.IDLE.value
    assert sibling_state.last_reason == automation_module.AD_ONLY_GROUP_CONTROL_MANUAL_RESUME_REASON
    latest_log = (
        await test_db.execute(
            select(AdDeliveryLog)
            .where(
                AdDeliveryLog.account_id == account.id,
                AdDeliveryLog.group_id == group.id,
                AdDeliveryLog.ad_campaign_id == campaign.id,
            )
            .order_by(AdDeliveryLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert latest_log.status == DeliveryStatus.SKIPPED.value
    assert latest_log.error == (
        f"{automation_module.AD_ONLY_GROUP_CONTROL_MANUAL_RESUME_REASON}:user:42"
    )


@pytest.mark.asyncio
async def test_paused_ad_delivery_api_lists_and_resumes_schedule(
    test_db,
    client,
    monkeypatch,
):
    from app.core.security import get_current_user
    from app.main import app

    now = datetime(2026, 8, 29, 3, 0, 0)
    account = TelegramAccount(
        identifier="ad-only-paused-api",
        session_name="ad-only-paused-api",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940016,
        title="Paused API group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Paused API campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    state = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        next_due_at=now,
        status=AdScheduleStatus.PAUSED.value,
        last_reason=(
            f"{automation_module.AD_ONLY_GROUP_CONTROL_PAUSED_REASON_PREFIX}:"
            "6:group_control:ChatWriteForbiddenError"
        ),
        updated_at=now,
    )
    test_db.add(state)
    await test_db.commit()
    monkeypatch.setattr(automation_module, "_now", lambda: now)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 77,
        "username": "admin",
        "role": "admin",
    }
    try:
        listed = await client.get(
            "/api/automation/ads/paused-deliveries",
            params={"account_id": account.id},
        )
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["total"] == 1
        assert payload["data"][0]["id"] == state.id
        assert payload["data"][0]["group_title"] == group.title
        assert payload["data"][0]["backoff_count"] == 5

        resumed = await client.post(f"/api/automation/ads/paused-deliveries/{state.id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["data"]["status"] == AdScheduleStatus.IDLE.value

        listed_after = await client.get(
            "/api/automation/ads/paused-deliveries",
            params={"account_id": account.id},
        )
        assert listed_after.status_code == 200
        assert listed_after.json()["total"] == 0
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_ad_only_group_mute_backoff_resets_after_success(test_db):
    now = datetime(2026, 8, 29, 2, 0, 0)
    account = TelegramAccount(
        identifier="ad-only-mute-reset",
        session_name="ad-only-mute-reset",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940013,
        title="Reopened group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Reopened group campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        send_mode=AdSendMode.INTERVAL.value,
        interval_minutes=30,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    error = f"{automation_module.AD_GROUP_CONTROL_ERROR_PREFIX}ChatWriteForbiddenError"
    test_db.add_all(
        [
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.FAILED.value,
                error=error,
            ),
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.SUCCESS.value,
                sent_at=now - timedelta(minutes=1),
            ),
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.FAILED.value,
                error=error,
            ),
        ]
    )
    state = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        next_due_at=now,
        status=AdScheduleStatus.SENDING.value,
        lock_token="mute-reset",
    )
    test_db.add(state)
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)

    await service._finish_ad_schedule_state(
        state.id,
        state.lock_token,
        campaign=campaign,
        succeeded=False,
        reason=error,
        completed_at=now,
    )

    await test_db.refresh(state)
    assert state.next_due_at == now + timedelta(minutes=40)


@pytest.mark.asyncio
async def test_ad_only_group_mute_is_retryable_but_permanent_loss_is_blocked(
    test_db,
):
    now = datetime.utcnow()
    account = TelegramAccount(
        identifier="ad-only-mute-filter",
        session_name="ad-only-mute-filter",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940014,
        title="Mute filter group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Mute filter campaign",
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status=DeliveryStatus.FAILED.value,
            error=(f"{automation_module.AD_GROUP_CONTROL_ERROR_PREFIX}ChatWriteForbiddenError"),
            created_at=now,
        )
    )
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)

    assert (
        await service._ad_recent_undeliverable_failure_reason(
            account.id,
            campaign.id,
            group.group_id,
            delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        )
        is None
    )
    assert (
        await service._ad_recent_undeliverable_failure_reason(
            account.id,
            campaign.id,
            group.group_id,
            delivery_policy=AdDeliveryPolicy.GROWTH.value,
        )
        == "group_recent_undeliverable_failure"
    )

    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status=DeliveryStatus.FAILED.value,
            error=(f"{automation_module.AD_GROUP_CONTROL_ERROR_PREFIX}UserBannedInChannelError"),
            created_at=now + timedelta(seconds=1),
        )
    )
    await test_db.commit()

    assert (
        await service._ad_recent_undeliverable_failure_reason(
            account.id,
            campaign.id,
            group.group_id,
            delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        )
        == "group_recent_undeliverable_failure"
    )


@pytest.mark.asyncio
async def test_ad_only_temporary_group_mute_never_leaves_membership(test_db):
    account = TelegramAccount(
        identifier="ad-only-no-leave",
        session_name="ad-only-no-leave",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940015,
        title="Muted without leave",
        level=GroupLevel.A,
        status="active",
    )
    membership = GroupAccountMembership(
        group=group,
        account=account,
        telegram_group_id=group.group_id,
        status="joined",
    )
    test_db.add_all([account, group, membership])
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    service._leave_group = AsyncMock()

    await service._handle_group_control_ad_failure(
        account.id,
        group,
        (f"{automation_module.AD_GROUP_CONTROL_ERROR_PREFIX}ChatWriteForbiddenError"),
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
    )

    await test_db.refresh(membership)
    assert membership.status == "joined"
    service._leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_exposure_reconciliation_creates_schedule_and_preserves_live_owners(test_db):
    observed_at = datetime(2026, 8, 30, 8, 0, 0)
    exposure_at = observed_at - timedelta(hours=1)
    next_due_at = exposure_at + timedelta(hours=24)
    account = TelegramAccount(
        identifier="exposure-reconcile-account",
        session_name="exposure-reconcile-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    groups = [
        Group(
            group_id=949100 + index,
            title=f"Exposure reconcile {index}",
            level=GroupLevel.A,
            status="active",
        )
        for index in range(4)
    ]
    campaign = AdCampaign(
        name="Exposure reconciliation campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
    )
    test_db.add_all([account, *groups, campaign])
    await test_db.flush()

    expired = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=groups[1].id,
        telegram_group_id=groups[1].group_id,
        next_due_at=observed_at - timedelta(minutes=1),
        status=AdScheduleStatus.SENDING.value,
        lock_token="expired-owner",
        lease_expires_at=observed_at - timedelta(seconds=1),
    )
    live = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=groups[2].id,
        telegram_group_id=groups[2].group_id,
        next_due_at=observed_at - timedelta(minutes=1),
        status=AdScheduleStatus.SENDING.value,
        lock_token="live-owner",
        lease_expires_at=observed_at + timedelta(minutes=5),
    )
    paused = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=groups[3].id,
        telegram_group_id=groups[3].group_id,
        next_due_at=observed_at - timedelta(minutes=1),
        status=AdScheduleStatus.PAUSED.value,
        last_reason="manual_pause",
    )
    test_db.add_all([expired, live, paused])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    for group in groups:
        assert await service._reconcile_overdue_ad_schedule_after_exposure(
            campaign_id=campaign.id,
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            exposure_at=exposure_at,
            next_due_at=next_due_at,
            observed_at=observed_at,
        )

    created = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == campaign.id,
                AdDeliveryScheduleState.account_id == account.id,
                AdDeliveryScheduleState.group_id == groups[0].id,
            )
        )
    ).scalar_one()
    await test_db.refresh(expired)
    await test_db.refresh(live)
    await test_db.refresh(paused)

    assert created.status == AdScheduleStatus.IDLE.value
    assert created.last_success_at == exposure_at
    assert created.next_due_at == next_due_at
    assert expired.status == AdScheduleStatus.IDLE.value
    assert expired.lock_token is None
    assert expired.lease_expires_at is None
    assert expired.last_success_at == exposure_at
    assert expired.next_due_at == next_due_at
    assert live.status == AdScheduleStatus.SENDING.value
    assert live.lock_token == "live-owner"
    assert live.lease_expires_at == observed_at + timedelta(minutes=5)
    assert live.last_success_at == exposure_at
    assert live.next_due_at == next_due_at
    assert paused.status == AdScheduleStatus.PAUSED.value
    assert paused.last_reason == "manual_pause"
    assert paused.last_success_at == exposure_at
    assert paused.next_due_at == next_due_at


@pytest.mark.asyncio
async def test_tuple_lease_blocks_until_expired_then_growth_success_sets_24h_due(
    test_db,
    monkeypatch,
):
    now = datetime(2026, 8, 26, 5, 0, 0)
    account = TelegramAccount(
        identifier="tuple-lease-account",
        session_name="tuple-lease-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940002,
        title="Tuple lease group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Tuple lease campaign",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
    )
    state = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        next_due_at=now - timedelta(minutes=1),
        status=AdScheduleStatus.SENDING.value,
        lock_token="old-token",
        lease_expires_at=now + timedelta(minutes=5),
    )
    test_db.add_all([membership, state])
    await test_db.commit()

    monkeypatch.setattr(automation_module, "_now", lambda: now)
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_execution_settings",
        AsyncMock(
            return_value={
                "growth_group_global_cooldown_seconds": 86400,
                "dispatcher_interval_seconds": 60,
            }
        ),
    )
    service = AcquisitionAutomationService(test_db)

    state_id, token, reason = await service._claim_ad_schedule_state(
        campaign=campaign,
        account_id=account.id,
        membership=membership,
        lease_seconds=300,
    )
    assert state_id == state.id
    assert token is None
    assert reason == "delivery_tuple_inflight"

    state.lease_expires_at = now - timedelta(seconds=1)
    await test_db.commit()
    state_id, token, reason = await service._claim_ad_schedule_state(
        campaign=campaign,
        account_id=account.id,
        membership=membership,
        lease_seconds=300,
    )
    assert token is not None
    assert reason is None

    await service._finish_ad_schedule_state(
        state_id,
        token,
        campaign=campaign,
        succeeded=True,
        reason=None,
        completed_at=now,
    )
    await test_db.refresh(state)
    assert state.status == AdScheduleStatus.IDLE.value
    assert state.next_due_at == now + timedelta(hours=24)
    assert state.lock_token is None
    assert state.lease_expires_at is None


def test_delivery_schedule_tuple_unique_constraint_exists():
    constraint_names = {
        constraint.name
        for constraint in AdDeliveryScheduleState.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_ad_delivery_schedule_tuple" in constraint_names


def test_delivery_schedule_lock_targets_only_schedule_table_on_postgresql():
    statement = automation_module._ad_schedule_state_for_update_query().where(
        AdDeliveryScheduleState.id == 1
    )

    sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())

    assert "FOR UPDATE OF ad_delivery_schedule_state" in sql


@pytest.mark.asyncio
async def test_pure_ad_only_worker_does_not_evaluate_growth_health(
    test_db,
    monkeypatch,
):
    campaign = SimpleNamespace(
        id=81,
        delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
        enabled=True,
        status="active",
        start_at=None,
        end_at=None,
    )
    binding = SimpleNamespace(id=82, account_id=83, campaign=campaign)
    service = AcquisitionAutomationService(test_db)
    service._list_enabled_ad_bindings_for_account = AsyncMock(return_value=[binding])
    service._list_joined_groups_for_account = AsyncMock(return_value=[])
    service._growth_ad_health_allowed = AsyncMock(return_value=False)
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_execution_settings",
        AsyncMock(return_value={"job_lease_seconds": 300}),
    )

    result = await service._run_ad_delivery_for_account(
        binding.account_id,
        binding_ids=[binding.id],
        dry_run=False,
        delivery_budget={"remaining": 1},
        delivery_budget_lock=asyncio.Lock(),
        reserved_ad_targets=set(),
        ad_target_lock=asyncio.Lock(),
        max_deliveries_per_account=1,
        stop_after_success=False,
        stop_after_failure=False,
    )

    assert result.processed == 0
    service._growth_ad_health_allowed.assert_not_awaited()


def test_production_ad_delivery_sql_migration_is_registered_and_parseable():
    migration_name = "034_add_ad_delivery_policy_scheduler.sql"
    migrations_dir = Path(__file__).parents[2] / "migrations"
    migration_path = migrations_dir / migration_name

    assert migration_name in DEFAULT_MIGRATIONS
    assert all((migrations_dir / name).is_file() for name in DEFAULT_MIGRATIONS)
    statements = _split_sql_statements(migration_path.read_text(encoding="utf-8"))
    assert len(statements) == 9
    assert any(
        "CREATE TABLE IF NOT EXISTS ad_delivery_schedule_state" in item for item in statements
    )
    assert any("ALTER COLUMN max_messages_per_day DROP NOT NULL" in item for item in statements)


def test_growth_daily_quota_sql_migration_is_registered_and_parseable():
    migration_name = "042_fix_growth_daily_quotas.sql"
    migrations_dir = Path(__file__).parents[2] / "migrations"
    migration_path = migrations_dir / migration_name

    assert migration_name in DEFAULT_MIGRATIONS
    statements = _split_sql_statements(migration_path.read_text(encoding="utf-8"))
    assert any("ADD COLUMN IF NOT EXISTS telegram_action_attempted" in item for item in statements)
    assert any(
        "ALTER COLUMN max_sends_per_account_per_day SET DEFAULT 10" in item for item in statements
    )


def test_join_and_ad_cooldown_sql_migration_is_registered_and_parseable():
    migration_name = "043_normalize_join_and_ad_cooldowns.sql"
    migrations_dir = Path(__file__).parents[2] / "migrations"
    migration_path = migrations_dir / migration_name

    assert migration_name in DEFAULT_MIGRATIONS
    statements = _split_sql_statements(migration_path.read_text(encoding="utf-8"))
    assert any("ALTER COLUMN max_groups_per_day SET DEFAULT 10" in item for item in statements)
    assert any(
        "'automation.account_risk_guard'" in item
        and '"daily_limit":10' in item
        and '"cooldown_seconds":7200' in item
        for item in statements
    )
    assert any(
        "'automation.ad_delivery_execution'" in item
        and "growth_group_global_cooldown_seconds" in item
        and "86400" in item
        for item in statements
    )
