"""Explicit recovery verifies identity before status changes; all SDK calls fake."""
import asyncio
import importlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.account import pool as pool_module
from app.core.account.manager import AccountManager
from app.core.account.models import (
    AccountEnvironmentEvent,
    AccountStatus,
    Proxy,
    ProxyMode,
    TelegramAccount,
)
from app.core.account.operation_lease import AccountOperationLeaseHandle
from app.core.account.pool import AccountPool
from app.core.account.recovery import AccountRecoveryService, RecoveryResult, normalized_phone
from tests.test_session_lease import FakeRedis
from tests.unit.test_session_recovery import valid_string_session

api = importlib.import_module("app.api.accounts")

class Redis(FakeRedis):
    async def get(self, key):
        return self.values.get(key)


@pytest.fixture(autouse=True)
def isolated_quarantine_store(monkeypatch, tmp_path):
    monkeypatch.setattr("app.core.account.session_quarantine._root", lambda: tmp_path)
    monkeypatch.setattr("app.core.redis.redis_client", Redis())


async def probe_pool(monkeypatch, tmp_path, *, phone="10000000035", failure=None, authorized=True):
    redis = Redis()
    pool = AccountPool()
    account = await pool.add_account(
        account_id=35, phone="+10000000035", session_name="explicit-recovery",
        country_code="US", api_id="12345", api_hash="test",
        session_string=valid_string_session(), status=AccountStatus.ERROR,
    )
    account.proxy_mode, account.static_proxy_id = ProxyMode.STATIC, 30
    account.current_proxy = SimpleNamespace(protocol="socks5", host="proxy.test", port=1234,
                                            username="unused", password="unused")
    handle = AccountOperationLeaseHandle(35, "operation:35", "operation-test", "recovery", 600)
    pool._operation_lease_manager = SimpleNamespace(
        acquire=AsyncMock(return_value=handle), refresh=AsyncMock(return_value=True),
        release=AsyncMock(return_value=True), _client=lambda: redis,
    )
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    monkeypatch.setattr(pool, "_assert_proxy_policy_current", AsyncMock())
    monkeypatch.setattr(pool, "_ensure_proxy", AsyncMock())
    monkeypatch.setattr(pool, "_validate_runtime_environment", lambda _: None)
    monkeypatch.setattr(pool_module, "resolve_telegram_session_file", lambda *args, **kwargs: tmp_path / "test.session")
    built = []

    class Client:
        def __init__(self, *args, **kwargs):
            assert account.status == AccountStatus.ERROR
            assert redis.values.get("vanguard:telegram:lease:35")
            self.options, self.connected = kwargs, False
            self.auth_calls, self.me_calls = 0, 0
            built.append(self)

        async def connect(self):
            assert account.status == AccountStatus.ERROR
            self.connected = True

        async def is_user_authorized(self):
            self.auth_calls += 1
            assert account.status == AccountStatus.ERROR
            return authorized

        async def get_me(self):
            self.me_calls += 1
            assert account.status == AccountStatus.ERROR
            if failure:
                raise failure
            return SimpleNamespace(phone=phone, bot=False)

        async def disconnect(self):
            self.connected = False

        def is_connected(self):
            return self.connected

    def governor(client, _governor):
        client._vanguard_governor = SimpleNamespace(before=AsyncMock(), bootstrapping=False)

    monkeypatch.setattr(pool_module, "TelegramClient", Client)
    monkeypatch.setattr(pool_module, "install_governor", governor)
    return pool, account, redis, built


@pytest.mark.asyncio
@pytest.mark.parametrize("phone,ok,reason", [
    ("10000000035", True, "identity_verified"),
    ("10000000036", False, "identity_mismatch"),
    (None, False, "identity_unavailable"),
])
async def test_probe_keeps_error_and_releases_both_leases(monkeypatch, tmp_path, phone, ok, reason):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path, phone=phone)
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result == RecoveryResult(ok, reason)
    assert account.status == AccountStatus.ERROR
    assert account.client is account.session_guard is account.operation_lease is None
    assert not redis.values and not clients[0].connected
    assert clients[0].auth_calls == clients[0].me_calls == 1
    assert clients[0].options["request_retries"] == clients[0].options["connection_retries"] == 0
    assert clients[0].options["auto_reconnect"] is False
    assert clients[0].options["receive_updates"] is False
    pool._operation_lease_manager.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_does_not_get_me_or_retry(monkeypatch, tmp_path):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path, authorized=False)
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result.reason == "session_reauthentication_required"
    assert clients[0].auth_calls == 1 and clients[0].me_calls == 0
    assert not redis.values and account.status == AccountStatus.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("secret credential must never leak"), asyncio.CancelledError()])
async def test_failure_and_cancellation_cleanup_without_status_change(monkeypatch, tmp_path, failure):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path, failure=failure)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    else:
        result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
        assert result == RecoveryResult(False, "verification_failed")
    assert not redis.values and account.status == AccountStatus.ERROR
    assert account.client is account.session_guard is account.operation_lease is None
    assert clients[0].me_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("redis_case", ["missing", "unavailable", "busy"])
async def test_session_lease_is_required_without_client_fallback(monkeypatch, tmp_path, redis_case):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    if redis_case == "missing":
        monkeypatch.setattr("app.core.redis.redis_client", None)
    elif redis_case == "unavailable":
        redis.set = AsyncMock(side_effect=OSError("secret server address"))
    else:
        redis.values["vanguard:telegram:lease:35"] = "foreign-owner"
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert not result.ok and result.reason in {"lease_busy", "lease_unavailable"}
    assert not clients and account.status == AccountStatus.ERROR


@pytest.mark.asyncio
async def test_natural_sdk_bootstrap_self_result_is_consumed_without_second_read(monkeypatch, tmp_path):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    original_client = pool_module.TelegramClient

    class BootstrapClient(original_client):
        async def connect(self):
            await super().connect()
            await self.get_me()

    monkeypatch.setattr(pool_module, "TelegramClient", BootstrapClient)
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result.ok and clients[0].me_calls == 1 and clients[0].auth_calls == 0
    assert not redis.values and account.status == AccountStatus.ERROR
    assert not hasattr(clients[0], "_vanguard_recovery_identity")


@pytest.mark.asyncio
async def test_operation_generation_loss_prevents_identity_read(monkeypatch, tmp_path):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    pool._operation_lease_manager.refresh.return_value = False
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result.reason == "operation_lease_lost" and clients[0].me_calls == 0
    assert not redis.values and account.status == AccountStatus.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("case,ok", [("retained", False), ("unavailable", False), ("replaced", True)])
async def test_recovery_confirms_its_operation_token_is_gone(monkeypatch, tmp_path, case, ok):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    redis.values["operation:35"] = "replacement-owner" if case == "replaced" else "operation-test"
    pool._operation_lease_manager.release.return_value = False
    original_get = redis.get

    async def checked_get(key):
        if key == "operation:35" and case == "unavailable":
            raise OSError("private redis connection value")
        return await original_get(key)

    monkeypatch.setattr(redis, "get", checked_get)
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result == RecoveryResult(ok, "identity_verified" if ok else "cleanup_failed")
    assert account.status == AccountStatus.ERROR and not clients[0].connected
    assert account.operation_lease is account.session_guard is account.client is None
    assert redis.values["operation:35"] == ("replacement-owner" if case == "replaced" else "operation-test")


@pytest.mark.asyncio
async def test_failed_disconnect_retains_fenced_reference_and_never_verifies(monkeypatch, tmp_path):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    original_client = pool_module.TelegramClient

    class BrokenDisconnect(original_client):
        async def disconnect(self):
            raise OSError("private network value")

    monkeypatch.setattr(pool_module, "TelegramClient", BrokenDisconnect)
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result.reason == "cleanup_failed"
    assert account.status == AccountStatus.ERROR and account.client is clients[0]
    assert account.session_guard is not None and not account.session_guard.lease.lost.is_set()
    # A transport which failed to close keeps its renewable generation. No
    # replacement owner may use the same auth key while this client is alive.
    assert redis.values[account.session_guard.lease.key] == account.session_guard.lease.token
    assert account.operation_lease is None
    await account.session_guard.lease.close()  # Offline fixture teardown only.


@pytest.mark.asyncio
async def test_regular_acquire_and_connect_still_reject_error_without_probing(monkeypatch, tmp_path):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    assert await pool.acquire_by_id(35) is None
    assert await pool.connect_by_id(35) is None
    assert not clients and not redis.values and account.status == AccountStatus.ERROR


async def recovery_account(db):
    proxy = Proxy(id=30, host="proxy.test", port=1234, protocol="socks5", country="US", is_active=True)
    account = TelegramAccount(
        id=35, phone="+10000000035", identifier="recovery-test", session_name="recovery-test",
        status=AccountStatus.ERROR, spam_check_status="clear", risk_level="normal", is_active=True,
        proxy_mode=ProxyMode.STATIC, static_proxy_id=30, session_string="encrypted-test",
    )
    db.add_all([proxy, account])
    await db.commit()
    return account


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["inactive", "frozen", "pause", "phone", "shared_proxy"])
async def test_ineligible_account_never_probes_or_goes_online(test_db, block):
    account = await recovery_account(test_db)
    if block == "inactive":
        account.is_active = False
    elif block == "frozen":
        account.risk_level = "frozen"
    elif block == "pause":
        account.risk_pause_until = datetime.utcnow() + timedelta(hours=1)
    elif block == "phone":
        account.phone = None
    else:
        test_db.add(TelegramAccount(id=36, identifier="shared", session_name="shared", static_proxy_id=30))
    await test_db.commit()
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=AsyncMock())
    result = await AccountRecoveryService(test_db, pool).recover(35)
    assert not result.ok
    pool.add_account_from_db.assert_not_awaited()
    pool.verify_error_account.assert_not_awaited()
    assert account.status == AccountStatus.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
async def test_service_commits_only_after_probe_cleanup_and_audits_fixed_reason(test_db, monkeypatch, ok):
    await recovery_account(test_db)
    released = []

    async def verify(*args, **kwargs):
        account = await test_db.get(TelegramAccount, 35)
        assert account.status == AccountStatus.ERROR
        released.append(True)
        return RecoveryResult(ok, "identity_verified" if ok else "identity_mismatch")

    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=verify,
                           remove_account=AsyncMock())
    original = AccountManager.update_account_status

    async def commit(manager, account_id, status):
        assert released and status == AccountStatus.ONLINE
        return await original(manager, account_id, status)

    monkeypatch.setattr(AccountManager, "update_account_status", commit)
    result = await AccountRecoveryService(test_db, pool).recover(35)
    assert result.ok == ok
    account = await test_db.get(TelegramAccount, 35)
    assert account.status == (AccountStatus.ONLINE if ok else AccountStatus.ERROR)
    event = await test_db.scalar(select(AccountEnvironmentEvent).where(AccountEnvironmentEvent.account_id == 35))
    assert event.reason == result.reason
    assert json.loads(event.details)["identity_verified"] == ok
    if ok:
        pool.remove_account.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session", "phone", "proxy", "status", "risk"])
async def test_credential_change_during_probe_cannot_publish_online(test_db, change):
    await recovery_account(test_db)

    async def verify(*args, **kwargs):
        account = await test_db.get(TelegramAccount, 35)
        if change == "session":
            account.session_string = "changed-during-probe"
        elif change == "phone":
            account.phone = "+10000000036"
        elif change == "proxy":
            proxy = await test_db.get(Proxy, 30)
            proxy.password = "changed-during-probe"
        elif change == "status":
            account.status = AccountStatus.BANNED
        else:
            account.risk_level = "frozen"
        await test_db.commit()
        return RecoveryResult(True, "identity_verified")

    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=verify)
    result = await AccountRecoveryService(test_db, pool).recover(35)
    assert result.reason == "account_changed_during_verification"
    assert (await test_db.get(TelegramAccount, 35)).status == (
        AccountStatus.BANNED if change == "status" else AccountStatus.ERROR
    )


@pytest.mark.asyncio
async def test_service_cancelled_probe_stays_error_and_records_fixed_audit(test_db):
    await recovery_account(test_db)
    pool = SimpleNamespace(add_account_from_db=AsyncMock(),
                           verify_error_account=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await AccountRecoveryService(test_db, pool).recover(35)
    assert (await test_db.get(TelegramAccount, 35)).status == AccountStatus.ERROR
    event = await test_db.scalar(select(AccountEnvironmentEvent).where(AccountEnvironmentEvent.account_id == 35))
    assert event.reason == "verification_cancelled"


@pytest.mark.asyncio
async def test_error_connect_endpoint_cannot_use_placeholder(monkeypatch):
    manager = SimpleNamespace(get_account=AsyncMock(return_value=SimpleNamespace(status=AccountStatus.ERROR)),
                              update_account_status=AsyncMock())
    monkeypatch.setattr(api, "AccountManager", lambda db: manager)
    monkeypatch.setattr(api, "get_account_pool", lambda: object())
    service = SimpleNamespace(recover=AsyncMock(return_value=RecoveryResult(False, "identity_mismatch")))
    monkeypatch.setattr("app.core.account.recovery.AccountRecoveryService", lambda *args: service)
    monkeypatch.setattr(api, "_ensure_account_proxy_available", AsyncMock())
    with pytest.raises(HTTPException) as raised:
        await api.connect_account(35, object())
    assert raised.value.detail == {"reason": "identity_mismatch", "status": "error"}
    manager.update_account_status.assert_not_awaited()
    api._ensure_account_proxy_available.assert_not_awaited()


def test_phone_normalization_requires_unambiguous_international_digits():
    assert normalized_phone("+1 (000) 000-0035") == "10000000035"
    assert normalized_phone("10000000035") == "10000000035"
    assert normalized_phone("00000035") is None
    assert normalized_phone("+10000000035 ext 2") is None
