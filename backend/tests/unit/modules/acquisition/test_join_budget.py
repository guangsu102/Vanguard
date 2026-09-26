from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.account.models import (
    AccountOperationConfig,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.automation_settings import save_auto_join_scheduler_settings
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.join_budget import (
    JoinBudgetBlocked,
    JoinRequestBudgetService,
)
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus
from app.modules.owned_group.models import OwnedGroupAsset

pytestmark = pytest.mark.asyncio


class _OneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def _postgres_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def _eligible_account(
    db,
    suffix: str,
    *,
    now: datetime,
) -> tuple[TelegramAccount, AccountOperationConfig]:
    await save_auto_join_scheduler_settings(db, {"enabled": True})
    account = TelegramAccount(
        identifier=f"join-budget-{suffix}",
        session_name=f"join-budget-{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level=AccountRiskLevel.NORMAL.value,
        is_active=True,
        registered_at=now - timedelta(days=365),
        asset_verified_at=now - timedelta(days=1),
    )
    db.add(account)
    await db.flush()
    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_join_enabled=True,
        auto_ads_enabled=True,
        max_groups_per_day=30,
        max_groups_total=100,
        join_interval_min_seconds=2880,
        join_interval_max_seconds=7200,
    )
    db.add(config)
    await db.commit()
    await db.refresh(account)
    await db.refresh(config)
    return account, config


def _service(db) -> JoinRequestBudgetService:
    service = JoinRequestBudgetService(db)
    service._effective_limit = AsyncMock(return_value=30)
    return service


def _sent_attempt(
    account_id: int,
    sent_at: datetime,
    *,
    status: DeliveryStatus = DeliveryStatus.SUCCESS,
) -> AutoJoinAttempt:
    return AutoJoinAttempt(
        account_id=account_id,
        status=status.value,
        reason="test_request",
        telegram_action_attempted=True,
        request_state="sent",
        request_sent_at=sent_at,
        attempted_at=sent_at,
    )


async def test_account_and_config_are_locked_in_stable_postgresql_order(test_db):
    del test_db  # Initializes the full SQLAlchemy model registry used by this project.
    account = TelegramAccount(
        id=41,
        identifier="join-budget-lock",
        session_name="join-budget-lock",
        account_type=AccountType.PROMOTER,
    )
    config = AccountOperationConfig(account_id=41)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_OneResult(account), _OneResult(config)])

    locked = await JoinRequestBudgetService(db)._lock_account_and_config(41)

    assert locked == (account, config)
    account_sql, config_sql = [
        _postgres_sql(call.args[0]) for call in db.execute.await_args_list
    ]
    assert account_sql.rstrip().endswith("FOR UPDATE OF telegram_account")
    assert config_sql.rstrip().endswith(
        "FOR UPDATE OF telegram_account_operation_config"
    )


async def test_thirty_first_request_is_blocked_and_active_reservation_is_atomic_capacity(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "daily", now=now)
    for index in range(29):
        test_db.add(_sent_attempt(account.id, now - timedelta(hours=3, seconds=index)))
    await test_db.commit()

    service = _service(test_db)
    reservation = await service.reserve(account.id, source="test", now=now)
    assert reservation.request_state == "reserved"

    restarted_service = _service(test_db)
    with pytest.raises(JoinBudgetBlocked) as exc_info:
        await restarted_service.reserve(account.id, source="test-retry", now=now)
    assert exc_info.value.reason == "daily_join_quota"
    assert (
        await test_db.scalar(
            select(AutoJoinAttempt.id).where(
                AutoJoinAttempt.account_id == account.id,
                AutoJoinAttempt.request_state == "reserved",
            )
        )
    ) == reservation.id


async def test_failed_or_left_request_stays_counted_but_unsent_release_does_not(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "outcomes", now=now)
    service = _service(test_db)

    reservation = await service.reserve(account.id, source="failed", now=now)
    await service.mark_sent(reservation, now=now)
    await service.finalize(
        reservation,
        status=DeliveryStatus.FAILED,
        reason="join_failed_then_left",
        outcome_unknown=True,
    )
    assert reservation.request_state == "outcome_unknown"
    persisted = _service(test_db)
    status = await persisted.status(account.id, now=now + timedelta(hours=1))
    assert status.requested_today == 1
    assert status.requested_rolling_24h == 1

    released = await persisted.reserve(
        account.id,
        source="filtered_before_send",
        now=now + timedelta(hours=1),
    )
    await persisted.release(released, reason="local_filter_rejected")
    after_release = await persisted.status(account.id, now=now + timedelta(hours=1))
    assert after_release.requested_today == 1
    assert after_release.active_reservations == 0


async def test_rolling_window_blocks_cross_business_day_burst(test_db):
    now = datetime(2026, 9, 21, 16, 10, 0)
    account, _config = await _eligible_account(test_db, "rolling", now=now)
    for index in range(30):
        test_db.add(_sent_attempt(account.id, now - timedelta(hours=4, seconds=index)))
    await test_db.commit()

    status = await _service(test_db).status(account.id, now=now)
    assert status.requested_today == 0
    assert status.requested_rolling_24h == 30
    assert "rolling_24h_join_quota" in status.blocked_reasons


async def test_expired_reservation_is_replaced_without_allowing_late_send(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "expired", now=now)
    service = _service(test_db)

    expired = await service.reserve(account.id, source="stalled", now=now)
    replacement = await service.reserve(
        account.id,
        source="retry",
        now=now + timedelta(minutes=11),
    )

    assert expired.request_state == "released"
    assert replacement.request_state == "reserved"
    with pytest.raises(JoinBudgetBlocked, match="join_reservation_not_active"):
        await service.mark_sent(expired, now=now + timedelta(minutes=11))
    refreshed = await service.status(account.id, now=now + timedelta(minutes=11))
    assert refreshed.active_reservations == 1


async def test_review_backlog_hysteresis_and_overdue_review_pause_new_joins(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, config = await _eligible_account(test_db, "backlog", now=now)
    for index in range(30):
        group = Group(group_id=900_000 + index, title=f"review-{index}")
        test_db.add(group)
        await test_db.flush()
        test_db.add(
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=account.id,
                status="joined",
                ad_status="warming",
                warmup_status="joined_pending_test",
                joined_at=now - timedelta(minutes=30),
                last_checked_at=now - timedelta(minutes=30),
            )
        )
    await test_db.commit()

    service = _service(test_db)
    with pytest.raises(JoinBudgetBlocked) as exc_info:
        await service.reserve(account.id, source="backlog", now=now)
    assert exc_info.value.reason == "join_review_backlog"
    await test_db.refresh(config)
    assert config.join_review_backlog_paused is True

    membership_ids = list(
        (
            await test_db.scalars(
                select(GroupAccountMembership.id)
                .where(GroupAccountMembership.account_id == account.id)
                .order_by(GroupAccountMembership.id)
            )
        ).all()
    )
    await test_db.execute(
        update(GroupAccountMembership)
        .where(GroupAccountMembership.id.in_(membership_ids[:9]))
        .values(ad_status="active", warmup_status="ad_eligible", review_status="approved", review_next_at=None)
    )
    await test_db.commit()
    still_paused = await service.status(account.id, now=now)
    assert still_paused.pending_review_count == 21
    assert "join_review_backlog" in still_paused.blocked_reasons

    await test_db.execute(
        update(GroupAccountMembership)
        .where(GroupAccountMembership.id == membership_ids[9])
        .values(ad_status="active", warmup_status="ad_eligible", review_status="approved", review_next_at=None)
    )
    await test_db.commit()
    resumed = await service.reserve(account.id, source="backlog-cleared", now=now)
    assert resumed.request_state == "reserved"
    await test_db.refresh(config)
    assert config.join_review_backlog_paused is False

    overdue_account, _ = await _eligible_account(test_db, "overdue", now=now)
    overdue_group = Group(group_id=910_000, title="overdue-review")
    test_db.add(overdue_group)
    await test_db.flush()
    test_db.add(
        GroupAccountMembership(
            group_id=overdue_group.id,
            telegram_group_id=overdue_group.group_id,
            account_id=overdue_account.id,
            status="pending",
            joined_at=now - timedelta(hours=4),
            last_checked_at=now - timedelta(hours=4),
            review_status="initial_pending",
            review_started_at=now - timedelta(hours=4),
            review_next_at=now - timedelta(hours=2),
            review_deadline_at=now + timedelta(hours=20),
        )
    )
    await test_db.commit()
    overdue_status = await _service(test_db).status(overdue_account.id, now=now)
    assert overdue_status.overdue_review_count == 1
    assert "join_review_overdue" in overdue_status.blocked_reasons


async def test_global_pause_age_gate_owner_protection_and_interval_are_fail_closed(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "guards", now=now)
    service = _service(test_db)

    await save_auto_join_scheduler_settings(test_db, {"enabled": False})
    paused = await service.status(account.id, now=now)
    assert paused.blocked_reasons[0] == "global_auto_join_paused"
    with pytest.raises(JoinBudgetBlocked, match="global_auto_join_paused"):
        await service.reserve(account.id, source="paused", now=now)
    assert (
        await test_db.scalar(
            select(AutoJoinAttempt.id).where(
                AutoJoinAttempt.account_id == account.id
            )
        )
    ) is None
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})

    account.registered_at = now - timedelta(days=179)
    await test_db.commit()
    too_young = await service.status(account.id, now=now)
    assert "account_age_not_verified_six_months" in too_young.blocked_reasons

    account.registered_at = now - timedelta(days=365)
    owner_asset = OwnedGroupAsset(
        internal_name="protected-owner",
        title="Protected owner group",
        owner_account_id=account.id,
    )
    test_db.add(owner_asset)
    await test_db.commit()
    protected = await service.status(account.id, now=now)
    assert "owned_group_owner_protected" in protected.blocked_reasons
    with pytest.raises(JoinBudgetBlocked, match="owned_group_owner_protected"):
        await service.reserve(account.id, source="protected-owner", now=now)

    await test_db.delete(owner_asset)
    await test_db.commit()
    reservation = await service.reserve(account.id, source="interval", now=now)
    await service.mark_sent(reservation, now=now)
    blocked = await service.status(account.id, now=now + timedelta(minutes=47))
    assert "join_interval" in blocked.blocked_reasons
    assert blocked.next_allowed_at == now + timedelta(minutes=48)


async def test_send_boundary_rechecks_pause_and_expiration(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "send-boundary", now=now)
    service = _service(test_db)
    paused_attempt = await service.reserve(account.id, source="pause", now=now)
    await save_auto_join_scheduler_settings(test_db, {"enabled": False})
    with pytest.raises(JoinBudgetBlocked, match="global_auto_join_paused"):
        await service.mark_sent(paused_attempt, now=now + timedelta(seconds=1))
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    await service.release(paused_attempt, reason="paused_before_send")
    expired = await service.reserve(account.id, source="expired", now=now)
    with pytest.raises(JoinBudgetBlocked, match="join_reservation_expired"):
        await service.mark_sent(expired, now=now + timedelta(minutes=11))
    await test_db.refresh(expired)
    assert expired.request_state == "released"


async def test_send_boundary_rechecks_review_overdue_and_total_group_cap(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    overdue_account, _ = await _eligible_account(test_db, "send-overdue", now=now)
    service = _service(test_db)
    overdue_attempt = await service.reserve(
        overdue_account.id,
        target_key="https://t.me/send_overdue",
        source="send-boundary",
        now=now,
    )
    overdue_group = Group(group_id=930_001, title="became overdue")
    test_db.add(overdue_group)
    await test_db.flush()
    test_db.add(
        GroupAccountMembership(
            group_id=overdue_group.id,
            telegram_group_id=overdue_group.group_id,
            account_id=overdue_account.id,
            status="pending",
            review_status="initial_pending",
            review_started_at=now - timedelta(hours=4),
            review_next_at=now - timedelta(hours=2),
            review_deadline_at=now + timedelta(hours=20),
        )
    )
    await test_db.commit()
    with pytest.raises(JoinBudgetBlocked, match="join_review_overdue"):
        await service.mark_sent(overdue_attempt, now=now)
    await service.release(overdue_attempt, reason="overdue_before_send", now=now)

    capped_account, capped_config = await _eligible_account(
        test_db, "send-total-cap", now=now
    )
    capped_config.max_groups_total = 1
    await test_db.commit()
    capped_service = _service(test_db)
    capped_attempt = await capped_service.reserve(
        capped_account.id,
        target_key="https://t.me/send_total_cap",
        source="send-boundary",
        now=now,
    )
    joined_group = Group(group_id=930_002, title="joined elsewhere")
    test_db.add(joined_group)
    await test_db.flush()
    test_db.add(
        GroupAccountMembership(
            group_id=joined_group.id,
            telegram_group_id=joined_group.group_id,
            account_id=capped_account.id,
            status="joined",
            review_status="approved",
        )
    )
    await test_db.commit()
    with pytest.raises(JoinBudgetBlocked, match="total_group_quota"):
        await capped_service.mark_sent(capped_attempt, now=now)


async def test_stale_worker_cannot_turn_released_reservation_into_request(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "stale-worker", now=now)
    for index in range(29):
        test_db.add(_sent_attempt(account.id, now - timedelta(hours=3, seconds=index)))
    await test_db.commit()
    first_worker = _service(test_db)
    expired = await first_worker.reserve(account.id, source="stalled", now=now)
    session_factory = async_sessionmaker(test_db.bind, expire_on_commit=False)
    async with session_factory() as second_session:
        second_worker = _service(second_session)
        replacement = await second_worker.reserve(account.id, source="replacement", now=now + timedelta(minutes=11))
        await second_worker.mark_sent(replacement, now=now + timedelta(minutes=11))
    with pytest.raises(JoinBudgetBlocked, match="join_reservation_not_active"):
        await first_worker.mark_sent(expired, now=now + timedelta(minutes=12))
    sent_count = await test_db.scalar(select(func.count(AutoJoinAttempt.id)).where(AutoJoinAttempt.account_id == account.id, AutoJoinAttempt.request_state == "sent"))
    assert sent_count == 30


async def test_unknown_target_must_reconcile_before_retry(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "reconcile", now=now)
    service = _service(test_db)
    attempt = await service.reserve(account.id, target_key="https://t.me/example_group", source="first", now=now)
    await service.mark_sent(attempt, now=now)
    await service.finalize(attempt, status=DeliveryStatus.FAILED, reason="network_result_unknown", outcome_unknown=True)
    with pytest.raises(JoinBudgetBlocked, match="join_outcome_reconciliation_required"):
        await service.reserve(account.id, target_key="https://t.me/example_group/", source="retry", now=now + timedelta(minutes=49))


async def test_unknown_target_is_blocked_across_auto_and_manual_aliases(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "alias-reconcile", now=now)
    service = _service(test_db)
    attempt = await service.reserve(
        account.id,
        group_id=123,
        telegram_group_id=-1001234567890,
        group_username="Example_Group",
        source="auto_join",
        now=now,
    )
    assert attempt.target_key == "username:example_group"
    await service.mark_sent(attempt, now=now)
    await service.finalize(
        attempt,
        status=DeliveryStatus.FAILED,
        reason="network_result_unknown",
        outcome_unknown=True,
    )

    with pytest.raises(JoinBudgetBlocked, match="join_outcome_reconciliation_required"):
        await service.reserve(
            account.id,
            target_key="https://t.me/example_group",
            source="manual_group_link_join",
            require_auto_join_enabled=False,
            now=now + timedelta(minutes=49),
        )


async def test_unresolved_manual_request_counts_as_review_backlog(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, _config = await _eligible_account(test_db, "manual-pending", now=now)
    attempt = AutoJoinAttempt(
        account_id=account.id,
        status=DeliveryStatus.PENDING.value,
        reason="join_request_pending_approval",
        telegram_action_attempted=True,
        request_state="sent",
        target_key="invite:AbCdEf123",
        request_sent_at=now - timedelta(hours=4),
        attempted_at=now - timedelta(hours=4),
    )
    test_db.add(attempt)
    await test_db.commit()

    status = await _service(test_db).status(account.id, now=now)
    assert status.pending_review_count == 1
    assert status.overdue_review_count == 1
    assert "join_review_overdue" in status.blocked_reasons


async def test_central_budget_enforces_total_group_cap(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, config = await _eligible_account(test_db, "total-cap", now=now)
    config.max_groups_total = 1
    group = Group(group_id=920_000, title="already joined")
    test_db.add(group)
    await test_db.flush()
    test_db.add(GroupAccountMembership(group_id=group.id, telegram_group_id=group.group_id, account_id=account.id, status="joined", review_status="approved"))
    await test_db.commit()
    with pytest.raises(JoinBudgetBlocked, match="total_group_quota"):
        await _service(test_db).reserve(account.id, target_key="https://t.me/another_group", source="manual_group_link_join", require_auto_join_enabled=False, now=now)


async def test_status_does_not_clear_longer_existing_wait(test_db):
    now = datetime(2026, 9, 21, 12, 0, 0)
    account, config = await _eligible_account(test_db, "read-only-status", now=now)
    expected = now + timedelta(days=7)
    config.next_join_after = expected
    await test_db.commit()
    await JoinRequestBudgetService(test_db).status(account.id, now=now)
    await test_db.refresh(config)
    assert config.next_join_after == expected
