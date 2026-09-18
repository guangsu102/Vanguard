from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.core.account.bot_credentials import resolve_guardian_bot_token
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotHealthStatus,
    GuardianBotProfile,
    ManagedBotProvision,
    TelegramAccount,
)
from app.core.ephemeral_secret import decrypt_ephemeral_secret
from app.modules.managed_bot_provision.profiles import (
    mark_managed_bot_profiles_verified,
    register_managed_bot_profiles,
)
from app.modules.managed_bot_provision.service import serialize_provision
from app.modules.owned_group.models_extra import OwnedBotProfile


@pytest.mark.asyncio
async def test_register_profiles_encrypts_both_tokens_and_is_idempotent(test_db):
    owner = TelegramAccount(
        identifier="+15550001111",
        phone="+15550001111",
        session_name="managed-bot-owner",
        session_string="owner-session",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    plaintext = "123456789:managed-bot-secret"

    first = await register_managed_bot_profiles(
        test_db,
        owner_account=owner,
        bot_user_id=880001,
        username="@Provisioned_Test_Bot",
        display_name="Provisioned Test",
        token=plaintext,
    )
    second = await register_managed_bot_profiles(
        test_db,
        owner_account=owner,
        bot_user_id=880001,
        username="provisioned_test_bot",
        display_name="Provisioned Test",
        token=plaintext,
    )

    assert first == second
    account = await test_db.get(TelegramAccount, first.account_id)
    guardian = await test_db.get(GuardianBotProfile, first.guardian_bot_profile_id)
    owned = await test_db.get(OwnedBotProfile, first.owned_bot_profile_id)
    assert account is not None
    assert guardian is not None
    assert owned is not None
    assert account.is_active is False
    assert guardian.enabled is False
    assert owned.enabled is False
    assert guardian.bot_token.startswith("vge1:")
    assert owned.token_ciphertext.startswith("vge1:")
    assert plaintext not in guardian.bot_token
    assert plaintext not in owned.token_ciphertext
    assert resolve_guardian_bot_token(guardian.bot_token) == plaintext
    assert decrypt_ephemeral_secret(owned.token_ciphertext) == plaintext
    assert await test_db.scalar(select(func.count(GuardianBotProfile.id))) == 1
    assert await test_db.scalar(select(func.count(OwnedBotProfile.id))) == 1

    await mark_managed_bot_profiles_verified(
        test_db,
        guardian_bot_profile_id=first.guardian_bot_profile_id,
        owned_bot_profile_id=first.owned_bot_profile_id,
        bot_user_id=880001,
        username="provisioned_test_bot",
        display_name="Provisioned Test",
    )
    assert account.is_active is True
    assert guardian.enabled is True
    assert owned.enabled is True
    assert guardian.health_status.value == "healthy"
    assert owned.status == "verified"


@pytest.mark.asyncio
async def test_replaced_token_requires_fresh_identity_verification(test_db):
    owner = TelegramAccount(
        identifier="+15550002222",
        phone="+15550002222",
        session_name="managed-bot-token-rotation-owner",
        session_string="owner-session",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()

    registered = await register_managed_bot_profiles(
        test_db,
        owner_account=owner,
        bot_user_id=880002,
        username="rotated_token_test_bot",
        display_name="Rotated Token Test",
        token="880002:old-managed-bot-secret",
    )
    account = await test_db.get(TelegramAccount, registered.account_id)
    guardian = await test_db.get(
        GuardianBotProfile,
        registered.guardian_bot_profile_id,
    )
    owned = await test_db.get(OwnedBotProfile, registered.owned_bot_profile_id)
    assert account is not None
    assert guardian is not None
    assert owned is not None
    assert account.is_active is False
    assert guardian.enabled is False
    assert owned.enabled is False

    await mark_managed_bot_profiles_verified(
        test_db,
        guardian_bot_profile_id=registered.guardian_bot_profile_id,
        owned_bot_profile_id=registered.owned_bot_profile_id,
        bot_user_id=880002,
        username="rotated_token_test_bot",
        display_name="Rotated Token Test",
    )
    assert account.is_active is True
    assert guardian.enabled is True
    assert owned.enabled is True
    assert guardian.health_status == GuardianBotHealthStatus.HEALTHY
    assert owned.status == "verified"

    replacement = "880002:replacement-managed-bot-secret"
    repeated = await register_managed_bot_profiles(
        test_db,
        owner_account=owner,
        bot_user_id=880002,
        username="rotated_token_test_bot",
        display_name="Rotated Token Test",
        token=replacement,
    )

    assert repeated == registered
    assert account.is_active is False
    assert guardian.enabled is False
    assert owned.enabled is False
    assert guardian.health_status == GuardianBotHealthStatus.UNKNOWN
    assert guardian.sync_status == "pending"
    assert owned.status == "pending_verification"
    assert resolve_guardian_bot_token(guardian.bot_token) == replacement
    assert decrypt_ephemeral_secret(owned.token_ciphertext) == replacement

    await mark_managed_bot_profiles_verified(
        test_db,
        guardian_bot_profile_id=registered.guardian_bot_profile_id,
        owned_bot_profile_id=registered.owned_bot_profile_id,
        bot_user_id=880002,
        username="rotated_token_test_bot",
        display_name="Rotated Token Test",
    )

    assert account.is_active is True
    assert guardian.enabled is True
    assert owned.enabled is True
    assert guardian.health_status == GuardianBotHealthStatus.HEALTHY
    assert owned.status == "verified"


def test_serialize_provision_has_stable_safe_contract():
    operation = ManagedBotProvision(
        id=42,
        idempotency_key="managed-provision-idempotency",
        request_hash="a" * 64,
        owner_account_id=10,
        manager_bot_profile_id=11,
        display_name="Safe Bot",
        username="safe_test_bot",
        status="running",
        current_step="fetch_token",
        error_code=None,
        error_message=None,
        retryable=False,
        guardian_bot_profile_id=None,
        owned_bot_profile_id=None,
        created_at=datetime(2026, 9, 13, 1, 2, 3),
        updated_at=datetime(2026, 9, 13, 1, 2, 4),
    )

    payload = serialize_provision(operation)

    required = {
        "id",
        "status",
        "current_step",
        "error_code",
        "error_message",
        "retryable",
        "owner_account_id",
        "manager_bot_profile_id",
        "display_name",
        "username",
        "guardian_bot_profile_id",
        "owned_bot_profile_id",
        "created_at",
        "updated_at",
    }
    assert required <= set(payload)
    assert all("token" not in key.lower() for key in payload)
    assert "managed-provision-idempotency" not in repr(payload)
