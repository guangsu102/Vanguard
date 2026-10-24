"""Reconnect and deletion recovery without replaying unknown Telegram writes."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from app.core.account import event_inbox as inbox
from app.core.account.models import AccountStatus, ProxyMode, TelegramAccount
from app.core.account.pool import AccountPool
from app.core.account.session_guard import AccountSessionGuard
from app.core.worker_status import TelegramEventInbox
from app.workers import telegram_worker as worker_module
from app.workers.telegram_worker import TelegramWorker, TelegramWorkerRole
from tests.test_session_lease import FakeRedis


def valid_string_session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


async def pooled_account(pool):
    return await pool.add_account(
        account_id=35, phone="+10000000035", session_name="recovery",
        country_code="US", api_id="12345", api_hash="hash",
        session_string=valid_string_session(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("client_present", [True, False])
async def test_offline_releases_orphaned_session_before_next_owner(client_present):
    redis = FakeRedis()
    pool = AccountPool()
    account = await pooled_account(pool)
    client = SimpleNamespace(is_connected=lambda: True, disconnect=AsyncMock())
    guard = AccountSessionGuard(
        redis=redis,
        account_id=35,
        client=client if client_present else None,
    )
    assert await guard.acquire()
    renewal = guard.lease._renew_task
    account.client = client if client_present else None
    account.session_guard = guard
    account.keep_connected = True
    try:
        assert await pool.set_offline(account.session_name)
        assert account.client is None and account.session_guard is None
        assert account.status == AccountStatus.OFFLINE
        assert not account.keep_connected
        if client_present:
            client.disconnect.assert_awaited()
        else:
            client.disconnect.assert_not_awaited()
        assert guard.lease.key not in redis.values
        assert guard.lease.owner_key not in redis.values
        next_owner = AccountSessionGuard(redis=redis, account_id=35)
        assert await next_owner.acquire()
        try:
            # A late old close must not release the new owner's auth-key lease.
            await guard.close()
            assert redis.values[next_owner.lease.key] == next_owner.lease.token
        finally:
            await next_owner.close()
    finally:
        await guard.close()
        if renewal is not None:
            with suppress(asyncio.CancelledError):
                await renewal


@pytest.mark.asyncio
async def test_failed_client_construction_releases_acquired_session(monkeypatch, tmp_path):
    redis = FakeRedis()
    pool = AccountPool()
    account = await pooled_account(pool)
    account.session_string = valid_string_session()
    account.keep_connected = True
    account.status = AccountStatus.IDLE
    account.proxy_mode = ProxyMode.NONE
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(
        TELEGRAM_SESSION_DIR=str(tmp_path),
    ))
    monkeypatch.setattr(pool, "_validate_runtime_environment", lambda _: None)
    monkeypatch.setattr(pool, "_assert_proxy_policy_current", AsyncMock())
    monkeypatch.setattr(pool, "_ensure_proxy", AsyncMock())

    def broken_client(*args, **kwargs):
        raise ValueError("invalid client configuration")

    monkeypatch.setattr("app.core.account.pool.TelegramClient", broken_client)
    with pytest.raises(ValueError, match="invalid client configuration"):
        await pool._create_client(account)
    assert account.session_guard is None
    assert "vanguard:telegram:lease:35" not in redis.values
    assert "vanguard:telegram:owner:35" not in redis.values


@pytest.mark.asyncio
async def test_replacement_closes_disconnected_guard_before_claiming_new_session(monkeypatch, tmp_path):
    redis = FakeRedis()
    pool = AccountPool()
    account = await pooled_account(pool)
    account.proxy_mode = ProxyMode.NONE
    old_client = SimpleNamespace(is_connected=lambda: False, disconnect=AsyncMock())
    old_guard = AccountSessionGuard(redis=redis, account_id=35, client=old_client)
    assert await old_guard.acquire()
    account.client, account.session_guard = old_client, old_guard
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(
        TELEGRAM_SESSION_DIR=str(tmp_path),
    ))
    monkeypatch.setattr(pool, "_validate_runtime_environment", lambda _: None)
    monkeypatch.setattr(pool, "_ensure_proxy", AsyncMock())

    def construction_after_claim(*args, **kwargs):
        assert old_guard.lease.token != redis.values[old_guard.lease.key]
        raise ValueError("new owner acquired")

    monkeypatch.setattr("app.core.account.pool.TelegramClient", construction_after_claim)
    try:
        with pytest.raises(ValueError, match="new owner acquired"):
            await pool.connect_by_id(35, purpose="recovery", keep_connected=True)
        assert account.client is None and account.session_guard is None
        assert "vanguard:telegram:lease:35" not in redis.values
    finally:
        await old_guard.close()


@pytest.mark.asyncio
async def test_deleted_fact_progresses_offline_while_unknown_join_remains_ordered(test_db, monkeypatch):
    test_db.add(TelegramAccount(id=35, identifier="recovery", session_name="recovery"))
    await test_db.flush()
    unknown = TelegramEventInbox(
        account_id=35, event_key="unknown-join", kind="join", chat_id=-100123,
        payload_json="{}", state="reconciliation_required", external_attempted=True,
    )
    later_write = TelegramEventInbox(
        account_id=35, event_key="later-message", kind="message", chat_id=-100123,
        payload_json="{}", state="pending", next_attempt_at=datetime.utcnow(),
    )
    test_db.add_all([unknown, later_write])
    event = SimpleNamespace(
        id=999, chat_id=-100123, deleted_ids=[123], sender=None, message=None,
    )
    assert await inbox.enqueue(test_db, 35, "deleted", event)
    await test_db.commit()

    @asynccontextmanager
    async def same_session():
        yield test_db

    observed = AsyncMock()
    monkeypatch.setattr(worker_module, "get_db_session", same_session)
    monkeypatch.setattr("app.modules.acquisition.adaptive_frequency.queue_deleted_observation", observed)
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="offline-recovery")
    worker._account_pool = SimpleNamespace(get_account_by_id=AsyncMock(side_effect=AssertionError("no client needed")))
    worker._growth_runtime_account_ids = {35}
    assert not worker._growth_listener_sessions
    assert await worker._process_growth_inbox_once(local_only=False)
    observed.assert_awaited_once()
    restored = observed.await_args.args[2]
    assert restored.chat_id == -100123 and restored.deleted_ids == [123]
    assert observed.await_args.kwargs == {"confirmed": True}
    assert unknown.state == "reconciliation_required" and unknown.external_attempted
    assert later_write.state == "pending"
    assert await inbox.claim(test_db, [35], local_only=False) is None
