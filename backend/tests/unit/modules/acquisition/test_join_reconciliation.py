"""Database-backed regression tests for approval result reconciliation."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.account.models import AccountType, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    JoinedGroupAuditResult,
    JoinVerificationSettings,
)
from app.modules.acquisition.join_reconciliation import reconcile_joined_auto_join_attempts
from app.modules.acquisition.models import AutoJoinAttempt


async def make_case(db, *, number=1, member_status="joined", note=None):
    submitted = datetime(2026, 9, 12, 1, 0)
    checked = submitted + timedelta(minutes=5)
    account = TelegramAccount(
        identifier=f"reconcile-{number}",
        session_name=f"reconcile-{number}",
        account_type=AccountType.PROMOTER,
    )
    group = Group(
        group_id=90000 + number,
        title=f"Join approval {number}",
        status="active" if member_status == "joined" else "pending",
        discovery_source="auto_keyword_search",
    )
    db.add_all([account, group])
    await db.flush()
    membership = GroupAccountMembership(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        status=member_status,
        join_method="auto_keyword_search",
        joined_at=submitted + timedelta(seconds=1),
        last_checked_at=checked,
        ad_status="warming" if member_status == "joined" else "blocked",
        note=json.dumps({"passed": True}) if note is None else note,
    )
    attempt = AutoJoinAttempt(
        account_id=account.id,
        group_id=group.id,
        status="pending",
        reason="join_request_pending",
        error="Join request submitted",
        telegram_action_attempted=True,
        attempted_at=submitted,
    )
    db.add_all([membership, attempt])
    await db.commit()
    return attempt, membership, group


@pytest.mark.asyncio
async def test_preview_then_reconcile_is_idempotent_and_preserves_request_budget(test_db):
    attempt, membership, _ = await make_case(test_db)
    submitted = attempt.attempted_at

    preview = await reconcile_joined_auto_join_attempts(test_db, dry_run=True)
    await test_db.refresh(attempt)
    assert preview["updated"] == 1
    assert attempt.status == "pending"
    assert attempt.joined_at is None

    result = await reconcile_joined_auto_join_attempts(test_db)
    await test_db.refresh(attempt)
    assert result["updated"] == 1
    assert attempt.status == "success"
    assert attempt.reason is None
    assert attempt.error is None
    assert attempt.attempted_at == submitted
    assert attempt.telegram_action_attempted is True
    assert attempt.joined_at == membership.last_checked_at
    assert attempt.joined_at != membership.joined_at
    assert (await test_db.execute(select(func.count(AutoJoinAttempt.id)))).scalar() == 1
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "left", "banned", "rejected"])
async def test_unconfirmed_or_departed_memberships_are_not_success(test_db, status):
    attempt, _, _ = await make_case(test_db, member_status=status)
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0
    await test_db.refresh(attempt)
    assert attempt.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("telegram_action_attempted", False),
        ("reason", "unrecognized_pending_reason"),
        ("status", "failed"),
    ],
)
async def test_only_real_pending_approval_or_verification_requests_are_completed(
    test_db, field, value
):
    attempt, _, _ = await make_case(test_db)
    setattr(attempt, field, value)
    await test_db.commit()
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["", "not json", "[]", '{"passed": false}', '{"passed": "true"}'])
async def test_successful_audit_evidence_is_required(test_db, note):
    await make_case(test_db, note=note)
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0


@pytest.mark.asyncio
async def test_old_membership_audit_cannot_confirm_a_new_request(test_db):
    attempt, membership, _ = await make_case(test_db)
    membership.last_checked_at = attempt.attempted_at - timedelta(seconds=1)
    await test_db.commit()
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0


@pytest.mark.asyncio
async def test_other_account_membership_cannot_confirm_this_request(test_db):
    attempt, membership, _ = await make_case(test_db)
    other = TelegramAccount(
        identifier="other", session_name="other", account_type=AccountType.PROMOTER
    )
    test_db.add(other)
    await test_db.flush()
    membership.account_id = other.id
    await test_db.commit()
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0
    await test_db.refresh(attempt)
    assert attempt.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_was_real", [True, False])
async def test_newer_real_request_supersedes_old_pending_but_cooldown_skip_does_not(
    test_db, newer_was_real
):
    attempt, _, _ = await make_case(test_db)
    newer = AutoJoinAttempt(
        account_id=attempt.account_id,
        group_id=attempt.group_id,
        status="failed" if newer_was_real else "skipped",
        reason="join_failed" if newer_was_real else "risk_guard_blocked:join_cooldown",
        telegram_action_attempted=newer_was_real,
        attempted_at=attempt.attempted_at + timedelta(minutes=1),
    )
    test_db.add(newer)
    await test_db.commit()
    result = await reconcile_joined_auto_join_attempts(test_db)
    assert result["updated"] == (0 if newer_was_real else 1)


@pytest.mark.asyncio
async def test_existing_join_timestamp_is_preserved_for_pending_verification(test_db):
    attempt, _, _ = await make_case(test_db)
    attempt.reason = "verification_pending_recheck"
    attempt.joined_at = attempt.attempted_at
    expected = attempt.joined_at
    await test_db.commit()
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 1
    await test_db.refresh(attempt)
    assert attempt.joined_at == expected


@pytest.mark.asyncio
async def test_invalid_older_note_does_not_starve_later_valid_approval(test_db):
    await make_case(test_db, number=1, note="invalid")
    attempt, _, _ = await make_case(test_db, number=2)
    result = await reconcile_joined_auto_join_attempts(test_db, limit=1)
    assert result["checked"] == 2
    assert result["updated"] == 1
    assert result["details"][0]["attempt_id"] == attempt.id


@pytest.mark.asyncio
async def test_sync_marks_newly_approved_attempt_success_without_another_join(test_db):
    attempt, membership, _ = await make_case(
        test_db, member_status="pending", note=json.dumps({"reason": "join_request_pending"})
    )
    service = AcquisitionAutomationService(test_db)
    service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings())
    service._reconcile_failed_auto_join_groups = AsyncMock(
        return_value={"updated": 0, "details": []}
    )
    service._evaluate_joined_group = AsyncMock(
        return_value=JoinedGroupAuditResult(passed=True, can_send_messages=True)
    )
    service._account_ad_warmup_days = AsyncMock(return_value=0)
    service._sync_group_ad_policy_from_audit = AsyncMock()
    service._join_group = AsyncMock()
    service._leave_group = AsyncMock()

    result = await service._sync_pending_auto_join_memberships()

    await test_db.refresh(attempt)
    await test_db.refresh(membership)
    assert result["checked"] == 1
    assert membership.status == "joined"
    assert attempt.status == "success"
    assert attempt.joined_at == membership.last_checked_at
    service._join_group.assert_not_awaited()
    service._leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_leaves_still_unapproved_request_pending(test_db):
    attempt, _, _ = await make_case(
        test_db, member_status="pending", note=json.dumps({"reason": "join_request_pending"})
    )
    service = AcquisitionAutomationService(test_db)
    service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings())
    service._reconcile_failed_auto_join_groups = AsyncMock(
        return_value={"updated": 0, "details": []}
    )
    service._evaluate_joined_group = AsyncMock(
        return_value=JoinedGroupAuditResult(passed=False, reason="account_not_participant")
    )
    service._join_group = AsyncMock()
    result = await service._sync_pending_auto_join_memberships()
    await test_db.refresh(attempt)
    assert attempt.status == "pending"
    assert result["details"][0]["reason"] == "join_request_still_pending"
    service._join_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["legacy_truncated", "appended_event", "new_compact"])
async def test_long_or_appended_successful_audit_is_reconciled(test_db, format):
    audit = JoinedGroupAuditResult(
        passed=True,
        can_send_messages=True,
        permission_reason="default_send_allowed",
        message_count=100,
        ad_rule_details={
            "reason": "group_rules_ai_unavailable",
            "evidence": [{"text": "x" * 6000}],
        },
    )
    service = AcquisitionAutomationService(test_db)
    if format == "legacy_truncated":
        note = json.dumps(audit.details(), ensure_ascii=False)[:4000]
    elif format == "appended_event":
        note = service._append_membership_note(json.dumps({"passed": True}), {"event": "warmup"})
    else:
        note = service._format_join_audit_note(audit)
        assert len(note) <= 4000
        assert json.loads(note)["passed"] is True
        assert json.loads(note)["details_truncated"] is True
    attempt, _, _ = await make_case(test_db, note=note)
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 1
    await test_db.refresh(attempt)
    assert attempt.status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["failed", "string_passed", "reason", "missing_header", "wrong_size"]
)
async def test_legacy_truncation_does_not_bypass_confirmation(test_db, change):
    payload = JoinedGroupAuditResult(
        passed=True,
        can_send_messages=True,
        permission_reason="default_send_allowed",
        ad_rule_details={"evidence": [{"text": "x" * 6000}]},
    ).details()
    if change == "failed":
        payload["passed"] = False
    elif change == "string_passed":
        payload["passed"] = "true"
    elif change == "reason":
        payload["reason"] = "verification_failed"
    elif change == "missing_header":
        del payload["should_leave"]
    note = json.dumps(payload, ensure_ascii=False)[:4000]
    if change == "wrong_size":
        note = note[:-1]
    await make_case(test_db, note=note)
    assert (await reconcile_joined_auto_join_attempts(test_db))["updated"] == 0
