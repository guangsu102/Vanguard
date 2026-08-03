"""Normalize and persist NapCatQQ OneBot 11 events."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.qq.models import (
    QQBotConnection,
    QQGroupEvent,
    QQGroupMessage,
    QQManagedGroup,
)

QQ_WS_CHANNEL = "vanguard:ws:qq"
QQ_MESSAGE_CHANNEL = "qq:messages"
QQ_GROUP_CHANNEL = "qq:groups"


def parse_qq_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass
    return datetime.now(UTC).replace(tzinfo=None)


async def ensure_qq_connection(
    db: AsyncSession,
    account_id: str,
    *,
    display_name: str | None = None,
) -> QQBotConnection:
    result = await db.execute(
        select(QQBotConnection).where(QQBotConnection.app_id == account_id)
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        connection = QQBotConnection(
            app_id=account_id,
            bot_openid=account_id,
            display_name=display_name,
            enabled=True,
        )
        db.add(connection)
        await db.flush()
    elif display_name and connection.display_name != display_name:
        connection.display_name = display_name
    return connection


class QQEventProcessor:
    """Translate OneBot group messages and notices into local records."""

    def __init__(self, db: AsyncSession, redis_client: Redis | None = None) -> None:
        self.db = db
        self.redis = redis_client

    async def publish(self, channel: str, event_type: str, data: dict[str, Any]) -> None:
        if self.redis is None:
            return
        await self.redis.publish(
            QQ_WS_CHANNEL,
            json.dumps(
                {"channel": channel, "message": {"type": event_type, "data": data}},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    async def sync_groups(
        self,
        connection: QQBotConnection,
        onebot_groups: list[dict[str, Any]],
    ) -> list[QQManagedGroup]:
        synced: list[QQManagedGroup] = []
        for item in onebot_groups:
            group_number = str(item.get("group_id") or "").strip()
            if not group_number.isdigit():
                continue
            group_name = str(item.get("group_name") or "").strip() or None
            group = await self._get_or_create_group(
                connection,
                group_number,
                group_name=group_name,
            )
            if group.status == "removed":
                group.status = "active"
                group.bot_removed_at = None
            group.receive_all_messages_enabled = True
            group.proactive_messages_enabled = True
            synced.append(group)
        return synced

    async def handle_onebot_event(
        self,
        connection: QQBotConnection,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]] | None:
        post_type = str(payload.get("post_type") or "")
        if post_type == "message" and payload.get("message_type") == "group":
            serialized = await self._handle_group_message(connection, payload)
            if serialized is not None:
                return QQ_MESSAGE_CHANNEL, "qq:message", serialized
            return None
        if post_type == "notice" and payload.get("group_id") is not None:
            serialized = await self._handle_group_notice(connection, payload)
            if serialized is not None:
                return QQ_GROUP_CHANNEL, "qq:group-event", serialized
        return None

    async def _get_or_create_group(
        self,
        connection: QQBotConnection,
        group_number: str,
        *,
        group_name: str | None = None,
    ) -> QQManagedGroup:
        result = await self.db.execute(
            select(QQManagedGroup).where(
                QQManagedGroup.connection_id == connection.id,
                QQManagedGroup.group_openid == group_number,
            )
        )
        group = result.scalar_one_or_none()
        if group is None:
            group = QQManagedGroup(
                connection_id=connection.id,
                group_openid=group_number,
                local_name=group_name or f"QQ 群 {group_number}",
                status="active",
                receive_all_messages_enabled=True,
                proactive_messages_enabled=True,
                bot_added_at=datetime.utcnow(),
            )
            self.db.add(group)
            await self.db.flush()
        elif group_name and (
            not group.local_name or group.local_name == f"QQ 群 {group_number}"
        ):
            group.local_name = group_name
        return group

    async def _handle_group_message(
        self,
        connection: QQBotConnection,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        group_number = str(payload.get("group_id") or "").strip()
        message_id = str(payload.get("message_id") or "").strip()
        if not group_number.isdigit() or not message_id:
            return None

        group = await self._get_or_create_group(connection, group_number)
        occurred_at = parse_qq_timestamp(payload.get("time"))
        group.last_message_at = occurred_at
        group.receive_all_messages_enabled = True
        group.proactive_messages_enabled = True
        if group.status != "active" or not group.monitoring_enabled:
            return None

        existing = await self.db.execute(
            select(QQGroupMessage.id).where(
                QQGroupMessage.group_id == group.id,
                QQGroupMessage.provider_message_id == message_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return None

        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        segments = payload.get("message") if isinstance(payload.get("message"), list) else []
        attachments = self._message_attachments(segments)
        message = QQGroupMessage(
            group_id=group.id,
            provider_message_id=message_id,
            member_openid=str(payload.get("user_id") or sender.get("user_id") or "") or None,
            member_role=str(sender.get("role") or "") or None,
            content=self._message_text(payload, segments),
            attachments_json=(
                json.dumps(attachments, ensure_ascii=False, separators=(",", ":"))
                if attachments
                else None
            ),
            is_at_bot=self._is_at_account(segments, connection.app_id),
            occurred_at=occurred_at,
        )
        self.db.add(message)
        await self.db.flush()
        return self.serialize_message(message, group)

    async def _handle_group_notice(
        self,
        connection: QQBotConnection,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        group_number = str(payload.get("group_id") or "").strip()
        if not group_number.isdigit():
            return None
        notice_type = str(payload.get("notice_type") or "unknown")
        sub_type = str(payload.get("sub_type") or "")
        event_type = f"ONEBOT_{notice_type.upper()}"
        group = await self._get_or_create_group(connection, group_number)
        occurred_at = parse_qq_timestamp(payload.get("time"))
        target_id = str(payload.get("user_id") or "")

        if notice_type == "group_increase":
            event_type = "GROUP_ADD_ACCOUNT" if target_id == connection.app_id else "GROUP_MEMBER_ADD"
            if target_id == connection.app_id:
                group.status = "active"
                group.bot_added_at = occurred_at
                group.bot_removed_at = None
        elif notice_type == "group_decrease":
            event_type = (
                "GROUP_REMOVE_ACCOUNT" if target_id == connection.app_id else "GROUP_MEMBER_REMOVE"
            )
            if target_id == connection.app_id:
                group.status = "removed"
                group.bot_removed_at = occurred_at
        elif notice_type == "group_recall":
            event_type = "GROUP_MESSAGE_RECALL"
            recalled_id = str(payload.get("message_id") or "")
            message_result = await self.db.execute(
                select(QQGroupMessage).where(
                    QQGroupMessage.group_id == group.id,
                    QQGroupMessage.provider_message_id == recalled_id,
                )
            )
            message = message_result.scalar_one_or_none()
            if message is not None:
                message.recalled_at = occurred_at
                message.moderation_status = "recalled"

        event_id = self._onebot_event_id(payload)
        duplicate = await self.db.execute(
            select(QQGroupEvent.id).where(QQGroupEvent.event_id == event_id)
        )
        if duplicate.scalar_one_or_none() is not None:
            return None
        member_qq = target_id or str(payload.get("operator_id") or "") or None
        event = QQGroupEvent(
            group_id=group.id,
            event_id=event_id,
            event_type=event_type,
            member_openid=member_qq,
            payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            occurred_at=occurred_at,
        )
        self.db.add(event)
        await self.db.flush()
        return {
            "id": event.id,
            "group_id": group.id,
            "group_number": group.group_openid,
            "event_type": event_type,
            "sub_type": sub_type or None,
            "member_qq": member_qq,
            "occurred_at": occurred_at.isoformat(),
        }

    @staticmethod
    def _message_text(payload: dict[str, Any], segments: list[dict[str, Any]]) -> str | None:
        parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if segment_type == "text":
                parts.append(str(data.get("text") or ""))
            elif segment_type == "at":
                parts.append(f"@{data.get('qq') or ''}")
            elif segment_type not in {"reply", "image", "record", "video", "file"}:
                parts.append(f"[{segment_type}]")
        text = "".join(parts).strip()
        if text:
            return text
        raw = str(payload.get("raw_message") or "").strip()
        return raw or None

    @staticmethod
    def _message_attachments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "")
            if segment_type not in {"image", "record", "video", "file"}:
                continue
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            attachments.append(
                {
                    "content_type": f"onebot/{segment_type}",
                    "filename": str(data.get("file") or data.get("name") or "") or None,
                    "url": str(data.get("url") or "") or None,
                }
            )
        return attachments

    @staticmethod
    def _is_at_account(segments: list[dict[str, Any]], account_id: str) -> bool:
        return any(
            isinstance(segment, dict)
            and segment.get("type") == "at"
            and str((segment.get("data") or {}).get("qq") or "") == account_id
            for segment in segments
        )

    @staticmethod
    def _onebot_event_id(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"onebot:{hashlib.sha256(canonical.encode()).hexdigest()}"

    @staticmethod
    def serialize_message(message: QQGroupMessage, group: QQManagedGroup) -> dict[str, Any]:
        attachments: list[dict[str, Any]] = []
        if message.attachments_json:
            try:
                value = json.loads(message.attachments_json)
                attachments = value if isinstance(value, list) else []
            except (TypeError, json.JSONDecodeError):
                attachments = []
        return {
            "id": message.id,
            "group_id": group.id,
            "group_number": group.group_openid,
            "group_name": group.local_name,
            "provider_message_id": message.provider_message_id,
            "member_qq": message.member_openid,
            "member_role": message.member_role,
            "content": message.content,
            "attachments": attachments,
            "is_at_account": message.is_at_bot,
            "moderation_status": message.moderation_status,
            "occurred_at": message.occurred_at.isoformat(),
            "recalled_at": message.recalled_at.isoformat() if message.recalled_at else None,
        }
