"""Durable handoff after Telethon has ordered updates; unknown writes never replay."""
from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, func, exists
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db_session
from app.core.worker_status import TelegramEventInbox

current_event_id: ContextVar[int | None] = ContextVar("telegram_inbox_event", default=None)


def event_payload(kind: str, event: Any) -> tuple[str, dict]:
    message = getattr(event, "message", None) or getattr(event, "action_message", None)
    message_id = getattr(event, "id", None) or getattr(message, "id", None)
    sender = getattr(event, "sender", None) or getattr(message, "sender", None)
    added = getattr(event, "added_by", None)
    data = {
        "kind": kind, "chat_id": getattr(event, "chat_id", None),
        "id": message_id, "sender_id": getattr(event, "sender_id", None),
        "raw_text": getattr(event, "raw_text", None) or getattr(event, "text", None) or "",
        "is_private": bool(getattr(event, "is_private", False)),
        "out": bool(getattr(event, "out", False)),
        "sender": {name: getattr(sender, name, None) for name in ("id", "username", "first_name", "last_name", "bot")},
        "date": getattr(message, "date", None),
        "reply_to_msg_id": getattr(message, "reply_to_msg_id", None),
        "user_ids": list(getattr(event, "user_ids", None) or []),
        "user_joined": bool(getattr(event, "user_joined", False)),
        "user_added": bool(getattr(event, "user_added", False)),
        "added_by": added if isinstance(added, int) else getattr(added, "id", None),
        "deleted_ids": list(getattr(event, "deleted_ids", None) or []),
    }
    from app.workers.telegram_worker import TelegramWorker
    data["media_type"], data["media_metadata"] = TelegramWorker._private_message_media(message)
    if isinstance(data["date"], datetime):
        data["date"] = data["date"].isoformat()
    if kind == "message":
        if message_id is None or data["chat_id"] is None:
            raise ValueError("inbox_message_identity_missing")
        identity = [kind, data["chat_id"], message_id]
    elif message_id is not None:
        identity = [kind, data["chat_id"], message_id]
    else:
        update = getattr(event, "original_update", None)
        if update is None:
            raise ValueError("inbox_update_identity_missing")
        identity = [kind, hashlib.sha256(bytes(update)).hexdigest()]
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return key, data


def restore_event(data: dict, client: Any, *, retry: bool = False) -> Any:
    sender = SimpleNamespace(**data["sender"])
    message = SimpleNamespace(
        id=data["id"], date=datetime.fromisoformat(data["date"]) if data.get("date") else None,
        reply_to_msg_id=data.get("reply_to_msg_id"), sender=sender,
        inbox_media_type=data.get("media_type", "text"),
        inbox_media_metadata=data.get("media_metadata"),
    )
    values = {k: v for k, v in data.items() if k not in {"sender", "date", "kind", "media_type", "media_metadata"}}
    event = SimpleNamespace(**values, sender=sender, message=message, client=client,
                            _vanguard_durable_event=True, _vanguard_retry=retry)

    async def get_sender():
        return sender

    event.get_sender = get_sender
    return event


async def enqueue(db: Any, account_id: int, kind: str, event: Any) -> bool:
    key, data = event_payload(kind, event)
    try:
        async with db.begin_nested():
            db.add(TelegramEventInbox(
                account_id=account_id, event_key=key, kind=kind,
                chat_id=data.get("chat_id"), payload_json=json.dumps(data, ensure_ascii=False),
                state="pending", next_attempt_at=datetime.utcnow(),
            ))
            await db.flush()
        return True
    except IntegrityError:
        existing = await db.scalar(select(TelegramEventInbox.id).where(
            TelegramEventInbox.account_id == account_id, TelegramEventInbox.event_key == key,
        ))
        if existing is None:
            raise
        return False


async def claim(db: Any, account_ids: list[int]) -> Any:
    if not account_ids:
        return None
    older = aliased(TelegramEventInbox)
    blocked_peer = exists(select(older.id).where(
        older.account_id == TelegramEventInbox.account_id,
        older.chat_id.is_not_distinct_from(TelegramEventInbox.chat_id),
        older.id < TelegramEventInbox.id,
        older.state.in_(["pending", "processing", "executing", "reconciliation_required"]),
    ))
    row = await db.scalar(select(TelegramEventInbox).where(
        ~blocked_peer, TelegramEventInbox.account_id.in_(account_ids),
        TelegramEventInbox.state == "pending", TelegramEventInbox.next_attempt_at <= datetime.utcnow(),
    ).order_by(TelegramEventInbox.id).with_for_update(skip_locked=True).limit(1))
    if row is not None:
        row.state, row.started_at = "processing", datetime.utcnow()
        row.attempts += 1
        await db.flush()
    return row


async def mark_external_attempt() -> None:
    """Commit before any external write; a lost response is never auto-replayed."""
    event_id = current_event_id.get()
    if event_id is None:
        return
    async with get_db_session() as db:
        row = await db.get(TelegramEventInbox, event_id, with_for_update=True)
        if row is None or row.state not in {"processing", "executing"}:
            raise RuntimeError("inbox_execution_claim_lost")
        row.state = "executing"
        row.external_attempted = True


async def finish(db: Any, event_id: int, error: Exception | None = None) -> None:
    from app.core.account.rpc_governor import RpcDeferred
    row = await db.get(TelegramEventInbox, event_id, with_for_update=True)
    if row is None:
        return
    now = datetime.utcnow()
    if error is None:
        row.state, row.completed_at, row.payload_json = "completed", now, "{}"
        row.last_error = None
    elif isinstance(error, RpcDeferred) and not row.external_attempted:
        row.state = "pending"
        row.next_attempt_at = now + timedelta(seconds=error.retry_after_seconds + 1)
        row.last_error = error.reason
    else:
        row.state = "reconciliation_required"
        row.last_error = type(error).__name__


async def recover_interrupted(db: Any) -> int:
    # Pending events are safe to resume. A process may have died after an
    # external provider accepted an action; executing/processing are not replayed.
    from sqlalchemy import update
    result = await db.execute(update(TelegramEventInbox).where(
        TelegramEventInbox.state.in_(["processing", "executing"]),
    ).values(state="reconciliation_required", last_error="worker_interrupted"))
    return result.rowcount


async def inbox_snapshot(db: Any) -> dict:
    rows = (await db.execute(select(TelegramEventInbox.state, func.count()).group_by(TelegramEventInbox.state))).all()
    oldest = await db.scalar(select(func.min(TelegramEventInbox.received_at)).where(TelegramEventInbox.state == "pending"))
    return {"states": dict(rows), "oldest_pending_seconds": max(0, int((datetime.utcnow()-oldest).total_seconds())) if oldest else 0}
