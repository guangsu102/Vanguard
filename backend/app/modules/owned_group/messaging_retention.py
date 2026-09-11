"""Targeted retention cleanup for stage-three owned-group Persona data.

The cleaner intentionally knows about only two data classes:

* the body-like ``persona_snapshot`` on terminal message executions; and
* the explicit stage-three Persona configuration/settings audit events.

It never deletes executions or their hash/version metadata, and it never acts
on another ``OwnedGroupAuditEvent`` resource or event type.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.owned_group.messaging_contracts import TERMINAL_EXECUTION_STATUSES
from app.modules.owned_group.messaging_models import GroupAccountMessageExecution
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent

logger = structlog.get_logger().bind(module="owned_group_persona_retention")

PERSONA_SNAPSHOT_RETENTION_DAYS = 90
PERSONA_AUDIT_RETENTION_DAYS = 365
MAX_RETENTION_BATCH_SIZE = 500
PERSONA_RETENTION_LOCK_KEY = "vanguard:owned-group:persona-retention:lock"
PERSONA_RETENTION_LOCK_SECONDS = 6 * 60 * 60

PERSONA_AUDIT_RESOURCE_TYPES = frozenset({"account_persona", "persona_setting"})
PERSONA_AUDIT_EVENT_TYPES = frozenset(
    {
        "account_persona_created",
        "account_persona_updated",
        "account_persona_reset",
        "account_persona_preview_generated",
        "account_persona_preview_failed",
        "account_persona_feature_updated",
    }
)

_RELEASE_LOCK_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)
_REFRESH_LOCK_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)


@dataclass(slots=True)
class PersonaRetentionMetrics:
    """Low-cardinality counters returned and emitted by every cleanup run."""

    scanned: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    would_update: int = 0
    would_delete: int = 0
    execution_scanned: int = 0
    audit_scanned: int = 0
    dry_run: bool = False
    lock_acquired: bool = False
    skipped_locked: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class PersonaRetentionLock:
    """Token-owned Redis lease used to keep the daily task single-instance."""

    def __init__(
        self,
        client: Any,
        *,
        key: str = PERSONA_RETENTION_LOCK_KEY,
        lease_seconds: int = PERSONA_RETENTION_LOCK_SECONDS,
    ) -> None:
        self.client = client
        self.key = key
        self.lease_seconds = max(60, int(lease_seconds))
        self.token = uuid4().hex
        self.acquired = False

    async def acquire(self) -> bool:
        acquired = await self.client.set(
            self.key,
            self.token,
            nx=True,
            ex=self.lease_seconds,
        )
        self.acquired = bool(acquired)
        return self.acquired

    async def refresh(self) -> bool:
        if not self.acquired:
            return False
        refreshed = await self.client.eval(
            _REFRESH_LOCK_SCRIPT,
            1,
            self.key,
            self.token,
            self.lease_seconds,
        )
        if not bool(refreshed):
            self.acquired = False
        return bool(refreshed)

    async def release(self) -> bool:
        if not self.acquired:
            return False
        released = await self.client.eval(
            _RELEASE_LOCK_SCRIPT,
            1,
            self.key,
            self.token,
        )
        self.acquired = False
        return bool(released)


class PersonaRetentionLockLost(RuntimeError):
    """Raised before a batch when the caller no longer owns the Redis lease."""


class PersonaRetentionCleaner:
    """Cursor-based, commit-per-batch cleanup with a deliberately narrow scope."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        batch_size: int = MAX_RETENTION_BATCH_SIZE,
        dry_run: bool = False,
        heartbeat: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        if not 1 <= int(batch_size) <= MAX_RETENTION_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_RETENTION_BATCH_SIZE}")
        self.db = db
        self.now = now or datetime.utcnow()
        self.batch_size = int(batch_size)
        self.dry_run = bool(dry_run)
        self.heartbeat = heartbeat

    async def run(self) -> PersonaRetentionMetrics:
        metrics = PersonaRetentionMetrics(dry_run=self.dry_run, lock_acquired=True)
        try:
            await self._cleanup_execution_snapshots(metrics)
            await self._cleanup_persona_audits(metrics)
        except Exception as exc:
            await self.db.rollback()
            metrics.failed += 1
            logger.exception(
                "owned_group_persona_retention_failed",
                error_type=type(exc).__name__,
                **metrics.to_dict(),
            )

        logger.info("owned_group_persona_retention_completed", **metrics.to_dict())
        return metrics

    async def _require_lock(self) -> None:
        if self.heartbeat is not None and not await self.heartbeat():
            raise PersonaRetentionLockLost("Persona retention Redis lease was lost")

    async def _cleanup_execution_snapshots(self, metrics: PersonaRetentionMetrics) -> None:
        cutoff = self.now - timedelta(days=PERSONA_SNAPSHOT_RETENTION_DAYS)
        cursor = 0
        terminal_statuses = tuple(sorted(TERMINAL_EXECUTION_STATUSES))

        while True:
            await self._require_lock()
            result = await self.db.execute(
                select(GroupAccountMessageExecution.id)
                .where(
                    GroupAccountMessageExecution.id > cursor,
                    GroupAccountMessageExecution.status.in_(terminal_statuses),
                    GroupAccountMessageExecution.updated_at < cutoff,
                    GroupAccountMessageExecution.persona_snapshot.is_not(None),
                )
                .order_by(GroupAccountMessageExecution.id)
                .limit(self.batch_size)
            )
            ids = [int(row_id) for row_id in result.scalars().all()]
            if not ids:
                break

            cursor = ids[-1]
            metrics.scanned += len(ids)
            metrics.execution_scanned += len(ids)
            metrics.would_update += len(ids)

            if self.dry_run:
                await self.db.commit()
                continue

            changed = await self.db.execute(
                update(GroupAccountMessageExecution)
                .where(
                    GroupAccountMessageExecution.id.in_(ids),
                    GroupAccountMessageExecution.status.in_(terminal_statuses),
                    GroupAccountMessageExecution.updated_at < cutoff,
                    GroupAccountMessageExecution.persona_snapshot.is_not(None),
                )
                .values(persona_snapshot=None)
            )
            metrics.updated += max(0, int(changed.rowcount or 0))
            await self.db.commit()

    async def _cleanup_persona_audits(self, metrics: PersonaRetentionMetrics) -> None:
        cutoff = self.now - timedelta(days=PERSONA_AUDIT_RETENTION_DAYS)
        cursor = 0
        resource_types = tuple(sorted(PERSONA_AUDIT_RESOURCE_TYPES))
        event_types = tuple(sorted(PERSONA_AUDIT_EVENT_TYPES))

        while True:
            await self._require_lock()
            result = await self.db.execute(
                select(OwnedGroupAuditEvent.id)
                .where(
                    OwnedGroupAuditEvent.id > cursor,
                    OwnedGroupAuditEvent.resource_type.in_(resource_types),
                    OwnedGroupAuditEvent.event_type.in_(event_types),
                    OwnedGroupAuditEvent.created_at < cutoff,
                )
                .order_by(OwnedGroupAuditEvent.id)
                .limit(self.batch_size)
            )
            ids = [int(row_id) for row_id in result.scalars().all()]
            if not ids:
                break

            cursor = ids[-1]
            metrics.scanned += len(ids)
            metrics.audit_scanned += len(ids)
            metrics.would_delete += len(ids)

            if self.dry_run:
                await self.db.commit()
                continue

            removed = await self.db.execute(
                delete(OwnedGroupAuditEvent).where(
                    OwnedGroupAuditEvent.id.in_(ids),
                    OwnedGroupAuditEvent.resource_type.in_(resource_types),
                    OwnedGroupAuditEvent.event_type.in_(event_types),
                    OwnedGroupAuditEvent.created_at < cutoff,
                )
            )
            metrics.deleted += max(0, int(removed.rowcount or 0))
            await self.db.commit()


async def run_persona_retention(
    db: AsyncSession,
    *,
    redis_client: Any,
    now: datetime | None = None,
    batch_size: int = MAX_RETENTION_BATCH_SIZE,
    dry_run: bool = False,
    lock_seconds: int = PERSONA_RETENTION_LOCK_SECONDS,
) -> dict[str, int | bool]:
    """Acquire the distributed lease and execute one targeted retention pass."""

    lease = PersonaRetentionLock(redis_client, lease_seconds=lock_seconds)
    if not await lease.acquire():
        metrics = PersonaRetentionMetrics(
            dry_run=bool(dry_run),
            lock_acquired=False,
            skipped_locked=True,
        )
        logger.info("owned_group_persona_retention_skipped_locked", **metrics.to_dict())
        return metrics.to_dict()

    try:
        cleaner = PersonaRetentionCleaner(
            db,
            now=now,
            batch_size=batch_size,
            dry_run=dry_run,
            heartbeat=lease.refresh,
        )
        metrics = await cleaner.run()
        return metrics.to_dict()
    finally:
        try:
            await lease.release()
        except Exception as exc:  # pragma: no cover - Redis runtime boundary
            logger.exception(
                "owned_group_persona_retention_lock_release_failed",
                error_type=type(exc).__name__,
            )


__all__ = [
    "MAX_RETENTION_BATCH_SIZE",
    "PERSONA_AUDIT_EVENT_TYPES",
    "PERSONA_AUDIT_RESOURCE_TYPES",
    "PERSONA_AUDIT_RETENTION_DAYS",
    "PERSONA_RETENTION_LOCK_KEY",
    "PERSONA_SNAPSHOT_RETENTION_DAYS",
    "PersonaRetentionCleaner",
    "PersonaRetentionLock",
    "PersonaRetentionMetrics",
    "run_persona_retention",
]
