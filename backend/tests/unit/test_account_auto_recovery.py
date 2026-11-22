"""Automatic recovery never relaxes ERROR gates or resets durable attempts."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account import auto_recovery as module
from app.core.account.auto_recovery import (
    LOCK_PREFIX,
    MAX_ATTEMPTS,
    MAX_WAIT_RESULTS,
    STATE_PREFIX,
    AccountAutoRecovery,
    AutoRecoveryStore,
    FailureTrigger,
    load_failure_triggers,
)
from app.core.account.models import AccountEnvironmentEvent, AccountStatus, TelegramAccount
from app.core.account.recovery import RecoveryResult


class Redis:
    def __init__(self):
        self.values, self.expiry = {}, {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key], self.expiry[key] = value, ex
        return True

    async def eval(self, script, key_count, *args):
        keys, values = args[:key_count], args[key_count:]
        if self.values.get(keys[0]) != values[0]:
            return 0
        if "auto_recovery_write_v1" in script:
            self.values[keys[1]], self.expiry[keys[1]] = values[1], None
            return 1
        self.values.pop(keys[0], None)
        return 1


def account(account_id=7, *, status=AccountStatus.ERROR, active=True, enabled=True):
    return SimpleNamespace(id=account_id, status=status, is_active=active,
                           operation_config=SimpleNamespace(enabled=enabled))


def trigger(account_id=7, event_id=9, generation="a" * 32):
    return FailureTrigger(account_id, event_id, "session_conflict", generation,
                          "2026-11-05T13:00:00", generation)


async def setup(monkeypatch, *, verify=None):
    redis, now = Redis(), [1000.0]
    selected = trigger()
    monkeypatch.setattr(module, "load_failure_triggers", AsyncMock(return_value={7: selected}))
    controller = AccountAutoRecovery(object(), redis=redis, clock=lambda: now[0])
    controller._verify = verify or AsyncMock(return_value=RecoveryResult(False, "network_unavailable"))
    return controller, redis, now, selected


async def complete(controller):
    assert controller._task is not None
    await asyncio.wait_for(controller._task, timeout=2)


@pytest.mark.asyncio
async def test_normal_disabled_and_inactive_accounts_never_enter_recovery(monkeypatch):
    controller, _, _, _ = await setup(monkeypatch)
    await controller.tick([account(status=AccountStatus.ONLINE), account(active=False), account(enabled=False),
                           account(status=AccountStatus.BANNED), account(status=AccountStatus.RESTRICTED)])
    module.load_failure_triggers.assert_awaited_once_with([])
    controller._verify.assert_not_awaited()
    assert controller.snapshot()["accounts"] == []


@pytest.mark.asyncio
async def test_backoff_and_three_attempt_limit_survive_controller_restarts(monkeypatch):
    controller, redis, now, _ = await setup(monkeypatch)
    await controller.tick([account()])
    assert controller.snapshot()["accounts"][0]["attempts"] == 0
    assert controller._task is None
    assert redis.expiry[f"{STATE_PREFIX}7"] is None
    calls = []
    for due, count in ((1060, 1), (1360, 2), (2260, 3)):
        controller = AccountAutoRecovery(object(), redis=redis, clock=lambda: now[0])
        controller._verify = AsyncMock(return_value=RecoveryResult(False, "network_unavailable"))
        now[0] = due - 1
        await controller.tick([account()])
        controller._verify.assert_not_awaited()
        now[0] = due
        await controller.tick([account()])
        await complete(controller)
        calls.append(controller._verify.await_count)
        persisted = json.loads(redis.values[f"{STATE_PREFIX}7"])
        assert persisted["attempts"] == count
    assert calls == [1, 1, 1]
    now[0] += 8 * 24 * 3600
    await controller.tick([account()])
    assert controller.snapshot()["accounts"][0]["manual_required"] is True
    assert json.loads(redis.values[f"{STATE_PREFIX}7"])["attempts"] == MAX_ATTEMPTS
    assert controller._task is None
    assert set(redis.values) == {f"{STATE_PREFIX}7"}


@pytest.mark.asyncio
async def test_cancellation_keeps_pre_reserved_attempt_and_backoff(monkeypatch):
    entered = asyncio.Event()

    async def slow(_):
        entered.set()
        await asyncio.Event().wait()

    controller, redis, now, _ = await setup(monkeypatch, verify=slow)
    await controller.tick([account()])
    now[0] = 1060
    await controller.tick([account()])
    await entered.wait()
    assert json.loads(redis.values[f"{STATE_PREFIX}7"])["attempts"] == 1
    await controller.close()
    restarted = AccountAutoRecovery(object(), redis=redis, clock=lambda: now[0])
    restarted._verify = AsyncMock(return_value=RecoveryResult(True, "identity_verified"))
    await restarted.tick([account()])
    restarted._verify.assert_not_awaited()
    now[0] = 1360
    await restarted.tick([account()])
    await complete(restarted)
    assert json.loads(redis.values[f"{STATE_PREFIX}7"])["attempts"] == 2
    assert restarted.snapshot()["accounts"][0]["state"] == "recovered"


@pytest.mark.asyncio
async def test_two_workers_cannot_probe_same_account_and_tick_stays_responsive(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(_):
        entered.set()
        await release.wait()
        return RecoveryResult(True, "identity_verified")

    first, redis, now, _ = await setup(monkeypatch, verify=slow)
    second = AccountAutoRecovery(object(), redis=redis, clock=lambda: now[0])
    second._verify = AsyncMock(return_value=RecoveryResult(True, "identity_verified"))
    await first.tick([account()])
    now[0] = 1060
    await first.tick([account()])
    await entered.wait()
    await asyncio.wait_for(second.tick([account()]), timeout=0.2)
    await asyncio.wait_for(first.tick([account()]), timeout=0.2)
    second._verify.assert_not_awaited()
    assert first.snapshot()["in_progress"] is True
    release.set()
    await complete(first)
    assert json.loads(redis.values[f"{STATE_PREFIX}7"])["attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["read_budget_wait", "telegram_cooldown", "lease_busy", "static_proxy_unavailable"])
async def test_known_waits_refund_attempt_and_are_bounded_without_busy_polling(monkeypatch, reason):
    controller, redis, now, _ = await setup(monkeypatch, verify=AsyncMock(return_value=RecoveryResult(False, reason)))
    await controller.tick([account()])
    now[0] = 1060
    await controller.tick([account()])
    await complete(controller)
    state = json.loads(redis.values[f"{STATE_PREFIX}7"])
    assert state["attempts"] == 0 and state["waits"] == 1 and state["next_at"] >= 1360
    now[0] += 1
    await controller.tick([account()])
    assert controller._verify.await_count == 1
    state["waits"] = MAX_WAIT_RESULTS
    redis.values[f"{STATE_PREFIX}7"] = json.dumps(state)
    now[0] = state["next_at"]
    await controller.tick([account()])
    assert controller.snapshot()["accounts"][0]["state"] == "exhausted"
    assert controller._verify.await_count == 1


@pytest.mark.asyncio
async def test_waits_expire_after_twenty_four_hours(monkeypatch):
    controller, _, now, _ = await setup(monkeypatch)
    await controller.tick([account()])
    now[0] += module.EPISODE_SECONDS
    await controller.tick([account()])
    controller._verify.assert_not_awaited()
    assert controller.snapshot()["accounts"][0]["state"] == "exhausted"


@pytest.mark.asyncio
async def test_ownership_loss_after_identity_read_keeps_attempt_consumed(monkeypatch):
    controller, redis, now, _ = await setup(monkeypatch, verify=AsyncMock(
        return_value=RecoveryResult(False, "operation_lease_lost")))
    await controller.tick([account()])
    now[0] = 1060
    await controller.tick([account()])
    await complete(controller)
    state = json.loads(redis.values[f"{STATE_PREFIX}7"])
    assert state["attempts"] == 1 and state["waits"] == 0


@pytest.mark.asyncio
async def test_server_cooldown_deadline_is_respected(monkeypatch):
    controller, redis, now, _ = await setup(monkeypatch, verify=AsyncMock(
        return_value=RecoveryResult(False, "telegram_cooldown", retry_after_seconds=3600)))
    await controller.tick([account()])
    now[0] = 1060
    await controller.tick([account()])
    await complete(controller)
    assert json.loads(redis.values[f"{STATE_PREFIX}7"])["next_at"] == 4660


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["identity_mismatch", "session_reauthentication_required", "account_frozen", "verification_failed"])
async def test_terminal_or_unknown_verification_failure_never_retries(monkeypatch, reason):
    controller, redis, now, _ = await setup(monkeypatch, verify=AsyncMock(return_value=RecoveryResult(False, reason)))
    await controller.tick([account()])
    now[0] = 1060
    await controller.tick([account()])
    await complete(controller)
    now[0] += 3600
    await controller.tick([account()])
    assert controller._verify.await_count == 1
    assert json.loads(redis.values[f"{STATE_PREFIX}7"])["state"] == "manual_required"
    assert controller.snapshot()["accounts"][0]["reason"] == reason


@pytest.mark.asyncio
async def test_corrupt_state_and_redis_outage_cannot_reset_attempts_or_probe(monkeypatch):
    controller, redis, now, _ = await setup(monkeypatch)
    redis.values[f"{STATE_PREFIX}7"] = "corrupt counter"
    await controller.tick([account()])
    assert controller.snapshot()["accounts"][0]["reason"] == "recovery_state_unavailable"
    controller._verify.assert_not_awaited()
    redis.set = AsyncMock(side_effect=OSError("private credential text"))
    now[0] += 100000
    await controller.tick([account()])
    controller._verify.assert_not_awaited()
    assert "private" not in str(controller.snapshot())


@pytest.mark.asyncio
async def test_nonfinite_state_cannot_wait_forever_or_start_another_probe(monkeypatch):
    controller, redis, _, selected = await setup(monkeypatch)
    state = module.initial_state(selected, 1000)
    state["next_at"] = float("nan")
    redis.values[f"{STATE_PREFIX}7"] = json.dumps(state)
    await controller.tick([account()])
    assert controller.snapshot()["accounts"][0]["reason"] == "recovery_state_unavailable"
    controller._verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_lock_cannot_overwrite_new_state_or_release_new_owner():
    redis, store = Redis(), AutoRecoveryStore(Redis())
    store.redis = redis
    assert await store.acquire(7, "old")
    redis.values[f"{LOCK_PREFIX}7"] = "new"
    with pytest.raises(module.RecoveryStateUnavailable):
        await store.write(7, "old", module.initial_state(trigger(), 1000))
    await store.release(7, "old")
    assert redis.values[f"{LOCK_PREFIX}7"] == "new"
    assert f"{STATE_PREFIX}7" not in redis.values


@pytest.mark.asyncio
async def test_pool_identity_probe_does_not_block_heartbeat_or_allow_second_probe(monkeypatch, tmp_path):
    from app.core.account import pool as pool_module
    from tests.unit.test_account_error_recovery import probe_pool

    pool, _, _, _ = await probe_pool(monkeypatch, tmp_path)
    client_base = pool_module.TelegramClient
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowClient(client_base):
        async def connect(self):
            entered.set()
            await release.wait()
            await super().connect()

    monkeypatch.setattr(pool_module, "TelegramClient", SlowClient)
    task = asyncio.create_task(pool.verify_error_account(35, expected_phone="+10000000035", expected_proxy_id=30))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        heartbeat = await asyncio.wait_for(pool.health_check(), timeout=0.2)
        assert heartbeat["error"] == 1 and heartbeat["schedulable"] == 0
        second = await asyncio.wait_for(pool.verify_error_account(
            35, expected_phone="+10000000035", expected_proxy_id=30), timeout=0.2)
        assert second == RecoveryResult(False, "lease_busy")
    finally:
        release.set()
        await task
    assert task.result().ok


@pytest.mark.asyncio
async def test_probe_receives_generation_and_runs_outside_candidate_read_transaction(monkeypatch):
    events = []
    db = object()

    @asynccontextmanager
    async def session():
        events.append("open")
        try:
            yield db
        finally:
            events.append("close")

    service = SimpleNamespace(recover=AsyncMock(return_value=RecoveryResult(True, "identity_verified")))
    monkeypatch.setattr(module, "get_db_session", session)
    monkeypatch.setattr(module, "AccountRecoveryService", lambda actual_db, pool: service)
    controller = AccountAutoRecovery(object(), redis=Redis())
    selected = trigger()
    assert (await controller._verify(selected)).ok
    service.recover.assert_awaited_once_with(7, source="automatic_error_recovery",
        expected_error_updated_at=selected.error_updated_at, expected_failure_id=selected.event_id)
    assert events == ["open", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "stale_timestamp", "untrusted", "unknown", "fatal_probe", "old_success", "transient_probe", "auto_cancel", "explicit_cancel"])
async def test_only_latest_trusted_error_generation_can_trigger(test_db, monkeypatch, case):
    updated = datetime(2026, 11, 5, 13, 0, 0)
    actual = TelegramAccount(id=7, identifier="auto-recovery-test", session_name="auto-recovery-test",
                             status=AccountStatus.ERROR, updated_at=updated)
    test_db.add(actual)
    await test_db.commit()
    details = {"source": "account_pool", "quarantine_token": "a" * 32, "error_updated_at": updated.isoformat()}
    if case == "stale_timestamp":
        details["error_updated_at"] = (updated - timedelta(seconds=1)).isoformat()
    elif case == "untrusted":
        details["source"] = "manual"
    failure = AccountEnvironmentEvent(id=9, account_id=7, event_type="account_connection_failure", status="failed",
        reason="unknown_failure" if case == "unknown" else "session_conflict", details=json.dumps(details))
    test_db.add(failure)
    if case in {"fatal_probe", "old_success", "transient_probe", "auto_cancel", "explicit_cancel"}:
        test_db.add(AccountEnvironmentEvent(id=10, account_id=7, event_type="session_recovery",
            status="ok" if case == "old_success" else "failed", reason={
                "fatal_probe": "identity_mismatch", "old_success": "identity_verified",
                "transient_probe": "connection_timeout", "auto_cancel": "verification_cancelled",
                "explicit_cancel": "verification_cancelled"}[case], details=json.dumps({
                    "source": "automatic_error_recovery" if case == "auto_cancel" else "explicit_error_recovery"})))
    await test_db.commit()

    @asynccontextmanager
    async def session():
        yield test_db
        await test_db.rollback()

    monkeypatch.setattr(module, "get_db_session", session)
    result = await load_failure_triggers([SimpleNamespace(id=7, updated_at=updated)])
    if case in {"stale_timestamp", "untrusted"}:
        assert result == {}
    else:
        assert result[7].blocked_reason == {
            "unknown": "unknown_failure", "fatal_probe": "identity_mismatch",
            "old_success": "unclassified_or_stale_error",
            "explicit_cancel": "verification_cancelled",
        }.get(case)
    assert test_db.in_transaction() is False
