"""New join work pays one target step while preserving the existing quota ledger."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from telethon.errors import RPCError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import PeerChannel

from app.core.account import read_work, rpc_governor as rpc
from app.core.account.join_execution_metrics import observe
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, allocations, reservation_args
from app.modules.acquisition import candidate_inventory, qualification_engine as engine
from app.modules.acquisition.candidate_preview import CandidatePreview
from app.modules.acquisition.growth_admission import admission_plan
from app.modules.acquisition.review_read_plan import ReadStepPlan


@pytest.fixture
def efficiency(monkeypatch):
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")


@pytest_asyncio.fixture
async def lua_store():
    pytest.importorskip("lupa")
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    store = fakeredis.FakeRedis(decode_responses=True)
    try:
        yield store
    finally:
        await store.aclose()


def small_limits(*, enabled=True):
    # Small, exact allocations make all protected reads observable in one window.
    return {"minute": 1000, "hour": 100, "day": 1000, "growth_efficiency_v1": int(enabled)}


async def seed_hour(store, *, survival=12, critical=0, review=0, loan=0):
    counts = {"ad_hour": 35, "survival_hour": survival, "join_reserved_hour": critical,
              "sync_reserved_hour": 20, "flex_reserved_hour": 5}
    counts["hour"] = sum(counts.values())
    counts["critical_review_hour"] = review
    counts["survival_lent_hour"] = loan
    for name, value in counts.items():
        await store.set(f"vanguard:rpc:2:{name}", value, ex=1800)


async def allocate(store, cost, *, lane="critical", kind="join", limits=None, token="", mode="read"):
    keys, args = reservation_args(2, limits or small_limits(), lane, cost, 0,
                                 workload=kind, work_token=token, work_mode=mode,
                                 work_kind=kind, work_group=40, work_scope="target")
    return await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)


async def test_atomic_40_60_split_preserves_listener_and_original_ttl(lua_store):
    await seed_hour(lua_store)
    assert await allocate(lua_store, 10) == 0
    assert await allocate(lua_store, 1) > 0  # review owns the other 15 reads
    assert await allocate(lua_store, 15, kind="review") == 0
    assert await allocate(lua_store, 1, kind="review") > 0
    assert await allocate(lua_store, 1, lane="survival") > 0
    assert await allocate(lua_store, 3, lane="listener") == 0
    assert await lua_store.get("vanguard:rpc:2:hour") == "100"
    assert 1790 <= await lua_store.ttl("vanguard:rpc:2:hour") <= 1800


async def test_disabled_engine_keeps_legacy_quarter_floor_and_existing_ledger(lua_store):
    await seed_hour(lua_store)
    limits = small_limits(enabled=False)
    assert await allocate(lua_store, 19, limits=limits) == 0
    assert await allocate(lua_store, 1, limits=limits) > 0
    assert await allocate(lua_store, 6, kind="review", limits=limits) == 0
    assert await allocate(lua_store, 3, lane="listener", limits=limits) == 0
    assert await lua_store.get("vanguard:rpc:2:hour") == "100"
    assert 1790 <= await lua_store.ttl("vanguard:rpc:2:hour") <= 1800


async def test_spent_legacy_survival_loan_cannot_consume_future_listener_or_reset_it(lua_store):
    await seed_hour(lua_store, survival=10, critical=20, review=20, loan=15)
    assert await allocate(lua_store, 8) > 0
    assert await allocate(lua_store, 7) == 0
    assert await lua_store.get("vanguard:rpc:2:survival_lent_hour") == "15"
    assert await allocate(lua_store, 1, lane="survival") > 0
    assert await allocate(lua_store, 3, lane="listener") == 0
    assert await lua_store.get("vanguard:rpc:2:hour") == "100"
    assert await lua_store.get("vanguard:rpc:2:survival_lent_hour") == "15"
    assert 1790 <= await lua_store.ttl("vanguard:rpc:2:hour") <= 1800


async def test_old_owner_survival_loan_is_clamped_to_unspent_listener(lua_store):
    await seed_hour(lua_store, survival=10, critical=25, review=25)
    await lua_store.hset(read_work.key(2), mapping={
        "token": "owner", "remaining": 5, "kind": "join", "lane": 3,
        "survival_1": 5, "survival_2": 5,
    })
    # Even an old admitted owner cannot turn the last listener reads into a loan.
    limits = {**small_limits(), "survival_hold_hour": 0, "survival_hold_day": 0}
    assert await allocate(lua_store, 3, token="owner", limits=limits) > 0
    assert await allocate(lua_store, 2, token="owner", limits=limits) == 0
    assert int(await lua_store.get("vanguard:rpc:2:survival_lent_hour") or 0) == 2
    assert await allocate(lua_store, 3, lane="listener", limits=limits) == 0


async def test_new_target_plan_does_not_subtract_historical_review_debt(test_db, monkeypatch, efficiency):
    slots = AsyncMock(return_value=0)
    monkeypatch.setattr(engine, "initial_slots", slots)
    debt = AsyncMock(side_effect=AssertionError("historical debt must not be loaded"))
    monkeypatch.setattr("app.modules.acquisition.growth_admission.review_debt", debt)
    budget = {"state": "ready", "critical_workloads": {"join": {"remaining": 6, "retry_after_seconds": 0}}}
    plan = ReadStepPlan("join", "join:2:40", 6, datetime.utcnow(), missing_dependencies=("member_confirmation",))
    result = await admission_plan(test_db, 2, datetime.utcnow(), rpc=budget, group_id=40, read_plan=plan)
    assert result["remaining"] == 2 and result["remaining_read_cost"] == 6
    assert result["review_reads_due"] == 0
    debt.assert_not_awaited()
    slots.return_value = 2
    assert (await admission_plan(test_db, 2, datetime.utcnow()))["reason"] == "join_initial_collection_capacity"


async def test_new_join_reserves_its_step_without_p90_or_empty_review_handoff(monkeypatch, efficiency):
    monkeypatch.setattr("app.modules.acquisition.growth_admission.admission_plan",
                        AsyncMock(return_value={"remaining": 1}))
    monkeypatch.setattr("app.core.account.budget_ledger.reserve_read_work", AsyncMock())
    cache = Obj(lrange=AsyncMock(side_effect=AssertionError("no lifecycle sampling")),
                eval=AsyncMock(return_value=0), hget=AsyncMock(return_value="8"))
    work = read_work.ReadWork(2, 40, "join")
    token = read_work._current.set(work)
    try:
        await read_work.ensure(None, 2, small_limits(), cache)
        assert work.claimed and work.limit == 8 and work.review_reads == 0
        call = cache.eval.await_args.args
        assert call[2 + call[1]] == 8  # ARGV1 follows the existing ledger keys
        await read_work.handoff(work, "membership")
        assert not work.handed_off and cache.eval.await_count == 1
    finally:
        read_work._current.reset(token)


@pytest.mark.parametrize("invalid", [None, "account", "identity", "username", "expired", "deferred", "namespace", "excluded"])
async def test_join_plan_resolves_only_matching_reusable_local_preview(test_db, efficiency, invalid):
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.review_read_plan import for_join
    from tests.unit.test_completion_capacity import configured
    _, account, group, _, _, _ = await configured(test_db)
    now = datetime.utcnow()
    fact = candidate_inventory.record(group, account.id,
        CandidatePreview("metadata_checked", peer_namespace="channel", member_count=5), now)
    if invalid == "account":
        fact["account_id"] = account.id + 1
    elif invalid == "identity":
        fact["telegram_group_id"] = group.group_id + 1
    elif invalid == "username":
        fact["username"] = "other_group"
    elif invalid == "expired":
        fact.update(status="sampled", checked_at=(now-timedelta(hours=4)).isoformat(),
                    expires_at=(now-timedelta(hours=1)).isoformat())
    elif invalid == "deferred":
        fact["status"] = "deferred"
    elif invalid == "namespace":
        fact["namespace"] = None
    elif invalid == "excluded":
        fact["preview_exclusion"] = "identity_mismatch"
    test_db.add(SystemSetting(key=candidate_inventory.key(account.id, group.id), value=json.dumps(fact)))
    await test_db.commit()
    plan = await for_join(test_db, account.id, group.id, now)
    assert plan.required_reads == (6 if invalid is None else 8)
    assert ("target_preview" in plan.missing_dependencies) is (invalid is not None)


async def test_cached_join_six_read_forecast_and_atomic_claim_agree(test_db, monkeypatch, efficiency):
    from app.core.settings_models import SystemSetting
    from tests.unit.test_completion_capacity import configured
    _, account, group, _, _, _ = await configured(test_db)
    now = datetime.utcnow()
    fact = candidate_inventory.record(group, account.id,
        CandidatePreview("metadata_checked", peer_namespace="channel", member_count=5), now)
    test_db.add(SystemSetting(key=candidate_inventory.key(account.id, group.id), value=json.dumps(fact)))
    await test_db.commit()
    budget = {"state": "ready", "critical_workloads": {"join": {"remaining": 6, "retry_after_seconds": 0}}}
    monkeypatch.setattr(rpc, "snapshot", AsyncMock(return_value=budget))
    monkeypatch.setattr("app.core.account.budget_ledger.reserve_read_work", AsyncMock())
    result = await admission_plan(test_db, account.id, now, rpc=budget, group_id=group.id)
    assert result["remaining"] == 2 and result["remaining_read_cost"] == 6
    cache = Obj(lrange=AsyncMock(side_effect=AssertionError("no lifecycle sampling")),
                eval=AsyncMock(return_value=0), hget=AsyncMock(return_value="6"))
    work = read_work.ReadWork(account.id, group.id, "join")
    token = read_work._current.set(work)
    try:
        await read_work.ensure(test_db, account.id, small_limits(), cache)
        assert work.claimed and work.limit == work.planned_reads == 6 and work.review_reads == 0
        call = cache.eval.await_args.args
        assert call[2 + call[1]] == 6
    finally:
        read_work._current.reset(token)


@pytest.mark.parametrize("reason", ["members_below_50", "group_ad_ban_and_online_below_2",
                                    "no_ordinary_member_ad_48h", "account_permanent_send_restriction"])
async def test_value_history_only_is_retired_for_allowlisted_join(test_db, efficiency, reason):
    from tests.unit.test_qualification_join_gate import entity, seed
    from app.modules.acquisition.qualification_join_gate import qualification_join_gate
    await seed(test_db, reason=reason)
    expected = "qualification_rejoin_requires_changed_evidence_clearance"
    assert await qualification_join_gate(test_db, entity(), account_id=3) == expected
    actual = await qualification_join_gate(test_db, entity(), account_id=2)
    assert actual == (expected if reason == "account_permanent_send_restriction" else None)


def test_low_value_metadata_is_reused_only_for_enabled_account(efficiency):
    group = Obj(group_id=-1000000000123, username="candidate")
    hint = CandidatePreview("metadata_checked", peer_namespace="channel", member_count=5)
    new = candidate_inventory.record(group, 2, hint, datetime.utcnow())
    old = candidate_inventory.record(group, 3, hint, datetime.utcnow())
    assert candidate_inventory.ready(new)
    assert not candidate_inventory.ready(old)
    assert not candidate_inventory.ready({**new, "preview_exclusion": "identity_mismatch"})
    restored = candidate_inventory.for_account({**old, "status": "sampled"}, 2)
    assert restored["status"] == "metadata_checked" and candidate_inventory.ready(restored)


@pytest.mark.parametrize("case", ["success", "rpc_error", "network_error", "budget_wait", "owner_lost"])
async def test_sdk_observer_distinguishes_platform_response_from_local_wait(monkeypatch, case):
    from app.core.account import read_metrics
    monkeypatch.setattr(read_metrics, "start_metrics_health", lambda: None)
    monkeypatch.setattr(read_metrics, "record_rpc", AsyncMock())
    monkeypatch.setattr(read_metrics, "prepare_sdk_entry", AsyncMock())
    monkeypatch.setattr(read_metrics, "enter_sdk", lambda entry: None)
    monkeypatch.setattr(read_metrics, "finish_sdk_entry", AsyncMock())
    monkeypatch.setattr("app.core.account.send_receipts.record_request", AsyncMock())
    monkeypatch.setattr("app.core.account.event_inbox.mark_external_attempt", AsyncMock())
    error = (RPCError(None, "known", 400) if case == "rpc_error" else
             OSError("network") if case == "network_error" else None)
    original = AsyncMock(return_value=Obj(), side_effect=error)
    guard = Obj(assert_owned=AsyncMock(side_effect=RuntimeError("owner lost") if case == "owner_lost" else None))
    governor = Obj(account_id=2, purpose=lambda: "auto_join", failed=AsyncMock(), succeeded=AsyncMock(),
                   before=AsyncMock(side_effect=rpc.RpcDeferred("telegram_read_budget", 60) if case == "budget_wait" else None))
    client = Obj(_call=original, _vanguard_session_guard=guard)
    rpc.install_governor(client, governor)
    entered, responded = AsyncMock(), AsyncMock()
    with observe(on_sdk_entered=entered, on_platform_responded=responded):
        if case == "success":
            await client._call(None, JoinChannelRequest(PeerChannel(123)))
        else:
            with pytest.raises(Exception):
                await client._call(None, JoinChannelRequest(PeerChannel(123)))
    assert entered.await_count == original.await_count == int(case not in {"budget_wait", "owner_lost"})
    assert responded.await_count == int(case in {"success", "rpc_error"})
    if entered.await_count:
        assert isinstance(entered.await_args.args[0], datetime)


async def test_cancellation_during_preparation_never_creates_sdk_fact(monkeypatch):
    from app.core.account import read_metrics
    pending = asyncio.Event()

    async def prepare(*args, **kwargs):
        pending.set()
        await asyncio.Future()

    monkeypatch.setattr(read_metrics, "start_metrics_health", lambda: None)
    monkeypatch.setattr(read_metrics, "record_rpc", AsyncMock())
    monkeypatch.setattr(read_metrics, "prepare_sdk_entry", prepare)
    monkeypatch.setattr("app.core.account.send_receipts.record_request", AsyncMock())
    monkeypatch.setattr("app.core.account.event_inbox.mark_external_attempt", AsyncMock())
    original, entered, responded = AsyncMock(), AsyncMock(), AsyncMock()
    governor = Obj(account_id=2, purpose=lambda: "auto_join", before=AsyncMock(),
                   failed=AsyncMock(), succeeded=AsyncMock())
    client = Obj(_call=original)
    rpc.install_governor(client, governor)
    with observe(on_sdk_entered=entered, on_platform_responded=responded):
        task = asyncio.create_task(client._call(None, JoinChannelRequest(PeerChannel(123))))
    await pending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert original.await_count == entered.await_count == responded.await_count == 0


async def test_sdk_fact_survives_second_cancel_during_checkpoint_without_replay(monkeypatch):
    from app.core.account import read_metrics, join_execution_metrics
    entered_sdk, checkpoint_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    facts, sdk_times = [], []

    async def sdk(*args, **kwargs):
        sdk_times.append(datetime.utcnow())
        entered_sdk.set()
        await asyncio.Future()

    async def checkpoint(at):
        checkpoint_started.set()
        await release.wait()
        facts.append(at)

    monkeypatch.setattr(read_metrics, "start_metrics_health", lambda: None)
    monkeypatch.setattr(read_metrics, "record_rpc", AsyncMock())
    monkeypatch.setattr(read_metrics, "prepare_sdk_entry", AsyncMock())
    monkeypatch.setattr(read_metrics, "enter_sdk", lambda entry: None)
    monkeypatch.setattr(read_metrics, "finish_sdk_entry", AsyncMock())
    monkeypatch.setattr("app.core.account.send_receipts.record_request", AsyncMock())
    monkeypatch.setattr("app.core.account.event_inbox.mark_external_attempt", AsyncMock())
    original, responded = AsyncMock(side_effect=sdk), AsyncMock()
    governor = Obj(account_id=2, purpose=lambda: "auto_join", before=AsyncMock(),
                   failed=AsyncMock(), succeeded=AsyncMock())
    client = Obj(_call=original)
    rpc.install_governor(client, governor)
    with observe(on_sdk_entered=checkpoint, on_platform_responded=responded):
        task = asyncio.create_task(client._call(None, JoinChannelRequest(PeerChannel(123))))
    await entered_sdk.wait()
    assert not checkpoint_started.is_set()  # No durable callback awaited before the SDK.
    task.cancel()
    await checkpoint_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert join_execution_metrics._pending and not facts
    finally:
        release.set()
        await asyncio.gather(*join_execution_metrics._pending)
    assert original.await_count == len(facts) == 1 and responded.await_count == 0
    assert facts[0] <= sdk_times[0]


async def test_new_dynamic_limit_does_not_call_ad_inventory_gate(test_db, monkeypatch, efficiency):
    from tests.unit.test_completion_capacity import configured
    from app.modules.acquisition.dynamic_frequency import AccountDynamicFrequencyService
    _, account, _, _, _, config = await configured(test_db)
    monkeypatch.setattr("app.core.account.age.account_age_eligibility_reason", lambda *args: None)
    monkeypatch.setattr("app.core.account.outbound_budget.effective_capacity_limits",
                        AsyncMock(return_value={"join": 12}))
    old = AsyncMock(side_effect=AssertionError("old ad gate cannot veto joining"))
    monkeypatch.setattr("app.modules.acquisition.ad_output_plan.ad_output_plan", old)
    assert await AccountDynamicFrequencyService(test_db).auto_join_dynamic_daily_limit(config, datetime.utcnow()) == 12
    old.assert_not_awaited()


async def test_join_floor_ignores_legacy_backlog_and_keeps_both_window_shares(test_db, monkeypatch, efficiency):
    from tests.unit.test_completion_capacity import configured, add_join_candidate
    from tests.unit.test_ad_read_reserve import mock_usage
    _, account, _, member, _, config = await configured(test_db)
    config.join_review_backlog_paused, member.review_status = True, "initial_pending"
    await add_join_candidate(test_db, account.id)
    await test_db.commit()
    mock_usage(monkeypatch, {})
    due = AsyncMock(side_effect=AssertionError("value maintenance is not main-path demand"))
    monkeypatch.setattr("app.core.account.join_demand_lending.maintenance_due", due)
    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())
    assert state["state"] == "ready" and not state.get("forecast_errors")
    for window in ("hour", "day"):
        assert state["limits"][f"join_idle_hold_{window}"] == allocations(state["limits"][window])[2] * 40 // 100
    due.assert_not_awaited()


async def test_output_plan_keeps_old_debt_as_advisory_only(test_db, monkeypatch, efficiency):
    from tests.unit.test_completion_capacity import configured
    from app.modules.acquisition.ad_output_plan import ad_output_plan
    _, account, _, _, _, config = await configured(test_db)
    config.max_groups_total = 300
    monkeypatch.setattr("app.modules.acquisition.ad_output_plan.pending_trial_work", AsyncMock(return_value={
        "probe_survival_due": 100, "probe_unresolved": 100, "survival_due": 100, "unresolved": 100,
    }))
    result = await ad_output_plan(test_db, account, config, datetime.utcnow(),
        inventory={"total": 150, "usable": 0, "active_backlog": 150},
        rpc={"state": "ready", "limits": rpc.limits_for({}, datetime.utcnow(), account.id)},
        limits={"ad": 30}, workload={"ad_targets": 10, "material_count": 1, "blocker_counts": {}})
    assert result["join_blocker"] is None and result["group_deficit"] == 150
    assert result["join_advisories"] == ["join_wait_ad_reconciliation"]


async def test_forecast_and_lua_keep_listener_after_spent_transfer(lua_store, monkeypatch, efficiency):
    monkeypatch.setattr(rpc.settings, "TELEGRAM_READ_BUDGET_PERCENT", 100)
    limits = rpc.limits_for({}, datetime.utcnow(), 2)
    counts = {"hour": 234, "ad_hour": 84, "survival_hour": 30, "join_reserved_hour": 60,
              "sync_reserved_hour": 48, "flex_reserved_hour": 12, "critical_review_hour": 60,
              "survival_lent_hour": 36}
    for name, amount in counts.items():
        await lua_store.set(f"vanguard:rpc:2:{name}", amount, ex=1800)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=lua_store))
    state = await rpc.read_budget_state(2, limits, datetime.utcnow())
    assert state["lanes"]["critical"]["remaining"] == 1
    assert state["lanes"]["listener"]["remaining"] == 5
    assert await allocate(lua_store, 2, limits=limits) > 0
    assert await allocate(lua_store, 1, limits=limits) == 0
    assert await allocate(lua_store, 5, lane="listener", limits=limits) == 0
    assert await lua_store.get("vanguard:rpc:2:hour") == "240"
    assert await lua_store.get("vanguard:rpc:2:survival_lent_hour") == "36"


async def test_wake_coalesces_and_active_quantum_cannot_overwrite_new_event(lua_store, monkeypatch, efficiency):
    from app.core.scheduler import growth_dispatch as scheduler
    publish = Mock()
    monkeypatch.setattr(scheduler.dispatch_growth_task, "apply_async", publish)
    assert await scheduler.wake_account(2, stages=("review",), client=lua_store)
    assert not await scheduler.wake_account(2, stages=("join",), client=lua_store)
    assert publish.call_count == 1
    now = scheduler.time.time()
    assert await scheduler.reserve(lua_store, "join", 2, "ticket", now) == 1
    assert await scheduler.start(lua_store, "join", 2, "ticket", now) == 1
    assert await lua_store.zscore(scheduler.PREFIX + "join:wake_due", "2") is None
    await scheduler.wake_account(2, stages=("join",), client=lua_store)
    event_due = await lua_store.zscore(scheduler.PREFIX + "join:wake_due", "2")
    await scheduler.finish(lua_store, "join", 2, "ticket", now + 3600)
    assert await lua_store.zscore(scheduler.PREFIX + "join:due", "2") == event_due
    assert await lua_store.zscore(scheduler.PREFIX + "join:wake_due", "2") is None
    assert await scheduler.reserve(lua_store, "join", 2, "next", scheduler.time.time()) == 1
    assert await scheduler.reserve(lua_store, "join", 2, "duplicate", scheduler.time.time()) == 0


async def test_future_wake_is_local_and_does_not_coalesce_away_ready_event(lua_store, monkeypatch, efficiency):
    from app.core.scheduler import growth_dispatch as scheduler
    publish = Mock()
    monkeypatch.setattr(scheduler.dispatch_growth_task, "apply_async", publish)
    assert not await scheduler.wake_account(2, client=lua_store,
                                            ready_at=datetime.utcnow() + timedelta(hours=1))
    assert publish.call_count == 0
    assert await scheduler.wake_account(2, client=lua_store)
    assert publish.call_count == 1
    assert not await scheduler.wake_account(3, client=lua_store)
    assert await lua_store.get(scheduler.PREFIX + "wake:3") is None


async def test_new_due_dispatch_uses_work_not_immutable_audit_retries(test_db, monkeypatch, efficiency):
    from app.core.scheduler import growth_dispatch as scheduler
    from tests.unit.test_completion_capacity import configured
    _, account, _, member, audit, _ = await configured(test_db)
    now = datetime.utcnow()
    audit.state, audit.next_retry_at = "queued", now - timedelta(minutes=1)
    work = await engine.enqueue(test_db, member, initial_collection_pending=True)
    work.state, work.ready_at = "WAIT_MATURITY", now + timedelta(hours=1)
    await test_db.commit()
    assert account.id not in (await scheduler.account_sets(test_db, now))["review"]
    work.state, work.ready_at = "READY", now
    await test_db.commit()
    assert account.id in (await scheduler.account_sets(test_db, now))["review"]


@pytest.mark.parametrize("state", ["DONE", "WAIT_EVENT"])
@pytest.mark.parametrize("change", ["policy", "profile"])
async def test_scope_change_bootstraps_even_when_same_membership_has_old_work(
        test_db, monkeypatch, efficiency, state, change):
    from app.core.scheduler import growth_dispatch as scheduler
    from app.modules.acquisition.qualification_service import policy
    from tests.unit.test_completion_capacity import configured
    _, account, _, member, _, _ = await configured(test_db)
    config = dict(await policy(test_db, fresh=True))
    monkeypatch.setattr("app.modules.acquisition.qualification_service.policy", AsyncMock(return_value=config))
    now = datetime.utcnow()
    work = await engine.enqueue(test_db, member)
    work.state, work.ready_at = state, None
    await test_db.commit()
    assert account.id not in (await scheduler.account_sets(test_db, now))["review"]
    if change == "profile":
        account.profile_bio = "updated account advertising profile"
    else:
        config["system_user_ids"] = [*config.get("system_user_ids", []), 999999]
    await test_db.commit()
    assert account.id in (await scheduler.account_sets(test_db, now))["review"]
    # Repeated local dispatch keeps the scope repair visible until bootstrap.
    assert account.id in (await scheduler.account_sets(test_db, now))["review"]


async def test_business_work_is_due_in_exit_lane_only_for_current_scope(test_db, monkeypatch, efficiency):
    from app.core.scheduler import growth_dispatch as scheduler
    from app.modules.acquisition.qualification_service import policy
    from tests.unit.test_completion_capacity import configured
    _, account, _, member, _, _ = await configured(test_db)
    config = dict(await policy(test_db, fresh=True))
    config["execute_reviews"], config["execute_exits"] = False, True
    monkeypatch.setattr("app.modules.acquisition.qualification_service.policy", AsyncMock(return_value=config))
    now = datetime.utcnow()
    work = await engine.enqueue_business_maintenance(test_db, member, now=now)
    work.state, work.ready_at = "WAIT_EVENT", now + timedelta(hours=1)
    await test_db.commit()
    assert account.id not in (await scheduler.account_sets(test_db, now))["exit"]
    work.ready_at = now
    await test_db.commit()
    due = await scheduler.account_sets(test_db, now)
    assert account.id in due["exit"] and account.id not in due["review"]
    account.profile_bio = "new scope invalidates only the old maintenance work"
    await test_db.commit()
    assert account.id not in (await scheduler.account_sets(test_db, now))["exit"]
