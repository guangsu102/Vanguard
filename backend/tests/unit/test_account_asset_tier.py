from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api.accounts import (
    AccountBatchImportRequest,
    CompleteLoginRequest,
    _apply_auth_asset_tier,
    _apply_onboarding_operation_mode,
    _normalize_account_asset_tier,
)
from app.core.account.models import (
    AccountAssetTier,
    AccountOperationConfig,
    AccountOperationMode,
    AccountType,
    TelegramAccount,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, AccountAssetTier.UNKNOWN.value),
        ("  year_3_plus  ", AccountAssetTier.YEAR_3_PLUS.value),
    ],
)
def test_normalize_account_asset_tier(value, expected):
    assert _normalize_account_asset_tier(value) == expected


def test_normalize_account_asset_tier_rejects_unknown_value():
    with pytest.raises(HTTPException, match="asset_tier must be one of"):
        _normalize_account_asset_tier("decade_old")


def test_auth_asset_tier_does_not_clear_existing_tier_with_unknown_default():
    verified_at = datetime(2026, 1, 1)
    account = TelegramAccount(
        asset_tier=AccountAssetTier.YEAR_2.value,
        asset_verified_at=verified_at,
    )

    _apply_auth_asset_tier(account, AccountAssetTier.UNKNOWN.value)

    assert account.asset_tier == AccountAssetTier.YEAR_2.value
    assert account.asset_verified_at == verified_at


def test_auth_asset_tier_updates_known_value_and_timestamp():
    account = TelegramAccount(asset_tier=AccountAssetTier.UNKNOWN.value)

    _apply_auth_asset_tier(account, AccountAssetTier.YEAR_3_PLUS.value)

    assert account.asset_tier == AccountAssetTier.YEAR_3_PLUS.value
    assert account.asset_verified_at is not None


def test_login_and_batch_import_requests_preserve_selected_operation_mode():
    login = CompleteLoginRequest(
        phone="+15550001001",
        session_string="session-value",
        operation_mode=AccountOperationMode.AD_ONLY.value,
    )
    batch = AccountBatchImportRequest(
        accounts=[
            {
                "phone": "+15550001002",
                "operation_mode": AccountOperationMode.AD_ONLY.value,
            }
        ]
    )

    assert login.operation_mode == AccountOperationMode.AD_ONLY.value
    assert (
        batch.accounts[0].operation_mode
        == AccountOperationMode.AD_ONLY.value
    )


@pytest.mark.asyncio
async def test_onboarding_role_creates_legacy_config_without_overwriting_existing_role(
    test_db,
):
    legacy = TelegramAccount(
        identifier="legacy-without-operation-config",
        session_name="legacy-without-operation-config",
        account_type=AccountType.PROMOTER,
    )
    existing = TelegramAccount(
        identifier="existing-growth-operation-config",
        session_name="existing-growth-operation-config",
        account_type=AccountType.PROMOTER,
    )
    existing_config = AccountOperationConfig(
        account=existing,
        operation_mode=AccountOperationMode.GROWTH.value,
    )
    test_db.add_all([legacy, existing, existing_config])
    await test_db.commit()

    created = await _apply_onboarding_operation_mode(
        test_db,
        legacy,
        AccountOperationMode.AD_ONLY.value,
    )
    await test_db.commit()

    assert created is not None
    assert created.operation_mode == AccountOperationMode.AD_ONLY.value
    with pytest.raises(
        HTTPException, match="existing_account_operation_mode_conflict"
    ):
        await _apply_onboarding_operation_mode(
            test_db,
            existing,
            AccountOperationMode.AD_ONLY.value,
        )
