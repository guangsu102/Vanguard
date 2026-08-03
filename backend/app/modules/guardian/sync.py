"""Guardian bot group synchronization helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.group.models import Group, GroupLevel
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
    VerificationType,
)


class ManagedGroupSyncConflict(ValueError):
    """Raised when a group is already bound to a different guardian bot."""


@dataclass(slots=True)
class ManagedGroupSyncResult:
    binding: ManagedGroupBinding
    created_group: bool = False
    created_binding: bool = False
    updated_binding: bool = False


def _merge_permissions_snapshot(existing_raw: Optional[str], incoming: dict[str, Any]) -> str:
    existing: dict[str, Any] = {}
    if existing_raw:
        try:
            parsed = json.loads(existing_raw)
            if isinstance(parsed, dict):
                existing = parsed
        except Exception:
            existing = {"raw": existing_raw}

    existing.update(incoming)
    return json.dumps(existing, ensure_ascii=False)


def guardian_role_and_status_from_member(
    member: dict[str, Any],
) -> tuple[ManagedGroupBotRole, ManagedGroupBindingStatus]:
    """Map Telegram getChatMember status to Vanguard managed-group state."""

    status = member.get("status")
    if status == "creator":
        return ManagedGroupBotRole.OWNER, ManagedGroupBindingStatus.ACTIVE
    if status == "administrator":
        return ManagedGroupBotRole.ADMIN, ManagedGroupBindingStatus.ACTIVE
    if status in {"left", "kicked"}:
        return ManagedGroupBotRole.MEMBER, ManagedGroupBindingStatus.INACTIVE
    return ManagedGroupBotRole.MEMBER, ManagedGroupBindingStatus.DEGRADED


async def ensure_default_group_governance(db: AsyncSession, telegram_group_id: int) -> None:
    """Create default governance policy rows for a managed Telegram group."""

    verification = await db.execute(
        select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == telegram_group_id)
    )
    if verification.scalar_one_or_none() is None:
        db.add(
            GroupVerificationConfig(
                group_id=telegram_group_id,
                enable_verification=False,
                verification_type=VerificationType.CAPTCHA,
                timeout_minutes=5,
                max_attempts=3,
                whitelist_bypass=True,
                auto_kick_unverified=False,
                kick_after_minutes=10,
            )
        )

    moderation = await db.execute(
        select(GroupModerationPolicy).where(GroupModerationPolicy.group_id == telegram_group_id)
    )
    if moderation.scalar_one_or_none() is None:
        db.add(GroupModerationPolicy(group_id=telegram_group_id))

    punishment = await db.execute(
        select(GroupPunishmentPolicy).where(GroupPunishmentPolicy.group_id == telegram_group_id)
    )
    if punishment.scalar_one_or_none() is None:
        db.add(GroupPunishmentPolicy(group_id=telegram_group_id))


async def sync_managed_group_binding(
    db: AsyncSession,
    *,
    bot_account_id: int,
    telegram_group_id: int,
    group_id: Optional[int] = None,
    title: Optional[str] = None,
    username: Optional[str] = None,
    member_count: Optional[int] = None,
    binding_status: ManagedGroupBindingStatus = ManagedGroupBindingStatus.ACTIVE,
    bot_role: ManagedGroupBotRole = ManagedGroupBotRole.ADMIN,
    permissions_snapshot: Optional[dict[str, Any]] = None,
    chat_type: str = "group",
    discovery_source: str = "guardian_binding",
    allow_existing: bool = True,
) -> ManagedGroupSyncResult:
    """Create or update the primary guardian-bot binding for a Telegram group."""

    now = datetime.utcnow()
    normalized_chat_type = chat_type if chat_type in {"group", "supergroup", "channel"} else "group"
    incoming_permissions = {"chat_type": normalized_chat_type, **(permissions_snapshot or {})}
    created_group = False
    updated_binding = False

    group: Optional[Group] = None
    if group_id is not None:
        group = await db.get(Group, group_id)
    if group is None:
        result = await db.execute(select(Group).where(Group.group_id == telegram_group_id))
        group = result.scalar_one_or_none()

    if group is None:
        group = Group(
            group_id=telegram_group_id,
            title=title,
            username=username,
            member_count=member_count or 0,
            status="active",
            discovery_source=discovery_source,
            source_keyword=None,
            level=GroupLevel.UNRATED,
        )
        db.add(group)
        await db.flush()
        created_group = True
    else:
        if title:
            group.title = title
        if username is not None:
            group.username = username
        if member_count is not None:
            group.member_count = member_count
        group.updated_at = now

    result = await db.execute(
        select(ManagedGroupBinding).where(ManagedGroupBinding.group_id == group.id)
    )
    binding = result.scalar_one_or_none()

    if binding is not None:
        if not allow_existing:
            raise ManagedGroupSyncConflict("This group already has a primary guardian bot")
        if binding.bot_account_id != bot_account_id:
            raise ManagedGroupSyncConflict(
                "This group is already bound to a different guardian bot"
            )

        binding.telegram_group_id = telegram_group_id
        binding.binding_status = binding_status
        binding.bot_role = bot_role
        binding.permissions_snapshot = _merge_permissions_snapshot(
            binding.permissions_snapshot, incoming_permissions
        )
        binding.last_synced_at = now
        updated_binding = True
    else:
        binding = ManagedGroupBinding(
            group_id=group.id,
            telegram_group_id=telegram_group_id,
            bot_account_id=bot_account_id,
            binding_status=binding_status,
            bot_role=bot_role,
            permissions_snapshot=json.dumps(incoming_permissions, ensure_ascii=False),
            last_synced_at=now,
        )
        db.add(binding)

    if normalized_chat_type != "channel":
        await ensure_default_group_governance(db, telegram_group_id)
    await db.flush()
    return ManagedGroupSyncResult(
        binding=binding,
        created_group=created_group,
        created_binding=not updated_binding,
        updated_binding=updated_binding,
    )
