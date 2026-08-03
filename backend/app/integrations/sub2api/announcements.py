"""Signed Sub2API announcement intake and configured public-channel delivery."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.automation_settings import get_app_runtime_settings
from app.core.config import settings
from app.integrations.qq.client import OneBotClient
from app.integrations.sub2api.alerts import (
    Sub2APIAlertSource,
    _acquire_delivery,
    _mark_delivery_sent,
    _release_delivery,
    parse_telegram_chat_ids,
)
from app.integrations.telegram.client import get_telegram_client
from app.modules.qq.models import QQBotConnection, QQManagedGroup


class Sub2APIAnnouncementEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    revision: int = Field(gt=0)
    published_at: datetime


class Sub2APIAnnouncementContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=60000)
    audience: Literal["public"]
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class Sub2APIAnnouncementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^1$")
    type: Literal["announcement.published"]
    source: Sub2APIAlertSource
    event: Sub2APIAnnouncementEvent
    announcement: Sub2APIAnnouncementContent


class Sub2APIAnnouncementDeliveryError(RuntimeError):
    """Raised when a selected public channel cannot accept an announcement."""


def expected_sub2api_announcement_idempotency_key(
    payload: Sub2APIAnnouncementPayload,
) -> str:
    return (
        f"{payload.source.instance_id.strip()}:announcement:"
        f"{payload.event.id}:published:{payload.event.revision}"
    )


def format_sub2api_announcement_message(
    payload: Sub2APIAnnouncementPayload,
    *,
    limit: int = 3500,
) -> str:
    title = payload.announcement.title.strip()
    content = payload.announcement.content.strip()
    lines = [f"[公告] {title}", "", content]
    if payload.source.base_url:
        lines.extend(["", f"站内查看：{payload.source.base_url.rstrip('/')}"])
    message = "\n".join(lines)
    if len(message) <= limit:
        return message
    suffix = "\n\n内容较长，请前往站内查看"
    return message[: max(0, limit - len(suffix))].rstrip() + suffix


async def deliver_sub2api_announcement(
    payload: Sub2APIAnnouncementPayload,
    idempotency_key: str,
    db: AsyncSession,
    redis: Redis,
) -> dict[str, int]:
    raw_settings = await get_app_runtime_settings(db)
    notification = raw_settings.get("notification")
    notification = notification if isinstance(notification, dict) else {}

    if not bool(notification.get("sub2apiAnnouncementsEnabled", False)):
        return {"sent": 0, "duplicate": 0, "skipped": 1}

    sent = 0
    duplicate = 0
    skipped = 0
    errors: list[str] = []

    if bool(notification.get("telegramAnnouncementsEnabled", False)):
        chat_ids = parse_telegram_chat_ids(notification.get("telegramAnnouncementChatId"))
        if not chat_ids:
            skipped += 1
        elif not settings.BOT_TOKEN:
            errors.append("Telegram Bot Token is not configured")
        else:
            telegram = get_telegram_client()
            should_pin = bool(notification.get("telegramAnnouncementPin", True))
            silent_pin = bool(notification.get("telegramAnnouncementPinSilent", True))
            message = format_sub2api_announcement_message(payload, limit=3500)
            for chat_id in chat_ids:
                state, message_id = await _acquire_telegram_announcement_delivery(
                    redis, idempotency_key, chat_id
                )
                if state == "sent":
                    duplicate += 1
                    continue
                if state == "processing":
                    errors.append(f"Telegram {chat_id} announcement delivery is already processing")
                    continue
                try:
                    if state == "acquired":
                        result = await telegram.send_message(
                            chat_id,
                            message,
                            parse_mode=None,
                            disable_web_page_preview=True,
                        )
                        message_id = int(getattr(result, "message_id", 0) or 0)
                        if should_pin and message_id <= 0:
                            raise RuntimeError("Telegram send response did not include message_id")
                        await _mark_telegram_announcement_message_sent(
                            redis, idempotency_key, chat_id, message_id
                        )
                    if should_pin:
                        await telegram.pin_chat_message(
                            chat_id,
                            message_id,
                            disable_notification=silent_pin,
                        )
                    await _mark_telegram_announcement_delivery_sent(redis, idempotency_key, chat_id)
                    sent += 1
                except Exception as exc:
                    if message_id <= 0:
                        await _release_telegram_announcement_delivery(
                            redis, idempotency_key, chat_id
                        )
                    errors.append(f"Telegram {chat_id}: {exc}")

    if bool(notification.get("qqAnnouncementsEnabled", False)):
        account_id = (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
        groups_result = await db.execute(
            select(QQManagedGroup).join(QQBotConnection).where(
                QQBotConnection.app_id == account_id,
                QQManagedGroup.status == "active",
                QQManagedGroup.notifications_enabled.is_(True),
            )
        )
        groups = list(groups_result.scalars().all())
        if not groups:
            skipped += 1
        elif not settings.QQ_ONEBOT_ENABLED:
            errors.append("NapCat OneBot is not enabled")
        else:
            qq = OneBotClient()
            message = format_sub2api_announcement_message(payload, limit=1800)
            try:
                for group in groups:
                    target = group.group_openid
                    state = await _acquire_delivery(
                        redis, idempotency_key, "qq-announcement", target
                    )
                    if state == "sent":
                        duplicate += 1
                        continue
                    if state == "processing":
                        errors.append(
                            f"QQ group {target} announcement delivery is already processing"
                        )
                        continue
                    try:
                        await qq.send_group_message(target, message)
                        await _mark_delivery_sent(redis, idempotency_key, "qq-announcement", target)
                        sent += 1
                    except Exception as exc:
                        await _release_delivery(redis, idempotency_key, "qq-announcement", target)
                        errors.append(f"QQ group {target}: {exc}")
            finally:
                await qq.close()

    if errors:
        raise Sub2APIAnnouncementDeliveryError("; ".join(errors)[:2000])
    return {"sent": sent, "duplicate": duplicate, "skipped": skipped}


def _telegram_announcement_delivery_key(idempotency_key: str, chat_id: str) -> str:
    digest = hashlib.sha256(
        f"{idempotency_key}:telegram-announcement:{chat_id}".encode()
    ).hexdigest()
    return f"sub2api:announcement:delivery:{digest}"


def _decode_redis_value(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


async def _acquire_telegram_announcement_delivery(
    redis: Redis,
    idempotency_key: str,
    chat_id: str,
) -> tuple[str, int]:
    key = _telegram_announcement_delivery_key(idempotency_key, chat_id)
    existing = _decode_redis_value(await redis.get(key))
    if existing == "sent":
        return "sent", 0
    if existing.startswith("message:"):
        try:
            return "message_sent", int(existing.split(":", 1)[1])
        except ValueError:
            await redis.delete(key)
    acquired = await redis.set(key, "processing", nx=True, ex=300)
    if acquired:
        return "acquired", 0
    existing = _decode_redis_value(await redis.get(key))
    if existing == "sent":
        return "sent", 0
    if existing.startswith("message:"):
        try:
            return "message_sent", int(existing.split(":", 1)[1])
        except ValueError:
            return "processing", 0
    return "processing", 0


async def _mark_telegram_announcement_message_sent(
    redis: Redis,
    idempotency_key: str,
    chat_id: str,
    message_id: int,
) -> None:
    key = _telegram_announcement_delivery_key(idempotency_key, chat_id)
    await redis.set(
        key,
        f"message:{message_id}",
        ex=settings.SUB2API_ALERT_IDEMPOTENCY_TTL_SECONDS,
    )


async def _mark_telegram_announcement_delivery_sent(
    redis: Redis,
    idempotency_key: str,
    chat_id: str,
) -> None:
    key = _telegram_announcement_delivery_key(idempotency_key, chat_id)
    await redis.set(
        key,
        "sent",
        ex=settings.SUB2API_ALERT_IDEMPOTENCY_TTL_SECONDS,
    )


async def _release_telegram_announcement_delivery(
    redis: Redis,
    idempotency_key: str,
    chat_id: str,
) -> None:
    await redis.delete(_telegram_announcement_delivery_key(idempotency_key, chat_id))
