import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.account.models import AccountStatus
from app.core.account.operation_lease import (
    AccountOperationLeaseBusy,
    AccountOperationLeaseHandle,
    AccountOperationLeaseUnavailable,
)
from app.core.account.pool import AccountPool


class FakeClient:
    def __init__(self):
        self.disconnected = False

    def is_connected(self):
        return not self.disconnected

    async def disconnect(self):
        self.disconnected = True


class FakeLeaseManager:
    def __init__(self, handle=None, acquire_error=None, refresh_result=True):
        self.handle = handle
        self.acquire_error = acquire_error
        self.refresh_result = refresh_result
        self.refreshed = []
        self.released = []

    async def acquire(self, account_id, *, owner, ttl_seconds):
        del account_id, owner, ttl_seconds
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.handle

    async def refresh(self, handle):
        self.refreshed.append(handle)
        return self.refresh_result

    async def release(self, handle):
        self.released.append(handle)
        return True


async def add_test_account(pool: AccountPool, account_id: int = 41):
    return await pool.add_account(
        account_id=account_id,
        phone=f"+1555000{account_id}",
        session_name=f"lease_{account_id}",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
    )


@pytest.mark.asyncio
async def test_account_pool_fails_closed_when_lease_backend_is_unavailable():
    manager = FakeLeaseManager(
        acquire_error=AccountOperationLeaseUnavailable("redis unavailable")
    )
    pool = AccountPool(operation_lease_manager=manager)
    account = await add_test_account(pool)

    assert await pool._claim_operation_lease(account, "test") is False
    assert account.operation_lease is None


@pytest.mark.asyncio
async def test_account_pool_can_surface_busy_lease_to_opted_in_caller():
    pool = AccountPool(operation_lease_manager=FakeLeaseManager(handle=None))
    account = await add_test_account(pool)

    with pytest.raises(AccountOperationLeaseBusy):
        await pool.acquire_by_id(
            account.account_id,
            purpose="auto_join",
            raise_on_lease_failure=True,
        )

    assert account.operation_lease is None


@pytest.mark.asyncio
async def test_account_pool_redacts_composite_secrets_from_acquire_log():
    handle = AccountOperationLeaseHandle(
        account_id=45,
        key="account:45",
        token="token",
        owner="account-pool:test",
        ttl_seconds=30,
    )
    pool = AccountPool(operation_lease_manager=FakeLeaseManager(handle=handle))
    account = await add_test_account(pool, 45)
    pool._assert_proxy_policy_current = AsyncMock(
        side_effect=RuntimeError(
            "proxy_password=ProxyPlain TELEGRAM_API_HASH=HashPlain jwt_secret=JwtPlain"
        )
    )
    pool.logger = SimpleNamespace(warning=MagicMock(), debug=MagicMock())

    with pytest.raises(RuntimeError):
        await pool.acquire_by_id(
            account.account_id,
            purpose="owned_group_message",
            operation_lease=handle,
        )

    logged_error = pool.logger.warning.call_args.kwargs["error"]
    assert "ProxyPlain" not in logged_error
    assert "HashPlain" not in logged_error
    assert "JwtPlain" not in logged_error


@pytest.mark.asyncio
async def test_account_pool_keeps_legacy_none_for_busy_lease_by_default():
    pool = AccountPool(operation_lease_manager=FakeLeaseManager(handle=None))
    account = await add_test_account(pool)

    assert await pool.acquire_by_id(account.account_id, purpose="test") is None
    assert account.operation_lease is None


@pytest.mark.asyncio
async def test_account_pool_can_surface_lease_backend_failure_to_opted_in_caller():
    pool = AccountPool(
        operation_lease_manager=FakeLeaseManager(
            acquire_error=AccountOperationLeaseUnavailable("redis unavailable")
        )
    )
    account = await add_test_account(pool)

    with pytest.raises(AccountOperationLeaseUnavailable, match="redis unavailable"):
        await pool.acquire_by_id(
            account.account_id,
            purpose="auto_join",
            raise_on_lease_failure=True,
        )

    assert account.operation_lease is None


@pytest.mark.asyncio
async def test_account_pool_can_surface_same_process_working_account_as_busy():
    pool = AccountPool(operation_lease_manager=FakeLeaseManager())
    account = await add_test_account(pool)
    account.status = AccountStatus.WORKING

    with pytest.raises(AccountOperationLeaseBusy):
        await pool.acquire_by_id(
            account.account_id,
            purpose="auto_join",
            raise_on_lease_failure=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AccountStatus.ERROR, AccountStatus.BANNED])
async def test_account_pool_does_not_treat_unhealthy_account_as_lease_busy(status):
    pool = AccountPool(operation_lease_manager=FakeLeaseManager())
    account = await add_test_account(pool)
    account.status = status

    assert (
        await pool.acquire_by_id(
            account.account_id,
            purpose="auto_join",
            raise_on_lease_failure=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_account_pool_disconnects_client_when_lease_renewal_is_lost(monkeypatch):
    handle = AccountOperationLeaseHandle(
        account_id=42,
        key="account:42",
        token="token",
        owner="account-pool:test",
        ttl_seconds=30,
    )
    manager = FakeLeaseManager(handle=handle, refresh_result=False)
    pool = AccountPool(operation_lease_manager=manager)
    account = await add_test_account(pool, 42)
    client = FakeClient()
    account.client = client
    account.operation_lease = handle
    monkeypatch.setattr("app.core.account.pool.asyncio.sleep", AsyncMock())

    await pool._renew_operation_lease(account, handle)

    assert manager.refreshed == [handle]
    assert account.operation_lease_lost is True
    assert account.status == AccountStatus.ERROR
    assert client.disconnected is True
    assert account.client is None


@pytest.mark.asyncio
async def test_account_pool_release_cancels_renewal_and_releases_owned_lease():
    handle = AccountOperationLeaseHandle(
        account_id=43,
        key="account:43",
        token="token",
        owner="account-pool:test",
        ttl_seconds=600,
    )
    manager = FakeLeaseManager(handle=handle)
    pool = AccountPool(operation_lease_manager=manager)
    account = await add_test_account(pool, 43)

    assert await pool._claim_operation_lease(account, "test") is True
    renewal_task = account.operation_lease_renewal_task
    assert renewal_task is not None

    await pool.release(account)

    assert renewal_task.cancelled()
    assert manager.released == [handle]
    assert account.operation_lease is None
    assert account.status == AccountStatus.IDLE


@pytest.mark.asyncio
@pytest.mark.parametrize("acquire_method", ["acquire", "acquire_by_id"])
async def test_cancelled_account_acquire_releases_owned_lease(
    monkeypatch,
    acquire_method,
):
    account_id = 45 if acquire_method == "acquire" else 46
    handle = AccountOperationLeaseHandle(
        account_id=account_id,
        key=f"account:{account_id}",
        token="token",
        owner="account-pool:test",
        ttl_seconds=600,
    )
    manager = FakeLeaseManager(handle=handle)
    pool = AccountPool(operation_lease_manager=manager)
    account = await add_test_account(pool, account_id)
    account.status = AccountStatus.IDLE
    previous_status = account.status
    renewal_tasks = []

    async def cancel_during_acquire(_account):
        renewal_tasks.append(account.operation_lease_renewal_task)
        raise asyncio.CancelledError

    monkeypatch.setattr(pool, "_assert_proxy_policy_current", cancel_during_acquire)

    with pytest.raises(asyncio.CancelledError):
        if acquire_method == "acquire":
            await pool.acquire(purpose="cancelled-acquire")
        else:
            await pool.acquire_by_id(account_id, purpose="cancelled-acquire-by-id")

    assert len(renewal_tasks) == 1
    assert renewal_tasks[0] is not None and renewal_tasks[0].cancelled()
    assert manager.released == [handle]
    assert account.operation_lease is None
    assert account.operation_lease_renewal_task is None
    assert account.status == previous_status


@pytest.mark.asyncio
async def test_account_invalidation_cancels_renewal_and_releases_owned_lease():
    handle = AccountOperationLeaseHandle(
        account_id=44,
        key="account:44",
        token="token",
        owner="account-pool:test",
        ttl_seconds=600,
    )
    manager = FakeLeaseManager(handle=handle)
    pool = AccountPool(operation_lease_manager=manager)
    account = await add_test_account(pool, 44)
    client = FakeClient()
    account.client = client

    assert await pool._claim_operation_lease(account, "test") is True
    renewal_task = account.operation_lease_renewal_task

    assert await pool.invalidate_account(44, reason="session_updated") is True

    assert renewal_task is not None and renewal_task.cancelled()
    assert manager.released == [handle]
    assert client.disconnected is True
    assert await pool.get_account_by_id(44) is None
