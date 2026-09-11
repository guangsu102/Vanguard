"""One bounded worker tick for stage-two owned-group messages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.automation_settings import get_owned_group_messaging_settings
from app.core.config import settings
from app.modules.owned_group.lock_queries import message_execution_for_update_query
from app.modules.owned_group.messaging_contracts import MessageExecutionStatus
from app.modules.owned_group.messaging_execution_service import (
    OwnedGroupMessageExecutionService,
)
from app.modules.owned_group.messaging_models import GroupAccountMessageExecution
from app.modules.owned_group.messaging_trigger_service import (
    OwnedGroupMessageTriggerService,
)
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent


class OwnedGroupMessageWorker:
    def __init__(
        self,
        db: AsyncSession,
        *,
        account_pool: AccountPool | None = None,
        execution_service: OwnedGroupMessageExecutionService | None = None,
    ) -> None:
        self.db = db
        self.account_pool = account_pool or AccountPool()
        self.execution_service = execution_service or OwnedGroupMessageExecutionService(
            db,
            account_pool=self.account_pool,
        )

    async def _expire_reviews(
        self,
        *,
        now: datetime,
        ttl_hours: int,
        limit: int,
    ) -> int:
        ids = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessageExecution.id)
                    .where(
                        GroupAccountMessageExecution.status
                        == MessageExecutionStatus.PENDING_REVIEW.value,
                        GroupAccountMessageExecution.updated_at
                        <= now - timedelta(hours=ttl_hours),
                    )
                    .order_by(GroupAccountMessageExecution.id)
                    .limit(limit)
                )
            ).all()
        )
        expired = 0
        for execution_id in ids:
            execution = await self.db.scalar(
                message_execution_for_update_query(skip_locked=True)
                .where(
                    GroupAccountMessageExecution.id == int(execution_id),
                    GroupAccountMessageExecution.status
                    == MessageExecutionStatus.PENDING_REVIEW.value,
                )
            )
            if execution is None:
                await self.db.rollback()
                continue
            execution.status = MessageExecutionStatus.EXPIRED.value
            execution.error_code = "REVIEW_EXPIRED"
            execution.error_message = "审核窗口已过期"
            execution.revision = int(execution.revision) + 1
            self.db.add(
                OwnedGroupAuditEvent(
                    event_type="message_execution_failed",
                    group_asset_id=int(execution.owned_group_asset_id),
                    resource_type="message_execution",
                    resource_id=int(execution.id),
                    actor_id=None,
                    after_state=json.dumps(
                        {
                            "execution_id": int(execution.id),
                            "account_id": int(execution.account_id),
                            "status": MessageExecutionStatus.EXPIRED.value,
                        },
                        sort_keys=True,
                    ),
                    result="expired",
                    reason_code="REVIEW_EXPIRED",
                    correlation_id=execution.correlation_id,
                )
            )
            await self.db.commit()
            expired += 1
        return expired

    async def tick(
        self,
        *,
        limit: int = 50,
        now: datetime | None = None,
    ) -> dict[str, int]:
        now = now or datetime.utcnow()
        bounded_limit = max(1, min(int(limit), 200))
        runtime = await get_owned_group_messaging_settings(self.db)
        metrics = {
            "processed": 0,
            "created": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "pending_review": 0,
            "expired": 0,
            "retried": 0,
            "recovered_generating": 0,
            "failed_stale_generating": 0,
        }
        metrics["expired"] = await self._expire_reviews(
            now=now,
            ttl_hours=max(1, int(runtime.get("reviewTtlHours", 24) or 24)),
            limit=bounded_limit,
        )

        if bool(settings.OWNED_GROUP_MESSAGING_ENABLED) and bool(
            runtime.get("enabled", False)
        ):
            scheduled = (
                await OwnedGroupMessageTriggerService(
                    self.db
                ).create_due_scheduled_executions(
                    now=now,
                    limit=bounded_limit,
                )
            )
            metrics["created"] = len(scheduled)

        stale_generating_ids = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessageExecution.id)
                    .where(
                        GroupAccountMessageExecution.status
                        == MessageExecutionStatus.GENERATING.value,
                        or_(
                            GroupAccountMessageExecution.lease_expires_at.is_(None),
                            GroupAccountMessageExecution.lease_expires_at < now,
                        ),
                    )
                    .order_by(GroupAccountMessageExecution.id)
                    .limit(bounded_limit)
                )
            ).all()
        )
        for execution_id in stale_generating_ids:
            result = await self.execution_service.fail_stale_generating(
                int(execution_id)
            )
            if (
                result is not None
                and str(result.status) == MessageExecutionStatus.FAILED.value
            ):
                metrics["failed_stale_generating"] += 1

        queued_ids = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessageExecution.id)
                    .where(
                        GroupAccountMessageExecution.status
                        == MessageExecutionStatus.QUEUED.value,
                        or_(
                            GroupAccountMessageExecution.scheduled_at.is_(None),
                            GroupAccountMessageExecution.scheduled_at <= now,
                        ),
                        or_(
                            GroupAccountMessageExecution.next_retry_at.is_(None),
                            GroupAccountMessageExecution.next_retry_at <= now,
                        ),
                    )
                    .order_by(GroupAccountMessageExecution.id)
                    .limit(bounded_limit)
                )
            ).all()
        )
        for execution_id in queued_ids:
            result = await self.execution_service.prepare_execution(int(execution_id))
            if result is None:
                continue
            metrics["processed"] += 1
            status = str(result.status)
            if status == MessageExecutionStatus.PENDING_REVIEW.value:
                metrics["pending_review"] += 1
            elif status == MessageExecutionStatus.SKIPPED.value:
                metrics["skipped"] += 1
            elif status == MessageExecutionStatus.FAILED.value:
                metrics["failed"] += 1

        ready_ids = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessageExecution.id)
                    .where(
                        GroupAccountMessageExecution.status
                        == MessageExecutionStatus.READY_TO_SEND.value,
                        or_(
                            GroupAccountMessageExecution.scheduled_at.is_(None),
                            GroupAccountMessageExecution.scheduled_at <= now,
                        ),
                        or_(
                            GroupAccountMessageExecution.next_retry_at.is_(None),
                            GroupAccountMessageExecution.next_retry_at <= now,
                        ),
                    )
                    .order_by(GroupAccountMessageExecution.id)
                    .limit(bounded_limit)
                )
            ).all()
        )
        for execution_id in ready_ids:
            before_attempt = await self.db.scalar(
                select(GroupAccountMessageExecution.attempt_count).where(
                    GroupAccountMessageExecution.id == int(execution_id)
                )
            )
            result = await self.execution_service.send_execution(int(execution_id))
            if result is None:
                continue
            metrics["processed"] += 1
            status = str(result.status)
            if status == MessageExecutionStatus.SENT.value:
                metrics["sent"] += 1
            elif status == MessageExecutionStatus.SKIPPED.value:
                metrics["skipped"] += 1
            elif status == MessageExecutionStatus.FAILED.value:
                metrics["failed"] += 1
            elif (
                status == MessageExecutionStatus.READY_TO_SEND.value
                and int(result.attempt_count) > int(before_attempt or 0)
            ):
                metrics["retried"] += 1

        stale_ids = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessageExecution.id)
                    .where(
                        GroupAccountMessageExecution.status
                        == MessageExecutionStatus.SENDING.value,
                        GroupAccountMessageExecution.lease_expires_at < now,
                    )
                    .order_by(GroupAccountMessageExecution.id)
                    .limit(bounded_limit)
                )
            ).all()
        )
        for execution_id in stale_ids:
            result = await self.execution_service.fail_stale_sending(
                int(execution_id)
            )
            if result is not None:
                metrics["processed"] += 1
                if str(result.status) == MessageExecutionStatus.FAILED.value:
                    metrics["failed"] += 1
                else:
                    metrics["retried"] += 1
        return metrics


async def run_owned_group_message_tick(
    db: AsyncSession,
    *,
    limit: int = 50,
    now: datetime | None = None,
    account_pool: AccountPool | None = None,
) -> dict[str, int]:
    return await OwnedGroupMessageWorker(
        db,
        account_pool=account_pool,
    ).tick(limit=limit, now=now)


__all__ = ["OwnedGroupMessageWorker", "run_owned_group_message_tick"]
