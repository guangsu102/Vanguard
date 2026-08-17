"""Recover public groups orphaned when a promoter account is banned."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import (
    AccountAdBinding,
    AutoJoinAttempt,
    DeliveryStatus,
    GroupFailoverStatus,
    GroupFailoverTask,
)

if TYPE_CHECKING:
    from app.modules.acquisition.automation import (
        AcquisitionAutomationService,
        JoinedGroupAuditResult,
    )


logger = structlog.get_logger()

FAILOVER_MAX_ATTEMPTS = 5
FAILOVER_RETRY_BASE_MINUTES = 15
FAILOVER_JOINING_STALE_MINUTES = 30
FAILOVER_SOURCE_MEMBERSHIP_STATUS = "account_lost"
FAILOVER_DUE_STATUSES = (GroupFailoverStatus.QUEUED.value, GroupFailoverStatus.RETRY.value)
FAILOVER_TERMINAL_STATUSES = (
    GroupFailoverStatus.SUCCEEDED.value,
    GroupFailoverStatus.FAILED.value,
    GroupFailoverStatus.CANCELLED.value,
)


def _now() -> datetime:
    return datetime.utcnow()


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class FailoverRunResult:
    processed: int = 0
    created: int = 0
    updated: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "created": self.created,
            "updated": self.updated,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "details": self.details,
        }


class GroupFailoverService:
    """Discover and recover group memberships lost with a banned account."""

    def __init__(self, db: AsyncSession, automation: AcquisitionAutomationService) -> None:
        self.db = db
        self.automation = automation
        self.logger = logger.bind(module="group_failover")

    async def run(
        self,
        *,
        max_tasks: int = 20,
        dry_run: bool = False,
        target_account_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        result = FailoverRunResult()
        now = _now()
        selected_target_ids = set(target_account_ids or []) or None
        orphaned = await self._list_orphaned_memberships()
        result.processed += len(orphaned)

        if dry_run:
            for membership, _account, group in orphaned[:100]:
                result.details.append(
                    {
                        "action": "would_enqueue",
                        "source_account_id": membership.account_id,
                        "group_id": group.id,
                        "telegram_group_id": group.group_id,
                        "automatic": bool(group.username),
                    }
                )
            due_count = await self._due_task_count(now)
            result.details.append({"action": "would_process_existing", "count": due_count})
            return result.as_dict()

        result.updated += await self._recover_stale_claims(now)
        result.created += await self._enqueue_orphaned_memberships(
            orphaned, now, target_account_ids=selected_target_ids
        )
        result.updated += await self._complete_tasks_with_existing_coverage(
            now, target_account_ids=selected_target_ids
        )

        tasks = await self._list_due_tasks(now, max(1, min(int(max_tasks), 100)))
        for task in tasks:
            result.processed += 1
            group = task.group
            if group is None:
                await self._mark_terminal(task, GroupFailoverStatus.FAILED, "group_missing", now)
                result.failed += 1
                continue

            target = await self._resolve_target_account(
                task, now, target_account_ids=selected_target_ids
            )
            if target is None:
                if not group.username:
                    await self._mark_terminal(
                        task,
                        GroupFailoverStatus.MANUAL_REQUIRED,
                        "public_username_required",
                        now,
                    )
                    result.updated += 1
                else:
                    await self._defer_without_attempt(task, "no_eligible_target_account", now)
                    result.skipped += 1
                continue

            if not await self._claim_task(task, target.id, now):
                result.skipped += 1
                continue

            try:
                outcome = await self._execute_claimed_task(task.id, now)
            except Exception as exc:
                await self.db.rollback()
                message = str(exc)[:2000]
                await self._schedule_retry(task.id, "failover_execution_error", message, now)
                result.failed += 1
                result.errors.append(f"task={task.id}: {message}")
                continue

            result.details.append(outcome)
            if outcome["status"] == GroupFailoverStatus.SUCCEEDED.value:
                result.succeeded += 1
            elif outcome["status"] in {
                GroupFailoverStatus.FAILED.value,
                GroupFailoverStatus.MANUAL_REQUIRED.value,
            }:
                result.failed += 1
            else:
                result.skipped += 1

        return result.as_dict()

    async def _list_orphaned_memberships(
        self,
    ) -> list[tuple[GroupAccountMembership, TelegramAccount, Group]]:
        rows = await self.db.execute(
            select(GroupAccountMembership, TelegramAccount, Group)
            .join(TelegramAccount, TelegramAccount.id == GroupAccountMembership.account_id)
            .join(Group, Group.id == GroupAccountMembership.group_id)
            .where(
                GroupAccountMembership.status == "joined",
                Group.status == "active",
                or_(
                    TelegramAccount.status == AccountStatus.BANNED,
                    TelegramAccount.risk_reason == "account_banned",
                ),
            )
            .order_by(Group.level_score.desc(), GroupAccountMembership.id)
        )
        return list(rows.all())

    async def _enqueue_orphaned_memberships(
        self,
        orphaned: list[tuple[GroupAccountMembership, TelegramAccount, Group]],
        now: datetime,
        *,
        target_account_ids: set[int] | None = None,
    ) -> int:
        if not orphaned:
            return 0
        source_ids = [membership.id for membership, _account, _group in orphaned]
        existing = await self.db.execute(
            select(GroupFailoverTask.source_membership_id).where(
                GroupFailoverTask.source_membership_id.in_(source_ids)
            )
        )
        existing_ids = set(existing.scalars().all())
        created = 0
        for membership, source_account, group in orphaned:
            if membership.id in existing_ids:
                continue
            covered_account_id = await self._healthy_joined_account_id(
                group.id, source_account.id, now, target_account_ids=target_account_ids
            )
            status = (
                GroupFailoverStatus.SUCCEEDED.value
                if covered_account_id is not None
                else GroupFailoverStatus.QUEUED.value
                if group.username
                else GroupFailoverStatus.MANUAL_REQUIRED.value
            )
            reason = (
                "already_covered"
                if covered_account_id is not None
                else "account_banned"
                if group.username
                else "public_username_required"
            )
            task = GroupFailoverTask(
                source_membership_id=membership.id,
                source_account_id=source_account.id,
                target_account_id=covered_account_id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                status=status,
                reason=reason,
                completed_at=now if status == GroupFailoverStatus.SUCCEEDED.value else None,
            )
            self.db.add(task)
            if covered_account_id is not None:
                await self._copy_source_ad_bindings(source_account.id, covered_account_id)
            membership.status = FAILOVER_SOURCE_MEMBERSHIP_STATUS
            membership.last_checked_at = now
            membership.warmup_status = "blocked"
            membership.probe_status = "skipped"
            membership.ad_status = "blocked"
            membership.note = self.automation._append_membership_note(
                membership.note,
                {"event": "account_failover_queued", "reason": "account_banned"},
            )
            created += 1
        await self.db.commit()
        return created

    async def _healthy_joined_account_id(
        self,
        group_id: int,
        source_account_id: int,
        now: datetime,
        *,
        target_account_ids: set[int] | None = None,
    ) -> int | None:
        target_account_filter = (
            TelegramAccount.id.in_(target_account_ids) if target_account_ids else True
        )
        row = await self.db.execute(
            select(GroupAccountMembership.account_id)
            .join(TelegramAccount, TelegramAccount.id == GroupAccountMembership.account_id)
            .join(AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id)
            .where(
                GroupAccountMembership.group_id == group_id,
                target_account_filter,
                GroupAccountMembership.status == "joined",
                GroupAccountMembership.account_id != source_account_id,
                TelegramAccount.is_active,
                TelegramAccount.account_type == AccountType.PROMOTER,
                TelegramAccount.status.notin_([AccountStatus.ERROR, AccountStatus.BANNED]),
                TelegramAccount.risk_level.in_(
                    [AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value]
                ),
                AccountOperationConfig.enabled,
                AccountOperationConfig.auto_ads_enabled,
                or_(
                    TelegramAccount.risk_pause_until.is_(None),
                    TelegramAccount.risk_pause_until <= now,
                ),
            )
            .order_by(TelegramAccount.risk_score.asc(), TelegramAccount.id)
            .limit(1)
        )
        value = row.scalar_one_or_none()
        return int(value) if value is not None else None

    async def _complete_tasks_with_existing_coverage(
        self,
        now: datetime,
        *,
        target_account_ids: set[int] | None = None,
    ) -> int:
        rows = await self.db.execute(
            select(GroupFailoverTask).where(
                GroupFailoverTask.status.notin_(FAILOVER_TERMINAL_STATUSES)
            )
        )
        updated = 0
        for task in rows.scalars().all():
            account_id = await self._healthy_joined_account_id(
                task.group_id,
                task.source_account_id,
                now,
                target_account_ids=target_account_ids,
            )
            if account_id is None:
                continue
            await self._copy_source_ad_bindings(task.source_account_id, account_id)
            task.target_account_id = account_id
            task.status = GroupFailoverStatus.SUCCEEDED.value
            task.reason = "existing_membership_covered"
            task.error = None
            task.completed_at = now
            task.updated_at = now
            updated += 1
        if updated:
            await self.db.commit()
        return updated

    async def _recover_stale_claims(self, now: datetime) -> int:
        cutoff = now - timedelta(minutes=FAILOVER_JOINING_STALE_MINUTES)
        recovered = await self.db.execute(
            update(GroupFailoverTask)
            .where(
                GroupFailoverTask.status == GroupFailoverStatus.JOINING.value,
                or_(
                    GroupFailoverTask.last_attempt_at.is_(None),
                    GroupFailoverTask.last_attempt_at <= cutoff,
                ),
            )
            .values(
                status=GroupFailoverStatus.RETRY.value,
                reason="stale_claim_recovered",
                target_account_id=None,
                next_retry_at=now,
                updated_at=now,
            )
        )
        count = int(recovered.rowcount or 0)
        if count:
            await self.db.commit()
        return count

    async def _due_task_count(self, now: datetime) -> int:
        value = await self.db.execute(
            select(func.count(GroupFailoverTask.id)).where(
                GroupFailoverTask.status.in_(FAILOVER_DUE_STATUSES),
                or_(
                    GroupFailoverTask.next_retry_at.is_(None),
                    GroupFailoverTask.next_retry_at <= now,
                ),
            )
        )
        return int(value.scalar() or 0)

    async def _list_due_tasks(self, now: datetime, limit: int) -> list[GroupFailoverTask]:
        rows = await self.db.execute(
            select(GroupFailoverTask)
            .join(Group, Group.id == GroupFailoverTask.group_id)
            .options(
                selectinload(GroupFailoverTask.group),
                selectinload(GroupFailoverTask.source_membership),
            )
            .where(
                GroupFailoverTask.status.in_(FAILOVER_DUE_STATUSES),
                or_(
                    GroupFailoverTask.next_retry_at.is_(None),
                    GroupFailoverTask.next_retry_at <= now,
                ),
            )
            .order_by(Group.level_score.desc(), GroupFailoverTask.created_at, GroupFailoverTask.id)
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def _resolve_target_account(
        self,
        task: GroupFailoverTask,
        now: datetime,
        *,
        target_account_ids: set[int] | None = None,
    ) -> TelegramAccount | None:
        target_account_filter = (
            TelegramAccount.id.in_(target_account_ids) if target_account_ids else True
        )
        if task.target_account_id is not None and (
            target_account_ids is None or task.target_account_id in target_account_ids
        ):
            account = await self.db.get(TelegramAccount, task.target_account_id)
            if await self._account_is_eligible(account, task.group_id, now, allow_existing=True):
                return account
            task.target_account_id = None
            await self.db.commit()
        elif task.target_account_id is not None:
            task.target_account_id = None
            await self.db.commit()

        joined_counts = (
            select(
                GroupAccountMembership.account_id.label("account_id"),
                func.count(GroupAccountMembership.id).label("joined_count"),
            )
            .where(GroupAccountMembership.status.in_(["joined", "pending"]))
            .group_by(GroupAccountMembership.account_id)
            .subquery()
        )
        daily_attempts = (
            select(
                AutoJoinAttempt.account_id.label("account_id"),
                func.count(AutoJoinAttempt.id).label("daily_count"),
            )
            .where(AutoJoinAttempt.attempted_at >= _day_start(now))
            .group_by(AutoJoinAttempt.account_id)
            .subquery()
        )
        active_assignments = (
            select(
                GroupFailoverTask.target_account_id.label("account_id"),
                func.count(GroupFailoverTask.id).label("assignment_count"),
            )
            .where(
                GroupFailoverTask.target_account_id.isnot(None),
                GroupFailoverTask.status.in_(
                    [GroupFailoverStatus.JOINING.value, GroupFailoverStatus.RETRY.value]
                ),
            )
            .group_by(GroupFailoverTask.target_account_id)
            .subquery()
        )
        existing_membership = (
            select(GroupAccountMembership.id)
            .where(
                GroupAccountMembership.group_id == task.group_id,
                GroupAccountMembership.account_id == TelegramAccount.id,
                GroupAccountMembership.status.in_(["joined", "pending", "banned", "rejected"]),
            )
            .exists()
        )
        joined_count = func.coalesce(joined_counts.c.joined_count, 0)
        daily_count = func.coalesce(daily_attempts.c.daily_count, 0)
        assignment_count = func.coalesce(active_assignments.c.assignment_count, 0)
        rows = await self.db.execute(
            select(TelegramAccount)
            .join(AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id)
            .outerjoin(joined_counts, joined_counts.c.account_id == TelegramAccount.id)
            .outerjoin(daily_attempts, daily_attempts.c.account_id == TelegramAccount.id)
            .outerjoin(active_assignments, active_assignments.c.account_id == TelegramAccount.id)
            .where(
                TelegramAccount.id != task.source_account_id,
                target_account_filter,
                TelegramAccount.account_type == AccountType.PROMOTER,
                TelegramAccount.is_active,
                TelegramAccount.status.notin_([AccountStatus.ERROR, AccountStatus.BANNED]),
                TelegramAccount.risk_level.in_(
                    [AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value]
                ),
                or_(
                    TelegramAccount.risk_pause_until.is_(None),
                    TelegramAccount.risk_pause_until <= now,
                ),
                AccountOperationConfig.enabled,
                AccountOperationConfig.auto_join_enabled,
                AccountOperationConfig.auto_ads_enabled,
                AccountOperationConfig.operation_mode != AccountOperationMode.AD_ONLY.value,
                or_(
                    AccountOperationConfig.next_join_after.is_(None),
                    AccountOperationConfig.next_join_after <= now,
                ),
                joined_count + assignment_count < AccountOperationConfig.max_groups_total,
                daily_count < AccountOperationConfig.max_groups_per_day,
                ~existing_membership,
            )
            .order_by(
                (joined_count + assignment_count).asc(),
                daily_count.asc(),
                TelegramAccount.risk_score.asc(),
                TelegramAccount.last_active_at.desc().nullslast(),
                TelegramAccount.id,
            )
            .limit(1)
        )
        return rows.scalar_one_or_none()

    async def _account_is_eligible(
        self,
        account: TelegramAccount | None,
        group_id: int,
        now: datetime,
        *,
        allow_existing: bool,
    ) -> bool:
        if account is None or not account.is_active:
            return False
        if account.account_type != AccountType.PROMOTER:
            return False
        if account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
            return False
        if account.risk_level not in {AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value}:
            return False
        if account.risk_pause_until and account.risk_pause_until > now:
            return False
        config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == account.id
                )
            )
        ).scalar_one_or_none()
        if (
            not config
            or not config.enabled
            or not config.auto_join_enabled
            or not config.auto_ads_enabled
            or (getattr(config, "operation_mode", None) or AccountOperationMode.GROWTH.value)
            == AccountOperationMode.AD_ONLY.value
        ):
            return False
        if config.next_join_after and config.next_join_after > now:
            return False
        membership = (
            await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.group_id == group_id,
                    GroupAccountMembership.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        return allow_existing or membership is None

    async def _claim_task(
        self, task: GroupFailoverTask, target_account_id: int, now: datetime
    ) -> bool:
        claimed = await self.db.execute(
            update(GroupFailoverTask)
            .where(
                GroupFailoverTask.id == task.id,
                GroupFailoverTask.status == task.status,
            )
            .values(
                target_account_id=target_account_id,
                status=GroupFailoverStatus.JOINING.value,
                reason="target_assigned",
                error=None,
                attempt_count=int(task.attempt_count or 0) + 1,
                last_attempt_at=now,
                next_retry_at=None,
                updated_at=now,
            )
        )
        await self.db.commit()
        return bool(claimed.rowcount == 1)

    async def _execute_claimed_task(self, task_id: int, now: datetime) -> dict[str, Any]:
        task = (
            await self.db.execute(
                select(GroupFailoverTask)
                .options(
                    selectinload(GroupFailoverTask.group),
                    selectinload(GroupFailoverTask.source_membership),
                )
                .where(GroupFailoverTask.id == task_id)
            )
        ).scalar_one()
        group = task.group
        target_account_id = task.target_account_id
        if group is None or target_account_id is None:
            raise RuntimeError("claimed failover task lost group or target account")

        membership = (
            await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.group_id == group.id,
                    GroupAccountMembership.account_id == target_account_id,
                )
            )
        ).scalar_one_or_none()
        discovered = self.automation._group_to_discovered(group)
        if membership is None or membership.status not in {"joined", "pending"}:
            if not group.username:
                await self._mark_terminal(
                    task,
                    GroupFailoverStatus.MANUAL_REQUIRED,
                    "public_username_required",
                    now,
                )
                return self._task_detail(task)
            await self.automation._join_group(target_account_id, discovered)

        audit = await self.automation._evaluate_joined_group(target_account_id, group)
        if not audit.passed:
            return await self._handle_failed_audit(task, group, discovered, audit, now)

        note = self.automation._format_join_audit_note(audit)
        note = self.automation._append_membership_note(
            note,
            {
                "event": "account_failover_succeeded",
                "source_account_id": task.source_account_id,
                "failover_task_id": task.id,
            },
        )
        membership = await self.automation._upsert_account_membership(
            group,
            target_account_id,
            status="joined",
            join_method="account_failover",
            source_keyword=group.source_keyword,
            note=note,
        )
        await self.automation._record_join_attempt(
            target_account_id,
            discovered,
            DeliveryStatus.SUCCESS,
            db_group=group,
            source_keyword=group.source_keyword,
            reason="account_failover",
            joined_at=now,
        )
        await self.automation._sync_group_ad_policy_from_audit(group, audit)
        if audit.ad_allowed is False:
            await self.automation._apply_join_audit_ad_rule_decision(group, membership, audit)
        else:
            await self.automation.group_manager.update_group(group.id, status="active")
        await self._copy_source_ad_bindings(task.source_account_id, target_account_id)
        config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == target_account_id
                )
            )
        ).scalar_one_or_none()
        if config is not None:
            self.automation._schedule_next_join(config)
        task.status = GroupFailoverStatus.SUCCEEDED.value
        task.reason = "recovered_and_audited"
        task.error = None
        task.next_retry_at = None
        task.completed_at = now
        task.updated_at = now
        await self.db.commit()
        return self._task_detail(task)

    async def _handle_failed_audit(
        self,
        task: GroupFailoverTask,
        group: Group,
        discovered: Any,
        audit: JoinedGroupAuditResult,
        now: datetime,
    ) -> dict[str, Any]:
        leave_error = (
            await self.automation._leave_group(task.target_account_id, discovered)
            if audit.should_leave
            else None
        )
        membership_status = self.automation._membership_status_after_failed_audit(
            audit,
            leave_error=leave_error,
        )
        await self.automation._upsert_account_membership(
            group,
            int(task.target_account_id),
            status=membership_status,
            join_method="account_failover",
            source_keyword=group.source_keyword,
            note=self.automation._format_join_audit_note(audit, leave_error=leave_error),
        )
        await self.automation._record_join_attempt(
            int(task.target_account_id),
            discovered,
            DeliveryStatus.PENDING if membership_status == "pending" else DeliveryStatus.SKIPPED,
            db_group=group,
            source_keyword=group.source_keyword,
            reason=audit.reason or "failover_join_audit_failed",
            error=leave_error,
            joined_at=now,
        )
        if membership_status == "pending":
            if audit.reason == "verification_manual_required":
                await self._mark_terminal(
                    task,
                    GroupFailoverStatus.MANUAL_REQUIRED,
                    audit.reason,
                    now,
                    error=leave_error,
                )
            else:
                await self._schedule_retry(
                    task.id,
                    audit.reason or "verification_pending",
                    leave_error,
                    now,
                    keep_target=True,
                )
        elif audit.reason in {
            "account_banned",
            "group_membership_banned",
            "account_not_participant",
        }:
            await self._schedule_retry(
                task.id,
                audit.reason or "target_account_rejected",
                leave_error,
                now,
                keep_target=False,
            )
        else:
            await self.automation._reject_group_after_failed_audit(group, audit.reason)
            await self._mark_terminal(
                task,
                GroupFailoverStatus.FAILED,
                audit.reason or "join_audit_failed",
                now,
                error=leave_error,
            )
        refreshed = await self.db.get(GroupFailoverTask, task.id)
        return self._task_detail(refreshed or task)

    async def _copy_source_ad_bindings(self, source_account_id: int, target_account_id: int) -> int:
        source_rows = await self.db.execute(
            select(AccountAdBinding).where(
                AccountAdBinding.account_id == source_account_id,
                AccountAdBinding.enabled,
            )
        )
        copied = 0
        for source in source_rows.scalars().all():
            existing = (
                await self.db.execute(
                    select(AccountAdBinding).where(
                        AccountAdBinding.account_id == target_account_id,
                        AccountAdBinding.ad_campaign_id == source.ad_campaign_id,
                        AccountAdBinding.creative_id == source.creative_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if not existing.enabled:
                    existing.enabled = True
                    copied += 1
                continue
            self.db.add(
                AccountAdBinding(
                    account_id=target_account_id,
                    ad_campaign_id=source.ad_campaign_id,
                    creative_id=source.creative_id,
                    enabled=True,
                    priority=source.priority,
                )
            )
            copied += 1
        return copied

    async def _defer_without_attempt(
        self, task: GroupFailoverTask, reason: str, now: datetime
    ) -> None:
        task.reason = reason
        task.next_retry_at = now + timedelta(minutes=30)
        task.updated_at = now
        await self.db.commit()

    async def _schedule_retry(
        self,
        task_id: int,
        reason: str,
        error: str | None,
        now: datetime,
        *,
        keep_target: bool = True,
    ) -> None:
        task = await self.db.get(GroupFailoverTask, task_id)
        if task is None:
            return
        if int(task.attempt_count or 0) >= FAILOVER_MAX_ATTEMPTS:
            await self._mark_terminal(
                task,
                GroupFailoverStatus.FAILED,
                "max_attempts_exceeded",
                now,
                error=error or reason,
            )
            return
        delay_minutes = min(
            6 * 60,
            FAILOVER_RETRY_BASE_MINUTES * (2 ** max(0, int(task.attempt_count or 1) - 1)),
        )
        task.status = GroupFailoverStatus.RETRY.value
        task.reason = reason[:255]
        task.error = error[:2000] if error else None
        task.next_retry_at = now + timedelta(minutes=delay_minutes)
        task.updated_at = now
        if not keep_target:
            task.target_account_id = None
        await self.db.commit()

    async def _mark_terminal(
        self,
        task: GroupFailoverTask,
        status: GroupFailoverStatus,
        reason: str,
        now: datetime,
        *,
        error: str | None = None,
    ) -> None:
        task.status = status.value
        task.reason = reason[:255]
        task.error = error[:2000] if error else None
        task.next_retry_at = None
        task.completed_at = now
        task.updated_at = now
        await self.db.commit()

    @staticmethod
    def _task_detail(task: GroupFailoverTask) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "source_account_id": task.source_account_id,
            "target_account_id": task.target_account_id,
            "group_id": task.group_id,
            "telegram_group_id": task.telegram_group_id,
            "status": task.status,
            "reason": task.reason,
            "attempt_count": task.attempt_count,
        }


async def run_group_failover_with_db(**kwargs: Any) -> dict[str, Any]:
    """Run group failover from a standalone worker process."""
    from app.core import database as db_module
    from app.modules.acquisition.automation import AcquisitionAutomationService

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        automation = AcquisitionAutomationService(db)
        return await GroupFailoverService(db, automation).run(**kwargs)
