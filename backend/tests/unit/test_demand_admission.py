"""Actual Lua spending, demand forecasts, and admission under exhausted shares."""

import asyncio
import json
import random
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account import read_work, rpc_governor as rpc
from app.core.account.demand_lending import ad_demand
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
from app.modules.acquisition.growth_admission import admission_plan
from app.modules.acquisition.qualification_events import request
from app.modules.acquisition.review_cost import review_shape
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_completion_capacity import configured


async def allocate(store, account_id, limits, cost, *, lane='critical', token='', reserve=False):
    keys, args = reservation_args(account_id, limits, lane, cost, 0, workload='review',
                                  work_token=token, work_mode='reserve' if reserve else 'read', work_kind='review')
    return await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)


@pytest.mark.asyncio
async def test_thirty_accounts_drain_old_work_keep_sends_and_never_reset_windows(queue_store):
    limits = {'minute': 162, 'hour': 324, 'day': 3240,
              'survival_hold_hour': 24, 'survival_hold_day': 63,
              'join_idle_hold_hour': 12, 'join_idle_hold_day': 12,
              'ad_demand_hold_hour': 40, 'ad_demand_hold_day': 300}
    usage = {'day': 2545, 'ad_day': 643, 'survival_day': 264,
             'join_reserved_day': 859, 'sync_reserved_day': 735, 'flex_reserved_day': 44,
             'critical_join_day': 81, 'critical_review_day': 778, 'listener_day': 22, 'survival_lent_day': 18}
    for account_id in range(1, 31):
        for name, value in usage.items():
            await queue_store.set(f'vanguard:rpc:{account_id}:{name}', value, ex=21410)
        for index in range(9):
            token = str(index)
            assert await allocate(queue_store, account_id, limits, 24, token=token, reserve=True) == 0
            before = await queue_store.get(f'vanguard:rpc:{account_id}:day')
            assert await allocate(queue_store, account_id, limits, 17, token=token) == 0
            assert int(await queue_store.get(f'vanguard:rpc:{account_id}:day')) == int(before) + 17
            await queue_store.delete(read_work.key(account_id))
        assert await allocate(queue_store, account_id, limits, 8, lane='ad') == 0
        assert int(await queue_store.get(f'vanguard:rpc:{account_id}:day')) == 2545 + 153 + 8
        assert int(await queue_store.get(f'vanguard:rpc:{account_id}:ad_lent_day') or '0') > 0
        assert 21390 <= await queue_store.ttl(f'vanguard:rpc:{account_id}:day') <= 21410


@pytest.mark.asyncio
async def test_ad_loan_reservation_survives_changed_forecast_and_cancel_keeps_spend(queue_store):
    limits = rpc.limits_for({}, datetime.utcnow())
    # No critical, survival or sync room: only proven ad headroom is lent.
    for name, value in {'hour': 156, 'join_reserved_hour': 72, 'survival_hour': 36,
                         'sync_reserved_hour': 48, 'critical_review_hour': 72}.items():
        await queue_store.set('vanguard:rpc:2:' + name, value, ex=1800)
    limits.update(ad_demand_hold_hour=40, ad_demand_hold_day=40, join_idle_hold_hour=0)
    claims = await asyncio.gather(*(allocate(queue_store, 2, limits, 24, token=str(i), reserve=True) for i in range(10)))
    assert claims.count(0) == 1
    token = str(claims.index(0))
    assert await queue_store.hget(read_work.key(2), 'ad_1') == '24'
    original = rpc.limits_for({}, datetime.utcnow())
    assert await allocate(queue_store, 2, original, 60, lane='ad') == 0
    assert await allocate(queue_store, 2, original, 17, token=token) == 0
    await queue_store.delete(read_work.key(2))
    assert await queue_store.get('vanguard:rpc:2:ad_lent_hour') == '17'
    assert await queue_store.get('vanguard:rpc:2:hour') == '233'
    assert await allocate(queue_store, 2, original, 8, lane='ad') > 0


@pytest.mark.asyncio
async def test_forecast_includes_future_sends_and_event_repairs_and_fails_closed(test_db, monkeypatch):
    from app.modules.acquisition.models import AdDeliveryScheduleState
    _, _, _, member, row, config = await configured(test_db)
    config.adaptive_ads_enabled = True
    now = datetime.utcnow()
    schedule = AdDeliveryScheduleState(account_id=2, campaign_id=1, group_id=member.group_id,
        telegram_group_id=member.telegram_group_id, next_due_at=now + timedelta(hours=2), status='idle')
    test_db.add(schedule)
    await test_db.commit()
    monkeypatch.setattr('app.modules.acquisition.read_costs.operation_costs', AsyncMock(return_value={'delivery_read_cost': 12}))
    budget = {'usage': {'hour': {'ttl_seconds': 3600}, 'day': {'ttl_seconds': 21600}}}
    before = await ad_demand(test_db, 2, now, budget)
    assert before['hour']['sends'] == 0 and before['day']['sends'] >= 1
    await request(test_db, member, 'gap')
    await test_db.flush()
    changed = await ad_demand(test_db, 2, now, budget)
    assert changed['day']['renewals'] == 1 and changed['day']['hold'] > before['day']['hold']
    schedule.status, schedule.lease_expires_at = 'sending', now + timedelta(seconds=45)
    await test_db.flush()
    assert await ad_demand(test_db, 2, now, budget) == {}


@pytest.mark.asyncio
async def test_join_forecast_pays_for_old_reviews_and_followup_before_new_group(test_db, monkeypatch):
    _, _, _, member, row, _ = await configured(test_db)
    mock_usage(monkeypatch, {'day': (640, 60000), 'join_reserved_day': (640, 60000),
                            'critical_review_day': (640, 60000)})
    now = datetime.utcnow()
    before = await admission_plan(test_db, 2, now)
    assert before['available_reads'] == 80 and before['remaining'] == 1
    row.decision, row.reason, member.review_status = 'technical_wait', 'telegram_read_budget', 'review_2h'
    row.next_retry_at = now + timedelta(hours=6)
    await test_db.flush()
    after = await admission_plan(test_db, 2, now)
    assert after['review_reads_due'] == 24 and after['new_group_read_cost'] == 60
    assert after['remaining'] == 0 and after['reason'] == 'join_wait_inventory_review'


@pytest.mark.asyncio
async def test_final_join_check_does_not_require_already_spent_bootstrap_again(test_db, monkeypatch):
    await configured(test_db)
    mock_usage(monkeypatch, {'day': (672, 60000), 'join_reserved_day': (672, 60000),
                            'critical_review_day': (660, 60000), 'critical_join_day': (12, 60000)})
    assert (await admission_plan(test_db, 2, datetime.utcnow()))['remaining'] == 0
    async with read_work.operation(2, 99, 'join') as work:
        work.claimed, work.limit, work.reads = True, 12, 12
        plan = await admission_plan(test_db, 2, datetime.utcnow())
        assert plan['remaining_read_cost'] == 48 and plan['remaining'] == 1
        work.claimed = False


@pytest.mark.asyncio
async def test_small_review_shape_requires_current_scope_and_real_completed_costs(test_db):
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    _, account, _, member, row, _ = await configured(test_db)
    now = datetime.utcnow()
    previous = json.loads(row.evidence_json)
    previous.update(account_id=account.id, profile_fingerprint=profile_fingerprint(account),
                    collected_at=now.isoformat(), collection_event=None,
                    collection_progress={'history_cursors': [{'complete': True}], 'pending_message_ids': []})
    assert review_shape(previous, row, member, account, None, now) == ('continuation', 16)
    assert review_shape(previous, row, member, account, None, now + timedelta(hours=6)) == ('continuation', 16)
    assert review_shape(previous, row, member, account, None, now + timedelta(hours=25)) == ('full', 24)
    assert review_shape(previous, row, member, account, None, now, force_refresh=True) == ('full', 24)
    previous['profile_fingerprint'] = 'changed'
    assert review_shape(previous, row, member, account, None, now) == ('full', 24)
    samples = [json.dumps({'at': 1000, 'reads': 17, 'outcome': 'observe', 'phase': 'full'})] * 20
    samples += [json.dumps({'at': 1000, 'reads': 3, 'outcome': 'technical_wait'})] * 100
    assert read_work.estimate(samples, 24, 1000, minimum=16, phase='full') == 19
    assert read_work.estimate(samples, 24, 1000, minimum=16, phase='continuation') == 24


def test_advertising_pacing_accounts_for_spent_loans():
    from app.modules.acquisition.ad_pacing import pacing_deadline
    now = datetime.utcnow()
    rpc_state = {'limits': {'ad_day': 100}, 'usage': {'day': {'ttl_seconds': 3600}, 'ad_day': {'used': 20}}}
    before = pacing_deadline(rpc_state, capacity=1000, delivery_cost=10, last_sent=now, now=now)
    rpc_state['usage']['ad_lent_day'] = {'used': 40}
    after = pacing_deadline(rpc_state, capacity=1000, delivery_cost=10, last_sent=now, now=now)
    assert after > before


def test_funded_forecast_does_not_freeze_ads_by_reapplying_buffer_to_spent_reads():
    from app.modules.acquisition.ad_pacing import pacing_deadline
    now = datetime.utcnow()
    state = {'limits': {'ad_day': 1134}, 'usage': {'day': {'ttl_seconds': 21600},
             'ad_day': {'used': 680}, 'ad_lent_day': {'used': 350}},
             'ad_demand_lending': {'forecast': {'day': {'hold': 100, 'sends': 3}}}}
    due = pacing_deadline(state, capacity=90, delivery_cost=11, last_sent=now, now=now)
    assert due == now + timedelta(seconds=960)
    # Missing forecasts are conservative, but positive real headroom must not
    # turn into zero solely because usage predates the loan.
    state.pop('ad_demand_lending')
    due = pacing_deadline(state, capacity=90, delivery_cost=11, last_sent=now, now=now)
    assert now < due < now + timedelta(seconds=21600)


@pytest.mark.asyncio
async def test_forecast_and_atomic_admission_agree_under_clipped_claims(queue_store):
    from app.core.account.reserved_reads import allocations, available, lending_caps, transferred_caps
    rng = random.Random(812)
    for account_id in range(101, 181):
        used = [rng.randint(400, 950), rng.randint(200, 350), rng.randint(720, 1200),
                rng.randint(580, 850), rng.randint(0, 120)]
        limits = {'minute': 200, 'hour': 1000, 'day': 3240,
                  'survival_hold_day': rng.randint(60, 160), 'sync_hold_day': 40,
                  'ad_demand_hold_day': rng.randint(120, 450), 'join_idle_hold_day': 12}
        lent, ad_lent = rng.randint(0, 60), rng.randint(0, 60)
        counters = dict(zip(('ad_day', 'survival_day', 'join_reserved_day', 'sync_reserved_day', 'flex_reserved_day'), used))
        counters.update(day=sum(used), survival_lent_day=lent, ad_lent_day=ad_lent,
                        critical_review_day=used[2])
        for name, value in counters.items():
            await queue_store.set(f'vanguard:rpc:{account_id}:{name}', value, ex=9000)
        caps = transferred_caps(allocations(3240), lent, 0, ad_lent)
        caps = lending_caps(caps, used, 'critical', limits['survival_hold_day'], 40, limits['ad_demand_hold_day'])
        expected = available(3240, used, caps, 'critical') - 12 >= 24
        admitted = await allocate(queue_store, account_id, limits, 24, token='scope', reserve=True)
        assert (admitted == 0) == expected, (used, limits, caps)
        assert int(await queue_store.get(f'vanguard:rpc:{account_id}:day')) == sum(used)


@pytest.mark.asyncio
async def test_near_expiry_still_protects_unspent_listener_allowance(test_db, monkeypatch):
    from app.core.account.survival_lending import survival_holds
    from tests.unit.test_event_cycle_survival import scenario
    from tests.unit.test_adaptive_group_frequency import NOW
    account, _, _, state, _, _ = await scenario(test_db)
    state.daily_review_due_at = NOW + timedelta(days=2)
    await test_db.commit()
    monkeypatch.setattr('app.modules.acquisition.read_costs.operation_costs', AsyncMock(return_value={'daily_review_read_cost': 10}))
    limits = rpc.limits_for({}, NOW)
    budget = {'usage': {w: {'ttl_seconds': 30} for w in ('hour', 'day')}}
    holds = await survival_holds(test_db, account.id, NOW, limits, budget)
    assert holds == {'hour': 10, 'day': limits['listener_day']}
    budget['usage']['day']['ttl_seconds'] = -1
    assert await survival_holds(test_db, account.id, NOW, limits, budget) == {}
