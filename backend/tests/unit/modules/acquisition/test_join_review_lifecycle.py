from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, AccountStatus, AccountType, TelegramAccount
from app.core.automation_settings import save_auto_join_scheduler_settings
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    JOIN_REVIEW_APPROVED,
    JOIN_REVIEW_FINAL,
    JOIN_REVIEW_LEAVE_FAILED,
    JOIN_REVIEW_LEFT,
    JOIN_REVIEW_TWO_HOUR,
    JoinedGroupAuditResult,
    MessageLanguageProfile,
)
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus

pytestmark = pytest.mark.asyncio


def _membership(now: datetime) -> GroupAccountMembership:
    return GroupAccountMembership(
        group_id=1,
        telegram_group_id=1001,
        account_id=1,
        status="joined",
        review_status="initial_pending",
        review_started_at=now,
        review_next_at=now + timedelta(hours=2),
        review_deadline_at=now + timedelta(hours=24),
        review_attempts=0,
    )


async def test_review_uses_fixed_two_hour_and_twenty_four_hour_deadlines(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    membership = _membership(now)
    unresolved = JoinedGroupAuditResult(
        passed=True,
        should_leave=False,
        ad_allowed=None,
    )

    service._apply_join_review_result(membership, unresolved, now=now)
    assert membership.review_status == JOIN_REVIEW_TWO_HOUR
    assert membership.review_next_at == now + timedelta(hours=2)
    service._apply_join_review_result(
        membership,
        unresolved,
        now=now + timedelta(hours=2),
    )
    assert membership.review_status == JOIN_REVIEW_FINAL
    assert membership.review_next_at == now + timedelta(hours=24)
    assert membership.review_deadline_at == now + timedelta(hours=24)


async def test_real_audit_result_approves_or_schedules_review_without_false_leave(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    language = MessageLanguageProfile(
        total_messages=30,
        text_messages=30,
        chinese_messages=30,
        chinese_chars=300,
        text_chars=350,
        chinese_message_ratio=1.0,
        chinese_char_ratio=300 / 350,
    )
    approved_audit = service._build_join_audit_result(
        can_send_messages=True,
        permission_reason="send_allowed",
        language=language,
        message_count=30,
        unique_senders=10,
        member_count=100,
    )
    approved_audit.ad_allowed = True
    assert approved_audit.passed is True
    assert approved_audit.should_leave is False
    approved = _membership(now)
    service._apply_join_review_result(approved, approved_audit, now=now)
    assert approved.status == "joined"
    assert approved.review_status == JOIN_REVIEW_APPROVED

    unresolved_audit = service._build_join_audit_result(
        can_send_messages=True,
        permission_reason="send_allowed",
        language=language,
        message_count=30,
        unique_senders=10,
        member_count=100,
    )
    unresolved = _membership(now)
    service._apply_join_review_result(unresolved, unresolved_audit, now=now)
    assert unresolved.status == "joined"
    assert unresolved.review_status == JOIN_REVIEW_TWO_HOUR
    assert unresolved.review_next_at == now + timedelta(hours=2)


async def test_technical_audit_failure_becomes_manual_without_leave(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    membership = _membership(now - timedelta(hours=25))
    audit = JoinedGroupAuditResult(
        passed=False,
        reason="join_audit_account_unavailable",
        permission_reason="temporary proxy failure",
    )

    service._apply_join_review_result(membership, audit, now=now)
    assert membership.status == "joined"
    assert membership.review_status == "manual_required"
    assert membership.review_next_at is None
    assert membership.leave_requested_at is None
    assert membership.ad_status == "blocked"


async def test_review_runs_even_when_no_account_can_add_new_groups(test_db):
    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    service._sync_pending_auto_join_memberships = AsyncMock(
        return_value={"checked": 1, "updated": 1, "details": []}
    )
    service._list_join_enabled_account_configs = AsyncMock(return_value=[])

    result = await service.run_auto_join()

    service._sync_pending_auto_join_memberships.assert_awaited_once()
    assert result["updated"] == 1


async def test_pending_link_request_is_reconciled_without_resending_join(test_db):
    now = datetime.utcnow()
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    account = TelegramAccount(
        identifier="pending-link-review",
        session_name="pending-link-review",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        registered_at=now - timedelta(days=365),
        asset_verified_at=now - timedelta(days=1),
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(
        AccountOperationConfig(
            account_id=account.id,
            enabled=True,
            auto_join_enabled=False,
            max_groups_per_day=0,
        )
    )
    attempt = AutoJoinAttempt(
        account_id=account.id,
        status=DeliveryStatus.PENDING.value,
        reason="join_request_pending_approval",
        telegram_action_attempted=True,
        request_state="sent",
        target_key="username:approved_group",
        request_sent_at=now - timedelta(hours=2),
        attempted_at=now - timedelta(hours=2),
    )
    test_db.add(attempt)
    await test_db.commit()

    wrapper = object()
    pool = AsyncMock()
    pool.acquire_by_id.return_value = wrapper
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(
        return_value={
            "id": -100700003,
            "title": "Approved group",
            "username": "approved_group",
            "participants_count": 120,
        }
    )
    service._account_ad_warmup_days = AsyncMock(return_value=0)

    result = await service._reconcile_unresolved_join_requests(limit=10)

    assert result["updated"] == 1
    service.telegram_execution.resolve_join_group_by_link_membership.assert_awaited_once_with(
        wrapper,
        "@approved_group",
    )
    membership = (
        await test_db.execute(
            select(GroupAccountMembership).where(
                GroupAccountMembership.account_id == account.id,
                GroupAccountMembership.telegram_group_id == -100700003,
            )
        )
    ).scalar_one()
    await test_db.refresh(attempt)
    assert membership.status == "joined"
    assert membership.review_status == "initial_pending"
    assert membership.review_next_at is not None
    assert attempt.status == DeliveryStatus.SUCCESS.value
    assert attempt.group_id == membership.group_id


async def test_timeout_after_send_is_marked_unknown_and_blocks_retry(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    account = TelegramAccount(
        identifier="unknown-timeout",
        session_name="unknown-timeout",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
        registered_at=now - timedelta(days=365),
        asset_verified_at=now - timedelta(days=1),
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(
        AccountOperationConfig(
            account_id=account.id,
            enabled=True,
            auto_join_enabled=True,
            max_groups_per_day=30,
            max_groups_total=100,
            join_interval_min_seconds=2880,
            join_interval_max_seconds=7200,
        )
    )
    group = Group(group_id=-100700004, title="Unknown timeout", username="unknown_timeout")
    test_db.add(group)
    await test_db.commit()

    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    service.join_budget._effective_limit = AsyncMock(return_value=30)
    attempt = await service.join_budget.reserve(
        account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        group_username=group.username,
        source="auto_join",
        now=now,
    )
    await service.join_budget.mark_sent(attempt, now=now)
    timeout = TimeoutError("connection timed out")
    await service._record_join_attempt(
        account.id,
        SimpleNamespace(
            group_id=group.group_id,
            username=group.username,
            title=group.title,
        ),
        DeliveryStatus.FAILED,
        db_group=group,
        reason="join_failed",
        error=str(timeout),
        telegram_action_attempted=True,
        attempt=attempt,
        outcome_unknown=service._join_error_outcome_unknown(
            timeout,
            telegram_action_attempted=True,
        ),
    )
    await test_db.refresh(attempt)
    assert attempt.request_state == "outcome_unknown"


async def test_leave_failure_is_not_recorded_as_left(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    membership = _membership(now)

    confirmed = service._record_membership_leave_outcome(
        membership,
        leave_error="temporary network error",
        now=now,
    )
    assert confirmed is False
    assert membership.status == "leave_failed"
    assert membership.left_at is None
    assert membership.review_status == JOIN_REVIEW_LEAVE_FAILED

    confirmed = service._record_membership_leave_outcome(
        membership,
        leave_error=None,
        now=now + timedelta(hours=2),
    )
    assert confirmed is True
    assert membership.status == "left"
    assert membership.left_at == now + timedelta(hours=2)
    assert membership.review_status == JOIN_REVIEW_LEFT


async def test_ad_warmup_is_blocked_until_join_review_is_approved(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    membership = _membership(now)

    assert (
        await service._ad_warmup_skip_reason(1, membership, now, dry_run=True)
        == "join_review_pending"
    )
    membership.review_status = JOIN_REVIEW_APPROVED


async def test_global_pause_prevents_due_review_and_leave_calls(test_db, monkeypatch):
    now = datetime.utcnow()
    account = TelegramAccount(
        identifier="review-paused",
        session_name="review-paused",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(AccountOperationConfig(account_id=account.id, enabled=True))
    group = Group(group_id=700001, title="review paused", status="pending")
    test_db.add(group)
    await test_db.flush()
    test_db.add(
        GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="leave_failed",
            review_status=JOIN_REVIEW_LEAVE_FAILED,
            ad_status="blocked",
            warmup_status="blocked",
            probe_status="skipped",
            review_started_at=now - timedelta(hours=25),
            review_next_at=now - timedelta(minutes=1),
            review_deadline_at=now - timedelta(hours=1),
            leave_requested_at=now - timedelta(hours=1),
        )
    )
    await test_db.commit()
    await save_auto_join_scheduler_settings(test_db, {"enabled": False})

    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    monkeypatch.setattr(
        "app.modules.acquisition.automation.reconcile_joined_auto_join_attempts",
        AsyncMock(return_value={"checked": 0, "updated": 0, "details": []}),
    )
    service._reconcile_failed_auto_join_groups = AsyncMock(
        return_value={"updated": 0, "details": []}
    )
    service._leave_group = AsyncMock()

    result = await service._sync_pending_auto_join_memberships()
    service._leave_group.assert_not_awaited()
    assert result["details"][-1]["reason"] == "global_auto_join_paused"


async def test_final_review_exits_and_retries_until_leave_is_confirmed(test_db, monkeypatch):
    now = datetime.utcnow()
    account = TelegramAccount(
        identifier="review-final",
        session_name="review-final",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(AccountOperationConfig(account_id=account.id, enabled=True))
    group = Group(group_id=700002, title="review final", status="pending")
    test_db.add(group)
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        review_status=JOIN_REVIEW_FINAL,
        review_started_at=now - timedelta(hours=25),
        review_next_at=now - timedelta(minutes=1),
        review_deadline_at=now - timedelta(hours=1),
        review_attempts=2,
    )
    test_db.add(membership)
    await test_db.commit()
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})

    service = AcquisitionAutomationService(test_db, account_pool=AsyncMock())
    monkeypatch.setattr(
        "app.modules.acquisition.automation.reconcile_joined_auto_join_attempts",
        AsyncMock(return_value={"checked": 0, "updated": 0, "details": []}),
    )
    service._reconcile_failed_auto_join_groups = AsyncMock(
        return_value={"updated": 0, "details": []}
    )
    service._evaluate_joined_group = AsyncMock(
        return_value=JoinedGroupAuditResult(
            passed=True,
            should_leave=False,
            ad_allowed=None,
        )
    )
    service._leave_group = AsyncMock(return_value="temporary network error")
    service._account_ad_warmup_days = AsyncMock(return_value=0)
    service._reject_group_after_failed_audit = AsyncMock(return_value=True)

    await service._sync_pending_auto_join_memberships()
    await test_db.refresh(membership)
    assert membership.status == "leave_failed"
    assert membership.left_at is None
    assert membership.review_status == JOIN_REVIEW_LEAVE_FAILED
    assert membership.leave_retry_at is not None

    membership.review_next_at = datetime.utcnow() - timedelta(minutes=1)
    membership.leave_retry_at = membership.review_next_at
    await test_db.commit()
    service._leave_group = AsyncMock(return_value=None)

    await service._sync_pending_auto_join_memberships()
    await test_db.refresh(membership)
    assert membership.status == "left"
    assert membership.left_at is not None
    assert membership.review_status == JOIN_REVIEW_LEFT
    assert membership.leave_confirmed_at is not None
