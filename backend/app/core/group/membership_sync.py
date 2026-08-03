"""
Synchronize Telegram account memberships from joined dialogs.

When an operator logs in or imports a promoter account, Telegram already knows
which groups that account belongs to. This module mirrors those memberships into
the local group pool so automation can target them immediately.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator, Iterable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountType, TelegramAccount
from app.core.account.session_crypto import decrypt_session_string
from app.core.group.auto_rating import apply_joined_group_auto_rating
from app.core.group.manager import GroupManager
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.search.group_finder import (
    is_joinable_group_info,
    telegram_chat_to_dict,
)

logger = structlog.get_logger()

DEFAULT_SYNC_TIMEOUT_SECONDS = 45
DISCOVERY_SOURCE = "account_dialog_sync"


@dataclass(frozen=True)
class MembershipSyncResult:
    """Summary of a dialog membership sync."""

    scanned: int = 0
    imported: int = 0
    skipped: int = 0


async def sync_account_joined_groups(
    db: AsyncSession,
    account: TelegramAccount,
    *,
    timeout_seconds: int = DEFAULT_SYNC_TIMEOUT_SECONDS,
) -> MembershipSyncResult:
    """
    Best-effort sync of groups already joined by a Telegram account.

    Only promoter accounts are synced. Broadcast channels and private chats are
    ignored. Failures are logged and converted into an empty result so login
    flows are not blocked by temporary Telegram/proxy issues.
    """

    if account.account_type != AccountType.PROMOTER:
        return MembershipSyncResult()
    if not decrypt_session_string(account.session_string):
        return MembershipSyncResult()

    try:
        return await asyncio.wait_for(
            _sync_account_joined_groups(db, account),
            timeout=max(1, int(timeout_seconds)),
        )
    except Exception as exc:
        logger.warning(
            "account_dialog_membership_sync_failed",
            account_id=account.id,
            identifier=account.identifier,
            error=str(exc),
        )
        await db.rollback()
        return MembershipSyncResult()


async def _sync_account_joined_groups(
    db: AsyncSession,
    account: TelegramAccount,
) -> MembershipSyncResult:
    from app.core.account.pool import get_account_pool

    pool = get_account_pool()
    wrapper = await pool.get_account_by_id(account.id)
    if wrapper is None:
        wrapper = await pool.add_account_from_db(account)

    connected = await pool.connect_by_id(
        account.id,
        purpose="account_dialog_membership_sync",
        require_session=True,
        keep_connected=False,
    )
    if connected is None or connected.client is None:
        return MembershipSyncResult()

    scanned = 0
    imported = 0
    skipped = 0
    group_manager = GroupManager(db)

    try:
        async for dialog in _iter_dialogs(connected.client):
            scanned += 1
            entity = getattr(dialog, "entity", dialog)
            info = telegram_chat_to_dict(entity)
            title = getattr(dialog, "title", None) or info.get("title") or ""
            username = getattr(dialog, "username", None) or info.get("username")
            if title:
                info["title"] = title
            if username:
                info["username"] = username

            if not is_joinable_group_info(info):
                skipped += 1
                continue

            group_id = int(info.get("id") or 0)
            if not group_id:
                skipped += 1
                continue

            await _upsert_synced_group_membership(
                db,
                account_id=account.id,
                group_id=group_id,
                title=title,
                username=username,
                member_count=int(info.get("participants_count") or 0),
                group_manager=group_manager,
            )
            imported += 1

        await db.commit()
        logger.info(
            "account_dialog_membership_sync_completed",
            account_id=account.id,
            scanned=scanned,
            imported=imported,
            skipped=skipped,
        )
        return MembershipSyncResult(scanned=scanned, imported=imported, skipped=skipped)
    finally:
        await pool.release(connected)


async def _iter_dialogs(client: Any) -> AsyncIterator[Any]:
    if hasattr(client, "iter_dialogs"):
        dialogs = client.iter_dialogs()
        if hasattr(dialogs, "__aiter__"):
            async for dialog in dialogs:
                yield dialog
            return

        for dialog in dialogs:
            yield dialog
        return

    dialogs = await client.get_dialogs()
    if isinstance(dialogs, Iterable):
        for dialog in dialogs:
            yield dialog


async def _upsert_synced_group_membership(
    db: AsyncSession,
    *,
    account_id: int,
    group_id: int,
    title: str,
    username: str | None,
    member_count: int,
    group_manager: GroupManager | None = None,
) -> GroupAccountMembership:
    now = datetime.utcnow()
    if group_manager is None:
        group_manager = GroupManager(db)

    group_result = await db.execute(select(Group).where(Group.group_id == group_id))
    group = group_result.scalar_one_or_none()
    if group is None:
        group = Group(
            group_id=group_id,
            title=title or None,
            username=username,
            member_count=max(0, member_count),
            status="active",
            discovery_source=DISCOVERY_SOURCE,
            level=GroupLevel.UNRATED,
        )
        db.add(group)
        await db.flush()
    else:
        if title:
            group.title = title
        if username:
            group.username = username
        if member_count:
            group.member_count = member_count
        if group.status in {"pending", "left", "rejected"}:
            group.status = "active"
        group.updated_at = now

    await apply_joined_group_auto_rating(group, group_manager.scorer)

    membership_result = await db.execute(
        select(GroupAccountMembership).where(
            GroupAccountMembership.group_id == group.id,
            GroupAccountMembership.account_id == account_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        membership = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account_id,
            status="joined",
            join_method=DISCOVERY_SOURCE,
            source_keyword=group.source_keyword,
            joined_at=now,
            left_at=None,
            last_checked_at=now,
        )
        db.add(membership)
    else:
        membership.status = "joined"
        membership.join_method = DISCOVERY_SOURCE
        membership.telegram_group_id = group.group_id
        membership.last_checked_at = now
        membership.updated_at = now
        if membership.joined_at is None:
            membership.joined_at = now
        membership.left_at = None

    return membership
