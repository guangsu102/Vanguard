from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.account_spam import AccountSpamCheckCreateRequest
from app.core.account.models import (
    AccountOperationConfig,
    AccountRiskLevel,
    AccountSpamCheckItem,
    AccountSpamCheckOperation,
    AccountStatus,
    AccountType,
    SpamCheckAccountStatus,
    SpamCheckItemStatus,
    SpamCheckOperationStatus,
    TelegramAccount,
)
from app.core.account.risk_guard import (
    AccountRiskAction,
    AccountRiskGuard,
    RiskDecision,
)
from app.modules.account_spam.service import AccountSpamCheckService
from app.modules.acquisition.models import AccountAdBinding, AdCampaign


@pytest.mark.asyncio
async def test_spambot_clear_does_not_remove_telegram_rpc_restriction(test_db):
    account = TelegramAccount(
        identifier="rpc-restricted-spam-check",
        session_name="rpc-restricted-spam-check",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.RESTRICTED,
        is_active=True,
        restriction_previous_status=AccountStatus.ONLINE.value,
        restriction_source="telegram_rpc",
        restriction_reason="user_restricted",
        restriction_detected_at=datetime.utcnow(),
        spam_check_status=SpamCheckAccountStatus.CHECKING.value,
    )
    test_db.add(account)
    await test_db.flush()
    operation = AccountSpamCheckOperation(
        idempotency_key="test-rpc-clear-does-not-restore",
        account_ids_snapshot=f"[{account.id}]",
        snapshot_hash="a" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=1,
    )
    test_db.add(operation)
    await test_db.flush()
    item = AccountSpamCheckItem(
        operation_id=operation.id,
        account_id=account.id,
        status=SpamCheckItemStatus.IN_PROGRESS.value,
        attempts=1,
        lease_id="lease-rpc-clear",
    )
    test_db.add(item)
    await test_db.commit()

    service = AccountSpamCheckService(
        test_db,
        account_pool=SimpleNamespace(),
        telegram_execution=SimpleNamespace(),
    )
    await service._finish_result(
        item.id,
        "lease-rpc-clear",
        "clear",
        "Good news, no limits are currently applied.",
    )

    await test_db.refresh(account)
    await test_db.refresh(operation)
    await test_db.refresh(item)
    assert account.spam_check_status == SpamCheckAccountStatus.CLEAR.value
    assert account.status == AccountStatus.RESTRICTED
    assert account.restriction_source == "telegram_rpc"
    assert account.restriction_reason == "user_restricted"
    assert item.status == SpamCheckItemStatus.SUCCEEDED.value
    assert item.result == "clear"
    assert operation.status == SpamCheckOperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_spambot_restricted_disables_existing_join_and_ad_automation(test_db):
    account = TelegramAccount(
        identifier="spambot-disables-automation",
        session_name="spambot-disables-automation",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        spam_check_status=SpamCheckAccountStatus.CHECKING.value,
    )
    campaign = AdCampaign(
        name="SpamBot Restricted Disable Campaign",
        enabled=True,
        status="active",
    )
    test_db.add_all([account, campaign])
    await test_db.flush()

    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_join_enabled=True,
        auto_ads_enabled=True,
    )
    operation = AccountSpamCheckOperation(
        idempotency_key="test-spambot-restricted-disables-automation",
        account_ids_snapshot=f"[{account.id}]",
        snapshot_hash="b" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=1,
    )
    test_db.add_all([config, operation])
    await test_db.flush()

    item = AccountSpamCheckItem(
        operation_id=operation.id,
        account_id=account.id,
        status=SpamCheckItemStatus.IN_PROGRESS.value,
        attempts=1,
        lease_id="lease-spambot-restricted",
    )
    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        enabled=True,
    )
    test_db.add_all([item, binding])
    await test_db.commit()

    service = AccountSpamCheckService(
        test_db,
        account_pool=SimpleNamespace(),
        telegram_execution=SimpleNamespace(),
    )
    await service._finish_result(
        item.id,
        "lease-spambot-restricted",
        "restricted",
        "Sorry, your account is limited.",
    )

    for model in (account, config, binding, item, operation):
        await test_db.refresh(model)
    assert account.status == AccountStatus.RESTRICTED
    assert account.spam_check_status == SpamCheckAccountStatus.RESTRICTED.value
    assert config.enabled is False
    assert config.auto_join_enabled is False
    assert config.auto_ads_enabled is False
    assert binding.enabled is False
    assert item.status == SpamCheckItemStatus.SUCCEEDED.value
    assert operation.status == SpamCheckOperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_spambot_flagged_keeps_account_usable_and_untouched(test_db):
    account = TelegramAccount(
        identifier="spambot-flagged-online",
        session_name="spambot-flagged-online",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        spam_check_status=SpamCheckAccountStatus.CHECKING.value,
    )
    test_db.add(account)
    await test_db.flush()
    operation = AccountSpamCheckOperation(
        idempotency_key="test-spambot-flagged-keeps-usable",
        account_ids_snapshot=f"[{account.id}]",
        snapshot_hash="e" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=1,
    )
    test_db.add(operation)
    await test_db.flush()
    item = AccountSpamCheckItem(
        operation_id=operation.id,
        account_id=account.id,
        status=SpamCheckItemStatus.IN_PROGRESS.value,
        attempts=1,
        lease_id="lease-spambot-flagged",
    )
    test_db.add(item)
    await test_db.commit()

    service = AccountSpamCheckService(
        test_db,
        account_pool=SimpleNamespace(),
        telegram_execution=SimpleNamespace(),
    )
    await service._finish_result(
        item.id,
        "lease-spambot-flagged",
        "flagged",
        (
            "Unfortunately, some phone numbers may trigger a harsh response from "
            "our anti-spam systems."
        ),
    )

    await test_db.refresh(account)
    await test_db.refresh(operation)
    await test_db.refresh(item)
    assert account.spam_check_status == SpamCheckAccountStatus.FLAGGED.value
    assert account.status == AccountStatus.ONLINE
    assert account.restriction_source is None
    assert account.spam_restriction_confirmed_at is None
    assert item.status == SpamCheckItemStatus.SUCCEEDED.value
    assert item.result == "flagged"
    assert item.reason_code == "spambot_flagged"
    assert operation.status == SpamCheckOperationStatus.SUCCEEDED.value
    assert operation.failed_accounts == 0


@pytest.mark.asyncio
async def test_spambot_flagged_does_not_clear_existing_spambot_restriction(test_db):
    confirmed_at = datetime.utcnow()
    account = TelegramAccount(
        identifier="spambot-flagged-restricted",
        session_name="spambot-flagged-restricted",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.RESTRICTED,
        is_active=True,
        restriction_previous_status=AccountStatus.ONLINE.value,
        restriction_source="spambot",
        restriction_reason="spambot_restricted",
        restriction_detected_at=confirmed_at,
        spam_restriction_confirmed_at=confirmed_at,
        spam_check_status=SpamCheckAccountStatus.CHECKING.value,
    )
    test_db.add(account)
    await test_db.flush()
    operation = AccountSpamCheckOperation(
        idempotency_key="test-spambot-flagged-keeps-restriction",
        account_ids_snapshot=f"[{account.id}]",
        snapshot_hash="0" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=1,
    )
    test_db.add(operation)
    await test_db.flush()
    item = AccountSpamCheckItem(
        operation_id=operation.id,
        account_id=account.id,
        status=SpamCheckItemStatus.IN_PROGRESS.value,
        attempts=1,
        lease_id="lease-spambot-flagged-restricted",
    )
    test_db.add(item)
    await test_db.commit()

    service = AccountSpamCheckService(
        test_db,
        account_pool=SimpleNamespace(),
        telegram_execution=SimpleNamespace(),
    )
    await service._finish_result(
        item.id,
        "lease-spambot-flagged-restricted",
        "flagged",
        (
            "Unfortunately, some phone numbers may trigger a harsh response from "
            "our anti-spam systems."
        ),
    )

    await test_db.refresh(account)
    assert account.spam_check_status == SpamCheckAccountStatus.FLAGGED.value
    assert account.status == AccountStatus.RESTRICTED
    assert account.restriction_source == "spambot"
    assert account.restriction_reason == "spambot_restricted"
    assert account.restriction_previous_status == AccountStatus.ONLINE.value
    assert account.spam_restriction_confirmed_at == confirmed_at


@pytest.mark.asyncio
async def test_restricted_frozen_account_can_run_dedicated_spambot_check(
    test_db,
    monkeypatch,
):
    from app.core.account import risk_guard as risk_guard_module

    account = TelegramAccount(
        identifier="rpc-restricted-frozen",
        session_name="rpc-restricted-frozen",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.RESTRICTED,
        is_active=True,
        restriction_source="telegram_rpc",
        restriction_reason="user_restricted",
        risk_level=AccountRiskLevel.QUARANTINED.value,
        risk_reason="account_restricted",
        risk_pause_until=datetime.utcnow() + timedelta(hours=12),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    monkeypatch.setattr(
        risk_guard_module,
        "get_account_risk_guard_settings",
        AsyncMock(return_value={"enabled": True, "actions": {}}),
    )
    guard = AccountRiskGuard(test_db)
    guard._apply_risk_lifecycle = AsyncMock()
    guard._check_content_policy = AsyncMock(return_value=RiskDecision(True))
    guard._reserve_budget = AsyncMock(return_value=(True, "reserved", None))
    guard.record_event = AsyncMock()
    guard._block = AsyncMock(return_value=RiskDecision(False, "blocked"))

    decision = await guard.check_and_reserve(
        SimpleNamespace(account_id=account.id),
        AccountRiskAction.SPAM_CHECK,
        target_type="official_bot",
        target_id=178220800,
    )

    assert decision.allowed is True
    guard._block.assert_not_awaited()
    reserve_call = guard._reserve_budget.await_args
    assert reserve_call.args[1] == AccountRiskAction.SPAM_CHECK
    assert reserve_call.args[2].daily_limit == 3
    assert reserve_call.args[2].cooldown_seconds == 300


def test_spam_check_request_accepts_2000_unique_accounts_and_rejects_more():
    request = AccountSpamCheckCreateRequest(account_ids=list(range(1, 2001)))
    assert len(request.account_ids) == 2000

    with pytest.raises(ValidationError):
        AccountSpamCheckCreateRequest(account_ids=list(range(1, 2002)))

@pytest.mark.asyncio
async def test_user_restricted_rpc_marks_account_and_preserves_previous_status(
    test_db,
    monkeypatch,
):
    from app.core.account import pool as pool_module

    class UserRestrictedError(Exception):
        pass

    account = TelegramAccount(
        identifier="user-restricted-rpc",
        session_name="user-restricted-rpc",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    invalidate = AsyncMock(return_value=0)
    monkeypatch.setattr(pool_module, "invalidate_account_in_all_pools", invalidate)

    await AccountRiskGuard(test_db).record_failure(
        SimpleNamespace(account_id=account.id),
        AccountRiskAction.CHANNEL_CREATE,
        UserRestrictedError("USER_RESTRICTED"),
        target_type="account",
        target_id=account.id,
    )

    await test_db.refresh(account)
    assert account.status == AccountStatus.RESTRICTED
    assert account.restriction_previous_status == AccountStatus.ONLINE.value
    assert account.restriction_source == "telegram_rpc"
    assert account.restriction_reason == "user_restricted"
    assert account.restriction_detected_at is not None
    invalidate.assert_awaited_once_with(
        account.id,
        reason="telegram_rpc_user_restricted",
    )

@pytest.mark.asyncio
async def test_unrecognized_final_item_flushes_and_finalizes_partial_failed_operation(
    test_db,
):
    accounts = [
        TelegramAccount(
            identifier=f"spam-finalize-{index}",
            session_name=f"spam-finalize-{index}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            spam_check_status=(
                SpamCheckAccountStatus.CLEAR.value
                if index < 3
                else SpamCheckAccountStatus.CHECKING.value
            ),
        )
        for index in range(4)
    ]
    test_db.add_all(accounts)
    await test_db.flush()
    operation = AccountSpamCheckOperation(
        idempotency_key="test-unrecognized-finalizes-operation",
        account_ids_snapshot=str([account.id for account in accounts]),
        snapshot_hash="c" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=4,
        processed_accounts=3,
        clear_accounts=3,
    )
    test_db.add(operation)
    await test_db.flush()
    completed_items = [
        AccountSpamCheckItem(
            operation_id=operation.id,
            account_id=account.id,
            status=SpamCheckItemStatus.SUCCEEDED.value,
            result="clear",
            attempts=1,
            finished_at=datetime.utcnow(),
        )
        for account in accounts[:3]
    ]
    final_item = AccountSpamCheckItem(
        operation_id=operation.id,
        account_id=accounts[3].id,
        status=SpamCheckItemStatus.IN_PROGRESS.value,
        attempts=1,
        lease_id="lease-unrecognized-final",
    )
    test_db.add_all([*completed_items, final_item])
    await test_db.commit()

    service = AccountSpamCheckService(
        test_db,
        account_pool=SimpleNamespace(),
        telegram_execution=SimpleNamespace(),
    )
    await service._finish_unrecognized(
        final_item.id,
        "lease-unrecognized-final",
        "SpamBot introductory response without a current restriction verdict",
    )

    await test_db.refresh(operation)
    await test_db.refresh(final_item)
    assert final_item.status == SpamCheckItemStatus.FAILED.value
    assert final_item.reason_code == "response_unrecognized"
    assert final_item.next_retry_at is None
    assert final_item.finished_at is not None
    assert operation.processed_accounts == 4
    assert operation.clear_accounts == 3
    assert operation.failed_accounts == 1
    assert operation.status == SpamCheckOperationStatus.PARTIAL_FAILED.value
    assert operation.finished_at is not None


@pytest.mark.asyncio
async def test_tick_reconciles_preexisting_stuck_terminal_operation(test_db):
    accounts = [
        TelegramAccount(
            identifier=f"spam-stuck-{index}",
            session_name=f"spam-stuck-{index}",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        for index in range(2)
    ]
    test_db.add_all(accounts)
    await test_db.flush()
    operation = AccountSpamCheckOperation(
        idempotency_key="test-reconcile-stuck-terminal-operation",
        account_ids_snapshot=str([account.id for account in accounts]),
        snapshot_hash="d" * 64,
        status=SpamCheckOperationStatus.RUNNING.value,
        total_accounts=2,
        processed_accounts=1,
        clear_accounts=1,
    )
    test_db.add(operation)
    await test_db.flush()
    test_db.add_all(
        [
            AccountSpamCheckItem(
                operation_id=operation.id,
                account_id=accounts[0].id,
                status=SpamCheckItemStatus.SUCCEEDED.value,
                result="clear",
                attempts=1,
                finished_at=datetime.utcnow(),
            ),
            AccountSpamCheckItem(
                operation_id=operation.id,
                account_id=accounts[1].id,
                status=SpamCheckItemStatus.FAILED.value,
                reason_code="response_unrecognized",
                attempts=1,
                finished_at=datetime.utcnow(),
            ),
        ]
    )
    await test_db.commit()

    service = AccountSpamCheckService(
        test_db,
        account_pool=SimpleNamespace(),
        telegram_execution=SimpleNamespace(),
    )
    result = await service.run_tick(limit=1)

    await test_db.refresh(operation)
    assert result == {
        "processed": 0,
        "recovered": 0,
        "cancelled": 0,
        "reconciled": 1,
    }
    assert operation.processed_accounts == 2
    assert operation.clear_accounts == 1
    assert operation.failed_accounts == 1
    assert operation.status == SpamCheckOperationStatus.PARTIAL_FAILED.value
    assert operation.finished_at is not None
    assert await service.reconcile_completed_operations() == 0
