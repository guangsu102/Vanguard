"""Exercise the real dispatcher entry before any local client can be acquired."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.account import session_exec
from app.core.account.session_lease import OWNER_PREFIX, PROCESS_ID, SessionLease
from app.core.scheduler import growth_dispatch as dispatcher

pytestmark = pytest.mark.asyncio

ACCOUNT_ID = 34
OWNER_KEY = f"{OWNER_PREFIX}{ACCOUNT_ID}"
LEASE_KEY = SessionLease(None, ACCOUNT_ID).key


from tests.fakes.session_exec_redis import FakeRedis


class OwnershipRedis(FakeRedis):
    """In-memory ownership plus the real forwarding channel's list interface."""

    def __init__(self, owner=None, lease=None):
        super().__init__()
        self.values = self.strings
        if owner is not None:
            self.values[OWNER_KEY] = owner
        if lease is not None:
            self.values[LEASE_KEY] = lease
        self.lists = {}
        self.snapshots = []
        self.before_snapshot = None
        self.get_error = None
        self.snapshot_error = None
        self.snapshot_result = ...

    async def get(self, key):
        if self.get_error is not None:
            raise self.get_error
        return self.values.get(key)

    async def mget(self, keys):
        self.snapshots.append(tuple(keys))
        if self.snapshot_error is not None:
            raise self.snapshot_error
        if self.before_snapshot is not None:
            self.before_snapshot()
        if self.snapshot_result is not ...:
            return self.snapshot_result
        return [self.values.get(key) for key in keys]

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def rpop(self, key):
        items = self.lists.get(key, [])
        return items.pop() if items else None

    async def brpop(self, key, timeout=1):
        deadline = time.monotonic() + timeout + 0.05
        while time.monotonic() < deadline:
            value = await self.rpop(key)
            if value is not None:
                return key, value
            await asyncio.sleep(0.001)
        return None

    async def expire(self, key, seconds):
        # The forwarding protocol expires reply lists only. No session key is
        # mutated or force-released by either this test or the dispatcher.
        assert key.startswith(session_exec.REPLY_PREFIX)
        return True


@pytest.fixture
def entry(monkeypatch):
    from app.core import config, redis
    from app.core.account import pool
    from app.modules.acquisition import automation, qualification_service

    redis_state = OwnershipRedis()
    account_pool = object()
    account = SimpleNamespace(id=ACCOUNT_ID)
    db = SimpleNamespace(get=AsyncMock(return_value=account), commit=AsyncMock())
    get_pool = Mock(return_value=account_pool)
    construct = Mock(side_effect=lambda db, account_pool=None: SimpleNamespace(
        db=db, account_pool=account_pool))
    run_reviews = AsyncMock(return_value={"processed": 1, "sentinel": "local-stage"})
    forwarding = AsyncMock(return_value={"error": "not_owner"})
    monkeypatch.setattr(redis, "get_redis", AsyncMock(return_value=redis_state))
    monkeypatch.setattr(pool, "get_account_pool", get_pool)
    monkeypatch.setattr(config, "settings", SimpleNamespace(
        p2_outbox_enabled_for=lambda kind, account_id: False))
    monkeypatch.setattr(qualification_service, "account_block_reason", lambda account, now: None)
    monkeypatch.setattr(qualification_service, "run_reviews", run_reviews)
    monkeypatch.setattr(automation, "AcquisitionAutomationService", construct)
    monkeypatch.setattr(session_exec, "forward", forwarding)
    return SimpleNamespace(redis=redis_state, pool=account_pool, db=db,
        get_pool=get_pool, construct=construct, run_reviews=run_reviews,
        forwarding=forwarding)


def assert_wait(entry, result, reason):
    assert result == ({"processed": 0, "reason": reason, "retryable": True}, 30)
    entry.get_pool.assert_not_called()
    entry.construct.assert_not_called()
    entry.run_reviews.assert_not_awaited()


async def test_paused_owner_real_forward_cannot_trigger_local_client(entry, monkeypatch):
    # The owner has disconnected its transport but retained the auth-key lease.
    entry.redis.values.update({OWNER_KEY: "listener-owner", LEASE_KEY: "retained-generation"})
    original = dict(entry.redis.values)
    from app.core.account.session_exec import serve_requests
    owner_stage = AsyncMock()
    server = asyncio.create_task(serve_requests(entry.redis, owns=lambda account_id: False,
        execute_stage=owner_stage, poll_interval=0.001))
    async def send(account_id, stage):
        return await _REAL_FORWARD(account_id, stage, client=entry.redis, timeout=1)
    monkeypatch.setattr(session_exec, "forward", send)
    try:
        result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert_wait(entry, result, "session_owner_not_connected")
    assert entry.redis.snapshots == [(OWNER_KEY, LEASE_KEY)]
    assert entry.redis.values == original
    owner_stage.assert_not_awaited()


@pytest.mark.parametrize("owner,lease", [
    ("listener-owner", "retained"), (None, "retained"), ("new-owner", "new-generation"),
    ("listener-owner", None), ("", ""), (None, b""), (b"", None),
])
async def test_not_owner_reply_rechecks_both_live_keys(entry, owner, lease):
    entry.redis.values[OWNER_KEY] = "old-owner"
    async def decline(account_id, stage):
        entry.redis.values.clear()
        if owner is not None:
            entry.redis.values[OWNER_KEY] = owner
        if lease is not None:
            entry.redis.values[LEASE_KEY] = lease
        return {"error": "not_owner"}
    entry.forwarding.side_effect = decline
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_not_connected")
    entry.forwarding.assert_awaited_once_with(ACCOUNT_ID, "review")
    assert entry.redis.snapshots == [(OWNER_KEY, LEASE_KEY)]


async def test_dead_owner_natural_expiry_allows_local_stage(entry):
    entry.redis.values.update({OWNER_KEY: "dead-owner", LEASE_KEY: "expired-generation"})
    async def expired_owner(account_id, stage):
        entry.redis.values.clear()  # Simulate natural TTL expiry before snapshot.
        return {"error": "not_owner"}
    entry.forwarding.side_effect = expired_owner
    result, interval = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert result == {"processed": 1, "sentinel": "local-stage"}
    assert interval == dispatcher.STAGES["review"].interval
    entry.run_reviews.assert_awaited_once()
    assert entry.construct.call_args.kwargs["account_pool"] is entry.pool


async def test_initial_missing_registry_still_checks_live_lease(entry):
    entry.redis.values[LEASE_KEY] = "owner-marker-not-published"
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_not_connected")
    entry.forwarding.assert_not_awaited()


async def test_owner_appearing_after_initial_lookup_blocks_fallback(entry):
    entry.redis.before_snapshot = lambda: entry.redis.values.update({
        OWNER_KEY: "replacement-owner", LEASE_KEY: "replacement-generation"})
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_not_connected")


async def test_initial_missing_both_keys_allows_local_stage(entry):
    result, _ = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert result["sentinel"] == "local-stage"
    assert entry.redis.snapshots == [(OWNER_KEY, LEASE_KEY)]
    entry.forwarding.assert_not_awaited()


async def test_same_owner_explicit_original_pool_keeps_local_reuse(entry):
    entry.redis.values.update({OWNER_KEY: PROCESS_ID, LEASE_KEY: "our-generation"})
    entry.redis.snapshot_error = AssertionError("owner path must not use fallback")
    result, _ = await dispatcher.execute(entry.db, "review", ACCOUNT_ID, account_pool=entry.pool)
    assert result["sentinel"] == "local-stage"
    assert entry.construct.call_args.kwargs["account_pool"] is entry.pool
    entry.get_pool.assert_not_called()
    entry.forwarding.assert_not_awaited()
    assert entry.redis.snapshots == []


async def test_same_owner_without_explicit_pool_cannot_create_unrelated_pool(entry):
    entry.redis.values.update({OWNER_KEY: PROCESS_ID, LEASE_KEY: "our-generation"})
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_not_connected")


async def test_redis_connection_failure_cannot_fall_back(entry, monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(side_effect=OSError("offline")))
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_unavailable")


@pytest.mark.parametrize("snapshot_has_lease", [True, False])
async def test_swallowed_registry_error_requires_successful_atomic_confirmation(entry, snapshot_has_lease):
    entry.redis.get_error = OSError("initial GET failed")
    if snapshot_has_lease:
        entry.redis.values[LEASE_KEY] = "retained"
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    if snapshot_has_lease:
        assert_wait(entry, result, "session_owner_not_connected")
    else:
        assert result[0]["sentinel"] == "local-stage"
    assert entry.redis.snapshots == [(OWNER_KEY, LEASE_KEY)]


async def test_atomic_snapshot_failure_is_fail_closed(entry):
    entry.redis.snapshot_error = OSError("MGET failed")
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_unavailable")


@pytest.mark.parametrize("snapshot", [None, [], [None], {"owner": None}, "invalid"])
async def test_malformed_atomic_snapshot_is_fail_closed(entry, snapshot):
    entry.redis.snapshot_result = snapshot
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert_wait(entry, result, "session_owner_unavailable")


async def test_atomic_snapshot_cancellation_propagates(entry):
    entry.redis.snapshot_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    entry.construct.assert_not_called()


async def test_connected_foreign_owner_result_unchanged(entry):
    entry.redis.values.update({OWNER_KEY: "connected-owner", LEASE_KEY: "retained"})
    entry.forwarding.return_value = {"result": {"processed": 2}, "interval": 45}
    result = await dispatcher.execute(entry.db, "review", ACCOUNT_ID)
    assert result == ({"processed": 2}, 45)
    assert entry.redis.snapshots == []
    entry.construct.assert_not_called()


# Keep the real channel entry before fixtures replace its module attribute.
_REAL_FORWARD = session_exec.forward
