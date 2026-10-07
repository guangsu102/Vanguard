"""Exercise the real shared allocator without issuing a Telegram request."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types
from telethon.tl.functions.updates import GetDifferenceRequest

from app.core.account import rpc_governor as rpc
from app.core.account import sync_completion
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401


@pytest.fixture
def sync_governor(monkeypatch, queue_store):  # noqa: F811
    from app.core import database
    from app.core import redis as redis_module

    @asynccontextmanager
    async def session():
        yield Obj()

    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=queue_store))
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={}))
    monkeypatch.setattr(rpc.settings, "TELEGRAM_READ_BUDGET_PERCENT", 150)
    return rpc.RpcGovernor(2, lambda: "growth_listener")


@pytest.mark.asyncio
async def test_sync_page_can_progress_without_inheriting_idle_poll_wait(
    queue_store, sync_governor,  # noqa: F811
):
    seconds, micros = await queue_store.time()
    now_ms = seconds * 1000 + micros // 1000
    normal_due = str(now_ms + 480000)
    await queue_store.set("vanguard:rpc:2:sync_pace", normal_due, px=540000)
    for name, value in {"hour": 12, "day": 120, "sync_reserved_hour": 12,
                        "sync_reserved_day": 120}.items():
        await queue_store.set(f"vanguard:rpc:2:{name}", value, ex=1800)
    day_ttl = await queue_store.pttl("vanguard:rpc:2:day")

    # The normal poll continues to respect its prior four-read burst.
    with pytest.raises(rpc.RpcDeferred) as wait:
        await sync_governor.before(["updates.GetDifferenceRequest"], sync=True)
    assert 118 <= wait.value.retry_after_seconds <= 120
    assert await queue_store.get("vanguard:rpc:2:day") == "120"

    sync_governor.catchup = True
    for _ in range(4):
        await sync_governor.before(["updates.GetDifferenceRequest"], sync=True)
    assert await queue_store.get("vanguard:rpc:2:sync_pace") == normal_due
    assert await queue_store.get("vanguard:rpc:2:hour") == "16"
    assert await queue_store.get("vanguard:rpc:2:day") == "124"
    assert await queue_store.get("vanguard:rpc:2:sync_reserved_day") == "124"
    assert day_ttl - 2000 <= await queue_store.pttl("vanguard:rpc:2:day") <= day_ttl

    # A second governor shares the same catch-up burst, including its wait.
    competitor = rpc.RpcGovernor(2, lambda: "growth_listener")
    competitor.catchup = True
    with pytest.raises(rpc.RpcDeferred) as wait:
        await competitor.before(["updates.GetDifferenceRequest"], sync=True)
    assert 1 <= wait.value.retry_after_seconds <= 2
    assert await queue_store.get("vanguard:rpc:2:day") == "124"

    # Returning to ordinary polling retains the original debt.
    sync_governor.catchup = False
    with pytest.raises(rpc.RpcDeferred):
        await sync_governor.before(["updates.GetDifferenceRequest"], sync=True)
    assert await queue_store.get("vanguard:rpc:2:sync_pace") == normal_due


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["day", "platform"])
async def test_catchup_retains_daily_limit_and_platform_cooldown(
    queue_store, sync_governor, blocker,  # noqa: F811
):
    sync_governor.catchup = True
    if blocker == "day":
        await queue_store.set("vanguard:rpc:2:day", 3600, ex=1800)
        reason = "telegram_read_budget"
    else:
        await queue_store.set("vanguard:rpc:cooldown:2", "1", ex=1800)
        reason = "telegram_rpc_cooldown"
    with pytest.raises(rpc.RpcDeferred, match=reason):
        await sync_governor.before(["updates.GetDifferenceRequest"], sync=True)
    assert await queue_store.get("vanguard:rpc:2:sync_catchup_pace") is None
    assert await queue_store.get("vanguard:rpc:2:hour") is None
    assert await queue_store.get("vanguard:rpc:2:day") == ("3600" if blocker == "day" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("progress,expected", [(True, True), (False, False)])
async def test_catchup_requires_the_actual_monotonic_page_cursor(monkeypatch, progress, expected):
    from app.core.account import listener_budget_wait, send_receipts

    now = datetime.now(UTC)
    before = GetDifferenceRequest(54, now, 8)
    following = types.updates.State(54, 8, now + timedelta(seconds=int(progress)), 2, 0)
    slice_result = types.updates.DifferenceSlice([], [], [], [], [], following)
    original = AsyncMock(return_value=types.updates.Difference([], [], [], [], [], following))
    client = Obj(_call=original, _updates_handle=asyncio.current_task(),
                 _vanguard_durable_checkpoint=True, _updates_queue=asyncio.Queue(),
                 _vanguard_difference_continuation=True)
    sync_completion.record(client, [before], slice_result)
    seen = []

    async def check(*args, **kwargs):
        seen.append(governor.catchup)

    governor = Obj(account_id=2, before=check, succeeded=AsyncMock(), failed=AsyncMock())
    monkeypatch.setattr(listener_budget_wait, "record_sync_result", AsyncMock())
    monkeypatch.setattr(send_receipts, "record_request", AsyncMock())
    rpc.install_governor(client, governor)
    await client._call(None, GetDifferenceRequest(following.pts, following.date, following.qts))
    assert seen == [expected]
    original.assert_awaited_once()
