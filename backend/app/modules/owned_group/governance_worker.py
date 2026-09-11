"""Fail-closed target resolution for self-owned group Guardian events.

The Guardian Bot API receives Telegram chat IDs, while policy and punishment
tables use the internal ``Group.id``. This module keeps that boundary explicit
and centralizes the additional checks required before a worker dispatches an
event from a self-owned group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.governance_gate import (
    get_governance_gate_state,
    is_owned_group_governance_enabled,
)
from app.core.group.models import Group
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.owned_group.governance import REQUIRED_GUARDIAN_PERMISSIONS
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupAuditEvent


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


@dataclass(frozen=True, slots=True)
class GuardianWorkerTarget:
    """Resolved IDs and the decision for one incoming group update."""

    allowed: bool
    reason_code: str
    telegram_chat_id: int
    bot_account_id: int
    core_group_id: int | None = None
    managed_binding_id: int | None = None
    owned_group_asset_id: int | None = None
    binding: ManagedGroupBinding | None = field(default=None, repr=False, compare=False)
    asset: OwnedGroupAsset | None = field(default=None, repr=False, compare=False)

    @property
    def is_owned_group(self) -> bool:
        return self.owned_group_asset_id is not None


@dataclass(frozen=True, slots=True)
class GuardianMemberEvaluation:
    """Safe, whitelisted result of inspecting the bot's group membership."""

    binding_status: ManagedGroupBindingStatus
    bot_role: ManagedGroupBotRole
    reason_code: str | None
    missing_permissions: tuple[str, ...]
    snapshot: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.reason_code is None


def owned_governance_audit_state(
    target: GuardianWorkerTarget,
    *,
    permission_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a strict allowlist state for worker/campaign audit events."""

    asset = target.asset
    binding = target.binding
    state: dict[str, Any] = {
        "asset_status": _value(asset.status) if asset is not None else None,
        "governance_status": (
            _value(asset.governance_status) if asset is not None else None
        ),
        "guardian_bot_account_id": (
            asset.guardian_bot_account_id if asset is not None else target.bot_account_id
        ),
        "core_group_id": target.core_group_id,
        "managed_binding_id": target.managed_binding_id,
        "telegram_chat_id": target.telegram_chat_id,
        "binding_status": _value(binding.binding_status) if binding is not None else None,
        "bot_role": _value(binding.bot_role) if binding is not None else None,
        "reason_code": target.reason_code,
    }
    if permission_snapshot is not None:
        state["permission_probe"] = {
            key: permission_snapshot.get(key)
            for key in (
                "source",
                "checked_at",
                "chat_type",
                "bot_user_id",
                "bot_role",
                "can_delete_messages",
                "can_restrict_members",
                "can_invite_users",
                "can_pin_messages",
                "missing_permissions",
            )
        }
    return state


def add_owned_governance_audit(
    db: AsyncSession,
    target: GuardianWorkerTarget,
    *,
    event_type: str,
    result: str,
    reason_code: str,
    correlation_id: str,
    before_state: dict[str, Any] | None = None,
    permission_snapshot: dict[str, Any] | None = None,
) -> None:
    """Append a secret-free audit event for an owned target only."""

    if target.asset is None:
        return
    after_state = owned_governance_audit_state(
        target, permission_snapshot=permission_snapshot
    )
    after_state["reason_code"] = reason_code
    db.add(
        OwnedGroupAuditEvent(
            event_type=event_type,
            group_asset_id=target.asset.id,
            resource_type="bot",
            resource_id=target.asset.guardian_bot_account_id,
            actor_id=None,
            before_state=json.dumps(
                before_state if before_state is not None else after_state,
                ensure_ascii=False,
                sort_keys=True,
            ),
            after_state=json.dumps(after_state, ensure_ascii=False, sort_keys=True),
            result=result,
            reason_code=reason_code,
            correlation_id=correlation_id[:128],
        )
    )


async def owned_group_governance_gate_reason() -> str | None:
    """Return a stable worker skip reason, leaving old managed groups unaffected."""

    if not is_owned_group_governance_enabled():
        return "governance_feature_disabled"
    state = await get_governance_gate_state()
    if not state.backend_available:
        return "governance_gate_backend_unavailable"
    if state.governance_stop:
        return "governance_stop_enabled"
    return None


async def resolve_governance_worker_target(
    db: AsyncSession,
    *,
    telegram_chat_id: int,
    bot_account_id: int,
    owned_gate_reason: str | None = None,
) -> GuardianWorkerTarget:
    """Resolve a group update and enforce binding/asset identity invariants.

    ``owned_gate_reason`` is a caller-supplied current gate sample. It applies
    only when a matching ``OwnedGroupAsset`` exists; legacy managed groups
    retain their current behavior.
    """

    asset = await db.scalar(
        select(OwnedGroupAsset)
        .where(OwnedGroupAsset.telegram_chat_id == int(telegram_chat_id))
        .execution_options(populate_existing=True)
    )
    bindings = (
        await db.execute(
            select(ManagedGroupBinding)
            .where(ManagedGroupBinding.telegram_group_id == int(telegram_chat_id))
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    group_ids = list({int(item.group_id) for item in bindings})
    groups_by_id = {
        int(group.id): group
        for group in (
            (
                await db.execute(
                    select(Group)
                    .where(Group.id.in_(group_ids))
                    .execution_options(populate_existing=True)
                )
            ).scalars().all()
            if group_ids
            else []
        )
    }

    def binding_matches_chat(binding: ManagedGroupBinding) -> bool:
        group = groups_by_id.get(int(binding.group_id))
        return group is not None and int(group.group_id) == int(telegram_chat_id)

    def decision(
        allowed: bool,
        reason_code: str,
        binding: ManagedGroupBinding | None = None,
    ) -> GuardianWorkerTarget:
        return GuardianWorkerTarget(
            allowed=allowed,
            reason_code=reason_code,
            telegram_chat_id=int(telegram_chat_id),
            bot_account_id=int(bot_account_id),
            core_group_id=int(binding.group_id) if binding is not None else None,
            managed_binding_id=int(binding.id) if binding is not None else None,
            owned_group_asset_id=int(asset.id) if asset is not None else None,
            binding=binding,
            asset=asset,
        )

    if asset is not None:
        if owned_gate_reason:
            return decision(False, owned_gate_reason)
        if _value(asset.status) != "ready" or asset.archived_at is not None:
            return decision(False, "owned_group_asset_not_ready")
        if asset.managed_binding_id is None or asset.core_group_id is None:
            return decision(False, "owned_group_binding_missing")
        if asset.guardian_bot_account_id != int(bot_account_id):
            return decision(False, "mismatched_guardian_bot")
        if _value(asset.governance_status) != "managed":
            return decision(False, "owned_group_governance_not_managed")

        account = await db.get(
            TelegramAccount,
            int(bot_account_id),
            populate_existing=True,
        )
        guardian_profiles = (
            await db.scalars(
                select(GuardianBotProfile).where(
                    GuardianBotProfile.account_id == int(bot_account_id)
                ).execution_options(populate_existing=True)
            )
        ).all()
        owned_profiles = (
            await db.scalars(
                select(OwnedBotProfile).where(
                    OwnedBotProfile.account_id == int(bot_account_id)
                ).execution_options(populate_existing=True)
            )
        ).all()
        if (
            account is None
            or _value(account.account_type) != AccountType.GUARDIAN_BOT.value
            or not bool(account.is_active)
        ):
            return decision(False, "guardian_bot_account_invalid")
        if len(guardian_profiles) != 1 or not bool(guardian_profiles[0].enabled):
            return decision(False, "guardian_bot_profile_invalid")
        eligible_owned_profiles = [
            profile
            for profile in owned_profiles
            if profile.owner_account_id == asset.owner_account_id
            and bool(profile.enabled)
            and _value(profile.status) == "verified"
        ]
        if len(eligible_owned_profiles) != 1:
            return decision(False, "owned_bot_profile_invalid")
        guardian_user_id = guardian_profiles[0].bot_user_id
        owned_user_id = eligible_owned_profiles[0].bot_user_id
        if (
            guardian_user_id is not None
            and owned_user_id is not None
            and int(guardian_user_id) != int(owned_user_id)
        ):
            return decision(False, "guardian_bot_identity_mismatch")

        expected = next(
            (item for item in bindings if item.id == asset.managed_binding_id),
            None,
        )
        if expected is None:
            return decision(False, "owned_group_binding_missing")
        if expected.bot_account_id != int(bot_account_id):
            return decision(False, "mismatched_guardian_bot", expected)
        if expected.group_id != asset.core_group_id:
            return decision(False, "owned_group_core_id_mismatch", expected)
        if not binding_matches_chat(expected):
            return decision(False, "core_group_telegram_id_mismatch", expected)
        if _value(expected.binding_status) != ManagedGroupBindingStatus.ACTIVE.value:
            return decision(False, "managed_binding_not_active", expected)
        return decision(True, "allowed_owned_group", expected)

    matching = [item for item in bindings if item.bot_account_id == int(bot_account_id)]
    if not matching:
        return decision(
            False,
            "mismatched_guardian_bot" if bindings else "managed_binding_missing",
        )
    if len(matching) != 1:
        return decision(False, "managed_binding_ambiguous")
    binding = matching[0]
    if not binding_matches_chat(binding):
        return decision(False, "core_group_telegram_id_mismatch", binding)
    if _value(binding.binding_status) != ManagedGroupBindingStatus.ACTIVE.value:
        return decision(False, "managed_binding_not_active", binding)
    return decision(True, "allowed_managed_group", binding)


def evaluate_guardian_member(
    member: dict[str, Any],
    *,
    chat_type: str | None,
    bot_user_id: int,
    checked_at: datetime | None = None,
    probe_failed: bool = False,
) -> GuardianMemberEvaluation:
    """Map a Bot API member payload to a safe snapshot and dispatch status."""

    now = checked_at or datetime.utcnow()
    status = str(member.get("status") or "unknown").strip().lower()
    normalized_chat_type = str(chat_type or "unknown").strip().lower()
    if status == "creator":
        role = ManagedGroupBotRole.OWNER
        granted = list(REQUIRED_GUARDIAN_PERMISSIONS)
    elif status == "administrator":
        role = ManagedGroupBotRole.ADMIN
        granted = [
            permission
            for permission in REQUIRED_GUARDIAN_PERMISSIONS
            if member.get(permission) is True
        ]
    else:
        role = ManagedGroupBotRole.MEMBER
        granted = []
    missing = [
        permission
        for permission in REQUIRED_GUARDIAN_PERMISSIONS
        if permission not in granted
    ]

    if probe_failed:
        binding_status = ManagedGroupBindingStatus.DEGRADED
        reason_code = "guardian_permission_probe_failed"
    elif status in {"left", "kicked"}:
        binding_status = ManagedGroupBindingStatus.INACTIVE
        reason_code = "guardian_bot_not_member"
    elif status not in {"creator", "administrator"}:
        binding_status = ManagedGroupBindingStatus.DEGRADED
        reason_code = "guardian_bot_not_admin"
    elif normalized_chat_type != "supergroup":
        binding_status = ManagedGroupBindingStatus.DEGRADED
        reason_code = "telegram_chat_type_unsupported"
    elif missing:
        binding_status = ManagedGroupBindingStatus.DEGRADED
        reason_code = "guardian_permissions_missing"
    else:
        binding_status = ManagedGroupBindingStatus.ACTIVE
        reason_code = None

    snapshot = {
        "schema_version": 1,
        "source": "guardian_worker_updates",
        "chat_type": normalized_chat_type,
        "bot_user_id": int(bot_user_id),
        "bot_status": status,
        "bot_role": role.value,
        "required_permissions": list(REQUIRED_GUARDIAN_PERMISSIONS),
        "granted_permissions": granted,
        "missing_permissions": missing,
        "probed_at": now.isoformat(),
    }
    return GuardianMemberEvaluation(
        binding_status=binding_status,
        bot_role=role,
        reason_code=reason_code,
        missing_permissions=tuple(missing),
        snapshot=snapshot,
    )


__all__ = [
    "GuardianMemberEvaluation",
    "GuardianWorkerTarget",
    "evaluate_guardian_member",
    "owned_group_governance_gate_reason",
    "resolve_governance_worker_target",
]
