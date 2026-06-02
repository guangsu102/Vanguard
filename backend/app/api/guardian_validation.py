"""
Shared validation helpers for guardian-managed group APIs.
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountType, TelegramAccount
from app.modules.guardian.models import ManagedGroupBinding


async def ensure_guardian_bot_account(db: AsyncSession, account_id: int) -> TelegramAccount:
    """Ensure the given account exists and is a guardian bot."""
    account = await db.get(TelegramAccount, account_id)
    if not account or account.account_type != AccountType.GUARDIAN_BOT:
        raise HTTPException(status_code=400, detail="bot_account_id must reference a guardian_bot account")
    return account


async def ensure_managed_group_binding(
    db: AsyncSession,
    telegram_group_id: int,
) -> ManagedGroupBinding:
    """Ensure the Telegram group is a known guardian-managed group."""
    result = await db.execute(
        select(ManagedGroupBinding).where(ManagedGroupBinding.telegram_group_id == telegram_group_id)
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    return binding


async def ensure_managed_group_bindings(
    db: AsyncSession,
    telegram_group_ids: list[int],
) -> list[ManagedGroupBinding]:
    """Ensure every Telegram group ID belongs to a managed-group binding."""
    unique_group_ids = list(dict.fromkeys(telegram_group_ids))
    if not unique_group_ids:
        raise HTTPException(status_code=400, detail="target_group_ids is required for managed_group campaigns")

    result = await db.execute(
        select(ManagedGroupBinding).where(ManagedGroupBinding.telegram_group_id.in_(unique_group_ids))
    )
    bindings = {binding.telegram_group_id: binding for binding in result.scalars().all()}

    missing_group_ids = [group_id for group_id in unique_group_ids if group_id not in bindings]
    if missing_group_ids:
        raise HTTPException(
            status_code=404,
            detail=f"Managed group binding not found for Telegram group(s): {', '.join(map(str, missing_group_ids))}",
        )

    return [bindings[group_id] for group_id in unique_group_ids]
