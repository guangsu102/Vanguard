from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api.accounts import _apply_auth_asset_tier, _normalize_account_asset_tier
from app.core.account.models import AccountAssetTier, TelegramAccount


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
