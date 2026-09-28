"""Regression coverage for the eight follow-up audit findings (no Telegram I/O)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from telethon.errors import FloodWaitError, UserNotParticipantError
from telethon.tl.types import Channel, ChatInviteAlready, ChatInvitePeek, ChatPhotoEmpty

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionError, TelegramExecutionService
from app.core.automation_settings import save_auto_join_scheduler_settings
from app.core.group.membership_sync import _upsert_synced_group_membership
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    JoinedGroupAuditResult,
    MessageLanguageProfile,
)
from app.modules.acquisition.join_budget import JoinBudgetBlocked
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus, GroupAdProfile
from app.modules.owned_group.models import OwnedGroupAsset

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 22, 10)
RAW_ID = 700001
MARKED_ID = -1000000700001


@pytest.fixture(autouse=True)
def available_read_budget(monkeypatch):
    # Reconciliation scenarios are offline; Redis readiness has separate tests.
    monkeypatch.setattr("app.core.account.read_schedule.check_read_ready", AsyncMock())


async def seed(db, *, membership=False, age_hours=2, raw_id=False, owned=False):
    await save_auto_join_scheduler_settings(db, {"enabled": True})
    account = TelegramAccount(
        identifier="followup-test",
        session_name="followup-test",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
        registered_at=NOW - timedelta(days=365),
        asset_verified_at=NOW - timedelta(days=1),
    )
    db.add(account)
    await db.flush()
    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_join_enabled=True,
        max_groups_per_day=30,
        max_groups_total=100,
        join_interval_min_seconds=2880,
        join_interval_max_seconds=7200,
    )
    group = Group(
        group_id=RAW_ID if raw_id else MARKED_ID,
        title="Test group",
        username="audit_group",
        status="pending",
    )
    db.add_all([config, group])
    await db.flush()
    member = None
    if membership:
        started = NOW - timedelta(hours=age_hours)
        member = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="joined",
            join_method="account_dialog_sync",
            ad_status="warming",
            warmup_status="joined_pending_test",
            review_status="final_pending" if age_hours >= 24 else "review_2h",
            review_started_at=started,
            review_deadline_at=started + timedelta(hours=24),
            review_next_at=NOW - timedelta(minutes=1),
            review_attempts=1,
        )
        db.add(member)
    if owned:
        db.add(
            OwnedGroupAsset(
                internal_name="owned-test",
                title="Owned test group",
                owner_account_id=account.id,
                core_group_id=group.id,
                telegram_chat_id=MARKED_ID,
            )
        )
    await db.commit()
    return account, config, group, member


def service(db):
    svc = AcquisitionAutomationService(db, account_pool=AsyncMock())
    svc._account_ad_warmup_days = AsyncMock(return_value=0)
    svc.join_budget._effective_limit = AsyncMock(return_value=30)
    return svc


async def request(
    db, account, group, *, state="outcome_unknown", minutes=60, username=None, linked=True
):
    row = AutoJoinAttempt(
        account_id=account.id,
        group_id=group.id if linked else None,
        telegram_group_id=group.group_id if linked else None,
        group_username=username or group.username,
        target_key="username:" + (username or group.username),
        request_state=state,
        status=DeliveryStatus.PENDING.value,
        telegram_action_attempted=True,
        attempted_at=NOW - timedelta(minutes=minutes),
        request_sent_at=NOW - timedelta(minutes=minutes),
    )
    db.add(row)
    await db.commit()
    return row


def resolved(group):
    return {
        "id": group.group_id,
        "title": group.title,
        "username": group.username,
        "participants_count": 120,
    }


def freeze(monkeypatch, now=NOW):
    monkeypatch.setattr("app.modules.acquisition.automation._now", lambda: now)


@pytest.mark.parametrize("existing", [False, True])
async def test_success_closes_unknown_once_without_resetting_deadline_or_quota(
    test_db, monkeypatch, existing
):
    freeze(monkeypatch)
    account, _, group, member = await seed(test_db, membership=existing)
    original_deadline = member.review_deadline_at if member else NOW + timedelta(hours=24)
    row = await request(test_db, account, group)
    svc = service(test_db)
    resolver = svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(
        return_value=resolved(group)
    )
    first = await svc._reconcile_unresolved_join_requests(limit=10)
    freeze(monkeypatch, NOW + timedelta(hours=1))
    second = await svc._reconcile_unresolved_join_requests(limit=10)
    await test_db.refresh(row)
    member = (await test_db.scalars(select(GroupAccountMembership))).one()
    assert (first["updated"], second["updated"], resolver.await_count) == (1, 0, 1)
    assert row.request_state == "sent" and row.status == "success"
    assert row.request_sent_at == NOW - timedelta(hours=1)
    assert member.review_deadline_at == original_deadline
    budget = await svc.join_budget.status(account.id, now=NOW + timedelta(hours=1))
    assert budget.requested_rolling_24h == 1
    assert budget.pending_review_count == 1


@pytest.mark.parametrize("leave_error", [None, "timeout waiting for Telegram"])
async def test_forbidden_review_actually_leaves_or_retries(test_db, monkeypatch, leave_error):
    freeze(monkeypatch)
    _, _, group, member = await seed(test_db, membership=True)
    svc = service(test_db)
    audit = svc._build_join_audit_result(
        can_send_messages=True,
        permission_reason="send_allowed",
        message_count=30,
        unique_senders=10,
        member_count=100,
        language=MessageLanguageProfile(
            total_messages=30,
            text_messages=30,
            chinese_messages=30,
            chinese_chars=300,
            text_chars=350,
            chinese_message_ratio=1.0,
            chinese_char_ratio=300 / 350,
        ),
    )
    audit.ad_allowed = False
    assert audit.passed and not audit.should_leave
    svc._evaluate_joined_group = AsyncMock(return_value=audit)
    svc._leave_group = AsyncMock(return_value=leave_error)
    await svc._sync_pending_auto_join_memberships()
    await test_db.refresh(member)
    svc._leave_group.assert_awaited_once()
    assert member.status == ("left" if leave_error is None else "leave_failed")
    assert member.leave_attempts == 1
    assert (member.leave_confirmed_at is not None) == (leave_error is None)
    assert (member.review_next_at is not None) == (leave_error is not None)
    profile = await test_db.scalar(
        select(GroupAdProfile).where(GroupAdProfile.group_id == group.id)
    )
    assert profile.ad_policy_mode == "forbidden"


async def test_rule_decision_without_leave_result_only_queues_exit(test_db):
    _, _, _, member = await seed(test_db, membership=True)
    service(test_db)._apply_join_review_result(
        member, JoinedGroupAuditResult(passed=True, ad_allowed=False), now=NOW
    )
    assert member.status == "joined"
    assert member.review_status == "exit_pending"
    assert member.leave_confirmed_at is None and member.left_at is None


@pytest.mark.parametrize("raw_id", [False, True])
async def test_owned_group_never_enters_review_or_leave_retry(test_db, monkeypatch, raw_id):
    freeze(monkeypatch)
    account, _, group, member = await seed(
        test_db, membership=True, age_hours=25, owned=True, raw_id=raw_id
    )
    svc = service(test_db)
    svc._evaluate_joined_group = AsyncMock()
    svc._leave_group = AsyncMock()
    await svc._sync_pending_auto_join_memberships()
    await test_db.refresh(member)
    svc._evaluate_joined_group.assert_not_awaited()
    svc._leave_group.assert_not_awaited()
    assert member.status == "joined" and member.review_status == "owned_group_excluded"
    synced = await _upsert_synced_group_membership(
        test_db,
        account_id=account.id,
        group_id=group.group_id,
        title=group.title,
        username=group.username,
        member_count=120,
    )
    assert synced.review_status == "owned_group_excluded" and synced.review_next_at is None


@pytest.mark.parametrize("group_id", [RAW_ID, MARKED_ID])
async def test_final_leave_boundary_protects_owned_id_aliases(test_db, group_id):
    account, _, _, _ = await seed(test_db, owned=True, raw_id=True)
    client = AsyncMock()
    execution = TelegramExecutionService(AccountRiskGuard(test_db))
    with pytest.raises(TelegramExecutionError, match="owned_group_protected"):
        await execution.leave_group(
            SimpleNamespace(client=client, account_id=account.id),
            SimpleNamespace(id=RAW_ID, megagroup=True),
            group_id=group_id,
        )
    client.assert_not_awaited()


async def test_review_loads_accounts_before_evaluation_when_new_joins_disabled(
    test_db, monkeypatch
):
    freeze(monkeypatch)
    account, config, _, member = await seed(test_db, membership=True, age_hours=25)
    config.auto_join_enabled = False
    config.max_groups_per_day = 0
    await test_db.commit()
    svc = service(test_db)

    async def evaluate(account_id, group):
        svc.account_pool.sync_from_db.assert_awaited_once()
        assert svc.account_pool.sync_from_db.await_args.args[0][0].id == account_id == account.id
        return JoinedGroupAuditResult(passed=True, ad_allowed=True)

    svc._evaluate_joined_group = AsyncMock(side_effect=evaluate)
    await svc.run_auto_join()
    await test_db.refresh(member)
    svc._evaluate_joined_group.assert_awaited_once()
    assert member.review_status == "approved"


@pytest.mark.parametrize("paused", ["global", "account"])
async def test_paused_review_performs_no_external_action(test_db, monkeypatch, paused):
    freeze(monkeypatch)
    account, _, group, _ = await seed(test_db, membership=True, age_hours=25)
    await request(test_db, account, group)
    if paused == "global":
        await save_auto_join_scheduler_settings(test_db, {"enabled": False})
    else:
        account.risk_pause_until = NOW + timedelta(hours=2)
        await test_db.commit()
    svc = service(test_db)
    svc._evaluate_joined_group = AsyncMock()
    svc._leave_group = AsyncMock()
    svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock()
    await svc._sync_pending_auto_join_memberships()
    svc._evaluate_joined_group.assert_not_awaited()
    svc._leave_group.assert_not_awaited()
    svc.account_pool.acquire_by_id.assert_not_awaited()
    svc.telegram_execution.resolve_join_group_by_link_membership.assert_not_awaited()


@pytest.mark.parametrize("key", ["username:previous_name", "group:1", "telegram:700001"])
async def test_known_group_identity_blocks_username_changes_and_legacy_keys(test_db, key):
    account, _, group, _ = await seed(test_db)
    old = await request(test_db, account, group, username="previous_name")
    old.target_key = key
    await test_db.commit()
    with pytest.raises(JoinBudgetBlocked, match="join_outcome_reconciliation_required"):
        await service(test_db).join_budget.reserve(
            account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            group_username=group.username,
            source="test",
            now=NOW,
        )


async def test_send_boundary_also_blocks_renamed_unresolved_target(test_db):
    account, _, group, _ = await seed(test_db)
    svc = service(test_db)
    reservation = await svc.join_budget.reserve(
        account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        group_username=group.username,
        source="test",
        now=NOW,
    )
    await request(test_db, account, group, username="previous_name")
    with pytest.raises(JoinBudgetBlocked, match="join_outcome_reconciliation_required"):
        await svc.join_budget.mark_sent(reservation, now=NOW)
    await test_db.refresh(reservation)
    assert reservation.telegram_action_attempted is False


@pytest.mark.parametrize("linked", [False, True])
async def test_reconcile_reuses_raw_id_group_and_existing_review(test_db, monkeypatch, linked):
    freeze(monkeypatch)
    account, _, group, member = await seed(test_db, membership=True, raw_id=True)
    deadline = member.review_deadline_at
    row = await request(test_db, account, group, state="sent", linked=linked)
    svc = service(test_db)
    data = resolved(group)
    data.update(id=MARKED_ID, raw_id=RAW_ID)
    svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(return_value=data)
    result = await svc._reconcile_unresolved_join_requests(limit=10)
    assert result["updated"] == 1
    assert len((await test_db.scalars(select(Group))).all()) == 1
    assert len((await test_db.scalars(select(GroupAccountMembership))).all()) == 1
    assert row.group_id == group.id and member.review_deadline_at == deadline


async def test_username_reassigned_to_another_group_does_not_confirm_old_request(
    test_db, monkeypatch
):
    freeze(monkeypatch)
    account, _, group, _ = await seed(test_db)
    row = await request(test_db, account, group)
    svc = service(test_db)
    data = resolved(group)
    data["id"] = MARKED_ID - 1
    svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(return_value=data)
    result = await svc._reconcile_unresolved_join_requests(limit=10)
    assert result["updated"] == 0 and row.request_state == "outcome_unknown"
    assert result["details"][-1]["reason"] == "join_reconciliation_identity_mismatch"


async def test_pending_head_rotates_durably_even_after_service_restart(test_db, monkeypatch):
    freeze(monkeypatch)
    account, _, group, _ = await seed(test_db)
    old = await request(
        test_db, account, group, state="sent", minutes=120, username="old_pending", linked=False
    )
    later = await request(
        test_db, account, group, state="sent", username="new_approved", linked=False
    )
    svc = service(test_db)
    svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(return_value=None)
    await svc._reconcile_unresolved_join_requests(limit=1)
    assert old.reconciliation_checked_at == NOW and old.reconciliation_next_at > NOW
    restarted = service(test_db)
    resolver = restarted.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(
        return_value=resolved(group)
    )
    await restarted._reconcile_unresolved_join_requests(limit=1)
    assert resolver.await_args.args[1] == "@new_approved"
    assert later.status == "success" and old.status == "pending"


def channel():
    return Channel(
        id=RAW_ID,
        title="Test preview",
        photo=ChatPhotoEmpty(),
        date=NOW.replace(tzinfo=UTC),
        megagroup=True,
        access_hash=1,
    )


@pytest.mark.parametrize("joined", [False, True])
async def test_peek_requires_membership_confirmation(test_db, joined):
    entity = channel()
    client = AsyncMock(return_value=ChatInvitePeek(chat=entity, expires=NOW.replace(tzinfo=UTC)))
    client.get_permissions.return_value = SimpleNamespace(has_left=False)
    if not joined:
        client.get_permissions.side_effect = UserNotParticipantError(request=None)
    result = await TelegramExecutionService().resolve_join_group_by_link_membership(
        SimpleNamespace(client=client), "https://t.me/+AuditPreviewToken"
    )
    assert (result is not None) == joined
    client.get_permissions.assert_awaited_once_with(entity, "me")
    assert client.await_count == 1  # Only CheckChatInvite, never a join.


async def test_invite_already_is_confirmed_without_extra_membership_request(test_db):
    client = AsyncMock(return_value=ChatInviteAlready(chat=channel()))
    result = await TelegramExecutionService().resolve_join_group_by_link_membership(
        SimpleNamespace(client=client), "https://t.me/+AuditPreviewToken"
    )
    assert result["id"] == MARKED_ID
    client.get_permissions.assert_not_awaited()


async def test_initial_peek_join_is_counted_and_imports_invite(test_db):
    entity = channel()
    client = AsyncMock(
        side_effect=[
            ChatInvitePeek(chat=entity, expires=NOW.replace(tzinfo=UTC)),
            SimpleNamespace(chats=[entity]),
        ]
    )
    counted = AsyncMock()
    result = await TelegramExecutionService().join_group_by_link(
        SimpleNamespace(client=client),
        "https://t.me/+AuditPreviewToken",
        on_join_request_attempted=counted,
    )
    assert result["id"] == MARKED_ID
    assert [type(call.args[0]).__name__ for call in client.await_args_list] == [
        "CheckChatInviteRequest",
        "ImportChatInviteRequest",
    ]
    counted.assert_awaited_once()


@pytest.mark.parametrize("failure", ["unresolvable", "network"])
async def test_failed_reconciliation_rotates_without_losing_request_count(
    test_db, monkeypatch, failure
):
    freeze(monkeypatch)
    account, _, group, _ = await seed(test_db)
    old = await request(test_db, account, group, minutes=120, linked=False)
    if failure == "unresolvable":
        old.target_key = "group:999999"
        await test_db.commit()
    later = await request(test_db, account, group, minutes=60, linked=False)
    svc = service(test_db)
    svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(
        side_effect=TimeoutError()
    )
    await svc._reconcile_unresolved_join_requests(limit=1)
    restarted = service(test_db)
    restarted.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(
        return_value=resolved(group)
    )
    await restarted._reconcile_unresolved_join_requests(limit=1)
    assert later.status == "success" and old.request_state == "outcome_unknown"
    assert old.reconciliation_checked_at == NOW
    assert (await restarted.join_budget.status(account.id, now=NOW)).requested_today == 2


async def test_reconciliation_flood_wait_is_persisted_and_blocks_other_account_checks(
    test_db, monkeypatch
):
    freeze(monkeypatch)
    account, _, group, _ = await seed(test_db)
    first = await request(test_db, account, group, minutes=120, linked=False)
    await request(test_db, account, group, minutes=60, linked=False)
    svc = service(test_db)
    resolver = svc.telegram_execution.resolve_join_group_by_link_membership = AsyncMock(
        side_effect=FloodWaitError(request=None, capture=7200)
    )
    await svc._reconcile_unresolved_join_requests(limit=10)
    await test_db.refresh(account)
    assert account.risk_pause_until == NOW + timedelta(hours=2)
    assert first.reconciliation_next_at == account.risk_pause_until
    resolver.assert_awaited_once()
    freeze(monkeypatch, NOW + timedelta(minutes=16))
    await svc._reconcile_unresolved_join_requests(limit=10)
    resolver.assert_awaited_once()


async def test_numeric_target_reconciliation_remains_read_only(test_db):
    client = AsyncMock()
    client.get_entity.return_value = channel()
    client.get_permissions.return_value = SimpleNamespace(has_left=False)
    result = await TelegramExecutionService().resolve_join_group_by_link_membership(
        SimpleNamespace(client=client), MARKED_ID
    )
    assert result["id"] == MARKED_ID
    client.get_entity.assert_awaited_once_with(MARKED_ID)
    client.assert_not_awaited()


async def test_broadcast_peek_is_rejected_before_import_or_quota_consumption(test_db):
    entity = channel()
    entity.megagroup = False
    entity.broadcast = True
    client = AsyncMock(return_value=ChatInvitePeek(chat=entity, expires=NOW.replace(tzinfo=UTC)))
    counted = AsyncMock()
    with pytest.raises(TelegramExecutionError, match="broadcast channel"):
        await TelegramExecutionService().join_group_by_link(
            SimpleNamespace(client=client),
            "https://t.me/+AuditPreviewToken",
            on_join_request_attempted=counted,
        )
    counted.assert_not_awaited()
    assert client.await_count == 1
    assert type(client.await_args.args[0]).__name__ == "CheckChatInviteRequest"
