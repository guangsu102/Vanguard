"""
Punishment Module

Enforcement actions for moderation violations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.modules.guardian.models import ViolationAction

logger = structlog.get_logger()


class PunishmentType(str, Enum):
    WARN = "warn"
    MUTE = "mute"
    BAN = "ban"
    KICK = "kick"


@dataclass
class PunishmentResult:
    success: bool
    punishment_type: PunishmentType
    user_id: int
    group_id: int
    reason: Optional[str] = None
    until_date: Optional[datetime] = None
    error: Optional[str] = None


class PunishmentManager:
    """Execute moderation punishments via Telethon client."""

    def __init__(self, db: AsyncSession, account_pool: AccountPool):
        self.db = db
        self.account_pool = account_pool
        self.logger = logger.bind(module="punishment_manager")

    async def warn(self, user_id: int, group_id: int, reason: str) -> PunishmentResult:
        return PunishmentResult(True, PunishmentType.WARN, user_id, group_id, reason=reason)

    async def mute(self, user_id: int, group_id: int, duration_seconds: int, reason: str = "") -> PunishmentResult:
        account = await self.account_pool.acquire(purpose="mute")
        if account is None:
            return PunishmentResult(False, PunishmentType.MUTE, user_id, group_id, reason=reason, error="no_account")

        until_date = datetime.utcnow() + timedelta(seconds=duration_seconds)
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()

        try:
            if client is None:
                return PunishmentResult(False, PunishmentType.MUTE, user_id, group_id, reason=reason, until_date=until_date, error="no_client")
            if hasattr(client, "edit_permissions"):
                await client.edit_permissions(group_id, user_id, send_messages=False, until_date=until_date)
                return PunishmentResult(True, PunishmentType.MUTE, user_id, group_id, reason=reason, until_date=until_date)
            return PunishmentResult(False, PunishmentType.MUTE, user_id, group_id, reason=reason, until_date=until_date, error="unsupported")
        finally:
            await self.account_pool.release(account)

    async def ban(self, user_id: int, group_id: int, reason: str = "") -> PunishmentResult:
        account = await self.account_pool.acquire(purpose="ban")
        if account is None:
            return PunishmentResult(False, PunishmentType.BAN, user_id, group_id, reason=reason, error="no_account")

        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()

        try:
            if client is None:
                return PunishmentResult(False, PunishmentType.BAN, user_id, group_id, reason=reason, error="no_client")
            if hasattr(client, "kick_participant"):
                await client.kick_participant(group_id, user_id)
                return PunishmentResult(True, PunishmentType.BAN, user_id, group_id, reason=reason)
            if hasattr(client, "edit_permissions"):
                await client.edit_permissions(group_id, user_id, view_messages=False)
                return PunishmentResult(True, PunishmentType.BAN, user_id, group_id, reason=reason)
            return PunishmentResult(False, PunishmentType.BAN, user_id, group_id, reason=reason, error="unsupported")
        finally:
            await self.account_pool.release(account)

    async def kick(self, user_id: int, group_id: int, reason: str = "") -> PunishmentResult:
        return await self.ban(user_id, group_id, reason=reason)
