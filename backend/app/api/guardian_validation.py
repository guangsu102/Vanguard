"""
Shared validation helpers for guardian-managed group APIs.
"""

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountType, TelegramAccount
from app.core.group.models import Group
from app.core.security import get_current_user
from app.modules.guardian.models import ManagedGroupBinding

_GUARDIAN_READER_ROLES = {"admin", "operator", "auditor"}


@dataclass(frozen=True, slots=True)
class GuardianGroupTarget:
    """Explicitly carry internal and Telegram IDs for Guardian operations."""

    core_group_id: int
    telegram_chat_id: int
    managed_binding_id: int
    bot_account_id: int
    binding: ManagedGroupBinding = field(repr=False, compare=False)


async def require_guardian_reader(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Allow operational roles to inspect Guardian-managed group state."""

    if current_user.get("role") not in _GUARDIAN_READER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "owned_group_role_forbidden",
                "message": "Guardian read access required",
                "retryable": False,
            },
        )
    return current_user


async def require_guardian_operator(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Allow only administrators and operators to mutate Guardian state."""

    if current_user.get("role") not in {"admin", "operator"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "owned_group_role_forbidden",
                "message": "Guardian operator access required",
                "retryable": False,
            },
        )
    return current_user


async def ensure_guardian_bot_account(db: AsyncSession, account_id: int) -> TelegramAccount:
    """Ensure the given account exists and is a guardian bot."""
    account = await db.get(TelegramAccount, account_id)
    if not account or account.account_type != AccountType.GUARDIAN_BOT:
        raise HTTPException(status_code=400, detail="bot_account_id must reference a guardian_bot account")
    return account


async def resolve_guardian_group_target(
    db: AsyncSession,
    telegram_chat_id: int,
) -> GuardianGroupTarget | None:
    """Resolve a Telegram chat ID to its binding and internal core group ID."""

    rows = (
        await db.execute(
            select(ManagedGroupBinding, Group)
            .outerjoin(Group, Group.id == ManagedGroupBinding.group_id)
            .where(ManagedGroupBinding.telegram_group_id == telegram_chat_id)
        )
    ).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "managed_group_binding_ambiguous",
                "message": "Multiple managed bindings reference this Telegram group",
            },
        )
    binding, group = rows[0]
    if group is None or int(group.group_id) != int(telegram_chat_id):
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "guardian_group_id_mismatch",
                "message": "Managed binding core group does not match the Telegram group",
            },
        )
    return GuardianGroupTarget(
        core_group_id=binding.group_id,
        telegram_chat_id=binding.telegram_group_id,
        managed_binding_id=binding.id,
        bot_account_id=binding.bot_account_id,
        binding=binding,
    )


async def ensure_managed_group_binding(
    db: AsyncSession,
    telegram_chat_id: int,
) -> GuardianGroupTarget:
    """Ensure a Telegram chat is managed and return its explicit ID target."""

    target = await resolve_guardian_group_target(db, telegram_chat_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    return target


async def ensure_managed_group_bindings(
    db: AsyncSession,
    telegram_group_ids: list[int],
) -> list[ManagedGroupBinding]:
    """Ensure every Telegram group ID belongs to a managed-group binding."""
    unique_group_ids = list(dict.fromkeys(telegram_group_ids))
    if not unique_group_ids:
        raise HTTPException(status_code=400, detail="target_group_ids is required for managed_group campaigns")

    rows = (
        await db.execute(
            select(ManagedGroupBinding, Group)
            .outerjoin(Group, Group.id == ManagedGroupBinding.group_id)
            .where(ManagedGroupBinding.telegram_group_id.in_(unique_group_ids))
        )
    ).all()
    bindings_by_chat: dict[int, list[tuple[ManagedGroupBinding, Group | None]]] = {}
    for binding, group in rows:
        bindings_by_chat.setdefault(int(binding.telegram_group_id), []).append(
            (binding, group)
        )

    missing_group_ids = [
        group_id for group_id in unique_group_ids if group_id not in bindings_by_chat
    ]
    if missing_group_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Managed group binding not found for Telegram group(s): {', '.join(map(str, missing_group_ids))}",
        )

    bindings: list[ManagedGroupBinding] = []
    for telegram_chat_id in unique_group_ids:
        candidates = bindings_by_chat[telegram_chat_id]
        if len(candidates) != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "managed_group_binding_ambiguous",
                    "message": "Multiple managed bindings reference a Telegram group",
                },
            )
        binding, group = candidates[0]
        if group is None or int(group.group_id) != int(telegram_chat_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "guardian_group_id_mismatch",
                    "message": "Managed binding core group does not match the Telegram group",
                },
            )
        bindings.append(binding)

    return bindings
