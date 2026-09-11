"""Exclusive inbound-event ownership for stage-two owned groups."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import GuardianBotProfile
from app.core.automation_settings import get_owned_group_messaging_settings
from app.core.config import settings
from app.core.ephemeral_secret import EphemeralSecretService, get_ephemeral_secret_service
from app.core.group.models import Group
from app.core.redis import RedisCache
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_prompt_context import (
    EphemeralContextPayloadError,
    decrypt_ephemeral_context,
    encrypt_ephemeral_context,
)
from app.modules.owned_group.messaging_trigger_service import (
    IncomingOwnedGroupMessage,
    OwnedGroupMessageTriggerService,
)
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import (
    OwnedBotProfile,
    OwnedGroupMembership,
)
from app.modules.owned_group.security import safe_exception_message

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class OwnedGroupEventRouteResult:
    owned_group_event: bool
    asset_id: int | None = None
    execution_id: int | None = None
    created_status: str | None = None
    ignored_reason: str | None = None


class OwnedGroupMessagingEventRouter:
    """Claim owned chats before legacy acquisition handlers can observe them."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        cache: RedisCache | None = None,
        trigger_service: OwnedGroupMessageTriggerService | None = None,
        context_secret_service: EphemeralSecretService | None = None,
    ) -> None:
        self.db = db
        self.cache = cache or RedisCache()
        self.trigger_service = trigger_service or OwnedGroupMessageTriggerService(db)
        self.context_secret_service = (
            context_secret_service or get_ephemeral_secret_service()
        )
        self.logger = logger.bind(module="owned_group_message_event_router")

    async def find_owned_asset(
        self,
        telegram_chat_id: int,
    ) -> tuple[OwnedGroupAsset | None, bool]:
        assets = list(
            (
                await self.db.scalars(
                    select(OwnedGroupAsset)
                    .outerjoin(
                        Group,
                        OwnedGroupAsset.core_group_id == Group.id,
                    )
                    .where(
                        OwnedGroupAsset.archived_at.is_(None),
                        or_(
                            OwnedGroupAsset.telegram_chat_id == int(telegram_chat_id),
                            Group.group_id == int(telegram_chat_id),
                        ),
                    )
                    .order_by(OwnedGroupAsset.id)
                )
            ).all()
        )
        return (assets[0] if assets else None, len(assets) > 1)

    async def _managed_sender_ids(self, asset_id: int) -> set[int]:
        member_ids = list(
            (
                await self.db.scalars(
                    select(OwnedGroupMembership.telegram_user_id).where(
                        OwnedGroupMembership.group_asset_id == int(asset_id),
                        OwnedGroupMembership.telegram_user_id.is_not(None),
                    )
                )
            ).all()
        )
        guardian_ids = list(
            (
                await self.db.scalars(
                    select(GuardianBotProfile.bot_user_id).where(
                        GuardianBotProfile.bot_user_id.is_not(None)
                    )
                )
            ).all()
        )
        owned_bot_ids = list(
            (
                await self.db.scalars(
                    select(OwnedBotProfile.bot_user_id).where(
                        OwnedBotProfile.bot_user_id.is_not(None)
                    )
                )
            ).all()
        )
        return {
            int(value)
            for value in [*member_ids, *guardian_ids, *owned_bot_ids]
            if value is not None
        }

    async def _has_unbound_managed_sender_identity(self, asset_id: int) -> bool:
        """Fail closed while any active managed user lacks a trusted Telegram ID."""

        membership_id = await self.db.scalar(
            select(OwnedGroupMembership.id)
            .where(
                OwnedGroupMembership.group_asset_id == int(asset_id),
                OwnedGroupMembership.resource_type == "user",
                OwnedGroupMembership.status.in_(
                    {
                        "member_verified",
                        "admin_verified",
                        "skipped_already_member",
                        "unknown_needs_reconcile",
                    }
                ),
                OwnedGroupMembership.telegram_user_id.is_(None),
            )
            .limit(1)
        )
        return membership_id is not None

    async def _load_context(self, asset_id: int) -> list[Mapping[str, Any]]:
        key = f"owned_group:message:context:{int(asset_id)}"
        try:
            value = await self.cache.get_json(key)
        except Exception:
            return []
        if value is None:
            return []
        try:
            decrypted = decrypt_ephemeral_context(
                value,
                secret_service=self.context_secret_service,
            )
        except EphemeralContextPayloadError:
            try:
                await self.cache.delete(key)
            except Exception:
                pass
            return []
        return list(decrypted)[-100:] if isinstance(decrypted, list) else []

    async def _append_context(
        self,
        asset_id: int,
        *,
        source_message_id: int,
        sender_name: str,
        text: str,
        occurred_at: datetime,
        previous: Sequence[Mapping[str, Any]],
    ) -> None:
        rows = [
            *list(previous)[-99:],
            {
                "message_id": int(source_message_id),
                "user_name": str(sender_name)[:100],
                "text": str(text)[:1000],
                "occurred_at": occurred_at.isoformat(),
            },
        ]
        try:
            encrypted = encrypt_ephemeral_context(
                rows,
                secret_service=self.context_secret_service,
            )
            await self.cache.set_json(
                f"owned_group:message:context:{int(asset_id)}",
                encrypted,
                ttl=24 * 3600,
            )
        except Exception:
            return

    async def route_incoming(
        self,
        *,
        telegram_chat_id: int,
        source_message_id: int,
        sender_id: int,
        sender_name: str,
        text: str,
        occurred_at: datetime,
        listener_account_id: int | None = None,
        reply_to_message_id: int | None = None,
        reply_to_sender_id: int | None = None,
        mentioned_usernames: Sequence[str] = (),
        mentioned_user_ids: Sequence[int] = (),
        mention_resolver: Callable[[str], Awaitable[int | None]] | None = None,
        sender_is_bot: bool = False,
        outgoing: bool = False,
    ) -> OwnedGroupEventRouteResult:
        asset, duplicate_mapping = await self.find_owned_asset(telegram_chat_id)
        if asset is None:
            return OwnedGroupEventRouteResult(owned_group_event=False)

        # Ownership is decided solely by a non-archived asset mapping. Every
        # return below therefore consumes the event and forbids legacy fallback.
        if duplicate_mapping:
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="TARGET_MAPPING_INVALID",
            )
        if outgoing or sender_is_bot:
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="managed_or_system_sender",
            )
        if int(sender_id) in await self._managed_sender_ids(int(asset.id)):
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="managed_or_system_sender",
            )
        if await self._has_unbound_managed_sender_identity(int(asset.id)):
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="managed_sender_identity_unresolved",
            )
        if not str(text or "").strip() or int(source_message_id) <= 0:
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="empty_or_unidentified_message",
            )

        runtime = await get_owned_group_messaging_settings(self.db)
        if not bool(settings.OWNED_GROUP_MESSAGING_ENABLED) or not bool(
            runtime.get("enabled", False)
        ):
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="owned_group_messaging_disabled",
            )

        context = await self._load_context(int(asset.id))
        resolved_mention_ids = {int(value) for value in mentioned_user_ids if value is not None}
        if mention_resolver is not None:
            for username in mentioned_usernames:
                try:
                    resolved = await mention_resolver(str(username))
                except Exception as exc:
                    self.logger.info(
                        "owned_group_mention_resolution_failed_closed",
                        asset_id=int(asset.id),
                        username=str(username),
                        error_type=type(exc).__name__,
                    )
                    continue
                if resolved is not None:
                    resolved_mention_ids.add(int(resolved))
        try:
            execution = await self.trigger_service.handle_incoming_message(
                asset_id=int(asset.id),
                message=IncomingOwnedGroupMessage(
                    source_message_id=int(source_message_id),
                    telegram_chat_id=int(telegram_chat_id),
                    sender_id=int(sender_id),
                    sender_name=str(sender_name),
                    text=str(text),
                    occurred_at=occurred_at,
                    listener_account_id=listener_account_id,
                    reply_to_message_id=reply_to_message_id,
                    reply_to_sender_id=reply_to_sender_id,
                    mentioned_usernames=tuple(mentioned_usernames),
                    mentioned_user_ids=tuple(sorted(resolved_mention_ids)),
                    recent_context=tuple(context),
                ),
            )
        except OwnedGroupMessagingError as exc:
            await self.db.rollback()
            self.logger.info(
                "owned_group_inbound_skipped",
                asset_id=int(asset.id),
                telegram_chat_id=int(telegram_chat_id),
                source_message_id=int(source_message_id),
                reason_code=exc.code,
            )
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason=exc.code,
            )
        except Exception as exc:
            await self.db.rollback()
            self.logger.error(
                "owned_group_inbound_failed_closed",
                asset_id=int(asset.id),
                telegram_chat_id=int(telegram_chat_id),
                source_message_id=int(source_message_id),
                error_type=type(exc).__name__,
                error=safe_exception_message(exc, max_length=500),
            )
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="owned_group_router_error",
            )
        finally:
            await self._append_context(
                int(asset.id),
                source_message_id=int(source_message_id),
                sender_name=sender_name,
                text=text,
                occurred_at=occurred_at,
                previous=context,
            )

        if execution is None:
            return OwnedGroupEventRouteResult(
                owned_group_event=True,
                asset_id=int(asset.id),
                ignored_reason="no_matching_policy",
            )
        return OwnedGroupEventRouteResult(
            owned_group_event=True,
            asset_id=int(asset.id),
            execution_id=int(execution.id),
            created_status=str(execution.status),
        )


async def route_owned_group_message_event(
    db: AsyncSession,
    **kwargs: Any,
) -> OwnedGroupEventRouteResult:
    return await OwnedGroupMessagingEventRouter(db).route_incoming(**kwargs)


__all__ = [
    "OwnedGroupEventRouteResult",
    "OwnedGroupMessagingEventRouter",
    "route_owned_group_message_event",
]
