"""Explicit Guardian governance bridge for self-owned Telegram groups.

The owned-group orchestrator and Guardian intentionally remain separate domains.
This module is the narrow bridge between them: it validates the two bot profile
records, probes Telegram without holding a database lock, and then atomically
links an ``OwnedGroupAsset`` to the core ``Group`` and its primary
``ManagedGroupBinding``.

Only whitelisted Telegram fields are persisted.  Bot tokens, sessions, proxy
credentials, invite links, request URLs, and raw Telegram responses must never
leave this module.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.bot_credentials import resolve_guardian_bot_token
from app.core.account.models import (
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.governance_gate import require_owned_group_governance_available
from app.core.telegram_chat_lock import acquire_telegram_chat_transaction_lock
from app.integrations.telegram.client import TelegramAPIError, TelegramClient, TelegramConfig
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.guardian.sync import ManagedGroupSyncConflict, sync_managed_group_binding
from app.modules.owned_group.contracts import AssetStatus
from app.modules.owned_group.lock_queries import owned_group_asset_for_update_query
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import (
    OwnedBotProfile,
    OwnedGroupAuditEvent,
    OwnedGroupMembership,
)
from app.modules.owned_group.security import redact_sensitive_text, redact_sensitive_value

GOVERNANCE_STATUSES = {"disabled", "pending", "managed", "degraded"}
REQUIRED_GUARDIAN_PERMISSIONS = (
    "can_delete_messages",
    "can_restrict_members",
    "can_invite_users",
    "can_pin_messages",
)
GOVERNANCE_CAPABILITIES = (
    "verification",
    "sensitive_keywords",
    "anti_spam",
    "warn",
    "mute",
    "ban",
    "announcement",
    "pin_message",
    "activity",
)
PENDING_STALE_AFTER = timedelta(minutes=2)
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _now() -> datetime:
    # Models currently use naive UTC DateTime columns throughout this project.
    return datetime.utcnow()


def new_correlation_id(asset_id: int, candidate: str | None = None) -> str:
    """Return a bounded, log-safe correlation id supplied by or for a caller."""

    normalized = str(candidate or "").strip()
    if normalized and _CORRELATION_ID.fullmatch(normalized):
        return normalized
    return f"og-gov-{asset_id}-{uuid.uuid4().hex}"


class GovernanceServiceError(Exception):
    """Stable service error which the HTTP layer can expose without raw exceptions."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        missing_permissions: list[str] | None = None,
        probe: GuardianPermissionProbe | None = None,
        binding_status: ManagedGroupBindingStatus | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.message = redact_sensitive_text(message, max_length=500)
        self.status_code = status_code
        self.retryable = retryable
        self.missing_permissions = list(missing_permissions or [])
        self.probe = probe
        self.binding_status = binding_status
        self.correlation_id = correlation_id
        super().__init__(self.message)

    def detail(self, *, asset_id: int, correlation_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reason": self.reason,
            "message": self.message,
            "retryable": self.retryable,
            "asset_id": asset_id,
            "correlation_id": self.correlation_id or correlation_id,
        }
        if self.missing_permissions:
            payload["missing_permissions"] = self.missing_permissions
        if self.probe is not None:
            payload["permission_probe"] = self.probe.as_response()
        return payload


async def _require_governance_gate(asset_id: int, correlation_id: str) -> None:
    """Convert gate failures to the public governance error envelope."""

    try:
        await require_owned_group_governance_available()
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise GovernanceServiceError(
            str(detail.get("reason") or "governance_stop_enabled"),
            str(detail.get("message") or "自建群 Guardian 治理当前不可用"),
            status_code=exc.status_code,
            retryable=bool(detail.get("retryable", False)),
            correlation_id=correlation_id,
        ) from exc


@dataclass(slots=True)
class EligibleGuardianBot:
    account: TelegramAccount
    guardian_profile: GuardianBotProfile
    owned_profile: OwnedBotProfile


@dataclass(slots=True)
class GuardianPermissionProbe:
    bot_user_id: int
    bot_username: str | None
    chat_type: str
    bot_status: str
    bot_role: str
    granted_permissions: list[str]
    missing_permissions: list[str]
    checked_at: datetime

    @property
    def passed(self) -> bool:
        return (
            self.chat_type == "supergroup"
            and self.bot_role in {"admin", "owner"}
            and not self.missing_permissions
        )

    def as_snapshot(self, source: str) -> dict[str, Any]:
        """Return the exact whitelist permitted in persistent snapshots."""

        return {
            "schema_version": 1,
            "source": source,
            "chat_type": self.chat_type,
            "bot_user_id": self.bot_user_id,
            "bot_status": self.bot_status,
            "bot_role": self.bot_role,
            "required_permissions": list(REQUIRED_GUARDIAN_PERMISSIONS),
            "granted_permissions": list(self.granted_permissions),
            "missing_permissions": list(self.missing_permissions),
            "probed_at": self.checked_at.isoformat(),
        }

    def as_response(self) -> dict[str, Any]:
        return {
            "status": "passed" if self.passed else "failed",
            "required_permissions": list(REQUIRED_GUARDIAN_PERMISSIONS),
            "granted_permissions": list(self.granted_permissions),
            "missing_permissions": list(self.missing_permissions),
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class _PendingSnapshot:
    telegram_chat_id: int
    guardian_bot_account_id: int
    pending_at: datetime


def _actor_id(actor: dict[str, Any] | None) -> int | None:
    try:
        value = (actor or {}).get("id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_archived(asset: OwnedGroupAsset) -> bool:
    return _value(asset.status) == AssetStatus.ARCHIVED.value or asset.archived_at is not None


def _validate_ready_asset(asset: OwnedGroupAsset) -> None:
    if _is_archived(asset) or _value(asset.status) != AssetStatus.READY.value:
        raise GovernanceServiceError(
            "asset_not_ready",
            "自建群尚未就绪或已归档",
            status_code=409,
            retryable=not _is_archived(asset),
        )
    if not asset.telegram_chat_id:
        raise GovernanceServiceError(
            "asset_not_ready",
            "自建群缺少 Telegram Chat ID",
            status_code=409,
            retryable=True,
        )


async def _locked_asset(db: AsyncSession, asset_id: int) -> OwnedGroupAsset:
    asset = await db.scalar(
        owned_group_asset_for_update_query()
        .where(OwnedGroupAsset.id == asset_id)
        .execution_options(populate_existing=True)
    )
    if asset is None:
        raise GovernanceServiceError(
            "owned_group_asset_not_found",
            "自建群资产不存在",
            status_code=404,
        )
    return asset


def _audit_state(
    asset: OwnedGroupAsset,
    *,
    probe: GuardianPermissionProbe | None = None,
    binding: ManagedGroupBinding | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "asset_status": _value(asset.status),
        "governance_status": str(asset.governance_status or "disabled"),
        "guardian_bot_account_id": asset.guardian_bot_account_id,
        "core_group_id": asset.core_group_id,
        "managed_binding_id": asset.managed_binding_id,
        "telegram_chat_id": asset.telegram_chat_id,
        "reason_code": asset.governance_last_error_code,
    }
    if binding is not None:
        value["binding_status"] = _value(binding.binding_status)
        value["bot_role"] = _value(binding.bot_role)
    if probe is not None:
        value["permission_probe"] = probe.as_response()
    return redact_sensitive_value(value)


def _add_audit(
    db: AsyncSession,
    *,
    event_type: str,
    asset: OwnedGroupAsset,
    actor: dict[str, Any] | None,
    before_state: dict[str, Any] | None,
    after_state: dict[str, Any] | None,
    result: str,
    reason_code: str | None,
    correlation_id: str,
) -> None:
    db.add(
        OwnedGroupAuditEvent(
            event_type=event_type,
            group_asset_id=asset.id,
            resource_type="bot" if asset.guardian_bot_account_id else None,
            resource_id=asset.guardian_bot_account_id,
            actor_id=_actor_id(actor),
            before_state=(
                json.dumps(redact_sensitive_value(before_state), ensure_ascii=False, sort_keys=True)
                if before_state is not None
                else None
            ),
            after_state=(
                json.dumps(redact_sensitive_value(after_state), ensure_ascii=False, sort_keys=True)
                if after_state is not None
                else None
            ),
            result=result,
            reason_code=reason_code,
            correlation_id=correlation_id,
        )
    )


async def resolve_guardian_bot(
    db: AsyncSession,
    asset: OwnedGroupAsset,
    guardian_bot_account_id: int,
) -> EligibleGuardianBot:
    """Resolve and revalidate both profiles for the selected runtime bot account."""

    account = await db.get(TelegramAccount, guardian_bot_account_id)
    blocked_statuses = {AccountStatus.ERROR.value, AccountStatus.BANNED.value, AccountStatus.RESTRICTED.value}
    blocked_risks = {
        AccountRiskLevel.LIMITED.value,
        AccountRiskLevel.FROZEN.value,
        AccountRiskLevel.QUARANTINED.value,
    }
    if (
        account is None
        or _value(account.account_type) != AccountType.GUARDIAN_BOT.value
        or not bool(account.is_active)
        or _value(account.status) in blocked_statuses
        or _value(account.risk_level) in blocked_risks
    ):
        raise GovernanceServiceError(
            "guardian_bot_not_eligible",
            "Guardian Bot 账号不可用于治理",
            status_code=422,
            retryable=True,
        )

    guardian_profile = await db.scalar(
        select(GuardianBotProfile).where(GuardianBotProfile.account_id == account.id)
    )
    owned_profile = await db.scalar(
        select(OwnedBotProfile).where(OwnedBotProfile.account_id == account.id)
    )
    if (
        guardian_profile is None
        or not guardian_profile.enabled
        or not str(guardian_profile.bot_token or "").strip()
        or owned_profile is None
        or not owned_profile.enabled
        or str(owned_profile.status or "") != "verified"
        or owned_profile.owner_account_id != asset.owner_account_id
        or guardian_profile.account_id != owned_profile.account_id
    ):
        raise GovernanceServiceError(
            "guardian_bot_not_eligible",
            "Guardian Bot 的运行资料、验证状态或资产归属不符合要求",
            status_code=422,
            retryable=True,
        )

    guardian_user_id = guardian_profile.bot_user_id
    owned_user_id = owned_profile.bot_user_id
    if guardian_user_id is not None and owned_user_id is not None:
        if int(guardian_user_id) != int(owned_user_id):
            raise GovernanceServiceError(
                "guardian_bot_identity_mismatch",
                "Guardian Bot 的两个 Profile 身份不一致",
                status_code=409,
            )

    return EligibleGuardianBot(
        account=account,
        guardian_profile=guardian_profile,
        owned_profile=owned_profile,
    )


def _probe_error_from_transport(exc: BaseException) -> GovernanceServiceError:
    message = redact_sensitive_text(exc, max_length=500)
    lowered = message.lower()
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return GovernanceServiceError(
            "telegram_probe_timeout",
            "Telegram 权限探针超时",
            status_code=504,
            retryable=True,
        )
    return GovernanceServiceError(
        "telegram_probe_failed",
        "Telegram 权限探针失败",
        status_code=502,
        retryable=True,
    )


async def probe_guardian_permissions(
    asset: OwnedGroupAsset,
    eligible_bot: EligibleGuardianBot,
    *,
    source: str = "owned_group_governance_bind",
) -> GuardianPermissionProbe:
    """Probe live Bot API identity/chat/admin state in the mandated order."""

    del source  # The caller selects the persisted snapshot source after success.
    client = TelegramClient(
        TelegramConfig(bot_token=resolve_guardian_bot_token(eligible_bot.guardian_profile.bot_token), timeout=15)
    )
    try:
        try:
            bot_user = await client.get_me()
            bot_user_id = int(getattr(bot_user, "user_id", 0) or 0)
            if not bot_user_id or not bool(getattr(bot_user, "is_bot", False)):
                raise GovernanceServiceError(
                    "guardian_bot_identity_mismatch",
                    "Guardian Token 未解析为有效 Bot 身份",
                    status_code=409,
                )

            known_ids = {
                int(value)
                for value in (
                    eligible_bot.guardian_profile.bot_user_id,
                    eligible_bot.owned_profile.bot_user_id,
                )
                if value is not None
            }
            if known_ids and (len(known_ids) != 1 or bot_user_id not in known_ids):
                raise GovernanceServiceError(
                    "guardian_bot_identity_mismatch",
                    "Guardian Token 与已登记 Bot 身份不一致",
                    status_code=409,
                )

            chat = await client.get_chat(int(asset.telegram_chat_id or 0))
            chat_type = str(getattr(chat, "type", "") or "").lower()
            if chat_type != "supergroup":
                raise GovernanceServiceError(
                    "telegram_chat_type_unsupported",
                    "目标 Telegram 会话不是超级群",
                    status_code=422,
                )

            member = await client.get_chat_member(int(asset.telegram_chat_id or 0), bot_user_id)
        except GovernanceServiceError:
            raise
        except (TelegramAPIError, TimeoutError) as exc:
            raise _probe_error_from_transport(exc) from exc
        except Exception as exc:  # pragma: no cover - transport implementations vary
            raise _probe_error_from_transport(exc) from exc

        bot_status = str(member.get("status") or "unknown").lower()
        checked_at = _now()
        if bot_status == "creator":
            bot_role = ManagedGroupBotRole.OWNER.value
            granted = list(REQUIRED_GUARDIAN_PERMISSIONS)
        else:
            bot_role = (
                ManagedGroupBotRole.ADMIN.value
                if bot_status == "administrator"
                else ManagedGroupBotRole.MEMBER.value
            )
            granted = [
                permission
                for permission in REQUIRED_GUARDIAN_PERMISSIONS
                if member.get(permission) is True
            ]
        missing = [permission for permission in REQUIRED_GUARDIAN_PERMISSIONS if permission not in granted]
        probe = GuardianPermissionProbe(
            bot_user_id=bot_user_id,
            bot_username=getattr(bot_user, "username", None),
            chat_type=chat_type,
            bot_status=bot_status,
            bot_role=bot_role,
            granted_permissions=granted,
            missing_permissions=missing,
            checked_at=checked_at,
        )

        if bot_status in {"left", "kicked"}:
            raise GovernanceServiceError(
                "guardian_bot_not_member",
                "Guardian Bot 不在目标群内",
                status_code=422,
                retryable=True,
                probe=probe,
                binding_status=ManagedGroupBindingStatus.INACTIVE,
            )
        if bot_status not in {"administrator", "creator"}:
            raise GovernanceServiceError(
                "guardian_bot_not_admin",
                "Guardian Bot 不是目标群管理员",
                status_code=422,
                retryable=True,
                probe=probe,
                binding_status=ManagedGroupBindingStatus.DEGRADED,
            )
        if missing:
            raise GovernanceServiceError(
                "guardian_permissions_missing",
                "Guardian Bot 缺少治理所需权限",
                status_code=422,
                retryable=True,
                missing_permissions=missing,
                probe=probe,
                binding_status=ManagedGroupBindingStatus.DEGRADED,
            )
        return probe
    finally:
        try:
            await client.close()
        except Exception:
            # Closing a temporary probe client is mandatory but its failure must
            # not replace the already determined, safely reportable outcome.
            pass


def _parse_snapshot(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _probe_response_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    required = [
        item
        for item in snapshot.get("required_permissions", REQUIRED_GUARDIAN_PERMISSIONS)
        if item in REQUIRED_GUARDIAN_PERMISSIONS
    ]
    granted = [
        item for item in snapshot.get("granted_permissions", []) if item in REQUIRED_GUARDIAN_PERMISSIONS
    ]
    missing = [
        item for item in snapshot.get("missing_permissions", []) if item in REQUIRED_GUARDIAN_PERMISSIONS
    ]
    if not missing:
        missing = [item for item in required if item not in granted]
    checked_at = snapshot.get("probed_at") or snapshot.get("checked_at")
    return {
        "status": "passed" if required and not missing else "failed",
        "required_permissions": required,
        "granted_permissions": granted,
        "missing_permissions": missing,
        "checked_at": checked_at,
    }


async def _latest_correlation_id(db: AsyncSession, asset_id: int) -> str | None:
    return await db.scalar(
        select(OwnedGroupAuditEvent.correlation_id)
        .where(
            OwnedGroupAuditEvent.group_asset_id == asset_id,
            OwnedGroupAuditEvent.event_type.like("owned_group_governance_%"),
        )
        .order_by(desc(OwnedGroupAuditEvent.id))
        .limit(1)
    )


async def get_governance_status(
    db: AsyncSession,
    asset_id: int,
    actor: dict[str, Any] | None = None,
    *,
    reused: bool = False,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Read governance state without writes or Telegram requests."""

    del actor
    asset = await db.get(OwnedGroupAsset, asset_id)
    if asset is None:
        raise GovernanceServiceError(
            "owned_group_asset_not_found", "自建群资产不存在", status_code=404
        )

    binding = (
        await db.get(ManagedGroupBinding, asset.managed_binding_id)
        if asset.managed_binding_id is not None
        else None
    )
    account = (
        await db.get(TelegramAccount, asset.guardian_bot_account_id)
        if asset.guardian_bot_account_id is not None
        else None
    )
    guardian_profile = None
    owned_profile = None
    if asset.guardian_bot_account_id is not None:
        guardian_profile = await db.scalar(
            select(GuardianBotProfile).where(
                GuardianBotProfile.account_id == asset.guardian_bot_account_id
            )
        )
        owned_profile = await db.scalar(
            select(OwnedBotProfile).where(OwnedBotProfile.account_id == asset.guardian_bot_account_id)
        )

    stored_status = str(asset.governance_status or "disabled")
    effective_status = stored_status if stored_status in GOVERNANCE_STATUSES else "degraded"
    binding_active = binding is not None and _value(binding.binding_status) == "active"
    managed_invariants = all(
        (
            asset.core_group_id is not None,
            asset.managed_binding_id is not None,
            asset.guardian_bot_account_id is not None,
            binding_active,
            binding is not None and binding.group_id == asset.core_group_id,
            binding is not None and binding.bot_account_id == asset.guardian_bot_account_id,
            binding is not None and binding.telegram_group_id == asset.telegram_chat_id,
        )
    )
    if effective_status == "managed" and not managed_invariants:
        # GET is intentionally read-only; report the safe effective state and
        # let an explicit reconcile persist it.
        effective_status = "degraded"

    snapshot = _parse_snapshot(binding.permissions_snapshot if binding else None)
    probe = _probe_response_from_snapshot(snapshot)
    capabilities_enabled = effective_status == "managed" and managed_invariants
    capabilities = dict.fromkeys(GOVERNANCE_CAPABILITIES, capabilities_enabled)

    pending_at = asset.governance_pending_at
    stale_pending = bool(
        effective_status == "pending"
        and pending_at is not None
        and _now() - pending_at > PENDING_STALE_AFTER
    )
    last_correlation = correlation_id or await _latest_correlation_id(db, asset.id)
    failure = None
    if asset.governance_last_error_code:
        failure = {
            "reason": asset.governance_last_error_code,
            "message": redact_sensitive_text(asset.governance_last_error_message or "治理状态异常"),
            "retryable": asset.governance_last_error_code
            not in {
                "governance_bot_change_not_supported",
                "managed_binding_conflict",
                "guardian_bot_identity_mismatch",
                "telegram_chat_type_unsupported",
            },
        }
    elif stored_status == "managed" and effective_status == "degraded":
        failure = {
            "reason": "governance_binding_inconsistent",
            "message": "治理关联不完整，请执行重新检测",
            "retryable": True,
        }

    return {
        "asset_id": asset.id,
        "asset_status": _value(asset.status),
        "telegram_chat_id": asset.telegram_chat_id,
        "core_group_id": asset.core_group_id,
        "managed_binding_id": asset.managed_binding_id,
        "guardian_bot_account_id": asset.guardian_bot_account_id,
        "guardian_bot_profile_id": guardian_profile.id if guardian_profile else None,
        "owned_bot_profile_id": owned_profile.id if owned_profile else None,
        "guardian_bot_display_name": account.display_name if account else None,
        "guardian_bot_username": (
            guardian_profile.bot_username
            if guardian_profile and guardian_profile.bot_username
            else (owned_profile.bot_username if owned_profile else None)
        ),
        "governance_status": effective_status,
        "binding_status": _value(binding.binding_status) if binding else None,
        "bot_role": _value(binding.bot_role) if binding else None,
        "permission_probe": probe,
        "capabilities": capabilities,
        "failure": failure,
        "governance_pending_at": pending_at.isoformat() if pending_at else None,
        "governance_enabled_at": (
            asset.governance_enabled_at.isoformat() if asset.governance_enabled_at else None
        ),
        "governance_last_checked_at": (
            asset.governance_last_checked_at.isoformat()
            if asset.governance_last_checked_at
            else None
        ),
        "stale_pending": stale_pending,
        "reused": reused,
        "correlation_id": last_correlation,
    }


async def list_eligible_guardian_bots(
    db: AsyncSession,
    asset_id: int,
) -> list[dict[str, Any]]:
    """Return already-filtered candidates; never return either profile's token."""

    asset = await db.get(OwnedGroupAsset, asset_id)
    if asset is None:
        raise GovernanceServiceError(
            "owned_group_asset_not_found", "自建群资产不存在", status_code=404
        )
    profiles = (
        await db.scalars(
            select(OwnedBotProfile)
            .where(OwnedBotProfile.owner_account_id == asset.owner_account_id)
            .order_by(OwnedBotProfile.id)
        )
    ).all()
    memberships = {
        item.resource_id: item
        for item in (
            await db.scalars(
                select(OwnedGroupMembership).where(
                    OwnedGroupMembership.group_asset_id == asset.id,
                    OwnedGroupMembership.resource_type == "bot",
                )
            )
        ).all()
    }
    candidates: list[dict[str, Any]] = []
    for owned_profile in profiles:
        try:
            eligible = await resolve_guardian_bot(db, asset, int(owned_profile.account_id))
        except GovernanceServiceError:
            continue
        membership = memberships.get(owned_profile.id)
        candidates.append(
            {
                "guardian_bot_account_id": eligible.account.id,
                "guardian_bot_profile_id": eligible.guardian_profile.id,
                "owned_bot_profile_id": eligible.owned_profile.id,
                "display_name": eligible.account.display_name or eligible.owned_profile.display_name,
                "username": eligible.guardian_profile.bot_username
                or eligible.owned_profile.bot_username,
                "owned_profile_status": eligible.owned_profile.status,
                "guardian_health_status": _value(eligible.guardian_profile.health_status),
                "local_membership_status": membership.status if membership else None,
                "local_is_admin": bool(membership.is_admin) if membership else False,
            }
        )
    return candidates


async def _record_preflight_failure(
    db: AsyncSession,
    *,
    asset: OwnedGroupAsset,
    actor: dict[str, Any] | None,
    operation: str,
    error: GovernanceServiceError,
    correlation_id: str,
) -> None:
    before = _audit_state(asset)
    asset.governance_status = "degraded"
    asset.governance_pending_at = None
    asset.governance_last_error_code = error.reason
    asset.governance_last_error_message = error.message
    _add_audit(
        db,
        event_type=f"owned_group_governance_{operation}_failed",
        asset=asset,
        actor=actor,
        before_state=before,
        after_state=_audit_state(asset, probe=error.probe),
        result="failed",
        reason_code=error.reason,
        correlation_id=correlation_id,
    )
    _add_audit(
        db,
        event_type="owned_group_governance_degraded",
        asset=asset,
        actor=actor,
        before_state=before,
        after_state=_audit_state(asset, probe=error.probe),
        result="failed",
        reason_code=error.reason,
        correlation_id=correlation_id,
    )
    await db.commit()


async def _finish_failure(
    db: AsyncSession,
    *,
    asset_id: int,
    expected: _PendingSnapshot,
    actor: dict[str, Any] | None,
    operation: str,
    error: GovernanceServiceError,
    correlation_id: str,
) -> None:
    await db.rollback()
    asset = await _locked_asset(db, asset_id)
    before = _audit_state(asset)
    still_same_request = (
        asset.guardian_bot_account_id == expected.guardian_bot_account_id
        and asset.telegram_chat_id == expected.telegram_chat_id
        and asset.governance_pending_at == expected.pending_at
        and asset.governance_status == "pending"
    )
    binding = (
        await db.get(ManagedGroupBinding, asset.managed_binding_id)
        if asset.managed_binding_id is not None
        else None
    )
    if still_same_request:
        asset.governance_status = "degraded"
        asset.governance_pending_at = None
        asset.governance_last_error_code = error.reason
        asset.governance_last_error_message = error.message
        if error.probe is not None:
            asset.governance_last_checked_at = error.probe.checked_at
        if binding is not None and error.binding_status is not None:
            binding.binding_status = error.binding_status
            binding.bot_role = (
                ManagedGroupBotRole(error.probe.bot_role)
                if error.probe and error.probe.bot_role in {"admin", "owner", "member"}
                else ManagedGroupBotRole.MEMBER
            )
            if error.probe is not None:
                binding.permissions_snapshot = json.dumps(
                    error.probe.as_snapshot(f"owned_group_governance_{operation}"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                binding.last_synced_at = error.probe.checked_at
    _add_audit(
        db,
        event_type=f"owned_group_governance_{operation}_failed",
        asset=asset,
        actor=actor,
        before_state=before,
        after_state=_audit_state(asset, probe=error.probe, binding=binding),
        result="failed",
        reason_code=error.reason,
        correlation_id=correlation_id,
    )
    if still_same_request:
        _add_audit(
            db,
            event_type="owned_group_governance_degraded",
            asset=asset,
            actor=actor,
            before_state=before,
            after_state=_audit_state(asset, probe=error.probe, binding=binding),
            result="failed",
            reason_code=error.reason,
            correlation_id=correlation_id,
        )
    await db.commit()


async def _mark_pending(
    db: AsyncSession,
    *,
    asset: OwnedGroupAsset,
    guardian_bot_account_id: int,
    actor: dict[str, Any] | None,
    operation: str,
    correlation_id: str,
) -> _PendingSnapshot:
    before = _audit_state(asset)
    now = _now()
    asset.guardian_bot_account_id = guardian_bot_account_id
    asset.governance_status = "pending"
    asset.governance_pending_at = now
    asset.governance_last_error_code = None
    asset.governance_last_error_message = None
    _add_audit(
        db,
        event_type=f"owned_group_governance_{operation}_started",
        asset=asset,
        actor=actor,
        before_state=before,
        after_state=_audit_state(asset),
        result="started",
        reason_code=None,
        correlation_id=correlation_id,
    )
    await db.commit()
    return _PendingSnapshot(
        telegram_chat_id=int(asset.telegram_chat_id or 0),
        guardian_bot_account_id=guardian_bot_account_id,
        pending_at=now,
    )


def _assert_pending_snapshot(asset: OwnedGroupAsset, expected: _PendingSnapshot) -> None:
    if (
        _is_archived(asset)
        or _value(asset.status) != AssetStatus.READY.value
        or asset.governance_status != "pending"
        or asset.telegram_chat_id != expected.telegram_chat_id
        or asset.guardian_bot_account_id != expected.guardian_bot_account_id
        or asset.governance_pending_at != expected.pending_at
    ):
        raise GovernanceServiceError(
            "governance_state_changed",
            "探针期间自建群治理状态已变化，结果未写入",
            status_code=409,
            retryable=True,
        )


async def _finish_success(
    db: AsyncSession,
    *,
    asset_id: int,
    expected: _PendingSnapshot,
    eligible: EligibleGuardianBot,
    probe: GuardianPermissionProbe,
    actor: dict[str, Any] | None,
    operation: str,
    correlation_id: str,
) -> bool:
    # The pending marker was committed before the external Telegram probe and
    # this project intentionally uses expire_on_commit=False. Expire the
    # identity map so a concurrent archive/chat/bot/profile change cannot be
    # overwritten by the stale objects held across that network call.
    await acquire_telegram_chat_transaction_lock(db, expected.telegram_chat_id)
    db.expire_all()
    asset = await _locked_asset(db, asset_id)
    _assert_pending_snapshot(asset, expected)
    # Re-run every local eligibility check after the external call.  The token,
    # profile state, account risk, or ownership may have changed meanwhile.
    eligible = await resolve_guardian_bot(db, asset, expected.guardian_bot_account_id)
    # The runtime stop may be enabled while the external Telegram probe is in
    # flight. Re-sample it inside the chat-locked finalization boundary before
    # creating or updating any core group, binding, or managed asset state.
    await _require_governance_gate(asset_id, correlation_id)
    before = _audit_state(asset)

    try:
        sync_result = await sync_managed_group_binding(
            db,
            bot_account_id=eligible.account.id,
            telegram_group_id=expected.telegram_chat_id,
            group_id=asset.core_group_id,
            title=asset.title,
            username=asset.telegram_username,
            member_count=asset.member_count,
            binding_status=ManagedGroupBindingStatus.ACTIVE,
            bot_role=ManagedGroupBotRole(probe.bot_role),
            permissions_snapshot=probe.as_snapshot(
                f"owned_group_governance_{operation}"
            ),
            replace_permissions_snapshot=True,
            chat_type="supergroup",
            discovery_source="owned_group_governance",
            allow_existing=True,
            reject_owned_group_auto_bind=False,
        )
    except ManagedGroupSyncConflict as exc:
        raise GovernanceServiceError(
            "managed_binding_conflict",
            "核心群已绑定另一个 Guardian Bot",
            status_code=409,
        ) from exc

    binding = sync_result.binding
    if asset.managed_binding_id is not None and asset.managed_binding_id != binding.id:
        raise GovernanceServiceError(
            "managed_binding_conflict",
            "自建群已关联另一个 Guardian 绑定",
            status_code=409,
        )

    if eligible.guardian_profile.bot_user_id is None:
        eligible.guardian_profile.bot_user_id = probe.bot_user_id
    if eligible.owned_profile.bot_user_id is None:
        eligible.owned_profile.bot_user_id = probe.bot_user_id
    if probe.bot_username:
        eligible.guardian_profile.bot_username = (
            eligible.guardian_profile.bot_username or probe.bot_username
        )
        eligible.owned_profile.bot_username = eligible.owned_profile.bot_username or probe.bot_username

    asset.core_group_id = binding.group_id
    asset.managed_binding_id = binding.id
    asset.guardian_bot_account_id = eligible.account.id
    asset.governance_status = "managed"
    asset.governance_pending_at = None
    asset.governance_enabled_at = asset.governance_enabled_at or probe.checked_at
    asset.governance_last_checked_at = probe.checked_at
    asset.governance_last_error_code = None
    asset.governance_last_error_message = None
    _add_audit(
        db,
        event_type=(
            "owned_group_governance_bound"
            if operation == "bind"
            else "owned_group_governance_reconciled"
        ),
        asset=asset,
        actor=actor,
        before_state=before,
        after_state=_audit_state(asset, probe=probe, binding=binding),
        result="success",
        reason_code="reused" if not sync_result.created_group and not sync_result.created_binding else None,
        correlation_id=correlation_id,
    )
    await db.commit()
    return not sync_result.created_group and not sync_result.created_binding


async def bind_governance(
    db: AsyncSession,
    asset_id: int,
    guardian_bot_account_id: int,
    actor: dict[str, Any] | None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    correlation = new_correlation_id(asset_id, correlation_id)
    await _require_governance_gate(asset_id, correlation)
    asset = await _locked_asset(db, asset_id)
    _validate_ready_asset(asset)

    if asset.governance_status == "managed":
        if asset.guardian_bot_account_id != guardian_bot_account_id:
            raise GovernanceServiceError(
                "governance_bot_change_not_supported",
                "第一版不支持更换主 Guardian Bot",
                status_code=409,
                correlation_id=correlation,
            )
        before = _audit_state(asset)
        _add_audit(
            db,
            event_type="owned_group_governance_bound",
            asset=asset,
            actor=actor,
            before_state=before,
            after_state=before,
            result="success",
            reason_code="reused",
            correlation_id=correlation,
        )
        await db.commit()
        return await get_governance_status(
            db, asset_id, reused=True, correlation_id=correlation
        )

    if asset.governance_status == "pending":
        raise GovernanceServiceError(
            "governance_state_changed",
            "治理接入正在执行，请刷新状态或稍后重新检测",
            status_code=409,
            retryable=True,
            correlation_id=correlation,
        )
    if asset.guardian_bot_account_id not in {None, guardian_bot_account_id}:
        raise GovernanceServiceError(
            "governance_bot_change_not_supported",
            "第一版不支持更换主 Guardian Bot",
            status_code=409,
            correlation_id=correlation,
        )

    try:
        eligible = await resolve_guardian_bot(db, asset, guardian_bot_account_id)
    except GovernanceServiceError as exc:
        await _record_preflight_failure(
            db,
            asset=asset,
            actor=actor,
            operation="bind",
            error=exc,
            correlation_id=correlation,
        )
        exc.correlation_id = correlation
        raise

    expected = await _mark_pending(
        db,
        asset=asset,
        guardian_bot_account_id=guardian_bot_account_id,
        actor=actor,
        operation="bind",
        correlation_id=correlation,
    )
    try:
        probe = await probe_guardian_permissions(asset, eligible, source="owned_group_governance_bind")
        reused = await _finish_success(
            db,
            asset_id=asset_id,
            expected=expected,
            eligible=eligible,
            probe=probe,
            actor=actor,
            operation="bind",
            correlation_id=correlation,
        )
    except GovernanceServiceError as exc:
        await _finish_failure(
            db,
            asset_id=asset_id,
            expected=expected,
            actor=actor,
            operation="bind",
            error=exc,
            correlation_id=correlation,
        )
        exc.correlation_id = correlation
        raise
    except IntegrityError as exc:
        error = GovernanceServiceError(
            "governance_state_changed",
            "治理关联并发写入冲突，请重新检测",
            status_code=409,
            retryable=True,
            correlation_id=correlation,
        )
        await _finish_failure(
            db,
            asset_id=asset_id,
            expected=expected,
            actor=actor,
            operation="bind",
            error=error,
            correlation_id=correlation,
        )
        raise error from exc
    except Exception as exc:
        error = GovernanceServiceError(
            "governance_internal_error",
            "治理接入执行失败，请重新检测",
            status_code=500,
            retryable=True,
            correlation_id=correlation,
        )
        try:
            await _finish_failure(
                db,
                asset_id=asset_id,
                expected=expected,
                actor=actor,
                operation="bind",
                error=error,
                correlation_id=correlation,
            )
        except Exception:
            # A database outage may also prevent the compensating write. The
            # committed pending marker remains visible as stale_pending and is
            # recoverable through reconcile once storage is healthy again.
            await db.rollback()
        raise error from exc

    return await get_governance_status(
        db, asset_id, reused=reused, correlation_id=correlation
    )


async def unbind_governance(
    db: AsyncSession,
    asset_id: int,
    actor: dict[str, Any] | None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Detach Guardian governance so irreversible steps (e.g. dissolution) unblock.

    This is a local bookkeeping operation only: it never contacts Telegram and
    never demotes the bot in the group.  The managed binding row is marked
    inactive so guardian views stop treating the group as governed.
    """

    correlation = new_correlation_id(asset_id, correlation_id)
    asset = await _locked_asset(db, asset_id)
    _validate_ready_asset(asset)
    current_status = str(asset.governance_status or "disabled")
    if current_status == "pending":
        raise GovernanceServiceError(
            "governance_state_changed",
            "治理操作正在执行，请等待完成或重新检测后再停用",
            status_code=409,
            retryable=True,
            correlation_id=correlation,
        )
    if current_status == "disabled" and asset.guardian_bot_account_id is None:
        raise GovernanceServiceError(
            "governance_not_bound",
            "自建群尚未接入 Guardian 治理",
            status_code=409,
            correlation_id=correlation,
        )

    before = _audit_state(asset)
    binding = (
        await db.get(ManagedGroupBinding, asset.managed_binding_id)
        if asset.managed_binding_id is not None
        else None
    )
    if binding is not None and _value(binding.binding_status) == "active":
        binding.binding_status = ManagedGroupBindingStatus.INACTIVE
    asset.managed_binding_id = None
    asset.guardian_bot_account_id = None
    asset.governance_status = "disabled"
    asset.governance_pending_at = None
    asset.governance_enabled_at = None
    asset.governance_last_error_code = None
    asset.governance_last_error_message = None
    asset.updated_at = _now()
    _add_audit(
        db,
        event_type="owned_group_governance_unbound",
        asset=asset,
        actor=actor,
        before_state=before,
        after_state=_audit_state(asset),
        result="success",
        reason_code="operator_unbind",
        correlation_id=correlation,
    )
    await db.commit()
    return await get_governance_status(db, asset_id, correlation_id=correlation)


async def reconcile_governance(
    db: AsyncSession,
    asset_id: int,
    actor: dict[str, Any] | None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    correlation = new_correlation_id(asset_id, correlation_id)
    await _require_governance_gate(asset_id, correlation)
    asset = await _locked_asset(db, asset_id)
    _validate_ready_asset(asset)
    if asset.guardian_bot_account_id is None:
        raise GovernanceServiceError(
            "governance_not_bound",
            "自建群尚未选择 Guardian Bot",
            status_code=409,
            correlation_id=correlation,
        )
    selected_bot = int(asset.guardian_bot_account_id)
    expected = await _mark_pending(
        db,
        asset=asset,
        guardian_bot_account_id=selected_bot,
        actor=actor,
        operation="reconcile",
        correlation_id=correlation,
    )
    try:
        eligible = await resolve_guardian_bot(db, asset, selected_bot)
        probe = await probe_guardian_permissions(
            asset, eligible, source="owned_group_governance_reconcile"
        )
        reused = await _finish_success(
            db,
            asset_id=asset_id,
            expected=expected,
            eligible=eligible,
            probe=probe,
            actor=actor,
            operation="reconcile",
            correlation_id=correlation,
        )
    except GovernanceServiceError as exc:
        await _finish_failure(
            db,
            asset_id=asset_id,
            expected=expected,
            actor=actor,
            operation="reconcile",
            error=exc,
            correlation_id=correlation,
        )
        exc.correlation_id = correlation
        raise
    except IntegrityError as exc:
        error = GovernanceServiceError(
            "governance_state_changed",
            "治理关联并发写入冲突，请重新检测",
            status_code=409,
            retryable=True,
            correlation_id=correlation,
        )
        await _finish_failure(
            db,
            asset_id=asset_id,
            expected=expected,
            actor=actor,
            operation="reconcile",
            error=error,
            correlation_id=correlation,
        )
        raise error from exc
    except Exception as exc:
        error = GovernanceServiceError(
            "governance_internal_error",
            "治理重新检测失败，请稍后重试",
            status_code=500,
            retryable=True,
            correlation_id=correlation,
        )
        try:
            await _finish_failure(
                db,
                asset_id=asset_id,
                expected=expected,
                actor=actor,
                operation="reconcile",
                error=error,
                correlation_id=correlation,
            )
        except Exception:
            await db.rollback()
        raise error from exc

    return await get_governance_status(
        db, asset_id, reused=reused, correlation_id=correlation
    )


__all__ = [
    "GOVERNANCE_CAPABILITIES",
    "GOVERNANCE_STATUSES",
    "PENDING_STALE_AFTER",
    "REQUIRED_GUARDIAN_PERMISSIONS",
    "EligibleGuardianBot",
    "GovernanceServiceError",
    "GuardianPermissionProbe",
    "bind_governance",
    "get_governance_status",
    "list_eligible_guardian_bots",
    "new_correlation_id",
    "probe_guardian_permissions",
    "reconcile_governance",
    "resolve_guardian_bot",
]
