"""Business review progress survives recurring renewals and sync-gap callbacks."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.listener_facts import apply_fact
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_events import pending, queue_due_gaps, request
from app.modules.acquisition.qualification_queue import cursor
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


async def test_continuous_renewal_cannot_starve_initial_reviews_across_read_windows(test_db, monkeypatch):
    _, _, member, renewal = await setup(test_db)
    now = datetime.utcnow()
    renewal.state, renewal.next_retry_at = 'queued', now
    initial = []
    for offset in range(5):
        group = Group(id=41+offset, group_id=1234567891+offset, title='pending')
        joined = GroupAccountMembership(id=61+offset, group_id=group.id,
            telegram_group_id=group.group_id, account_id=2, status='joined',
            joined_at=member.joined_at, review_status='review_2h', ad_status='blocked')
        row = GroupQualificationAudit(batch_id=f'initial-{offset}', membership_id=joined.id,
            account_id=2, group_id=group.id, policy_version=POLICY_VERSION,
            content_scope='text_profile', membership_joined_at=joined.joined_at,
            state='queued', decision='observe', next_retry_at=now)
        test_db.add_all([group, joined, row])
        initial.append(row)
    await test_db.commit()
    monkeypatch.setattr(service, 'ensure_membership_reviews', AsyncMock())
    monkeypatch.setattr('app.core.account.read_schedule.read_wait', AsyncMock(return_value=None))
    seen = []

    async def assess(actor, account_id, group, *, row):
        seen.append(row.id)
        row.state, row.next_retry_at = 'completed', None
        return SimpleNamespace(passed=False, reason='reviewed')

    monkeypatch.setattr(service, 'assess', assess)
    for window in range(10):
        # Each window has enough budget for one review. Existing ad targets
        # keep receiving events, including while initial reviews wait.
        renewal.state, renewal.next_retry_at = 'queued', now
        await request(test_db, member, 'gap')
        for row in initial:
            if row.id not in seen:
                row.next_retry_at = now
        await test_db.commit()
        before = await cursor(test_db, 2)
        if window:
            wait = AsyncMock(return_value=('telegram_read_budget', now+timedelta(hours=1)))
            monkeypatch.setattr('app.core.account.read_schedule.read_wait', wait)
            assert (await service.run_reviews(SimpleNamespace(db=test_db), account_id=2, limit=1))['processed'] == 0
            assert await cursor(test_db, 2) == before
            renewal.next_retry_at = now
            for row in initial:
                if row.id not in seen:
                    row.next_retry_at = now
            await test_db.commit()
            monkeypatch.setattr('app.core.account.read_schedule.read_wait', AsyncMock(return_value=None))
        await service.run_reviews(SimpleNamespace(db=test_db), account_id=2, limit=1)
    assert seen[::2] == [row.id for row in initial]
    assert seen[1::2] == [renewal.id]*5


@pytest.mark.parametrize('decision,state', [('observe','completed'), ('reject','completed'),
                                          ('technical_wait','queued')])
async def test_generic_gap_does_not_resurrect_conclusions_or_reset_backoff(test_db, decision, state):
    _, _, member, row = await setup(test_db, decision=decision)
    member.telegram_group_id = -1001234567890
    member.review_status, member.ad_status = 'review_2h', 'blocked'
    deadline = datetime.utcnow()+timedelta(minutes=40) if state == 'queued' else None
    row.state, row.next_retry_at = state, deadline
    original = row.evidence_json
    await test_db.commit()
    fact = {'kind':'gap', 'peer':member.telegram_group_id, 'at':datetime.now(UTC).timestamp(),
            'joined_at':member.joined_at.isoformat()}
    await apply_fact(test_db, 2, fact)
    assert (row.state,row.next_retry_at,row.evidence_json) == (state,deadline,original)


@pytest.mark.parametrize('retry_state', ['queued', 'completed'])
async def test_concrete_rule_change_wakes_observation_and_preserves_existing_wait(test_db, retry_state):
    _, _, member, row = await setup(test_db, decision='observe')
    member.telegram_group_id = -1001234567890
    member.review_status, member.ad_status = 'review_2h', 'blocked'
    row.state, row.next_retry_at = 'completed', None
    await test_db.commit()
    fact = {'kind':'rules', 'peer':member.telegram_group_id, 'at':datetime.now(UTC).timestamp(),
            'joined_at':member.joined_at.isoformat(), 'message_ids':[901]}
    await apply_fact(test_db, 2, fact)
    assert row.state == 'queued' and 'rules' in (await pending(test_db,member))['kinds']
    deadline = datetime.utcnow()+timedelta(minutes=40)
    row.state, row.next_retry_at = retry_state, deadline
    fact['message_ids'] = [902]
    await test_db.flush()
    await apply_fact(test_db, 2, fact)
    assert row.next_retry_at == deadline
    assert (await pending(test_db,member))['message_ids'] == [901,902]


async def test_new_callback_cannot_erase_renewal_read_backoff(test_db):
    _, _, member, row = await setup(test_db)
    deadline = datetime.utcnow()+timedelta(minutes=40)
    row.state, row.next_retry_at = 'completed', deadline
    await request(test_db,member,'rules',message_ids=[901])
    await test_db.commit()
    assert await queue_due_gaps(test_db,2) == 0
    assert row.next_retry_at == deadline and await pending(test_db,member)
    row.next_retry_at = datetime.utcnow()-timedelta(seconds=10)
    old_due = row.next_retry_at
    await test_db.flush()
    assert await queue_due_gaps(test_db,2) == 1
    assert row.state == 'queued' and row.next_retry_at == old_due


@pytest.mark.parametrize('hold', ['exit_pending','manual_required'])
async def test_rule_callbacks_keep_explicit_membership_hold(test_db,hold):
    _, _, member, row = await setup(test_db,decision='reject')
    member.telegram_group_id = -1001234567890
    member.review_status, member.ad_status = hold,'blocked'
    row.state, row.next_retry_at = 'completed',None
    await test_db.commit()
    await apply_fact(test_db,2,{'kind':'rules','peer':member.telegram_group_id,
        'at':datetime.now(UTC).timestamp(),'joined_at':member.joined_at.isoformat()})
    assert member.review_status == hold and row.state == 'completed' and row.next_retry_at is None


@pytest.mark.parametrize('state', ['exit_pending','leave_failed','membership_reconciliation'])
async def test_ordinary_review_cannot_steal_exit_reconciliation(test_db,monkeypatch,state):
    _,_,member,row = await setup(test_db,decision='technical_wait')
    member.review_status,member.ad_status = state,'blocked'
    row.state,row.next_retry_at = 'completed',datetime.utcnow()-timedelta(minutes=1)
    row.reason = 'telegram_read_budget'
    await test_db.commit()
    monkeypatch.setattr(service,'ensure_membership_reviews',AsyncMock())
    assess = AsyncMock()
    monkeypatch.setattr(service,'assess',assess)
    assert (await service.run_reviews(SimpleNamespace(db=test_db),account_id=2,limit=2))['processed']==0
    assert member.review_status==state and row.attempts==0
    assess.assert_not_awaited()


async def test_real_exit_assessment_keeps_task_when_rpc_budget_is_empty(test_db):
    from app.core.account.rpc_governor import RpcDeferred
    from app.modules.acquisition.qualification_actions import _run_exits_serialized

    _,_,member,row = await setup(test_db,decision='reject')
    member.review_status,member.ad_status = 'exit_pending','blocked'
    member.review_next_at = datetime.utcnow()-timedelta(minutes=1)
    await test_db.commit()
    pool = SimpleNamespace(add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(side_effect=RpcDeferred('telegram_read_budget',600)),
        release=AsyncMock())
    actor = SimpleNamespace(db=test_db,account_pool=pool,
        _join_review_account_block_reason=lambda *args:None,_leave_group=AsyncMock())
    result = await _run_exits_serialized(actor,account_id=2)
    assert result['processed']==1
    pool.acquire_by_id.assert_awaited_once()
    assert member.review_status=='exit_pending'
    assert row.decision=='technical_wait' and row.reason=='telegram_read_budget'
    assert member.review_next_at >= datetime.utcnow()+timedelta(seconds=590)
    actor._leave_group.assert_not_awaited()
