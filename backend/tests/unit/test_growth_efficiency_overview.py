"""Actual SQL and HTTP projection checks against the derived frozen backend."""
from contextlib import contextmanager
from datetime import datetime,timedelta
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport,AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import event,func,select,text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker

from app.api import group_qualification as api
from app.core.account.models import AccountOperationConfig,AccountStatus,AccountType,TelegramAccount
from app.core.database import Base,get_db
from app.core.group.models import Group,GroupAccountMembership
from app.core.security import require_admin
from app.core.settings_models import SystemSetting
from app.modules.acquisition import growth_efficiency_overview as helper
from app.modules.acquisition.models import AutoJoinAttempt,QualificationWork

NOW=datetime(2026,11,6,1)

@pytest_asyncio.fixture
async def sql_db():
    for name in ('core.account.control_models','core.keyword.models','core.user.models','core.campaign.models',
        'core.worker_status','modules.guardian.models','modules.private_chat.models','modules.qq.models',
        'modules.owned_group.models','modules.owned_group.models_extra','modules.owned_group.messaging_models',
        'integrations.xboard.models'):
        importlib.import_module('app.'+name)
    engine=create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session=async_sessionmaker(engine,expire_on_commit=False)
    async with session() as db:
        yield db,engine
    await engine.dispose()


def account(identity):
    return TelegramAccount(id=identity,identifier=f'overview-{identity}',session_name=f'overview-{identity}',
        is_active=True,status=AccountStatus.ONLINE,account_type=AccountType.PROMOTER,risk_level='normal')


def work(identity,*,account_id=33,family='business_maintenance',membership_id=60):
    return QualificationWork(id=identity,account_id=account_id,group_id=60,membership_id=membership_id,
        membership_joined_at=NOW-timedelta(days=3),scope_key=f'overview-work-{identity}',
        policy_version='test',profile_fingerprint='test',family=family,state='WAIT_EVENT',
        reason='business_evidence_missing',next_step_reads=0,updated_at=NOW)


async def seed(db):
    db.add_all([account(33),account(34),AccountOperationConfig(account_id=33,enabled=True,
        auto_join_enabled=True,auto_ads_enabled=False),SystemSetting(key='automation.group_qualification',
        value=json.dumps({'enabled':True,'rollout_accounts':{'33':{'phase':'dynamic'}}}))])
    # Use the actual policy key, so all join gates read the real SQL policy.
    from app.modules.acquisition.qualification_service import SETTING_KEY
    setting=next(row for row in db.new if isinstance(row,SystemSetting))
    setting.key=SETTING_KEY
    candidates=[]
    for identity in (10,20,30,40,50):
        group=Group(id=identity,group_id=-1_000_000_000_000-identity,username=f'overview_{identity}',
            title=f'candidate {identity}',status='pending_join')
        candidates.append(group)
        db.add(group)
    db.add(Group(id=60,group_id=-1_000_000_000_060,title='joined',status='active'))
    for identity,account_id,group_id in ((60,33,60),(61,34,40),(62,34,60)):
        db.add(GroupAccountMembership(id=identity,account_id=account_id,group_id=group_id,
            telegram_group_id=-1_000_000_000_000-group_id,status='joined',joined_at=NOW-timedelta(days=3)))
    from app.modules.acquisition.candidate_inventory import key
    ready=candidates[0]
    db.add(SystemSetting(key=key(33,ready.id),value=json.dumps({'version':1,'account_id':33,
        'telegram_group_id':ready.group_id,'username':ready.username,'namespace':'channel',
        'member_count':75,'status':'metadata_checked','checked_at':NOW.isoformat(),'efficiency_mode':True})))
    # Another account's unknown join fences this target globally, without
    # turning an unrelated target or the whole account into a global blocker.
    db.add(AutoJoinAttempt(account_id=34,group_id=50,telegram_group_id=-1_000_000_000_050,
        request_state='outcome_unknown',status='pending',telegram_action_attempted=True,request_sent_at=NOW))
    db.add_all([work(1),work(2,family='qualification'),work(3,account_id=34,membership_id=62)])
    await db.flush()
    from app.modules.acquisition.qualification_engine import _scope
    current=await db.get(QualificationWork,2)
    current.scope_key,current.policy_version,current.profile_fingerprint=await _scope(
        db,await db.get(GroupAccountMembership,60))
    await db.commit()


@contextmanager
def unlocked_selects_only(engine):
    statements=[]
    def check(connection,cursor,statement,parameters,context,many):
        normalized=statement.lstrip().upper()
        assert normalized.startswith(('SELECT','SAVEPOINT','RELEASE','ROLLBACK')),statement
        assert 'FOR UPDATE' not in normalized,statement
        statements.append(normalized)
    event.listen(engine.sync_engine,'before_cursor_execute',check)
    try:yield statements
    finally:event.remove(engine.sync_engine,'before_cursor_execute',check)


@pytest.fixture
def no_execution(monkeypatch):
    import app.core.redis as redis_module
    from app.core.account.pool import AccountPool
    from app.core.account import budget_ledger
    from app.modules.acquisition import qualification_engine
    spies=[]
    for owner,name in ((AccountPool,'acquire_by_id'),(budget_ledger,'reserve'),
                       (budget_ledger,'reserve_read_work'),(qualification_engine,'enqueue'),(redis_module,'get_redis')):
        spy=AsyncMock(side_effect=AssertionError(f'projection cannot execute {name}'))
        monkeypatch.setattr(owner,name,spy)
        spies.append(spy)
    from app.core.config import settings
    monkeypatch.setattr(settings,'GROWTH_EFFICIENCY_ENABLED',True)
    monkeypatch.setattr(settings,'GROWTH_EFFICIENCY_ACCOUNT_IDS','33')
    yield spies
    for spy in spies:spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_inventory_sql_keeps_target_fences_scope_and_maintenance_separate(sql_db,no_execution):
    db,engine=sql_db
    await seed(db)
    db.commit=AsyncMock(side_effect=AssertionError('GET must not commit'))
    with unlocked_selects_only(engine) as sql:
        result=await helper.overview(db,33,NOW)
    assert result['available'],result
    assert result['ad_targets']==0 and result['join_candidates']==1
    assert result['join_candidates_total']==3 and result['join_candidates_preview_pending']==2
    assert result['shared_candidate_pool_available']==3 and result['sample_complete'] is True
    assert result['maintenance']['available'] is True
    assert [row['id'] for row in result['maintenance']['items']]==[1]
    assert result['maintenance']['blocks_qualification'] is False
    assert result['live_boundary_recheck_required'] is True
    assert any('ACQUISITION_AUTO_JOIN_ATTEMPT' in query for query in sql)
    assert not db.new and not db.dirty and not db.deleted
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintenance_sql_failure_does_not_erase_known_inventory(sql_db,no_execution):
    db,engine=sql_db
    await seed(db)
    original=db.scalars
    async def fail_only_maintenance(statement,*args,**kwargs):
        if any(value.value=='business_maintenance' for value in statement._generate_cache_key().bindparams):
            raise RuntimeError('maintenance unavailable')
        return await original(statement,*args,**kwargs)
    db.scalars=fail_only_maintenance
    with unlocked_selects_only(engine):
        result=await helper.overview(db,33,NOW)
    assert result['available'] and result['join_candidates']==1
    assert result['maintenance']=={'available':False,'reason':'maintenance_snapshot_unavailable',
        'items':[],'blocks_qualification':False,'retry_is_prediction':True}
    assert await db.scalar(select(func.count(QualificationWork.id)))==3


@pytest.mark.asyncio
async def test_maintenance_sql_is_account_scoped_bounded_and_honest(sql_db,no_execution):
    db,engine=sql_db
    await seed(db)
    db.add_all([work(identity) for identity in range(10,111)])
    await db.commit()
    with unlocked_selects_only(engine):
        result=await helper.overview(db,33,NOW)
    maintenance=result['maintenance']
    assert maintenance['truncated'] is True and maintenance['limit']==100
    assert len(maintenance['items'])==100
    assert maintenance['items'][0]['id']==110 and maintenance['items'][-1]['id']==11
    assert result['join_candidates']==1


@pytest.mark.asyncio
async def test_runtime_http_carries_real_readiness_and_maintenance_without_execution(sql_db,no_execution,monkeypatch):
    db,engine=sql_db
    await seed(db)
    from app.core.config import settings
    import app.core.account.rpc_governor as governor
    monkeypatch.setattr(settings,'BUDGET_LEDGER_ENABLED',False)
    monkeypatch.setattr(governor,'snapshot',AsyncMock(return_value={'state':'ready',
        'critical_workloads':{'review':{'remaining':0}},'lanes':{'ad_refresh':{'remaining':0}},'usage':{}}))
    monkeypatch.setattr('app.core.account.read_metrics.snapshot',AsyncMock(return_value={'available':False,'actual_reads':None}))
    monkeypatch.setattr(api,'sample_summary',AsyncMock(return_value={'available':False}))
    monkeypatch.setattr('app.modules.acquisition.review_read_runtime.evidence_status',AsyncMock(return_value={'enabled':False}))
    monkeypatch.setattr('app.modules.acquisition.review_storage_budget.verify',lambda paths:{'ok':False})
    app=FastAPI()
    app.include_router(api.router,prefix='/automation')
    async def local_db():yield db
    app.dependency_overrides[get_db]=local_db
    app.dependency_overrides[require_admin]=lambda:SimpleNamespace(is_admin=True)
    db.commit=AsyncMock(side_effect=AssertionError('runtime GET must not commit'))
    with unlocked_selects_only(engine):
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://fixture') as client:
            response=await client.get('/automation/group-reviews/runtime',params={'account_id':33})
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert data['business']['available'] is True,data
    assert data['business']['readiness']['join_candidates']==1
    assert data['business']['readiness']['maintenance']['items'][0]['id']==1
    assert data['business']['ad_opportunities']['completion_rate'] is None
    assert data['efficiency']['net_savings'] is None and data['measurement']['actual_reads'] is None
    assert data['current_work']['items'][0]['job']['id']==2
    assert not db.new and not db.dirty
    db.commit.assert_not_awaited()
