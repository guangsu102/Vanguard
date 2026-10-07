"""Demand allocation and renewal slices against isolated SQL and real Redis."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account import read_work, rpc_governor as rpc
from app.core.account.models import AccountOperationConfig
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, allocations, reservation_args
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.qualification_events import acknowledge, pending, request
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_qualification_light_renewal import fixture


async def configured(db):
    from app.core.automation_settings import AUTO_JOIN_SCHEDULER_SETTING_KEY
    from app.core.settings_models import SystemSetting

    actor, account, group, member, row, previous = await fixture(db)
    config = AccountOperationConfig(account_id=account.id, enabled=True,
        auto_join_enabled=True, auto_ads_enabled=True, dynamic_capacity_enabled=True)
    db.add_all([config, SystemSetting(key=AUTO_JOIN_SCHEDULER_SETTING_KEY, value='{"enabled":true}')])
    await db.commit()
    return actor, account, group, member, row, config


async def add_join_candidate(db, account_id, *, ready=True):
    from app.core.group.models import Group
    from app.modules.acquisition import candidate_inventory
    from app.modules.acquisition.candidate_preview import CandidatePreview

    group = Group(
        group_id=-1000000999999,
        username="work-demand-candidate",
        status="pending_join",
    )
    db.add(group)
    await db.flush()
    if ready:
        now = datetime.utcnow()
        await candidate_inventory.save(
            db,
            account_id,
            group.id,
            candidate_inventory.record(
                group,
                account_id,
                CandidatePreview(
                    "sampled",
                    peer_namespace="channel",
                    member_count=100,
                    rule_signal="explicit_allow",
                ),
                now,
            ),
        )
    return group


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['backlog', 'initial', 'none'])
async def test_idle_join_floor_is_lent_with_and_without_backlog_flag(test_db, monkeypatch, blocked):
    _, _, _, member, row, config = await configured(test_db)
    if blocked == 'backlog':
        config.join_review_backlog_paused = True
    if blocked == 'initial':
        member.review_status, member.ad_status = 'initial_pending', 'blocked'
        row.batch_id, row.decision = 'join-attempt:unfinished', 'technical_wait'
    await test_db.commit()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert state['state'] == 'ready'
    assert state['limits']['join_idle_hold_day'] == 0
    reason = 'join_wait_pending_review' if blocked != 'none' else 'no_executable_join_candidate'
    assert state['join_idle_lending']['reason'] == reason


@pytest.mark.asyncio
async def test_join_hold_requires_an_executable_candidate(test_db, monkeypatch):
    _, account, _, _, _, config = await configured(test_db)
    await add_join_candidate(test_db, account.id, ready=True)
    await test_db.commit()
    mock_usage(monkeypatch, {})

    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())

    demand = state['join_idle_lending']['candidate_inventory']
    assert demand['ready'] == 1 and demand['executable'] is True
    assert state['limits']['join_idle_hold_hour'] == 12
    assert state['limits']['join_idle_hold_day'] == 12


@pytest.mark.asyncio
async def test_due_preview_with_funded_lifecycle_keeps_a_small_join_hold(test_db, monkeypatch):
    _, account, _, _, _, config = await configured(test_db)
    await add_join_candidate(test_db, account.id, ready=False)
    await test_db.commit()
    mock_usage(monkeypatch, {})

    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())

    demand = state['join_idle_lending']['candidate_inventory']
    assert demand['preview_pending'] == 1 and demand['ready'] == 0
    assert state['limits']['join_idle_hold_hour'] == 12
    assert state['limits']['join_idle_hold_day'] == 12
    assert state['join_idle_lending']['reason'] == 'candidate_preview_due'


@pytest.mark.asyncio
async def test_qualification_gate_rejection_does_not_hold_join_budget(test_db, monkeypatch):
    _, account, _, _, _, config = await configured(test_db)
    await add_join_candidate(test_db, account.id, ready=True)
    await test_db.commit()
    monkeypatch.setattr(
        "app.modules.acquisition.qualification_join_gate.qualification_join_gate",
        AsyncMock(return_value="frequency_rejoin_blocked"),
    )
    mock_usage(monkeypatch, {})

    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())

    demand = state['join_idle_lending']['candidate_inventory']
    assert demand['excluded'] == 1 and demand['ready'] == 0
    assert state['limits']['join_idle_hold_day'] == 0


@pytest.mark.asyncio
async def test_retry_wait_does_not_release_existing_ready_join_demand(test_db, monkeypatch):
    _, account, _, _, _, config = await configured(test_db)
    await add_join_candidate(test_db, account.id, ready=True)
    from app.modules.acquisition import candidate_inventory

    await candidate_inventory.defer_account(
        test_db, account.id, datetime.utcnow() + timedelta(minutes=10)
    )
    await test_db.commit()
    mock_usage(monkeypatch, {})

    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())

    demand = state['join_idle_lending']['candidate_inventory']
    assert demand['retry_wait'] is True and demand['ready'] == 1
    assert state['limits']['join_idle_hold_day'] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reservation",
    [
        {"kind": "join", "remaining": 1},
        {"kind": "review", "pending": True, "remaining": 1},
    ],
)
async def test_active_lifecycle_reservation_keeps_join_hold_without_candidates(
    test_db, reservation
):
    from app.core.account.join_demand_lending import add_join_demand_headroom

    _, account, _, _, _, config = await configured(test_db)
    config.auto_ads_enabled = False
    await test_db.commit()
    limits = {}
    budget = {"work_reservation": reservation}

    await add_join_demand_headroom(test_db, account.id, datetime.utcnow(), limits, budget)

    assert limits["join_idle_hold_hour"] == 12
    assert limits["join_idle_hold_day"] == 12
    assert budget["join_idle_lending"]["reason"] == "active_join_or_review_reservation"


@pytest.mark.asyncio
async def test_ad_dependency_capacity_stops_after_one_current_grant_is_repaired(test_db, monkeypatch):
    _, _, _, member, _, _ = await configured(test_db)
    mock_usage(monkeypatch, {'hour': (50, 1800), 'ad_hour': (50, 1800)})
    before = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert before['lanes']['ad_refresh']['remaining'] == 0
    await request(test_db, member, 'gap')
    await test_db.flush()
    blocked = await rpc.check_read_ready(test_db, 2, purpose='ad_qualification_refresh', minimum_reads=16)
    assert blocked['ad_dependency_recovery']['blocked_grants'] == 1
    assert blocked['lanes']['ad_refresh']['remaining'] == 17
    assert blocked['lanes']['ad']['remaining'] == before['lanes']['ad']['remaining']
    assert await acknowledge(test_db, member, await pending(test_db, member))
    after = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert 'ad_dependency_recovery' not in after
    assert after['lanes']['ad_refresh']['remaining'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['expired', 'paused', 'disabled', 'old_epoch'])
async def test_invalid_or_paused_grants_cannot_request_extra_ad_reads(test_db, monkeypatch, invalid):
    _, _, _, member, row, config = await configured(test_db)
    if invalid == 'expired':
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif invalid == 'paused':
        member.ad_status = 'paused'
    elif invalid == 'disabled':
        config.auto_ads_enabled = False
    else:
        row.membership_joined_at -= timedelta(days=1)
    await request(test_db, member, 'gap')
    await test_db.flush()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert state['state'] == 'ready' and 'ad_dependency_recovery' not in state


@pytest.mark.asyncio
async def test_future_delivery_does_not_hide_due_qualification_repair(test_db, monkeypatch):
    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition.models import AdDeliveryScheduleState, GroupQualificationAudit

    _, _, _, member, row, _ = await configured(test_db)
    # The available grant's next delivery is tomorrow; today's due grant still
    # needs repair. It must not be stranded behind an unusable planning cushion.
    test_db.add(AdDeliveryScheduleState(account_id=2, campaign_id=1, group_id=40, telegram_group_id=1234567890,
        next_due_at=datetime.utcnow() + timedelta(days=1), status='idle'))
    other_group = Group(id=41, group_id=1234567891, title='due')
    other_member = GroupAccountMembership(id=61, group_id=41, telegram_group_id=1234567891,
        account_id=2, joined_at=member.joined_at, status='joined', review_status='approved', ad_status='active')
    other_row = GroupQualificationAudit(batch_id='due', membership_id=61, account_id=2,
        group_id=41, state='completed', decision=row.decision, policy_version=row.policy_version,
        content_scope=row.content_scope, membership_joined_at=member.joined_at,
        checked_at=row.checked_at, expires_at=row.expires_at, evidence_json=row.evidence_json)
    test_db.add_all([other_group, other_member, other_row])
    test_db.add(AdDeliveryScheduleState(account_id=2, campaign_id=1, group_id=41,
        telegram_group_id=1234567891, next_due_at=datetime.utcnow() + timedelta(minutes=10),
        status='retry', last_reason='qualification_event_pending'))
    await test_db.flush()
    await request(test_db, other_member, 'gap')
    await test_db.flush()
    mock_usage(monkeypatch, {'hour': (50, 1800), 'ad_hour': (50, 1800)})
    state = await rpc.check_read_ready(test_db, 2, purpose='ad_qualification_refresh', minimum_reads=16)
    assert state['ad_dependency_recovery']['blocked_grants'] == 1


async def allocate(store, limits, cost, *, lane='critical', token='', mode='read', renewal=False):
    keys, args = reservation_args(2, limits, lane, cost, 0, workload='review',
        work_token=token, work_mode=mode, work_kind='renewal' if renewal else 'review',
        work_group=40, work_scope='member:epoch', ad_refresh=renewal)
    return await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)


@pytest.mark.asyncio
async def test_idle_join_floor_can_finish_review_without_resetting_day_or_stealing_ads(queue_store):
    limits = rpc.limits_for({}, datetime.utcnow())
    # Daily critical room is 44; the unspent join floor is 150. Nothing can
    # start under the fixed floor, although total capacity is still available.
    for name, value in {'day': 676, 'join_reserved_day': 676,
                        'critical_review_day': 676}.items():
        await queue_store.set('vanguard:rpc:2:' + name, value, ex=60000)
    assert await allocate(queue_store, limits, 24, token='review', mode='reserve') > 0
    limits.update(join_idle_hold_hour=12, join_idle_hold_day=12)
    assert await allocate(queue_store, limits, 24, token='review', mode='reserve') == 0
    assert await queue_store.get('vanguard:rpc:2:day') == '676'
    assert await allocate(queue_store, limits, 24, token='review') == 0
    assert await allocate(queue_store, limits, 24, lane='ad') == 0
    assert await queue_store.get('vanguard:rpc:2:day') == '724'
    assert 59995 <= await queue_store.ttl('vanguard:rpc:2:day') <= 60000


@pytest.mark.asyncio
async def test_renewal_atomic_reservation_and_hold_survive_demand_change(queue_store):
    limits = rpc.limits_for({}, datetime.utcnow())
    for window in ('hour', 'day'):
        caps = allocations(limits[window])
        limits['ad_refresh_hold_' + window] = caps[4] + (caps[0] + 4) // 5
    await queue_store.set('vanguard:rpc:2:hour', 50, ex=1800)
    await queue_store.set('vanguard:rpc:2:ad_hour', 50, ex=1800)
    claims = await asyncio.gather(*(allocate(queue_store, limits, 16,
        lane='ad', token=str(i), mode='reserve', renewal=True) for i in range(20)))
    assert claims.count(0) == 1
    owner = str(claims.index(0))
    assert await queue_store.get('vanguard:rpc:2:hour') == '50'
    # Other ad consumers cannot spend the 16 reads already reserved.
    assert await allocate(queue_store, limits, 31, lane='ad') > 0
    assert await allocate(queue_store, limits, 12, lane='ad') == 0
    ordinary_limits = rpc.limits_for({}, datetime.utcnow())
    # The slice passed its admission hold already. Actual sending can consume
    # its own cushion without invalidating the still-protected renewal slice.
    assert await allocate(queue_store, ordinary_limits, 16, lane='ad', token=owner, renewal=True) == 0
    assert await allocate(queue_store, ordinary_limits, 1, lane='ad', token=owner, renewal=True) > 0
    assert await queue_store.get('vanguard:rpc:2:hour') == '78'
    assert 1795 <= await queue_store.ttl('vanguard:rpc:2:hour') <= 1800


@pytest.mark.asyncio
@pytest.mark.parametrize('percent', [100, 150])
async def test_thirty_paused_accounts_can_finish_old_reviews_and_send_independently(queue_store, monkeypatch, percent):
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', percent)
    limits = rpc.limits_for({}, datetime.utcnow())
    caps = allocations(limits['day'])
    prior = caps[2] + caps[4] - 44
    for account_id in range(1, 31):
        for name in ('day', 'join_reserved_day', 'critical_review_day'):
            await queue_store.set(f'vanguard:rpc:{account_id}:{name}', prior, ex=60000)
        for released in (False, True):
            demand = {**limits, **({'join_idle_hold_hour': 12, 'join_idle_hold_day': 12} if released else {})}
            keys, args = reservation_args(account_id, demand, 'critical', 24, 0,
                workload='review', work_mode='reserve', work_token='review', work_kind='review')
            delay = await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)
            assert (delay == 0) is released
        for lane, cost, token in [('ad', 12, ''), ('critical', 24, 'review')]:
            keys, args = reservation_args(account_id, demand, lane, cost, 0,
                workload='review', work_token=token)
            assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
        assert int(await queue_store.get(f'vanguard:rpc:{account_id}:day')) == prior + 36
        assert 59990 <= await queue_store.ttl(f'vanguard:rpc:{account_id}:day') <= 60000


@pytest.mark.asyncio
async def test_live_overcommitted_window_matches_preview_and_completes_reserved_reads(queue_store):
    # Read-only production capture at 16:20:25, replayed in disposable Redis.
    # One previously clipped ad claim must be restored as well as the slice
    # and maintenance floor. A single subtraction underestimated this by one.
    limits = {'minute': 162, 'hour': 324, 'day': 3240,
        'survival_hold_hour': 37, 'survival_hold_day': 182,
        'join_idle_hold_hour': 12, 'join_idle_hold_day': 12}
    usage = {'day': 2447, 'ad_day': 563, 'survival_day': 263,
        'join_reserved_day': 842, 'sync_reserved_day': 735, 'flex_reserved_day': 44,
        'critical_join_day': 81, 'critical_review_day': 761, 'listener_day': 21}
    for name, value in usage.items():
        await queue_store.set('vanguard:rpc:2:' + name, value, ex=22000)
    assert await allocate(queue_store, limits, 24, token='review', mode='reserve') == 0
    assert await queue_store.hget(read_work.key(2), 'survival_2') == '37'
    assert await queue_store.get('vanguard:rpc:2:day') == '2447'
    for _ in range(24):
        assert await allocate(queue_store, limits, 1, token='review') == 0
    assert await queue_store.get('vanguard:rpc:2:day') == '2471'
    assert await queue_store.get('vanguard:rpc:2:survival_lent_day') == '25'
    assert await allocate(queue_store, limits, 12, lane='ad') == 0
    assert 21995 <= await queue_store.ttl('vanguard:rpc:2:day') <= 22000


@pytest.mark.asyncio
async def test_real_governor_caps_renewal_and_cancellation_keeps_spent_reads(queue_store, monkeypatch):
    monkeypatch.setattr('app.core.redis.get_redis', AsyncMock(return_value=queue_store))

    @asynccontextmanager
    async def db():
        yield object()

    monkeypatch.setattr('app.core.database.get_db_session', db)
    monkeypatch.setattr(rpc, 'load_state', AsyncMock(return_value={}))
    monkeypatch.setattr(rpc, 'snapshot', AsyncMock(return_value={
        'state': 'ready', 'limits': rpc.limits_for({}, datetime.utcnow())}))
    with pytest.raises(asyncio.CancelledError):
        async with read_work.operation(2, 40, 'renewal') as work:
            governor = rpc.RpcGovernor(2, lambda: 'ad_qualification_refresh')
            await governor.before(['channels.GetParticipantRequest'] * 16)
            assert work.limit == work.reads == 16
            with pytest.raises(rpc.RpcDeferred, match='telegram_read_slice'):
                await governor.before(['channels.GetParticipantRequest'])
            raise asyncio.CancelledError
    assert not await queue_store.exists(read_work.key(2))
    assert await queue_store.get('vanguard:rpc:2:hour') == '16'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,process', [('telegram_read_budget', True), ('telegram_rpc_cooldown', False)])
async def test_future_local_renewal_budget_wait_is_reconsidered_but_platform_wait_is_not(test_db, monkeypatch, reason, process):
    actor, _, _, member, row, _ = await configured(test_db)
    await request(test_db, member, 'gap')
    row.next_retry_at = datetime.utcnow() + timedelta(hours=3)
    saved = json.loads(row.evidence_json)
    saved['renewal_wait_reason'] = reason
    row.evidence_json = json.dumps(saved)
    await test_db.commit()
    monkeypatch.setattr(service, 'ensure_membership_reviews', AsyncMock())
    monkeypatch.setattr(rpc, 'check_read_ready', AsyncMock(return_value={}))
    monkeypatch.setattr('app.core.account.read_schedule.read_wait', AsyncMock(return_value=None))

    async def assess(actor, account_id, group, *, row):
        row.state, row.next_retry_at = 'completed', None
        return Obj(passed=False, reason='repaired')

    assess_mock = AsyncMock(side_effect=assess)
    monkeypatch.setattr(service, 'assess', assess_mock)
    await service.run_reviews(actor, account_id=2, limit=1)
    assert bool(assess_mock.await_count) is process
