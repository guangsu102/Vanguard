"""Guardian bot group synchronization helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.group.models import Group, GroupLevel
from app.core.telegram_chat_lock import acquire_telegram_chat_transaction_lock
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
    VerificationType,
)
from app.modules.owned_group.models import OwnedGroupAsset


class ManagedGroupSyncConflict(ValueError):
    """Raised when a group is already bound to a different guardian bot."""


@dataclass(slots=True)
class ManagedGroupSyncResult:
    binding: ManagedGroupBinding
    created_group: bool = False
    created_binding: bool = False
    updated_binding: bool = False


def _merge_permissions_snapshot(existing_raw: str | None, incoming: dict[str, Any]) -> str:
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


async def ensure_default_group_governance(db: AsyncSession, core_group_id: int) -> None:
    """Create default policies keyed by the internal ``Group.id``."""

    verification = await db.execute(
        select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == core_group_id)
    )
    if verification.scalar_one_or_none() is None:
        db.add(
            GroupVerificationConfig(
                group_id=core_group_id,
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
        select(GroupModerationPolicy).where(GroupModerationPolicy.group_id == core_group_id)
    )
    if moderation.scalar_one_or_none() is None:
        db.add(GroupModerationPolicy(group_id=core_group_id))

    punishment = await db.execute(
        select(GroupPunishmentPolicy).where(GroupPunishmentPolicy.group_id == core_group_id)
    )
    if punishment.scalar_one_or_none() is None:
        db.add(GroupPunishmentPolicy(group_id=core_group_id))


async def sync_managed_group_binding(
    db: AsyncSession,
    *,
    bot_account_id: int,
    telegram_group_id: int,
    group_id: int | None = None,
    title: str | None = None,
    username: str | None = None,
    member_count: int | None = None,
    binding_status: ManagedGroupBindingStatus = ManagedGroupBindingStatus.ACTIVE,
    bot_role: ManagedGroupBotRole = ManagedGroupBotRole.ADMIN,
    permissions_snapshot: dict[str, Any] | None = None,
    replace_permissions_snapshot: bool = False,
    chat_type: str = "group",
    discovery_source: str = "guardian_binding",
    allow_existing: bool = True,
    reject_owned_group_auto_bind: bool = True,
) -> ManagedGroupSyncResult:
    """Create or update the primary guardian-bot binding for a Telegram group."""

    # Keep the final ownership classification and binding write in one
    # transaction-scoped chat critical section. Generic managed-group sync is
    # fail-closed by default; only an explicit owned-governance path may opt out
    # after it has validated the classified asset and selected Guardian bot.
    await acquire_telegram_chat_transaction_lock(db, telegram_group_id)
    if reject_owned_group_auto_bind:
        owned_asset_id = await db.scalar(
            select(OwnedGroupAsset.id).where(
                OwnedGroupAsset.telegram_chat_id == int(telegram_group_id)
            )
        )
        if owned_asset_id is not None:
            raise ManagedGroupSyncConflict(
                "Self-owned groups require explicit Guardian governance binding"
            )

    now = datetime.utcnow()
    normalized_chat_type = chat_type if chat_type in {"group", "supergroup", "channel"} else "group"
    incoming_permissions = {"chat_type": normalized_chat_type, **(permissions_snapshot or {})}
    created_group = False
    updated_binding = False

    group: Group | None = None
    if group_id is not None:
        group = await db.get(Group, group_id)
        if group is None:
            raise ManagedGroupSyncConflict("The requested core group does not exist")
        if int(group.group_id) != int(telegram_group_id):
            raise ManagedGroupSyncConflict(
                "The requested core group does not match the Telegram group"
            )
    else:
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
        binding.permissions_snapshot = (
            json.dumps(incoming_permissions, ensure_ascii=False, sort_keys=True)
            if replace_permissions_snapshot
            else _merge_permissions_snapshot(
                binding.permissions_snapshot, incoming_permissions
            )
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
        await ensure_default_group_governance(db, group.id)
    await db.flush()
    return ManagedGroupSyncResult(
        binding=binding,
        created_group=created_group,
        created_binding=not updated_binding,
        updated_binding=updated_binding,
    )
