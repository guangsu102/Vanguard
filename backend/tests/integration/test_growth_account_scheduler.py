"""Actual SQL selection and Redis Lua under 30-account contention; no Telegram I/O."""
import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.scheduler import growth_dispatch as scheduler
from app.modules.acquisition import qualification_service as reviews
from app.modules.acquisition.models import GroupQualificationAudit

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def queue_store():
    socket = os.getenv("GROWTH_TEST_REDIS_SOCKET")
    if not socket:
        pytest.skip("requires isolated Redis Unix socket (never a production URL)")
    client = Redis(unix_socket_path=socket, db=14, decode_responses=True)
    # Only an explicit test socket is accepted; this DB belongs to the disposable container.
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def add_accounts(db, count=30):
    accounts = [TelegramAccount(id=i, identifier=f"test-{i}", session_name=f"test-{i}",
                                account_type=AccountType.PROMOTER, status=AccountStatus.ONLINE,
                                is_active=True, risk_level="normal") for i in range(1, count + 1)]
    db.add_all(accounts)
    await db.commit()
    return accounts


async def test_thirty_accounts_unfunded_plan_completes_one_renewal_and_retains_sends(
    queue_store, monkeypatch,
):
    from app.core import redis as redis_module
    from app.core.account import rpc_governor as rpc
    from app.core.account.demand_lending import add_refresh_demand_headroom
    from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args

    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=queue_store))
    monkeypatch.setattr(rpc.settings, "TELEGRAM_READ_BUDGET_PERCENT", 150)
    now = datetime.utcnow()
    usage = {"day": 2069, "ad_day": 747, "survival_day": 174,
             "join_reserved_day": 660, "sync_reserved_day": 399, "flex_reserved_day": 89}
    demand = {"delivery_cost": 5, "renewal_cost": 11,
              "hour": {"sends": 5, "renewals": 1},
              "day": {"sends": 103, "renewals": 23}}

    async def allocate(account_id, limits, cost, *, token="", reserve=False, renewal=False):
        keys, args = reservation_args(
            account_id, limits, "ad", cost, 0, work_token=token,
            work_mode="reserve" if reserve else "read", work_kind="renewal",
            ad_refresh=renewal,
        )
        return await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)

    for account_id in range(1, 31):
        for name, value in usage.items():
            await queue_store.set(f"vanguard:rpc:{account_id}:{name}", value, ex=60000)
        limits = rpc.limits_for({}, now)
        budget = await rpc.read_budget_state(account_id, limits, now)
        add_refresh_demand_headroom(limits, budget, demand)
        assert budget["lanes"]["ad_refresh"]["remaining"] == 16
        claims = await asyncio.gather(*(
            allocate(account_id, limits, 16, token=str(i), reserve=True, renewal=True)
            for i in range(10)
        ))
        assert claims.count(0) == 1
        owner = str(claims.index(0))
        assert await queue_store.get(f"vanguard:rpc:{account_id}:day") == "2069"
        assert await allocate(account_id, limits, 5) == 0
        assert await allocate(account_id, limits, 16, token=owner, renewal=True) == 0
        assert await allocate(account_id, limits, 5) == 0
        assert await queue_store.get(f"vanguard:rpc:{account_id}:day") == "2095"
        assert 59990 <= await queue_store.ttl(f"vanguard:rpc:{account_id}:day") <= 60000


@pytest.mark.parametrize('window', ['hour', 'day'])
@pytest.mark.parametrize('percent', [100, 150])
async def test_thirty_accounts_refresh_backlog_preserves_real_send_budget(queue_store, monkeypatch, window, percent):
    from app.core import redis as redis_module
    from app.core.account import rpc_governor as rpc
    from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
    from app.core.account.critical_fairness import purpose_budget
    from app.modules.acquisition.ad_pacing import pacing_deadline

    monkeypatch.setattr(redis_module, 'get_redis', AsyncMock(return_value=queue_store))
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', percent)
    now = datetime.utcnow()
    limits = rpc.limits_for({}, now)
    for account_id in range(1, 31):
        if window == 'day':
            # Prior-hour reads still count against the same daily window.
            prior = limits['ad_day'] * 3 // 5 - 1
            await queue_store.set(f'vanguard:rpc:{account_id}:day', prior, ex=60000)
            await queue_store.set(f'vanguard:rpc:{account_id}:ad_day', prior, ex=60000)
        spend = limits['ad_hour'] * 3 // 5 if window == 'hour' else 1
        keys, args = reservation_args(account_id, limits, 'ad', spend, 0, ad_refresh=True)
        assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
        keys, args = reservation_args(account_id, limits, 'ad', 1, 0, ad_refresh=True)
        assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) > 0
        state = await rpc.read_budget_state(account_id, limits, now)
        assert state['lanes']['ad']['remaining'] == {
            (100, 'hour'): 46, (100, 'day'): 95,
            (150, 'hour'): 69, (150, 'day'): 143,
        }[percent, window]
        assert purpose_budget(state, 'ad_qualification_refresh')['remaining'] == 0
        due = pacing_deadline({**state, 'limits':limits}, capacity=38, delivery_cost=12,
                              last_sent=now-timedelta(hours=2), now=now)
        assert due <= now  # The separate 80% pacing planner still permits sending.
        # A normal 12-read delivery can still finish on every account.
        keys, args = reservation_args(account_id, limits, 'ad', 12, 0)
        assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
        assert int(await queue_store.get(f'vanguard:rpc:{account_id}:hour')) == spend + 12
        # Renewal cannot borrow initial review/join reservations to bypass its hold.
        keys, args = reservation_args(account_id, limits, 'critical', 12, 0, workload='review')
        assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0


async def test_budget_expansion_preserves_spending_and_original_expiries(queue_store, monkeypatch):
    from app.core.account import rpc_governor as rpc
    from app.core.account.reserved_reads import COUNTERS, RESERVED_BUDGET_LUA, allocations, reservation_args

    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 100)
    old = rpc.limits_for({}, datetime.utcnow())
    for window, ttl in [('hour', 1200), ('day', 60000)]:
        await queue_store.set(f'vanguard:rpc:2:{window}', old[window], ex=ttl)
        for name, used in zip(COUNTERS, allocations(old[window])):
            await queue_store.set(f'vanguard:rpc:2:{name}_{window}', used, ex=ttl)
    keys, args = reservation_args(2, old, 'ad', 12, 0)
    assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) > 0
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 150)
    expanded = rpc.limits_for({}, datetime.utcnow())
    keys, args = reservation_args(2, expanded, 'ad', 12, 0)
    assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
    for window, ttl in [('hour', 1200), ('day', 60000)]:
        assert int(await queue_store.get(f'vanguard:rpc:2:{window}')) == old[window] + 12
        assert int(await queue_store.get(f'vanguard:rpc:2:ad_{window}')) == old[f'ad_{window}'] + 12
        assert ttl - 5 <= await queue_store.ttl(f'vanguard:rpc:2:{window}') <= ttl
        assert int(await queue_store.get(f'vanguard:rpc:2:join_reserved_{window}')) == old[f'critical_{window}']


async def test_thirty_accounts_review_progress_despite_due_exits(test_db, monkeypatch):
    from app.core.account import read_schedule
    accounts = await add_accounts(test_db)
    accounts[0].risk_level = "quarantined"
    now = datetime.utcnow()
    for account in accounts:
        group = Group(group_id=-1000000000000-account.id)
        test_db.add(group)
        await test_db.flush()
        member = GroupAccountMembership(account_id=account.id, group_id=group.id,
            telegram_group_id=group.group_id, status="joined", joined_at=now-timedelta(days=2),
            review_status="initial_pending", ad_status="blocked")
        test_db.add(member)
        await test_db.flush()
        test_db.add(GroupQualificationAudit(batch_id=f"review-{account.id}", account_id=account.id, group_id=group.id,
            membership_id=member.id, membership_joined_at=member.joined_at,
            policy_version=reviews.POLICY_VERSION, content_scope="text_profile",
            state="queued", next_retry_at=now-timedelta(hours=1)))
        # A separate exit would previously veto every review for this account.
        exit_group = Group(group_id=-1000000010000-account.id)
        test_db.add(exit_group)
        await test_db.flush()
        exit_member = GroupAccountMembership(account_id=account.id, group_id=exit_group.id,
            telegram_group_id=exit_group.group_id, status="joined", joined_at=now-timedelta(days=2),
            review_status="exit_pending", ad_status="blocked", review_next_at=now)
        test_db.add(exit_member)
        await test_db.flush()
        test_db.add(GroupQualificationAudit(batch_id=f"exit-{account.id}", account_id=account.id, group_id=exit_group.id,
            membership_id=exit_member.id, membership_joined_at=exit_member.joined_at,
            policy_version=reviews.POLICY_VERSION, content_scope="text_profile",
            state="completed", decision="reject", next_retry_at=None))
    await test_db.commit()
    monkeypatch.setattr(reviews, "policy", AsyncMock(return_value={"enabled": True, "execute_exits": True, "exit_all_accounts": True}))
    monkeypatch.setattr(read_schedule, "read_wait", AsyncMock(return_value=None))
    seen = []
    async def assess(service, account_id, group, *, row):
        seen.append(account_id)
        row.state, row.decision, row.reason = "completed", "allowed", "test_evidence"
        row.next_retry_at = None
        row.checked_at = now
        row.expires_at = now + timedelta(days=1)
        return Obj(passed=True, reason="test_evidence")
    monkeypatch.setattr(reviews, "assess", assess)
    actor = Obj(db=test_db, _sync_group_ad_policy_from_audit=AsyncMock())
    for account in accounts:
        await reviews.run_reviews(actor, account_id=account.id, limit=1)
    assert seen == list(range(2, 31))
    assert list((await test_db.scalars(scheduler.eligible_accounts(now))).all()) == list(range(2, 31))


async def test_frequency_deferrals_do_not_suppress_ordinary_exit_lane(test_db, monkeypatch):
    from app.modules.acquisition import qualification_actions as actions, adaptive_frequency
    monkeypatch.setattr(actions, "get_settings", lambda: Obj(APP_ENV="test"))
    monkeypatch.setattr(actions, "policy", AsyncMock(return_value={"enabled": True, "execute_exits": True, "exit_all_accounts": True}))
    monkeypatch.setattr(adaptive_frequency, "run_frequency_exits", AsyncMock(return_value={
        "processed": 1, "results": [{"reason": "qualification_account_unavailable"}]}))
    ordinary = AsyncMock(return_value={"processed": 1, "results": [{"status": "left"}]})
    monkeypatch.setattr(actions, "_run_exits_serialized", ordinary)
    monkeypatch.setattr(actions, "reconcile_stale_exit_memberships", AsyncMock(return_value={}))
    for _ in range(30):
        result = await actions.run_exits(Obj(db=test_db), account_id=30)
        assert result["results"][-1]["status"] == "left"
    assert ordinary.await_count == 30


async def test_offline_consumers_keep_physical_queues_bounded_and_resume_fairly(queue_store, monkeypatch):
    cache = queue_store
    available = {stage: list(range(1, 31)) for stage in scheduler.STAGES}
    monkeypatch.setattr(scheduler, "account_sets", AsyncMock(return_value=available))
    db = Obj(commit=AsyncMock())
    published = []
    async def publish(stage, account_id, token, queue):
        value = json.dumps([stage, account_id, token])
        await cache.lpush(queue, value)
        published.append((stage, account_id, token, queue))
    now = 100000.0
    for _ in range(200):
        await scheduler.dispatch(db, cache, cache, publish, now=now)
        # Simulate broker-unavailable execution for arbitrarily long time.
        # Tickets expire, while the physical Celery deliveries remain queued.
        for stage, account_id, _, _ in published:
            await cache.delete(scheduler.ticket_key(stage, account_id))
        now += 180
    for queue in {spec.queue for spec in scheduler.STAGES.values()}:
        assert await scheduler.broker_depth(cache, queue) <= scheduler.BROKER_CAP
    assert len(published) <= 4 * scheduler.BROKER_CAP
    # Stale copies cannot acquire an execution slot after replacement/expiry.
    for stage, account_id, token, _ in published:
        assert await scheduler.start(cache, stage, account_id, token, now) == 0


async def test_thirty_account_overload_fairness_and_recovery(queue_store, monkeypatch):
    """Six simulated hours: slow account, cooling account, restart, sustained load."""
    cache = queue_store
    available = {stage: list(range(1, 31)) for stage in scheduler.STAGES}
    monkeypatch.setattr(scheduler, "account_sets", AsyncMock(return_value=available))
    db = Obj(commit=AsyncMock())
    queues = {spec.queue: [] for spec in scheduler.STAGES.values()}
    queued_at = {}
    expired = set()
    async def publish(stage, account_id, token, queue):
        job = (stage, account_id, token)
        queues[queue].append(job)
        queued_at[token] = now
        await cache.lpush(queue, json.dumps(job))
    done = Counter()
    first = {}
    running = []
    peak = Counter()
    # A long join cannot occupy the dedicated advertising consumer.
    durations = {"ads": 3, "join": 20, "review": 8, "survival": 4,
                 "reconcile": 4, "verify": 4, "exit": 8}
    now = 100000.0
    for tick in range(1440):
        elapsed = tick * 15
        # Account 1 is unavailable for one hour, then recovers.
        for stage in available:
            available[stage] = list(range(2 if elapsed < 3600 else 1, 31))
        for job, end in list(running):
            if end <= now:
                stage, account_id, token = job
                await scheduler.finish(cache, stage, account_id, token, now + scheduler.STAGES[stage].interval)
                done[(stage, account_id)] += 1
                first.setdefault((stage, account_id), elapsed)
                running.remove((job, end))
        # Advance queued leases and Celery expiry on the simulated clock too.
        # Otherwise a five-minute join would incorrectly execute deliveries
        # whose actual two-minute queue lifetime had already elapsed.
        for jobs in queues.values():
            for stage, account_id, token in jobs:
                if token not in expired and now >= queued_at[token] + scheduler.QUEUED_SECONDS:
                    await cache.eval(scheduler.UNLOCK, 1, scheduler.ticket_key(stage, account_id), "q:" + token)
                    expired.add(token)
        await scheduler.dispatch(db, cache, cache, publish, now=now)
        occupied = Counter(scheduler.STAGES[job[0]].queue for job, _ in running)
        for queue in queues:
            concurrency = 2 if queue == "qualification" else 1
            while occupied[queue] < concurrency and queues[queue]:
                job = queues[queue].pop(0)
                assert await cache.rpop(queue) == json.dumps(job)
                stage, account_id, token = job
                if token in expired:
                    assert await scheduler.start(cache, stage, account_id, token, now) == 0
                    continue
                claimed = await scheduler.start(cache, stage, account_id, token, now)
                if claimed < 0:
                    await scheduler.finish(cache, stage, account_id, token, now + 15)
                elif claimed > 0:
                    duration = 300 if stage == "join" and account_id == 2 else durations[stage]
                    running.append((job, now + duration))
                    occupied[queue] += 1
        assert len(running) <= 5
        for queue in queues:
            peak[queue] = max(peak[queue], len(queues[queue]))
            assert len(queues[queue]) <= scheduler.BROKER_CAP
        now += 15
    # Every healthy and recovered account progresses in every stage, including IDs 27-30.
    assert all(done[(stage, account)] > 0 for stage in available for account in range(1, 31))
    assert max(first[("ads", i)] for i in range(2, 31)) < 15 * 60
    assert max(first[("join", i)] for i in range(2, 31)) < 30 * 60
    assert expired
    assert done[("ads", 30)] >= 36  # At least the 144/day planning rate over six hours.
    assert done[("review", 30)] * scheduler.STAGES["review"].quantum >= 75
    print("SCHEDULER_LOAD=" + json.dumps({
        "accounts": 30, "simulated_hours": 6, "peak_queues": peak,
        "expired_queued_deliveries": len(expired),
        "minimum_joins_per_healthy_account": min(done[("join", i)] for i in range(2, 31)),
        "max_first_join_seconds": max(first[("join", i)] for i in range(2, 31)),
        "minimum_ads_per_healthy_account": min(done[("ads", i)] for i in range(2, 31)),
        "minimum_reviews_per_healthy_account": min(done[("review", i)] * 2 for i in range(2, 31)),
        "max_first_ad_seconds": max(first[("ads", i)] for i in range(2, 31)),
    }))


async def test_duplicate_claim_crash_recovery_and_old_release_fencing(queue_store):
    cache = queue_store
    now = 1000.0
    assert await scheduler.reserve(cache, "ads", 30, "old", now) == 1
    assert await scheduler.reserve(cache, "ads", 30, "duplicate", now) == 0
    assert await scheduler.start(cache, "ads", 30, "old", now) == 1
    assert await scheduler.start(cache, "ads", 30, "old", now) == 0
    # Loss of a process leaves the ticket until its bounded lease expires.
    await cache.pexpire(scheduler.ticket_key("ads", 30), 1)
    await asyncio.sleep(.02)
    now += scheduler.RUN_SECONDS + 1
    assert await scheduler.reserve(cache, "ads", 30, "new", now) == 1
    await scheduler.finish(cache, "ads", 30, "old", now + 3600)
    assert await scheduler.start(cache, "ads", 30, "new", now) == 1


async def test_busy_non_ad_slots_do_not_take_reserved_ad_slot(queue_store):
    cache = queue_store
    for account in (1, 2):
        assert await scheduler.reserve(cache, "review", account, str(account), 1000) == 1
        assert await scheduler.start(cache, "review", account, str(account), 1000) == 1
    assert await scheduler.reserve(cache, "join", 30, "join", 1000) == 1
    assert await scheduler.start(cache, "join", 30, "join", 1000) == 1
    assert await scheduler.reserve(cache, "review", 30, "review-full", 1000) == 1
    assert await scheduler.start(cache, "review", 30, "review-full", 1000) == -1
    assert await scheduler.reserve(cache, "exit", 30, "maintenance", 1000) == 1
    assert await scheduler.start(cache, "exit", 30, "maintenance", 1000) == 1
    assert await scheduler.reserve(cache, "ads", 30, "ad", 1000) == 1
    assert await scheduler.start(cache, "ads", 30, "ad", 1000) == 1


@pytest.mark.parametrize("stage,result,sent,joined,reason", [
    ("ads", {"processed": 1, "succeeded": 1}, 1, 0, None),
    ("join", {"processed": 1, "succeeded": 1, "details": [{"action": "joined_pending_review"}]}, 0, 1, None),
    ("join", {"processed": 1, "skipped": 1, "details": [{"reason": "join_review_backlog"}]}, 0, 0, "join_review_backlog"),
])
async def test_runtime_observation_uses_business_results(queue_store, test_db, monkeypatch, stage, result, sent, joined, reason):
    import time
    from app.core import redis
    from app.core.scheduler import tasks
    monkeypatch.setattr(redis, "get_redis", AsyncMock(return_value=queue_store))
    async def with_db(fn):
        return await fn(test_db)
    monkeypatch.setattr(tasks, "_run_with_db", with_db)
    monkeypatch.setattr(scheduler, "execute", AsyncMock(return_value=(result, 60)))
    assert await scheduler.reserve(queue_store, stage, 30, "observation", time.time()) == 1
    await scheduler.run_ticket(stage, 30, "observation")
    latest = json.loads(await queue_store.hget(scheduler.PREFIX + "last_runs", f"{stage}:30"))
    assert (latest["sent"], latest["joined"], latest["reason"]) == (sent, joined, reason)
    assert latest["succeeded"] == result.get("succeeded", 0)
    assert latest["skipped"] == result.get("skipped", 0)


async def test_stalled_reviews_still_apply_admission_backpressure(test_db, monkeypatch):
    from app.modules.acquisition import capacity
    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    await add_accounts(test_db, 1)
    config = AccountOperationConfig(account_id=1, dynamic_capacity_enabled=True)
    test_db.add(config)
    await test_db.commit()
    inventory = {"active_backlog": 12, "overdue": 0, "probe_active_backlog": 12,
                 "probe_review_credit": 0, "probe_overdue": 0}
    monkeypatch.setattr(capacity, "inventory_snapshot", AsyncMock(return_value=inventory))
    budget = JoinRequestBudgetService(test_db)
    assert (await budget.refresh_review_backlog_pause(1))[2] is True
    inventory["probe_active_backlog"] = 7
    assert (await budget.refresh_review_backlog_pause(1))[2] is True
    inventory["probe_active_backlog"] = 6
    assert (await budget.refresh_review_backlog_pause(1))[2] is False


async def test_poisoned_review_is_deferred_and_same_account_keeps_progress(test_db, monkeypatch):
    from app.core.account import read_schedule
    await add_accounts(test_db, 1)
    now = datetime.utcnow()
    ids = []
    for n in range(2):
        group = Group(group_id=-1000000040000-n)
        test_db.add(group)
        await test_db.flush()
        member = GroupAccountMembership(account_id=1, group_id=group.id,
            telegram_group_id=group.group_id, status="joined", joined_at=now,
            review_status="initial_pending", ad_status="blocked")
        test_db.add(member)
        await test_db.flush()
        row = GroupQualificationAudit(batch_id=f"poison-{n}", account_id=1, group_id=group.id,
            membership_id=member.id, membership_joined_at=member.joined_at,
            policy_version=reviews.POLICY_VERSION, content_scope="text_profile",
            state="queued", next_retry_at=now)
        test_db.add(row)
        await test_db.flush()
        ids.append(row.id)
    await test_db.commit()
    monkeypatch.setattr(reviews, "policy", AsyncMock(return_value={"enabled":True,"exit_all_accounts":True}))
    monkeypatch.setattr(read_schedule, "read_wait", AsyncMock(return_value=None))
    async def assess(service, account_id, group, *, row):
        if row.id == ids[0]:
            raise ValueError("malformed legacy peer")
        row.state, row.decision, row.next_retry_at = "completed", "allowed", None
        return Obj(passed=False, reason="test_collected")
    monkeypatch.setattr(reviews, "assess", assess)
    result = await reviews.run_reviews(Obj(db=test_db), account_id=1, limit=2)
    assert result["failed"] == 1 and result["processed"] == 1
    failed = await test_db.get(GroupQualificationAudit, ids[0])
    assert failed.state == "queued" and failed.next_retry_at > now
    assert (await test_db.get(GroupQualificationAudit, ids[1])).state == "completed"
