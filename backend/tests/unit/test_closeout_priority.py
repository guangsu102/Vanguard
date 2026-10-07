import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl import types
from telethon.tl.functions.updates import GetDifferenceRequest

from app.core.account import rpc_governor as rpc
from app.core.account.rpc_budget_policy import read_lane, window_plan

pytestmark = pytest.mark.asyncio


async def test_preview_has_critical_headroom_without_borrowing_ad_or_survival():
    limits = rpc.limits_for({}, datetime.utcnow())
    assert read_lane([], "join_candidate_preview") == "critical"
    assert read_lane([], "group_metadata_sync") == "background"
    windows = dict((name, cap) for name, _, cap in window_plan(limits, "critical"))
    assert windows["non_survival_day"] == 1200
    assert windows["critical_hour"] == 60
    assert "background_hour" not in windows


async def test_difference_continuation_finishes_even_after_queue_drops_below_threshold(monkeypatch):
    from app.core.account import listener_budget_wait
    monkeypatch.setattr(listener_budget_wait, "record_sync_result", AsyncMock())
    state = types.updates.State(pts=5, qts=0, date=datetime.utcnow(), seq=3, unread_count=0)
    page = types.updates.DifferenceSlice([], [], [], [], [], state)
    final = types.updates.DifferenceEmpty(datetime.utcnow(), seq=3)
    seen = []
    governor = Obj(succeeded=AsyncMock(), failed=AsyncMock())
    async def before(*args, **kwargs):
        seen.append(governor.catchup)
    governor.before = before
    original = AsyncMock(side_effect=[page, final, final])
    client = Obj(_call=original, _updates_handle=asyncio.current_task(),
                 _updates_queue=asyncio.Queue(), _vanguard_durable_checkpoint=True)
    rpc.install_governor(client, governor)
    # A real listener continues each slice from the server-provided cursor;
    # the continuation registry only recognizes that exact monotonic point.
    request = GetDifferenceRequest(pts=5, date=state.date - timedelta(seconds=1), qts=0)
    for _ in range(3):
        result = await client._call(None, request)
        following = getattr(result, "intermediate_state", None)
        if following is not None:
            request = GetDifferenceRequest(
                pts=following.pts, date=following.date, qts=following.qts
            )
    assert seen == [False, True, False]
    assert original.await_count == 3


async def test_catchup_keeps_all_shared_budget_windows(monkeypatch, test_db):
    from app.core import database, redis
    @asynccontextmanager
    async def session():
        yield test_db
    pipe = Mock(execute=AsyncMock())
    @asynccontextmanager
    async def pipeline(**kwargs):
        yield pipe
    cache = Obj(eval=AsyncMock(return_value=0), ttl=AsyncMock(return_value=-2), pipeline=pipeline)
    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(redis, "get_redis", AsyncMock(return_value=cache))
    governor = rpc.RpcGovernor(2, lambda: "listener")
    governor.catchup = True
    await governor.before(["updates.GetDifferenceRequest"], sync=True)
    args = cache.eval.await_args.args
    keys_count = args[1]
    assert args[2 + keys_count:4 + keys_count] == (1, 1000)
    assert all(f"vanguard:rpc:2:{key}" in args for key in
               ["minute", "hour", "day", "sync_reserved_hour", "sync_reserved_day", "join_reserved_day", "ad_day", "survival_day"])


async def test_metadata_yields_to_trial_replenishment_before_acquiring_telegram(test_db, monkeypatch):
    from app.core.group.collection import sync_stale_groups
    from app.modules.acquisition import ad_output_plan
    from tests.unit.test_dynamic_outbound_capacity import account_config, add_member
    account, config = await account_config(test_db)
    config.auto_join_enabled = True
    config.dynamic_capacity_enabled = True
    await add_member(test_db, account.id, 51, decision="allowed", review="approved")
    monkeypatch.setattr(ad_output_plan, "ad_output_plan", AsyncMock(return_value={"group_deficit": 30}))
    pool = Obj(sync_from_db=AsyncMock(), acquire_by_id=AsyncMock())
    result = await sync_stale_groups(test_db, pool=pool)
    assert result["details"][0]["reason"] == "metadata_wait_probe_replenishment"
    pool.acquire_by_id.assert_not_awaited()


async def test_old_preview_budget_retry_rechecks_current_gate(test_db, monkeypatch):
    from app.modules.acquisition import candidate_inventory, qualification_service, automation
    monkeypatch.setattr(qualification_service, "policy", AsyncMock(return_value={"enabled": True}))
    await candidate_inventory.defer_account(test_db, 2, datetime.utcnow() + timedelta(hours=1))
    await test_db.commit()
    gate = AsyncMock(return_value={"state": "ready"})
    monkeypatch.setattr(rpc, "check_read_ready", gate)
    service = object.__new__(automation.AcquisitionAutomationService)
    service.db, service.logger = test_db, Mock()
    service.account_pool = Obj(acquire_by_id=AsyncMock(return_value=None))
    group = Obj(id=1, group_id=-1000000000123, username="offline")
    await service._rank_pending_join_candidates(2, [group])
    service.account_pool.acquire_by_id.assert_awaited_once()
    gate.side_effect = rpc.RpcDeferred("telegram_rpc_cooldown", 600)
    service.account_pool.acquire_by_id.reset_mock()
    await service._rank_pending_join_candidates(2, [group])
    service.account_pool.acquire_by_id.assert_not_awaited()
