from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.account.models import AccountRiskDailyStat, AccountStatus, AccountType, TelegramAccount
from app.core.account.risk_guard import (
    AccountRiskAction,
    AccountRiskGuard,
    _ad_probe_attempt_limit,
    _ad_probe_operating_date,
)
from app.modules.owned_group.models import OwnedGroupAsset


class FakeRedisClient:
    def __init__(self):
        self.values = {}
        self.lists = {}

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start:end + 1]

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    async def delete(self, key):
        self.values.pop(key, None)

    async def lrem(self, key, count, value):
        if value in self.lists.get(key, []):
            self.lists[key].remove(value)

    async def exists(self, key):
        return 1 if key in self.values else 0

    async def incrby(self, key, amount):
        self.values[key] = int(self.values.get(key, 0)) + amount
        return self.values[key]

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ttl=None):
        self.values[key] = value
        return True


class FakeCache:
    def __init__(self):
        self.client = FakeRedisClient()

    async def exists(self, key):
        return bool(await self.client.exists(key))

    async def incr(self, key, amount=1):
        return await self.client.incrby(key, amount)

    async def expire(self, key, ttl):
        return await self.client.expire(key, ttl)

    async def get(self, key):
        return await self.client.get(key)

    async def set(self, key, value, ttl=None):
        return await self.client.set(key, value, ttl=ttl)


@pytest.mark.asyncio
async def test_owned_group_owner_is_blocked_from_external_ad_delivery(test_db):
    now = datetime.utcnow()
    account = TelegramAccount(
        identifier="protected-owner-ad",
        session_name="protected-owner-ad",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        created_at=now - timedelta(days=365),
        managed_started_at=now - timedelta(days=365),
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(
        OwnedGroupAsset(
            internal_name="protected-owner-ad",
            title="Protected owner ad",
            owner_account_id=account.id,
        )
    )
    await test_db.commit()

    decision = await AccountRiskGuard(test_db, cache=FakeCache()).check_and_reserve(
        SimpleNamespace(account_id=account.id, country_code="US"),
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id=12345,
        details={"delivery_policy": "ad_only"},
    )

    assert decision.allowed is False
    assert decision.reason == "owned_group_owner_protected"


@pytest.mark.asyncio
async def test_redis_budget_blocks_private_messages_when_limit_is_exceeded(test_db):
    account = TelegramAccount(
        phone="+15559990050",
        identifier="+15559990050",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="budget_session",
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=20),
        managed_started_at=datetime.utcnow() - timedelta(days=20),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    guard = AccountRiskGuard(test_db, cache=FakeCache())
    wrapper = SimpleNamespace(account_id=account.id, country_code="US")

    allowed = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.PRIVATE_MESSAGE,
        target_type="user",
        target_id=1,
    )
    blocked = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.PRIVATE_MESSAGE,
        target_type="user",
        target_id=2,
    )

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.reason == "private_message_cooldown"

    stats = (await test_db.execute(select(AccountRiskDailyStat))).scalars().all()
    assert len(stats) == 2
    assert {stat.status for stat in stats} == {"allow", "block"}
    assert all(stat.action == AccountRiskAction.PRIVATE_MESSAGE.value for stat in stats)


@pytest.mark.asyncio
async def test_owned_group_join_allows_restricted_account_that_join_blocks(test_db):
    account = TelegramAccount(
        phone="+15559990051",
        identifier="+15559990051",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="owned_join_restricted_session",
        status=AccountStatus.RESTRICTED,
        created_at=datetime.utcnow() - timedelta(days=20),
        managed_started_at=datetime.utcnow() - timedelta(days=20),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    guard = AccountRiskGuard(test_db, cache=FakeCache())
    wrapper = SimpleNamespace(account_id=account.id, country_code="US")

    blocked = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.JOIN,
        target_type="group",
        target_id=-100123,
    )
    assert blocked.allowed is False

    allowed_join = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.OWNED_GROUP_JOIN,
        target_type="group",
        target_id=-100123,
    )
    assert allowed_join.allowed is True

    # A restricted account may still edit its own profile.
    allowed_profile = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.PROFILE_UPDATE,
        target_type="account",
        target_id=account.id,
    )
    assert allowed_profile.allowed is True


@pytest.mark.asyncio
async def test_promotional_join_uses_durable_budget_without_redis_join_counter(test_db):
    account = TelegramAccount(
        identifier="durable-join-budget",
        session_name="durable-join-budget",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=365),
        managed_started_at=datetime.utcnow() - timedelta(days=365),
    )
    test_db.add(account)
    await test_db.commit()
    cache = FakeCache()
    guard = AccountRiskGuard(test_db, cache=cache)

    decision = await guard.check_and_reserve(
        SimpleNamespace(account_id=account.id, country_code="US"),
        AccountRiskAction.JOIN,
        target_type="group",
        target_id="example_group",
    )

    assert decision.allowed is True
    assert not any(":daily:join:" in key for key in cache.client.values)
    assert not any(":cooldown:join" in key for key in cache.client.values)


@pytest.mark.asyncio
async def test_ad_only_delivery_repeats_content_while_growth_is_deduped(test_db):
    account = TelegramAccount(
        phone="+15559990052",
        identifier="+15559990052",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="ad_only_dedup_session",
        status=AccountStatus.ONLINE,
        created_at=datetime.utcnow() - timedelta(days=20),
        managed_started_at=datetime.utcnow() - timedelta(days=20),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    guard = AccountRiskGuard(test_db, cache=FakeCache())
    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    content = "GPT不降智低至0.09x、纯血国模0.2x 缓存命中99%"

    first = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id=-100123,
        details={
            "source": "ad_delivery",
            "delivery_policy": "ad_only",
            "content": content,
        },
    )
    assert first.allowed is True

    # The dedicated account repeats the operator-curated creative on its
    # configured cadence: content dedup must not block the repeat.
    repeat = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id=-100123,
        details={
            "source": "ad_delivery",
            "delivery_policy": "ad_only",
            "content": content,
        },
    )
    assert repeat.allowed is True

    growth_first = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id=-100456,
        details={
            "source": "ad_delivery",
            "delivery_policy": "growth",
            "content": content,
        },
    )
    assert growth_first.allowed is True

    growth_repeat = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.AD_DELIVERY,
        target_type="group",
        target_id=-100456,
        details={
            "source": "ad_delivery",
            "delivery_policy": "growth",
            "content": content,
        },
    )
    assert growth_repeat.allowed is False
    assert growth_repeat.reason in {"content_repeat_account", "content_repeat_target"}

def test_ad_probe_budget_is_derived_and_resets_at_nine_beijing():
    assert _ad_probe_attempt_limit({"max_new_ad_groups_per_day": 5}) == 10
    assert _ad_probe_attempt_limit({"max_new_ad_groups_per_day": 10}) == 15
    assert _ad_probe_attempt_limit({"max_new_ad_groups_per_day": 100}) == 15
    capacity = {"timezone_offset_hours": 8, "window_start_hour": 9}
    assert _ad_probe_operating_date(datetime(2026, 9, 20, 0, 59), capacity).isoformat() == "2026-09-19"
    assert _ad_probe_operating_date(datetime(2026, 9, 20, 1, 0), capacity).isoformat() == "2026-09-20"


@pytest.mark.asyncio
async def test_restricted_account_can_only_clear_own_bio_during_restriction_pause(test_db):
    account = TelegramAccount(
        identifier="restricted-bio-cleanup",
        session_name="restricted-bio-cleanup",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.RESTRICTED,
        risk_level="frozen",
        risk_reason="account_restricted",
        risk_pause_until=datetime.utcnow() + timedelta(days=1),
        created_at=datetime.utcnow() - timedelta(days=365),
        managed_started_at=datetime.utcnow() - timedelta(days=365),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    wrapper = SimpleNamespace(account_id=account.id, country_code="US")
    cleanup = {
        "source": "account_profile_update_batch",
        "bio_length": 0,
    }

    allowed = await guard.check_and_reserve(
        wrapper,
        AccountRiskAction.PROFILE_UPDATE,
        target_type="account",
        target_id=account.id,
        details=cleanup,
    )
    assert allowed.allowed is True
    await test_db.refresh(account)
    assert account.status == AccountStatus.RESTRICTED
    assert account.risk_level == "frozen"
    assert account.risk_reason == "account_restricted"
    assert account.risk_pause_until > datetime.utcnow()

    for action, target_type, target_id, details in (
        (AccountRiskAction.JOIN, "group", -100123, {}),
        (AccountRiskAction.PROFILE_UPDATE, "account", account.id, {"source": "account_profile_update_batch", "bio_length": 10}),
        (AccountRiskAction.PROFILE_UPDATE, "account", account.id + 1, cleanup),
        (AccountRiskAction.PROFILE_UPDATE, "account", account.id, {"source": "account_profile", "bio_length": 0}),
    ):
        denied = await guard.check_and_reserve(
            wrapper, action, target_type=target_type, target_id=target_id, details=details
        )
        assert denied.allowed is False


@pytest.mark.asyncio
async def test_restricted_bio_cleanup_does_not_bypass_unrelated_risk_pause(test_db):
    account = TelegramAccount(
        identifier="flood-paused-bio-cleanup",
        session_name="flood-paused-bio-cleanup",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.RESTRICTED,
        risk_level="frozen",
        risk_reason="flood_wait",
        risk_pause_until=datetime.utcnow() + timedelta(hours=2),
        created_at=datetime.utcnow() - timedelta(days=365),
        managed_started_at=datetime.utcnow() - timedelta(days=365),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    guard = AccountRiskGuard(test_db, cache=FakeCache())
    denied = await guard.check_and_reserve(
        SimpleNamespace(account_id=account.id, country_code="US"),
        AccountRiskAction.PROFILE_UPDATE,
        target_type="account",
        target_id=account.id,
        details={"source": "account_profile_update_batch", "bio_length": 0},
    )
    assert denied.allowed is False
    assert denied.reason == "flood_wait"
