"""Business authorization survives scheduling; stale reads never publish."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.models import GroupQualificationAudit, QualificationActiveGrant, QualificationWork
from app.modules.acquisition.qualification_events import pending, request
from tests.unit.test_qualification_service import setup


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


async def baseline(db):
    account, group, member, audit = await setup(db)
    work = await engine.bootstrap_member(db, member)
    await db.commit()
    assert work.state == "DONE"
    return account, group, member, audit, work


def clone(audit, member, *, decision="allowed"):
    now = datetime.utcnow()
    evidence = json.loads(audit.evidence_json)
    evidence.update(decision=decision, authorization_mode="events")
    return GroupQualificationAudit(
        batch_id="engine-final-" + now.isoformat(), membership_id=member.id,
        account_id=member.account_id, group_id=member.group_id,
        membership_joined_at=member.joined_at, policy_version=audit.policy_version,
        content_scope="text_profile", state="completed", decision=decision,
        checked_at=now, expires_at=now+timedelta(days=1), evidence_json=json.dumps(evidence),
    )


async def run_owner_reviews(service, *, account_id, limit=1):
    from app.core.account.session_exec import _execute_in_actor
    async def execute(identity, stage):
        return await engine.run_reviews(service, account_id=identity, limit=limit), 30
    result, _ = await _execute_in_actor(execute, account_id, "review")
    return result


def test_rollout_requires_valid_explicit_accounts(monkeypatch):
    assert engine.enabled_for(2) and not engine.enabled_for(3)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2,all")
    assert not engine.enabled_for(2)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "")
    assert not engine.enabled_for(2)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", False)
    assert not engine.enabled_for(2)


@pytest.mark.asyncio
async def test_missing_pointer_never_uses_legacy_authorization(test_db):
    account, group, member, old = await setup(test_db)
    row, _, _ = await qualification.current_authorization(test_db, account.id, group.group_id)
    assert row is None
    await engine.bootstrap_member(test_db, member)
    row, _, _ = await qualification.current_authorization(test_db, account.id, group.group_id)
    assert row.id == old.id


@pytest.mark.asyncio
async def test_background_wait_preserves_grant_and_final_audit(test_db):
    account, _, member, old, work = await baseline(test_db)
    original = (old.evidence_json, old.checked_at, old.state, old.next_retry_at)
    await engine.enqueue(test_db, member, reason="manual")
    claim = await engine.claim_next(test_db, account.id)
    assert claim
    assert await engine.defer(test_db, claim, snapshot={"efficiency_v1": True}, reason="telegram_read_budget")
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is old
    assert (old.evidence_json, old.checked_at, old.state, old.next_retry_at) == original
    assert work.state == "WAIT_BUDGET"
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1


@pytest.mark.asyncio
async def test_event_replay_merges_but_newer_event_fences_old_claim(test_db):
    account, _, member, old, work = await baseline(test_db)
    original = old.evidence_json
    await request(test_db, member, "rules", message_ids=[10], source_token="edit1")
    revision = work.revision
    await request(test_db, member, "rules", message_ids=[10], source_token="edit1")
    assert work.revision == revision
    first = await engine.claim_next(test_db, account.id)
    await test_db.commit()
    assert first
    await request(test_db, member, "rules", message_ids=[11], source_token="edit2")
    current_event = await pending(test_db, member)
    assert not await engine.on_decision(test_db, member, clone(old, member), claim=first)
    await test_db.commit()
    assert await pending(test_db, member) == current_event
    assert old.evidence_json == original
    assert await test_db.scalar(select(func.count(QualificationWork.id)).where(QualificationWork.family == "qualification")) == 1
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1
    assert await engine.active_authorization(test_db, member) is None


@pytest.mark.asyncio
async def test_final_decision_appends_and_swaps_pointer_with_event_ack(test_db):
    account, _, member, old, work = await baseline(test_db)
    original = old.evidence_json
    await request(test_db, member, "membership", source_token="permissions-changed")
    claim = await engine.claim_next(test_db, account.id)
    await test_db.commit()
    final = clone(old, member)
    final.evidence_json = json.dumps({**json.loads(final.evidence_json), "collection_event": claim.event})
    assert await engine.on_decision(test_db, member, final, claim=claim)
    await test_db.commit()
    assert await pending(test_db, member) is None
    assert (await engine.active_authorization(test_db, member)).id == final.id
    assert final.id != old.id and old.evidence_json == original
    assert work.state == "DONE"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["incarnation", "profile", "policy"])
async def test_scope_change_immediately_denies_old_pointer(test_db, change):
    account, _, member, _, _ = await baseline(test_db)
    if change == "incarnation":
        member.joined_at += timedelta(seconds=1)
    elif change == "profile":
        account.profile_bio = "new profile material"
    else:
        from app.core.settings_models import SystemSetting
        setting = await test_db.get(SystemSetting, qualification.SETTING_KEY)
        setting.value = json.dumps({**json.loads(setting.value), "manual_protected_group_ids": [member.group_id]})
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is None


@pytest.mark.parametrize("key,current,unrelated", [
    ("manual_protected_group_ids", 40, 41),
    ("protected_telegram_group_ids", -1001234567890, -1001234567891),
    ("protected_membership_ids", 60, 61),
])
def test_protection_scope_changes_only_for_the_current_target(key, current, unrelated):
    member = SimpleNamespace(account_id=2, id=60, group_id=40,
        telegram_group_id=1234567890, joined_at=datetime(2026, 1, 1))
    original = engine._scope_key(member, {}, "profile")
    assert engine._scope_key(member, {key: [unrelated]}, "profile") == original
    protected = engine._scope_key(member, {key: [current]}, "profile")
    assert protected != original
    assert engine._scope_key(member, {key: [unrelated, current]}, "profile") == protected
    if key == "protected_telegram_group_ids":
        assert engine._scope_key(member, {key: [member.telegram_group_id]}, "profile") == protected


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"execute_reviews": False}, {"execute_exits": False}, {"enabled": False},
    {"account_ids": [99], "promotion_account_ids": [], "exit_all_accounts": False},
    {"rollout_accounts": {"2": {"phase": "paused"}}, "min_member_count": 500},
])
async def test_operation_switches_do_not_revoke_existing_grant(test_db, changes):
    from app.core.settings_models import SystemSetting
    account, _, member, audit, work = await baseline(test_db)
    setting = await test_db.get(SystemSetting, qualification.SETTING_KEY)
    setting.value = json.dumps({**json.loads(setting.value), **changes})
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is audit
    assert await engine.bootstrap_member(test_db, member) is work
    assert work.state == "DONE"
    runtime = await engine.runtime_for_memberships(test_db, [member])
    assert runtime[member.id]["authorization"]["state"] == "active"


@pytest.mark.asyncio
async def test_new_unmapped_promoter_invalidates_identity_coverage_locally(test_db):
    from app.core.account.models import AccountStatus, TelegramAccount
    _, _, member, _, _ = await baseline(test_db)
    test_db.add(TelegramAccount(id=99, identifier="new-promoter", session_name="new-promoter",
        status=AccountStatus.OFFLINE, is_active=False))
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is None
    assert (await engine.runtime_for_memberships(test_db, [member]))[member.id]["authorization"]["state"] == "invalidated"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["paused", "held", "left", "incarnation", "scope"])
async def test_33rd_work_is_claimed_after_unexecutable_queue_head(test_db, blocked):
    from app.core.group.models import Group, GroupAccountMembership
    account, _, _, _, _ = await baseline(test_db)
    rows = []
    for index in range(33):
        group = Group(id=1000+index, group_id=2200000000+index, title="queue fixture")
        member = GroupAccountMembership(id=1000+index, account_id=account.id, group_id=group.id,
            telegram_group_id=group.group_id, joined_at=datetime.utcnow()-timedelta(days=1),
            status="joined", ad_status="warming", review_status="pending")
        test_db.add_all([group, member])
        await test_db.flush()
        work = await engine.enqueue(test_db, member, initial_collection_pending=True)
        rows.append((member, work))
        if index < 32:
            if blocked == "paused":
                member.ad_status = "paused"
            elif blocked == "held":
                member.review_status = "manual_required"
            elif blocked == "left":
                member.status, member.left_at = "left", datetime.utcnow()
                member.ad_status = "blocked"
            elif blocked == "incarnation":
                member.joined_at += timedelta(seconds=1)
            else:
                work.scope_key = "obsolete-scope-"+str(index)
    await test_db.commit()
    assert await engine.initial_slots(test_db, account.id) == 1
    claim = await engine.claim_next(test_db, account.id)
    assert claim and claim.work_id == rows[-1][1].id
    if blocked == "scope":
        assert all(work.state == "STALE" and not work.initial_collection_pending for _, work in rows[:32])


@pytest.mark.asyncio
async def test_scope_change_retires_old_initial_slot_before_new_work(test_db):
    account, _, member, _, old = await baseline(test_db)
    old.state, old.initial_collection_pending = "WAIT_BUDGET", True
    account.profile_bio = "changed scope"
    await test_db.flush()
    assert await engine.initial_slots(test_db, account.id) == 0
    fresh = await engine.enqueue(test_db, member, reason="scope_changed", initial_collection_pending=True)
    assert fresh.id != old.id and old.state == "STALE" and not old.initial_collection_pending
    assert await engine.initial_slots(test_db, account.id) == 1


@pytest.mark.asyncio
async def test_repeated_account_bootstrap_batches_unchanged_members_without_audit_reads(test_db):
    from sqlalchemy import event
    from app.core.group.models import Group, GroupAccountMembership
    account, _, member, _, work = await baseline(test_db)
    for index in range(12):
        group = Group(id=2000+index, group_id=2300000000+index, title="bootstrap fixture")
        relation = GroupAccountMembership(id=2000+index, account_id=account.id, group_id=group.id,
            telegram_group_id=group.group_id, joined_at=member.joined_at, status="joined",
            ad_status="warming", review_status="pending")
        test_db.add_all([group, relation])
        await test_db.flush()
        current = await engine.enqueue(test_db, relation)
        current.state, current.ready_at = "DONE", None
    work.state, work.ready_at = "WAIT_MATURITY", datetime.utcnow()+timedelta(hours=1)
    await test_db.commit()
    config = await qualification.policy(test_db, fresh=True)
    statements = []
    def collect(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(test_db.bind.sync_engine, "before_cursor_execute", collect)
    try:
        assert await engine.bootstrap_account(test_db, account.id, config=config) == 0
        assert await engine.bootstrap_account(test_db, account.id, config=config) == 0
    finally:
        event.remove(test_db.bind.sync_engine, "before_cursor_execute", collect)
    assert len(statements) == 8  # Four queries for 13 members, on either pass.
    assert not any("group_qualification_audit" in statement for statement in statements)
    assert work.state == "WAIT_MATURITY" and await engine.claim_next(test_db, account.id) is None
    work.ready_at = datetime.utcnow()-timedelta(seconds=1)
    await test_db.flush()
    assert await engine.bootstrap_account(test_db, account.id, config=config) == 0
    assert (await engine.claim_next(test_db, account.id)).work_id == work.id


@pytest.mark.asyncio
async def test_batch_bootstrap_only_adopts_missing_work_and_queues_new_event(test_db):
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.qualification_events import key
    account, _, member, _, work = await baseline(test_db)
    # An already known event version must not consume the limit before later work.
    test_db.add(SystemSetting(key=key(member.id), value=json.dumps({
        "joined_at": member.joined_at.isoformat(), "kinds": ["membership"],
        "token": "new-permissions", "message_ids": [],
    })))
    await test_db.commit()
    assert await engine.queue_pending_events(test_db, account.id, limit=1) == 1
    revision = work.revision
    assert await engine.queue_pending_events(test_db, account.id, limit=1) == 0
    assert work.revision == revision and work.state == "READY"
    # Scope changes still create/adopt the affected target, not all current work.
    account.profile_bio = "new profile"
    await test_db.flush()
    assert await engine.bootstrap_account(test_db, account.id) == 1
    assert work.state == "STALE"


@pytest.mark.asyncio
async def test_initial_slots_ignore_migration_and_release_provider_wait(test_db):
    account, _, member, _, work = await baseline(test_db)
    assert await engine.initial_slots(test_db, account.id) == 0
    work.state, work.initial_collection_pending = "READY", True
    await test_db.flush()
    assert await engine.initial_slots(test_db, account.id) == 1
    claim = await engine.claim_next(test_db, account.id)
    await engine.defer(test_db, claim, snapshot={"ai_pending": True, "collection_complete": True}, reason="provider_pending")
    await test_db.flush()
    assert work.state == "WAIT_PROVIDER"
    assert await engine.initial_slots(test_db, account.id) == 0


def test_partial_discovery_continues_but_terminal_discovery_parks():
    now = datetime.utcnow()
    assert engine._wait({"efficiency_v1": True, "collection_progress": {"history_cursors": [{"complete": False}]}}, "missing_facts", now)[0] == "READY"
    assert engine._wait({"efficiency_v1": True, "discovery_exhausted": True}, "missing_facts", now) == ("WAIT_EVENT", None)


@pytest.mark.asyncio
async def test_runtime_uses_audit_membership_id_and_never_enqueues(test_db):
    _, _, member, audit, work = await baseline(test_db)
    runtime = await engine.runtime_for_memberships(test_db, [audit])
    assert runtime[member.id]["authorization"]["grant_audit_id"] == audit.id
    assert runtime[member.id]["job"]["id"] == work.id
    assert await test_db.scalar(select(func.count(QualificationWork.id)).where(QualificationWork.family == "qualification")) == 1


@pytest.mark.asyncio
async def test_group_peer_change_invalidates_single_and_batch_authorization(test_db):
    _, group, member, _, _ = await baseline(test_db)
    group.group_id += 1
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is None
    runtime = await engine.runtime_for_memberships(test_db, [member])
    assert runtime[member.id]["authorization"]["state"] == "invalidated"


@pytest.mark.asyncio
async def test_runtime_selects_current_scope_work_after_profile_rollback(test_db):
    account, _, member, _, original = await baseline(test_db)
    account.profile_bio = "temporary profile scope"
    await test_db.flush()
    newer = await engine.enqueue(test_db, member, reason="scope_changed")
    account.profile_bio = None
    await test_db.commit()
    runtime = await engine.runtime_for_memberships(test_db, [member])
    assert newer.id > original.id
    assert runtime[member.id]["job"]["id"] == original.id


@pytest.mark.asyncio
async def test_soft_candidate_event_does_not_block_live_send(test_db):
    from app.modules.acquisition.qualification_actions import validate_live_send
    account, group, member, old, work = await baseline(test_db)
    await request(test_db, member, "evidence_candidate", message_ids=[999], source_token="candidate")
    assert await engine.active_authorization(test_db, member) is old
    await validate_live_send(test_db, None, account.id, group.group_id)


@pytest.mark.asyncio
async def test_real_runner_deferral_does_not_append_technical_audit(test_db, monkeypatch):
    account, _, member, old, work = await baseline(test_db)
    await engine.enqueue(test_db, member, reason="manual")
    original = old.evidence_json
    async def assess(_service, _account_id, _group, *, row, **_kwargs):
        assert row is not old and row.id is None
        row.decision, row.reason = "technical_wait", "telegram_read_budget"
        row.evidence_json = json.dumps({"efficiency_v1": True, "collection_progress": {"pending_message_ids": [90], "history_cursors": [{"offset_id": 80}]}})
        return SimpleNamespace(passed=False, reason="telegram_read_budget")
    monkeypatch.setattr(qualification, "assess", assess)
    service = SimpleNamespace(db=test_db, _sync_group_ad_policy_from_audit=AsyncMock())
    result = await run_owner_reviews(service, account_id=account.id)
    assert result["processed"] == 1
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1
    assert old.evidence_json == original and work.state == "WAIT_BUDGET"
    assert json.loads(work.progress_json)["collection_progress"]["history_cursors"] == [{"offset_id": 80}]


@pytest.mark.asyncio
async def test_maturity_observation_preserves_existing_grant(test_db):
    account, _, member, old, _ = await baseline(test_db)
    await engine.enqueue(test_db, member, reason="manual")
    claim = await engine.claim_next(test_db, account.id)
    observed = clone(old, member, decision="observe")
    evidence = json.loads(observed.evidence_json)
    evidence["next_evidence_maturity_at"] = (datetime.utcnow()+timedelta(hours=8)).isoformat()
    observed.evidence_json = json.dumps(evidence)
    assert await engine.on_decision(test_db, member, observed, claim=claim)
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is old
    assert (member.review_status, member.ad_status) == ("approved", "active")


@pytest.mark.asyncio
async def test_actual_light_renewal_releases_db_and_appends_final_audit(test_db):
    from tests.unit.test_qualification_short_transactions_r10 import OfflineClient
    account, group, member, old, work = await baseline(test_db)
    # Complete the existing fixture's semantic grant without altering its scope.
    snapshot = json.loads(old.evidence_json)
    snapshot["advertising_audit"] = {"ad_allowed": True, "policy_mode": "soft_ad_allowed"}
    old.evidence_json = json.dumps(snapshot)
    work.progress_json = old.evidence_json
    await test_db.commit()
    await request(test_db, member, "membership", source_token="fresh-member-rights")
    claim = await engine.claim_next(test_db, account.id)
    draft = await engine._draft(test_db, claim)
    await test_db.commit()
    original = old.evidence_json
    client = OfflineClient(test_db, group)
    wrapper = SimpleNamespace(client=client)
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock())
    service = SimpleNamespace(db=test_db, account_pool=pool)
    try:
        result = await engine.try_light_renewal(service, account, group, member, draft, json.loads(draft.evidence_json), force_refresh=False)
    finally:
        client.close()
    assert result and result.passed
    assert client.reads == 3
    assert await engine.on_decision(test_db, member, draft, claim=claim)
    await test_db.commit()
    assert old.evidence_json == original and draft.id != old.id
    assert (await engine.active_authorization(test_db, member)).id == draft.id


@pytest.mark.asyncio
async def test_actual_light_renewal_does_not_publish_after_concurrent_event(test_db):
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.core.group.models import GroupAccountMembership
    from tests.unit.test_qualification_short_transactions_r10 import OfflineClient
    account, group, member, old, work = await baseline(test_db)
    original = json.loads(old.evidence_json)
    original["advertising_audit"] = {"ad_allowed": True, "policy_mode": "soft_ad_allowed"}
    old.evidence_json = json.dumps(original)
    work.progress_json = old.evidence_json
    await test_db.commit()
    await request(test_db, member, "membership", source_token="first-event")
    claim = await engine.claim_next(test_db, account.id)
    draft = await engine._draft(test_db, claim)
    await test_db.commit()
    async def newer_fact():
        async with AsyncSession(test_db.bind, expire_on_commit=False) as concurrent:
            relation = await concurrent.get(GroupAccountMembership, member.id)
            await request(concurrent, relation, "rules", message_ids=[999], source_token="newer-event")
            await concurrent.commit()
    client = OfflineClient(test_db, group, during_read=newer_fact)
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=SimpleNamespace(client=client)), release=AsyncMock())
    try:
        result = await engine.try_light_renewal(SimpleNamespace(db=test_db, account_pool=pool), account, group, member, draft, json.loads(draft.evidence_json), force_refresh=False)
    finally:
        client.close()
    assert result and result.passed
    assert not await engine.on_decision(test_db, member, draft, claim=claim)
    await test_db.rollback()
    # Read the persisted event after rollback; the later fact was not consumed.
    relation = await test_db.get(GroupAccountMembership, claim.membership_id)
    assert 999 in (await pending(test_db, relation))["message_ids"]
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1


@pytest.mark.asyncio
async def test_actual_assess_provider_wait_never_acquires_telegram(test_db, monkeypatch):
    from app.modules.acquisition.automation import GroupAdRulesAuditResult
    from app.modules.acquisition import qualification_ai
    account, group, member, old, work = await baseline(test_db)
    await engine.enqueue(test_db, member, reason="manual")
    material = json.loads(old.evidence_json)
    material.update(ai_pending=True, ai_final=False, quality_reason="evidence_sufficient", quality_status="qualified",
                    collection_complete=False, collected_at=datetime.utcnow().isoformat(),
                    collection_event=None, decision="technical_wait",
                    evidence=[{"source": "full_about", "text": "欢迎讨论技术", "sender_role": "admin"}])
    work.progress_json = json.dumps(material)
    claim = await engine.claim_next(test_db, account.id)
    draft = await engine._draft(test_db, claim)
    await test_db.commit()
    async def provider(_service, snapshot, _account, result, _limits, **_options):
        snapshot.update(ai_pending=True, ai_request_started=False, ai_final=False)
        return GroupAdRulesAuditResult(reason="group_rules_ai_capacity_unavailable", evidence=snapshot["evidence"])
    provider_mock = AsyncMock(side_effect=provider)
    monkeypatch.setattr(qualification_ai, "review_semantics", provider_mock)
    pool = SimpleNamespace(add_account_from_db=AsyncMock(side_effect=AssertionError("provider wait acquired Telegram")))
    service = SimpleNamespace(db=test_db, account_pool=pool,
        _join_review_account_block_reason=lambda *_args: None,
        _evaluate_group_ad_rules=lambda evidence: GroupAdRulesAuditResult(evidence=evidence))
    result = await qualification.assess(service, account.id, group, row=draft)
    assert not result.passed
    provider_mock.assert_awaited_once()
    pool.add_account_from_db.assert_not_awaited()
    assert json.loads(draft.evidence_json)["ai_pending"] is True
    await engine.defer(test_db, claim, snapshot=json.loads(draft.evidence_json), reason=draft.reason, draft=draft)
    await test_db.commit()
    assert work.state == "WAIT_PROVIDER"
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1


@pytest.mark.asyncio
async def test_queued_legacy_conclusion_is_adopted_without_telegram(test_db):
    account, group, member, old = await setup(test_db)
    checked, original = old.checked_at, old.evidence_json
    old.state, old.next_retry_at = "queued", datetime.utcnow()
    await test_db.commit()
    work = await engine.bootstrap_member(test_db, member)
    await test_db.commit()
    active = await engine.active_authorization(test_db, member)
    assert work.state == "DONE"
    assert active.id != old.id and active.state == "completed"
    assert active.checked_at == checked
    assert json.loads(active.evidence_json)["adopted_from_audit_id"] == old.id
    assert old.state == "queued" and old.evidence_json == original
    assert (await qualification.current_authorization(test_db, account.id, group.group_id))[0].id == active.id


@pytest.mark.asyncio
async def test_enqueue_wakes_only_after_commit_and_rollback_discards_wake(test_db):
    account, _, member, _, _ = await baseline(test_db)
    engine._publish_wakes.reset_mock()
    await engine.enqueue(test_db, member, reason="manual")
    engine._publish_wakes.assert_not_awaited()
    await test_db.rollback()
    engine._publish_wakes.assert_not_awaited()
    from app.core.group.models import GroupAccountMembership
    relation = await test_db.get(GroupAccountMembership, 60)
    await engine.enqueue(test_db, relation, reason="manual")
    await test_db.commit()
    engine._publish_wakes.assert_awaited_once()
    assert 2 in engine._publish_wakes.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [True, False])
async def test_business_maintenance_consumes_only_fresh_verified_metadata(test_db, fresh):
    from app.core.settings_models import SystemSetting
    account, group, member, old, _ = await baseline(test_db)
    original = old.evidence_json
    observed = datetime.utcnow()-timedelta(minutes=0 if fresh else 10)
    test_db.add(SystemSetting(key="group.collection."+str(group.group_id), value=json.dumps({
        "member_count": 30, "member_count_checked_at": observed.isoformat(), "member_count_source": "metadata:verified",
    })))
    await test_db.commit()
    service = SimpleNamespace(db=test_db, account_pool=SimpleNamespace(add_account_from_db=AsyncMock(side_effect=AssertionError("maintenance read Telegram"))))
    outcome = await engine.run_business_maintenance(service, account_id=account.id)
    assert outcome["processed"] == 1
    assert old.evidence_json == original
    if fresh:
        assert member.review_status == "exit_pending"
        decision = await engine.business_decision(test_db, member)
        assert decision and decision.reason == "members_below_50"
        assert await qualification.authorize_leave(test_db, account.id, group, member) == (True, "members_below_50")
        assert await engine.active_authorization(test_db, member) is None
    else:
        assert member.review_status == "approved"
        assert await engine.business_decision(test_db, member) is None
        assert await engine.active_authorization(test_db, member) is old
        assert outcome["results"][0]["state"] == "WAIT_EVENT"
    service.account_pool.add_account_from_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_maintenance_respects_existing_exit_pause(test_db):
    from app.core.settings_models import SystemSetting
    account, group, member, old = await setup(test_db)
    setting = await test_db.get(SystemSetting, qualification.SETTING_KEY)
    setting.value = json.dumps({**json.loads(setting.value), "execute_exits": False})
    await test_db.commit()
    await engine.bootstrap_member(test_db, member)
    test_db.add(SystemSetting(key="group.collection."+str(group.group_id), value=json.dumps({
        "member_count": 30, "member_count_checked_at": datetime.utcnow().isoformat(),
    })))
    await test_db.commit()
    await engine.run_business_maintenance(SimpleNamespace(db=test_db), account_id=account.id)
    assert member.review_status == "approved"
    assert await engine.active_authorization(test_db, member) is old
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1


@pytest.mark.asyncio
async def test_account_gap_cannot_restore_old_grant_after_maturity_observation(test_db):
    from app.modules.acquisition.qualification_events import account_gap
    account, _, member, old, _ = await baseline(test_db)
    await account_gap(test_db, account.id)
    await engine.queue_pending_events(test_db, account.id)
    claim = await engine.claim_next(test_db, account.id)
    assert claim and "gap" in claim.event["kinds"]
    observed = clone(old, member, decision="observe")
    observed.evidence_json = json.dumps({**json.loads(observed.evidence_json),
        "collection_event": claim.event, "next_evidence_maturity_at": (datetime.utcnow()+timedelta(hours=8)).isoformat()})
    assert await engine.on_decision(test_db, member, observed, claim=claim)
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is None
    assert await pending(test_db, member) is not None
    grant = await test_db.get(QualificationActiveGrant, member.id)
    assert grant.audit_id == old.id and grant.invalidated_at is not None
