"""Only trim closed local facts behind a verified listener checkpoint."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import delete, select, exists
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_models import SystemSetting
from app.core.worker_status import TelegramEventInbox as Inbox

WATERMARK = "growth.event_retention.watermark."


async def clean_completed(db: AsyncSession, limit: int = 1000) -> int:
    marks = list((await db.scalars(select(SystemSetting).where(SystemSetting.key.like(WATERMARK + "%")))).all())
    removed = 0
    cutoff = datetime.utcnow() - timedelta(days=7)
    other = aliased(Inbox)
    for mark in marks:
        value = json.loads(mark.value)
        if not value.get("protocol_checkpoint_safe"):
            continue
        before = min(cutoff, datetime.fromisoformat(value["before"]))
        ids = list((await db.scalars(select(Inbox.id).where(
            Inbox.account_id == int(mark.key.removeprefix(WATERMARK)), Inbox.kind == "listener_fact",
            Inbox.state == "completed", Inbox.external_attempted.is_(False),
            Inbox.completed_at < before, Inbox.received_at < before,
            ~exists().where(other.account_id == Inbox.account_id, other.chat_id == Inbox.chat_id,
                            other.state.in_(["pending", "processing", "executing", "reconciliation_required"])),
        ).order_by(Inbox.completed_at, Inbox.id).limit(limit - removed).with_for_update(skip_locked=True))).all())
        if ids:
            await db.execute(delete(Inbox).where(Inbox.id.in_(ids)))
            removed += len(ids)
        if removed >= limit:
            break
    return removed
