import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.functions.updates import GetDifferenceRequest

from app.core.account import rpc_governor as rpc
from app.core.account.rpc_budget_policy import read_lane, window_plan

pytestmark = pytest.mark.asyncio


def listener(governor):
    client = TelegramClient(MemorySession(), 1, "offline-test-placeholder")
    client.is_connected = Mock(return_value=True)
    client.disconnect = AsyncMock()
    client._message_box = Mock(
        is_empty=Mock(return_value=False),
        get_difference=Mock(
            return_value=GetDifferenceRequest(pts=1, date=datetime.utcnow(), qts=1)
        ),
    )
    original = AsyncMock(side_effect=asyncio.CancelledError)
    client._call = original
    rpc.install_governor(client, governor)
    return client, original


async def test_native_update_loop_waits_then_resumes_without_fatal_disconnect(monkeypatch):
    governor = Obj(
        before=AsyncMock(side_effect=[rpc.RpcDeferred("telegram_read_budget", 120), None]),
        failed=AsyncMock(),
        succeeded=AsyncMock(),
    )
    client, original = listener(governor)
    sleep = AsyncMock()
    monkeypatch.setattr(rpc.asyncio, "sleep", sleep)
    client._updates_handle = asyncio.create_task(client._update_loop())
    await asyncio.wait_for(client._updates_handle, timeout=1)
    assert client._updates_error is None
    client.disconnect.assert_not_awaited()
    assert governor.before.await_count == 2
    original.assert_awaited_once()
    sleep.assert_awaited_once_with(60)


async def test_budget_wait_is_cancellable_without_rpc():
    entered = asyncio.Event()

    async def blocked(methods, **kwargs):
        entered.set()
        raise rpc.RpcDeferred("telegram_rpc_cooldown", 3600)

    client, original = listener(Obj(before=blocked, failed=AsyncMock(), succeeded=AsyncMock()))
    client._updates_handle = asyncio.create_task(client._update_loop())
    await asyncio.wait_for(entered.wait(), timeout=1)
    client._updates_handle.cancel()
    await asyncio.wait_for(client._updates_handle, timeout=1)
    original.assert_not_awaited()
    client.disconnect.assert_not_awaited()


async def test_same_read_in_business_task_propagates_wait_without_hidden_retry():
    governor = Obj(
        before=AsyncMock(side_effect=rpc.RpcDeferred("telegram_read_budget", 120)),
        failed=AsyncMock(),
        succeeded=AsyncMock(),
    )
    client, original = listener(governor)
    with pytest.raises(rpc.RpcDeferred):
        await client._call(None, GetDifferenceRequest(pts=1, date=datetime.utcnow(), qts=1))
    original.assert_not_awaited()
    governor.before.assert_awaited_once()


async def test_sync_cannot_inherit_critical_advertising_priority():
    limits = rpc.limits_for({}, datetime.utcnow())
    assert read_lane(["updates.GetStateRequest"], "ad_delivery") == "ad"
    routine = {name: cap for name, _, cap in window_plan(limits, "routine")}
    critical = {name: cap for name, _, cap in window_plan(limits, "critical")}
    assert (routine["hour"], critical["hour"]) == (240, 240)
    assert (routine["routine_hour"], critical["critical_hour"]) == (24, 60)
    assert (routine["day"], critical["day"]) == (2400, 2400)
    assert (routine["routine_day"], critical["critical_day"]) == (240, 600)
    sync = {name: cap for name, _, cap in window_plan(limits, "sync")}
    assert sync["sync_hour"] == 120 and sync["sync_day"] == 1200


async def test_snapshot_exposes_lane_wait_without_blocking_critical_reads(monkeypatch, test_db):
    from app.core import redis as redis_module

    cache = Obj(
        eval=AsyncMock(return_value=[value for name, _ in rpc.WINDOWS for value in ({"hour": (120, 100), "day": (120, 1000), "sync_hour": (60, 200), "sync_day": (60, 1000), "routine_hour": (24, 100), "routine_day": (24, 1000)}.get(name, (0, -2)))]),
        ttl=AsyncMock(return_value=-2),
    )
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=cache))
    now = datetime.utcnow()
    state = await rpc.check_read_ready(test_db, 3, now, purpose="ad_survival_check")
    assert state["state"] == "ready" and state["lanes"]["survival"]["remaining"] == 36
    assert state["lanes"]["routine"]["retry_after_seconds"] == 100
    assert state["lanes"]["sync"]["retry_after_seconds"] == 100
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(test_db, 3, now, purpose="group_qualification")


async def test_governor_atomically_charges_shared_and_sync_windows_and_records_purpose(
    monkeypatch, test_db
):
    from app.core import database
    from app.core import redis as redis_module

    @asynccontextmanager
    async def session():
        yield test_db

    pipe = Mock(hincrby=Mock(), expire=Mock(), execute=AsyncMock())

    @asynccontextmanager
    async def pipeline(**kwargs):
        yield pipe

    cache = Obj(eval=AsyncMock(return_value=0), ttl=AsyncMock(return_value=-2), pipeline=pipeline)
    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=cache))
    await rpc.RpcGovernor(3, lambda: "ad_delivery").before(
        ["updates.GetChannelDifferenceRequest"], sync=True
    )
    args = cache.eval.await_args.args
    assert args[1] == len(window_plan(rpc.limits_for({}, datetime.utcnow()), "sync")) + 5
    assert "vanguard:rpc:3:hour" in args and "vanguard:rpc:3:sync_hour" in args
    assert any(
        "sync|ad_delivery|updates.GetChannelDifferenceRequest" in call.args
        for call in pipe.hincrby.call_args_list
    )


@pytest.mark.parametrize("persistent", [False, True])
async def test_only_persistent_clients_subscribe_to_updates(monkeypatch, persistent):
    from app.core.account import pool as module

    pool = module.AccountPool()
    account = await pool.add_account(
        account_id=999,
        phone="+10000000999",
        session_name="offline-subscription",
        country_code="US",
        api_id="1",
        api_hash="offline-test-placeholder",
    )
    account.keep_connected = persistent
    pool._validate_runtime_environment = Mock()
    client = Obj(
        _call=AsyncMock(),
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "TelegramClient", factory)
    monkeypatch.setattr(module, "RpcGovernor", Mock(return_value=Obj(before=AsyncMock())))
    assert await pool._create_client(account) is client
    assert factory.call_args.kwargs["receive_updates"] is persistent


async def test_listener_flag_is_set_before_creation_and_restored_on_failure():
    from app.core.account.pool import AccountPool

    pool = AccountPool()
    account = await pool.add_account(
        account_id=999,
        phone="+10000000999",
        session_name="offline-listener-flag",
        country_code="US",
        api_id="1",
        api_hash="offline-test-placeholder",
    )
    pool._assert_proxy_policy_current = AsyncMock()
    pool._ensure_proxy = AsyncMock()

    async def create(wrapper):
        assert wrapper.keep_connected is True
        raise rpc.RpcDeferred("telegram_read_budget", 600)

    pool._create_client = create
    with pytest.raises(rpc.RpcDeferred):
        await pool.connect_by_id(account.account_id, require_session=False, keep_connected=True)
    assert account.keep_connected is False


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
async def test_listener_bootstrap_uses_sync_lane_and_restores_purpose(retained, deferred):
    from app.core.account.pool import AccountPool
    from app.core.account.listener_pause import ListenerPause

    pool = AccountPool()
    account = await pool.add_account(account_id=995, phone="+10000000995",
        session_name="offline-bootstrap-lane", country_code="US", api_id="1",
        api_hash="offline-test-placeholder")
    pool._assert_proxy_policy_current = AsyncMock()
    pool._ensure_proxy = AsyncMock()
    previous = account.active_purpose
    client = Obj(is_connected=Mock(return_value=False), disconnect=AsyncMock())
    async def bootstrap(*args):
        lane = read_lane(["users.GetUsersRequest"], account.active_purpose)
        assert lane == "sync"
        caps = dict((name, cap) for name, _, cap in window_plan(rpc.limits_for({}, datetime.utcnow()), lane))
        # Routine is exhausted; the listener still owns the final quarter.
        assert caps["day"] > 2000
        if deferred:
            raise rpc.RpcDeferred("telegram_read_budget", 60)
        return client
    if retained:
        client.connect = bootstrap
        client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
        account.client = client
    else:
        pool._create_client = bootstrap
    if deferred:
        with pytest.raises(rpc.RpcDeferred):
            await pool.connect_by_id(995, purpose="growth_listener", require_session=False)
    else:
        assert await pool.connect_by_id(995, purpose="growth_listener", require_session=False) is account
    assert account.active_purpose == previous
