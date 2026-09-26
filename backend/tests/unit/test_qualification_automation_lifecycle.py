"""Durable join, rollout and survival regressions; no Telegram writes are performed."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl import types

from app.core.account.models import AccountOperationConfig, AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import automation as module
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, AutoJoinAttempt, GroupQualificationAudit
from app.modules.acquisition.qualification_service import SETTING_KEY


async def case(db):
    now = datetime.utcnow()
    account = TelegramAccount(identifier='strict-lifecycle', session_name='strict-lifecycle',
                              account_type=AccountType.PROMOTER, status=AccountStatus.ONLINE,
                              is_active=True, risk_level='normal')
    group = Group(group_id=900011, username='lifecycle_group', title='Lifecycle', status='pending')
    campaign = AdCampaign(name='Lifecycle', enabled=True, status='active')
    db.add_all([account, group, campaign])
    await db.flush()
    attempt = AutoJoinAttempt(account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
                              target_key='username:lifecycle_group', request_state='sent',
                              status='pending', telegram_action_attempted=True,
                              request_sent_at=now, attempted_at=now)
    log = AdDeliveryLog(account_id=account.id, group_id=group.id, group=group,
                        telegram_group_id=group.group_id, ad_campaign_id=campaign.id,
                        status='success', telegram_message_id=19, survival_status='pending',
                        survival_stage='two_minute', survival_check_due_at=now,
                        sent_at=now-timedelta(minutes=3))
    db.add_all([attempt, log])
    await db.commit()
    pool = SimpleNamespace(acquire_by_id=AsyncMock(return_value=object()), release=AsyncMock())
    service = AcquisitionAutomationService(db, account_pool=pool)
    service._sync_account_pool = AsyncMock()
    return service, account, group, attempt, log, now


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['joined', 'pending'])
async def test_join_membership_audit_attempt_commit_atomically_or_rollback(test_db, status):
    service, account, group, attempt, _, _ = await case(test_db)
    attempt_id = attempt.id
    member = await service._persist_join_qualification_result(group, attempt, status=status)
    assert member.ad_status == 'blocked'
    assert member.review_started_at is None
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1
    await test_db.rollback()
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 0
    assert await test_db.scalar(select(func.count(GroupAccountMembership.id))) == 0
    restored = await test_db.get(AutoJoinAttempt, attempt_id, populate_existing=True)
    assert restored.status == 'pending'
    assert restored.request_state == 'sent'


@pytest.mark.asyncio
async def test_join_approval_enqueues_new_scope_without_inline_external_actions(test_db):
    service, _, group, attempt, _, _ = await case(test_db)
    member = await service._persist_join_qualification_result(group, attempt, status='pending')
    await test_db.commit()
    await service._persist_join_qualification_result(group, attempt, status='joined')
    await test_db.commit()
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 2
    assert member.status == 'joined'
    assert attempt.status == 'success'
    assert attempt.reconciliation_status == 'confirmed'
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_read_failures_keep_automatic_reconciliation_and_original_target_lock(test_db):
    service, _, _, attempt, _, now = await case(test_db)
    for _ in range(3):
        await service._defer_join_reconciliation(attempt, now, 'read_failed', technical=True)
    assert attempt.reconciliation_status == 'pending'
    assert attempt.reconciliation_next_at > now
    assert attempt.request_state == 'sent'
    assert attempt.target_key == 'username:lifecycle_group'
    assert (await service.reconcile_join_requests())['checked'] == 0
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed, delay, manual', [(0, 900, False), (3, 7200, False), (48, 7200, False), (240, 7200, False)])
async def test_join_reconciliation_schedule_is_bounded(test_db, elapsed, delay, manual):
    service, _, _, attempt, _, now = await case(test_db)
    attempt.request_sent_at = now-timedelta(hours=elapsed)
    await service._defer_join_reconciliation(attempt, now, 'still_pending')
    assert attempt.reconciliation_status == ('manual_required' if manual else 'pending')
    assert attempt.reconciliation_next_at == (None if manual else now+timedelta(seconds=delay))


@pytest.mark.asyncio
async def test_survival_claim_has_one_owner_and_can_be_reclaimed_after_expiry(test_db):
    service, _, _, _, log, now = await case(test_db)
    first = await service._claim_survival_check(log.id, now)
    async with AsyncSession(test_db.bind, expire_on_commit=False) as other:
        second_service = AcquisitionAutomationService(other)
        assert await second_service._claim_survival_check(log.id, now) is None
        replacement = await second_service._claim_survival_check(log.id, now+timedelta(minutes=4))
    assert first[0] != replacement[0]
    assert replacement[1] > first[1]


@pytest.mark.asyncio
async def test_survival_stale_worker_cannot_advance_checkpoint(test_db, monkeypatch):
    service, _, _, _, log, now = await case(test_db)
    monkeypatch.setattr(module, '_now', lambda: now)
    async def replace_claim(*_args):
        await test_db.execute(update(AdDeliveryLog).where(AdDeliveryLog.id == log.id).values(
            survival_claim_token='another-worker', survival_version=AdDeliveryLog.survival_version+1))
        await test_db.commit()
        return {'exists': True, 'group_accessible': True, 'account_readable': True,
                'member': True, 'can_send': True, 'errors': []}
    service._inspect_ad_survival_facts = replace_claim
    assert await service._check_one_ad_survival(log, now) == 'stale_claim'
    await test_db.refresh(log)
    assert log.survival_stage == 'two_minute'
    assert log.survived_two_minute_at is None
    service.account_pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_survival_duplicate_check_does_not_advance_one_hour_at_two_minutes(test_db, monkeypatch):
    service, _, _, _, log, now = await case(test_db)
    monkeypatch.setattr(module, '_now', lambda: now)
    service._inspect_ad_survival_facts = AsyncMock(return_value={
        'exists': True, 'group_accessible': True, 'account_readable': True,
        'member': True, 'can_send': True, 'ttl_period': 0, 'errors': []})
    assert await service._check_one_ad_survival(log, now) == 'pending_one_hour'
    assert await service._check_one_ad_survival(log, now) == 'claim_unavailable'
    assert log.survived_one_hour_at is None
    service._inspect_ad_survival_facts.assert_awaited_once()
    assert log.survival_claim_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,seconds', [('two_minute', 120), ('one_hour', 3600), ('twenty_four_hour', 86400)])
async def test_corrupt_early_due_cannot_shorten_real_elapsed_deadline(test_db, monkeypatch, stage, seconds):
    service, _, _, _, log, now = await case(test_db)
    monkeypatch.setattr(module, '_now', lambda: now)
    log.sent_at = now-timedelta(seconds=seconds-1)
    log.survival_stage = stage
    await test_db.commit()
    assert await service._check_one_ad_survival(log, now) == 'not_due'
    assert log.survival_check_due_at == now+timedelta(seconds=1)
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.parametrize('facts,reason', [
    ({'errors': ['message_query:TimeoutError']}, 'survival_query_unknown'),
    ({'group_accessible': False}, 'survival_group_access_unknown'),
    ({'group_accessible': True, 'account_readable': True, 'member': False}, 'survival_account_or_membership_unavailable'),
    ({'group_accessible': True, 'account_readable': True, 'member': True, 'can_send': False}, 'survival_account_send_restricted'),
    ({'group_accessible': True, 'account_readable': True, 'member': True, 'can_send': True, 'exists': False, 'ttl_period': 60}, 'message_missing_auto_delete_possible'),
    ({'group_accessible': True, 'account_readable': True, 'member': True, 'can_send': True, 'exists': False, 'ttl_period': 0}, 'message_missing_cause_unconfirmed'),
])
def test_absence_never_claims_admin_deletion(facts, reason):
    now = datetime.utcnow()
    log = SimpleNamespace(sent_at=now-timedelta(hours=1))
    assert AcquisitionAutomationService._survival_unknown_reason(facts, log, now).startswith(reason)


@pytest.mark.asyncio
async def test_survival_error_releases_claim_and_pool_without_marking_deleted(test_db, monkeypatch):
    service, _, _, _, log, now = await case(test_db)
    monkeypatch.setattr(module, '_now', lambda: now)
    service._inspect_ad_survival_facts = AsyncMock(side_effect=TimeoutError())
    assert await service._check_one_ad_survival(log, now) == 'retry_scheduled'
    assert log.survival_status == 'pending'
    assert log.survival_claim_token is None
    service.account_pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_username_rebound_survival_does_not_query_other_groups_messages(test_db):
    service, _, _, _, log, _ = await case(test_db)
    client = SimpleNamespace(get_entity=AsyncMock(return_value=types.Channel(
        id=1234, title='wrong', photo=types.ChatPhotoEmpty(), date=datetime.utcnow(), megagroup=True)),
        get_messages=AsyncMock())
    facts = await service._inspect_ad_survival_facts(SimpleNamespace(client=client), log)
    assert facts['errors'] == ['group_identity_mismatch']
    client.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollout_missing_or_paused_cannot_use_approved_candidate(test_db):
    service, account, group, _, _, _ = await case(test_db)
    test_db.add(SystemSetting(key=SETTING_KEY, value=json.dumps({'enabled': True})))
    await test_db.commit()
    context, reason = await service._qualified_ad_context(account.id, group.group_id, datetime.utcnow())
    assert context is None
    assert reason == 'qualification_rollout_paused'


@pytest.mark.asyncio
async def test_dynamic_candidate_uses_latest_qualification_and_preserves_real_wait(test_db):
    service, account, group, _, _, now = await case(test_db)
    member = GroupAccountMembership(account_id=account.id, group_id=group.id,
        telegram_group_id=group.group_id, status='joined', joined_at=now,
        review_status='approved', ad_status='active', group=group,
        first_ad_allowed_at=now+timedelta(days=7), probe_status='not_started')
    config = AccountOperationConfig(account_id=account.id, dynamic_capacity_enabled=True)
    test_db.add_all([member, config])
    await test_db.flush()
    snapshot = {'account_id': account.id, 'group_id': group.id, 'telegram_group_id': group.group_id,
        'group_type': 'supergroup', 'policy_version': POLICY_VERSION, 'content_scope': 'text_profile',
        'permissions': {}}
    audit = GroupQualificationAudit(batch_id='test', membership_id=member.id, account_id=account.id,
        group_id=group.id, policy_version=POLICY_VERSION, content_scope='text_profile', state='completed',
        decision='trial', membership_joined_at=member.joined_at, checked_at=now,
        expires_at=now+timedelta(hours=24), evidence_json=json.dumps(snapshot))
    test_db.add_all([audit, SystemSetting(key=SETTING_KEY, value=json.dumps({'enabled': True,
        'rollout_accounts': {str(account.id): {'phase': 'pilot', 'started_at': now.isoformat(), 'max_groups': 1}}}))])
    await test_db.commit()
    candidates = await service._list_joined_groups_for_account(account.id)
    assert candidates == [member]
    from sqlalchemy import inspect
    assert "group" not in inspect(candidates[0]).unloaded
    assert candidates[0].group is group
    assert await service._ad_warmup_skip_reason(account.id, member, now, dry_run=False) is None
    snapshot['permissions']['temporary_until'] = (now+timedelta(minutes=5)).isoformat()
    audit.evidence_json = json.dumps(snapshot)
    await test_db.commit()
    assert await service._ad_warmup_skip_reason(account.id, member, now, dry_run=False) == 'qualification_wait_condition'
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('found', [True, False])
async def test_unknown_delivery_reconciles_only_original_id_and_never_releases_missing(test_db, monkeypatch, found):
    service, account, _, _, log, now = await case(test_db)
    from app.core.account.models import AccountOutboundAttempt
    log.status, log.sent_at, log.reservation_token = 'pending', None, 'original-token'
    log.survival_status, log.survival_stage, log.survival_check_due_at = 'not_required', 'complete', None
    log.error = 'send_outcome_unknown:post_send_audit_failed'
    log.created_at = now-timedelta(seconds=10)
    ledger = AccountOutboundAttempt(account_id=account.id, attempt_key='ad:original-token',
        category='ad', state='unknown', attempted_at=now-timedelta(seconds=5),
        target_key='group:900011')
    test_db.add(ledger)
    await test_db.commit()
    monkeypatch.setattr(module, '_now', lambda: now)
    service._inspect_ad_survival_facts = AsyncMock(return_value={
        'exists': found, 'is_own_message': True, 'group_accessible': True,
        'message_created_at': (now-timedelta(seconds=5)).isoformat(), 'errors': []})
    result = await service.check_ad_survival(limit=5)
    await test_db.refresh(log)
    await test_db.refresh(ledger)
    if found:
        assert result['send_reconciled'] == 1
        assert log.status == 'success'
        assert log.sent_at == now-timedelta(seconds=5)
        assert log.survival_stage == 'two_minute'
        assert log.survival_check_due_at == now+timedelta(seconds=115)
        assert ledger.state == 'succeeded'
        assert ledger.message_id == 19
    else:
        assert result['retry_scheduled'] == 1
        assert log.status == 'pending'
        assert ledger.state == 'unknown'
        assert log.telegram_message_id == 19
    service.account_pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_delivery_without_id_stays_blocked_and_is_not_guessed(test_db):
    service, _, _, _, log, _ = await case(test_db)
    log.status, log.telegram_message_id = 'pending', None
    log.error = 'send_outcome_unknown:timeout'
    await test_db.commit()
    assert (await service.check_ad_survival())['processed'] == 0
    assert log.status == 'pending'
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,cap,old_decision,old_phase,expected', [
    ('pilot', 1, 'allowed', 'pilot', 'qualification_pilot_group_cap'),
    ('pilot', 5, 'allowed', 'pilot', None),
    ('dynamic', 5, 'allowed', 'pilot', None),
])
async def test_pilot_limits_are_account_scoped_and_do_not_upgrade_phase(test_db, phase, cap, old_decision, old_phase, expected):
    service, account, _, _, log, now = await case(test_db)
    log.telegram_group_id = 901111
    log.created_at = now-timedelta(seconds=1)
    log.qualification_context_json = json.dumps({'decision': old_decision, 'rollout_phase': old_phase})
    await test_db.commit()
    context = {'account_id': account.id, 'decision': 'trial', 'rollout_phase': phase,
               'rollout_started_at': (now-timedelta(minutes=1)).isoformat(), 'pilot_max_groups': cap}
    assert await service._qualification_delivery_quota_reason(context, 999111, now) == expected
    assert context['rollout_phase'] == phase


@pytest.mark.asyncio
async def test_trial_can_repeat_across_accounts_after_24h_survival(test_db):
    service, account, group, _, log, now = await case(test_db)
    log.sent_at = now-timedelta(days=2)
    log.survival_status = 'survived'
    log.qualification_context_json = json.dumps({'decision': 'trial', 'rollout_phase': 'pilot'})
    await test_db.commit()
    context = {'account_id': account.id+1, 'decision': 'trial', 'rollout_phase': 'dynamic'}
    assert await service._qualification_delivery_quota_reason(context, -1000000900011, now) is None
    context['decision'] = 'allowed'
    assert await service._qualification_delivery_quota_reason(context, group.group_id, now) is None


@pytest.mark.asyncio
async def test_two_overlapping_survival_workers_issue_one_telegram_read(test_db, monkeypatch):
    import asyncio
    service, _, _, _, log, now = await case(test_db)
    monkeypatch.setattr(module, '_now', lambda: now)
    entered, release = asyncio.Event(), asyncio.Event()
    async def inspect(*_args):
        entered.set()
        await release.wait()
        return {'exists': True, 'group_accessible': True, 'account_readable': True,
                'member': True, 'can_send': True, 'errors': []}
    service._inspect_ad_survival_facts = AsyncMock(side_effect=inspect)
    first = asyncio.create_task(service._check_one_ad_survival(log, now))
    await entered.wait()
    async with AsyncSession(test_db.bind, expire_on_commit=False) as other:
        second = AcquisitionAutomationService(other, account_pool=service.account_pool)
        assert await second._check_one_ad_survival(log, now) == 'claim_unavailable'
    release.set()
    assert await first == 'pending_one_hour'
    service._inspect_ad_survival_facts.assert_awaited_once()
    service.account_pool.acquire_by_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_dynamic_creatives_do_not_generate_or_silently_change_reviewed_form(test_db):
    service, account, _, _, _, _ = await case(test_db)
    from app.modules.acquisition.models import AdCreative
    valid = AdCreative(id=1, name='profile text', content='需要 AI 订阅服务可看我的简介', enabled=True, weight=1)
    link = AdCreative(id=2, name='link', content='服务 https://example.com', enabled=True, weight=1)
    media = AdCreative(id=3, name='image', content='服务', media_url='https://example.com/img', enabled=True, weight=1)
    service._dynamic_qualification_mode = AsyncMock(return_value=True)
    service._creative_pool_for_binding = AsyncMock(return_value=[link, media, valid])
    service._generate_and_bind_ad_creatives = AsyncMock()
    binding = SimpleNamespace(account_id=account.id)
    assert await service._choose_delivery_creative(binding, 999) is valid
    service._creative_pool_for_binding.return_value = [link, media]
    assert await service._choose_delivery_creative(binding, 999) is None
    service._generate_and_bind_ad_creatives.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [False, True])
async def test_strict_join_entry_never_inline_assesses_leaves_or_probes(test_db, pending):
    from unittest.mock import Mock
    service, account, group, attempt, _, _ = await case(test_db)
    group.status = 'pending_join'
    test_db.add(SystemSetting(key=SETTING_KEY, value=json.dumps({'enabled': True})))
    await test_db.commit()
    service._joined_membership_account_id_for_group = AsyncMock(return_value=None)
    service.dynamic_frequency.join_candidate_decision = AsyncMock(return_value={'allowed': True})
    service.join_budget.reserve = AsyncMock(return_value=attempt)
    service._join_group = AsyncMock(side_effect=RuntimeError('InviteRequestSentError') if pending else None)
    service._schedule_next_join = Mock()
    service._evaluate_joined_group = AsyncMock(side_effect=AssertionError('inline review'))
    service._leave_group = AsyncMock(side_effect=AssertionError('inline leave'))
    service._send_ad_probe = AsyncMock(side_effect=AssertionError('inline probe'))
    result = await service._attempt_join_queued_group(SimpleNamespace(account=account, account_id=account.id), group, dry_run=False)
    assert result.succeeded == (0 if pending else 1)
    member = await test_db.scalar(select(GroupAccountMembership))
    audit = await test_db.scalar(select(GroupQualificationAudit))
    assert member.status == ('pending' if pending else 'joined')
    assert audit.state == ('waiting_membership' if pending else 'queued')
    service._evaluate_joined_group.assert_not_awaited()
    service._leave_group.assert_not_awaited()
    service._send_ad_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewed_membership_gets_new_join_version(test_db):
    service, account, group, attempt, _, now = await case(test_db)
    member = GroupAccountMembership(account_id=account.id, group_id=group.id,
        telegram_group_id=group.group_id, status='left', joined_at=now-timedelta(days=20),
        left_at=now-timedelta(days=5), ad_status='blocked', warmup_status='blocked', probe_status='skipped')
    test_db.add(member)
    await test_db.commit()
    renewed = await service._persist_join_qualification_result(group, attempt, status='joined')
    assert renewed.joined_at >= now
    assert renewed.left_at is None
    audit = await test_db.scalar(select(GroupQualificationAudit))
    assert audit.membership_joined_at == renewed.joined_at


@pytest.mark.asyncio
async def test_original_join_reservation_key_reaches_execution_feedback(test_db):
    service, account, group, attempt, _, _ = await case(test_db)
    attempt.reservation_key = 'durable-original-join-key'
    await test_db.commit()
    service.telegram_execution.join_group = AsyncMock()
    await service._join_group(account.id, service._group_to_discovered(group), reservation=attempt)
    kwargs = service.telegram_execution.join_group.await_args.kwargs
    assert kwargs['join_reservation_key'] == 'durable-original-join-key'
    assert callable(kwargs['on_join_request_attempted'])
