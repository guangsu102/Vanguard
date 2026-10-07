"""Exercise renewal through real claims/assessment while initial reads wait."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.capacity import inventory_snapshot
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_events import pending, request
from app.modules.acquisition.qualification_renewal import REFRESH_PURPOSE, eligible
from tests.unit.test_qualification_light_renewal import fixture
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


async def test_exhausted_initial_reads_do_not_starve_real_ad_renewal(test_db, monkeypatch):
    actor, account, group, member, renewal, _ = await fixture(test_db)
    actor._join_review_account_block_reason = lambda *args: None
    actor._sync_group_ad_policy_from_audit = AsyncMock()
    now = datetime.utcnow()
    renewal.state, renewal.next_retry_at = 'queued', now
    await request(test_db, member, 'rules', message_ids=[901])
    initial = []
    for i in range(4):
        other_group = Group(id=41+i, group_id=-1002000000000-i)
        other_member = GroupAccountMembership(id=61+i, group_id=other_group.id,
            telegram_group_id=other_group.group_id, account_id=account.id,
            status='joined', joined_at=member.joined_at,
            review_status='initial_pending', ad_status='blocked')
        other = GroupQualificationAudit(batch_id=f'waiting-{i}', membership_id=other_member.id,
            account_id=account.id, group_id=other_group.id, policy_version=service.POLICY_VERSION,
            content_scope='text_profile', membership_joined_at=member.joined_at,
            state='queued', decision='technical_wait', reason='telegram_read_budget', next_retry_at=now)
        test_db.add_all([other_group, other_member, other])
        initial.append(other)
    await test_db.commit()
    monkeypatch.setattr(service, 'ensure_membership_reviews', AsyncMock())
    purposes = []

    async def readiness(db, account_id, now, *, purpose, **kwargs):
        purposes.append(purpose)
        return None if purpose == REFRESH_PURPOSE else ('telegram_read_budget', now+timedelta(hours=1))

    monkeypatch.setattr('app.core.account.read_schedule.read_wait', readiness)
    refresh = AsyncMock()
    monkeypatch.setattr('app.modules.acquisition.qualification_actions.refresh_live_authorization', refresh)
    result = await service.run_reviews(actor, account_id=account.id, limit=2)
    assert result['processed'] == 1
    assert purposes == ['group_qualification']*4 + [REFRESH_PURPOSE]
    refresh.assert_awaited_once()
    assert actor.account_pool.acquire_by_id.await_args.kwargs['purpose'] == REFRESH_PURPOSE
    assert renewal.state == 'completed' and renewal.next_retry_at is None
    assert member.ad_status == 'active' and member.review_status == 'approved'
    assert await pending(test_db, member) is None
    assert all(r.attempts == 0 and r.next_retry_at > now for r in initial)
    purposes.clear()
    await service.run_reviews(actor, account_id=account.id, limit=2)
    assert purposes == []  # Budget waits do not ignore the persisted deadline.


@pytest.mark.parametrize('change', ['decision', 'profile', 'scope', 'invalidated', 'ai', 'paused'])
async def test_unapproved_or_changed_evidence_cannot_use_ad_refresh_budget(test_db, change):
    _, account, group, member, row, previous = await fixture(test_db)
    if change == 'decision': row.decision = 'observe'
    if change == 'profile': previous['profile_fingerprint'] = 'changed'
    if change == 'scope': row.membership_joined_at -= timedelta(days=1)
    if change == 'invalidated': previous['invalidated_at'] = datetime.utcnow().isoformat()
    if change == 'ai': previous['ai_pending'] = True
    if change == 'paused': member.ad_status = 'paused'
    assert not await eligible(test_db, account, group, member, row, previous)


async def test_scheduled_observation_remains_backlog_until_wait_has_no_timer(test_db):
    account, _, member, row = await setup(test_db, decision='observe')
    member.review_status, member.ad_status = 'review_2h', 'blocked'
    row.next_retry_at = datetime.utcnow()+timedelta(hours=2)
    await test_db.commit()
    counted = await inventory_snapshot(test_db, account.id)
    assert counted['active_backlog'] == counted['probe_active_backlog'] == 1
    row.next_retry_at = None
    await test_db.commit()
    assert (await inventory_snapshot(test_db, account.id))['active_backlog'] == 0


async def test_terminal_rules_observation_waits_for_a_real_evidence_event(test_db):
    from app.core.account.listener_facts import apply_fact
    from app.modules.acquisition.review_retention import waits_for_new_evidence
    from datetime import UTC

    _, _, member, row = await setup(test_db, decision='observe')
    now = datetime.utcnow()
    previous = {'decision':'observe', 'reason':'rules_coverage_incomplete',
                'observation_started_at':(now-timedelta(hours=25)).isoformat()}
    assert waits_for_new_evidence(previous, now)
    previous['review_trigger'] = 'evidence_changed'
    row.evidence_json = json.dumps(previous)
    row.state, row.next_retry_at = 'completed', None
    member.review_status, member.ad_status = 'review_2h', 'blocked'
    member.telegram_group_id = -1001234567890
    await test_db.commit()
    fact = {'kind':'gap', 'peer':member.telegram_group_id,
            'at':now.replace(tzinfo=UTC).timestamp(), 'joined_at':member.joined_at.isoformat()}
    await apply_fact(test_db, member.account_id, fact)
    assert row.next_retry_at is None and row.decision == 'observe'
    await apply_fact(test_db, member.account_id, {**fact, 'kind':'rules', 'message_ids':[999]})
    assert row.state == 'queued' and row.decision == 'observe'
    assert (await pending(test_db, member))['message_ids'] == [999]
