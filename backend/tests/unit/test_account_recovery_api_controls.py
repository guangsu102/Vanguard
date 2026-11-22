"""Connection controls must preserve fences and operator stop decisions."""

import importlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.account.recovery import RecoveryResult
from app.core.account.session_quarantine import QuarantineUnavailable

api = importlib.import_module("app.api.accounts")


async def account_row(db, status=AccountStatus.ERROR):
    account = TelegramAccount(
        id=135, identifier="control-test", session_name="control-test",
        phone="+10000000135", status=status, is_active=True,
        spam_check_status="clear", risk_level="normal", connection_count=7,
        last_connected_at=datetime(2026, 1, 1),
    )
    db.add(account)
    await db.commit()
    return account


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AccountStatus.ERROR, AccountStatus.BANNED, AccountStatus.RESTRICTED])
async def test_stop_preserves_protective_state_and_disables_auto_recovery(test_db, status):
    account = await account_row(test_db, status)
    result = await api.disconnect_account(account.id, test_db)
    await test_db.refresh(account)
    assert account.status == status
    assert account.is_active is False
    assert result["data"] == {"account_id": 135, "status": status.value, "is_active": False}


@pytest.mark.asyncio
async def test_stop_normal_account_is_persisted_for_worker_filter(test_db):
    account = await account_row(test_db, AccountStatus.ONLINE)
    await api.disconnect_account(135, test_db)
    await test_db.refresh(account)
    assert account.status == AccountStatus.OFFLINE and account.is_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AccountStatus.BANNED, AccountStatus.RESTRICTED])
async def test_connect_cannot_clear_platform_protection(test_db, monkeypatch, status):
    account = await account_row(test_db, status)
    probe = AsyncMock()
    monkeypatch.setattr(api, "_ensure_account_proxy_available", probe)
    with pytest.raises(HTTPException) as raised:
        await api.connect_account(135, test_db)
    assert raised.value.detail["reason"] == "account_status_blocked"
    probe.assert_not_awaited()
    assert account.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AccountStatus.OFFLINE, AccountStatus.ONLINE])
async def test_fenced_normal_status_uses_identity_recovery(test_db, monkeypatch, status):
    await account_row(test_db, status)
    observed = []

    async def recover(account_id):
        account = await test_db.get(TelegramAccount, account_id)
        observed.append(account.status)
        return RecoveryResult(False, "identity_mismatch")

    monkeypatch.setattr("app.core.account.session_quarantine.current", AsyncMock(return_value="a" * 32))
    monkeypatch.setattr(api, "get_account_pool", lambda: object())
    monkeypatch.setattr("app.core.account.recovery.AccountRecoveryService", lambda *_: SimpleNamespace(recover=recover))
    check_proxy = AsyncMock()
    monkeypatch.setattr(api, "_ensure_account_proxy_available", check_proxy)
    with pytest.raises(HTTPException) as raised:
        await api.connect_account(135, test_db)
    assert raised.value.detail["reason"] == "identity_mismatch"
    assert observed == [AccountStatus.ERROR]
    check_proxy.assert_not_awaited()
    assert (await test_db.get(TelegramAccount, 135)).status == AccountStatus.ERROR


@pytest.mark.asyncio
async def test_fence_inspection_releases_db_and_fails_closed(test_db, monkeypatch):
    await account_row(test_db, AccountStatus.OFFLINE)

    async def unavailable(*_):
        assert not test_db.in_transaction()
        raise QuarantineUnavailable("private endpoint must not cross API")

    monkeypatch.setattr("app.core.account.session_quarantine.current", unavailable)
    with pytest.raises(HTTPException) as raised:
        await api.connect_account(135, test_db)
    assert raised.value.status_code == 503
    assert raised.value.detail == {"reason": "lease_unavailable", "status": "error"}
    assert (await test_db.get(TelegramAccount, 135)).status == AccountStatus.OFFLINE


@pytest.mark.asyncio
async def test_concurrent_error_wins_over_normal_connect_request(test_db, monkeypatch):
    await account_row(test_db, AccountStatus.OFFLINE)
    monkeypatch.setattr("app.core.account.session_quarantine.current", AsyncMock(return_value=None))

    async def proxy_check(*_):
        await test_db.execute(update(TelegramAccount).where(TelegramAccount.id == 135).values(status=AccountStatus.ERROR))
        await test_db.commit()

    monkeypatch.setattr(api, "_ensure_account_proxy_available", proxy_check)
    with pytest.raises(HTTPException) as raised:
        await api.connect_account(135, test_db)
    assert raised.value.status_code == 409
    assert (await test_db.get(TelegramAccount, 135)).status == AccountStatus.ERROR


@pytest.mark.asyncio
async def test_connect_request_does_not_fabricate_actual_connection_statistics(test_db, monkeypatch):
    await account_row(test_db, AccountStatus.OFFLINE)
    monkeypatch.setattr("app.core.account.session_quarantine.current", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_ensure_account_proxy_available", AsyncMock())
    result = await api.connect_account(135, test_db)
    account = await test_db.get(TelegramAccount, 135)
    assert result["data"]["identity_verified"] is False
    assert result["data"]["is_active"] is True
    assert account.last_connected_at == datetime(2026, 1, 1)
    assert account.connection_count == 7
