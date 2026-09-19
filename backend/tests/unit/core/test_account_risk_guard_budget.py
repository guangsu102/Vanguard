from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.account.models import AccountRiskDailyStat, AccountStatus, AccountType, TelegramAccount
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard


class FakeRedisClient:
    def __init__(self):
        self.values = {}

    async def exists(self, key):
        return 0

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
