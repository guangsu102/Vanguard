"""Exercise the existing Lua allocator through R18's actual read governor."""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import lupa  # noqa: F401 -- a missing Lua runtime is a failure, not a skipped case
import fakeredis.aioredis  # noqa: F401
import pytest
import pytest_asyncio

from app.core.account import read_work, rpc_governor as rpc
from app.core.account.budget_ledger import reserve_read_work as actual_reserve_read_work
from app.core.account.budget_ledger import finish_read_work as actual_finish_read_work
from app.modules.acquisition import qualification_engine as engine


@pytest_asyncio.fixture
async def lua_store():
    # Same FakeRedis+Lupa fixture as the existing allocator tests. All decisions
    # and charges execute RESERVED_BUDGET_LUA; this is not a second allocator.
    store = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield store
    finally:
        await store.aclose()


@pytest.fixture
def efficiency(monkeypatch):
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")


def limits(enabled=True):
    return {"minute": 1000, "hour": 3600, "day": 3600, "growth_efficiency_v1": int(enabled)}


async def seed_review_exhausted(store, *, join_used=0):
    # 25% * 3600 = 900; historical reviews have spent their 60% = 540.
    # Other business lanes are spent; only the protected 360 join + 72 listener
    # reads remain. This uses the existing total and per-lane Redis keys.
    values = {"ad_day": 1260, "survival_day": 468, "join_reserved_day": 540 + join_used,
              "sync_reserved_day": 720, "flex_reserved_day": 180,
              "critical_review_day": 540, "critical_join_day": join_used}
    values["day"] = sum(values[k] for k in (
        "ad_day", "survival_day", "join_reserved_day", "sync_reserved_day", "flex_reserved_day"))
    for name, amount in values.items():
        await store.set(f"vanguard:rpc:2:{name}", amount, ex=7200)
    return values["day"]


async def seed_production_windows(store):
    await seed_review_exhausted(store)
    hour = {"ad_hour": 126, "survival_hour": 46, "join_reserved_hour": 54,
            "sync_reserved_hour": 72, "flex_reserved_hour": 18,
            "critical_review_hour": 54, "critical_join_hour": 0, "hour": 316}
    for name, amount in hour.items():
        await store.set(f"vanguard:rpc:2:{name}", amount, ex=1800)
    # Preserve already spent donor transfers as well as the total's original TTL.
    for window, amounts in (("hour", (3, 2, 1)), ("day", (30, 20, 10))):
        for donor, amount in zip(("survival", "sync", "ad"), amounts):
            await store.set(f"vanguard:rpc:2:{donor}_lent_{window}", amount,
                            ex=1800 if window == "hour" else 7200)


async def spent_state(store):
    names = ["minute", "hour", "day"] + [f"{name}_{window}"
        for name in ("ad", "survival", "join_reserved", "sync_reserved", "flex_reserved",
                     "critical_join", "critical_review", "survival_lent", "sync_lent", "ad_lent")
        for window in ("hour", "day")]
    return {name: await store.get(f"vanguard:rpc:2:{name}") for name in names}


@pytest.fixture
def guarded(monkeypatch, lua_store, efficiency):
    @asynccontextmanager
    async def database():
        yield Obj()

    monkeypatch.setattr("app.core.database.get_db_session", database)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=lua_store))
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={}))
    state = {"state": "ready", "limits": limits(), "critical_workloads": {
        "join": {"remaining": 360, "retry_after_seconds": 0},
        "review": {"remaining": 0, "retry_after_seconds": 7200}}}
    monkeypatch.setattr(rpc, "snapshot", AsyncMock(side_effect=lambda *a, **kw: state))
    monkeypatch.setattr("app.core.account.budget_ledger.reserve_read_work", AsyncMock())
    monkeypatch.setattr("app.core.account.budget_ledger.finish_read_work", AsyncMock())
    monkeypatch.setattr("app.core.account.read_metrics.record_work", AsyncMock())
    monkeypatch.setattr("app.modules.acquisition.read_costs.charge_reads", Mock())
    monkeypatch.setattr("app.core.scheduler.growth_dispatch.wake_account", AsyncMock())
    for name in ("slice_started", "slice_finished", "budget_step_entered"):
        monkeypatch.setattr("app.modules.acquisition.qualification_sla_runtime." + name, Mock())
    proof = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "initial_join_budget_current", proof)
    claim_current = AsyncMock(return_value=True)
    binding = AsyncMock(return_value=True)
    finish = AsyncMock()
    monkeypatch.setattr(engine, "initial_join_claim_current", claim_current, raising=False)
    monkeypatch.setattr(engine, "bind_initial_join_read_work", binding, raising=False)
    monkeypatch.setattr(engine, "finish_initial_join_read_work", finish, raising=False)
    return Obj(store=lua_store, state=state, proof=proof, claim_current=claim_current,
               binding=binding, finish=finish)


def test_budget_class_preserves_kind_and_uses_purpose_fallback():
    work = read_work.ReadWork(2, 40, "review", budget_workload="join")
    assert read_work.budget_kind(work, "group_qualification") == "join"
    assert work.kind == "review" and work.summary()["budget_workload"] == "join"
    assert read_work.budget_kind(read_work.ReadWork(2, 40, "join_reconciliation")) == "join"
    assert read_work.budget_kind(read_work.ReadWork(2, 40, "review"), "group_qualification") == "review"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_context", [False, True])
async def test_first_collection_uses_join_floor_and_charges_actual_eight(guarded, monkeypatch, event_context):
    before = await seed_review_exhausted(guarded.store)
    from app.core.account.event_inbox import current_event_id
    event_token = current_event_id.set("fixture-event") if event_context else None
    estimate = AsyncMock(side_effect=AssertionError("old review samples cannot enlarge bounded first step"))
    monkeypatch.setattr(read_work, "review_required", estimate)
    try:
        async with read_work.operation(2, 40, "review", scope="60:joined") as work:
            work.budget_workload, work.budget_claim = "join", Obj(work_id=60)
            work.planned_reads, work.phase = 8, "first"
            governor = rpc.RpcGovernor(2, lambda: "group_qualification")
            await governor.before([])
            assert work.claimed and work.limit == 8
            assert await guarded.store.get("vanguard:rpc:2:day") == str(before)
            assert await guarded.store.hget(read_work.key(2), "kind") == "review"
            await governor.before(["messages.GetHistoryRequest"] * 8)
            assert work.reads == 8 and work.kind == "review"
            assert await guarded.store.get("vanguard:rpc:2:critical_review_day") == "540"
            assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "8"
            assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 8)
            with pytest.raises(rpc.RpcDeferred, match="telegram_read_slice"):
                await governor.before(["messages.GetHistoryRequest"])
            assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 8)
            assert guarded.proof.await_count == 3  # includes both claimed=True calls
            assert 7190 <= await guarded.store.ttl("vanguard:rpc:2:day") <= 7200
        assert not await guarded.store.exists(read_work.key(2))
    finally:
        if event_token is not None:
            current_event_id.reset(event_token)


@pytest.mark.asyncio
async def test_ordinary_review_cannot_take_unspent_join_floor(guarded):
    before = await seed_review_exhausted(guarded.store)
    async with read_work.operation(2, 40, "review") as work:
        work.planned_reads, work.phase = 8, "first"
        with pytest.raises(rpc.RpcDeferred, match="telegram_read_budget"):
            await rpc.RpcGovernor(2, lambda: "group_qualification").before([])
    assert not work.claimed and work.reads == 0
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before)
    guarded.proof.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_claim_blocks_next_rpc_without_refunding_prior_read(guarded):
    before = await seed_review_exhausted(guarded.store)
    async with read_work.operation(2, 40, "review") as work:
        work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), 8
        governor = rpc.RpcGovernor(2, lambda: "group_qualification")
        await governor.before(["messages.GetHistoryRequest"])
        await governor.failed(["messages.GetHistoryRequest"], ValueError("synthetic SDK failure"))
        guarded.proof.return_value = False
        with pytest.raises(rpc.RpcDeferred, match="qualification_work_claim_changed"):
            await governor.before(["messages.GetHistoryRequest"])
        assert work.reads == 1 and guarded.proof.await_count == 2
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 1)
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "1"
    assert not await guarded.store.exists(read_work.key(2))


@pytest.mark.asyncio
async def test_spent_first_token_falls_back_to_review_only_while_claim_current(guarded):
    guarded.proof.return_value = False
    async with read_work.operation(2, 40, "review") as work:
        work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), 8
        await rpc.RpcGovernor(2, lambda: "group_qualification").before(["messages.GetHistoryRequest"])
        assert work.budget_workload is None and work.budget_claim is None
    assert await guarded.store.get("vanguard:rpc:2:critical_review_day") == "1"
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") is None
    guarded.claim_current.assert_awaited_once()
    guarded.binding.assert_not_awaited()
    guarded.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_claim_cannot_downgrade_into_another_review_work(guarded):
    guarded.proof.return_value = guarded.claim_current.return_value = False
    async with read_work.operation(2, 40, "review") as work:
        work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), 8
        with pytest.raises(rpc.RpcDeferred, match="qualification_work_claim_changed"):
            await rpc.RpcGovernor(2, lambda: "group_qualification").before(["messages.GetHistoryRequest"])
    assert await guarded.store.get("vanguard:rpc:2:day") is None
    guarded.binding.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_binding_failure_releases_only_own_reservation_before_any_charge(guarded, cancelled):
    import asyncio

    before = await seed_review_exhausted(guarded.store)
    if cancelled:
        guarded.binding.side_effect = asyncio.CancelledError()
    else:
        guarded.binding.return_value = False
    expected = asyncio.CancelledError if cancelled else rpc.RpcDeferred
    with pytest.raises(expected):
        async with read_work.operation(2, 40, "review") as work:
            work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), 8
            await rpc.RpcGovernor(2, lambda: "group_qualification").before(["messages.GetHistoryRequest"])
    assert not work.claimed and work.reads == 0
    assert not await guarded.store.exists(read_work.key(2))
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before)
    guarded.finish.assert_awaited_once_with(work, failed=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("planned", [None, 0, 9, True])
async def test_first_collection_requires_its_bounded_plan(guarded, planned):
    before = await seed_review_exhausted(guarded.store)
    async with read_work.operation(2, 40, "review") as work:
        work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), planned
        with pytest.raises(rpc.RpcDeferred, match="telegram_rpc_guard_unavailable"):
            await rpc.RpcGovernor(2, lambda: "group_qualification").before([])
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before)
    assert not await guarded.store.exists(read_work.key(2))


@pytest.mark.asyncio
async def test_confirmation_reserves_eight_join_reads_without_rejoining_or_initial_slots(guarded, monkeypatch):
    before = await seed_review_exhausted(guarded.store)
    for path in ("app.modules.acquisition.review_read_plan.for_join",
                 "app.modules.acquisition.growth_admission.admission_plan",
                 "app.modules.acquisition.qualification_engine.initial_slots"):
        monkeypatch.setattr(path, AsyncMock(side_effect=AssertionError("confirmation cannot re-admit joining")))
    async with read_work.operation(2, 40, "join_reconciliation", scope="join:99") as work:
        work.planned_reads, work.budget_workload = 8, "join"
        governor = rpc.RpcGovernor(2, lambda: "join_request_reconciliation")
        await governor.before([])
        assert work.limit == 8 and work.claimed and work.review_reads == 0
        await governor.before(["channels.GetParticipantRequest"] * 8)
        assert work.reads == 8 and work.kind == "join_reconciliation"
    guarded.proof.assert_not_awaited()
    assert await guarded.store.get("vanguard:rpc:2:critical_review_day") == "540"
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "8"
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 8)


@pytest.mark.asyncio
async def test_disabled_confirmation_preserves_local_eight_join_reservation(guarded, monkeypatch):
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ENABLED", False)
    guarded.state["limits"] = limits(False)
    before = await seed_review_exhausted(guarded.store)
    async with read_work.operation(2, 40, "join_reconciliation", scope="join:99") as work:
        work.planned_reads = 8
        governor = rpc.RpcGovernor(2, lambda: "join_request_reconciliation")
        await governor.before([])
        # Preserve the current local disabled path: bounded fallback=8 and
        # join_reconciliation budget classification=join; R18 changes neither.
        assert work.limit == 8 and work.review_reads == read_work.DEFAULT_REVIEW_READS
        assert await guarded.store.hget(read_work.key(2), "remaining") == "8"
        assert work.budget_workload is None and work.budget_claim is None
        await governor.before(["channels.GetParticipantRequest"])
    guarded.proof.assert_not_awaited()
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 1)
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "1"


@pytest.mark.asyncio
async def test_disabled_review_ignores_explicit_join_override(guarded, monkeypatch):
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ENABLED", False)
    guarded.state["limits"] = limits(False)
    before = await seed_review_exhausted(guarded.store)
    async with read_work.operation(2, 40, "review") as work:
        work.planned_reads, work.budget_workload = 8, "join"
        await rpc.RpcGovernor(2, lambda: "group_qualification").before(["messages.GetHistoryRequest"])
    guarded.proof.assert_not_awaited()
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 1)
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "0"
    assert await guarded.store.get("vanguard:rpc:2:critical_review_day") == "541"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,room,passes,required", [(True, 8, True, 8), (True, 7, False, 8), (False, 8, False, 37)])
async def test_preview_without_work_uses_only_target_step_when_enabled(guarded, monkeypatch, enabled, room, passes, required):
    monkeypatch.setattr(engine.settings, "GROWTH_EFFICIENCY_ENABLED", enabled)
    guarded.state["limits"] = limits(enabled)
    guarded.state["critical_workloads"]["join"]["remaining"] = room
    before = await seed_review_exhausted(guarded.store, join_used=360-room)
    monkeypatch.setattr("app.modules.acquisition.growth_admission.admission_plan", AsyncMock(return_value={"remaining": 2, "reason": None}))
    wait = Mock(return_value=60)
    monkeypatch.setattr(read_work, "minimum_wait", wait)
    assert read_work.current(2) is None
    governor = rpc.RpcGovernor(2, lambda: "join_candidate_preview")
    if passes:
        await governor.before(["messages.GetHistoryRequest"])
        wait.assert_not_called()
        assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 1)
        assert await guarded.store.get("vanguard:rpc:2:critical_review_day") == "540"
    else:
        with pytest.raises(rpc.RpcDeferred, match="telegram_read_budget"):
            await governor.before(["messages.GetHistoryRequest"])
        assert wait.call_args.args[2] == required
        assert await guarded.store.get("vanguard:rpc:2:day") == str(before)
    assert not await guarded.store.exists(read_work.key(2))


@pytest.mark.asyncio
async def test_cleanup_cannot_delete_a_new_owner_reservation(guarded):
    await seed_review_exhausted(guarded.store)
    async with read_work.operation(2, 40, "review") as work:
        work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), 8
        await rpc.RpcGovernor(2, lambda: "group_qualification").before(["messages.GetHistoryRequest"])
        await guarded.store.hset(read_work.key(2), mapping={"token": "new-owner", "remaining": 7})
    assert await guarded.store.hget(read_work.key(2), "token") == "new-owner"
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "1"


@pytest.mark.asyncio
async def test_join_budget_class_keeps_real_ledger_work_identity_and_separate_journals(guarded, monkeypatch, test_db):
    from app.core.account.control_models import AccountBudgetReservation
    from app.core.config import settings
    from tests.unit.test_qualification_service import setup

    await setup(test_db)

    @asynccontextmanager
    async def database():
        yield test_db

    monkeypatch.setattr("app.core.database.get_db_session", database)
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr("app.core.account.budget_ledger.reserve_read_work", actual_reserve_read_work)
    monkeypatch.setattr("app.core.account.budget_ledger.finish_read_work", actual_finish_read_work)
    before = await seed_review_exhausted(guarded.store)
    reservations = []
    for group_id, cost in ((40, 3), (41, 2)):
        async with read_work.operation(2, group_id, "review", scope=f"member:{group_id}") as work:
            work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=group_id), 8
            governor = rpc.RpcGovernor(2, lambda: "group_qualification")
            await governor.before([])
            reservations.append(work.ledger_id)
            row = await test_db.get(AccountBudgetReservation, work.ledger_id, populate_existing=True)
            assert (row.workload, row.lane, row.reserved_reads, row.consumed_reads) == ("review", "critical", 8, 0)
            await governor.before(["messages.GetHistoryRequest"] * cost)
        await test_db.commit()
        row = await test_db.get(AccountBudgetReservation, work.ledger_id, populate_existing=True)
        assert (row.state, row.workload, row.consumed_reads) == ("settled", "review", cost)
    assert reservations[0] != reservations[1]
    assert [await guarded.store.get("vanguard:budget:consumed:" + rid) for rid in reservations] == ["3", "2"]
    assert await guarded.store.get("vanguard:rpc:2:day") == str(before + 5)
    assert await guarded.store.get("vanguard:rpc:2:critical_review_day") == "540"
    assert await guarded.store.get("vanguard:rpc:2:critical_join_day") == "5"


@pytest.mark.asyncio
async def test_real_180_360_3600_windows_pay_initial_eight_and_confirmation_eight(guarded):
    guarded.state["limits"] = {"minute": 180, "hour": 360, "day": 3600, "growth_efficiency_v1": 1}
    await seed_production_windows(guarded.store)
    old = await spent_state(guarded.store)
    for kind, purpose in (("review", "group_qualification"), ("join_reconciliation", "join_request_reconciliation")):
        async with read_work.operation(2, 40, kind, scope="member:60" if kind == "review" else "join:99") as work:
            work.budget_workload, work.planned_reads = "join", 8
            if kind == "review":
                work.budget_claim = Obj(work_id=60)
            await rpc.RpcGovernor(2, lambda: purpose).before(["messages.GetHistoryRequest"] * 8)
            assert work.reads == 8 and work.limit == 8
    state = await spent_state(guarded.store)
    assert (state["minute"], state["hour"], state["day"]) == ("16", "332", "3184")
    assert (state["critical_join_hour"], state["critical_join_day"]) == ("16", "16")
    assert (state["critical_review_hour"], state["critical_review_day"]) == ("54", "540")
    for name, value in old.items():
        if "lent_" in name:
            assert state[name] == value
    assert 1790 <= await guarded.store.ttl("vanguard:rpc:2:hour") <= 1800
    assert 7190 <= await guarded.store.ttl("vanguard:rpc:2:day") <= 7200


@pytest.mark.asyncio
@pytest.mark.parametrize("full_window", ["minute", "hour", "day"])
async def test_any_real_parent_window_exhaustion_blocks_floor_without_reset(guarded, full_window):
    actual = {"minute": 180, "hour": 360, "day": 3600, "growth_efficiency_v1": 1}
    guarded.state["limits"] = actual
    await seed_production_windows(guarded.store)
    await guarded.store.set(f"vanguard:rpc:2:{full_window}", actual[full_window], ex=1234)
    before = await spent_state(guarded.store)
    async with read_work.operation(2, 40, "review", scope="member:60") as work:
        work.budget_workload, work.budget_claim, work.planned_reads = "join", Obj(work_id=60), 8
        with pytest.raises(rpc.RpcDeferred, match="telegram_read_budget"):
            await rpc.RpcGovernor(2, lambda: "group_qualification").before(["messages.GetHistoryRequest"])
        assert not work.claimed and work.reads == 0
    assert await spent_state(guarded.store) == before
    assert 1224 <= await guarded.store.ttl(f"vanguard:rpc:2:{full_window}") <= 1234
    assert not await guarded.store.exists(read_work.key(2))
