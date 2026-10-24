"""Durable handoff after Telethon has ordered updates; unknown writes never replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.database import get_db_session
from app.core.worker_status import TelegramEventInbox

current_event_id: ContextVar[int | None] = ContextVar("telegram_inbox_event", default=None)
current_claim_owner: ContextVar[str | None] = ContextVar("telegram_inbox_claim_owner", default=None)
RECOVERABLE_KINDS = ("listener_fact", "deleted", "message", "join")
LOCAL_KINDS = ("listener_fact", "deleted")
BUSINESS_KINDS = ("message", "join")


async def rotation_cursor(account_ids: list[int], *, local_only: bool | None, fallback: int = 0) -> int:
    """Share the next account rotation across consumers; Redis loss loses no work."""
    if not settings.P1_RUNTIME_RELIABILITY_ENABLED or len(set(account_ids)) < 2:
        return fallback
    try:
        from app.core.redis import get_redis
        async with asyncio.timeout(1):
            cache = await get_redis()
            index = int(await cache.incr(f"vanguard:inbox:v1:cursor:{'local' if local_only else 'business'}"))
        accounts = sorted(set(account_ids))
        return accounts[(index - 2) % len(accounts)]
    except Exception:
        return fallback


async def observe_claim(row: Any) -> None:
    if settings.P1_RUNTIME_RELIABILITY_ENABLED:
        from app.core.metrics import inbox_baseline_at, record_inbox_observation
        wait = max(0, (datetime.utcnow() - row.received_at).total_seconds())
        baseline = inbox_baseline_at()
        historical = baseline is not None and row.received_at < baseline
        await record_inbox_observation(lane=lane_for(row.kind),
                                       claim_wait_seconds=None if historical else wait,
                                       starvation=int(not historical and wait > 120),
                                       event_key=f"inbox-claim:{row.id}:{row.attempts}")


def lane_for(kind: str) -> str:
    return "listener_fact" if kind in LOCAL_KINDS else "business"


def _claim_owner(owner: str | None = None) -> str:
    # The worker id may be reused on restart, so fence every individual claim.
    return f"{(owner or f'inbox:{os.getpid()}')[:80]}:{uuid.uuid4().hex}"


def _coalesce_key(account_id: int, kind: str, chat_id: int | None) -> str | None:
    """Bounded key for pending events that have no external side effect."""
    # Generic SDK messages/deletions can drive different effects and cannot
    # safely replace one another. Listener cues are coalesced by their typed
    # producer, which knows which evidence/message ids must be preserved.
    return None


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
        "mentioned": bool(getattr(message, "mentioned", False)),
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
        mentioned=data.get("mentioned", False),
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
            now = datetime.utcnow()
            ckey = _coalesce_key(account_id, kind, data.get("chat_id"))
            if ckey and settings.P1_RUNTIME_RELIABILITY_ENABLED:
                existing = await db.scalar(
                    select(TelegramEventInbox)
                    .where(
                        TelegramEventInbox.account_id == account_id,
                        TelegramEventInbox.coalesce_key == ckey,
                        TelegramEventInbox.state == "pending",
                        TelegramEventInbox.external_attempted.is_(False),
                        TelegramEventInbox.received_at >= now - timedelta(
                            seconds=settings.P1_INBOX_COALESCE_SECONDS
                        ),
                    )
                    .order_by(TelegramEventInbox.id.desc())
                    .with_for_update()
                )
                if existing is not None:
                    existing.coalesced_count = int(existing.coalesced_count or 0) + 1
                    payload = json.loads(existing.payload_json or "{}")
                    payload.setdefault("coalesced_event_ids", []).append(data.get("id"))
                    existing.payload_json = json.dumps(payload, ensure_ascii=False)
                    return True
            db.add(TelegramEventInbox(
                account_id=account_id, event_key=key, kind=kind,
                chat_id=data.get("chat_id"), payload_json=json.dumps(data, ensure_ascii=False),
                state="pending", next_attempt_at=now, available_at=now,
                lane=lane_for(kind), coalesce_key=ckey,
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


async def claim(db: Any, account_ids: list[int], *, local_account_ids: list[int] | None = None,
                after_account_id: int = 0, local_only: bool | None = None,
                owner: str | None = None, lease_seconds: int | None = None) -> Any:
    local_account_ids = local_account_ids or []
    owner = _claim_owner(owner)
    lease_seconds = lease_seconds or settings.P1_INBOX_CLAIM_LEASE_SECONDS
    if settings.P1_RUNTIME_RELIABILITY_ENABLED:
        await recover_expired_leases(db, limit=64)
    if not account_ids and not local_account_ids:
        return None
    older = aliased(TelegramEventInbox)
    blocked_peer = exists(select(older.id).where(
        older.account_id == TelegramEventInbox.account_id,
        older.chat_id.is_not_distinct_from(TelegramEventInbox.chat_id),
        older.id < TelegramEventInbox.id,
        older.state.in_(["pending", "processing", "executing", "reconciliation_required"]),
    ))
    # Local facts never wait for earlier chat actions. An OR predicate still
    # lets PostgreSQL execute the correlated history scan for every fact before
    # sorting, even when kind is already constrained to listener_fact.
    # Deletion observations only update durable delivery/survival state; they
    # never call Telegram. Let them drain while an earlier unknown outbound
    # action awaits reconciliation, just like listener facts.
    local_kinds = TelegramEventInbox.kind.in_(["listener_fact", "deleted"])
    ordered = (
        True
        if local_only is True
        else case((TelegramEventInbox.kind == "deleted", True), else_=~blocked_peer)
        if local_only is False
        else case((local_kinds, True), else_=~blocked_peer)
    )
    row = await db.scalar(select(TelegramEventInbox).where(
        ordered,
        or_(TelegramEventInbox.account_id.in_(account_ids),
            and_(TelegramEventInbox.kind.in_(["listener_fact", "deleted"]),
                 TelegramEventInbox.account_id.in_(local_account_ids))),
        TelegramEventInbox.state == "pending",
        TelegramEventInbox.next_attempt_at <= datetime.utcnow(),
        TelegramEventInbox.available_at <= datetime.utcnow() if settings.P1_RUNTIME_RELIABILITY_ENABLED else True,
        (True if local_only is None else
         TelegramEventInbox.kind.in_(list(LOCAL_KINDS)) if local_only else
         ~TelegramEventInbox.kind.in_(list(LOCAL_KINDS))),
    ).order_by(case((TelegramEventInbox.account_id > after_account_id, 0), else_=1),
               TelegramEventInbox.account_id, TelegramEventInbox.id)
        .with_for_update(skip_locked=True).limit(1))
    if row is not None:
        now = datetime.utcnow()
        row.state, row.started_at = "processing", now
        row.claim_owner = owner if settings.P1_RUNTIME_RELIABILITY_ENABLED else None
        row.claim_lease_expires_at = (
            now + timedelta(seconds=max(30, int(lease_seconds)))
            if settings.P1_RUNTIME_RELIABILITY_ENABLED else None
        )
        row.attempts += 1
        await db.flush()
    return row


async def claim_batch(
    db: Any,
    account_ids: list[int],
    *,
    local_account_ids: list[int] | None = None,
    after_account_id: int = 0,
    local_only: bool | None = None,
    limit: int = 32,
    owner: str | None = None,
    lease_seconds: int | None = None,
) -> list[Any]:
    """Claim a bounded batch while keeping the same lane and ordering rules.

    Listener facts have no external side effect and can be projected in one
    database session.  Claiming them together removes one transaction and one
    round trip per fact from the hot path.  Business events continue to use
    :func:`claim` because their handlers may perform an external Telegram
    operation and must retain the single-event reconciliation boundary.
    """
    local_account_ids = local_account_ids or []
    owner = _claim_owner(owner)
    lease_seconds = lease_seconds or settings.P1_INBOX_CLAIM_LEASE_SECONDS
    if settings.P1_RUNTIME_RELIABILITY_ENABLED:
        await recover_expired_leases(db, limit=max(64, limit))
    limit = max(1, min(int(limit), 128))
    if not account_ids and not local_account_ids:
        return []
    older = aliased(TelegramEventInbox)
    blocked_peer = exists(select(older.id).where(
        older.account_id == TelegramEventInbox.account_id,
        older.chat_id.is_not_distinct_from(TelegramEventInbox.chat_id),
        older.id < TelegramEventInbox.id,
        older.state.in_(["pending", "processing", "executing", "reconciliation_required"]),
    ))
    local_kinds = TelegramEventInbox.kind.in_(["listener_fact", "deleted"])
    ordered = (
        True
        if local_only is True
        else case((TelegramEventInbox.kind == "deleted", True), else_=~blocked_peer)
        if local_only is False
        else case((local_kinds, True), else_=~blocked_peer)
    )
    kind_filter = (
        True
        if local_only is None
        else TelegramEventInbox.kind.in_(list(LOCAL_KINDS))
        if local_only
        else ~TelegramEventInbox.kind.in_(list(LOCAL_KINDS))
    )
    query = (
        select(TelegramEventInbox).where(
            ordered,
            or_(TelegramEventInbox.account_id.in_(account_ids),
                 and_(TelegramEventInbox.kind.in_(["listener_fact", "deleted"]),
                     TelegramEventInbox.account_id.in_(local_account_ids))),
            TelegramEventInbox.state == "pending",
            TelegramEventInbox.next_attempt_at <= datetime.utcnow(),
            TelegramEventInbox.available_at <= datetime.utcnow() if settings.P1_RUNTIME_RELIABILITY_ENABLED else True,
            kind_filter,
        ).order_by(
            case((TelegramEventInbox.account_id > after_account_id, 0), else_=1),
            TelegramEventInbox.account_id,
            TelegramEventInbox.id,
        )
    )
    if settings.P1_RUNTIME_RELIABILITY_ENABLED:
        # Rank eligible events by account in SQL so fairness costs one round
        # trip, even with a large account pool. Lock only the outer inbox rows.
        ranked = query.order_by(None).with_only_columns(
            TelegramEventInbox.id.label("event_id"),
            func.row_number().over(
                partition_by=TelegramEventInbox.account_id,
                order_by=TelegramEventInbox.id,
            ).label("turn"),
        ).subquery()
        # Rank first, then lock each concrete row. Locking an outer join to a
        # windowed subquery can revisit the same visible row under concurrent
        # PostgreSQL snapshots; the per-id CAS lock makes a claim exclusive.
        candidate_ids = list((await db.scalars(select(ranked.c.event_id).order_by(
            ranked.c.turn,
            case((ranked.c.event_id > after_account_id, 0), else_=1),
            ranked.c.event_id,
        ).limit(max(128, limit * 8)))).all())
        rows = []
        for event_id in candidate_ids:
            row = await db.scalar(select(TelegramEventInbox).where(
                TelegramEventInbox.id == event_id,
                TelegramEventInbox.state == "pending",
            ).with_for_update(skip_locked=True))
            if row is not None:
                rows.append(row)
            if len(rows) >= limit:
                break
    else:
        rows = list((await db.scalars(query.with_for_update(skip_locked=True).limit(limit))).all())
    now = datetime.utcnow()
    for row in rows:
        row.state, row.started_at = "processing", now
        row.claim_owner = owner if settings.P1_RUNTIME_RELIABILITY_ENABLED else None
        row.claim_lease_expires_at = (
            now + timedelta(seconds=max(30, int(lease_seconds)))
            if settings.P1_RUNTIME_RELIABILITY_ENABLED else None
        )
        row.attempts += 1
    if rows:
        await db.flush()
    return rows


async def mark_external_attempt() -> None:
    """Commit before any external write; a lost response is never auto-replayed."""
    event_id = current_event_id.get()
    if event_id is None:
        return
    async with get_db_session() as db:
        row = await db.get(TelegramEventInbox, event_id, with_for_update=True, populate_existing=True)
        if row is None or row.state not in {"processing", "executing"}:
            raise RuntimeError("inbox_execution_claim_lost")
        _assert_claim(row, current_claim_owner.get())
        row.state = "executing"
        row.external_attempted = True


def _assert_claim(row: Any, owner: str | None) -> None:
    if row.claim_owner is not None and (
        row.claim_owner != owner
        or row.claim_lease_expires_at is None
        or row.claim_lease_expires_at <= datetime.utcnow()
        or row.state not in {"processing", "executing"}
    ):
        raise RuntimeError("inbox_execution_claim_lost")


async def finish(db: Any, event_id: int, error: Exception | None = None, *, owner: str | None = None) -> None:
    from app.core.account.rpc_governor import RpcDeferred
    row = await db.get(TelegramEventInbox, event_id, with_for_update=True, populate_existing=True)
    if row is None:
        return
    _assert_claim(row, owner if owner is not None else current_claim_owner.get())
    if owner is not None and row.claim_owner is None:
        raise RuntimeError("inbox_execution_claim_lost")
    now = datetime.utcnow()
    if error is None:
        # P3 treats inbox facts as the invalidation boundary.  Capture the
        # original payload before the completed-row audit replaces it, then
        # advance the local fact watermark and evict only affected Redis keys.
        if any((settings.P3_ENTITY_CACHE, settings.P3_GROUP_FACT_CACHE,
                settings.P3_SURVIVAL_CACHE, settings.P3_QUALIFICATION_SNAPSHOT)):
            try:
                original_payload = json.loads(row.payload_json or "{}")
            except (TypeError, ValueError):
                original_payload = {}
            event_data = {
                **original_payload,
                "account_id": row.account_id,
                # Listener facts carry the precise Telegram change kind in
                # their payload (membership, permissions, evidence, ...).
                # Preserve it so the cache evicts only the affected lane.
                "event_type": original_payload.get("kind") or row.kind,
                "target_key": original_payload.get("chat_id") or row.chat_id,
                "message_key": original_payload.get("message_id") or original_payload.get("id"),
                "deleted_ids": original_payload.get("ids") or original_payload.get("deleted_ids"),
            }
            try:
                from app.core.account.read_cache import invalidate_telegram_read_cache
                await invalidate_telegram_read_cache(event_data)
            except Exception:
                # Cache invalidation is best effort; local facts and the next
                # version check still prevent stale evidence from authorizing
                # a send.
                pass
            try:
                if event_data.get("target_key") is not None:
                    from app.modules.acquisition.qualification_snapshot import bump_fact_version
                    await bump_fact_version(db, str(event_data["target_key"]))
            except Exception:
                # A missing migration must not prevent the inbox event from
                # being acknowledged. The migration runner creates the table
                # before enabling the P3 flags.
                pass
        metadata = {}
        if row.kind == "listener_fact":
            try:
                fact = json.loads(row.payload_json or "{}")
            except (ValueError, TypeError):
                fact = {}
            metadata = {"audit": {"version": 1, "kind": fact.get("kind"), "source": "listener",
                                  "received_at": fact.get("at"), "attempts": row.attempts,
                                  "completed_at": now.isoformat()}}
        row.state, row.completed_at, row.payload_json = "completed", now, json.dumps(metadata)
        row.last_error = None
        if settings.P1_RUNTIME_RELIABILITY_ENABLED:
            row.checkpoint_stage = "completed"
            row.checkpoint_revision = row.event_key
            row.checkpoint_json = json.dumps({"completed": True, "external_attempted": row.external_attempted})
            row.checkpoint_updated_at = now
    elif isinstance(error, RpcDeferred) and not row.external_attempted:
        row.state = "pending"
        row.next_attempt_at = now + timedelta(seconds=error.retry_after_seconds + 1)
        row.last_error = error.reason
    elif row.kind in RECOVERABLE_KINDS and not row.external_attempted:
        row.state = 'pending'
        row.next_attempt_at = now + timedelta(seconds=min(300, 5 * max(1, row.attempts)))
        row.last_error = type(error).__name__
    else:
        row.state = "reconciliation_required"
        row.last_error = type(error).__name__
    row.claim_owner = None
    row.claim_lease_expires_at = None


async def recover_interrupted(db: Any) -> int:
    # Claims that never crossed the durable external-write marker are safe to
    # resume. An attempted write stays available for reconciliation only.
    from sqlalchemy import update
    safe = await db.execute(update(TelegramEventInbox).where(
        TelegramEventInbox.kind.in_(RECOVERABLE_KINDS), TelegramEventInbox.external_attempted.is_(False),
        TelegramEventInbox.state.in_(['processing', 'reconciliation_required']),
        or_(TelegramEventInbox.claim_lease_expires_at.is_(None),
            TelegramEventInbox.claim_lease_expires_at <= datetime.utcnow()),
    ).values(
        state='pending', next_attempt_at=datetime.utcnow(), available_at=datetime.utcnow(),
        claim_owner=None, claim_lease_expires_at=None,
        last_error='no_external_attempt_retry',
    ))
    result = await db.execute(update(TelegramEventInbox).where(
        TelegramEventInbox.state.in_(["processing", "executing"]),
        or_(TelegramEventInbox.claim_lease_expires_at.is_(None),
            TelegramEventInbox.claim_lease_expires_at <= datetime.utcnow()),
    ).values(
        state="reconciliation_required", claim_owner=None,
        claim_lease_expires_at=None, last_error="worker_interrupted",
    ))
    return result.rowcount + safe.rowcount


async def recover_expired_leases(db: Any, *, limit: int = 128) -> dict[str, int]:
    """Recover short P1 claims without replaying an attempted external write."""
    now = datetime.utcnow()
    rows = (
        await db.scalars(
            select(TelegramEventInbox)
            .where(
                TelegramEventInbox.state.in_(["processing", "executing"]),
                TelegramEventInbox.claim_lease_expires_at.is_not(None),
                TelegramEventInbox.claim_lease_expires_at <= now,
            )
            .order_by(TelegramEventInbox.claim_lease_expires_at, TelegramEventInbox.id)
            .with_for_update(skip_locked=True)
            .limit(max(1, min(int(limit), 512)))
        )
    ).all()
    retried = reconciled = 0
    for row in rows:
        row.claim_owner = None
        row.claim_lease_expires_at = None
        if not row.external_attempted and row.kind in RECOVERABLE_KINDS:
            row.state = "pending"
            row.next_attempt_at = now
            row.available_at = now
            row.last_error = "claim_lease_expired"
            retried += 1
        else:
            row.state = "reconciliation_required"
            row.last_error = "claim_lease_expired_after_external_attempt"
            reconciled += 1
    if rows:
        await db.flush()
    return {"retried": retried, "reconciled": reconciled}


async def save_checkpoint(
    db: Any,
    event_id: int,
    *,
    stage: str,
    revision: str,
    payload: dict[str, Any] | None = None,
    owner: str | None = None,
) -> Any:
    """Persist an idempotent, re-entrant inbox checkpoint."""
    row = await db.get(TelegramEventInbox, event_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise LookupError("inbox_event_not_found")
    _assert_claim(row, owner if owner is not None else current_claim_owner.get())
    row.checkpoint_stage = stage
    row.checkpoint_revision = revision
    row.checkpoint_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
    row.checkpoint_updated_at = datetime.utcnow()
    await db.flush()
    return row


def checkpoint_reusable(row: Any, *, stage: str, revision: str) -> bool:
    return bool(
        getattr(row, "checkpoint_stage", None) == stage
        and getattr(row, "checkpoint_revision", None) == revision
        and getattr(row, "checkpoint_json", None)
    )


async def recover_safe_events(db: Any, *, limit: int = 32) -> int:
    """Recover only durable claims that never crossed the external write marker."""
    rows = (await db.scalars(select(TelegramEventInbox).where(
        TelegramEventInbox.state == "reconciliation_required",
        TelegramEventInbox.external_attempted.is_(False),
        TelegramEventInbox.kind.in_(RECOVERABLE_KINDS),
    ).order_by(TelegramEventInbox.id).with_for_update(skip_locked=True).limit(limit))).all()
    for row in rows:
        row.state, row.next_attempt_at = "pending", datetime.utcnow()
        row.last_error = "no_external_attempt_retry"
    await db.flush()
    return len(rows)


async def recover_stale_processing(
    db: Any, *, stale_after_seconds: int = 900, limit: int = 128
) -> dict[str, int]:
    """Release claims left behind by a live worker that stopped making progress.

    A worker can lose its task without restarting (for example, while waiting
    for a database connection).  ``recover_interrupted`` only runs at process
    startup, so those claims otherwise remain in ``processing`` forever.  A
    claim that has not crossed the durable external-write marker is safe to
    retry.  A claim that did cross it must be parked for evidence instead of
    being replayed.
    """
    stale_after_seconds = max(60, int(stale_after_seconds))
    limit = max(1, min(int(limit), 512))
    cutoff = datetime.utcnow() - timedelta(seconds=stale_after_seconds)
    rows = (
        await db.scalars(
            select(TelegramEventInbox)
            .where(
                TelegramEventInbox.state.in_(["processing", "executing"]),
                TelegramEventInbox.started_at.is_not(None),
                TelegramEventInbox.started_at < cutoff,
                or_(TelegramEventInbox.claim_lease_expires_at.is_(None),
                    TelegramEventInbox.claim_lease_expires_at <= datetime.utcnow()),
            )
            .order_by(TelegramEventInbox.started_at, TelegramEventInbox.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    ).all()
    now = datetime.utcnow()
    retried = reconciled = 0
    for row in rows:
        row.claim_owner = None
        row.claim_lease_expires_at = None
        if not row.external_attempted and row.kind in RECOVERABLE_KINDS:
            row.state = "pending"
            row.next_attempt_at = now
            row.available_at = now
            row.last_error = "stale_claim_retry"
            retried += 1
        else:
            row.state = "reconciliation_required"
            row.last_error = "stale_external_attempt_reconciliation"
            reconciled += 1
    if rows:
        await db.flush()
    return {"retried": retried, "reconciled": reconciled}


async def resolve_reconciliation(db: Any, event_id: int, *, evidence: str, actor: str) -> Any:
    """Close an operator-reconciled event without issuing or replaying a write."""
    if len(evidence.strip()) < 10:
        raise ValueError("reconciliation_evidence_required")
    row = await db.get(TelegramEventInbox, event_id, with_for_update=True)
    if row is None:
        raise LookupError("inbox_event_not_found")
    if row.state != "reconciliation_required":
        raise ValueError("inbox_event_not_awaiting_reconciliation")
    data = json.loads(row.payload_json or "{}")
    data["reconciliation"] = {"evidence": evidence, "actor": actor,
                              "resolved_at": datetime.utcnow().isoformat(), "replayed": False}
    row.payload_json = json.dumps(data, ensure_ascii=False)
    row.state, row.completed_at = "reconciled", datetime.utcnow()
    await db.flush()
    return row


async def inbox_snapshot(db: Any) -> dict:
    rows = (await db.execute(select(TelegramEventInbox.state, func.count()).group_by(TelegramEventInbox.state))).all()
    oldest = await db.scalar(select(func.min(TelegramEventInbox.received_at)).where(TelegramEventInbox.state == "pending"))
    return {"states": dict(rows), "oldest_pending_seconds": max(0, int((datetime.utcnow()-oldest).total_seconds())) if oldest else 0}
