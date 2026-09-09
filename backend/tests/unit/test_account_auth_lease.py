import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.account.operation_lease import (
    AccountOperationLeaseHandle,
    AccountOperationLeaseUnavailable,
)

accounts_api = importlib.import_module("app.api.accounts")


class FakeLeaseManager:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.released = []

    async def acquire(self, account_id, *, owner, ttl_seconds):
        assert account_id == 7
        assert owner.startswith("account-auth:")
        assert ttl_seconds == 600
        if self.error is not None:
            raise self.error
        return self.result

    async def release(self, handle):
        self.released.append(handle)
        return True


@pytest.mark.asyncio
async def test_existing_account_auth_returns_conflict_when_account_is_busy(monkeypatch):
    manager = FakeLeaseManager(result=None)
    monkeypatch.setattr(accounts_api, "AccountOperationLeaseManager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await accounts_api._acquire_account_auth_lease(
            SimpleNamespace(id=7),
            purpose="complete-login",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_existing_account_auth_fails_closed_when_redis_is_unavailable(monkeypatch):
    manager = FakeLeaseManager(
        error=AccountOperationLeaseUnavailable("event loop is closed")
    )
    monkeypatch.setattr(accounts_api, "AccountOperationLeaseManager", lambda: manager)

    with pytest.raises(HTTPException) as exc_info:
        await accounts_api._acquire_account_auth_lease(
            SimpleNamespace(id=7),
            purpose="import-session",
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_account_auth_lease_is_released(monkeypatch):
    handle = AccountOperationLeaseHandle(
        account_id=7,
        key="account:7",
        token="token",
        owner="account-auth:verify-code",
        ttl_seconds=600,
    )
    manager = FakeLeaseManager(result=handle)
    monkeypatch.setattr(accounts_api, "AccountOperationLeaseManager", lambda: manager)

    lease = await accounts_api._acquire_account_auth_lease(
        SimpleNamespace(id=7),
        purpose="verify-code",
    )
    await accounts_api._release_account_auth_lease(lease)

    assert manager.released == [handle]
