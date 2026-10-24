from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account import session_exec
from app.core.account.models import (
    AccountSpamCheckItem,
    AccountSpamCheckOperation,
    AccountStatus,
    AccountType,
    SpamCheckAccountStatus,
    SpamCheckItemStatus,
    SpamCheckOperationStatus,
    TelegramAccount,
)
from app.core.account.operation_lease import AccountOperationLeaseBusy
from app.modules.account_spam import service as service_module
from app.modules.account_spam.service import (
    SPAM_CHECK_OWNER_STAGE,
    AccountSpamCheckService,
    execute_forwarded_spam_check,
)


async def make_claim(db, *, attempts=1):
    account = TelegramAccount(
        identifier="spam-check-session-owner",
        session_name="spam-check-session-owner",
        session_string="test-session",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        spam_check_status=SpamCheckAccountStatus.CHECKING.value,
    )
    db.add(account)
    await db.flush()
    operation = AccountSpamCheckOperation(
        idempotency_key="spam-check-session-owner",
        account_ids_snapshot=f"[{account.id}]",
        snapshot_hash="f" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=1,
        max_attempts=3,
    )
    db.add(operation)
    await db.flush()
    item = AccountSpamCheckItem(
        operation_id=operation.id,
        account_id=account.id,
        status=SpamCheckItemStatus.IN_PROGRESS.value,
        attempts=attempts,
        lease_id="owner-claim",
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.add(item)
    await db.commit()
    return account, operation, item


def fake_pool():
    return SimpleNamespace(
        peek_connected=lambda account_id: True,
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(),
        release=AsyncMock(),
    )


def remote_owner(monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=object()))
    monkeypatch.setattr(session_exec, "owner_id", AsyncMock(return_value="listener-process"))


@pytest.mark.asyncio
async def test_check_forwards_claim_without_holding_db_or_creating_second_client(
    test_db, monkeypatch
):
    account, operation, item = await make_claim(test_db)
    account_id, item_id = account.id, item.id
    pool = fake_pool()
    execution = SimpleNamespace(check_spambot_status=AsyncMock())
    service = AccountSpamCheckService(test_db, account_pool=pool, telegram_execution=execution)
    remote_owner(monkeypatch)

    async def owner_reply(account_id, stage, **kwargs):
        assert not test_db.in_transaction()
        await service._finish_result(
            item_id, "owner-claim", "clear", "Good news, no limits are currently applied."
        )
        return {"result": {"processed": 1}, "interval": 30}

    forward = AsyncMock(side_effect=owner_reply)
    monkeypatch.setattr(session_exec, "forward", forward)
    await service._execute_claim(item_id, "owner-claim")

    await test_db.refresh(account)
    await test_db.refresh(operation)
    await test_db.refresh(item)
    assert item.result == "clear"
    assert item.attempts == 1
    assert account.spam_check_status == SpamCheckAccountStatus.CLEAR.value
    assert operation.status == SpamCheckOperationStatus.SUCCEEDED.value
    forward.assert_awaited_once_with(
        account_id,
        f"{SPAM_CHECK_OWNER_STAGE}:{item_id}:owner-claim",
        timeout=service_module.SPAM_CHECK_OWNER_TIMEOUT_SECONDS,
    )
    pool.add_account_from_db.assert_not_awaited()
    pool.acquire_by_id.assert_not_awaited()
    execution.check_spambot_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_owner_defers_without_exhausting_check_attempts(test_db, monkeypatch):
    account, operation, item = await make_claim(test_db, attempts=3)
    item_id = item.id
    pool = fake_pool()
    service = AccountSpamCheckService(test_db, account_pool=pool)
    remote_owner(monkeypatch)
    monkeypatch.setattr(session_exec, "forward", AsyncMock(return_value={"error": "not_owner"}))

    await service._execute_claim(item_id, "owner-claim")

    for model in (account, operation, item):
        await test_db.refresh(model)
    assert item.status == SpamCheckItemStatus.RETRY_WAIT.value
    assert item.attempts == 2
    assert item.reason_code == "account_operation_busy"
    assert item.lease_id is None
    assert item.next_retry_at > datetime.utcnow()
    assert operation.status == SpamCheckOperationStatus.RUNNING.value
    assert account.spam_check_status == SpamCheckAccountStatus.QUEUED.value
    pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_timeout_does_not_open_another_telegram_connection(test_db, monkeypatch):
    account, operation, item = await make_claim(test_db)
    item_id = item.id
    pool = fake_pool()
    service = AccountSpamCheckService(test_db, account_pool=pool)
    remote_owner(monkeypatch)
    monkeypatch.setattr(session_exec, "forward", AsyncMock(return_value=None))

    await service._execute_claim(item_id, "owner-claim")

    await test_db.refresh(item)
    assert item.status == SpamCheckItemStatus.RETRY_WAIT.value
    assert item.response_summary == "spambot_owner_unreachable"
    pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_reply_does_not_overwrite_the_owners_completed_verdict(test_db, monkeypatch):
    account, operation, item = await make_claim(test_db)
    item_id = item.id
    service = AccountSpamCheckService(test_db, account_pool=fake_pool())
    remote_owner(monkeypatch)

    async def lost_reply(*args, **kwargs):
        await service._finish_result(
            item_id, "owner-claim", "clear", "Good news, no limits are currently applied."
        )
        return None

    monkeypatch.setattr(session_exec, "forward", AsyncMock(side_effect=lost_reply))
    await service._execute_claim(item_id, "owner-claim")

    await test_db.refresh(item)
    await test_db.refresh(account)
    assert item.status == SpamCheckItemStatus.SUCCEEDED.value
    assert item.result == "clear"
    assert account.spam_check_status == SpamCheckAccountStatus.CLEAR.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AccountOperationLeaseBusy("account operation lease busy"),
        RuntimeError("Telegram session lease is busy for account 1"),
    ],
)
async def test_connection_ownership_race_waits_without_spending_attempts(
    test_db, monkeypatch, error
):
    account, operation, item = await make_claim(test_db, attempts=3)
    pool = fake_pool()
    pool.acquire_by_id.side_effect = error
    execution = SimpleNamespace(check_spambot_status=AsyncMock())
    service = AccountSpamCheckService(test_db, account_pool=pool, telegram_execution=execution)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=object()))
    monkeypatch.setattr(session_exec, "owner_id", AsyncMock(return_value=None))

    await service._execute_claim(item.id, "owner-claim")

    await test_db.refresh(item)
    await test_db.refresh(operation)
    assert item.attempts == 2
    assert item.status == SpamCheckItemStatus.RETRY_WAIT.value
    assert operation.failed_accounts == 0
    execution.check_spambot_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_reuses_pool_and_persists_official_result(test_db, monkeypatch):
    account, operation, item = await make_claim(test_db)
    pool = fake_pool()
    wrapper = SimpleNamespace(account_id=account.id, client=object())
    pool.acquire_by_id.return_value = wrapper
    execution = SimpleNamespace(
        check_spambot_status=AsyncMock(
            return_value="Good news, no limits are currently applied to your account."
        )
    )
    monkeypatch.setattr(service_module, "TelegramExecutionService", lambda guard: execution)
    forward = AsyncMock()
    monkeypatch.setattr(session_exec, "forward", forward)

    result, interval = await execute_forwarded_spam_check(
        test_db,
        account.id,
        f"{SPAM_CHECK_OWNER_STAGE}:{item.id}:owner-claim",
        account_pool=pool,
    )

    await test_db.refresh(account)
    await test_db.refresh(operation)
    await test_db.refresh(item)
    assert result == {"processed": 1}
    assert item.result == "clear"
    assert account.spam_check_status == SpamCheckAccountStatus.CLEAR.value
    assert operation.status == SpamCheckOperationStatus.SUCCEEDED.value
    execution.check_spambot_status.assert_awaited_once_with(
        wrapper, source="account_spam_check", attempt_key=f"diagnostic:spam-check:{item.id}"
    )
    pool.release.assert_awaited_once_with(wrapper)
    forward.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["account", "lease", "expired", "completed", "disconnected"])
async def test_owner_rejects_delayed_or_mismatched_claims(test_db, monkeypatch, changed):
    account, operation, item = await make_claim(test_db)
    account_id, item_id = account.id, item.id
    if changed == "account":
        account_id += 1
    elif changed == "lease":
        item.lease_id = "newer-claim"
    elif changed == "expired":
        item.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif changed == "completed":
        item.status = SpamCheckItemStatus.SUCCEEDED.value
    await test_db.commit()
    execute = AsyncMock()
    monkeypatch.setattr(AccountSpamCheckService, "_execute_claim", execute)
    pool = fake_pool()
    if changed == "disconnected":
        pool.peek_connected = lambda account_id: False

    result, interval = await execute_forwarded_spam_check(
        test_db,
        account_id,
        f"{SPAM_CHECK_OWNER_STAGE}:{item_id}:owner-claim",
        account_pool=pool,
    )

    reason = (
        "spam_check_owner_disconnected" if changed == "disconnected" else "stale_spam_check_claim"
    )
    assert result == {"processed": 0, "reason": reason}
    execute.assert_not_awaited()
