from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account import rpc_governor as rpc
from app.core.account.rpc_budget_policy import read_lane


def mock_usage(monkeypatch, values):
    from app.core import redis as redis_module

    cache = SimpleNamespace(
        eval=AsyncMock(
            return_value=[v for name, _ in rpc.WINDOWS for v in values.get(name, (0, -2))]
        ),
        ttl=AsyncMock(return_value=-2),
    )
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=cache))
    return cache


@pytest.mark.parametrize("factor,hour,day", [(1.0, 84, 840), (0.5, 42, 420), (0.6, 51, 504)])
def test_ad_reserve_tracks_account_recovery_without_raising_total(factor, hour, day):
    limits = rpc.limits_for({"read_factor": factor}, datetime.utcnow())
    assert (limits["ad_hour"], limits["ad_day"]) == (hour, day)
    assert limits["non_ad_day"] + limits["ad_day"] == limits["day"]
    assert limits["non_ad_hour"] + limits["ad_hour"] == limits["hour"]
    assert limits["sync_day"] == int(1200 * factor)
    assert limits["ad_hour"] * 100 >= limits["hour"] * 35
    assert limits["ad_day"] * 100 >= limits["day"] * 35


@pytest.mark.parametrize("purpose", ["ad_delivery", "ad_result_reconciliation"])
def test_ad_bootstrap_keeps_its_dedicated_lane(purpose):
    assert read_lane(["updates.GetStateRequest"], purpose) == "ad"
    assert read_lane(["users.GetUsersRequest"], purpose) == "ad"
    assert read_lane(["channels.GetMessagesRequest"], "ad_survival_check") == "survival"


@pytest.mark.asyncio
async def test_non_ad_exhaustion_does_not_block_ad_preflight(monkeypatch, test_db):
    mock_usage(monkeypatch, {"hour": (156, 1800), "day": (1560, 60000)})
    state = await rpc.check_read_ready(test_db, 3, purpose="ad_delivery", requires_bootstrap=True)
    assert state["lanes"]["ad"]["remaining"] == 84
    for purpose in [
        "ad_survival_check",
        "group_qualification",
        "growth_listener",
        "join_candidate_preview",
    ]:
        with pytest.raises(rpc.RpcDeferred) as caught:
            await rpc.check_read_ready(test_db, 3, purpose=purpose)
        assert caught.value.retry_after_seconds == 60000


@pytest.mark.asyncio
async def test_spent_ad_allowance_does_not_consume_non_ad_allowance(monkeypatch, test_db):
    mock_usage(
        monkeypatch,
        {"hour": (84, 1800), "day": (840, 60000), "ad_hour": (84, 1800), "ad_day": (840, 60000)},
    )
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(test_db, 3, purpose="ad_delivery")
    state = await rpc.check_read_ready(test_db, 3, purpose="ad_survival_check")
    assert state["usage"]["non_ad_day"]["used"] == 0
    assert state["lanes"]["critical"]["remaining"] == 60


@pytest.mark.asyncio
async def test_existing_exhausted_window_stays_blocked_without_reset(monkeypatch, test_db):
    cache = mock_usage(monkeypatch, {"day": (2400, 60000)})
    with pytest.raises(rpc.RpcDeferred) as caught:
        await rpc.check_read_ready(test_db, 3, purpose="ad_delivery")
    assert caught.value.retry_after_seconds == 60000
    script = cache.eval.await_args.args[0]
    assert all(op not in script for op in ["SET", "DEL", "EXPIRE", "INCR"])
