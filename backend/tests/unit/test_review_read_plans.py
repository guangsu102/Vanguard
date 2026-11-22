from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.qualification_ai import profile_fingerprint
from app.modules.acquisition.review_collection_progress import for_event
from app.modules.acquisition.review_read_flags import measurement_stage, stage_enabled
from app.modules.acquisition.review_read_plan import ReadPlan, finish_candidate, plan


@pytest.fixture
def scope(monkeypatch):
    for key in ("REVIEW_READ_OPTIMIZATION_ENABLED", "REVIEW_READ_MINIMAL_PLAN", "REVIEW_READ_REUSE"):
        monkeypatch.setattr(settings, key, True)
    monkeypatch.setattr(settings, "REVIEW_READ_FAST_PATH", False)
    monkeypatch.setattr(settings, "REVIEW_READ_PUSH", False)
    monkeypatch.setattr(settings, "REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS", "34")
    now = datetime(2026, 11, 3, 12)
    member = SimpleNamespace(id=1, account_id=34, group_id=8, joined_at=now-timedelta(days=4), review_status="approved")
    row = SimpleNamespace(membership_id=1, membership_joined_at=member.joined_at, account_id=34)
    account = SimpleNamespace(id=34, profile_bio="same profile")
    previous = {"account_id":34, "policy_version":POLICY_VERSION,
                "profile_fingerprint":profile_fingerprint(account)}
    return previous, row, member, account, now


def test_membership_only_can_plan_a_complete_six_read_slice(scope):
    prev, row, member, account, now = scope
    result = plan(prev,row,member,account,{"kinds":["membership"]},now,renewal=True)
    assert result.bounded and result.reads == 6
    assert result.phase == "permissions_only"


@pytest.mark.parametrize("event", [None, {"kinds":["gap"]}, {"kinds":["rules"]},
                                    {"kinds":["rules"],"message_ids":list(range(1,8))}])
def test_unknown_or_large_renewal_keeps_original_forecast(scope,event):
    prev,row,member,account,now=scope
    result=plan(prev,row,member,account,event,now,renewal=True)
    assert not result.bounded and result.reads==16


def test_rule_message_does_not_skip_unread_rule_dependencies(scope):
    prev,row,member,account,now=scope
    prev['rules_evaluation_status']='NOT_EVALUATED'
    result=plan(prev,row,member,account,{'kinds':['rules'],'message_ids':[10]},now,renewal=True)
    assert not result.bounded


def test_failed_candidate_returns_to_full_plan(scope,monkeypatch):
    prev,row,member,account,now=scope
    monkeypatch.setattr(settings,'REVIEW_READ_FAST_PATH',True)
    prev['read_plan_requires_full']=True
    result=plan(prev,row,member,account,None,now,candidate_available=True)
    assert result.phase=='full' and result.reads==24 and not result.bounded


def test_outside_allowlist_cannot_receive_smaller_slice(scope):
    prev,row,member,account,now=scope
    account.id=33
    result=plan(prev,row,member,account,{'kinds':['membership']},now,renewal=True)
    assert result.reads==16 and not result.bounded
    assert measurement_stage(33)=='baseline'


def test_each_stage_has_independent_and_total_switch(scope,monkeypatch):
    assert stage_enabled(34,'reuse') and not stage_enabled(34,'fast_path')
    assert measurement_stage(34)=='phase1'
    monkeypatch.setattr(settings,'REVIEW_READ_PUSH',True)
    assert measurement_stage(34)=='phase2'
    monkeypatch.setattr(settings,'REVIEW_READ_OPTIMIZATION_ENABLED',False)
    assert not stage_enabled(34,'reuse') and measurement_stage(34)=='baseline'


def test_bad_allowlist_fails_closed(scope,monkeypatch):
    monkeypatch.setattr(settings,'REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS','34,broken')
    assert not stage_enabled(34,'reuse')


def test_precise_rule_invalidates_only_target_without_mutating_prior():
    old={'kinds':['rules'],'message_ids':[1],'token':'old','joined_at':'same'}
    new={**old,'message_ids':[2],'token':'new'}
    previous={'collection_event':old,'collection_progress':{'history_cursors':[{'offset_id':100}]},
              'evidence':[{'message_id':1},{'message_id':2}]}
    result=for_event(previous,new)
    assert result['collection_progress']['history_cursors']==[{'offset_id':100}]
    assert result['evidence']==[{'message_id':1}]
    assert result['collection_progress']['pending_message_ids']==[2]
    assert previous['evidence']==[{'message_id':1},{'message_id':2}]


@pytest.mark.parametrize('event',[{'kinds':['gap']},{'kinds':['rules'],'unscoped_rules_token':'new'},
                                 {'kinds':['membership'],'account_generation':'gap:new'}])
def test_uncertain_changes_clear_history_positions(event):
    previous={'collection_event':None,'collection_progress':{'history_cursors':[{'offset_id':100}]}}
    assert for_event(previous,event)['collection_progress']['history_cursors']==[]


def test_queue_bookkeeping_does_not_invalidate_progress():
    event={'kinds':['rules'],'token':'same','message_ids':[1],'queued':False}
    previous={'collection_event':event,'collection_progress':{'history_cursors':[{'offset_id':5}]}}
    assert for_event(previous,{**event,'queued':True}) is previous


def test_rejoin_invalidates_even_precise_membership_event():
    old = {'kinds': ['membership'], 'joined_at': 'old'}
    previous = {'collection_event': old, 'collection_progress': {'history_cursors': [{'offset_id': 5}]}}
    result = for_event(previous, {**old, 'joined_at': 'new'})
    assert result['collection_progress']['history_cursors'] == []


@pytest.mark.parametrize('snapshot', [
    {'collection_halted': 'telegram_read_slice'},
    {'quality_status': 'technical_wait', 'pending_collection': {'evidence': [{'message_id': 1}]}},
])
def test_deferred_candidate_promotes_the_next_effective_snapshot_to_full(scope, snapshot):
    prev, row, member, account, now = scope
    finish_candidate(ReadPlan('candidate', 12, 'one_mature_candidate', True), snapshot)
    next_previous = snapshot.get('pending_collection') or snapshot
    assert next_previous['read_plan_requires_full'] is True
    assert plan(next_previous, row, member, account, None, now).reads == 24


def test_complete_candidate_and_unrelated_work_keep_their_plan():
    success = {'decision_dependencies_complete': True}
    finish_candidate(ReadPlan('candidate', 12, 'ready', True), success)
    assert 'read_plan_requires_full' not in success
    full = {'quality_status': 'technical_wait'}
    finish_candidate(ReadPlan('full', 24, 'original'), full)
    assert 'read_plan_requires_full' not in full
