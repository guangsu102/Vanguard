"""Gradual app_version roll: outdated accounts move onto the current pool."""

from unittest.mock import AsyncMock

import pytest

from app.core.account import app_version_roll as roll_module
from app.core.account.app_version_roll import (
    _rolled_app_version,
    roll_outdated_app_versions,
)
from app.core.account.models import AccountType, TelegramAccount
from app.core.network.fingerprint import FingerprintManager


def _account(session_name: str, app_version: str | None) -> TelegramAccount:
    return TelegramAccount(
        identifier=session_name,
        session_name=session_name,
        account_type=AccountType.PROMOTER,
        status="offline",
        app_version=app_version,
    )


@pytest.mark.asyncio
async def test_outdated_app_version_is_rolled_onto_current_pool(test_db, monkeypatch):
    account = _account("roll-me", app_version="5.0.1 x64")
    test_db.add(account)
    await test_db.commit()
    invalidate = AsyncMock(return_value=1)
    monkeypatch.setattr(roll_module, "invalidate_account_in_all_pools", invalidate)

    result = await roll_outdated_app_versions(test_db, batch_size=5)

    await test_db.refresh(account)
    expected = _rolled_app_version(account)
    assert account.app_version == expected
    assert account.app_version in set().union(
        *FingerprintManager.TELEGRAM_APP_VERSIONS.values()
    )
    assert result["rolled_count"] == 1
    assert result["rolled"][0]["from"] == "5.0.1 x64"
    invalidate.assert_awaited_once_with(account.id, reason="app_version_rolled")


@pytest.mark.asyncio
async def test_current_app_version_is_left_alone(test_db):
    account = _account("already-current", app_version=None)
    account.app_version = _rolled_app_version(account)
    test_db.add(account)
    await test_db.commit()

    result = await roll_outdated_app_versions(test_db, batch_size=5)

    assert result["rolled_count"] == 0
    assert result["outdated_remaining"] == 0


@pytest.mark.asyncio
async def test_missing_app_version_counts_as_outdated(test_db, monkeypatch):
    account = _account("no-version", app_version=None)
    test_db.add(account)
    await test_db.commit()
    invalidate = AsyncMock(return_value=1)
    monkeypatch.setattr(roll_module, "invalidate_account_in_all_pools", invalidate)

    result = await roll_outdated_app_versions(test_db, batch_size=5)

    await test_db.refresh(account)
    assert account.app_version
    assert account.app_version in set().union(
        *FingerprintManager.TELEGRAM_APP_VERSIONS.values()
    )
    assert result["rolled_count"] == 1


def test_device_profile_exposes_os_type():
    profile = FingerprintManager().generate_telegram_device_profile("os-type-key")
    assert profile["os_type"] in {"windows", "macos", "android", "ios"}
