"""Latest user rules: independent ad proof, automatic recovery, current/future accounts."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.capacity import executable_inventory
from app.modules.acquisition.group_qualification import quality_decision
from app.modules.acquisition.qualification_system_identity import ensure_system_identities
from app.modules.acquisition.qualification_verification import _claim
from tests.unit.test_qualification_assess_rule_scope import assess
from tests.unit.test_qualification_automation_lifecycle import case
from tests.unit.test_qualification_service import setup
from tests.unit.test_qualification_verification import setup as verification_setup
from tests.unit.test_dynamic_outbound_capacity import account_config

pytestmark = pytest.mark.asyncio

@pytest.mark.parametrize('rule', [
    '禁止任何广告、外链和简介引流。',
    '广告仅限指定话题，其他地方禁止广告。',
    '禁止直接链接，但允许普通成员文字广告及简介引导。',
])
async def test_ordinary_precedent_is_independent_of_rule_permission(test_db, rule):
    result, row, llm = await assess(test_db, rule, [], ad_count=1, online_count=2)
    assert result.passed and row.decision == 'trial'
    llm.generate.assert_not_awaited()
    snapshot = json.loads(row.evidence_json)
    snapshot['rules_incomplete'] = True
    assert quality_decision(snapshot)[0] == 'qualified'

@pytest.mark.parametrize('status,age,survival,expected', [
    ('success',25,'survived',None),
    ('success',23,'survived','qualification_group_daily_cap'),
    ('success',25,'pending','qualification_previous_survival_unresolved'),
    ('unknown',25,'survived','qualification_delivery_reconciliation_required'),
])
async def test_repeated_trial_preserves_real_send_and_survival_guards(test_db,status,age,survival,expected):
    service, account, group, _, log, now = await case(test_db)
    log.status, log.sent_at, log.survival_status = status, now-timedelta(hours=age), survival
    log.qualification_context_json = json.dumps({'decision':'trial'})
    await test_db.commit()
    context={'account_id':account.id,'decision':'trial','rollout_phase':'dynamic'}
    assert await service._qualification_delivery_quota_reason(context,group.group_id,now)==expected

async def test_contention_does_not_consume_join_technical_failures(test_db):
    service, _, _, attempt, _, now = await case(test_db)
    attempt.reconciliation_failure_count=2
    await service._defer_join_reconciliation(attempt,now,'lease_busy',contention=True)
    assert attempt.reconciliation_failure_count==2
    assert attempt.reconciliation_status=='pending'
    assert attempt.reconciliation_next_at==now+timedelta(minutes=1)
    assert attempt.request_state=='sent'

async def test_new_identity_registers_and_renews_existing_approvals(test_db):
    _, _, _, row=await setup(test_db)
    account=TelegramAccount(id=3,identifier='new',session_name='new',status=AccountStatus.ONLINE)
    test_db.add(account)
    setting=await test_db.get(SystemSetting,qualification.SETTING_KEY)
    config=json.loads(setting.value);config['automatic_identity_registration']=True
    setting.value=json.dumps(config);await test_db.commit()
    client=Obj(get_me=AsyncMock(return_value=Obj(id=301,bot=False)))
    pool=Obj(add_account_from_db=AsyncMock(),acquire_by_id=AsyncMock(return_value=Obj(client=client)),release=AsyncMock())
    result=await ensure_system_identities(Obj(db=test_db,account_pool=pool))
    assert result['registered']==1
    config=await qualification.policy(test_db,fresh=True)
    assert config['system_account_user_ids']['3']==301
    await test_db.refresh(row)
    assert row.next_retry_at<=datetime.utcnow() and row.expires_at<=datetime.utcnow()
    pool.release.assert_awaited_once()

async def test_bad_identity_session_does_not_starve_later_accounts(test_db):
    await setup(test_db)
    test_db.add_all([TelegramAccount(id=i,identifier=f'new{i}',session_name=f'new{i}',status=AccountStatus.ONLINE) for i in (3,4)])
    setting=await test_db.get(SystemSetting,qualification.SETTING_KEY)
    config=json.loads(setting.value);config['automatic_identity_registration']=True
    setting.value=json.dumps(config);await test_db.commit()
    pool=Obj(add_account_from_db=AsyncMock(),acquire_by_id=AsyncMock(return_value=None),release=AsyncMock())
    service=Obj(db=test_db,account_pool=pool)
    await ensure_system_identities(service,limit=1)
    pool.acquire_by_id=AsyncMock(return_value=Obj(client=Obj(get_me=AsyncMock(return_value=Obj(id=401,bot=False)))))
    assert (await ensure_system_identities(service,limit=1))['registered']==1
    config=await qualification.policy(test_db,fresh=True)
    assert '3' not in config['system_account_user_ids'] and config['system_account_user_ids']['4']==401

async def test_verification_follows_enabled_new_promoters(test_db):
    await setup(test_db)
    test_db.add(TelegramAccount(id=3,identifier='new',session_name='new',status=AccountStatus.ONLINE))
    test_db.add_all([AccountOperationConfig(account_id=2,enabled=False,auto_join_enabled=False),AccountOperationConfig(account_id=3,enabled=True,auto_join_enabled=True)])
    setting=await test_db.get(SystemSetting,qualification.SETTING_KEY)
    config=json.loads(setting.value);config.update(verification_active_promoters=True,verification_account_ids=[2])
    setting.value=json.dumps(config);await test_db.commit()
    config=await qualification.policy(test_db,fresh=True)
    assert config['verification_account_ids']==[3]

async def test_verification_reads_recover_after_three_failures(test_db):
    _,_,member,_=await verification_setup(test_db)
    test_db.add(SystemSetting(key=f'qualification.verification.{member.id}',value=json.dumps({
        'membership_version':member.joined_at.isoformat(),'actions':[],'read_failures':3,
        'next_check_at':(datetime.utcnow()-timedelta(seconds=1)).isoformat(),
    })))
    await test_db.commit()
    assert await _claim(test_db,member.id) is not None

@pytest.mark.parametrize('state,expected', [('member','joined'),('left','left'),('unknown','rejected')])
async def test_stale_exit_recovery_reads_membership_and_never_leaves(test_db,monkeypatch,state,expected):
    _,group,member,_=await setup(test_db)
    member.status='rejected';member.ad_status='blocked';member.review_status='exit_pending';member.review_next_at=datetime.utcnow()-timedelta(minutes=1)
    await test_db.commit()
    monkeypatch.setattr(actions,'reconcile_exit',AsyncMock(return_value=state))
    service=Obj(db=test_db,account_pool=Obj(add_account_from_db=AsyncMock()),_leave_group=AsyncMock())
    result=await actions.reconcile_stale_exit_memberships(service,{'exit_all_accounts':True})
    assert result['checked']==1 and member.status==expected
    assert member.review_status in {'initial_pending','left','membership_reconciliation'}
    service._leave_group.assert_not_awaited()
    assert 'read_only_recovery' in actions.reconcile_exit.await_args.kwargs

async def test_unresolved_candidates_are_visible_and_verified_cache_expires(test_db):
    account,config=await account_config(test_db)
    now=datetime.utcnow()
    group=Group(group_id=98765,username='pending_identity',status='pending_join')
    test_db.add(group);await test_db.commit()
    initial=await executable_inventory(test_db,account.id,config,now)
    assert initial['join_candidates']==0 and initial['join_candidates_pending_identity']==1
    assert initial['join_candidates_total']==1 and 'join_candidates_unavailable' not in initial['blocker_counts']
    record=SystemSetting(key=f'qualification.candidate_preview.{account.id}.{group.id}',value=json.dumps({
        'version':1,'account_id':account.id,
        'telegram_group_id':group.group_id,'username':group.username,'namespace':'channel',
        'checked_at':now.isoformat(),'next_preview_at':(now+timedelta(minutes=30)).isoformat(),
        'member_count':100,'ordinary_advertisers':1,
    }))
    test_db.add(record);await test_db.commit()
    fresh=await executable_inventory(test_db,account.id,config,now)
    assert fresh['join_candidates_pending_identity']==0
    expired=await executable_inventory(test_db,account.id,config,now+timedelta(minutes=31))
    assert expired['join_candidates_pending_identity']==1


@pytest.mark.parametrize('expired', [False, True])
async def test_expired_ad_approval_does_not_starve_qualification_refresh(test_db, expired):
    from app.modules.acquisition.models import AdCampaign, AdDeliveryScheduleState
    account, group, member, row = await setup(test_db)
    now = datetime.utcnow().replace(minute=1)
    row.checked_at = now - timedelta(minutes=1)
    row.expires_at = now + timedelta(hours=-1 if expired else 1)
    campaign = AdCampaign(name='due-test', status='active')
    test_db.add(campaign)
    await test_db.flush()
    test_db.add(AccountOperationConfig(account_id=account.id,enabled=True,auto_ads_enabled=True,auto_join_enabled=False))
    test_db.add(AdDeliveryScheduleState(campaign_id=campaign.id,account_id=account.id,group_id=group.id,
        telegram_group_id=group.group_id,next_due_at=now-timedelta(minutes=1),status='idle'))
    await test_db.commit()
    assert await qualification.priority_account_ids(test_db, {}, now) == (set() if expired else {account.id})


async def test_verification_unsent_attempts_retry_but_unknown_writes_remain_bounded(test_db):
    from app.modules.acquisition.qualification_verification import action_count
    _,_,member,_=await verification_setup(test_db)
    ledger={'membership_version':member.joined_at.isoformat(),'actions':[{'status':'not_sent'} for _ in range(3)]}
    record=SystemSetting(key=f'qualification.verification.{member.id}',value=json.dumps(ledger))
    test_db.add(record);await test_db.commit()
    assert action_count(ledger)==0 and await _claim(test_db,member.id) is not None
    ledger['actions']=[{'status':'unknown','attempted_at':datetime.utcnow().isoformat()} for _ in range(3)]
    record.value=json.dumps(ledger);await test_db.commit()
    assert action_count(ledger)==3 and await _claim(test_db,member.id) is None


async def test_future_promoter_inherits_rules_but_explicit_pause_is_preserved(test_db):
    await setup(test_db)
    test_db.add_all([TelegramAccount(id=i,identifier=f'new{i}',session_name=f'new{i}',status=AccountStatus.ONLINE) for i in (3,5)])
    test_db.add_all([AccountOperationConfig(account_id=i,enabled=True,auto_ads_enabled=True) for i in (3,5)])
    setting=await test_db.get(SystemSetting,qualification.SETTING_KEY)
    config=json.loads(setting.value);config['promotion_active_promoters']=True;config['rollout_accounts']['3']={'phase':'paused'}
    setting.value=json.dumps(config);await test_db.commit()
    config=await qualification.policy(test_db,fresh=True)
    assert set(config['promotion_account_ids'])=={3,5}
    assert config['rollout_accounts']['3']['phase']=='paused'
    assert config['rollout_accounts']['5']['phase']=='dynamic'
    assert await qualification.send_gate(test_db,2,1234567890,'test',None)=='qualification_account_not_promoter'


async def test_exit_account_busy_preserves_membership_and_retries_without_counting_rpc(test_db,monkeypatch):
    account,group,member,row=await setup(test_db)
    member.review_status='exit_pending';member.ad_status='blocked';member.review_next_at=datetime.utcnow()-timedelta(minutes=1)
    row.decision='reject';payload=json.loads(row.evidence_json);payload.update(permissions={'member':True},member_count=12,member_count_verified=True);row.evidence_json=json.dumps(payload)
    await test_db.commit()
    monkeypatch.setattr(actions,'assess',AsyncMock(return_value=Obj(verification_details={'qualification_decision':'reject'})))
    actor=Obj(db=test_db,_leave_group=AsyncMock(return_value='account unavailable'),_discovered_group_from_model=lambda x:x)
    await actions._run_exits_serialized(actor,limit=1)
    assert member.status=='joined' and member.review_status=='exit_pending'
    assert member.leave_attempts==0 and member.leave_confirmed_at is None
    assert datetime.utcnow()<member.review_next_at<datetime.utcnow()+timedelta(minutes=3)


async def test_exit_reconciliation_loads_account_in_fresh_worker_pool(test_db):
    account,group,member,_=await setup(test_db)
    loaded=[]
    async def load(value):loaded.append(value.id)
    client=Obj(get_permissions=AsyncMock(return_value=Obj(has_left=False,is_admin=False,is_creator=False)))
    wrapper=Obj(client=client)
    async def acquire(account_id,**kwargs):
        assert loaded==[account_id]
        return wrapper
    pool=Obj(add_account_from_db=AsyncMock(side_effect=load),acquire_by_id=AsyncMock(side_effect=acquire),release=AsyncMock())
    actor=Obj(db=test_db,account_pool=pool,_discovered_group_from_model=lambda x:x,_resolve_group_entity_for_leave=AsyncMock(return_value=(Obj(id=group.group_id),None)))
    assert await actions.reconcile_exit(actor,member,group)=='member'
    client.get_permissions.assert_awaited_once()
    pool.release.assert_awaited_once_with(wrapper)
