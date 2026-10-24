"""Session-bound stage quanta are forwarded to the process owning the session."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.account import session_exec
from app.core.account.session_exec import forward, owner_id, serve_requests


class FakeRedis:
    """Minimal async Redis with the list/TTL operations the channel uses."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        self.ttl[key] = ex or 0
        return True

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def lpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def brpop(self, key: str, timeout: int = 5) -> tuple[str, str] | None:
        deadline = time.monotonic() + max(0, int(timeout)) + 0.05
        while time.monotonic() < deadline:
            items = self.lists.get(key)
            if items:
                return key, items.pop()
            await asyncio.sleep(0.01)
        return None

    async def rpop(self, key: str) -> str | None:
        items = self.lists.get(key)
        if not items:
            return None
        return items.pop()

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttl[key] = seconds
        return key in self.strings or key in self.lists


async def _start_server(client: FakeRedis, owns, execute_stage):
    return asyncio.create_task(serve_requests(client, owns=owns, execute_stage=execute_stage))


@pytest.mark.asyncio
async def test_forward_returns_owner_result():
    client = FakeRedis()
    seen: list[tuple[int, str]] = []

    async def execute_stage(account_id: int, stage: str):
        seen.append((account_id, stage))
        return {"processed": 2, "details": [{"status": "left"}]}, 45

    server = await _start_server(client, owns=lambda a: a == 7, execute_stage=execute_stage)
    try:
        reply = await forward(7, "exit", client=client, timeout=5)
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)

    assert reply == {"result": {"processed": 2, "details": [{"status": "left"}]}, "interval": 45}
    assert seen == [(7, "exit")]


@pytest.mark.asyncio
async def test_owner_declines_accounts_it_does_not_hold():
    client = FakeRedis()
    calls = []

    async def execute_stage(account_id: int, stage: str):
        calls.append(account_id)
        return {"processed": 0}, 30

    server = await _start_server(client, owns=lambda a: a == 7, execute_stage=execute_stage)
    try:
        reply = await forward(8, "ads", client=client, timeout=5)
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)

    assert reply == {"error": "not_owner"}
    assert calls == []


@pytest.mark.asyncio
async def test_stage_failure_is_reported_not_dropped():
    client = FakeRedis()

    async def execute_stage(account_id: int, stage: str):
        raise RuntimeError("boom")

    server = await _start_server(client, owns=lambda a: True, execute_stage=execute_stage)
    try:
        reply = await forward(3, "review", client=client, timeout=5)
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)

    assert reply["error"] == "execution_failed"
    assert reply["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_forward_without_owner_times_out():
    client = FakeRedis()
    assert await forward(5, "ads", client=client, timeout=1) is None


@pytest.mark.asyncio
async def test_owner_id_reads_the_registry():
    client = FakeRedis()
    await client.set("vanguard:telegram:owner:5", "proc-a", ex=90)
    assert await owner_id(client, 5) == "proc-a"
    assert await owner_id(client, 6) is None


@pytest.mark.asyncio
async def test_execute_forwards_to_foreign_owner(monkeypatch):
    from app.core.scheduler import growth_dispatch

    account = type("Account", (), {})()

    async def fake_get_redis():
        return FakeRedis()

    async def fake_owner_id(client, account_id):
        return "another-process"

    async def fake_forward(account_id, stage, **kwargs):
        return {"result": {"processed": 1, "succeeded": 1}, "interval": 90}

    monkeypatch.setattr("app.core.redis.get_redis", fake_get_redis)
    monkeypatch.setattr(session_exec, "owner_id", fake_owner_id)
    monkeypatch.setattr(session_exec, "forward", fake_forward)

    db = AsyncMock()
    db.get = AsyncMock(return_value=account)
    import app.modules.acquisition.qualification_service as qualification_service
    monkeypatch.setattr(
        qualification_service, "account_block_reason", lambda account, now: None
    )

    result, interval = await growth_dispatch.execute(db, "ads", 5)
    assert result == {"processed": 1, "succeeded": 1}
    assert interval == 90


@pytest.mark.asyncio
async def test_execute_defers_when_owner_unreachable(monkeypatch):
    from app.core.scheduler import growth_dispatch

    async def fake_get_redis():
        return FakeRedis()

    async def fake_owner_id(client, account_id):
        return "another-process"

    async def fake_forward(account_id, stage, **kwargs):
        return None

    monkeypatch.setattr("app.core.redis.get_redis", fake_get_redis)
    monkeypatch.setattr(session_exec, "owner_id", fake_owner_id)
    monkeypatch.setattr(session_exec, "forward", fake_forward)

    db = AsyncMock()
    db.get = AsyncMock(return_value=object())
    import app.modules.acquisition.qualification_service as qualification_service
    monkeypatch.setattr(
        qualification_service, "account_block_reason", lambda account, now: None
    )

    result, interval = await growth_dispatch.execute(db, "review", 5)
    assert result == {"processed": 0, "reason": "session_owner_unreachable"}
    assert interval == 30


@pytest.mark.asyncio
async def test_execute_falls_back_locally_on_stale_owner(monkeypatch):
    from app.core.scheduler import growth_dispatch

    async def fake_get_redis():
        return FakeRedis()

    async def fake_owner_id(client, account_id):
        return "another-process"

    forwarded = []

    async def fake_forward(account_id, stage, **kwargs):
        forwarded.append(account_id)
        return {"error": "not_owner"}

    monkeypatch.setattr("app.core.redis.get_redis", fake_get_redis)
    monkeypatch.setattr(session_exec, "owner_id", fake_owner_id)
    monkeypatch.setattr(session_exec, "forward", fake_forward)

    db = AsyncMock()
    db.get = AsyncMock(return_value=object())
    import app.modules.acquisition.qualification_service as qualification_service
    monkeypatch.setattr(
        qualification_service, "account_block_reason", lambda account, now: None
    )
    local_result = {"processed": 3, "details": []}

    class FakeService:
        def __init__(self, db, account_pool=None):
            pass

        async def reconcile_join_requests(self, *, account_id=None, limit=None):
            return local_result

    import app.modules.acquisition.automation as automation
    monkeypatch.setattr(automation, "AcquisitionAutomationService", FakeService)

    result, interval = await growth_dispatch.execute(db, "reconcile", 5)
    assert forwarded == [5]
    assert result is local_result


@pytest.mark.asyncio
async def test_pool_peek_connected_reports_only_live_clients():
    from types import SimpleNamespace

    from app.core.account.pool import AccountPool

    pool = AccountPool()
    wrapper = SimpleNamespace(account_id=11, client=None)
    pool._accounts[0] = wrapper
    assert pool.peek_connected(11) is False

    wrapper.client = SimpleNamespace(is_connected=lambda: True)
    assert pool.peek_connected(11) is True
    assert pool.peek_connected(12) is False


@pytest.mark.asyncio
async def test_different_accounts_execute_concurrently():
    """A fleet of accounts must not queue behind each other's stages."""
    client = FakeRedis()
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def execute_stage(account_id: int, stage: str):
        if account_id == 1:
            first_entered.set()
            await release.wait()
        return {"processed": 1}, 30

    server = asyncio.create_task(
        serve_requests(
            client, owns=lambda a: True, execute_stage=execute_stage,
            poll_interval=0.01, max_concurrency=2,
        )
    )
    try:
        blocked = asyncio.create_task(forward(1, "review", client=client, timeout=5))
        await asyncio.wait_for(first_entered.wait(), 2)
        # Account 2 must finish while account 1 is still blocked.
        other = await asyncio.wait_for(forward(2, "review", client=client, timeout=5), 2)
        assert other["result"] == {"processed": 1}
    finally:
        release.set()
        server.cancel()
        await asyncio.gather(server, blocked, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_account_stays_serial():
    client = FakeRedis()
    first_entered = asyncio.Event()
    release = asyncio.Event()
    second_started = False

    async def execute_stage(account_id: int, stage: str):
        nonlocal second_started
        if stage == "first":
            first_entered.set()
            await release.wait()
        second_started = second_started or stage == "second"
        return {"processed": 1}, 30

    server = asyncio.create_task(
        serve_requests(
            client, owns=lambda a: True, execute_stage=execute_stage,
            poll_interval=0.01, max_concurrency=2,
        )
    )
    try:
        first = asyncio.create_task(forward(1, "first", client=client, timeout=5))
        await asyncio.wait_for(first_entered.wait(), 2)
        second = asyncio.create_task(forward(1, "second", client=client, timeout=5))
        # The same account's second stage must not start while the first runs.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.3)
        assert not second_started
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        server.cancel()
        await asyncio.gather(server, first, second, return_exceptions=True)
    assert await first is not None and await second is not None


@pytest.mark.asyncio
async def test_concurrency_cap_bounds_inflight_stages():
    client = FakeRedis()
    running = 0
    peak = 0
    release = asyncio.Event()

    async def execute_stage(account_id: int, stage: str):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return {"processed": 1}, 30

    server = asyncio.create_task(
        serve_requests(
            client, owns=lambda a: True, execute_stage=execute_stage,
            poll_interval=0.01, max_concurrency=2,
        )
    )
    try:
        tasks = [
            asyncio.create_task(forward(a, "review", client=client, timeout=5))
            for a in (1, 2, 3, 4)
        ]
        await asyncio.sleep(0.4)
        release.set()
        replies = await asyncio.gather(*tasks)
    finally:
        release.set()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert all(reply["result"]["processed"] == 1 for reply in replies)
    assert peak <= 2


@pytest.mark.asyncio
async def test_busy_owner_defers_without_leaving_a_stale_waiting_write():
    client = FakeRedis()
    started, release = asyncio.Event(), asyncio.Event()
    executed = []
    async def stage(account_id, name):
        executed.append(account_id)
        started.set()
        await release.wait()
        return {"processed": 1}, 60
    server = asyncio.create_task(serve_requests(client, owns=lambda _: True, execute_stage=stage,
                                              max_concurrency=1, poll_interval=0.01, defer_if_busy=True))
    first = asyncio.create_task(forward(1, "review", client=client, timeout=2))
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = await asyncio.wait_for(forward(2, "join", client=client, timeout=2), 1)
        assert second["result"]["reason"] == "session_owner_capacity_wait"
        assert executed == [1]
        release.set()
        assert (await first)["result"]["processed"] == 1
    finally:
        server.cancel()
        await asyncio.gather(server, first, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_request_never_acquires_or_calls_telegram():
    client = FakeRedis()
    await client.lpush(session_exec.REQUEST_KEY, json.dumps({"id": "expired", "account_id": 1,
        "stage": "join", "expires_at": time.time() - 1}))
    execute = AsyncMock()
    server = asyncio.create_task(serve_requests(client, owns=lambda _: True, execute_stage=execute, poll_interval=0.01))
    try:
        reply = await client.brpop(session_exec.REPLY_PREFIX + "expired", timeout=1)
        assert json.loads(reply[1])["result"]["reason"] == "session_owner_request_expired"
        execute.assert_not_called()
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_expiry", [True, False])
async def test_running_deadline_cancels_and_releases_slot_for_another_account(monkeypatch, use_expiry):
    client = FakeRedis()
    started, cleaned = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(session_exec, "STAGE_TIMEOUT_SECONDS", 0.15 if not use_expiry else 240)

    async def stage(account_id, name):
        if account_id == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return {"processed": 1}, 30

    server = asyncio.create_task(serve_requests(client, owns=lambda _: True, execute_stage=stage,
        max_concurrency=1, poll_interval=0.005, defer_if_busy=True))
    await client.lpush(session_exec.REQUEST_KEY, json.dumps({"id": "running", "account_id": 1,
        "stage": "review", "expires_at": time.time() + (0.2 if use_expiry else 1000)}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        reply = await client.brpop(session_exec.REPLY_PREFIX + "running", timeout=1)
        assert json.loads(reply[1])["result"]["reason"] == "session_owner_stage_timeout"
        assert cleaned.is_set()
        other = await forward(2, "ads", client=client, timeout=1)
        assert other["result"]["processed"] == 1
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_internal_timeout_is_reported_as_stage_error():
    client = FakeRedis()
    execute = AsyncMock(side_effect=TimeoutError("RPC timed out"))
    server = asyncio.create_task(serve_requests(client, owns=lambda _: True, execute_stage=execute,
                                               poll_interval=0.005))
    try:
        reply = await forward(1, "review", client=client, timeout=1)
        assert reply["error"] == "execution_failed" and reply["error_type"] == "TimeoutError"
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_stage,second_stage,parallel", [
    ("review", "review", True), ("review", "ads", False), ("join", "review", False),
    ("ads", "join", False),
])
async def test_only_read_reviews_share_capacity(first_stage, second_stage, parallel):
    client = FakeRedis()
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def execute(account_id, stage):
        seen.append((account_id, stage))
        if account_id == 1:
            entered.set()
            await release.wait()
        return {"processed": 1}, 30

    server = asyncio.create_task(serve_requests(client, owns=lambda _: True, execute_stage=execute,
        max_concurrency=2, defer_if_busy=True, parallel_stages=frozenset({"review"}), poll_interval=0.005))
    first = asyncio.create_task(forward(1, first_stage, client=client, timeout=2))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = await forward(2, second_stage, client=client, timeout=1)
        if parallel:
            assert second["result"]["processed"] == 1
            assert (2, second_stage) in seen
        else:
            assert second["result"]["reason"] == "session_owner_capacity_wait"
            assert (2, second_stage) not in seen
        release.set()
        assert (await first)["result"]["processed"] == 1
    finally:
        release.set()
        server.cancel()
        await asyncio.gather(server, first, return_exceptions=True)


@pytest.mark.asyncio
async def test_priority_ad_waits_behind_reviews_then_uses_first_freed_slot():
    """A review stream cannot starve an advertisement forever."""
    client = FakeRedis()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    ad_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    release_ad = asyncio.Event()

    async def execute(account_id, stage):
        if stage == "review" and account_id == 1:
            first_started.set()
            await release_first.wait()
        elif stage == "review" and account_id == 2:
            second_started.set()
            await release_second.wait()
        elif stage == "ads":
            ad_started.set()
            await release_ad.wait()
        return {"processed": 1}, 30

    server = asyncio.create_task(serve_requests(
        client,
        owns=lambda _: True,
        execute_stage=execute,
        max_concurrency=2,
        defer_if_busy=True,
        parallel_stages=frozenset({"review", "ads"}),
        queued_stages=frozenset({"ads"}),
        poll_interval=0.005,
    ))
    try:
        first = asyncio.create_task(forward(1, "review", client=client, timeout=5))
        await asyncio.wait_for(first_started.wait(), 1)
        second = asyncio.create_task(forward(2, "review", client=client, timeout=5))
        await asyncio.wait_for(second_started.wait(), 1)

        ad = asyncio.create_task(forward(3, "ads", client=client, timeout=5))
        await asyncio.sleep(0.05)
        assert not ad.done()

        release_first.set()
        await asyncio.wait_for(ad_started.wait(), 1)
        assert not second.done()

        release_ad.set()
        assert (await ad)["result"]["processed"] == 1
        release_second.set()
        assert (await first)["result"]["processed"] == 1
        assert (await second)["result"]["processed"] == 1
    finally:
        release_first.set()
        release_second.set()
        release_ad.set()
        server.cancel()
        await asyncio.gather(server, first, second, ad, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_stage", ["join", "ads"])
async def test_protected_stage_has_reserved_owner_slot(protected_stage):
    """Review work cannot consume the last owner slot needed by growth work."""
    client = FakeRedis()
    review_started = asyncio.Event()
    protected_started = asyncio.Event()
    release_review = asyncio.Event()

    async def execute(account_id, stage):
        if stage == "review":
            review_started.set()
            await release_review.wait()
        else:
            protected_started.set()
        return {"processed": 1}, 30

    server = asyncio.create_task(serve_requests(
        client,
        owns=lambda _: True,
        execute_stage=execute,
        max_concurrency=2,
        defer_if_busy=True,
        parallel_stages=frozenset({"review", "ads", "join"}),
        priority_stages=frozenset({"ads", "join"}),
        priority_reserved_slots=1,
        queued_stages=frozenset({"ads", "join"}),
        poll_interval=0.005,
    ))
    first = asyncio.create_task(forward(1, "review", client=client, timeout=3))
    protected = None
    try:
        await asyncio.wait_for(review_started.wait(), 1)
        background = await forward(2, "review", client=client, timeout=1)
        assert background["result"]["reason"] == "session_owner_capacity_wait"

        protected = asyncio.create_task(forward(3, protected_stage, client=client, timeout=3))
        await asyncio.wait_for(protected_started.wait(), 1)
        assert not first.done()
        assert (await protected)["result"]["processed"] == 1
        release_review.set()
        assert (await first)["result"]["processed"] == 1
    finally:
        release_review.set()
        server.cancel()
        pending = [server, first]
        if protected is not None:
            pending.append(protected)
        await asyncio.gather(*pending, return_exceptions=True)
