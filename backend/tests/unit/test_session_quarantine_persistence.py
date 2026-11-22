"""Durable isolation during DB/Redis failures; all providers are offline fakes."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account import session_quarantine as quarantine
from app.core.account.models import AccountStatus, TelegramAccount
from app.core.account.session_guard import AccountSessionGuard
from app.core.account.session_lease import PROCESS_ID, SessionLease
from tests.test_session_lease import FakeRedis


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(quarantine, "_root", lambda: tmp_path)


class FailingRedis(FakeRedis):
    fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("offline Redis")
        return await super().get(key)

    async def eval(self, *args):
        if self.fail:
            raise ConnectionError("offline Redis")
        return await super().eval(*args)


@pytest.mark.asyncio
async def test_no_ttl_sentinel_blocks_legacy_owner_and_stale_release():
    redis = FakeRedis()
    owner = SessionLease(redis, 81)
    assert await owner.acquire()
    fence = await quarantine.publish(81, redis, lease_token=owner.token, owner=PROCESS_ID)
    assert fence.file_confirmed and fence.sentinel_confirmed
    assert redis.values[owner.key] == f"quarantine:{fence.token}"
    assert redis.expiry[owner.key] == 0
    assert not await redis.set(owner.key, "legacy-owner", nx=True, ex=90)
    assert not await owner.renew()
    assert not await owner.release()
    assert not await SessionLease(redis, 81).acquire()


@pytest.mark.asyncio
async def test_probe_preserves_fence_until_explicit_compare_clear():
    redis = FakeRedis()
    fence = await quarantine.publish(82, redis)
    probe = SessionLease(redis, 82, quarantine_token=fence.token)
    assert await probe.acquire()
    assert not await quarantine.clear_after_recovery(82, fence.token, redis)
    assert await probe.release()
    assert redis.values[probe.key] == f"quarantine:{fence.token}"
    assert await quarantine.clear_after_recovery(82, fence.token, redis)
    assert await quarantine.current(82, redis) is None
    normal = SessionLease(redis, 82)
    assert await normal.acquire()
    await normal.release()


@pytest.mark.asyncio
async def test_old_recovery_cannot_clear_new_probe_failure():
    redis = FakeRedis()
    first = await quarantine.publish(83, redis)
    probe = SessionLease(redis, 83, quarantine_token=first.token)
    assert await probe.acquire()
    newer = await quarantine.publish(83, redis, lease_token=probe.token, owner=PROCESS_ID)
    assert newer.token != first.token
    assert not await probe.release()
    assert not await quarantine.clear_after_recovery(83, first.token, redis)
    assert await quarantine.current(83, redis) == newer.token
    assert redis.values[probe.key] == f"quarantine:{newer.token}"


@pytest.mark.asyncio
async def test_stale_generation_changes_neither_new_fence_nor_owner(tmp_path):
    redis = FakeRedis()
    first = await quarantine.publish(84, redis)
    before_file = (tmp_path / "84.json").read_bytes()
    before_values = dict(redis.values)
    with pytest.raises(quarantine.StaleQuarantineOwner):
        await quarantine.publish(84, redis, lease_token="old-generation", owner=PROCESS_ID)
    assert (tmp_path / "84.json").read_bytes() == before_file
    assert redis.values == before_values
    assert await quarantine.clear_after_recovery(84, first.token, redis)
    # An already-retired generation cannot re-quarantine after recovery.
    with pytest.raises(quarantine.StaleQuarantineOwner):
        await quarantine.publish(84, redis, lease_token="old-generation", owner=PROCESS_ID)
    assert not (tmp_path / "84.json").exists()
    assert not redis.values


@pytest.mark.asyncio
async def test_foreign_live_owner_never_gets_overwritten_or_db_callback():
    redis = FakeRedis()
    callback = AsyncMock()
    guard = AccountSessionGuard(redis=redis, account_id=85, on_quarantine=callback)
    assert await guard.acquire()
    redis.values[guard.lease.key] = "new-live-generation"
    redis.values[guard.lease.owner_key] = "foreign-process"
    await guard._quarantine(RuntimeError("session lease generation replaced"))
    callback.assert_not_awaited()
    assert redis.values[guard.lease.key] == "new-live-generation"
    assert not any("quarantine:" in key for key in redis.values)


@pytest.mark.asyncio
async def test_redis_failure_survives_restart_as_small_shared_file(tmp_path):
    redis = FailingRedis()
    redis.fail = True
    published = await quarantine.publish(86, redis)
    assert published.file_confirmed and not published.sentinel_confirmed
    payload = json.loads((tmp_path / "86.json").read_text())
    assert set(payload) == {"token", "reason", "created_at"}
    assert (tmp_path / "86.json").stat().st_size < 512
    with pytest.raises(quarantine.QuarantineUnavailable):
        await SessionLease(redis, 86).acquire()
    # New process/Redis instance can recover the fence from the shared file.
    fresh_redis = FakeRedis()
    assert await quarantine.current(86, fresh_redis) == published.token
    assert not await SessionLease(fresh_redis, 86).acquire()


@pytest.mark.asyncio
async def test_file_write_failure_still_publishes_redis_sentinel(monkeypatch):
    def no_space(*args):
        raise OSError("no space")

    monkeypatch.setattr(quarantine, "_write", no_space)
    redis = FakeRedis()
    published = await quarantine.publish(87, redis)
    assert not published.file_confirmed and published.sentinel_confirmed
    assert not await SessionLease(redis, 87).acquire()


@pytest.mark.asyncio
async def test_dual_persistence_failure_retains_existing_owner(monkeypatch):
    redis = FailingRedis()
    guard = AccountSessionGuard(redis=redis, account_id=88)
    assert await guard.acquire()
    original_token = redis.values[guard.lease.key]
    redis.fail = True

    def broken_store(*args):
        raise PermissionError("offline shared store")

    monkeypatch.setattr(quarantine, "_lock", broken_store)
    try:
        with pytest.raises(quarantine.QuarantineUnavailable):
            await guard._quarantine(RuntimeError("wrong session ID"))
        assert guard.quarantined and not guard._fence_confirmed
        assert redis.values[guard.lease.key] == original_token
        assert guard.lease._renew_task is not None
        await guard.close()
        assert redis.values[guard.lease.key] == original_token
    finally:
        guard.lease.stop_renewal()


@pytest.mark.asyncio
async def test_corrupt_file_is_not_deleted_or_bypassed(tmp_path):
    path = tmp_path / "89.json"
    path.write_text("broken fence")
    with pytest.raises(quarantine.QuarantineUnavailable):
        await SessionLease(FakeRedis(), 89).acquire()
    assert path.read_text() == "broken fence"


@pytest.mark.asyncio
async def test_cancelled_publish_leaves_fence_and_releases_os_lock(tmp_path):
    class PausedRedis(FakeRedis):
        entered = asyncio.Event()

        async def eval(self, *args):
            result = await super().eval(*args)
            self.entered.set()
            await asyncio.Event().wait()
            return result

    redis = PausedRedis()
    task = asyncio.create_task(quarantine.publish(90, redis))
    await redis.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Redis CAS already durably fenced this account. A new process must block.
    assert redis.values["vanguard:telegram:lease:90"].startswith("quarantine:")
    healthy = FakeRedis()
    healthy.values.update(redis.values)
    assert not await SessionLease(healthy, 90).acquire()
    async with quarantine._locked(90):
        pass


@pytest.mark.asyncio
async def test_db_retry_closes_session_before_backoff(monkeypatch):
    active, attempts = 0, 0
    observed_sleeps = []

    class DB:
        async def execute(self, statement):
            if attempts < 3:
                raise TimeoutError("pool busy")
            return SimpleNamespace(rowcount=1)

    @asynccontextmanager
    async def session():
        nonlocal active, attempts
        attempts += 1
        active += 1
        try:
            yield DB()
        finally:
            active -= 1

    async def backoff(delay):
        assert active == 0
        observed_sleeps.append(delay)

    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr(quarantine.asyncio, "sleep", backoff)
    assert await quarantine.persist_error(91)
    assert attempts == 3 and active == 0 and len(observed_sleeps) == 2


@pytest.mark.asyncio
async def test_db_timeout_is_bounded_and_closes_cancelled_session(monkeypatch):
    active, attempts = 0, 0

    class DB:
        async def execute(self, statement):
            await asyncio.Event().wait()

    @asynccontextmanager
    async def session():
        nonlocal active, attempts
        active += 1
        attempts += 1
        try:
            yield DB()
        finally:
            active -= 1

    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr(quarantine, "DB_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(quarantine, "DB_RETRY_DELAYS", (0, 0, 0))
    assert not await quarantine.persist_error(92)
    assert active == 0 and attempts == 3


@pytest.mark.asyncio
async def test_cancelled_db_persistence_keeps_durable_quarantine(monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()
    redis = FakeRedis()

    class DB:
        async def execute(self, statement):
            entered.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def session():
        try:
            yield DB()
        finally:
            closed.set()

    monkeypatch.setattr("app.core.database.get_db_session", session)

    async def persist(*args):
        await quarantine.persist_error(93)

    guard = AccountSessionGuard(redis=redis, account_id=93, on_quarantine=persist)
    assert await guard.acquire()
    task = asyncio.create_task(guard._quarantine(RuntimeError("wrong session ID")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not await SessionLease(redis, 93).acquire()
    await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [AccountStatus.ONLINE, AccountStatus.BANNED, AccountStatus.RESTRICTED])
async def test_atomic_error_update_commits_without_overriding_risk_status(test_db, monkeypatch, original):
    test_db.add(TelegramAccount(id=94, identifier="quarantine-test", session_name="quarantine-test", status=original))
    await test_db.commit()

    @asynccontextmanager
    async def session():
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(test_db.bind, expire_on_commit=False) as db:
            try:
                yield db
                await db.commit()
            finally:
                await db.close()

    monkeypatch.setattr("app.core.database.get_db_session", session)
    assert await quarantine.persist_error(94) == (original == AccountStatus.ONLINE)
    status = await test_db.scalar(select(TelegramAccount.status).where(TelegramAccount.id == 94))
    assert status == (AccountStatus.ERROR if original == AccountStatus.ONLINE else original)


@pytest.mark.asyncio
async def test_recovery_clears_only_after_online_commit_and_read_transaction_release(test_db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.account.recovery import AccountRecoveryService, RecoveryResult
    from tests.unit.test_account_error_recovery import recovery_account

    await recovery_account(test_db)
    redis = FakeRedis()
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    fence = await quarantine.publish(35, redis)
    real_clear = quarantine._clear_locked
    observed = []

    async def clear(account_id, token, client):
        assert not test_db.in_transaction()
        assert token == fence.token
        async with AsyncSession(test_db.bind) as separate:
            status = await separate.scalar(select(TelegramAccount.status).where(TelegramAccount.id == 35))
        assert status == AccountStatus.ONLINE
        observed.append(True)
        return await real_clear(account_id, token, client)

    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        verify_error_account=AsyncMock(return_value=RecoveryResult(True, "identity_verified")),
        remove_account=AsyncMock(),
    )
    monkeypatch.setattr(quarantine, "_clear_locked", clear)
    result = await AccountRecoveryService(test_db, pool).recover(35)
    assert result.ok and observed
    assert pool.verify_error_account.await_args.kwargs["quarantine_token"] == fence.token
    assert await quarantine.current(35, redis) is None


@pytest.mark.asyncio
async def test_newer_fence_during_recovery_keeps_error_and_never_clears_it(test_db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.account.recovery import AccountRecoveryService, RecoveryResult
    from tests.unit.test_account_error_recovery import recovery_account

    await recovery_account(test_db)
    redis = FakeRedis()
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    first = await quarantine.publish(35, redis)
    newer_token = None

    async def verify(*args, **kwargs):
        nonlocal newer_token
        probe = SessionLease(redis, 35, quarantine_token=first.token)
        assert await probe.acquire()
        newer = await quarantine.publish(35, redis, lease_token=probe.token, owner=PROCESS_ID)
        newer_token = newer.token
        await probe.release()  # Cannot replace the newer sentinel with the old.
        return RecoveryResult(True, "identity_verified")

    @asynccontextmanager
    async def session():
        async with AsyncSession(test_db.bind) as db:
            yield db
            await db.commit()

    monkeypatch.setattr("app.core.database.get_db_session", session)
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), verify_error_account=verify, remove_account=AsyncMock())
    result = await AccountRecoveryService(test_db, pool).recover(35)
    assert not result.ok and result.reason == "session_quarantined"
    assert await quarantine.current(35, redis) == newer_token
    assert await test_db.scalar(select(TelegramAccount.status).where(TelegramAccount.id == 35)) == AccountStatus.ERROR
    pool.remove_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_pool_callback_cannot_modify_replacement_wrapper(monkeypatch):
    from app.core.account.pool import AccountPool

    pool = AccountPool()
    wrapper = await pool.add_account(
        account_id=95, phone="+10000000095", session_name="replacement-guard",
        country_code="US", api_id="1", api_hash="fake", status=AccountStatus.ONLINE,
    )
    wrapper.session_guard = SimpleNamespace(lease=SimpleNamespace(token="replacement"))
    persist = AsyncMock(side_effect=AssertionError("stale callback must not write DB"))
    monkeypatch.setattr(quarantine, "persist_error", persist)
    await pool._quarantine_session(95, RuntimeError("wrong session ID"), generation_token="retired")
    assert wrapper.status == AccountStatus.ONLINE and not wrapper.session_quarantined
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_fenced_account_never_constructs_an_unguarded_client(monkeypatch):
    from app.core.account.pool import AccountPool
    from app.core.account.session_guard import SessionQuarantined

    pool = AccountPool()
    wrapper = await pool.add_account(
        account_id=96, phone="+10000000096", session_name="fenced-client",
        country_code="US", api_id="1", api_hash="fake",
    )
    redis = FakeRedis()
    monkeypatch.setattr("app.core.redis.redis_client", redis)
    await quarantine.publish(96, redis)
    constructor = AsyncMock(side_effect=AssertionError("no client construction"))
    monkeypatch.setattr("app.core.account.pool.TelegramClient", constructor)
    with pytest.raises(SessionQuarantined):
        await pool._create_client(wrapper)
    constructor.assert_not_called()
    monkeypatch.setattr("app.core.redis.redis_client", None)
    with pytest.raises(quarantine.QuarantineUnavailable):
        await pool._create_client(wrapper)
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_delayed_error_retry_cannot_overwrite_completed_recovery(monkeypatch):
    redis = FakeRedis()
    fence = await quarantine.publish(97, redis)
    retrying, resume = asyncio.Event(), asyncio.Event()
    attempts = 0

    class DB:
        async def execute(self, statement):
            nonlocal attempts
            attempts += 1
            raise TimeoutError("first transaction failed")

    @asynccontextmanager
    async def session():
        yield DB()

    async def backoff(delay):
        retrying.set()
        await resume.wait()

    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr(quarantine.asyncio, "sleep", backoff)
    task = asyncio.create_task(quarantine.persist_error(97, token=fence.token, redis=redis))
    await retrying.wait()
    async with quarantine.recovery_scope(97, fence.token, redis) as clear:
        assert await clear()  # The successful ONLINE commit is already complete.
    resume.set()
    assert not await task
    assert attempts == 1  # No second UPDATE after recovery cleared the token.


@pytest.mark.asyncio
async def test_commit_to_clear_scope_blocks_same_token_error_writer(monkeypatch):
    redis = FakeRedis()
    fence = await quarantine.publish(98, redis)
    opened = 0

    @asynccontextmanager
    async def session():
        nonlocal opened
        opened += 1
        raise AssertionError("retired ERROR writer must not reach the DB")
        yield

    monkeypatch.setattr("app.core.database.get_db_session", session)
    async with quarantine.recovery_scope(98, fence.token, redis) as clear:
        task = asyncio.create_task(quarantine.persist_error(98, token=fence.token, redis=redis))
        assert await clear()
    assert not await task and opened == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["file", "redis"])
async def test_conditional_persistence_uses_remaining_store_without_unguarded_update(monkeypatch, store):
    redis = FailingRedis()
    fence = await quarantine.publish(99, redis)
    if store == "file":
        redis.fail = True
    else:
        quarantine._paths(99)[0].unlink()
    calls = 0

    class DB:
        async def execute(self, statement):
            nonlocal calls
            calls += 1
            return SimpleNamespace(rowcount=1)

    @asynccontextmanager
    async def session():
        yield DB()

    monkeypatch.setattr("app.core.database.get_db_session", session)
    assert await quarantine.persist_error(99, token=fence.token, redis=redis)
    assert calls == 1
    assert not await quarantine.persist_error(99, token="f" * 32, redis=redis)
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fsync", "cancel_unlink", "cancel_redis", "repeat_cancel"])
async def test_clear_failure_restores_durable_fence_before_unlock(monkeypatch, failure):
    import threading
    from pathlib import Path

    redis = FakeRedis()
    fence = await quarantine.publish(100, redis)
    entered, resume = threading.Event(), threading.Event()
    original_unlink, original_eval = Path.unlink, redis.eval

    if failure == "fsync":
        monkeypatch.setattr(quarantine, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("fsync")))
    elif failure in {"cancel_unlink", "repeat_cancel"}:
        def unlink(path, *args, **kwargs):
            if path.name == "100.json":
                entered.set()
                assert resume.wait(2)
            return original_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", unlink)
    else:
        async def delayed_eval(script, *args):
            result = await original_eval(script, *args)
            if "quarantine_compare_delete" in script:
                entered.set()
                await asyncio.to_thread(resume.wait, 2)
            return result
        monkeypatch.setattr(redis, "eval", delayed_eval)

    task = asyncio.create_task(quarantine.clear_after_recovery(100, fence.token, redis))
    if failure == "fsync":
        with pytest.raises(OSError):
            await task
    else:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        if failure == "repeat_cancel":
            await asyncio.sleep(0)
            task.cancel()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await quarantine.current(100, redis) == fence.token
    assert not await SessionLease(redis, 100).acquire()


@pytest.mark.asyncio
async def test_fixed_pending_file_replaces_one_crash_leftover(tmp_path):
    (tmp_path / ".101.json.pending").write_bytes(b"interrupted previous write")
    fence = await quarantine.publish(101, FakeRedis())
    assert json.loads((tmp_path / "101.json").read_text())["token"] == fence.token
    assert not tuple(tmp_path.glob("*.pending"))


@pytest.mark.asyncio
async def test_late_redis_clear_cannot_remove_last_fence_when_restore_disk_fails(monkeypatch):
    class LateApplyRedis(FakeRedis):
        pending = None

        async def eval(self, script, *args):
            if "quarantine_compare_delete" in script:
                self.pending = (script, args)
                raise TimeoutError("response unavailable; server may still apply")
            return await super().eval(script, *args)

    redis = LateApplyRedis()
    fence = await quarantine.publish(102, redis)

    def disk_failed(*args):
        raise OSError("disk is no longer writable")

    monkeypatch.setattr(quarantine, "_write", disk_failed)
    with pytest.raises(TimeoutError):
        await quarantine.clear_after_recovery(102, fence.token, redis)
    script, args = redis.pending
    assert await FakeRedis.eval(redis, script, *args)
    assert await quarantine.current(102, redis) == fence.token
    assert not await SessionLease(redis, 102).acquire()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel_after_commit", "clear_fsync"])
async def test_previously_unfenced_recovery_failure_after_online_still_stays_error(
    test_db, monkeypatch, failure,
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.account.manager import AccountManager
    from app.core.account.recovery import AccountRecoveryService, RecoveryResult
    from tests.unit.test_account_error_recovery import recovery_account

    await recovery_account(test_db)
    redis = FakeRedis()
    monkeypatch.setattr("app.core.redis.redis_client", redis)

    @asynccontextmanager
    async def session():
        async with AsyncSession(test_db.bind) as db:
            yield db
            await db.commit()

    original_update = AccountManager.update_account_status

    async def commit(manager, account_id, status):
        result = await original_update(manager, account_id, status)
        if failure == "cancel_after_commit":
            raise asyncio.CancelledError()
        monkeypatch.setattr(quarantine, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("fsync")))
        return result

    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr(AccountManager, "update_account_status", commit)
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        verify_error_account=AsyncMock(return_value=RecoveryResult(True, "identity_verified")),
        remove_account=AsyncMock(),
    )
    if failure == "cancel_after_commit":
        with pytest.raises(asyncio.CancelledError):
            await AccountRecoveryService(test_db, pool).recover(35)
    else:
        result = await AccountRecoveryService(test_db, pool).recover(35)
        assert not result.ok
    assert await quarantine.current(35, redis) is not None
    assert not await SessionLease(redis, 35).acquire()
    assert await test_db.scalar(select(TelegramAccount.status).where(TelegramAccount.id == 35)) == AccountStatus.ERROR
    pool.remove_account.assert_not_awaited()
