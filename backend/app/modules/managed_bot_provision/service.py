"""Persistent execution of official Telegram managed Bot creation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.bot_credentials import resolve_guardian_bot_token
from app.core.account.models import (
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    ManagedBotProvision,
    ManagedBotProvisionStatus,
    ManagedBotProvisionStep,
    TelegramAccount,
)
from app.core.account.pool import AccountPool, get_account_pool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
)
from app.core.config import settings
from app.integrations.telegram.client import (
    TelegramAPIError,
    TelegramClient,
    TelegramConfig,
)
from app.modules.managed_bot_provision.profiles import (
    ManagedBotProfileConflict,
    mark_managed_bot_profiles_verified,
    register_managed_bot_profiles,
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_ALLOWED_OWNER_STATUSES = {
    AccountStatus.IDLE,
    AccountStatus.ONLINE,
    AccountStatus.OFFLINE,
}
_BLOCKED_RISK_LEVELS = {
    AccountRiskLevel.LIMITED.value,
    AccountRiskLevel.FROZEN.value,
    AccountRiskLevel.QUARANTINED.value,
}
_RESERVED_USERNAME_STATUSES = {
    ManagedBotProvisionStatus.QUEUED.value,
    ManagedBotProvisionStatus.RUNNING.value,
    ManagedBotProvisionStatus.RETRY_WAIT.value,
    ManagedBotProvisionStatus.NEEDS_ATTENTION.value,
    ManagedBotProvisionStatus.SUCCEEDED.value,
}


@dataclass(frozen=True)
class ManagedBotProvisionFailure(RuntimeError):
    code: str
    retryable: bool = False
    retry_after_seconds: int = 300
    uncertain_create_outcome: bool = False

    def __str__(self) -> str:
        return self.code


class ManagedBotProvisionLeaseLost(RuntimeError):
    """The durable claim moved to another worker; external writes must stop."""


_LEASE_SECONDS = 300
_EXTERNAL_CALL_TIMEOUT_SECONDS = 120
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 15
_CAPABILITY_PROBE_CONCURRENCY = 8


def _reserved_username_filter(username: str) -> Any:
    return and_(
        ManagedBotProvision.username == username.casefold(),
        or_(
            ManagedBotProvision.status.in_(_RESERVED_USERNAME_STATUSES),
            ManagedBotProvision.bot_user_id.is_not(None),
            ManagedBotProvision.external_created_at.is_not(None),
        ),
    )


def normalize_managed_bot_username(value: str) -> str:
    username = str(value or "").strip().removeprefix("@")
    if not _USERNAME_RE.fullmatch(username) or not username.casefold().endswith("bot"):
        raise ValueError("USERNAME_INVALID")
    return username


def normalize_managed_bot_name(value: str) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 64:
        raise ValueError("NAME_INVALID")
    return name


def provision_request_hash(
    *,
    owner_account_id: int,
    manager_bot_profile_id: int,
    display_name: str,
    username: str,
) -> str:
    payload = json.dumps(
        {
            "owner_account_id": int(owner_account_id),
            "manager_bot_profile_id": int(manager_bot_profile_id),
            "display_name": normalize_managed_bot_name(display_name),
            "username": normalize_managed_bot_username(username).casefold(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _session_available(account: TelegramAccount) -> bool:
    if account.session_string:
        return True
    return (Path(settings.TELEGRAM_SESSION_DIR) / f"{account.session_name}.session").exists()


def owner_is_eligible(account: TelegramAccount | None) -> bool:
    return bool(
        account is not None
        and account.account_type == AccountType.PROMOTER
        and account.is_active
        and account.status in _ALLOWED_OWNER_STATUSES
        and _value(account.risk_level) not in _BLOCKED_RISK_LEVELS
        and _session_available(account)
    )


def serialize_provision(row: ManagedBotProvision) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": row.id,
        "status": _value(row.status),
        "current_step": _value(row.current_step),
        "owner_account_id": row.owner_account_id,
        "manager_bot_profile_id": row.manager_bot_profile_id,
        "display_name": row.display_name,
        "username": row.username,
        "bot_user_id": row.bot_user_id,
        "guardian_bot_profile_id": row.guardian_bot_profile_id,
        "owned_bot_profile_id": row.owned_bot_profile_id,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "retryable": bool(row.retryable),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "next_retry_at": iso(row.next_retry_at),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
    }


def _manager_reference(profile: GuardianBotProfile) -> str | int:
    username = str(profile.bot_username or "").strip().removeprefix("@")
    if username:
        return username
    if profile.bot_user_id:
        return int(profile.bot_user_id)
    raise ManagedBotProvisionFailure("MANAGER_INVALID")


def _classify_create_error(exc: BaseException) -> ManagedBotProvisionFailure:
    marker = f"{type(exc).__name__}:{exc}".upper().replace("_", "")
    mappings = (
        ("BOTCREATELIMITEXCEEDED", "BOT_CREATE_LIMIT_EXCEEDED"),
        ("MANAGERPERMISSIONMISSING", "MANAGER_PERMISSION_MISSING"),
        ("MANAGERINVALID", "MANAGER_INVALID"),
        ("USERNAMESUFFIXMISSING", "USERNAME_SUFFIX_MISSING"),
        ("USERNAMEOCCUPIED", "USERNAME_OCCUPIED"),
        ("USERNAMEUNAVAILABLE", "USERNAME_OCCUPIED"),
        ("USERNAMEINVALID", "USERNAME_INVALID"),
        ("NAMEINVALID", "NAME_INVALID"),
    )
    for needle, code in mappings:
        if needle in marker:
            return ManagedBotProvisionFailure(code)
    if "RISK_GUARD_BLOCKED" in str(exc).upper():
        return ManagedBotProvisionFailure("ACCOUNT_RISK_BLOCKED")
    if "FLOODWAIT" in marker:
        return ManagedBotProvisionFailure(
            "FLOOD_WAIT",
            retryable=True,
            retry_after_seconds=max(300, int(getattr(exc, "seconds", 0) or 0)),
        )
    if isinstance(exc, (TelegramExecutionError, TimeoutError)):
        return ManagedBotProvisionFailure(
            "TELEGRAM_CREATE_OUTCOME_UNKNOWN",
            retryable=True,
            retry_after_seconds=60,
            uncertain_create_outcome=True,
        )
    return ManagedBotProvisionFailure(
        "TELEGRAM_TEMPORARILY_UNAVAILABLE",
        retryable=True,
        retry_after_seconds=60,
    )


def _classify_username_check_error(exc: BaseException) -> ManagedBotProvisionFailure:
    marker = f"{type(exc).__name__}:{exc}".upper().replace("_", "")
    mappings = (
        ("USERNAMEPURCHASEAVAILABLE", "USERNAME_PURCHASE_AVAILABLE"),
        ("USERNAMESUFFIXMISSING", "USERNAME_SUFFIX_MISSING"),
        ("USERNAMEOCCUPIED", "USERNAME_OCCUPIED"),
        ("USERNAMEUNAVAILABLE", "USERNAME_OCCUPIED"),
        ("USERNAMEINVALID", "USERNAME_INVALID"),
    )
    for needle, code in mappings:
        if needle in marker:
            return ManagedBotProvisionFailure(code)
    return ManagedBotProvisionFailure(
        "TELEGRAM_TEMPORARILY_UNAVAILABLE",
        retryable=True,
        retry_after_seconds=60,
    )


def _safe_error_message(code: str) -> str:
    messages = {
        "ACCOUNT_NOT_ELIGIBLE": "Selected user account is unavailable",
        "ACCOUNT_OPERATION_LEASE_UNAVAILABLE": "Selected user account is busy",
        "ACCOUNT_RISK_BLOCKED": "Selected user account reached its managed Bot safety limit",
        "MANAGER_INVALID": "Manager Bot is unavailable",
        "MANAGER_PERMISSION_MISSING": "Manager Bot has not enabled Bot Management Mode",
        "USERNAME_INVALID": "Managed Bot username is invalid",
        "USERNAME_SUFFIX_MISSING": "Managed Bot username must end in bot",
        "USERNAME_OCCUPIED": "Managed Bot username is already occupied",
        "USERNAME_PURCHASE_AVAILABLE": "Managed Bot username is available only for purchase",
        "NAME_INVALID": "Managed Bot display name is invalid",
        "BOT_CREATE_LIMIT_EXCEEDED": "Telegram Bot creation limit was reached",
        "FLOOD_WAIT": "Telegram rate limit requires a later retry",
        "MANAGED_BOT_TOKEN_UNAVAILABLE": "Managed Bot token is temporarily unavailable",
        "MANAGED_BOT_IDENTITY_MISMATCH": "Managed Bot identity verification failed",
        "TELEGRAM_CREATE_OUTCOME_UNKNOWN": "Telegram creation outcome needs recovery verification",
        "TELEGRAM_TEMPORARILY_UNAVAILABLE": "Telegram is temporarily unavailable",
        "LOCAL_PROFILE_CONFLICT": "Existing local Bot records conflict with this request",
    }
    return messages.get(code, "Managed Bot provisioning failed")


async def list_manager_capabilities(db: AsyncSession) -> dict[str, Any]:
    try:
        from telethon.tl.functions.bots import CheckUsernameRequest, CreateBotRequest

        del CheckUsernameRequest, CreateBotRequest
        supported = True
    except ImportError:
        supported = False

    owners = (
        (
            await db.execute(
                select(TelegramAccount)
                .where(
                    TelegramAccount.account_type == AccountType.PROMOTER,
                    TelegramAccount.is_active.is_(True),
                    TelegramAccount.status.in_(list(_ALLOWED_OWNER_STATUSES)),
                )
                .order_by(TelegramAccount.id.asc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    owner_rows = [
        {
            "account_id": row.id,
            "identifier": row.identifier,
            "display_name": row.display_name,
            "status": _value(row.status),
        }
        for row in owners
        if owner_is_eligible(row)
    ]

    profiles = (
        (
            await db.execute(
                select(GuardianBotProfile)
                .join(TelegramAccount, TelegramAccount.id == GuardianBotProfile.account_id)
                .where(
                    GuardianBotProfile.enabled.is_(True),
                    TelegramAccount.is_active.is_(True),
                    TelegramAccount.account_type == AccountType.GUARDIAN_BOT,
                )
                .order_by(GuardianBotProfile.id.asc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    probe_semaphore = asyncio.Semaphore(_CAPABILITY_PROBE_CONCURRENCY)

    async def probe_manager(profile: GuardianBotProfile) -> dict[str, Any]:
        can_manage = False
        username = profile.bot_username
        bot_user_id = profile.bot_user_id
        client: TelegramClient | None = None
        async with probe_semaphore:
            try:
                token = resolve_guardian_bot_token(profile.bot_token)
                if not token:
                    raise ValueError("manager token missing")
                client = TelegramClient(TelegramConfig(bot_token=token, timeout=10))
                identity = await asyncio.wait_for(
                    client.get_me(), timeout=_CAPABILITY_PROBE_TIMEOUT_SECONDS
                )
                can_manage = bool(identity.is_bot and identity.can_manage_bots)
                username = identity.username or username
                bot_user_id = identity.user_id or bot_user_id
            except Exception:
                can_manage = False
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
        return {
            "profile_id": profile.id,
            "account_id": profile.account_id,
            "bot_user_id": bot_user_id,
            "bot_username": username,
            "display_name": profile.account.display_name,
            "can_manage_bots": can_manage,
            "enabled": bool(profile.enabled),
        }

    manager_rows = list(
        await asyncio.gather(*(probe_manager(profile) for profile in profiles))
    )

    blockers: list[str] = []
    if not supported:
        blockers.append("unsupported")
    if not owner_rows:
        blockers.append("owner_account_unavailable")
    if not any(item["can_manage_bots"] for item in manager_rows):
        blockers.append("manager_permission_missing")
    return {
        "supported": supported,
        "available": supported
        and bool(owner_rows)
        and any(item["can_manage_bots"] for item in manager_rows),
        "owner_accounts": owner_rows,
        "manager_bot_profiles": manager_rows,
        "blockers": blockers,
    }


async def create_managed_bot_provision(
    db: AsyncSession,
    *,
    owner_account_id: int,
    manager_bot_profile_id: int,
    display_name: str,
    username: str,
    idempotency_key: str,
    created_by_id: int | None,
) -> tuple[ManagedBotProvision, bool]:
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 128:
        raise ValueError("IDEMPOTENCY_KEY_INVALID")
    name = normalize_managed_bot_name(display_name)
    normalized_username = normalize_managed_bot_username(username)
    request_hash = provision_request_hash(
        owner_account_id=owner_account_id,
        manager_bot_profile_id=manager_bot_profile_id,
        display_name=name,
        username=normalized_username,
    )
    existing = await db.scalar(
        select(ManagedBotProvision).where(ManagedBotProvision.idempotency_key == key)
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ValueError("IDEMPOTENCY_KEY_CONFLICT")
        return existing, False

    same_username = await db.scalar(
        select(ManagedBotProvision).where(_reserved_username_filter(normalized_username))
    )
    if same_username is not None:
        same_request = (
            same_username.owner_account_id == int(owner_account_id)
            and same_username.manager_bot_profile_id == int(manager_bot_profile_id)
            and same_username.display_name == name
        )
        if same_request:
            return same_username, False
        raise ValueError("USERNAME_OPERATION_CONFLICT")

    owner = await db.get(TelegramAccount, int(owner_account_id))
    if not owner_is_eligible(owner):
        raise ValueError("ACCOUNT_NOT_ELIGIBLE")
    manager = await db.get(GuardianBotProfile, int(manager_bot_profile_id))
    if manager is None or not manager.enabled or not str(manager.bot_token or "").strip():
        raise ValueError("MANAGER_INVALID")
    manager_account = await db.get(TelegramAccount, manager.account_id)
    if (
        manager_account is None
        or manager_account.account_type != AccountType.GUARDIAN_BOT
        or not manager_account.is_active
    ):
        raise ValueError("MANAGER_INVALID")

    row = ManagedBotProvision(
        idempotency_key=key,
        request_hash=request_hash,
        owner_account_id=int(owner_account_id),
        manager_bot_profile_id=int(manager_bot_profile_id),
        display_name=name,
        username=normalized_username.casefold(),
        status=ManagedBotProvisionStatus.QUEUED.value,
        current_step=ManagedBotProvisionStep.PREFLIGHT.value,
        created_by_id=created_by_id,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await db.scalar(
            select(ManagedBotProvision).where(ManagedBotProvision.idempotency_key == key)
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ValueError("IDEMPOTENCY_KEY_CONFLICT") from None
            return existing, False
        same_username = await db.scalar(
            select(ManagedBotProvision).where(_reserved_username_filter(normalized_username))
        )
        if same_username is not None:
            same_request = (
                same_username.owner_account_id == int(owner_account_id)
                and same_username.manager_bot_profile_id == int(manager_bot_profile_id)
                and same_username.display_name == name
            )
            if same_request:
                return same_username, False
            raise ValueError("USERNAME_OPERATION_CONFLICT") from None
        raise
    await db.refresh(row)
    return row, True


class ManagedBotProvisionService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        account_pool: AccountPool | None = None,
        telegram_execution: TelegramExecutionService | None = None,
    ) -> None:
        self.db = db
        self.account_pool = account_pool or get_account_pool()
        self.telegram_execution = telegram_execution or TelegramExecutionService(
            AccountRiskGuard(db)
        )

    async def run_tick(
        self,
        *,
        limit: int = 5,
        stale_after_seconds: int = 300,
    ) -> dict[str, int]:
        recovered = await self.recover_stale(stale_after_seconds=stale_after_seconds)
        processed = 0
        for _ in range(max(1, min(int(limit), 20))):
            claim = await self._claim_next(stale_after_seconds=stale_after_seconds)
            if claim is None:
                break
            await self._execute_claim(*claim)
            processed += 1
        return {"processed": processed, "recovered": recovered}

    async def _claim_next(self, *, stale_after_seconds: int) -> tuple[int, str] | None:
        now = datetime.utcnow()
        row = (
            await self.db.execute(
                select(ManagedBotProvision)
                .where(
                    or_(
                        ManagedBotProvision.status == ManagedBotProvisionStatus.QUEUED.value,
                        and_(
                            ManagedBotProvision.status
                            == ManagedBotProvisionStatus.RETRY_WAIT.value,
                            or_(
                                ManagedBotProvision.next_retry_at.is_(None),
                                ManagedBotProvision.next_retry_at <= now,
                            ),
                        ),
                    )
                )
                .order_by(ManagedBotProvision.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            await self.db.rollback()
            return None
        lease_id = uuid.uuid4().hex
        row.status = ManagedBotProvisionStatus.RUNNING.value
        row.attempts += 1
        row.retryable = False
        row.error_code = None
        row.error_message = None
        row.next_retry_at = None
        row.lease_id = lease_id
        row.lease_expires_at = now + timedelta(seconds=max(90, int(stale_after_seconds)))
        row.heartbeat_at = now
        row.started_at = row.started_at or now
        await self.db.commit()
        return row.id, lease_id

    async def _fenced_update(
        self,
        row: ManagedBotProvision,
        lease_id: str,
        *,
        values: dict[str, Any],
        renew_lease: bool = True,
    ) -> None:
        now = datetime.utcnow()
        update_values = dict(values)
        update_values["heartbeat_at"] = now
        if renew_lease:
            update_values["lease_expires_at"] = now + timedelta(seconds=_LEASE_SECONDS)
        result = await self.db.execute(
            update(ManagedBotProvision)
            .where(
                ManagedBotProvision.id == int(row.id),
                ManagedBotProvision.lease_id == lease_id,
                ManagedBotProvision.status == ManagedBotProvisionStatus.RUNNING.value,
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self.db.rollback()
            raise ManagedBotProvisionLeaseLost()
        await self.db.commit()
        await self.db.refresh(row)

    async def _set_step(
        self,
        row: ManagedBotProvision,
        lease_id: str,
        step: ManagedBotProvisionStep,
    ) -> None:
        await self._fenced_update(
            row,
            lease_id,
            values={"current_step": step.value},
        )

    async def _mark_create_attempted(
        self,
        row: ManagedBotProvision,
        lease_id: str,
    ) -> None:
        await self._fenced_update(
            row,
            lease_id,
            values={"create_attempted_at": datetime.utcnow()},
        )

    async def _record_external_created(
        self,
        row: ManagedBotProvision,
        lease_id: str,
        *,
        bot_user_id: int,
    ) -> None:
        now = datetime.utcnow()
        await self._fenced_update(
            row,
            lease_id,
            values={
                "bot_user_id": int(bot_user_id),
                "create_attempted_at": row.create_attempted_at or now,
                "external_created_at": row.external_created_at or now,
            },
        )

    async def _record_registered_profiles(
        self,
        row: ManagedBotProvision,
        lease_id: str,
        *,
        guardian_bot_profile_id: int,
        owned_bot_profile_id: int,
    ) -> None:
        await self._fenced_update(
            row,
            lease_id,
            values={
                "guardian_bot_profile_id": int(guardian_bot_profile_id),
                "owned_bot_profile_id": int(owned_bot_profile_id),
            },
        )

    async def _complete_claim(
        self,
        row: ManagedBotProvision,
        lease_id: str,
    ) -> None:
        now = datetime.utcnow()
        await self._fenced_update(
            row,
            lease_id,
            values={
                "current_step": ManagedBotProvisionStep.COMPLETE.value,
                "status": ManagedBotProvisionStatus.SUCCEEDED.value,
                "retryable": False,
                "error_code": None,
                "error_message": None,
                "next_retry_at": None,
                "lease_id": None,
                "lease_expires_at": None,
                "finished_at": now,
            },
            renew_lease=False,
        )

    async def _load_claim(
        self,
        provision_id: int,
        lease_id: str,
    ) -> ManagedBotProvision | None:
        return await self.db.scalar(
            select(ManagedBotProvision).where(
                ManagedBotProvision.id == int(provision_id),
                ManagedBotProvision.lease_id == lease_id,
                ManagedBotProvision.status == ManagedBotProvisionStatus.RUNNING.value,
            )
        )

    async def _manager_client(
        self,
        profile: GuardianBotProfile,
    ) -> tuple[TelegramClient, Any]:
        token = resolve_guardian_bot_token(profile.bot_token)
        if not token:
            raise ManagedBotProvisionFailure("MANAGER_INVALID")
        client = TelegramClient(TelegramConfig(bot_token=token, timeout=20))
        try:
            identity = await asyncio.wait_for(
                client.get_me(), timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS
            )
        except (TelegramAPIError, TimeoutError) as exc:
            await client.close()
            raise ManagedBotProvisionFailure(
                "TELEGRAM_TEMPORARILY_UNAVAILABLE",
                retryable=(
                    not isinstance(exc, TelegramAPIError)
                    or not (exc.code and 400 <= exc.code < 500)
                ),
            ) from None
        except BaseException:
            await client.close()
            raise
        if not identity.is_bot or not identity.can_manage_bots:
            await client.close()
            raise ManagedBotProvisionFailure("MANAGER_PERMISSION_MISSING")
        if profile.bot_user_id and int(profile.bot_user_id) != int(identity.user_id):
            await client.close()
            raise ManagedBotProvisionFailure("MANAGER_INVALID")
        profile.bot_user_id = int(identity.user_id)
        profile.bot_username = identity.username or profile.bot_username
        await self.db.commit()
        return client, identity

    async def _recover_existing(
        self,
        row: ManagedBotProvision,
        wrapper: Any | None,
        manager_client: TelegramClient,
    ) -> tuple[Any, str] | None:
        if row.bot_user_id is not None:
            entity = SimpleNamespace(
                id=int(row.bot_user_id),
                bot=True,
                username=row.username,
            )
            try:
                token = await asyncio.wait_for(
                    manager_client.get_managed_bot_token(int(row.bot_user_id)),
                    timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
            except (TelegramAPIError, TimeoutError) as exc:
                raise ManagedBotProvisionFailure(
                    "MANAGED_BOT_TOKEN_UNAVAILABLE",
                    retryable=(
                        not isinstance(exc, TelegramAPIError)
                        or exc.code is None
                        or exc.code == 429
                        or exc.code >= 500
                    ),
                    retry_after_seconds=60,
                    uncertain_create_outcome=True,
                ) from None
            return entity, token

        client = getattr(wrapper, "client", None)
        if client is None:
            raise ManagedBotProvisionFailure("ACCOUNT_OPERATION_LEASE_UNAVAILABLE", retryable=True)
        try:
            entity = await asyncio.wait_for(
                client.get_entity(row.username),
                timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            marker = type(exc).__name__.upper()
            message = str(exc).casefold()
            if (
                "USERNAMENOTOCCUPIED" in marker
                or "USERNAMEINVALID" in marker
                or (
                    isinstance(exc, ValueError)
                    and "no user has" in message
                    and "username" in message
                )
            ):
                return None
            raise ManagedBotProvisionFailure(
                "TELEGRAM_TEMPORARILY_UNAVAILABLE", retryable=True, retry_after_seconds=60
            ) from None
        if (
            getattr(entity, "bot", None) is not True
            or str(getattr(entity, "username", "") or "").casefold() != row.username.casefold()
        ):
            raise ManagedBotProvisionFailure("USERNAME_OCCUPIED")
        raise ManagedBotProvisionFailure(
            "TELEGRAM_CREATE_OUTCOME_UNKNOWN",
            uncertain_create_outcome=True,
        )

    async def _execute_claim(self, provision_id: int, lease_id: str) -> None:
        row = await self._load_claim(provision_id, lease_id)
        if row is None:
            return
        wrapper = None
        manager_client: TelegramClient | None = None
        try:
            owner = await self.db.get(TelegramAccount, row.owner_account_id)
            if owner is None or owner.account_type != AccountType.PROMOTER:
                raise ManagedBotProvisionFailure("ACCOUNT_NOT_ELIGIBLE")
            manager = await self.db.get(GuardianBotProfile, row.manager_bot_profile_id)
            if manager is None or not manager.enabled:
                raise ManagedBotProvisionFailure("MANAGER_INVALID")
            manager_account = await self.db.get(TelegramAccount, manager.account_id)
            if (
                manager_account is None
                or manager_account.account_type != AccountType.GUARDIAN_BOT
                or not manager_account.is_active
            ):
                raise ManagedBotProvisionFailure("MANAGER_INVALID")

            await self._set_step(row, lease_id, ManagedBotProvisionStep.PREFLIGHT)
            manager_client, _manager_identity = await self._manager_client(manager)

            recovered = None
            if row.bot_user_id is not None:
                await self._set_step(
                    row,
                    lease_id,
                    ManagedBotProvisionStep.FETCH_TOKEN,
                )
                recovered = await self._recover_existing(row, None, manager_client)
            else:
                if not owner_is_eligible(owner):
                    raise ManagedBotProvisionFailure("ACCOUNT_NOT_ELIGIBLE")
                await asyncio.wait_for(
                    self.account_pool.add_account_from_db(owner),
                    timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
                wrapper = await asyncio.wait_for(
                    self.account_pool.acquire_by_id(
                        owner.id,
                        purpose="managed_bot_provision",
                        require_session=True,
                        raise_on_lease_failure=True,
                    ),
                    timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
                )
                if wrapper is None:
                    raise ManagedBotProvisionFailure(
                        "ACCOUNT_OPERATION_LEASE_UNAVAILABLE", retryable=True
                    )
                if row.create_attempted_at is not None:
                    await self._set_step(
                        row,
                        lease_id,
                        ManagedBotProvisionStep.FETCH_TOKEN,
                    )
                    recovered = await self._recover_existing(row, wrapper, manager_client)
            if recovered is None:
                await self._set_step(
                    row,
                    lease_id,
                    ManagedBotProvisionStep.CHECK_USERNAME,
                )
                try:
                    username_available = await asyncio.wait_for(
                        self.telegram_execution.check_managed_bot_username(
                            wrapper, row.username
                        ),
                        timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    raise _classify_username_check_error(exc) from None
                if not username_available:
                    raise ManagedBotProvisionFailure("USERNAME_OCCUPIED")
                await self._set_step(
                    row,
                    lease_id,
                    ManagedBotProvisionStep.CREATE_BOT,
                )

                async def mark_create_attempted() -> None:
                    await self._mark_create_attempted(row, lease_id)

                try:
                    entity = await asyncio.wait_for(
                        self.telegram_execution.create_managed_bot(
                            wrapper,
                            name=row.display_name,
                            username=row.username,
                            manager_bot=_manager_reference(manager),
                            source="managed_bot_provision",
                            risk_reservation_id=(
                                f"managed-bot-{row.id}-{row.request_hash[:16]}"
                            ),
                            on_create_attempted=mark_create_attempted,
                        ),
                        timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    raise _classify_create_error(exc) from None
                await self._record_external_created(
                    row,
                    lease_id,
                    bot_user_id=int(entity.id),
                )
                await self._set_step(
                    row,
                    lease_id,
                    ManagedBotProvisionStep.FETCH_TOKEN,
                )
                try:
                    token = await asyncio.wait_for(
                        manager_client.get_managed_bot_token(int(entity.id)),
                        timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS,
                    )
                except (TelegramAPIError, TimeoutError):
                    raise ManagedBotProvisionFailure(
                        "MANAGED_BOT_TOKEN_UNAVAILABLE",
                        retryable=True,
                        retry_after_seconds=60,
                        uncertain_create_outcome=True,
                    ) from None
            else:
                entity, token = recovered
                await self._record_external_created(
                    row,
                    lease_id,
                    bot_user_id=int(entity.id),
                )
                await self._set_step(
                    row,
                    lease_id,
                    ManagedBotProvisionStep.FETCH_TOKEN,
                )

            await self._set_step(
                row,
                lease_id,
                ManagedBotProvisionStep.VERIFY,
            )
            child_client = TelegramClient(TelegramConfig(bot_token=token, timeout=20))
            try:
                identity = await asyncio.wait_for(
                    child_client.get_me(), timeout=_EXTERNAL_CALL_TIMEOUT_SECONDS
                )
            except (TelegramAPIError, TimeoutError):
                raise ManagedBotProvisionFailure(
                    "MANAGED_BOT_TOKEN_UNAVAILABLE",
                    retryable=True,
                    retry_after_seconds=60,
                    uncertain_create_outcome=True,
                ) from None
            finally:
                await child_client.close()
            if (
                not identity.is_bot
                or int(identity.user_id) != int(entity.id)
                or str(identity.username or "").casefold() != row.username.casefold()
            ):
                raise ManagedBotProvisionFailure(
                    "MANAGED_BOT_IDENTITY_MISMATCH",
                    uncertain_create_outcome=True,
                )

            await self._set_step(
                row,
                lease_id,
                ManagedBotProvisionStep.REGISTER_PROFILES,
            )
            registered = await register_managed_bot_profiles(
                self.db,
                owner_account=owner,
                bot_user_id=int(identity.user_id),
                username=identity.username or row.username,
                display_name=identity.full_name or row.display_name,
                token=token,
            )
            await self._record_registered_profiles(
                row,
                lease_id,
                guardian_bot_profile_id=registered.guardian_bot_profile_id,
                owned_bot_profile_id=registered.owned_bot_profile_id,
            )
            await mark_managed_bot_profiles_verified(
                self.db,
                guardian_bot_profile_id=registered.guardian_bot_profile_id,
                owned_bot_profile_id=registered.owned_bot_profile_id,
                bot_user_id=int(identity.user_id),
                username=identity.username or row.username,
                display_name=identity.full_name or row.display_name,
            )
            await self._complete_claim(row, lease_id)
        except ManagedBotProvisionLeaseLost:
            await self.db.rollback()
        except ManagedBotProvisionFailure as exc:
            await self.db.rollback()
            await self._finish_failure(provision_id, lease_id, exc)
        except ManagedBotProfileConflict:
            await self.db.rollback()
            await self._finish_failure(
                provision_id,
                lease_id,
                ManagedBotProvisionFailure(
                    "LOCAL_PROFILE_CONFLICT",
                    uncertain_create_outcome=True,
                ),
            )
        except Exception:
            await self.db.rollback()
            await self._finish_failure(
                provision_id,
                lease_id,
                ManagedBotProvisionFailure(
                    "TELEGRAM_TEMPORARILY_UNAVAILABLE",
                    retryable=True,
                    retry_after_seconds=60,
                    uncertain_create_outcome=True,
                ),
            )
        finally:
            try:
                if manager_client is not None:
                    await manager_client.close()
            finally:
                if wrapper is not None:
                    await self.account_pool.release(wrapper)

    async def _finish_failure(
        self,
        provision_id: int,
        lease_id: str,
        failure: ManagedBotProvisionFailure,
    ) -> None:
        row = (
            await self.db.execute(
                select(ManagedBotProvision)
                .where(
                    ManagedBotProvision.id == int(provision_id),
                    ManagedBotProvision.lease_id == lease_id,
                    ManagedBotProvision.status == ManagedBotProvisionStatus.RUNNING.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            await self.db.rollback()
            return
        now = datetime.utcnow()
        may_retry = failure.retryable and row.attempts < row.max_attempts
        if may_retry:
            row.status = ManagedBotProvisionStatus.RETRY_WAIT.value
            row.next_retry_at = now + timedelta(
                seconds=max(30, min(int(failure.retry_after_seconds), 3600))
            )
        elif (
            row.bot_user_id is not None
            or row.external_created_at is not None
            or (failure.uncertain_create_outcome and row.create_attempted_at is not None)
        ):
            row.status = ManagedBotProvisionStatus.NEEDS_ATTENTION.value
            row.next_retry_at = None
        else:
            row.status = ManagedBotProvisionStatus.FAILED.value
            row.next_retry_at = None
        row.retryable = may_retry
        row.error_code = failure.code
        row.error_message = _safe_error_message(failure.code)
        row.lease_id = None
        row.lease_expires_at = None
        row.heartbeat_at = now
        if not may_retry:
            row.finished_at = now
        await self.db.commit()

    async def recover_stale(self, *, stale_after_seconds: int = 300) -> int:
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=max(90, int(stale_after_seconds)))
        rows = (
            (
                await self.db.execute(
                    select(ManagedBotProvision)
                    .where(
                        ManagedBotProvision.status == ManagedBotProvisionStatus.RUNNING.value,
                        or_(
                            ManagedBotProvision.lease_expires_at <= now,
                            and_(
                                ManagedBotProvision.lease_expires_at.is_(None),
                                ManagedBotProvision.updated_at <= cutoff,
                            ),
                        ),
                    )
                    .order_by(ManagedBotProvision.id.asc())
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.lease_id = None
            row.lease_expires_at = None
            row.heartbeat_at = now
            if row.attempts < row.max_attempts:
                row.status = ManagedBotProvisionStatus.RETRY_WAIT.value
                row.retryable = True
                row.next_retry_at = now
                row.error_code = "WORKER_LEASE_EXPIRED"
                row.error_message = "Provision worker lease expired; recovery queued"
            else:
                row.status = (
                    ManagedBotProvisionStatus.NEEDS_ATTENTION.value
                    if row.create_attempted_at is not None
                    else ManagedBotProvisionStatus.FAILED.value
                )
                row.retryable = False
                row.next_retry_at = None
                row.error_code = "WORKER_LEASE_EXPIRED"
                row.error_message = "Provision worker lease expired"
                row.finished_at = now
        if rows:
            await self.db.commit()
        else:
            await self.db.rollback()
        return len(rows)


async def run_managed_bot_provision_tick(
    db: AsyncSession,
    *,
    limit: int = 5,
    stale_after_seconds: int = 300,
    account_pool: AccountPool | None = None,
    telegram_execution: TelegramExecutionService | None = None,
) -> dict[str, int]:
    return await ManagedBotProvisionService(
        db,
        account_pool=account_pool,
        telegram_execution=telegram_execution,
    ).run_tick(limit=limit, stale_after_seconds=stale_after_seconds)


__all__ = [
    "ManagedBotProvisionFailure",
    "ManagedBotProvisionLeaseLost",
    "ManagedBotProvisionService",
    "create_managed_bot_provision",
    "list_manager_capabilities",
    "normalize_managed_bot_name",
    "normalize_managed_bot_username",
    "provision_request_hash",
    "run_managed_bot_provision_tick",
    "serialize_provision",
]
