"""Replay real exhausted windows; protect sends, sync recovery and consumed reads."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account import read_work, rpc_governor as rpc
from app.core.account.demand_lending import add_refresh_demand_headroom
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
from app.core.account.sync_lending import day_blocked_listener
from app.core.worker_status import TelegramWorkerStatus
from app.modules.acquisition.qualification_events import request
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401
from tests.unit.test_completion_capacity import configured


USAGE = {
    'hour': 168, 'ad_hour': 64, 'survival_hour': 1, 'join_reserved_hour': 97,
    'sync_reserved_hour': 6, 'critical_join_hour': 8, 'critical_review_hour': 89, 'listener_hour': 1,
    'day': 2713, 'ad_day': 707, 'survival_day': 265, 'join_reserved_day': 956,
    'sync_reserved_day': 741, 'flex_reserved_day': 44, 'critical_join_day': 89,
    'critical_review_day': 867, 'listener_day': 23, 'survival_lent_day': 121,
}
DEMAND = {'delivery_cost': 11, 'renewal_cost': 16,
          'hour': {'sends': 1, 'renewals': 1, 'hold': 65},
          'day': {'sends': 20, 'renewals': 20, 'hold': 648}}


def listener():
    return {'account_id': 2, 'state': 'connected_wait', 'connected': True,
            'sync_wait_reason': 'telegram_read_budget', 'sync_wait_scope': 'account',
            'checkpoint_scope': 'durable_session_and_inbox', 'update_handler_tasks': 0,
            'update_queue_size': 1, 'read_wait_seconds': 90,
            'raw_journal': {'available': True, 'states': {'checkpointed': 1000},
                            'queues': {'business_facts': {'count': 0}}, 'checkpoint': {'count': 56}}}


async def seed(store, monkeypatch):
    from app.core import redis as redis_module
    for name, value in USAGE.items():
        await store.set('vanguard:rpc:2:' + name, value, ex=400 if name.endswith('hour') else 17000)
    monkeypatch.setattr(redis_module, 'get_redis', AsyncMock(return_value=store))
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 150)
    monkeypatch.setattr(rpc, 'load_state', AsyncMock(return_value={'read_factor': .9}))
    monkeypatch.setattr('app.core.account.survival_lending.survival_holds',
                        AsyncMock(return_value={'hour': 30, 'day': 62}))
    monkeypatch.setattr('app.core.account.demand_lending.ad_demand', AsyncMock(return_value=DEMAND))


async def consume(store, limits, reads, *, kind='review', token='', reserve=False):
    keys, args = reservation_args(2, limits, 'ad' if kind in {'renewal', 'send'} else 'critical',
                                  reads, 0, workload='review', ad_refresh=kind == 'renewal',
                                  work_token=token, work_mode='reserve' if reserve else 'read',
                                  work_kind=kind)
    return await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)


@pytest.mark.asyncio
async def test_production_windows_finish_review_renewal_and_send_without_reset(test_db, queue_store, monkeypatch):
    _, _, _, member, _, config = await configured(test_db)
    config.adaptive_ads_enabled = config.join_review_backlog_paused = True
    await request(test_db, member, 'gap')
    test_db.add(TelegramWorkerStatus(worker_id='window-test', role='growth_user_worker', status='online',
        last_heartbeat_at=datetime.utcnow(), metadata_json=json.dumps({'runtime': {'listeners': [listener()]}})))
    await test_db.commit()
    await seed(queue_store, monkeypatch)
    state = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert state['state'] == 'recovering'
    assert state['sync_lending']['reason'] == 'sync_day_exhausted'
    assert state['limits']['sync_hold_hour'] == 32 and state['limits']['sync_hold_day'] == -1
    assert state['critical_workloads']['review']['remaining'] == 26
    assert state['ad_dependency_recovery']['delivery_holds'] == {'hour': 33, 'day': 264}
    assert state['lanes']['ad_refresh']['remaining'] == 17
    for kind, reserved, spent in [('review', 24, 24), ('renewal', 16, 9)]:
        state = await rpc.snapshot(test_db, 2, datetime.utcnow())
        assert await consume(queue_store, state['limits'], reserved, kind=kind, token=kind, reserve=True) == 0
        assert await consume(queue_store, state['limits'], spent, kind=kind, token=kind) == 0
        await queue_store.delete(read_work.key(2))
    assert await consume(queue_store, state['limits'], 11, kind='send') == 0
    assert int(await queue_store.get('vanguard:rpc:2:day')) == USAGE['day'] + 24 + 9 + 11
    assert int(await queue_store.get('vanguard:rpc:2:hour')) == USAGE['hour'] + 24 + 9 + 11
    assert await queue_store.get('vanguard:rpc:2:sync_lent_day') is None
    assert int(await queue_store.get('vanguard:rpc:2:sync_lent_hour')) <= 26
    assert 16990 <= await queue_store.ttl('vanguard:rpc:2:day') <= 17000
    assert 390 <= await queue_store.ttl('vanguard:rpc:2:hour') <= 400


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['cooldown', 'disconnected', 'stale', 'pressure', 'pending',
                                 'storage_error', 'day_room', 'day_before_hour', 'no_day_expiry'])
async def test_sync_bridge_requires_durable_listener_and_exhausted_later_day(test_db, queue_store, monkeypatch, bad):
    await seed(queue_store, monkeypatch)
    limits = rpc.limits_for({'read_factor': .9}, datetime.utcnow())
    budget = await rpc.read_budget_state(2, limits, datetime.utcnow())
    item = listener()
    if bad == 'cooldown':
        item['sync_wait_reason'] = 'telegram_rpc_cooldown'
    elif bad == 'disconnected':
        item['connected'] = False
    elif bad == 'pressure':
        item['raw_journal']['pressure'] = True
    elif bad == 'pending':
        item['raw_journal']['queues']['business_facts']['count'] = 1
    elif bad == 'storage_error':
        item['raw_journal']['storage_error'] = 'disk_full'
    elif bad == 'day_room':
        budget['usage']['sync_day']['used'] = 0
    elif bad == 'day_before_hour':
        budget['usage']['day']['ttl_seconds'] = 100
    elif bad == 'no_day_expiry':
        budget['usage']['day']['ttl_seconds'] = -1
    if bad == 'stale':
        from app.core.account.sync_lending import add_idle_sync_headroom
        test_db.add(TelegramWorkerStatus(worker_id='stale-window', role='growth_user_worker', status='online',
            last_heartbeat_at=datetime.utcnow() - timedelta(minutes=3),
            metadata_json=json.dumps({'runtime': {'listeners': [item]}})))
        await test_db.commit()
        await add_idle_sync_headroom(test_db, 2, datetime.utcnow(), limits, budget)
        assert 'sync_lending' not in budget
    else:
        assert not day_blocked_listener(item, limits, budget)


@pytest.mark.asyncio
async def test_new_hour_borrowing_requires_a_later_exhausted_day(queue_store, monkeypatch):
    await seed(queue_store, monkeypatch)
    limits = rpc.limits_for({'read_factor': .9}, datetime.utcnow())
    budget = await rpc.read_budget_state(2, limits, datetime.utcnow())
    budget['usage']['hour']['ttl_seconds'] = -2
    assert day_blocked_listener(listener(), limits, budget)
    budget['usage']['day']['ttl_seconds'] = 3500
    assert not day_blocked_listener(listener(), limits, budget)


@pytest.mark.asyncio
async def test_renewal_guard_remains_when_no_repairs_or_forecast_unavailable(queue_store, monkeypatch):
    await seed(queue_store, monkeypatch)
    limits = rpc.limits_for({'read_factor': .9}, datetime.utcnow())
    budget = await rpc.read_budget_state(2, limits, datetime.utcnow())
    before = dict(budget['lanes']['ad_refresh'])
    no_repairs = {**DEMAND, **{w: {**DEMAND[w], 'renewals': 0} for w in ('hour', 'day')}}
    add_refresh_demand_headroom(limits, budget, no_repairs)
    assert budget['lanes']['ad_refresh'] == before
    from app.core.account.demand_lending import add_ad_demand_headroom
    budget['ad_dependency_recovery'] = {'blocked_grants': 20}
    monkeypatch.setattr('app.core.account.demand_lending.ad_demand', AsyncMock(side_effect=RuntimeError))
    await add_ad_demand_headroom(None, 2, datetime.utcnow(), limits, budget)
    assert budget['lanes']['ad_refresh'] == before
    assert budget['ad_demand_lending']['reason'] == 'forecast_unavailable'


@pytest.mark.asyncio
async def test_available_grant_keeps_send_capacity_without_stopping_other_renewals(test_db, queue_store, monkeypatch):
    _, _, _, _, _, config = await configured(test_db)
    config.adaptive_ads_enabled = True
    await test_db.commit()
    # A current, ready grant causes the old all-blocked shortcut to return.
    # The complete forecast still proves that other scheduled grants need repair.
    await seed(queue_store, monkeypatch)
    state = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert not state['ad_dependency_recovery'].get('blocked_grants')
    assert state['ad_dependency_recovery']['forecast_renewals'] == 20
    assert state['lanes']['ad_refresh']['remaining'] == 17
    assert await consume(queue_store, state['limits'], 16, kind='renewal', token='renew', reserve=True) == 0
    assert await consume(queue_store, state['limits'], 9, kind='renewal', token='renew') == 0
    await queue_store.delete(read_work.key(2))
    assert await consume(queue_store, state['limits'], 33, kind='send') == 0
    assert int(await queue_store.get('vanguard:rpc:2:day')) == USAGE['day'] + 9 + 33


@pytest.mark.parametrize('funded', [True, False])
def test_complete_forecast_preserves_positive_send_slots_after_old_buffer_spent(funded):
    from app.modules.acquisition.ad_pacing import pacing_deadline
    now = datetime.utcnow()
    state = {'limits': {'ad_day': 1134}, 'usage': {'day': {'ttl_seconds': 7200},
             'ad_day': {'used': 950}, 'ad_lent_day': {'used': 0}},
             'ad_demand_lending': {'forecast': {'day': {'hold': 150 if funded else 300}}}}
    due = pacing_deadline(state, capacity=60, delivery_cost=11, last_sent=now, now=now)
    assert due == now + timedelta(seconds=1440)
    # Missing forecast retains the conservative fallback, never invents headroom.
    state.pop('ad_demand_lending')
    assert pacing_deadline(state, capacity=60, delivery_cost=11, last_sent=now, now=now) == now + timedelta(seconds=7201)
