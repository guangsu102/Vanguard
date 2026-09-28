import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account import operation_lease as lease


class PriorityRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, numkeys, *args):
        if numkeys == 2:
            key, pending, token, ttl = args
            if self.values.get(pending) not in (None, token):
                return 0
            return int(await self.set(key, token, nx=True, ex=ttl))
        key, token = args[:2]
        if self.values.get(key) != token:
            return 0
        if script == lease._RELEASE_LEASE_LUA:
            self.values.pop(key)
        return 1


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["ad_delivery", "ad_survival_check"])
async def test_waiting_ad_gets_next_lease_without_interrupting_current_operation(monkeypatch, purpose):
    redis = PriorityRedis()
    manager = lease.AccountOperationLeaseManager(SimpleNamespace(client=redis))
    existing = await manager.acquire(3, owner="existing")

    async def release_current(delay):
        assert redis.values[existing.key] == existing.token
        assert await manager.acquire(3, owner="account-pool:ad_survival_check") is None
        assert await manager.release(existing)

    sleep = AsyncMock(side_effect=release_current)
    monkeypatch.setattr(lease.asyncio, "sleep", sleep)
    result = await manager.acquire(3, owner="account-pool:" + purpose)
    assert result is not None and result.token != existing.token
    assert redis.values[result.key] == result.token
    assert result.key + ":ad-waiting" not in redis.values
    sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["ad_delivery", "ad_survival_check"])
async def test_priority_wait_is_bounded_and_cleans_marker(monkeypatch, purpose):
    redis = PriorityRedis()
    manager = lease.AccountOperationLeaseManager(SimpleNamespace(client=redis))
    original = await manager.acquire(3, owner="existing")
    monkeypatch.setattr(lease, "_AD_WAIT_ATTEMPTS", 2)
    sleep = AsyncMock()
    monkeypatch.setattr(lease.asyncio, "sleep", sleep)
    assert await manager.acquire(3, owner="account-pool:" + purpose) is None
    assert sleep.await_count == 2
    assert redis.values == {original.key: original.token}


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_leave_priority_or_release_other_owner(monkeypatch):
    redis = PriorityRedis()
    manager = lease.AccountOperationLeaseManager(SimpleNamespace(client=redis))
    original = await manager.acquire(3, owner="existing")
    monkeypatch.setattr(lease.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await manager.acquire(3, owner="account-pool:ad_result_reconciliation")
    assert redis.values == {original.key: original.token}


@pytest.mark.asyncio
async def test_unavailable_priority_backend_never_returns_a_lease():
    manager = lease.AccountOperationLeaseManager(
        SimpleNamespace(
            client=SimpleNamespace(set=AsyncMock(side_effect=ConnectionError("offline")))
        )
    )
    with pytest.raises(lease.AccountOperationLeaseUnavailable):
        await manager.acquire(3, owner="account-pool:ad_delivery")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_uncertain_acquisition_ack_releases_only_unreturned_token(error):
    class LostAckRedis(PriorityRedis):
        async def eval(self, script, numkeys, *args):
            result = await super().eval(script, numkeys, *args)
            if numkeys == 2 and result:
                raise error()
            return result

    redis = LostAckRedis()
    manager = lease.AccountOperationLeaseManager(SimpleNamespace(client=redis))
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await manager.acquire(3, owner="account-pool:ad_delivery")
    else:
        assert await manager.acquire(3, owner="account-pool:ad_delivery") is None
    assert redis.values == {}


@pytest.mark.asyncio
async def test_cancellation_during_priority_cleanup_releases_unreturned_lease():
    class CancelCleanupRedis(PriorityRedis):
        async def eval(self, script, numkeys, *args):
            result = await super().eval(script, numkeys, *args)
            if numkeys == 1 and args[0].endswith(":ad-waiting"):
                raise asyncio.CancelledError()
            return result

    redis = CancelCleanupRedis()
    manager = lease.AccountOperationLeaseManager(SimpleNamespace(client=redis))
    with pytest.raises(asyncio.CancelledError):
        await manager.acquire(3, owner="account-pool:ad_delivery")
    assert redis.values == {}
