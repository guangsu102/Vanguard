import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import UniqueConstraint

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
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements

from app.modules.acquisition.models import (
    AdCampaign,
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


class AtomicBudgetRedis:
    def __init__(self):
        self.values: dict[str, int | str] = {}
        self.lock = asyncio.Lock()

    async def eval(self, _script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        action_limit, outbound_limit, cooldown_seconds, now, _ttl = (
            int(value) for value in argv
        )
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
    assert sum(
        1
        for allowed, reason, _ in results
        if not allowed and reason == "account_outbound_message_hard_cap"
    ) == 17
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
    test_db.add_all(
        [account, other_account, config, group, growth_campaign, other_campaign]
    )
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
    service._ad_recent_inflight_delivery_reason = AsyncMock(return_value=None)
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
    service._list_enabled_ad_bindings_for_account = AsyncMock(
        return_value=[binding]
    )
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
    migration_path = Path(__file__).parents[2] / "migrations" / migration_name

    assert migration_name in DEFAULT_MIGRATIONS
    statements = _split_sql_statements(migration_path.read_text(encoding="utf-8"))
    assert len(statements) == 9
    assert any("CREATE TABLE IF NOT EXISTS ad_delivery_schedule_state" in item for item in statements)
    assert any("ALTER COLUMN max_messages_per_day DROP NOT NULL" in item for item in statements)
