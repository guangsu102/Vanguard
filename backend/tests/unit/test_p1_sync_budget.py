"""P1 uses only forecast-idle donors; all spending shares one atomic budget."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.account import rpc_governor as rpc
from app.core.account.reserved_reads import (
    RESERVED_BUDGET_LUA,
    allocations,
    reservation_args,
    transferred_caps,
)
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401
from tests.unit.test_ad_read_reserve import mock_usage


def p1_limits(monkeypatch, *, target=35):
    monkeypatch.setattr(rpc.settings, "P1_RUNTIME_RELIABILITY_ENABLED", True)
    monkeypatch.setattr(rpc.settings, "P1_SYNC_CANARY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(rpc.settings, "P1_SYNC_TARGET_PERCENT", target)
    limits = rpc.limits_for({}, datetime.utcnow(), 2)
    limits.update(minute=1000, sync_idle_lending=1,
                  ad_demand_hold_hour=40, ad_demand_hold_day=400,
                  survival_hold_hour=36, survival_hold_day=360)
    return limits


async def spend(store, limits, cost, lane="sync"):
    keys, args = reservation_args(2, limits, lane, cost, 0)
    return await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)


async def seed_sync(store, *, hour=60, day=600):
    for name, value in {"hour": hour, "day": day,
                        "sync_reserved_hour": hour, "sync_reserved_day": day}.items():
        await store.set("vanguard:rpc:2:" + name, value, ex=1800)


def test_p1_loan_argument_indexes_and_account_allowlist(monkeypatch):
    limits = p1_limits(monkeypatch)
    keys, args = reservation_args(2, limits, "sync", 1, 0)
    assert keys[29:33] == [
        "vanguard:rpc:2:sync_borrowed_ad_hour", "vanguard:rpc:2:sync_borrowed_ad_day",
        "vanguard:rpc:2:sync_borrowed_survival_hour", "vanguard:rpc:2:sync_borrowed_survival_day",
    ]
    assert args[41:44] == [84, 840, 1]
    assert args[45] == 1
    assert rpc.limits_for({}, datetime.utcnow(), 34)["sync_target_enforced"] == 0
    monkeypatch.setattr(rpc.settings, "P1_RUNTIME_RELIABILITY_ENABLED", False)
    assert rpc.limits_for({}, datetime.utcnow(), 2)["sync_target_hour"] == 48


def test_p1_spent_loan_transfers_only_idle_donors_never_critical():
    base = allocations(240)
    assert base == [84, 36, 60, 48, 12]
    moved = transferred_caps(base, 0, sync_borrowed_ad=20, sync_borrowed_survival=4)
    assert moved == [64, 32, 60, 72, 12]
    assert sum(moved) == sum(base)


@pytest.mark.asyncio
async def test_p1_snapshot_attributes_borrowed_capacity_to_donor(monkeypatch):
    limits = p1_limits(monkeypatch)
    mock_usage(monkeypatch, {name: (used, 1800) for name, used in {
        "hour": 84, "sync_reserved_hour": 84, "sync_borrowed_ad_hour": 24,
    }.items()})
    state = await rpc.read_budget_state(2, limits, datetime.utcnow())
    assert state["usage"]["ad_hour"]["guaranteed_limit"] == 60
    assert state["usage"]["critical_hour"]["guaranteed_limit"] == 60
    assert state["usage"]["sync_hour"]["guaranteed_limit"] == 72
    assert state["usage"]["sync_borrowed_ad_hour"]["used"] == 24


@pytest.mark.asyncio
async def test_p1_rpc_fetches_forecast_before_atomic_sync_admission(monkeypatch):
    from app.core import database, metrics
    from app.core import redis as redis_module

    limits = p1_limits(monkeypatch)
    db = SimpleNamespace()

    @asynccontextmanager
    async def session():
        yield db

    pipeline = MagicMock()
    pipeline.execute = AsyncMock(return_value=[])
    pipe_context = MagicMock()
    pipe_context.__aenter__ = AsyncMock(return_value=pipeline)
    pipe_context.__aexit__ = AsyncMock(return_value=None)
    cache = SimpleNamespace(eval=AsyncMock(return_value=0), ttl=AsyncMock(return_value=-2),
                            pipeline=MagicMock(return_value=pipe_context))
    forecast = AsyncMock(return_value={"state": "ready", "limits": limits})
    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=cache))
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={}))
    monkeypatch.setattr(rpc, "snapshot", forecast)
    observation = AsyncMock()
    monkeypatch.setattr(metrics, "record_sync_observation", observation)
    await rpc.RpcGovernor(2, lambda: "growth_listener").before(["updates.GetDifferenceRequest"], sync=True)
    forecast.assert_awaited_once()
    allocation_args = cache.eval.await_args.args
    key_count = allocation_args[1]
    assert allocation_args[2 + key_count + 43] == 1
    observation.assert_awaited_once()


@pytest.mark.asyncio
async def test_p1_missing_redis_fails_closed_before_admission(monkeypatch):
    from app.core import database
    from app.core import redis as redis_module

    p1_limits(monkeypatch)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={}))
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(side_effect=ConnectionError("offline")))
    with pytest.raises(rpc.RpcDeferred, match="telegram_rpc_guard_unavailable"):
        await rpc.RpcGovernor(2, lambda: "growth_listener").before(["updates.GetDifferenceRequest"], sync=True)


@pytest.mark.asyncio
async def test_p1_sync_stops_at_35_percent_in_each_window(queue_store, monkeypatch):  # noqa: F811
    limits = p1_limits(monkeypatch)
    await seed_sync(queue_store)
    assert await spend(queue_store, limits, 24) == 0
    assert await queue_store.get("vanguard:rpc:2:sync_reserved_hour") == "84"
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_hour") == "24"
    assert await spend(queue_store, limits, 1) > 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "84"
    await queue_store.flushdb()
    await seed_sync(queue_store, hour=0, day=840)
    assert await spend(queue_store, limits, 1) > 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [20, 35])
async def test_p1_no_forecast_never_borrows_protected_work(queue_store, monkeypatch, target):  # noqa: F811
    limits = p1_limits(monkeypatch, target=target)
    limits.update(ad_demand_hold_hour=-1, survival_hold_hour=-1)
    await seed_sync(queue_store)
    assert await spend(queue_store, limits, 1) > 0
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_hour") is None
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_survival_hour") is None


@pytest.mark.asyncio
async def test_p1_survival_idle_loan_retains_listener_control_reserve(queue_store, monkeypatch):  # noqa: F811
    limits = p1_limits(monkeypatch)
    limits.update(ad_demand_hold_hour=84, survival_hold_hour=0)
    await seed_sync(queue_store)
    assert await spend(queue_store, limits, 24) == 0
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_hour") is None
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_survival_hour") == "24"
    # Current demand can change immediately; the independent control slice is
    # retained even when the supplied forecast erroneously says zero demand.
    assert await spend(queue_store, limits, 5, "listener") == 0
    assert await spend(queue_store, limits, 84, "ad") == 0
    assert await spend(queue_store, limits, 60, "critical") == 0
    assert int(await queue_store.get("vanguard:rpc:2:hour")) <= limits["hour"]


@pytest.mark.asyncio
async def test_p1_explicit_20_percent_target_cannot_spend_flex_or_idle_loan(queue_store, monkeypatch):  # noqa: F811
    limits = p1_limits(monkeypatch, target=20)
    assert await spend(queue_store, limits, 48) == 0
    assert await spend(queue_store, limits, 1) > 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "48"


@pytest.mark.asyncio
async def test_p1_loan_survives_forecast_change_and_keeps_critical_and_control_work(queue_store, monkeypatch):  # noqa: F811
    limits = p1_limits(monkeypatch)
    await seed_sync(queue_store)
    ttl = await queue_store.pttl("vanguard:rpc:2:hour")
    assert await spend(queue_store, limits, 24) == 0
    # Rollback only disables new borrowing. Already spent loans still transfer.
    limits.update(sync_idle_lending=0, ad_demand_hold_hour=-1, survival_hold_hour=-1)
    assert await spend(queue_store, limits, 61, "ad") > 0
    assert await spend(queue_store, limits, 60, "ad") == 0
    assert await spend(queue_store, limits, 31, "survival") == 0
    assert await spend(queue_store, limits, 5, "listener") == 0
    assert await spend(queue_store, limits, 60, "critical") == 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "240"
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_hour") == "24"
    assert ttl - 2000 <= await queue_store.pttl("vanguard:rpc:2:sync_borrowed_ad_hour") <= ttl
    assert await spend(queue_store, limits, 1, "critical") > 0


@pytest.mark.asyncio
async def test_p1_sync_loan_is_atomic_under_contention_and_resets_only_with_window(queue_store, monkeypatch):  # noqa: F811
    limits = p1_limits(monkeypatch)
    await seed_sync(queue_store)
    results = await asyncio.gather(*(spend(queue_store, limits, 1) for _ in range(40)))
    assert results.count(0) == 24
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_hour") == "24"
    assert await queue_store.hget("vanguard:metrics:v1:counters", "vanguard_sync_borrowed_reads_total|hour") == "24"
    # A retained orphan counter cannot carry a loan into a fresh total window.
    await queue_store.delete("vanguard:rpc:2:hour", "vanguard:rpc:2:day")
    assert await spend(queue_store, limits, 1) == 0
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_hour") is None
    assert await queue_store.get("vanguard:rpc:2:sync_borrowed_ad_day") is None
