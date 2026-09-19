from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

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
