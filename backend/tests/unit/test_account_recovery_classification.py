"""Transport episodes and automatic identity checks; no real Telegram calls."""
import asyncio
import errno
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account import session_quarantine
from app.core.account.models import (
    AccountEnvironmentEvent,
    AccountOperationConfig,
    AccountStatus,
    TelegramAccount,
)
from app.core.account.pool import AccountPool
from app.core.account.recovery import (
    AccountRecoveryService,
    RecoveryResult,
    connection_failure_reason,
    recovery_failure,
    recovery_result,
)
from app.core.account.rpc_governor import RpcDeferred
from app.core.account.session_guard import AccountSessionGuard, install_session_failure_hook
from tests.test_session_lease import FakeRedis
from tests.unit.test_account_error_recovery import probe_pool, recovery_account


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    redis = FakeRedis()
    monkeypatch.setattr(session_quarantine, "_root", lambda: tmp_path)
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    return redis


@pytest.mark.parametrize("error,expected", [
    (ConnectionResetError("private address"), "network_unavailable"),
    (OSError(errno.ENETUNREACH, "private address"), "network_unavailable"),
    (TimeoutError("private address"), "connection_timeout"),
    (FileNotFoundError("private session path"), "session_missing"),
    (PermissionError("private path"), "verification_failed"),
    (OSError(errno.ENOSPC, "disk full"), "verification_failed"),
    (RuntimeError("Server replied with a wrong session ID"), "session_conflict"),
])
def test_only_known_transport_failures_are_retryable(error, expected):
    assert connection_failure_reason(error) == expected


def test_revoked_authentication_wins_over_wrapping_timeout():
    revoked = type("SessionRevokedError", (Exception,), {})("private auth value")
    error = TimeoutError("transport timed out")
    error.__cause__ = revoked
    assert recovery_failure(error) == "session_reauthentication_required"


def test_budget_wait_preserves_real_retry_after_seconds():
    error = RpcDeferred("telegram_read_budget", 813)
    assert recovery_result(error) == RecoveryResult(False, "read_budget_wait", 813)


@asynccontextmanager
async def committed_session(db):
    try:
        yield db
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


@pytest.mark.asyncio
async def test_first_error_transition_and_fact_commit_together_without_renewing_generation(test_db, monkeypatch):
    account = await recovery_account(test_db)
    account.status = AccountStatus.ONLINE
    await test_db.commit()
    before = account.updated_at
    monkeypatch.setattr("app.core.database.get_db_session", lambda: committed_session(test_db))
    assert await session_quarantine.persist_error(35, failure_reason="connection_timeout", expected_updated_at=before)
    account = await test_db.get(TelegramAccount, 35, populate_existing=True)
    event = await test_db.scalar(select(AccountEnvironmentEvent).where(
        AccountEnvironmentEvent.event_type == "account_connection_failure"))
    episode_at, episode_id = account.updated_at, event.id
    assert json.loads(event.details) == {
        "source": "account_pool", "quarantine_token": None, "error_updated_at": episode_at.isoformat(),
    }
    await test_db.rollback()
    assert await session_quarantine.persist_error(35, failure_reason="network_unavailable")
    account = await test_db.get(TelegramAccount, 35, populate_existing=True)
    events = (await test_db.scalars(select(AccountEnvironmentEvent))).all()
    assert account.updated_at == episode_at
    assert [item.id for item in events] == [episode_id]


@pytest.mark.asyncio
async def test_stale_transport_cannot_overwrite_operator_change(test_db, monkeypatch):
    account = await recovery_account(test_db)
    account.status = AccountStatus.ONLINE
    await test_db.commit()
    before = account.updated_at
    account.is_active = False
    await test_db.commit()
    monkeypatch.setattr("app.core.database.get_db_session", lambda: committed_session(test_db))
    assert not await session_quarantine.persist_error(35, failure_reason="connection_timeout", expected_updated_at=before)
    assert (await test_db.get(TelegramAccount, 35, populate_existing=True)).status == AccountStatus.ONLINE
    assert not (await test_db.scalars(select(AccountEnvironmentEvent))).all()


async def automatic_account(db):
    account = await recovery_account(db)
    db.add(AccountOperationConfig(account_id=35, enabled=True))
    event = AccountEnvironmentEvent(
        account_id=35, event_type="account_connection_failure", status="failed", reason="connection_timeout",
        details=json.dumps({"source": "account_pool", "quarantine_token": None,
                            "error_updated_at": account.updated_at.isoformat()}),
    )
    db.add(event)
    await db.commit()
    return event.id, account.updated_at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_episode", "disabled", "stale", "identity_mismatch", "bad_details"])
async def test_automatic_preflight_stops_stale_unknown_or_manual_failures(test_db, case):
    event_id, updated = await automatic_account(test_db)
    if case == "disabled":
        config = await test_db.scalar(select(AccountOperationConfig))
        config.enabled = False
    elif case == "identity_mismatch":
        test_db.add(AccountEnvironmentEvent(account_id=35, event_type="session_recovery", status="failed",
                                           reason="identity_mismatch", details='{"source":"explicit_error_recovery"}'))
    elif case == "bad_details":
        event = await test_db.get(AccountEnvironmentEvent, event_id)
        event.details = "[]"
    await test_db.commit()
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=AsyncMock())
    result = await AccountRecoveryService(test_db, pool).recover(
        35, source="automatic_error_recovery",
        expected_failure_id=None if case == "missing_episode" else event_id,
        expected_error_updated_at="2000-01-01T00:00:00" if case == "stale" else updated,
    )
    assert not result.ok
    pool.add_account_from_db.assert_not_awaited()
    pool.verify_error_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_admission_and_identity_probe_never_hold_database_transaction(test_db):
    await recovery_account(test_db)

    async def admission(account):
        assert not test_db.in_transaction()
        assert account.phone and account.static_proxy_id == 30

    async def verify(*args, **kwargs):
        assert not test_db.in_transaction()
        return RecoveryResult(False, "connection_timeout")

    pool = SimpleNamespace(add_account_from_db=admission, verify_error_account=verify)
    assert await AccountRecoveryService(test_db, pool).recover(35) == RecoveryResult(False, "connection_timeout")


@pytest.mark.asyncio
async def test_disabling_operations_during_probe_prevents_online_commit(test_db):
    event_id, updated = await automatic_account(test_db)

    async def verify(*args, **kwargs):
        config = await test_db.scalar(select(AccountOperationConfig))
        config.enabled = False
        await test_db.commit()
        return RecoveryResult(True, "identity_verified")

    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=verify)
    result = await AccountRecoveryService(test_db, pool).recover(
        35, source="automatic_error_recovery", expected_failure_id=event_id, expected_error_updated_at=updated,
    )
    assert result == RecoveryResult(False, "account_changed_during_verification")
    assert (await test_db.get(TelegramAccount, 35)).status == AccountStatus.ERROR


@pytest.mark.asyncio
async def test_manual_and_automatic_recovery_share_single_probe_lock(test_db, monkeypatch, isolated):
    entered, finish = asyncio.Event(), asyncio.Event()
    first = AccountRecoveryService(test_db, object())

    async def recover_once(_account_id):
        entered.set()
        await finish.wait()
        return RecoveryResult(False, "connection_timeout")

    monkeypatch.setattr(first, "_recover_once", recover_once)
    running = asyncio.create_task(first.recover(35))
    await entered.wait()
    other_db = SimpleNamespace(rollback=AsyncMock())
    other = AccountRecoveryService(other_db, object())
    assert await other.recover(35, source="automatic_error_recovery") == RecoveryResult(False, "lease_busy")
    finish.set()
    await running
    assert "vanguard:account:recovery-probe:35" not in isolated.values


@pytest.mark.asyncio
async def test_replaced_probe_lock_cannot_publish_online_or_delete_new_owner(test_db, isolated):
    await recovery_account(test_db)

    async def verify(*args, **kwargs):
        isolated.values["vanguard:account:recovery-probe:35"] = "replacement-owner"
        return RecoveryResult(True, "identity_verified")

    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=verify)
    result = await AccountRecoveryService(test_db, pool).recover(35)
    assert not result.ok
    assert (await test_db.get(TelegramAccount, 35)).status == AccountStatus.ERROR
    assert isolated.values["vanguard:account:recovery-probe:35"] == "replacement-owner"


@pytest.mark.asyncio
async def test_guard_waits_for_future_disconnect_before_releasing_session_lease(isolated):
    future = asyncio.get_running_loop().create_future()
    client = SimpleNamespace(disconnect=lambda: future)
    guard = AccountSessionGuard(redis=isolated, account_id=81, client=client)
    assert await guard.acquire()
    close = asyncio.create_task(guard.close())
    await asyncio.sleep(0)
    assert not close.done() and isolated.values[guard.lease.key] == guard.lease.token
    future.set_result(None)
    await close
    assert guard.lease.key not in isolated.values


@pytest.mark.asyncio
async def test_quarantine_waits_and_consumes_future_disconnect_failure(isolated):
    future = asyncio.get_running_loop().create_future()
    callback = AsyncMock()
    guard = AccountSessionGuard(redis=isolated, account_id=82,
                                client=SimpleNamespace(disconnect=lambda: future), on_quarantine=callback)
    assert await guard.acquire()
    failure = asyncio.create_task(guard._quarantine(RuntimeError("wrong session ID")))
    for _ in range(100):
        if isolated.values.get(guard.lease.key, "").startswith("quarantine:"):
            break
        await asyncio.sleep(0.001)
    assert not failure.done()
    callback.assert_not_awaited()
    future.set_exception(ConnectionError("disconnect failed"))
    await failure
    callback.assert_awaited_once()
    assert isolated.values[guard.lease.key].startswith("quarantine:")


@pytest.mark.asyncio
async def test_decoder_fallback_awaits_future_disconnect():
    future = asyncio.get_running_loop().create_future()

    def decode(_body):
        raise RuntimeError("wrong session ID")

    state = SimpleNamespace(decrypt_message_data=decode)
    client = SimpleNamespace(_sender=SimpleNamespace(_state=state), disconnect=lambda: future)
    install_session_failure_hook(client)
    with pytest.raises(RuntimeError):
        state.decrypt_message_data(b"test")
    await asyncio.sleep(0)
    assert not client._vanguard_session_failure_task.done()
    future.set_result(None)
    await client._vanguard_session_failure_task


@pytest.mark.asyncio
async def test_quarantine_disconnect_timeout_still_persists_and_consumes_late_future_failure(isolated, monkeypatch):
    monkeypatch.setattr("app.core.account.session_guard.DISCONNECT_TIMEOUT_SECONDS", 0.01)
    future = asyncio.get_running_loop().create_future()
    callback = AsyncMock()
    guard = AccountSessionGuard(redis=isolated, account_id=83,
                                client=SimpleNamespace(disconnect=lambda: future), on_quarantine=callback)
    assert await guard.acquire()
    await guard._quarantine(RuntimeError("wrong session ID"))
    callback.assert_awaited_once()
    assert not future.cancelled()
    assert isolated.values[guard.lease.key].startswith("quarantine:")
    future.set_exception(ConnectionError("late disconnect failure"))
    await asyncio.sleep(0)
    assert not getattr(future, "_log_traceback", True)


@pytest.mark.asyncio
async def test_unfenced_close_timeout_keeps_owner_and_sdk_future_running(isolated, monkeypatch):
    monkeypatch.setattr("app.core.account.session_guard.DISCONNECT_TIMEOUT_SECONDS", 0.01)
    future = asyncio.get_running_loop().create_future()
    guard = AccountSessionGuard(redis=isolated, account_id=84,
                                client=SimpleNamespace(disconnect=lambda: future))
    assert await guard.acquire()
    with pytest.raises(RuntimeError, match="session_cleanup_failed"):
        await guard.close()
    assert not future.cancelled()
    assert isolated.values[guard.lease.key] == guard.lease.token
    future.set_result(None)
    await guard.close()
    assert guard.lease.key not in isolated.values


@pytest.mark.asyncio
async def test_unfenced_close_cancellation_preserves_owner_and_consumes_late_failure(isolated):
    future = asyncio.get_running_loop().create_future()
    guard = AccountSessionGuard(redis=isolated, account_id=85,
                                client=SimpleNamespace(disconnect=lambda: future))
    assert await guard.acquire()
    closing = asyncio.create_task(guard.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not future.cancelled()
    assert isolated.values[guard.lease.key] == guard.lease.token
    future.set_exception(ConnectionError("late failure after cancellation"))
    await asyncio.sleep(0)
    assert not getattr(future, "_log_traceback", True)
    await guard.lease.close()


@pytest.mark.asyncio
async def test_pool_records_only_real_errors_and_fences_stale_snapshot(monkeypatch):
    pool = AccountPool()
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(session_quarantine, "persist_error", persist)
    account = SimpleNamespace(account_id=35, session_quarantined=False, database_updated_at="snapshot")
    assert not await pool._record_connection_failure(account, RpcDeferred("telegram_read_budget", 99))
    persist.assert_not_awaited()
    assert await pool._record_connection_failure(account, TimeoutError())
    persist.assert_awaited_once_with(35, failure_reason="connection_timeout", expected_updated_at="snapshot")


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_fails", [False, True])
async def test_probe_cleanup_timeout_preserves_future_owner_and_references(monkeypatch, tmp_path, connect_fails):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    monkeypatch.setattr("app.core.account.session_guard.DISCONNECT_TIMEOUT_SECONDS", 0.01)
    future = asyncio.get_running_loop().create_future()
    original_client = __import__("app.core.account.pool", fromlist=["TelegramClient"]).TelegramClient

    class Client(original_client):
        async def connect(self):
            await super().connect()
            if connect_fails:
                raise ConnectionError("private transport failure")

        def disconnect(self):
            return future

    monkeypatch.setattr("app.core.account.pool.TelegramClient", Client)
    result = await pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30)
    assert result == RecoveryResult(False, "cleanup_failed")
    assert not future.cancelled()
    assert account.client is clients[0] and account.session_guard is not None
    assert redis.values[account.session_guard.lease.key] == account.session_guard.lease.token
    assert account.operation_lease is None
    future.set_result(None)
    clients[0].connected = False
    await account.session_guard.close()


@pytest.mark.asyncio
async def test_probe_cancellation_during_cleanup_does_not_cancel_sdk_future(monkeypatch, tmp_path):
    pool, account, redis, clients = await probe_pool(monkeypatch, tmp_path)
    future = asyncio.get_running_loop().create_future()
    entered = asyncio.Event()
    original_client = __import__("app.core.account.pool", fromlist=["TelegramClient"]).TelegramClient

    class Client(original_client):
        def disconnect(self):
            entered.set()
            return future

    monkeypatch.setattr("app.core.account.pool.TelegramClient", Client)
    running = asyncio.create_task(pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not future.cancelled()
    assert account.client is clients[0] and account.session_guard is not None
    assert redis.values[account.session_guard.lease.key] == account.session_guard.lease.token
    future.set_result(None)
    clients[0].connected = False
    await account.session_guard.close()
