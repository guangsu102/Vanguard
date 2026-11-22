"""Real SQLite provenance + existing Redis Lua + installed SDK governor chain."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import PeerChannel

from app.core.account import read_work, rpc_governor as rpc, read_metrics
from app.core.account.control_models import AccountBudgetReservation
from app.core.account.critical_fairness import add_workload_budgets
from app.modules.acquisition import qualification_engine as engine
from .test_read_budget_class import lua_store, seed_production_windows
from .test_initial_join_provenance import joined, isolated_rollout


def request():
    return GetHistoryRequest(PeerChannel(1234567890), 0, None, 0, 1, 0, 0, 0)


@pytest.fixture
def chain_services(test_db, lua_store, monkeypatch, isolated_rollout):
    @asynccontextmanager
    async def session():
        try:
            yield test_db
            await test_db.commit()
        except Exception:
            await test_db.rollback()
            raise

    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=lua_store))
    monkeypatch.setattr(engine.settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr(engine.settings, "TELEGRAM_READ_BUDGET_PERCENT", 150)
    monkeypatch.setattr("app.core.scheduler.growth_dispatch.wake_account", AsyncMock())
    # Keep the real SDK entry/attempt counters. Isolate only the telemetry sink
    # and background health producer; neither enforces spending or provenance.
    async def telemetry_sink(*args, resolved=None, **kwargs):
        if resolved is not None:
            read_metrics._state().pending_sdk.pop(id(resolved), None)
    monkeypatch.setattr(read_metrics, "_persist", telemetry_sink)
    monkeypatch.setattr(read_metrics, "start_metrics_health", Mock())
    for name in ("slice_started", "slice_finished", "budget_step_entered"):
        monkeypatch.setattr("app.modules.acquisition.qualification_sla_runtime." + name, Mock())

    async def measured_snapshot(db, account_id, now):
        limits = rpc.limits_for({}, now, account_id)
        assert (limits["minute"], limits["hour"], limits["day"]) == (180, 360, 3600)
        budget = await rpc.read_budget_state(account_id, limits, now)
        add_workload_budgets(budget, limits)
        return {**budget, "limits": limits, "state": "ready" if not budget["retry_after_seconds"] else "budget_wait",
                "reason": "telegram_read_budget" if budget["retry_after_seconds"] else None}
    monkeypatch.setattr(rpc, "snapshot", measured_snapshot)
    return lua_store


def client_with_governor(account_id, purpose):
    class Client:
        def __init__(self):
            self.sdk_calls = 0
            self.sdk_requests = 0
        async def _call(self, sender, payload, **kwargs):
            self.sdk_calls += 1
            self.sdk_requests += len(payload) if isinstance(payload, list) else 1
            return Obj(messages=[])
    client = Client()
    governor = rpc.RpcGovernor(account_id, lambda: purpose.value)
    rpc.install_governor(client, governor)
    return client, governor


def bind_candidate(work, claim):
    work.budget_workload, work.budget_claim, work.planned_reads, work.phase = "join", claim, 8, "first"


async def expect_next_token_review_wait(db, store, account_id, group_id, claim, client):
    calls = client.sdk_calls
    spent = await store.get("vanguard:rpc:2:day")
    async with read_work.operation(account_id, group_id, "review", scope="next") as later:
        bind_candidate(later, claim)
        with pytest.raises(rpc.RpcDeferred, match="telegram_read_budget"):
            await client._call(None, request())
        assert later.budget_workload is None and later.budget_claim is None
        assert later.entered_reads == later.reads == 0
    assert client.sdk_calls == calls and await store.get("vanguard:rpc:2:day") == spent


@pytest.mark.asyncio
async def test_real_chain_preview_join_reads_first_eight_then_next_token_is_review(test_db, chain_services):
    store = chain_services
    a, g, _, _, _, durable, claim = await joined(test_db)
    account_id, group_id = a.id, g.id
    await seed_production_windows(store)
    purpose = Obj(value="join_candidate_preview")
    client, governor = client_with_governor(a.id, purpose)
    await client._call(None, request())  # one real preview read, with no ReadWork
    purpose.value = "auto_join"
    async with read_work.operation(a.id, g.id, "join") as joining:
        await client._call(None, [request() for _ in range(7)])
        assert joining.reads == joining.entered_reads == 7
    assert await store.get("vanguard:rpc:2:critical_join_day") == "8"
    purpose.value = "group_qualification"
    async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as first:
        bind_candidate(first, claim)
        await client._call(None, [request() for _ in range(3)])
        source = json.loads(durable.progress_json)["join_initial_budget"]
        assert source["first_read_token"] == first.token
        await client._call(None, [request() for _ in range(5)])
        assert first.reads == first.entered_reads == first.limit == 8
        with pytest.raises(rpc.RpcDeferred, match="telegram_read_slice"):
            await client._call(None, request())
        assert first.kind == "review"
    assert not durable.initial_collection_pending
    assert await engine.initial_slots(test_db, a.id) == 0
    row = await test_db.get(AccountBudgetReservation, first.ledger_id, populate_existing=True)
    assert (row.state, row.workload, row.consumed_reads) == ("settled", "review", 8)
    assert (client.sdk_calls, client.sdk_requests) == (4, 16)
    assert await store.get("vanguard:rpc:2:critical_review_hour") == "54"
    assert await store.get("vanguard:rpc:2:critical_review_day") == "540"
    assert await store.get("vanguard:rpc:2:critical_join_hour") == "16"
    assert await store.get("vanguard:rpc:2:critical_join_day") == "16"
    assert 1790 <= await store.ttl("vanguard:rpc:2:hour") <= 1800
    assert 7190 <= await store.ttl("vanguard:rpc:2:day") <= 7200
    await expect_next_token_review_wait(test_db, store, account_id, group_id, claim, client)


@pytest.mark.asyncio
async def test_real_zero_sdk_completion_certificate_allows_exactly_new_current_slice(test_db, chain_services):
    store = chain_services
    a, g, _, _, _, durable, claim = await joined(test_db)
    account_id, group_id = a.id, g.id
    await seed_production_windows(store)
    client, governor = client_with_governor(a.id, Obj(value="group_qualification"))
    async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as zero:
        bind_candidate(zero, claim)
        await governor.before([])
        assert zero.claimed and zero.reads == zero.entered_reads == 0
    assert client.sdk_calls == 0
    assert json.loads(durable.progress_json)["join_initial_budget"]["zero_sdk_released"] is True
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id, work_token="new")
    async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as first:
        bind_candidate(first, claim)
        assert first.token != zero.token
        await client._call(None, request())
        assert first.entered_reads == first.reads == 1
    assert not durable.initial_collection_pending and client.sdk_calls == 1
    zero_row = await test_db.get(AccountBudgetReservation, zero.ledger_id, populate_existing=True)
    first_row = await test_db.get(AccountBudgetReservation, first.ledger_id, populate_existing=True)
    assert (zero_row.consumed_reads, first_row.consumed_reads) == (0, 1)
    await expect_next_token_review_wait(test_db, store, account_id, group_id, claim, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cancelled", "missing_journal", "swallowed_bind_ack_unknown"])
async def test_real_unknown_or_cancelled_zero_never_regrants(test_db, chain_services, monkeypatch, case):
    store = chain_services
    a, g, _, _, _, durable, claim = await joined(test_db)
    account_id, group_id = a.id, g.id
    await seed_production_windows(store)
    client, governor = client_with_governor(a.id, Obj(value="group_qualification"))
    if case == "swallowed_bind_ack_unknown":
        actual_bind = engine.bind_initial_join_read_work
        async def committed_then_lost_ack(*args, **kwargs):
            assert await actual_bind(*args, **kwargs)
            raise ConnectionError("synthetic commit acknowledgement lost")
        monkeypatch.setattr(engine, "bind_initial_join_read_work", committed_then_lost_ack)
        async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as first:
            bind_candidate(first, claim)
            # Match _assess_efficiency: a local guard error can become a
            # returned technical wait without escaping operation().
            with pytest.raises(rpc.RpcDeferred, match="telegram_rpc_guard_unavailable"):
                await client._call(None, request())
            assert first.budget_bind_failed
    elif case == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as first:
                bind_candidate(first, claim)
                await governor.before([])
                raise asyncio.CancelledError()
    else:
        async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as first:
            bind_candidate(first, claim)
            await governor.before([])
            await store.delete("vanguard:budget:consumed:" + first.ledger_id)
    assert client.sdk_calls == 0 and first.reads == first.entered_reads == 0
    assert json.loads(durable.progress_json)["join_initial_budget"]["zero_sdk_released"] is False
    assert not durable.initial_collection_pending
    assert not await engine.initial_join_budget_current(test_db, claim, account_id, group_id, work_token="next")
    assert await engine.initial_slots(test_db, account_id) == 0
    await expect_next_token_review_wait(test_db, store, account_id, group_id, claim, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["claim_owner", "membership_generation", "session_owner"])
async def test_real_fence_or_owner_loss_never_enters_sdk(test_db, chain_services, case):
    store = chain_services
    a, g, member, _, _, durable, claim = await joined(test_db)
    account_id, group_id = a.id, g.id
    await seed_production_windows(store)
    client, governor = client_with_governor(a.id, Obj(value="group_qualification"))
    with pytest.raises((rpc.RpcDeferred, RuntimeError)):
        async with read_work.operation(a.id, g.id, "review", scope=claim.scope_key) as first:
            bind_candidate(first, claim)
            await governor.before([])
            if case == "claim_owner":
                durable.claim_token = "replacement-owner"
                await test_db.commit()
            elif case == "membership_generation":
                member.joined_at += timedelta(seconds=1)
                await test_db.commit()
            else:
                client._vanguard_session_guard = Obj(assert_owned=AsyncMock(side_effect=RuntimeError("session owner lost")))
            await client._call(None, request())
    assert client.sdk_calls == 0 and first.entered_reads == 0
    assert first.reads == (1 if case == "session_owner" else 0)
    assert await store.get("vanguard:rpc:2:critical_join_day") == ("1" if case == "session_owner" else "0")
    assert not await engine.initial_join_budget_current(test_db, claim, account_id, group_id, work_token="next")
