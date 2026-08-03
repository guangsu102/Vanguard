"""Celery command execution for NapCatQQ OneBot group operations."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select

from app.celery import celery_app
from app.integrations.qq import OneBotAPIError, OneBotClient
from app.modules.qq.models import QQGroupCommand, QQGroupMessage
from app.modules.qq.service import QQ_GROUP_CHANNEL, QQEventProcessor

logger = structlog.get_logger()


async def _execute_qq_command(command_id: str) -> dict[str, Any]:
    from app.core import database as db_module
    from app.core import redis as redis_module

    await db_module.init_db(create_tables=False)
    await redis_module.init_redis()
    client = OneBotClient()
    try:
        async with db_module.get_db_session() as db:
            command = await db.get(QQGroupCommand, command_id)
            if command is None:
                return {"status": "skipped", "reason": "command_not_found"}
            if command.status not in {"pending", "queued"}:
                return {"status": command.status, "reason": "command_not_pending"}

            command.status = "sending"
            command.started_at = datetime.utcnow()
            await db.commit()
            payload = json.loads(command.payload_json or "{}")
            try:
                if command.command_type == "notification":
                    result = await client.send_group_message(
                        command.group.group_openid,
                        str(payload.get("content") or ""),
                    )
                    command.provider_message_id = str(result.get("message_id") or "") or None
                elif command.command_type == "recall":
                    message_id = str(payload.get("message_id") or "")
                    await client.recall_group_message(command.group.group_openid, message_id)
                    message_result = await db.execute(
                        select(QQGroupMessage).where(
                            QQGroupMessage.group_id == command.group_id,
                            QQGroupMessage.provider_message_id == message_id,
                        )
                    )
                    message = message_result.scalar_one_or_none()
                    if message is not None:
                        message.recalled_at = datetime.utcnow()
                        message.moderation_status = "recalled"
                else:
                    raise OneBotAPIError(f"Unsupported QQ command: {command.command_type}")
                command.status = "succeeded"
                command.completed_at = datetime.utcnow()
                command.error_message = None
            except OneBotAPIError as exc:
                command.status = "unknown" if exc.uncertain else "failed"
                command.completed_at = datetime.utcnow()
                command.error_message = exc.message[:2000]
            except Exception as exc:
                command.status = "failed"
                command.completed_at = datetime.utcnow()
                command.error_message = str(exc)[:2000]

            await db.commit()
            processor = QQEventProcessor(db, redis_module.redis_client)
            await processor.publish(
                QQ_GROUP_CHANNEL,
                "qq:command",
                {
                    "id": command.id,
                    "group_id": command.group_id,
                    "command_type": command.command_type,
                    "status": command.status,
                    "provider_message_id": command.provider_message_id,
                    "error_message": command.error_message,
                },
            )
            return {"status": command.status, "command_id": command.id}
    finally:
        await client.close()
        await redis_module.close_redis()
        await db_module.close_db()


@celery_app.task(name="app.modules.qq.tasks.execute_qq_command")
def execute_qq_command(command_id: str) -> dict[str, Any]:
    logger.info("execute_qq_command", command_id=command_id)
    return asyncio.run(_execute_qq_command(command_id))


async def _cleanup_qq_messages() -> dict[str, Any]:
    from app.core import database as db_module
    from app.core.config import settings

    await db_module.init_db(create_tables=False)
    cutoff = datetime.utcnow() - timedelta(days=settings.QQ_ONEBOT_MESSAGE_RETENTION_DAYS)
    try:
        async with db_module.get_db_session() as db:
            result = await db.execute(delete(QQGroupMessage).where(QQGroupMessage.created_at < cutoff))
            return {
                "status": "completed",
                "deleted": result.rowcount or 0,
                "cutoff": cutoff.isoformat(),
            }
    finally:
        await db_module.close_db()


@celery_app.task(name="app.modules.qq.tasks.cleanup_qq_messages")
def cleanup_qq_messages() -> dict[str, Any]:
    return asyncio.run(_cleanup_qq_messages())
