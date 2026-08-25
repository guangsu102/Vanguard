from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.account.models import (
    AccountOperationConfig,
    AccountRiskDailyStat,
    AccountRiskEvent,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard, RiskBudget
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.models import AccountAdBinding, AdCampaign


@pytest.mark.asyncio
async def test_manual_ban_account_sets_banned_state_and_audits(test_db):
    account = TelegramAccount(
        identifier="manual-ban-account",
        session_name="manual-ban-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    operation_config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_join_enabled=True,
        auto_ads_enabled=True,
    )
    campaign = AdCampaign(name="Manual Ban Campaign", enabled=True, status="active")
    test_db.add_all([operation_config, campaign])
    await test_db.flush()
    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        enabled=True,
    )
    test_db.add(binding)
    await test_db.commit()

    guard = AccountRiskGuard(test_db)
    banned = await guard.manual_ban_account(
        account.id,
        reason="production_account_banned",
        operator="admin",
    )

    assert banned.status == AccountStatus.BANNED
    assert banned.is_active is False
    assert banned.risk_score == 100.0
    assert banned.risk_level == "quarantined"
    assert banned.risk_reason == "account_banned"

    await test_db.refresh(operation_config)
    await test_db.refresh(binding)
    assert operation_config.enabled is False
    assert operation_config.auto_join_enabled is False
    assert operation_config.auto_ads_enabled is False
    assert binding.enabled is False

    event = (await test_db.execute(select(AccountRiskEvent))).scalars().one()
    assert event.status == "quarantine"
    assert event.reason == "account_banned"
    assert "production_account_banned" in (event.details or "")


@pytest.mark.asyncio
async def test_risk_guard_blocks_paused_account(test_db):
    account = TelegramAccount(
        phone="+15559990001",
        identifier="+15559990001",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_paused_session",
        status=AccountStatus.ONLINE,
        risk_pause_until=datetime.utcnow() + timedelta(minutes=5),
        risk_reason="flood_wait",
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(
        account_id=account.id,
        fingerprint_id="fp-test",
        proxy_mode="dynamic",
        static_proxy_id=None,
        current_proxy_country="US",
        country_code="US",
    )
    guard = AccountRiskGuard(test_db)

    decision = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.PRIVATE_MESSAGE,
        target_type="user",
        target_id=123,
    )

    assert decision.allowed is False
    assert decision.reason == "flood_wait"
    assert decision.retry_after_seconds is not None

    events = (await test_db.execute(select(AccountRiskEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].status == "block"
    assert events[0].action == AccountRiskAction.PRIVATE_MESSAGE.value


@pytest.mark.asyncio
async def test_risk_guard_blocks_and_audits_without_redis(test_db):
    account = TelegramAccount(
        phone="+15559990002",
        identifier="+15559990002",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_allowed_session",
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=20),
        managed_started_at=datetime.utcnow() - timedelta(days=20),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)

    decision = await guard.check_and_reserve(wrapper, AccountRiskAction.JOIN, target_type="group", target_id="demo")

    assert decision.allowed is False
    assert decision.reason == "risk_budget_unavailable"
    events = (await test_db.execute(select(AccountRiskEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].status == "block"
    assert events[0].reason == "risk_budget_unavailable"


@pytest.mark.asyncio
async def test_internal_policy_block_does_not_increase_telegram_risk(test_db):
    account = TelegramAccount(
        phone="+15559990052",
        identifier="+15559990052",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_internal_block_session",
        status=AccountStatus.ONLINE,
        risk_score=20.0,
        risk_level="watch",
        risk_reason="group_write_forbidden",
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    guard = AccountRiskGuard(test_db)
    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    await guard.record_event(
        wrapper,
        AccountRiskAction.GROUP_MESSAGE,
        "block",
        reason="group_message_cooldown",
        target_type="group",
        target_id=123,
    )
    await test_db.refresh(account)

    assert account.risk_score == 20.0
    assert account.risk_level == "watch"
    assert account.risk_reason == "group_write_forbidden"


@pytest.mark.asyncio
async def test_risk_guard_blocks_ad_delivery_during_managed_warmup(test_db):
    account = TelegramAccount(
        phone="+15559990049",
        identifier="+15559990049",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_warmup_session",
        status=AccountStatus.ONLINE,
        managed_started_at=datetime.utcnow(),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)

    decision = await guard.check_and_reserve(wrapper, AccountRiskAction.AD_DELIVERY, target_type="group", target_id="demo")

    assert decision.allowed is False
    assert decision.reason == "account_warmup_observe_ad_delivery_blocked"
    await test_db.refresh(account)
    assert account.warmup_stage == "observe"


@pytest.mark.asyncio
async def test_risk_guard_freezes_on_flood_wait(test_db):
    account = TelegramAccount(
        phone="+15559990003",
        identifier="+15559990003",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_flood_session",
        status=AccountStatus.ONLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)

    await guard.record_failure(wrapper, AccountRiskAction.JOIN, "Flood wait of 120 seconds", target_type="group", target_id="demo")
    await test_db.refresh(account)

    assert account.risk_pause_until is not None
    assert account.risk_reason == "flood_wait"
    assert account.risk_score == 15.0
    assert account.risk_level == "frozen"

    events = (await test_db.execute(select(AccountRiskEvent).order_by(AccountRiskEvent.id))).scalars().all()
    assert [event.status for event in events] == ["failure", "freeze"]


@pytest.mark.asyncio
async def test_risk_guard_freezes_immediately_on_peer_flood(test_db):
    account = TelegramAccount(
        phone="+15559990037",
        identifier="+15559990037",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_peer_flood_session",
        status=AccountStatus.ONLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)

    await guard.record_failure(wrapper, AccountRiskAction.JOIN, "PEER_FLOOD", target_type="group", target_id="demo")
    await test_db.refresh(account)

    assert account.risk_pause_until is not None
    assert account.risk_pause_until > datetime.utcnow() + timedelta(hours=23)
    assert account.risk_reason == "peer_flood"
    assert account.risk_score == 35.0
    assert account.risk_level == "frozen"

    decision = await guard.check_and_reserve(wrapper, AccountRiskAction.JOIN, target_type="group", target_id="demo")
    assert decision.allowed is False
    assert decision.reason == "peer_flood"


@pytest.mark.asyncio
async def test_risk_guard_recovers_after_pause_and_decays_score(test_db):
    account = TelegramAccount(
        phone="+15559990033",
        identifier="+15559990033",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_recovery_session",
        status=AccountStatus.ONLINE,
        risk_score=76.0,
        risk_level="frozen",
        risk_pause_until=datetime.utcnow() - timedelta(minutes=1),
        last_risk_event_at=datetime.utcnow() - timedelta(days=3),
        last_risk_decay_at=datetime.utcnow() - timedelta(days=3),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    guard = AccountRiskGuard(test_db)
    await guard.decay_risk_scores(now=datetime.utcnow())
    await test_db.refresh(account)

    assert account.risk_pause_until is None
    assert account.risk_recovery_until is not None
    assert account.risk_score < 70
    assert account.risk_level in {"watch", "limited"}


@pytest.mark.asyncio
async def test_risk_failure_resets_decay_baseline(test_db):
    stale_decay = datetime.utcnow() - timedelta(days=10)
    account = TelegramAccount(
        phone="+15559990038",
        identifier="+15559990038",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_decay_baseline_session",
        status=AccountStatus.ONLINE,
        risk_score=10.0,
        risk_level="normal",
        last_risk_event_at=stale_decay,
        last_risk_decay_at=stale_decay,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    before = datetime.utcnow()
    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)
    await guard.record_failure(
        wrapper,
        AccountRiskAction.JOIN,
        "flood_wait",
        target_type="group",
        target_id="demo",
    )
    await test_db.refresh(account)

    assert account.last_risk_event_at >= before
    assert account.last_risk_decay_at >= before
    assert account.last_risk_decay_at == account.last_risk_event_at

@pytest.mark.asyncio
async def test_risk_guard_quarantines_banned_account(test_db):
    account = TelegramAccount(
        phone="+15559990034",
        identifier="+15559990034",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_quarantine_session",
        status=AccountStatus.ONLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)
    await guard.record_failure(wrapper, AccountRiskAction.PRIVATE_MESSAGE, "USER_DEACTIVATED_BAN")
    await test_db.refresh(account)

    assert account.risk_score == 100.0
    assert account.risk_level == "quarantined"
    assert account.risk_reason == "account_banned"

    decision = await guard.check_and_reserve(wrapper, AccountRiskAction.PRIVATE_MESSAGE)
    assert decision.allowed is False
    assert decision.reason == "account_risk_quarantined"


@pytest.mark.asyncio
async def test_group_scoped_write_ban_does_not_add_account_risk(test_db):
    account = TelegramAccount(
        phone="+15559990036",
        identifier="+15559990036",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_group_ban_session",
        status=AccountStatus.ONLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)
    await guard.record_failure(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        "CHAT_WRITE_FORBIDDEN",
        target_type="group",
        target_id="@example",
    )
    await test_db.refresh(account)

    assert account.risk_score == 0.0
    assert account.risk_level == "normal"
    assert account.risk_reason is None

    events = (await test_db.execute(select(AccountRiskEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].status == "failure"
    assert events[0].reason == "group_write_forbidden"


@pytest.mark.asyncio
async def test_repeated_group_write_forbidden_triggers_only_same_group_leave(test_db):
    account = TelegramAccount(
        phone="+15559990038",
        identifier="+15559990038",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_repeated_group_ban_session",
        status=AccountStatus.ONLINE,
    )
    other_account = TelegramAccount(
        phone="+15559990048",
        identifier="+15559990048",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_other_group_ban_session",
        status=AccountStatus.ONLINE,
    )
    group = Group(group_id=-100987654321, title="Read Only", level=GroupLevel.C)
    test_db.add_all([account, other_account, group])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=-100987654321,
        account_id=account.id,
        status="joined",
    )
    other_membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=-100987654321,
        account_id=other_account.id,
        status="joined",
    )
    test_db.add_all([membership, other_membership])
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)
    await guard.record_failure(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        "CHAT_WRITE_FORBIDDEN",
        target_type="group",
        target_id="-100987654321",
    )
    assert await guard.should_leave_group_after_write_forbidden(wrapper, 987654321) is False

    await guard.record_failure(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        "CHAT_WRITE_FORBIDDEN",
        target_type="group",
        target_id="987654321",
    )
    assert await guard.should_leave_group_after_write_forbidden(wrapper, -100987654321) is True
    assert await guard.should_leave_group_after_write_forbidden(wrapper, -100111111111) is False
    assert await guard.mark_group_write_forbidden_group_left(wrapper, 987654321) is True

    await test_db.refresh(account)
    await test_db.refresh(membership)
    await test_db.refresh(other_membership)

    assert account.risk_pause_until is None
    assert account.risk_level == "normal"
    assert account.risk_score == 0.0
    assert membership.status == "left"
    assert membership.warmup_status == "blocked"
    assert membership.probe_status == "failed"
    assert membership.ad_status == "blocked"
    assert membership.last_probe_error == "group_write_forbidden"
    assert other_membership.status == "joined"

    events = (await test_db.execute(select(AccountRiskEvent).order_by(AccountRiskEvent.id))).scalars().all()
    assert len(events) == 2
    assert all(event.status == "failure" for event in events)


@pytest.mark.asyncio
async def test_many_group_scoped_write_failures_do_not_freeze_account(test_db):
    account = TelegramAccount(
        phone="+15559990039",
        identifier="+15559990039",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_many_group_ban_session",
        status=AccountStatus.ONLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    for index in range(10):
        await guard.record_failure(
            wrapper,
            AccountRiskAction.GROUP_MESSAGE,
            "CHAT_WRITE_FORBIDDEN",
            target_type="group",
            target_id=f"@restricted_{index}",
        )

    await test_db.refresh(account)

    assert account.risk_score == 0.0
    assert account.risk_level == "normal"
    assert account.risk_reason is None

    decision = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.REACTION,
        target_type="group",
        target_id="@next_group",
    )
    assert decision.allowed is True
    assert decision.reason == "allowed"


@pytest.mark.asyncio
async def test_explicit_platform_group_write_ban_quarantines_account(test_db):
    account = TelegramAccount(
        phone="+15559990040",
        identifier="+15559990040",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_control_probe_session",
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=30),
        managed_started_at=datetime.utcnow() - timedelta(days=30),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    operation_config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_join_enabled=True,
        auto_ads_enabled=True,
    )
    campaign = AdCampaign(name="Platform Ban Campaign", enabled=True, status="active")
    test_db.add_all([operation_config, campaign])
    await test_db.flush()
    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        enabled=True,
    )
    test_db.add(binding)
    await test_db.commit()

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    await guard.record_failure(
        wrapper,
        AccountRiskAction.AD_PROBE,
        "You're banned from sending messages in supergroups/channels",
        target_type="group",
        target_id="@ordinary_business_group",
    )
    await test_db.refresh(account)

    assert account.risk_score == 100.0
    assert account.risk_level == "quarantined"
    assert account.risk_reason == "platform_group_write_banned"

    await test_db.refresh(operation_config)
    await test_db.refresh(binding)
    assert operation_config.enabled is False
    assert operation_config.auto_join_enabled is False
    assert operation_config.auto_ads_enabled is False
    assert binding.enabled is False

    for action in (
        AccountRiskAction.GROUP_MESSAGE,
        AccountRiskAction.AD_PROBE,
        AccountRiskAction.AI_WARMUP,
        AccountRiskAction.AD_DELIVERY,
    ):
        decision = await guard.check_and_reserve(
            wrapper,
            action,
            target_type="group",
            target_id="@next_group",
        )
        assert decision.allowed is False
        assert decision.reason == "platform_group_write_banned"

    for action in (AccountRiskAction.JOIN, AccountRiskAction.SEARCH):
        decision = await guard.check_and_reserve(
            wrapper,
            action,
            target_type="group",
            target_id=f"@{action.value}_target",
        )
        assert decision.allowed is False
        assert decision.reason == "account_risk_quarantined"

    await guard.record_success(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id="@stale_inflight_send",
    )
    await test_db.refresh(account)
    assert account.risk_reason == "platform_group_write_banned"


@pytest.mark.asyncio
async def test_group_send_success_clears_group_derived_freeze(test_db):
    account = TelegramAccount(
        phone="+15559990041",
        identifier="+15559990041",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_group_recovery_session",
        status=AccountStatus.ONLINE,
        risk_score=69.0,
        risk_level="frozen",
        risk_reason="platform_group_write_repeated",
        risk_pause_until=datetime.utcnow() + timedelta(hours=12),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)
    await guard.record_success(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id="@writable_group",
    )
    await test_db.refresh(account)

    assert account.risk_score == 44.0
    assert account.risk_level == "watch"
    assert account.risk_reason == "group_write_capability_confirmed"
    assert account.risk_pause_until is None
    assert account.risk_recovery_until is not None


def test_group_ban_error_is_context_aware():
    error = "You're banned from sending messages in supergroups/channels"

    assert (
        AccountRiskGuard.classify_error(
            error,
            action=AccountRiskAction.AD_DELIVERY,
            target_type="group",
        )
        == "group_write_forbidden"
    )
    assert AccountRiskGuard.classify_error(error, action=AccountRiskAction.PRIVATE_MESSAGE, target_type="user") == "account_banned"


def test_high_risk_telegram_errors_are_account_scoped():
    assert AccountRiskGuard.classify_error("PEER_FLOOD", action=AccountRiskAction.JOIN, target_type="group") == "peer_flood"
    assert (
        AccountRiskGuard.classify_error("USER_RESTRICTED", action=AccountRiskAction.JOIN, target_type="group")
        == "account_restricted"
    )


@pytest.mark.asyncio
async def test_risk_guard_records_daily_stats_for_low_value_events(test_db):
    account = TelegramAccount(
        phone="+15559990035",
        identifier="+15559990035",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_daily_stat_session",
        status=AccountStatus.ONLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)
    await guard.record_success(wrapper, AccountRiskAction.AD_DELIVERY, target_type="group", target_id=1)
    await guard.record_success(wrapper, AccountRiskAction.AD_DELIVERY, target_type="group", target_id=2)

    stats = (await test_db.execute(select(AccountRiskDailyStat))).scalars().all()
    events = (await test_db.execute(select(AccountRiskEvent))).scalars().all()
    assert len(stats) == 1
    assert stats[0].count == 2
    assert events == []


class FakeRedisClient:
    def __init__(self):
        self.values = {}
        self.lists = {}

    async def exists(self, key):
        return 1 if key in self.values else 0

    async def setex(self, key, ttl, value):
        self.values[key] = value
        return True

    async def set(self, key, value):
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def incrby(self, key, amount):
        self.values[key] = int(self.values.get(key, 0)) + amount
        return self.values[key]

    async def expire(self, key, ttl):
        return True

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start : end + 1]
        return True


class FakeCache:
    def __init__(self):
        self.client = FakeRedisClient()

    async def get(self, key):
        return await self.client.get(key)

    async def set(self, key, value, ttl=None):
        if ttl:
            return await self.client.setex(key, ttl, value)
        return await self.client.set(key, value)

    async def exists(self, key):
        return bool(await self.client.exists(key))

    async def incr(self, key, amount=1):
        return await self.client.incrby(key, amount)

    async def expire(self, key, ttl):
        return await self.client.expire(key, ttl)

@pytest.mark.asyncio
async def test_acquisition_group_writes_do_not_share_retired_daily_budget(test_db):
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    settings = {"global_daily_limit": 30, "group_write_daily_limit": 8}
    actions = (
        AccountRiskAction.GROUP_MESSAGE,
        AccountRiskAction.AD_PROBE,
        AccountRiskAction.AI_WARMUP,
        AccountRiskAction.AD_DELIVERY,
    )

    for index in range(8):
        allowed, reason, _ = await guard._reserve_budget(
            999,
            actions[index % len(actions)],
            RiskBudget(daily_limit=100),
            settings,
        )
        assert allowed is True
        assert reason == "reserved"
        guard.cache.client.values.pop(
            f"risk:account:999:cooldown:{actions[index % len(actions)].value}",
            None,
        )

    allowed, reason, _ = await guard._reserve_budget(
        999,
        AccountRiskAction.GROUP_MESSAGE,
        RiskBudget(daily_limit=100),
        settings,
    )
    assert allowed is True
    assert reason == "reserved"


@pytest.mark.asyncio
async def test_account_outbound_messages_share_operation_config_hard_cap(test_db):
    account = TelegramAccount(
        identifier="outbound-hard-cap-account",
        session_name="outbound-hard-cap-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(
        AccountOperationConfig(
            account_id=account.id,
            max_messages_per_day=3,
        )
    )
    await test_db.commit()

    guard = AccountRiskGuard(test_db, cache=FakeCache())
    settings = {"account_outbound_message_hard_cap_default": 30}
    for _ in range(3):
        allowed, reason, _ = await guard._reserve_budget(
            account.id,
            AccountRiskAction.GROUP_MESSAGE,
            RiskBudget(daily_limit=100),
            settings,
        )
        assert allowed is True
        assert reason == "reserved"
        guard.cache.client.values.pop(
            f"risk:account:{account.id}:cooldown:group_message",
            None,
        )

    allowed, reason, _ = await guard._reserve_budget(
        account.id,
        AccountRiskAction.AD_DELIVERY,
        RiskBudget(daily_limit=100),
        settings,
    )
    assert allowed is False
    assert reason == "account_outbound_message_hard_cap"


@pytest.mark.asyncio
async def test_account_outbound_messages_use_config_center_default(test_db):
    account = TelegramAccount(
        identifier="outbound-default-cap-account",
        session_name="outbound-default-cap-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()

    guard = AccountRiskGuard(test_db, cache=FakeCache())
    settings = {"account_outbound_message_hard_cap_default": 2}
    for action in (AccountRiskAction.AD_DELIVERY, AccountRiskAction.PRIVATE_MESSAGE):
        allowed, reason, _ = await guard._reserve_budget(
            account.id,
            action,
            RiskBudget(daily_limit=100),
            settings,
        )
        assert allowed is True
        assert reason == "reserved"

    allowed, reason, _ = await guard._reserve_budget(
        account.id,
        AccountRiskAction.GROUP_MESSAGE,
        RiskBudget(daily_limit=100),
        settings,
    )
    assert allowed is False
    assert reason == "account_outbound_message_hard_cap"


@pytest.mark.asyncio
async def test_risk_guard_blocks_repeated_message_content(test_db):
    account = TelegramAccount(
        phone="+15559990036",
        identifier="+15559990036",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_content_session",
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=10),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    details = {"content": "Try our new plan today https://example.com/deal"}

    first = await guard.check_and_reserve(wrapper, AccountRiskAction.AD_DELIVERY, target_type="group", target_id=123, details=details)
    second = await guard.check_and_reserve(wrapper, AccountRiskAction.AD_DELIVERY, target_type="group", target_id=123, details=details)

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "content_repeat_account"


@pytest.mark.asyncio
async def test_risk_guard_allows_repeated_managed_group_announcements(test_db):
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    details = {
        "source": "managed_group_channel_announcement",
        "content": "The current product rates and official URL are unchanged.",
    }

    first = await guard._check_content_policy(
        1,
        AccountRiskAction.BOT_MESSAGE,
        "chat",
        -100123,
        details,
    )
    second = await guard._check_content_policy(
        1,
        AccountRiskAction.BOT_MESSAGE,
        "chat",
        -100123,
        details,
    )

    assert first.allowed is True
    assert second.allowed is True


@pytest.mark.asyncio
async def test_risk_guard_fail_closed_blocks_when_redis_unavailable(test_db, monkeypatch):
    from app.core.account import risk_guard as risk_guard_module

    async def fake_risk_guard_settings(_db):
        return {
            "enabled": True,
            "global_daily_limit": 180,
            "redis_fail_closed": True,
            "actions": {
                "join": {"daily_limit": 30, "cooldown_seconds": 90},
            },
        }

    monkeypatch.setattr(
        risk_guard_module,
        "get_account_risk_guard_settings",
        fake_risk_guard_settings,
    )
    account = TelegramAccount(
        phone="+15559990004",
        identifier="+15559990004",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="risk_fail_closed_session",
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=20),
        managed_started_at=datetime.utcnow() - timedelta(days=20),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    guard = AccountRiskGuard(test_db)

    decision = await guard.check_and_reserve(wrapper, AccountRiskAction.JOIN, target_type="group", target_id="demo")

    assert decision.allowed is False
    assert decision.reason == "risk_budget_unavailable"
    events = (await test_db.execute(select(AccountRiskEvent))).scalars().all()
    assert events[-1].status == "block"
    assert events[-1].reason == "risk_budget_unavailable"


def test_runtime_risk_guard_parses_redis_fail_closed_string():
    from app.core.automation_settings import normalize_account_risk_guard_settings

    settings = normalize_account_risk_guard_settings({"redisFailClosed": "false"})

    assert settings["redis_fail_closed"] is True
