from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj

import pytest
from telethon.tl import types
from telethon.tl.functions.updates import GetDifferenceRequest

from app.core.account import sync_completion
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401


def test_difference_slice_continuation_requires_monotonic_progress():
    now = datetime.utcnow()
    request = GetDifferenceRequest(54, now, 8)
    result = types.updates.DifferenceSlice(
        [], [], [], [], [], types.updates.State(60, 9, now + timedelta(seconds=1), 2, 0)
    )
    following = sync_completion.next_cursor(request, result)
    assert following == ("account", 0, 60, 9,
                         int((now.replace(tzinfo=UTC) + timedelta(seconds=1)).timestamp()))

    client = Obj(_vanguard_durable_checkpoint=True)
    sync_completion.record(client, [request], result)
    assert sync_completion.continuing(client, [GetDifferenceRequest(60, now + timedelta(seconds=1), 9)])
    assert not sync_completion.continuing(client, [request])


def test_non_slice_response_does_not_create_completion_lease():
    now = datetime.utcnow()
    request = GetDifferenceRequest(54, now, 8)
    result = types.updates.Difference(
        [], [], [], [], [], types.updates.State(60, 9, now, 2, 0)
    )
    client = Obj(_vanguard_durable_checkpoint=True)
    sync_completion.record(client, [request], result)
    assert not getattr(client, "_vanguard_sync_continuations", {})


@pytest.mark.asyncio
async def test_completion_uses_only_idle_survival_reads(queue_store):  # noqa: F811
    from app.core.account.rpc_governor import limits_for

    limits = limits_for({}, datetime.utcnow())
    limits.update(minute=162, hour=324, day=3240,
                  ad_hour=114, survival_hour=49, critical_hour=81, sync_hour=64, routine_hour=16,
                  ad_day=1134, survival_day=486, critical_day=810, sync_day=648, routine_day=162,
                  listener_hour=7, listener_day=65,
                  survival_hold_hour=20, survival_hold_day=200)
    # The parent day is nearly full; its remaining capacity is protected for
    # advertising and survival. A normal critical read cannot use it, while a
    # progressing sync slice may use the proven survival surplus.
    values = {
        "day": 3153,
        "ad_day": 1082,
        "survival_day": 275,
        "join_reserved_day": 1011,
        "sync_reserved_day": 741,
        "flex_reserved_day": 44,
        "hour": 300,
        "ad_hour": 100,
        "survival_hour": 20,
        "join_reserved_hour": 80,
        "sync_reserved_hour": 80,
        "flex_reserved_hour": 20,
    }
    for name, value in values.items():
        await queue_store.set(f"vanguard:rpc:2:{name}", value, ex=1800)
    keys, args = reservation_args(2, limits, "survival", 4, 0, sync_completion=True)
    assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
    assert int(await queue_store.get("vanguard:rpc:2:day")) == 3157
    assert int(await queue_store.get("vanguard:rpc:2:sync_completion_day")) == 4
