import pytest

from app.core.account.operation_lease import (
    AccountOperationLeaseManager,
    AccountOperationLeaseUnavailable,
)


class FakeLeaseRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, numkeys, key, token, *args):
        assert numkeys == 1
        if self.values.get(key) != token:
            return 0
        if "DEL" in script:
            del self.values[key]
        return 1


class FakeCache:
    def __init__(self, client):
        self.client = client


@pytest.mark.asyncio
async def test_account_operation_lease_blocks_contention_and_releases_owner_only():
    redis = FakeLeaseRedis()
    first = AccountOperationLeaseManager(FakeCache(redis))
    second = AccountOperationLeaseManager(FakeCache(redis))

    handle = await first.acquire(7, owner="search:1", ttl_seconds=60)

    assert handle is not None
    assert await second.acquire(7, owner="search:2", ttl_seconds=60) is None
    assert await first.refresh(handle) is True
    assert await first.release(handle) is True
    assert await second.acquire(7, owner="search:2", ttl_seconds=60) is not None


@pytest.mark.asyncio
async def test_account_operation_lease_fails_closed_without_redis():
    manager = AccountOperationLeaseManager(FakeCache(None))

    with pytest.raises(AccountOperationLeaseUnavailable):
        await manager.acquire(7, owner="search:1", ttl_seconds=60)


@pytest.mark.asyncio
async def test_global_account_operation_lease_uses_current_redis_client(monkeypatch):
    from app.core import redis as redis_module

    first = FakeLeaseRedis()
    second = FakeLeaseRedis()
    monkeypatch.setattr(redis_module, "redis_client", first)
    manager = AccountOperationLeaseManager()

    first_handle = await manager.acquire(7, owner="first", ttl_seconds=60)
    assert first_handle is not None

    monkeypatch.setattr(redis_module, "redis_client", second)
    second_handle = await manager.acquire(7, owner="second", ttl_seconds=60)

    assert second_handle is not None
    assert first_handle.key in first.values
    assert second_handle.key in second.values
