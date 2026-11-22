"""Use the original ledger/journal to bound the first protected slice."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account import budget_ledger, read_work
from app.core.account.control_models import AccountBudgetReservation
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
from app.modules.acquisition import qualification_engine as engine
from .test_read_budget_class import lua_store, limits, seed_review_exhausted
from .test_initial_join_provenance import joined, isolated_rollout


@pytest.fixture
def local_services(test_db, lua_store, monkeypatch):
    @asynccontextmanager
    async def session():
        yield test_db
    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=lua_store))
    monkeypatch.setattr(engine.settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


async def reservation(db, store, claim, account, group, *, token=None):
    work = read_work.ReadWork(account.id, group.id, "review", token=token) if token else read_work.ReadWork(account.id, group.id, "review")
    work.planned_reads, work.limit = 8, 8
    work.budget_workload, work.budget_claim = "join", claim
    keys, args = reservation_args(account.id, limits(), "critical", 8, 0,
        workload="join", work_token=work.token, work_mode="reserve", work_kind="review",
        work_group=group.id, work_scope=claim.scope_key)
    assert await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
    await budget_ledger.reserve_read_work(db, store, work, 8)
    return work


async def test_missing_ledger_cannot_register_or_enter_sdk(test_db, lua_store, local_services):
    a, g, _, _, _, _, claim = await joined(test_db)
    assert not await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, "unreserved", 8)
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id)


async def test_crash_after_ledger_before_marker_cannot_have_entered_sdk(test_db, lua_store, local_services):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert first.ledger_id and first.reads == first.entered_reads == 0
    assert "first_read_token" not in json.loads(work.progress_json)["join_initial_budget"]
    # With no marker commit the governor has not reached its SDK boundary.
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id, work_token="next")
    assert await lua_store.get(budget_ledger.CONSUMED_PREFIX+first.ledger_id) == "0"
    assert await lua_store.get("vanguard:rpc:2:critical_join_day") == "0"


async def test_crash_after_marker_before_sdk_consumes_only_this_entitlement(test_db, lua_store, local_services):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id, work_token=first.token)
    assert not await engine.initial_join_budget_current(test_db, claim, a.id, g.id, work_token="next")
    assert await engine.initial_slots(test_db, a.id) == 1
    work.claim_until = datetime.utcnow()-timedelta(seconds=1)
    await test_db.commit()
    assert await engine.initial_slots(test_db, a.id) == 0
    assert await lua_store.get(budget_ledger.CONSUMED_PREFIX+first.ledger_id) == "0"
    assert await lua_store.get("vanguard:rpc:2:critical_join_day") == "0"


async def test_one_charged_read_ends_first_slice_and_releases_slot(test_db, lua_store, local_services):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    first.claimed = True
    current = read_work._current.set(first)
    try:
        keys, args = reservation_args(a.id, limits(), "critical", 1, 0, workload="join")
        assert await lua_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
    finally:
        read_work._current.reset(current)
    first.reads = first.entered_reads = 1
    first.closed = True
    await budget_ledger.finish_read_work(first)
    await engine.finish_initial_join_read_work(first, failed=False)
    assert not work.initial_collection_pending
    assert await engine.initial_slots(test_db, a.id) == 0
    assert not await engine.initial_join_budget_current(test_db, claim, a.id, g.id, work_token="next")
    assert await lua_store.get("vanguard:rpc:2:critical_join_day") == "1"
    assert await lua_store.get("vanguard:rpc:2:critical_review_day") == "540"
    row = await test_db.get(AccountBudgetReservation, first.ledger_id)
    assert (row.workload, row.state, row.consumed_reads) == ("review", "settled", 1)


@pytest.mark.parametrize("case", ["clean_zero", "cancelled", "missing_journal", "missing_settlement", "sdk_unknown", "bind_unknown"])
async def test_zero_sdk_retry_requires_complete_positive_zero_proof(test_db, lua_store, local_services, case):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    if case == "missing_journal":
        await lua_store.delete(budget_ledger.CONSUMED_PREFIX+first.ledger_id)
    if case == "sdk_unknown":
        first.entered_reads = 1
    if case == "bind_unknown":
        first.budget_bind_failed = True
    if case != "missing_settlement":
        await budget_ledger.finish_read_work(first)
    first.closed = True
    await engine.finish_initial_join_read_work(first, failed=case == "cancelled")
    source = json.loads(work.progress_json)["join_initial_budget"]
    assert source["zero_sdk_released"] is (case == "clean_zero")
    assert work.initial_collection_pending is (case == "clean_zero")
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id,
                                                     work_token="next") is (case == "clean_zero")
    assert await engine.initial_slots(test_db, a.id) == int(case == "clean_zero")
    assert await lua_store.get("vanguard:rpc:2:critical_join_day") == "0"


async def test_expired_zero_journal_without_sdk_release_certificate_never_regrants(test_db, lua_store, local_services):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    assert await budget_ledger.settle(test_db, first.ledger_id, 0, state="expired", reason="lease_expired")
    work.state, work.claim_token, work.claim_until = "READY", None, None
    await test_db.commit()
    next_claim = await engine.claim_next(test_db, a.id)
    await test_db.commit()
    assert await engine.initial_join_claim_current(test_db, next_claim, a.id, g.id)
    assert not await engine.initial_join_budget_current(test_db, next_claim, a.id, g.id, work_token="next")
    assert await engine.initial_slots(test_db, a.id) == 0


async def test_expired_claim_cannot_certify_zero_sdk_release(test_db, lua_store, local_services):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    await budget_ledger.finish_read_work(first)
    first.closed = True
    work.claim_until = datetime.utcnow()-timedelta(seconds=1)
    await test_db.commit()
    await engine.finish_initial_join_read_work(first, failed=False)
    assert json.loads(work.progress_json)["join_initial_budget"]["zero_sdk_released"] is False
    assert await engine.initial_slots(test_db, a.id) == 0


async def test_bind_commit_then_owner_change_is_rechecked_before_sdk(test_db, lua_store, local_services, monkeypatch):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    commit = test_db.commit
    async def replaced_owner():
        await commit()
        work.claim_token = "replacement-owner"
        await commit()
    monkeypatch.setattr(test_db, "commit", replaced_owner)
    assert not await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    assert first.reads == first.entered_reads == 0
    assert json.loads(work.progress_json)["join_initial_budget"]["first_read_token"] == first.token
    assert await lua_store.get("vanguard:rpc:2:critical_join_day") == "0"


@pytest.mark.parametrize("case", ["slow", "error", "cancel"])
async def test_zero_journal_io_never_holds_a_pg_transaction(test_db, lua_store, local_services, monkeypatch, case):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    await budget_ledger.finish_read_work(first)
    first.closed = True
    await test_db.commit()
    pg_active, pg_entries = False, []
    journal_entered = asyncio.Event()
    @asynccontextmanager
    async def short_pg():
        nonlocal pg_active
        pg_entries.append("entered")
        pg_active = True
        try:
            yield test_db
        finally:
            pg_active = False
    class Journal:
        async def get(self, key):
            assert not pg_active
            journal_entered.set()
            if case == "error":
                raise ConnectionError("local journal unavailable")
            await asyncio.Future()
    monkeypatch.setattr("app.core.database.get_db_session", short_pg)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=Journal()))
    task = asyncio.create_task(engine.finish_initial_join_read_work(first, failed=False))
    await journal_entered.wait()
    if case != "error":
        assert pg_entries == []
    if case == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pg_entries == []
    else:
        await task
        assert pg_entries == ["entered"]
        assert not work.initial_collection_pending
    assert not pg_active
    assert json.loads(work.progress_json)["join_initial_budget"]["zero_sdk_released"] is False


async def test_open_token_cannot_be_observed_or_finished_as_zero(test_db, lua_store, local_services, monkeypatch):
    a, g, _, _, _, work, claim = await joined(test_db)
    await seed_review_exhausted(lua_store)
    first = await reservation(test_db, lua_store, claim, a, g)
    assert await engine.bind_initial_join_read_work(test_db, claim, a.id, g.id, first.token, 8)
    probe = AsyncMock(side_effect=AssertionError("open token cannot get a zero certificate"))
    monkeypatch.setattr("app.core.redis.get_redis", probe)
    await engine.finish_initial_join_read_work(first, failed=False)
    probe.assert_not_awaited()
    assert work.initial_collection_pending
    assert json.loads(work.progress_json)["join_initial_budget"]["zero_sdk_released"] is False
