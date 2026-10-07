"""Backlog retirement must preserve grants and wake only on real new work."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.listener_facts import apply_fact
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.review_retention import retire_obsolete_reviews
from tests.unit.test_qualification_service import setup, snapshot


@pytest.mark.asyncio
async def test_retirement_is_idempotent_preserves_grant_and_current_review(test_db):
    _, group, member, grant = await setup(test_db)
    grant.next_retry_at = datetime.utcnow()
    evidence = grant.evidence_json
    older = GroupQualificationAudit(batch_id='old-history', membership_id=member.id, account_id=2,
        group_id=group.id, policy_version=grant.policy_version, membership_joined_at=member.joined_at,
        state='manual_required', decision='observe', next_retry_at=datetime.utcnow())
    latest = GroupQualificationAudit(batch_id='current-work', membership_id=member.id, account_id=2,
        group_id=group.id, policy_version=grant.policy_version, membership_joined_at=member.joined_at,
        state='queued', decision='observe', next_retry_at=datetime.utcnow())
    test_db.add_all([older, latest]);await test_db.commit()
    assert await retire_obsolete_reviews(test_db) == 2
    assert older.state == 'cancelled' and older.next_retry_at is None
    assert grant.decision == 'allowed' and grant.evidence_json == evidence and grant.next_retry_at is None
    assert latest.state == 'queued'
    assert await retire_obsolete_reviews(test_db) == 0
    await service.ensure_membership_reviews(test_db, {'enabled': True, 'account_ids': [2]})
    assert len((await test_db.scalars(select(GroupQualificationAudit))).all()) == 3
    assert older.state == 'cancelled'


@pytest.mark.asyncio
async def test_protected_and_unconfirmed_memberships_do_not_requeue_from_gap(test_db):
    _, _, member, row = await setup(test_db)
    member.telegram_group_id = -1001234567890
    row.decision, row.state = 'protected', 'queued'
    member.review_status = 'owned_group_excluded'
    await test_db.commit()
    assert await retire_obsolete_reviews(test_db) == 1
    fact = {'kind': 'gap', 'peer': member.telegram_group_id, 'at': datetime.now(UTC).timestamp()}
    await apply_fact(test_db, 2, fact)
    assert row.state == 'completed' and row.next_retry_at is None
    member.review_status, member.status = 'initial_pending', 'pending'
    row.decision, row.reason, row.state = 'unknown', 'join_approval_pending', 'queued'
    await test_db.flush()
    assert await retire_obsolete_reviews(test_db) == 1
    await apply_fact(test_db, 2, fact)
    assert row.state == 'waiting_membership' and row.next_retry_at is None


def test_unchanged_rejected_content_is_terminal_but_changed_content_resumes_once():
    now = datetime.utcnow()
    first = snapshot('observe', reason='group_rules_ai_provider_content_rejected')
    result = service.review_schedule(first, {}, now)
    assert result == ('reject', 'group_rules_ai_provider_content_rejected', 'completed', None)
    assert first['review_trigger'] == 'ai_terminal_failure'

    duplicate = snapshot('observe', reason='group_rules_ai_provider_content_rejected')
    assert service.review_schedule(duplicate, first, now) == (
        'reject', 'group_rules_ai_provider_content_rejected', 'completed', None
    )

    changed = snapshot('observe', reason='group_rules_ai_provider_content_rejected')
    changed.setdefault('evidence', []).append(
        {'source': 'full_about', 'message_id': 999, 'text': 'changed rules'}
    )
    assert service.review_schedule(changed, first, now)[3] == now + timedelta(hours=2)
    assert changed['review_trigger'] == 'evidence_changed'


def test_exhausted_observation_waits_for_evidence_and_budget_still_has_deadline():
    now = datetime.utcnow()
    check = snapshot('observe', reason='qualification_evidence_incomplete', unknowns=['rules'])
    assert service.review_schedule(check, {'observation_started_at': (now-timedelta(hours=25)).isoformat()}, now)[3] is None
    check = snapshot('technical_wait', reason='telegram_read_budget', retry_after_seconds=3600)
    assert service.review_schedule(check, {'technical_failures': 20}, now)[3] == now+timedelta(hours=1)


@pytest.mark.asyncio
async def test_new_evidence_wakes_wait_but_duplicate_hint_and_operator_pause_do_not(test_db):
    _, _, member, row = await setup(test_db)
    member.telegram_group_id = -1001234567890
    now = datetime.utcnow()
    member.ad_status, member.review_status = 'blocked', 'review_2h'
    row.state, row.decision, row.next_retry_at = 'completed', 'observe', None
    row.checked_at = now - timedelta(hours=3)
    row.evidence_json = json.dumps({'review_trigger': 'evidence_changed'})
    fact = {'kind': 'evidence_candidate', 'peer': member.telegram_group_id,
            'at': now.replace(tzinfo=UTC).timestamp(), 'joined_at': member.joined_at.isoformat(),
            'message_id': 456, 'message_date': (now-timedelta(hours=25)).isoformat()}
    await test_db.commit()
    await apply_fact(test_db, 2, fact)
    assert row.state == 'queued' and row.next_retry_at >= now
    row.state, row.next_retry_at = 'completed', None
    await test_db.flush()
    await apply_fact(test_db, 2, fact)
    assert row.state == 'completed' and row.next_retry_at is None
    member.ad_status = 'paused';fact['message_id'] = 457
    await apply_fact(test_db, 2, fact)
    assert row.state == 'completed' and row.next_retry_at is None

@pytest.mark.asyncio
async def test_maintenance_paused_schedule_resumes_only_when_account_enabled(test_db):
    from types import SimpleNamespace
    from app.core.account.models import AccountOperationConfig
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.models import AdCampaign, AdDeliveryScheduleState
    _, group, member, _ = await setup(test_db)
    config = AccountOperationConfig(account_id=2, enabled=False, auto_ads_enabled=False)
    campaign = AdCampaign(name='maintenance-resume', enabled=True)
    test_db.add_all([config, campaign]);await test_db.flush()
    schedule = AdDeliveryScheduleState(campaign_id=campaign.id, account_id=2, group_id=group.id,
        telegram_group_id=member.telegram_group_id, next_due_at=datetime.utcnow()-timedelta(minutes=1),
        status='paused', last_reason='history_cleanup_inactive')
    test_db.add(schedule);await test_db.commit()
    auto = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    args = dict(campaign=campaign, account_id=2, membership=member, lease_seconds=60)
    assert (await auto._claim_ad_schedule_state(**args))[1] is None
    assert schedule.status == 'paused'
    config.enabled = config.auto_ads_enabled = True
    await test_db.commit()
    assert (await auto._claim_ad_schedule_state(**args))[1] is not None
    assert schedule.status == 'sending'
