"""Idempotent profile registration for managed Bot provisioning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.bot_credentials import encrypt_guardian_bot_token
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotHealthStatus,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.ephemeral_secret import encrypt_ephemeral_secret
from app.modules.owned_group.models_extra import OwnedBotProfile


class ManagedBotProfileConflict(RuntimeError):
    """Existing local records do not belong to the requested owner or Bot."""


@dataclass(frozen=True)
class RegisteredManagedBotProfiles:
    account_id: int
    guardian_bot_profile_id: int
    owned_bot_profile_id: int


def _username(value: str) -> str:
    return str(value or "").strip().lstrip("@").lower()


async def register_managed_bot_profiles(
    db: AsyncSession,
    *,
    owner_account: TelegramAccount,
    bot_user_id: int,
    username: str,
    display_name: str,
    token: str,
) -> RegisteredManagedBotProfiles:
    """Create or resume all three local records without ever storing plaintext."""

    normalized = _username(username)
    encrypted_guardian = encrypt_guardian_bot_token(token)
    encrypted_owned = encrypt_ephemeral_secret(token)
    if not encrypted_guardian or not encrypted_owned:
        raise RuntimeError("managed_bot_token_encryption_unavailable")

    guardian_matches = (
        (
            await db.execute(
                select(GuardianBotProfile).where(
                    or_(
                        GuardianBotProfile.bot_user_id == int(bot_user_id),
                        func.lower(GuardianBotProfile.bot_username) == normalized,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if len(guardian_matches) > 1:
        raise ManagedBotProfileConflict("managed_bot_guardian_identity_conflict")
    guardian = guardian_matches[0] if guardian_matches else None
    if guardian is not None and (
        (guardian.bot_user_id is not None and int(guardian.bot_user_id) != int(bot_user_id))
        or (guardian.bot_username and _username(guardian.bot_username) != normalized)
    ):
        raise ManagedBotProfileConflict("managed_bot_guardian_identity_conflict")

    account = await db.get(TelegramAccount, guardian.account_id) if guardian else None
    if account is None:
        account_matches = (
            (
                await db.execute(
                    select(TelegramAccount).where(
                        func.lower(TelegramAccount.identifier).in_([normalized, f"@{normalized}"])
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(account_matches) > 1:
            raise ManagedBotProfileConflict("managed_bot_identifier_conflict")
        account = account_matches[0] if account_matches else None
    if account is not None and account.account_type != AccountType.GUARDIAN_BOT:
        raise ManagedBotProfileConflict("managed_bot_identifier_conflict")

    if account is None:
        account = TelegramAccount(
            phone=None,
            account_type=AccountType.GUARDIAN_BOT,
            identifier=f"@{normalized}",
            display_name=display_name,
            api_config_id=owner_account.api_config_id,
            api_config_name=owner_account.api_config_name,
            country_code=owner_account.country_code,
            country_name=owner_account.country_name,
            session_name=f"guardian_managed_{int(bot_user_id)}",
            status=AccountStatus.IDLE,
            is_active=False,
        )
        db.add(account)
        await db.flush()
    else:
        account.identifier = f"@{normalized}"
        account.display_name = display_name
        account.status = AccountStatus.IDLE
        account.is_active = False

    if guardian is None:
        guardian = (
            await db.execute(
                select(GuardianBotProfile).where(GuardianBotProfile.account_id == account.id)
            )
        ).scalar_one_or_none()
    if guardian is not None and (
        (guardian.bot_user_id is not None and int(guardian.bot_user_id) != int(bot_user_id))
        or (guardian.bot_username and _username(guardian.bot_username) != normalized)
    ):
        raise ManagedBotProfileConflict("managed_bot_guardian_identity_conflict")
    if guardian is None:
        guardian = GuardianBotProfile(
            account_id=account.id,
            bot_token=encrypted_guardian,
            bot_username=normalized,
            bot_user_id=int(bot_user_id),
            health_status=GuardianBotHealthStatus.UNKNOWN,
            sync_status="pending",
            enabled=False,
        )
        db.add(guardian)
    else:
        if guardian.account_id != account.id:
            raise ManagedBotProfileConflict("managed_bot_guardian_profile_conflict")
        guardian.bot_token = encrypted_guardian
        guardian.bot_username = normalized
        guardian.bot_user_id = int(bot_user_id)
        guardian.health_status = GuardianBotHealthStatus.UNKNOWN
        guardian.sync_status = "pending"
        guardian.enabled = False
    await db.flush()

    owned_matches = (
        (
            await db.execute(
                select(OwnedBotProfile).where(
                    or_(
                        OwnedBotProfile.account_id == account.id,
                        OwnedBotProfile.bot_user_id == int(bot_user_id),
                        func.lower(OwnedBotProfile.bot_username) == normalized,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if len(owned_matches) > 1:
        raise ManagedBotProfileConflict("managed_bot_owned_identity_conflict")
    owned = owned_matches[0] if owned_matches else None
    if owned is not None and (
        (owned.bot_user_id is not None and int(owned.bot_user_id) != int(bot_user_id))
        or (owned.bot_username and _username(owned.bot_username) != normalized)
    ):
        raise ManagedBotProfileConflict("managed_bot_owned_identity_conflict")
    if owned is not None and owned.owner_account_id != owner_account.id:
        raise ManagedBotProfileConflict("managed_bot_owned_profile_owner_conflict")
    if owned is None:
        owned = OwnedBotProfile(
            owner_account_id=owner_account.id,
            account_id=account.id,
            bot_user_id=int(bot_user_id),
            bot_username=normalized,
            display_name=display_name,
            token_ciphertext=encrypted_owned,
            status="pending_verification",
            enabled=False,
        )
        db.add(owned)
    else:
        if owned.account_id != account.id:
            raise ManagedBotProfileConflict("managed_bot_owned_profile_account_conflict")
        owned.bot_user_id = int(bot_user_id)
        owned.bot_username = normalized
        owned.display_name = display_name
        owned.token_ciphertext = encrypted_owned
        owned.status = "pending_verification"
        owned.enabled = False
    await db.flush()

    return RegisteredManagedBotProfiles(
        account_id=account.id,
        guardian_bot_profile_id=guardian.id,
        owned_bot_profile_id=owned.id,
    )


async def mark_managed_bot_profiles_verified(
    db: AsyncSession,
    *,
    guardian_bot_profile_id: int,
    owned_bot_profile_id: int,
    bot_user_id: int,
    username: str,
    display_name: str,
) -> None:
    """Finalize recovered or newly-created profiles after an exact getMe match."""

    normalized = _username(username)
    guardian = await db.get(GuardianBotProfile, int(guardian_bot_profile_id))
    owned = await db.get(OwnedBotProfile, int(owned_bot_profile_id))
    if guardian is None or owned is None or guardian.account_id != owned.account_id:
        raise ManagedBotProfileConflict("managed_bot_profiles_missing")
    account = await db.get(TelegramAccount, guardian.account_id)
    if account is None:
        raise ManagedBotProfileConflict("managed_bot_account_missing")

    now = datetime.utcnow()
    account.identifier = f"@{normalized}"
    account.display_name = display_name
    account.status = AccountStatus.IDLE
    account.is_active = True
    guardian.bot_user_id = int(bot_user_id)
    guardian.bot_username = normalized
    guardian.health_status = GuardianBotHealthStatus.HEALTHY
    guardian.enabled = True
    guardian.last_heartbeat_at = now
    owned.bot_user_id = int(bot_user_id)
    owned.bot_username = normalized
    owned.display_name = display_name
    owned.status = "verified"
    owned.enabled = True
    owned.last_verified_at = now
    await db.flush()


__all__ = [
    "ManagedBotProfileConflict",
    "RegisteredManagedBotProfiles",
    "mark_managed_bot_profiles_verified",
    "register_managed_bot_profiles",
]
