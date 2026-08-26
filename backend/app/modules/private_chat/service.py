"""Persistence and real-time events for the Telegram private chat inbox."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.automation_settings import is_private_messaging_enabled
from app.modules.private_chat.models import PrivateChatConversation, PrivateChatMessage

PRIVATE_CHAT_WS_REDIS_CHANNEL = "vanguard:ws:telegram-private-chat"
PRIVATE_CHAT_WS_CHANNEL = "telegram:private-chats"

logger = structlog.get_logger()


@dataclass(slots=True)
class IncomingPrivateMessage:
    account_id: int
    peer_telegram_id: int
    telegram_message_id: int
    content: str | None
    occurred_at: datetime
    peer_username: str | None = None
    peer_display_name: str | None = None
    message_type: str = "text"
    media: dict[str, Any] | None = None
    reply_to_telegram_message_id: int | None = None


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _loads_dict(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def serialize_private_message(message: PrivateChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "account_id": message.account_id,
        "peer_telegram_id": message.peer_telegram_id,
        "telegram_message_id": message.telegram_message_id,
        "reply_to_telegram_message_id": message.reply_to_telegram_message_id,
        "direction": message.direction,
        "source": message.source,
        "message_type": message.message_type,
        "content": message.content,
        "media": _loads_dict(message.media_json),
        "status": message.status,
        "operator_id": message.operator_id,
        "client_request_id": message.client_request_id,
        "attempt_count": message.attempt_count,
        "error_message": message.error_message,
        "occurred_at": message.occurred_at.isoformat(),
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "created_at": message.created_at.isoformat(),
        "updated_at": message.updated_at.isoformat(),
    }


def serialize_conversation(conversation: PrivateChatConversation) -> dict[str, Any]:
    account = conversation.__dict__.get("account")
    return {
        "id": conversation.id,
        "account_id": conversation.account_id,
        "account_name": (
            getattr(account, "display_name", None)
            or getattr(account, "identifier", None)
            or str(conversation.account_id)
        ),
        "account_identifier": getattr(account, "identifier", None),
        "account_status": _enum_value(getattr(account, "status", None)),
        "peer_telegram_id": conversation.peer_telegram_id,
        "peer_username": conversation.peer_username,
        "peer_display_name": conversation.peer_display_name,
        "status": conversation.status,
        "handling_mode": conversation.handling_mode,
        "assigned_admin_id": conversation.assigned_admin_id,
        "unread_count": conversation.unread_count,
        "last_message_preview": conversation.last_message_preview,
        "last_message_direction": conversation.last_message_direction,
        "last_message_at": (
            conversation.last_message_at.isoformat()
            if conversation.last_message_at
            else None
        ),
        "last_inbound_at": (
            conversation.last_inbound_at.isoformat()
            if conversation.last_inbound_at
            else None
        ),
        "last_outbound_at": (
            conversation.last_outbound_at.isoformat()
            if conversation.last_outbound_at
            else None
        ),
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


async def _get_or_create_conversation(
    db: AsyncSession,
    incoming: IncomingPrivateMessage,
) -> PrivateChatConversation:
    result = await db.execute(
        select(PrivateChatConversation).where(
            PrivateChatConversation.account_id == incoming.account_id,
            PrivateChatConversation.peer_telegram_id == incoming.peer_telegram_id,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        return conversation

    conversation = PrivateChatConversation(
        account_id=incoming.account_id,
        peer_telegram_id=incoming.peer_telegram_id,
        peer_username=incoming.peer_username,
        peer_display_name=incoming.peer_display_name,
    )
    try:
        async with db.begin_nested():
            db.add(conversation)
            await db.flush()
        return conversation
    except IntegrityError:
        result = await db.execute(
            select(PrivateChatConversation).where(
                PrivateChatConversation.account_id == incoming.account_id,
                PrivateChatConversation.peer_telegram_id == incoming.peer_telegram_id,
            )
        )
        return result.scalar_one()


async def persist_incoming_private_message(
    db: AsyncSession,
    incoming: IncomingPrivateMessage,
) -> tuple[PrivateChatConversation, PrivateChatMessage, bool]:
    """Persist an inbound message and return whether a new row was inserted."""
    existing = await db.execute(
        select(PrivateChatMessage).where(
            PrivateChatMessage.account_id == incoming.account_id,
            PrivateChatMessage.peer_telegram_id == incoming.peer_telegram_id,
            PrivateChatMessage.telegram_message_id == incoming.telegram_message_id,
        )
    )
    existing_message = existing.scalar_one_or_none()
    if existing_message is not None:
        conversation = await db.get(
            PrivateChatConversation, existing_message.conversation_id
        )
        if conversation is None:
            raise RuntimeError("Private chat conversation is missing")
        return conversation, existing_message, False

    conversation = await _get_or_create_conversation(db, incoming)
    media_json = (
        json.dumps(incoming.media, ensure_ascii=False, separators=(",", ":"))
        if incoming.media
        else None
    )
    message = PrivateChatMessage(
        conversation_id=conversation.id,
        account_id=incoming.account_id,
        peer_telegram_id=incoming.peer_telegram_id,
        telegram_message_id=incoming.telegram_message_id,
        reply_to_telegram_message_id=incoming.reply_to_telegram_message_id,
        direction="inbound",
        source="user",
        message_type=incoming.message_type,
        content=incoming.content,
        media_json=media_json,
        status="received",
        occurred_at=incoming.occurred_at,
    )
    try:
        async with db.begin_nested():
            db.add(message)
            await db.flush()
    except IntegrityError:
        existing = await db.execute(
            select(PrivateChatMessage).where(
                PrivateChatMessage.account_id == incoming.account_id,
                PrivateChatMessage.peer_telegram_id == incoming.peer_telegram_id,
                PrivateChatMessage.telegram_message_id == incoming.telegram_message_id,
            )
        )
        existing_message = existing.scalar_one()
        return conversation, existing_message, False

    preview = (incoming.content or "").strip()
    if not preview:
        preview = f"[{incoming.message_type}]"
    now = datetime.utcnow()
    values: dict[str, Any] = {
        "unread_count": PrivateChatConversation.unread_count + 1,
        "status": "open",
        "last_message_preview": preview[:255],
        "last_message_direction": "inbound",
        "last_message_at": incoming.occurred_at,
        "last_inbound_at": incoming.occurred_at,
        "updated_at": now,
    }
    if incoming.peer_username:
        values["peer_username"] = incoming.peer_username
    if incoming.peer_display_name:
        values["peer_display_name"] = incoming.peer_display_name

    await db.execute(
        update(PrivateChatConversation)
        .where(PrivateChatConversation.id == conversation.id)
        .values(**values)
    )
    await db.flush()
    await db.refresh(conversation)
    return conversation, message, True


async def queue_outbound_private_message(
    db: AsyncSession,
    conversation: PrivateChatConversation,
    *,
    content: str,
    operator_id: int,
    client_request_id: str,
) -> tuple[PrivateChatConversation, PrivateChatMessage, bool]:
    """Create one idempotent manual-reply outbox row."""
    existing = await db.execute(
        select(PrivateChatMessage).where(
            PrivateChatMessage.client_request_id == client_request_id
        )
    )
    existing_message = existing.scalar_one_or_none()
    if existing_message is not None:
        if (
            existing_message.conversation_id != conversation.id
            or existing_message.content != content
        ):
            raise ValueError("client_request_id is already used")
        return conversation, existing_message, False

    now = datetime.utcnow()
    message = PrivateChatMessage(
        conversation_id=conversation.id,
        account_id=conversation.account_id,
        peer_telegram_id=conversation.peer_telegram_id,
        direction="outbound",
        source="operator",
        message_type="text",
        content=content,
        status="pending",
        operator_id=operator_id,
        client_request_id=client_request_id,
        occurred_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(message)
            await db.flush()
    except IntegrityError:
        existing = await db.execute(
            select(PrivateChatMessage).where(
                PrivateChatMessage.client_request_id == client_request_id
            )
        )
        existing_message = existing.scalar_one()
        if (
            existing_message.conversation_id != conversation.id
            or existing_message.content != content
        ):
            raise ValueError("client_request_id is already used") from None
        return conversation, existing_message, False

    conversation.handling_mode = "human"
    conversation.assigned_admin_id = operator_id
    conversation.last_message_preview = content[:255]
    conversation.last_message_direction = "outbound"
    conversation.last_message_at = now
    conversation.updated_at = now
    await db.flush()
    await db.refresh(conversation)
    return conversation, message, True


async def claim_pending_outbound_message(
    db: AsyncSession,
) -> PrivateChatMessage | None:
    """Claim one pending outbox row for a Telegram growth worker."""
    result = await db.execute(
        select(PrivateChatMessage)
        .where(
            PrivateChatMessage.direction == "outbound",
            PrivateChatMessage.status == "pending",
        )
        .order_by(PrivateChatMessage.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    message = result.scalar_one_or_none()
    if message is None:
        return None
    message.status = "sending"
    message.attempt_count += 1
    message.error_message = None
    message.next_attempt_at = None
    message.updated_at = datetime.utcnow()
    await db.flush()
    await db.refresh(message)
    return message


async def finalize_outbound_private_message(
    db: AsyncSession,
    message_id: int,
    *,
    status: str,
    telegram_message_id: int | None = None,
    error_message: str | None = None,
) -> tuple[PrivateChatConversation, PrivateChatMessage]:
    if status not in {"sent", "failed", "unknown"}:
        raise ValueError(f"Unsupported outbound message status: {status}")
    message = await db.get(PrivateChatMessage, message_id)
    if message is None or message.direction != "outbound":
        raise RuntimeError("Private chat outbound message is missing")
    conversation = await db.get(PrivateChatConversation, message.conversation_id)
    if conversation is None:
        raise RuntimeError("Private chat conversation is missing")

    now = datetime.utcnow()
    message.status = status
    message.telegram_message_id = telegram_message_id
    message.error_message = (error_message or "")[:2000] or None
    message.updated_at = now
    if status == "sent":
        message.sent_at = now
        conversation.last_outbound_at = now
    conversation.updated_at = now
    await db.flush()
    await db.refresh(message)
    await db.refresh(conversation)
    return conversation, message


async def is_conversation_auto_reply_enabled(
    db: AsyncSession,
    conversation: PrivateChatConversation,
) -> bool:
    if conversation.status != "open" or conversation.handling_mode != "auto":
        return False
    return await is_private_messaging_enabled(db, initiated_by_user=True)


async def publish_private_chat_event(
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Publish after the database transaction commits."""
    from app.core.redis import get_redis

    try:
        redis = await get_redis()
        await redis.publish(
            PRIVATE_CHAT_WS_REDIS_CHANNEL,
            json.dumps(
                {
                    "channel": PRIVATE_CHAT_WS_CHANNEL,
                    "message": {"type": event_type, "data": data},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    except Exception as exc:
        logger.warning(
            "private_chat_realtime_publish_failed",
            event_type=event_type,
            error=str(exc),
        )
