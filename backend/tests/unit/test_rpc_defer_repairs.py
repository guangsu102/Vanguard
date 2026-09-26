import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account import rpc_governor as rpc
from app.modules.acquisition import automation as automation
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.capacity import capacity_snapshot
from app.modules.acquisition.models import AdCampaign, AdDeliveryScheduleState
from tests.unit.test_qualification_automation_lifecycle import case
from tests.unit.test_dynamic_outbound_capacity import account_config

pytestmark = pytest.mark.asyncio


def ready_usage():
    return {'usage': {}, 'blocked_windows': [], 'retry_after_seconds': 0, 'emergency_cooldown_seconds': 0}


async def test_read_only_budget_snapshot_blocks_hour_and_recovers_without_reservation(monkeypatch,test_db):
    from app.core import redis as redis_module
    now=datetime.utcnow()
    cache=Obj(eval=AsyncMock(return_value=[1,30,240,2300,498,70000,0,-2,23,70000,0,-2,0,-2]),ttl=AsyncMock(return_value=-2))
    monkeypatch.setattr(redis_module,'get_redis',AsyncMock(return_value=cache))
    state=await rpc.snapshot(test_db,3,now)
    assert state['state']=='budget_wait' and state['reason']=='telegram_read_budget'
    assert state['usage']['hour']['remaining']==0
    assert state['resume_at']==(now+timedelta(seconds=2301)).isoformat()
    assert 'INCR' not in cache.eval.await_args.args[0] and 'SET' not in cache.eval.await_args.args[0]
    cache.eval.return_value=[0,-2,0,-2,498,70000,0,-2,23,70000,0,-2,0,-2]
    assert (await rpc.snapshot(test_db,3,now))['state']=='ready'


async def test_background_exhaustion_does_not_block_foreground(monkeypatch,test_db):
    from app.core import redis as redis_module
    cache=Obj(eval=AsyncMock(return_value=[0,-2,10,10,100,100,60,3000,480,70000,0,-2,0,-2]),ttl=AsyncMock(return_value=-2))
    monkeypatch.setattr(redis_module,'get_redis',AsyncMock(return_value=cache))
    assert (await rpc.snapshot(test_db,3,datetime.utcnow()))['state']=='ready'


async def test_budget_backend_unavailable_fails_closed(monkeypatch,test_db):
    monkeypatch.setattr(rpc,'read_budget_state',AsyncMock(side_effect=ConnectionError()))
    with pytest.raises(rpc.RpcDeferred,match='telegram_rpc_guard_unavailable'):
        await rpc.check_read_ready(test_db,3)


async def test_capacity_disables_execution_but_preserves_unused_daily_allowance(monkeypatch,test_db):
    account,_=await account_config(test_db)
    monkeypatch.setattr(rpc,'read_budget_state',AsyncMock(return_value={**ready_usage(),'retry_after_seconds':2200,'blocked_windows':['hour']}))
    monkeypatch.setattr(automation.AcquisitionAutomationService,'_get_ad_delivery_cooldown_until',AsyncMock(return_value=None))
    cap=await capacity_snapshot(test_db,account.id)
    assert cap['effective']['ad']==30 and cap['quota_remaining']['ad']==30
    assert cap['executable_now']=={'join':0,'ad':0}
    assert cap['execution']['state']=='budget_wait'
    assert cap['ad_next_allowed_at'] and 'telegram_read_budget' in cap['blockers']


async def test_ad_dispatch_does_not_walk_groups_or_create_failure_rows_when_budget_exhausted(monkeypatch,test_db):
    service,account,*_=await case(test_db)
    monkeypatch.setattr(automation,'check_read_ready',AsyncMock(side_effect=rpc.RpcDeferred('telegram_read_budget',2000)))
    service._list_enabled_ad_bindings_for_account=AsyncMock()
    result=await service._run_ad_delivery_for_account(account.id,binding_ids=[],dry_run=False,delivery_budget={'remaining':1},delivery_budget_lock=asyncio.Lock(),reserved_ad_targets=set(),ad_target_lock=asyncio.Lock(),max_deliveries_per_account=1,stop_after_success=False,stop_after_failure=False)
    assert result.failed==0 and result.skipped==1
    service._list_enabled_ad_bindings_for_account.assert_not_awaited()
    service.account_pool.acquire_by_id.assert_not_awaited()


async def test_defer_uses_full_retry_after_and_unknown_send_is_not_retryable(test_db):
    service,account,group,_,log,now=await case(test_db)
    campaign=await test_db.get(AdCampaign,log.ad_campaign_id)
    row=AdDeliveryScheduleState(account_id=account.id,group_id=group.id,campaign_id=campaign.id,telegram_group_id=group.group_id,next_due_at=now,lock_token='claim',status='running')
    test_db.add(row);await test_db.commit()
    await service._finish_ad_schedule_state(row.id,'claim',campaign=campaign,succeeded=False,reason='unknown:telegram_read_budget: retry_after_seconds=2455',completed_at=now)
    assert row.next_due_at==now+timedelta(seconds=2456) and row.lock_token is None
    assert rpc.deferred_error('send_outcome_unknown:telegram_read_budget: retry_after_seconds=2455') is None


@pytest.mark.parametrize('where',['acquire','inspect'])
async def test_survival_deferral_preserves_stage_receipt_and_retry_budget(monkeypatch,test_db,where):
    service,_,_,_,log,now=await case(test_db)
    monkeypatch.setattr(automation,'_now',lambda:now)
    log.survival_retry_count=3;await test_db.commit()
    failure=rpc.RpcDeferred('telegram_read_budget',2400)
    if where=='acquire':service.account_pool.acquire_by_id.side_effect=failure
    else:service._inspect_ad_survival_facts=AsyncMock(side_effect=failure)
    assert await service._check_one_ad_survival(log,now)=='deferred'
    await test_db.refresh(log)
    assert log.status=='success' and log.telegram_message_id==19
    assert log.survival_status=='pending' and log.survival_retry_count==3
    assert log.survival_stage=='two_minute' and log.survived_two_minute_at is None
    assert log.survival_check_due_at==now+timedelta(seconds=2401) and log.survival_claim_token is None


async def test_nested_survival_read_does_not_swallow_rpc_defer(test_db):
    service,_,_,_,log,_=await case(test_db)
    client=Obj(get_entity=AsyncMock(side_effect=rpc.RpcDeferred('telegram_read_budget',100)))
    with pytest.raises(rpc.RpcDeferred):await service._inspect_ad_survival_facts(Obj(client=client),log)


async def test_listener_defers_without_connecting_or_marking_error(monkeypatch):
    from contextlib import asynccontextmanager
    from app.workers import telegram_worker as module
    from app.core.worker_status import TelegramWorkerRole
    @asynccontextmanager
    async def db():yield object()
    monkeypatch.setattr(module,'get_db_session',db)
    monkeypatch.setattr(rpc,'check_read_ready',AsyncMock(side_effect=rpc.RpcDeferred('telegram_read_budget',2000)))
    worker=module.TelegramWorker(role=TelegramWorkerRole.GROWTH_USER)
    worker._account_pool=Obj(connect_by_id=AsyncMock())
    result=await worker._ensure_growth_listeners([Obj(id=3)])
    assert result['listeners_deferred']==1 and result['listener_errors']==[]
    worker._account_pool.connect_by_id.assert_not_awaited()


@pytest.mark.parametrize('extra', [{}, {'verification_pending':True},{'temporary_until':'2027-01-01'},{'newcomer_until':'2027-01-01'}])
async def test_only_confirmed_long_term_mute_enters_exit_rule(extra):
    snapshot={'permissions':{'member':True,'can_send_text':False,'permanent_send_restriction_verified':True,**extra}}
    assert qualification.automatic_exit_reason(snapshot)==(None if extra else 'account_permanent_send_restriction')
    snapshot['protected']=True
    assert qualification.automatic_exit_reason(snapshot) is None


async def test_newcomer_verification_window_and_existing_long_term_mute():
    now=datetime.utcnow()
    for age,expected in [(1,None),(49,'account_permanent_send_restriction')]:
        snapshot={'permissions':{'member':True,'can_send_text':False,'permanent_send_restriction_verified':True}}
        qualification.annotate_newcomer_restriction(snapshot,now-timedelta(hours=age),now)
        assert qualification.automatic_exit_reason(snapshot)==expected
