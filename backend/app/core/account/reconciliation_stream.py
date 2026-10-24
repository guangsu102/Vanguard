"""Durable reconciliation outbox and lossy Redis Stream wake-up channel."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, or_, select, update

from app.core.account.control_models import ReconciliationTask
from app.core.config import settings

STREAM_KEY = "vanguard:reconciliation:v1"
CONSUMER_GROUP = "reconciliation"
STREAM_MAXLEN = 50_000
LEASE_SECONDS = 300


def task_key(kind: str, source_id: int) -> str:
    return f"{kind}:{int(source_id)}:v1"


def active_for(account_id: int) -> bool:
    return (
        settings.RECONCILIATION_STREAM_ENABLED
        and not settings.RECONCILIATION_SHADOW_MODE
        and (
            settings.RECONCILIATION_ACCOUNT_IDS.strip() == "*"
            or account_id in settings.reconciliation_accounts
        )
    )


def rollout_filter(column: Any) -> Any:
    """Shadow inventories all accounts; active work respects the canary set."""
    if settings.RECONCILIATION_SHADOW_MODE or settings.RECONCILIATION_ACCOUNT_IDS.strip() == "*":
        return True
    return column.in_(settings.reconciliation_accounts)


def legacy_account_filter(column: Any) -> Any:
    if not settings.RECONCILIATION_STREAM_ENABLED or settings.RECONCILIATION_SHADOW_MODE:
        return True
    if settings.RECONCILIATION_ACCOUNT_IDS.strip() == "*":
        return False
    return column.notin_(settings.reconciliation_accounts)


async def ensure_group(cache: Any) -> None:
    try:
        await cache.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0-0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _insert_task(
    db: Any, *, kind: str, source_id: int, account_id: int, priority: int,
    available_at: datetime | None = None,
) -> None:
    from app.core.account.budget_ledger import conflict_insert

    ready_at = available_at or datetime.utcnow()
    await db.execute(
        conflict_insert(db, ReconciliationTask)
        .values(
            task_key=task_key(kind, source_id),
            kind=kind,
            source_id=int(source_id),
            account_id=int(account_id),
            priority=int(priority),
            state="pending",
            available_at=ready_at,
            verification_due_at=ready_at,
        )
        .on_conflict_do_nothing(index_elements=["task_key"])
    )


async def scan_database(db: Any, *, limit: int = 300) -> int:
    """Create outbox rows only; source business rows are never rewritten."""
    from app.core.worker_status import TelegramEventInbox
    from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt
    from app.modules.acquisition.p2_models import GrowthOperation

    def missing(kind: str, source: Any) -> Any:
        return ~exists(
            select(ReconciliationTask.id).where(
                ReconciliationTask.kind == kind,
                ReconciliationTask.source_id == source,
            )
        )

    join_filter = [
        rollout_filter(AutoJoinAttempt.account_id),
        AutoJoinAttempt.telegram_action_attempted.is_(True),
        AutoJoinAttempt.request_state.in_(["sent", "outcome_unknown"]),
        AutoJoinAttempt.status != "success",
        AutoJoinAttempt.reconciliation_status.not_in(["confirmed", "resolved"]),
        missing("join", AutoJoinAttempt.id),
    ]
    ad_filter = [
        rollout_filter(AdDeliveryLog.account_id),
        AdDeliveryLog.status.in_(["pending", "unknown", "sending", "reconciliation_required"]),
        or_(
            AdDeliveryLog.status.in_(["unknown", "reconciliation_required"]),
            AdDeliveryLog.error.ilike("%unknown%"),
            AdDeliveryLog.created_at < datetime.utcnow() - timedelta(minutes=10),
        ),
        missing("ad", AdDeliveryLog.id),
    ]
    inbox_filter = [
        rollout_filter(TelegramEventInbox.account_id),
        TelegramEventInbox.state == "reconciliation_required",
        missing("inbox", TelegramEventInbox.id),
    ]
    growth_filter = [
        rollout_filter(GrowthOperation.account_id),
        GrowthOperation.reconciliation_required.is_(True),
        missing("growth", GrowthOperation.id),
    ]
    rows = []
    for source_id, account_id, request_state, reason, next_at in (
        await db.execute(
            select(AutoJoinAttempt.id, AutoJoinAttempt.account_id,
                   AutoJoinAttempt.request_state, AutoJoinAttempt.reason,
                   AutoJoinAttempt.reconciliation_next_at)
            .where(*join_filter)
            .order_by(AutoJoinAttempt.id)
            .limit(limit)
        )
    ).all():
        # Only a confirmed sent request with a completed pending-approval check
        # can start a future cycle. Budget/transport failures and unknown writes
        # retain their original overdue age, including while they back off.
        scheduled = next_at if request_state == "sent" and reason == "join_request_still_pending" else None
        await _insert_task(db, kind="join", source_id=source_id, account_id=account_id,
                           priority=20, available_at=scheduled)
        rows.append(1)
    for source_id, account_id in (
        await db.execute(
            select(AdDeliveryLog.id, AdDeliveryLog.account_id)
            .where(*ad_filter)
            .order_by(AdDeliveryLog.id)
            .limit(limit)
        )
    ).all():
        await _insert_task(db, kind="ad", source_id=source_id, account_id=account_id, priority=10)
        rows.append(1)
    for source_id, account_id in (
        await db.execute(
            select(TelegramEventInbox.id, TelegramEventInbox.account_id)
            .where(*inbox_filter)
            .order_by(TelegramEventInbox.id)
            .limit(limit)
        )
    ).all():
        await _insert_task(
            db, kind="inbox", source_id=source_id, account_id=account_id, priority=30
        )
        rows.append(1)
    for source_id, account_id in (
        await db.execute(
            select(GrowthOperation.id, GrowthOperation.account_id)
            .where(*growth_filter)
            .order_by(GrowthOperation.updated_at, GrowthOperation.id)
            .limit(limit)
        )
    ).all():
        await _insert_task(db, kind="growth", source_id=source_id, account_id=account_id, priority=0)
        rows.append(1)
    if rows:
        await db.commit()
    return len(rows)


async def publish_pending(db: Any, cache: Any, *, limit: int = 300) -> int:
    await ensure_group(cache)
    now = datetime.utcnow()
    rows = list(
        (
            await db.scalars(
                select(ReconciliationTask)
                .where(
                    rollout_filter(ReconciliationTask.account_id),
                    ReconciliationTask.state == "pending",
                    ReconciliationTask.available_at <= now,
                    or_(
                        ReconciliationTask.published_at.is_(None),
                        ReconciliationTask.published_at < now - timedelta(seconds=30),
                    ),
                )
                .order_by(ReconciliationTask.priority, ReconciliationTask.id)
                .limit(limit)
            )
        ).all()
    )
    for row in rows:
        await cache.xadd(
            STREAM_KEY,
            {
                "task_key": row.task_key,
                "kind": row.kind,
                "source_id": str(row.source_id),
                "account_id": str(row.account_id),
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        row.published_at = now
    if rows:
        await db.commit()
    return len(rows)


async def claim_task(
    db: Any, key: str, owner: str, *, active_only: bool = False
) -> ReconciliationTask | None:
    now = datetime.utcnow()
    row = await db.scalar(
        select(ReconciliationTask)
        .where(
            rollout_filter(ReconciliationTask.account_id) if active_only else True,
            ReconciliationTask.task_key == key,
            or_(ReconciliationTask.state == "pending", ReconciliationTask.state == "leased"),
            or_(
                ReconciliationTask.state == "pending",
                ReconciliationTask.lease_expires_at.is_(None),
                ReconciliationTask.lease_expires_at <= now,
            ),
            ReconciliationTask.available_at <= now,
        )
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    row.state, row.lease_owner = "leased", owner
    row.lease_expires_at, row.attempts = now + timedelta(seconds=LEASE_SECONDS), row.attempts + 1
    await db.commit()
    return row


async def finish_task(
    db: Any,
    key: str,
    owner: str,
    *,
    state: str,
    error_code: str | None = None,
    retry_after: int = 60,
    next_verification_at: datetime | None = None,
) -> bool:
    if state not in {"succeeded", "manual_required", "cancelled", "pending"}:
        raise ValueError("invalid_reconciliation_state")
    now = datetime.utcnow()
    values: dict[str, Any] = {
        "state": state,
        "last_error_code": error_code,
        "lease_owner": None,
        "lease_expires_at": None,
    }
    if state in {"succeeded", "manual_required", "cancelled"}:
        values["finished_at"] = now
    else:
        values.update(available_at=now + timedelta(seconds=max(1, retry_after)), published_at=None)
        if next_verification_at is not None:
            # Caller supplies this only after a known sent request has been
            # verified as still awaiting approval. Ordinary retries never move
            # the SLO start, so an unavailable budget cannot hide a stuck task.
            values.update(available_at=next_verification_at,
                          verification_due_at=next_verification_at)
    result = await db.execute(
        update(ReconciliationTask)
        .where(
            ReconciliationTask.task_key == key,
            ReconciliationTask.state == "leased",
            ReconciliationTask.lease_owner == owner,
            ReconciliationTask.lease_expires_at > now,
        )
        .values(**values)
        .returning(ReconciliationTask.id)
    )
    await db.commit()
    return result.scalar_one_or_none() is not None


async def recover_expired(db: Any, *, limit: int = 200) -> int:
    now = datetime.utcnow()
    ids = (
        select(ReconciliationTask.id)
        .where(
            ReconciliationTask.state == "leased",
            ReconciliationTask.lease_expires_at <= now,
        )
        .order_by(ReconciliationTask.lease_expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(
        update(ReconciliationTask)
        .where(ReconciliationTask.id.in_(ids))
        .values(
            state="pending",
            lease_owner=None,
            lease_expires_at=None,
            available_at=now,
            published_at=None,
        )
        .returning(ReconciliationTask.id)
    )
    await db.commit()
    return len(result.fetchall())


async def read_group(cache: Any, *, consumer: str, count: int = 8, block_ms: int = 1000) -> list:
    await ensure_group(cache)
    result = await cache.xreadgroup(
        CONSUMER_GROUP, consumer, {STREAM_KEY: ">"}, count=count, block=block_ms
    )
    return [
        (message_id, fields)
        for _stream, messages in (result or [])
        for message_id, fields in messages
    ]


async def reclaim(cache: Any, *, consumer: str, min_idle_ms: int = 300_000) -> list:
    await ensure_group(cache)
    pending = await cache.xpending(STREAM_KEY, CONSUMER_GROUP)
    if isinstance(pending, dict) and not pending.get("pending"):
        return []
    cursor_key = "vanguard:reconciliation:claim_cursor:" + consumer
    cursor = await cache.get(cursor_key) or "0-0"
    result = await cache.xautoclaim(
        STREAM_KEY, CONSUMER_GROUP, consumer, min_idle_ms, start_id=cursor, count=100
    )
    await cache.set(cursor_key, result[0], ex=3600)
    return result[1]
