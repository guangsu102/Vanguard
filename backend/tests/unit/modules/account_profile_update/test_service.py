from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telethon.errors import FrozenMethodInvalidError, UserRestrictedError

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountProfileUpdateItem,
    AccountProfileUpdateItemStatus,
    AccountProfileUpdateOperation,
    AccountProfileUpdateOperationStatus,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.telegram_execution import TelegramExecutionError
from app.modules.account_profile_update.service import (
    AccountProfileUpdateService,
    account_is_profile_update_eligible,
    create_profile_update_operation,
    operation_snapshot,
    safe_worker_error,
    terminal_profile_failure_reason,
)


class FakeAccountPool:
    def __init__(self) -> None:
        self.wrapper = SimpleNamespace()
        self.added: list[int] = []
        self.acquired: list[int] = []
        self.released = 0

    async def add_account_from_db(self, account: TelegramAccount) -> None:
        self.added.append(account.id)

    async def acquire_by_id(self, account_id: int, **_kwargs):
        self.acquired.append(account_id)
        return self.wrapper

    async def release(self, wrapper) -> None:
        assert wrapper is self.wrapper
        self.released += 1


class FakeTelegramExecution:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def update_profile_bio(self, _wrapper, bio: str, *, source: str) -> bool:
        self.calls.append((bio, source))
        if self.error is not None:
            raise self.error
        return True


async def _seed_accounts(test_db, count: int) -> list[TelegramAccount]:
    accounts: list[TelegramAccount] = []
    for index in range(count):
        account = TelegramAccount(
            identifier=f"profile-update-{index}",
            session_name=f"profile-update-{index}",
            session_string=f"session-{index}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            profile_bio=f"old bio {index}",
        )
        test_db.add(account)
        accounts.append(account)
    await test_db.flush()
    test_db.add_all(
        [
            AccountOperationConfig(
                account_id=account.id,
                operation_mode=AccountOperationMode.AD_ONLY.value,
            )
            for account in accounts
        ]
    )
    await test_db.commit()
    return (
        await test_db.execute(
            select(TelegramAccount)
            .options(selectinload(TelegramAccount.operation_config))
            .where(TelegramAccount.id.in_([account.id for account in accounts]))
            .order_by(TelegramAccount.id.asc())
        )
    ).scalars().all()


async def _create_operation(
    test_db,
    accounts: list[TelegramAccount],
    *,
    profile_bio: str = "new public bio",
    key: str = "profile-update-idempotency-key",
) -> AccountProfileUpdateOperation:
    operation, created = await create_profile_update_operation(
        test_db,
        account_ids=[account.id for account in accounts],
        profile_bio=profile_bio,
        idempotency_key=key,
        created_by_id=1,
        accounts=accounts,
    )
    assert created is True
    await test_db.commit()
    return operation


@pytest.mark.asyncio
async def test_create_operation_is_idempotent_and_does_not_change_local_profile(test_db):
    accounts = await _seed_accounts(test_db, 1)
    first = await _create_operation(test_db, accounts)
    second, created = await create_profile_update_operation(
        test_db,
        account_ids=[accounts[0].id],
        profile_bio="new public bio",
        idempotency_key="profile-update-idempotency-key",
        created_by_id=1,
        accounts=accounts,
    )

    await test_db.refresh(accounts[0])
    assert created is False
    assert second.id == first.id
    assert accounts[0].profile_bio == "old bio 0"
    _, first_hash = operation_snapshot([accounts[0].id], "new public bio")
    _, second_hash = operation_snapshot([accounts[0].id], "different public bio")
    assert first_hash != second_hash
    with pytest.raises(ValueError, match="idempotency_key_conflict"):
        await create_profile_update_operation(
            test_db,
            account_ids=[accounts[0].id],
            profile_bio="different public bio",
            idempotency_key="profile-update-idempotency-key",
            created_by_id=1,
            accounts=accounts,
        )


@pytest.mark.asyncio
async def test_tick_never_executes_more_than_one_real_update(test_db):
    accounts = await _seed_accounts(test_db, 2)
    operation = await _create_operation(test_db, accounts)
    operation_id = operation.id
    pool = FakeAccountPool()
    execution = FakeTelegramExecution()

    result = await AccountProfileUpdateService(
        test_db,
        account_pool=pool,
        telegram_execution=execution,
    ).run_tick(limit=999)

    items = (
        await test_db.execute(
            select(AccountProfileUpdateItem)
            .where(AccountProfileUpdateItem.operation_id == operation_id)
            .order_by(AccountProfileUpdateItem.id.asc())
        )
    ).scalars().all()
    await test_db.refresh(operation)
    await test_db.refresh(accounts[0])
    await test_db.refresh(accounts[1])
    assert result["processed"] == 1
    assert result["queue_busy"] == 0
    assert execution.calls == [("new public bio", "account_profile_update_batch")]
    assert pool.released == 1
    assert items[0].status == AccountProfileUpdateItemStatus.SUCCEEDED.value
    assert items[1].status == AccountProfileUpdateItemStatus.PENDING.value
    assert accounts[0].profile_bio == "new public bio"
    assert accounts[1].profile_bio == "old bio 1"
    assert operation.status == AccountProfileUpdateOperationStatus.RUNNING.value


@pytest.mark.asyncio
async def test_execution_rechecks_profile_before_telegram_rpc(test_db):
    accounts = await _seed_accounts(test_db, 1)
    operation = await _create_operation(test_db, accounts)
    operation_id = operation.id
    accounts[0].profile_bio = "operator changed this after queueing"
    await test_db.commit()
    execution = FakeTelegramExecution()

    result = await AccountProfileUpdateService(
        test_db,
        account_pool=FakeAccountPool(),
        telegram_execution=execution,
    ).run_tick()

    item = await test_db.scalar(
        select(AccountProfileUpdateItem).where(
            AccountProfileUpdateItem.operation_id == operation_id
        )
    )
    await test_db.refresh(operation)
    assert result["processed"] == 0
    assert execution.calls == []
    assert item is not None
    assert item.status == AccountProfileUpdateItemStatus.SKIPPED.value
    assert item.reason_code == "profile_changed_since_queued"
    assert operation.status == AccountProfileUpdateOperationStatus.FAILED.value


@pytest.mark.asyncio
async def test_risk_block_retries_then_cancellation_stops_pending_item(test_db):
    accounts = await _seed_accounts(test_db, 1)
    operation = await _create_operation(test_db, accounts)
    operation_id = operation.id
    execution = FakeTelegramExecution(
        TelegramExecutionError("risk_guard_blocked:profile_update_cooldown")
    )
    service = AccountProfileUpdateService(
        test_db,
        account_pool=FakeAccountPool(),
        telegram_execution=execution,
    )

    await service.run_tick()
    item = await test_db.scalar(
        select(AccountProfileUpdateItem).where(
            AccountProfileUpdateItem.operation_id == operation_id
        )
    )
    assert item is not None
    assert item.status == AccountProfileUpdateItemStatus.RETRY_WAIT.value
    assert item.attempts == 1
    assert item.reason_code == "risk_guard_blocked"
    assert item.next_retry_at is not None and item.next_retry_at > datetime.utcnow()
    operation = await test_db.get(AccountProfileUpdateOperation, operation_id)
    assert operation is not None

    operation.cancel_requested_at = datetime.utcnow()
    operation.status = AccountProfileUpdateOperationStatus.CANCELLING.value
    await test_db.commit()
    result = await service.run_tick()
    await test_db.refresh(operation)
    await test_db.refresh(item)
    assert result["cancelled"] == 1
    assert item.status == AccountProfileUpdateItemStatus.CANCELLED.value
    assert operation.status == AccountProfileUpdateOperationStatus.CANCELLED.value


def test_all_active_promoters_with_a_session_are_eligible():
    promoter = SimpleNamespace(
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="present",
        session_name="present",
        operation_config=SimpleNamespace(
            operation_mode=AccountOperationMode.AD_ONLY.value
        ),
    )
    growth = SimpleNamespace(
        **{**promoter.__dict__, "operation_config": SimpleNamespace(operation_mode="growth")}
    )
    assert account_is_profile_update_eligible(promoter) is True
    # Operation mode no longer gates profile updates: growth accounts qualify too.
    assert account_is_profile_update_eligible(growth) is True
    restricted = SimpleNamespace(
        **{**promoter.__dict__, "status": AccountStatus.RESTRICTED}
    )
    assert account_is_profile_update_eligible(restricted) is True
    inactive = SimpleNamespace(**{**promoter.__dict__, "is_active": False})
    assert account_is_profile_update_eligible(inactive) is False
    missing_session = SimpleNamespace(**{**promoter.__dict__, "session_string": None})
    assert account_is_profile_update_eligible(missing_session) is False


@pytest.mark.parametrize(("error", "reason"), [
    (FrozenMethodInvalidError(None), "telegram_account_frozen"),
    (UserRestrictedError(None), "telegram_account_restricted"),
    (RuntimeError("Your account is frozen; session=private-value"), "telegram_account_frozen"),
    (TelegramExecutionError("risk_guard_blocked:account_restricted"), "risk_guard_account_restricted"),
])
@pytest.mark.asyncio
async def test_explicit_refusal_fails_once_preserving_account_state(test_db, error, reason):
    accounts = await _seed_accounts(test_db, 1)
    account = accounts[0]
    account.status = AccountStatus.RESTRICTED
    account.risk_level = "frozen"
    account.spam_check_status = "restricted"
    await test_db.commit()
    operation = await _create_operation(test_db, accounts, profile_bio="")
    operation_id, account_id = operation.id, account.id
    execution = FakeTelegramExecution(error)
    pool = FakeAccountPool()
    service = AccountProfileUpdateService(test_db, account_pool=pool, telegram_execution=execution)

    result = await service.run_tick()
    item = await test_db.scalar(select(AccountProfileUpdateItem).where(
        AccountProfileUpdateItem.operation_id == operation_id))
    await test_db.refresh(operation)
    await test_db.refresh(account)
    assert result["processed"] == 1
    assert item.status == AccountProfileUpdateItemStatus.FAILED.value
    assert item.reason_code == reason
    assert item.attempts == 1 and item.next_retry_at is None
    assert item.finished_at is not None and item.remote_attempted_at is not None
    assert operation.status == AccountProfileUpdateOperationStatus.FAILED.value
    assert operation.failed_accounts == 1 and operation.succeeded_accounts == 0
    assert account.status == AccountStatus.RESTRICTED
    assert account.risk_level == "frozen" and account.spam_check_status == "restricted"
    assert account.profile_bio == "old bio 0" and account.profile_bio_synced_at is None
    assert "private-value" not in item.error_message
    assert pool.acquired == [account_id]
    await service.run_tick()
    assert len(execution.calls) == 1


@pytest.mark.parametrize("message", [
    "risk_guard_blocked:profile_update_daily_budget",
    "risk_guard_blocked:profile_update_cooldown",
    "risk_guard_blocked:account_restricted_unknown",
    "risk_guard_blocked:telegram_temporarily_unavailable",
    "account is not frozen",
    "TimeoutError: profile update failed",
    "FloodWaitError: profile update failed",
])
def test_temporary_or_unconfirmed_errors_are_not_frozen(message):
    assert terminal_profile_failure_reason(message) is None


def test_safe_error_records_class_and_terminal_reason_without_raw_rpc_text():
    error = RuntimeError("Your account is frozen; secret=private token")
    assert safe_worker_error(error) == "RuntimeError: profile update failed [telegram_account_frozen]"
    assert terminal_profile_failure_reason(safe_worker_error(error)) == "telegram_account_frozen"
    assert safe_worker_error(FrozenMethodInvalidError(None)).startswith("FrozenMethodInvalidError:")


@pytest.mark.asyncio
async def test_reconcile_existing_refusals_without_rpc_or_attempt_increment(test_db):
    accounts = await _seed_accounts(test_db, 3)
    for account in accounts:
        account.status = AccountStatus.RESTRICTED
        account.risk_level = "frozen"
    await test_db.commit()
    operation = await _create_operation(test_db, accounts, profile_bio="")
    operation_id = operation.id
    next_operation = await _create_operation(test_db, [accounts[0]], key="another-operation")
    next_operation_id = next_operation.id
    items = list((await test_db.scalars(select(AccountProfileUpdateItem).where(
        AccountProfileUpdateItem.operation_id == operation_id).order_by(AccountProfileUpdateItem.id))).all())
    stamp = datetime.utcnow() - timedelta(hours=1)
    for item, error in zip(items, [
        "FrozenMethodInvalidError: profile update failed",
        "risk_guard_blocked:account_restricted",
        "TimeoutError: profile update failed",
    ], strict=True):
        item.status = AccountProfileUpdateItemStatus.RETRY_WAIT.value
        item.attempts = 1
        item.remote_attempted_at = stamp
        item.next_retry_at = datetime.utcnow() + timedelta(days=1)
        item.error_message = error
        item.reason_code = "telegram_temporarily_unavailable"
    await test_db.commit()
    pool, execution = FakeAccountPool(), FakeTelegramExecution()
    service = AccountProfileUpdateService(test_db, account_pool=pool, telegram_execution=execution)
    assert await service.reconcile_terminal_failures(operation_id=operation_id) == 2
    for item in items:
        await test_db.refresh(item)
    assert [item.status for item in items] == ["failed", "failed", "retry_wait"]
    assert [item.reason_code for item in items[:2]] == [
        "telegram_account_frozen", "risk_guard_account_restricted"]
    assert all(item.attempts == 1 and item.remote_attempted_at == stamp for item in items)
    assert items[2].next_retry_at is not None
    await test_db.refresh(operation)
    assert operation.failed_accounts == 2 and operation.succeeded_accounts == 0
    assert operation.status == "running"
    assert execution.calls == [] and pool.acquired == []
    assert await service.reconcile_terminal_failures(operation_id=operation_id) == 0
    await test_db.refresh(next_operation)
    assert next_operation.id == next_operation_id and next_operation.status == "queued"
    for account in accounts:
        await test_db.refresh(account)
        assert account.status == AccountStatus.RESTRICTED and account.risk_level == "frozen"
        assert account.profile_bio_synced_at is None and account.profile_bio.startswith("old bio")


@pytest.mark.asyncio
async def test_tick_reconciles_old_frozen_retry_and_moves_to_next_operation(test_db):
    accounts = await _seed_accounts(test_db, 2)
    first = await _create_operation(test_db, [accounts[0]])
    first_id = first.id
    second = await _create_operation(test_db, [accounts[1]], key="next-profile-operation")
    first_item = await test_db.scalar(select(AccountProfileUpdateItem).where(
        AccountProfileUpdateItem.operation_id == first_id))
    first_item.status = "retry_wait"
    first_item.attempts = 1
    first_item.error_message = "FrozenMethodInvalidError: profile update failed"
    first_item.next_retry_at = datetime.utcnow() + timedelta(days=1)
    await test_db.commit()
    execution = FakeTelegramExecution()
    service = AccountProfileUpdateService(test_db, account_pool=FakeAccountPool(), telegram_execution=execution)
    result = await service.run_tick()
    await test_db.refresh(first)
    await test_db.refresh(second)
    await test_db.refresh(first_item)
    assert result["terminal_reconciled"] == 1 and result["processed"] == 1
    assert first.status == "failed" and first_item.status == "failed"
    assert first_item.attempts == 1 and first_item.reason_code == "telegram_account_frozen"
    assert second.status == "succeeded"
    assert len(execution.calls) == 1


@pytest.mark.asyncio
async def test_terminal_reconciliation_ignores_live_claims(test_db):
    accounts = await _seed_accounts(test_db, 1)
    operation = await _create_operation(test_db, accounts)
    operation_id = operation.id
    item = await test_db.scalar(select(AccountProfileUpdateItem).where(
        AccountProfileUpdateItem.operation_id == operation_id))
    item.status = "in_progress"
    item.lease_id = "live-claim"
    item.error_message = "FrozenMethodInvalidError: profile update failed"
    item.lease_expires_at = datetime.utcnow() + timedelta(minutes=10)
    await test_db.commit()
    execution = FakeTelegramExecution()
    service = AccountProfileUpdateService(test_db, account_pool=FakeAccountPool(), telegram_execution=execution)
    assert await service.reconcile_terminal_failures(operation_id=operation_id) == 0
    await test_db.refresh(item)
    assert item.status == "in_progress" and item.lease_id == "live-claim"
    assert execution.calls == []
