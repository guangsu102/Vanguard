"""Read-only, multi-account Telegram group resource search."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.account.operation_lease import (
    AccountOperationLeaseHandle,
    AccountOperationLeaseManager,
    AccountOperationLeaseUnavailable,
)
from app.core.account.pool import AccountPool
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard, RiskDecision
from app.modules.acquisition.models import (
    ResourceReviewStatus,
    ResourceSearchAccountTask,
    ResourceSearchResult,
    ResourceSearchRun,
    ResourceSearchStatus,
)
from app.modules.acquisition.search.group_finder import (
    DiscoveredGroup,
    GroupFinder,
    TelegramFloodWaitError,
)

logger = structlog.get_logger()
ACTIVE_SEARCH_STATUSES = {
    ResourceSearchStatus.QUEUED.value,
    ResourceSearchStatus.RUNNING.value,
}
TERMINAL_SEARCH_STATUSES = {
    ResourceSearchStatus.COMPLETED.value,
    ResourceSearchStatus.PARTIAL.value,
    ResourceSearchStatus.FAILED.value,
    ResourceSearchStatus.CANCELLED.value,
}


class ResourceSearchSuperseded(RuntimeError):
    """Raised inside an old worker after a stale run has been requeued."""


@dataclass(frozen=True)
class ResourceObservation:
    account_id: int
    keyword: str
    group: DiscoveredGroup


@dataclass
class AggregatedResource:
    dedupe_key: str
    telegram_group_id: int | None
    title: str
    username: str | None
    invite_link: str | None
    member_count: int
    is_private: bool
    matched_keywords: set[str] = field(default_factory=set)
    account_ids: set[int] = field(default_factory=set)
    discovery_count: int = 0


@dataclass
class AccountSearchOutcome:
    account_id: int
    status: str
    errors: list[str] = field(default_factory=list)
    flood_wait_seconds: int | None = None


def normalize_username(value: str | None) -> str | None:
    normalized = (value or "").strip().lstrip("@").lower()
    return normalized or None


def build_dedupe_key(group: DiscoveredGroup) -> str | None:
    if group.group_id:
        return f"telegram:{int(group.group_id)}"
    username = normalize_username(group.username)
    return f"username:{username}" if username else None


def merge_resource_observations(
    observations: list[ResourceObservation],
) -> list[AggregatedResource]:
    """Merge ID and username-only observations when a username maps to one ID."""

    username_ids: dict[str, set[int]] = {}
    for observation in observations:
        username = normalize_username(observation.group.username)
        if username and observation.group.group_id:
            username_ids.setdefault(username, set()).add(int(observation.group.group_id))

    merged: dict[str, AggregatedResource] = {}
    for observation in observations:
        group = observation.group
        username = normalize_username(group.username)
        group_id = int(group.group_id) if group.group_id else None
        inferred_ids = username_ids.get(username or "", set())
        if group_id is not None:
            key = f"telegram:{group_id}"
        elif username and len(inferred_ids) == 1:
            group_id = next(iter(inferred_ids))
            key = f"telegram:{group_id}"
        elif username:
            key = f"username:{username}"
        else:
            continue
        resource = merged.get(key)
        if resource is None:
            resource = AggregatedResource(
                dedupe_key=key,
                telegram_group_id=group_id,
                title=(group.title or "").strip(),
                username=username,
                invite_link=f"https://t.me/{username}" if username else None,
                member_count=max(0, int(group.member_count or 0)),
                is_private=bool(group.is_private),
            )
            merged[key] = resource
        else:
            title = (group.title or "").strip()
            if len(title) > len(resource.title):
                resource.title = title
            resource.member_count = max(resource.member_count, int(group.member_count or 0))
            if not resource.username and username:
                resource.username = username
                resource.invite_link = f"https://t.me/{username}"
                resource.is_private = False
        resource.matched_keywords.add(observation.keyword)
        resource.account_ids.add(observation.account_id)
        resource.discovery_count += 1
    return sorted(
        merged.values(),
        key=lambda item: (-item.member_count, item.title.casefold(), item.dedupe_key),
    )


class ResourceSearchService:
    """Run searches sequentially with risk checks and incremental commits."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        account_pool: AccountPool | None = None,
        group_finder: GroupFinder | None = None,
        risk_guard: AccountRiskGuard | None = None,
        lease_manager: AccountOperationLeaseManager | None = None,
        lease_ttl_seconds: int = 1800,
        keyword_delay_seconds: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.db = db
        self.account_pool = account_pool or AccountPool()
        self._owns_account_pool = account_pool is None
        self.group_finder = group_finder or GroupFinder(self.account_pool)
        self.risk_guard = risk_guard or AccountRiskGuard(db)
        self.lease_manager = lease_manager or AccountOperationLeaseManager()
        self.lease_ttl_seconds = max(60, int(lease_ttl_seconds))
        self.keyword_delay_seconds = max(0.0, float(keyword_delay_seconds))
        self.sleep = sleep
        self.current_task_id: str | None = None
        self.logger = logger.bind(module="resource_search")

    async def run(self, run_id: int, *, task_id: str | None = None) -> dict:
        self.current_task_id = task_id
        run = (
            await self.db.execute(
                select(ResourceSearchRun)
                .where(ResourceSearchRun.id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            return {"status": "skipped", "reason": "run_not_found", "run_id": run_id}
        if run.status not in ACTIVE_SEARCH_STATUSES:
            return {
                "status": "skipped",
                "reason": "run_not_active",
                "run_id": run_id,
                "run_status": run.status,
            }
        if (
            run.status == ResourceSearchStatus.RUNNING.value
            and run.celery_task_id
            and task_id
            and run.celery_task_id != task_id
        ):
            return {"status": "skipped", "reason": "superseded_task", "run_id": run_id}
        if run.cancel_requested_at:
            await self._mark_run_cancelled(run)
            return {"status": ResourceSearchStatus.CANCELLED.value, "run_id": run_id}

        now = datetime.utcnow()
        run.status = ResourceSearchStatus.RUNNING.value
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        run.updated_at = now
        if task_id:
            run.celery_task_id = task_id
        await self.db.commit()

        try:
            keywords = self._json_list(run.keywords_json, str)
            account_ids = self._json_list(run.account_ids_json, int)
            accounts = await self._load_accounts(account_ids)
            account_by_id = {account.id: account for account in accounts}
            eligible = [account for account in accounts if self._is_account_eligible(account)]
            await self.account_pool.sync_from_db(eligible)

            for account_id in account_ids:
                if await self._cancel_requested(run_id):
                    await self._cancel_pending_tasks(run_id)
                    break
                task = await self._load_account_task(run_id, account_id)
                if task is None or task.status in TERMINAL_SEARCH_STATUSES:
                    continue
                account = account_by_id.get(account_id)
                if account is None or not self._is_account_eligible(account):
                    reason = "账号不存在" if account is None else "账号未启用或不处于在线/空闲状态"
                    await self._finish_account(
                        task,
                        AccountSearchOutcome(
                            account_id, ResourceSearchStatus.FAILED.value, [reason]
                        ),
                    )
                    continue
                try:
                    lease = await self.lease_manager.acquire(
                        account_id,
                        owner=f"resource-search:{run_id}",
                        ttl_seconds=self.lease_ttl_seconds,
                    )
                except AccountOperationLeaseUnavailable as exc:
                    lease = None
                    lease_error = str(exc)
                else:
                    lease_error = "账号正被其他 Telegram 操作占用"
                if lease is None:
                    await self._finish_account(
                        task,
                        AccountSearchOutcome(
                            account_id, ResourceSearchStatus.FAILED.value, [lease_error]
                        ),
                    )
                    continue
                try:
                    outcome = await self._search_account(
                        run_id,
                        account,
                        task,
                        keywords,
                        run.max_results_per_keyword,
                        lease,
                    )
                    await self._finish_account(task, outcome)
                finally:
                    await self.lease_manager.release(lease)

            run = await self.db.get(ResourceSearchRun, run_id)
            if run is None:
                return {"status": "skipped", "reason": "run_deleted", "run_id": run_id}
            if run.cancel_requested_at or run.status == ResourceSearchStatus.CANCELLED.value:
                await self._mark_run_cancelled(run)
                return {"status": ResourceSearchStatus.CANCELLED.value, "run_id": run_id}
            return await self._finalize_run(run)
        except ResourceSearchSuperseded:
            await self.db.rollback()
            return {"status": "skipped", "reason": "superseded_task", "run_id": run_id}
        except Exception as exc:
            await self.db.rollback()
            await self._mark_run_failed(run_id, str(exc))
            self.logger.exception("resource_search_run_failed", run_id=run_id, error=str(exc))
            return {"status": "failed", "run_id": run_id, "error": str(exc)}
        finally:
            if self._owns_account_pool:
                await self.account_pool.close_all()

    async def _search_account(
        self,
        run_id: int,
        account: TelegramAccount,
        task: ResourceSearchAccountTask,
        keywords: list[str],
        limit: int,
        lease: AccountOperationLeaseHandle,
    ) -> AccountSearchOutcome:
        errors = [item for item in (task.error or "").splitlines() if item]
        flood_wait = task.flood_wait_seconds
        task.status = ResourceSearchStatus.RUNNING.value
        task.started_at = task.started_at or datetime.utcnow()
        await self._heartbeat(run_id)

        for index in range(task.keywords_completed, len(keywords)):
            keyword = keywords[index]
            if await self._cancel_requested(run_id):
                return AccountSearchOutcome(
                    account.id, ResourceSearchStatus.CANCELLED.value, errors, flood_wait
                )
            lease_error = await self._refresh_lease(lease)
            if lease_error:
                errors.append(f"{keyword}: {lease_error}")
                break
            decision = await self._reserve_search(account, run_id, keyword, lease)
            if not decision.allowed:
                error = f"{keyword}: 风控阻止 ({decision.reason})"
                errors.append(error)
                await self._persist_keyword(
                    run_id, task, [], succeeded=False, error=error
                )
                break

            observations: list[ResourceObservation] = []
            succeeded = False
            error = None
            try:
                groups = await self.group_finder.search_by_keyword(
                    keyword=keyword,
                    limit=limit,
                    account_id=account.id,
                    raise_errors=True,
                    operation_lease=lease,
                )
                observations = [
                    ResourceObservation(account.id, keyword, group) for group in groups
                ]
                await self.risk_guard.record_success(
                    account,
                    AccountRiskAction.SEARCH,
                    target_type="keyword",
                    target_id=keyword,
                    details={"source": "resource_search", "run_id": run_id},
                )
                succeeded = True
            except TelegramFloodWaitError as exc:
                flood_wait = exc.seconds
                error = f"{keyword}: Telegram 要求等待 {exc.seconds} 秒"
                errors.append(error)
                await self._record_failure(account, run_id, keyword, exc)
            except Exception as exc:
                error = f"{keyword}: {str(exc) or exc.__class__.__name__}"
                errors.append(error)
                await self._record_failure(account, run_id, keyword, exc)
            await self._persist_keyword(
                run_id,
                task,
                observations,
                succeeded=succeeded,
                error=error,
                flood_wait_seconds=flood_wait,
            )
            if flood_wait is not None:
                break
            if index < len(keywords) - 1 and self.keyword_delay_seconds:
                await self.sleep(self.keyword_delay_seconds)

        if await self._cancel_requested(run_id):
            status = ResourceSearchStatus.CANCELLED.value
        elif not errors:
            status = ResourceSearchStatus.COMPLETED.value
        elif task.successful_keywords:
            status = ResourceSearchStatus.PARTIAL.value
        else:
            status = ResourceSearchStatus.FAILED.value
        return AccountSearchOutcome(account.id, status, errors, flood_wait)

    async def _reserve_search(
        self,
        account: TelegramAccount,
        run_id: int,
        keyword: str,
        lease: AccountOperationLeaseHandle,
    ) -> RiskDecision:
        while True:
            decision = await self.risk_guard.check_and_reserve(
                account,
                AccountRiskAction.SEARCH,
                target_type="keyword",
                target_id=keyword,
                details={"source": "resource_search", "run_id": run_id},
            )
            if decision.allowed:
                return decision
            if decision.reason != "search_cooldown" or not decision.retry_after_seconds:
                return decision
            if await self._cancel_requested(run_id):
                return RiskDecision(False, reason="cancel_requested")
            await self._heartbeat(run_id)
            remaining = float(max(1, decision.retry_after_seconds))
            refresh_interval = max(
                10.0,
                min(300.0, self.lease_ttl_seconds / 3),
            )
            while remaining > 0:
                wait_seconds = min(remaining, refresh_interval)
                await self.sleep(wait_seconds)
                remaining -= wait_seconds
                if await self._cancel_requested(run_id):
                    return RiskDecision(False, reason="cancel_requested")
                lease_error = await self._refresh_lease(lease)
                if lease_error:
                    return RiskDecision(False, reason=lease_error)
                await self._heartbeat(run_id)

    async def _refresh_lease(
        self,
        lease: AccountOperationLeaseHandle,
    ) -> str | None:
        try:
            if await self.lease_manager.refresh(lease):
                return None
            return "账号操作锁已丢失"
        except AccountOperationLeaseUnavailable as exc:
            return str(exc) or "账号操作锁不可用"

    async def _record_failure(
        self,
        account: TelegramAccount,
        run_id: int,
        keyword: str,
        exc: Exception,
    ) -> None:
        await self.risk_guard.record_failure(
            account,
            AccountRiskAction.SEARCH,
            exc,
            target_type="keyword",
            target_id=keyword,
            details={"source": "resource_search", "run_id": run_id},
        )

    async def _persist_keyword(
        self,
        run_id: int,
        task: ResourceSearchAccountTask,
        observations: list[ResourceObservation],
        *,
        succeeded: bool,
        error: str | None,
        flood_wait_seconds: int | None = None,
    ) -> None:
        if observations:
            await self._upsert_observations(run_id, observations)
        task.keywords_completed += 1
        task.successful_keywords += int(succeeded)
        task.result_count += len(observations)
        if error:
            current = [item for item in (task.error or "").splitlines() if item]
            if error not in current:
                current.append(error)
            task.error = "\n".join(current)[:4000]
        task.flood_wait_seconds = flood_wait_seconds
        task.updated_at = datetime.utcnow()
        run = await self.db.get(ResourceSearchRun, run_id)
        if run is not None:
            run.raw_result_count += len(observations)
            run.unique_result_count = await self._result_count(run_id)
            run.heartbeat_at = task.updated_at
            run.updated_at = task.updated_at
        await self.db.commit()

    async def _upsert_observations(
        self,
        run_id: int,
        observations: list[ResourceObservation],
    ) -> None:
        for observation in observations:
            await self._upsert_observation(run_id, observation)

    async def _upsert_observation(
        self,
        run_id: int,
        observation: ResourceObservation,
    ) -> None:
        group = observation.group
        group_id = int(group.group_id) if group.group_id else None
        username = normalize_username(group.username)
        if group_id is None and username is None:
            return
        conditions = []
        if group_id is not None:
            conditions.extend(
                [
                    ResourceSearchResult.telegram_group_id == group_id,
                    ResourceSearchResult.dedupe_key == f"telegram:{group_id}",
                ]
            )
            if username:
                conditions.append(
                    (
                        ResourceSearchResult.username == username
                    )
                    & ResourceSearchResult.telegram_group_id.is_(None)
                )
        elif username:
            conditions.extend(
                [
                    ResourceSearchResult.username == username,
                    ResourceSearchResult.dedupe_key == f"username:{username}",
                ]
            )
        rows = (
            (
                await self.db.execute(
                    select(ResourceSearchResult).where(
                        ResourceSearchResult.run_id == run_id,
                        or_(*conditions),
                    )
                )
            )
            .scalars()
            .all()
        )
        if group_id is not None:
            primary = next(
                (row for row in rows if row.telegram_group_id == group_id),
                rows[0] if rows else None,
            )
        else:
            distinct_ids = {
                int(row.telegram_group_id)
                for row in rows
                if row.telegram_group_id is not None
            }
            primary = (
                rows[0]
                if len(distinct_ids) <= 1 and rows
                else next(
                    (row for row in rows if row.telegram_group_id is None),
                    None,
                )
            )
        now = datetime.utcnow()
        if primary is None:
            primary = ResourceSearchResult(
                run_id=run_id,
                dedupe_key=(
                    f"telegram:{group_id}" if group_id is not None else f"username:{username}"
                ),
                telegram_group_id=group_id,
                title=(group.title or "").strip(),
                username=username,
                invite_link=f"https://t.me/{username}" if username else None,
                member_count=max(0, int(group.member_count or 0)),
                is_private=bool(group.is_private),
                matched_keywords_json="[]",
                discovered_by_account_ids_json="[]",
                discovery_count=0,
                review_status=ResourceReviewStatus.PENDING.value,
                first_found_at=now,
                last_found_at=now,
            )
            self.db.add(primary)

        keywords = set(self._json_list(primary.matched_keywords_json, str))
        account_ids = set(self._json_list(primary.discovered_by_account_ids_json, int))
        discovery_count = int(primary.discovery_count or 0)
        for duplicate in rows:
            if duplicate is primary:
                continue
            keywords.update(self._json_list(duplicate.matched_keywords_json, str))
            account_ids.update(
                self._json_list(duplicate.discovered_by_account_ids_json, int)
            )
            discovery_count += int(duplicate.discovery_count or 0)
            if duplicate.review_status != ResourceReviewStatus.PENDING.value:
                primary.review_status = duplicate.review_status
                primary.note = duplicate.note or primary.note
                primary.reviewed_at = duplicate.reviewed_at or primary.reviewed_at
                primary.reviewed_by_id = duplicate.reviewed_by_id or primary.reviewed_by_id
            await self.db.delete(duplicate)

        title = (group.title or "").strip()
        if len(title) > len(primary.title or ""):
            primary.title = title
        if group_id is not None:
            primary.telegram_group_id = group_id
            primary.dedupe_key = f"telegram:{group_id}"
        elif primary.telegram_group_id is None:
            primary.dedupe_key = f"username:{username}"
        if username:
            primary.username = username
            primary.invite_link = f"https://t.me/{username}"
            primary.is_private = False
        primary.member_count = max(
            int(primary.member_count or 0), int(group.member_count or 0)
        )
        keywords.add(observation.keyword)
        account_ids.add(observation.account_id)
        primary.matched_keywords_json = json.dumps(
            sorted(keywords, key=str.casefold), ensure_ascii=False
        )
        primary.discovered_by_account_ids_json = json.dumps(sorted(account_ids))
        primary.discovery_count = discovery_count + 1
        primary.last_found_at = now

    async def _finish_account(
        self,
        task: ResourceSearchAccountTask,
        outcome: AccountSearchOutcome,
    ) -> None:
        now = datetime.utcnow()
        task.status = outcome.status
        task.error = "\n".join(outcome.errors)[:4000] or None
        task.flood_wait_seconds = outcome.flood_wait_seconds
        task.completed_at = now
        task.updated_at = now
        run = await self.db.get(ResourceSearchRun, task.run_id)
        if run is not None:
            run.heartbeat_at = now
            run.updated_at = now
        await self.db.commit()

    async def _finalize_run(self, run: ResourceSearchRun) -> dict:
        tasks = await self._account_tasks(run.id)
        now = datetime.utcnow()
        for task in tasks:
            if task.status not in TERMINAL_SEARCH_STATUSES:
                task.status = ResourceSearchStatus.FAILED.value
                task.error = task.error or "任务未完成"
                task.completed_at = now
        successful = sum(
            task.status
            in {
                ResourceSearchStatus.COMPLETED.value,
                ResourceSearchStatus.PARTIAL.value,
            }
            for task in tasks
        )
        failed = sum(task.status == ResourceSearchStatus.FAILED.value for task in tasks)
        has_partial = any(
            task.status == ResourceSearchStatus.PARTIAL.value for task in tasks
        )
        if successful == 0 and failed:
            final_status = ResourceSearchStatus.FAILED.value
        elif failed or has_partial:
            final_status = ResourceSearchStatus.PARTIAL.value
        else:
            final_status = ResourceSearchStatus.COMPLETED.value
        run.status = final_status
        run.completed_accounts = len(tasks)
        run.successful_accounts = successful
        run.failed_accounts = failed
        run.unique_result_count = await self._result_count(run.id)
        run.error_summary = "\n".join(
            f"账号 {task.account_identifier}: {task.error}"
            for task in tasks
            if task.error
        )[:8000] or None
        run.completed_at = now
        run.heartbeat_at = now
        run.updated_at = now
        await self.db.commit()
        return {
            "status": final_status,
            "run_id": run.id,
            "raw_result_count": run.raw_result_count,
            "unique_result_count": run.unique_result_count,
            "successful_accounts": successful,
            "failed_accounts": failed,
        }

    async def _mark_run_failed(self, run_id: int, error: str) -> None:
        run = await self.db.get(ResourceSearchRun, run_id)
        if run is None:
            return
        now = datetime.utcnow()
        run.status = ResourceSearchStatus.FAILED.value
        run.error_summary = (error or "未知错误")[:8000]
        run.completed_at = now
        run.heartbeat_at = now
        run.updated_at = now
        for task in await self._account_tasks(run_id):
            if task.status in ACTIVE_SEARCH_STATUSES:
                task.status = ResourceSearchStatus.FAILED.value
                task.error = task.error or run.error_summary
                task.completed_at = now
                task.updated_at = now
        await self.db.commit()

    async def _mark_run_cancelled(self, run: ResourceSearchRun) -> None:
        now = datetime.utcnow()
        run.status = ResourceSearchStatus.CANCELLED.value
        run.cancel_requested_at = run.cancel_requested_at or now
        run.completed_at = now
        run.heartbeat_at = now
        run.updated_at = now
        await self._cancel_pending_tasks(run.id, commit=False)
        run.completed_accounts = len(
            [
                task
                for task in await self._account_tasks(run.id)
                if task.status in TERMINAL_SEARCH_STATUSES
            ]
        )
        await self.db.commit()

    async def _cancel_pending_tasks(self, run_id: int, *, commit: bool = True) -> None:
        now = datetime.utcnow()
        for task in await self._account_tasks(run_id):
            if task.status in ACTIVE_SEARCH_STATUSES:
                task.status = ResourceSearchStatus.CANCELLED.value
                task.completed_at = now
                task.updated_at = now
        if commit:
            await self.db.commit()

    async def _heartbeat(self, run_id: int) -> None:
        run = await self.db.get(ResourceSearchRun, run_id)
        if run is not None:
            run.heartbeat_at = datetime.utcnow()
            run.updated_at = run.heartbeat_at
            await self.db.commit()

    async def _cancel_requested(self, run_id: int) -> bool:
        run = await self.db.get(ResourceSearchRun, run_id)
        if run is None:
            return True
        await self.db.refresh(
            run,
            attribute_names=["status", "cancel_requested_at", "celery_task_id"],
        )
        if (
            self.current_task_id
            and run.celery_task_id
            and self.current_task_id != run.celery_task_id
        ):
            raise ResourceSearchSuperseded
        return bool(
            run.cancel_requested_at
            or run.status == ResourceSearchStatus.CANCELLED.value
        )

    async def _load_accounts(self, account_ids: list[int]) -> list[TelegramAccount]:
        if not account_ids:
            return []
        rows = await self.db.execute(
            select(TelegramAccount)
            .options(selectinload(TelegramAccount.static_proxy))
            .where(TelegramAccount.id.in_(account_ids))
        )
        return list(rows.scalars().unique().all())

    async def _load_account_task(
        self,
        run_id: int,
        account_id: int,
    ) -> ResourceSearchAccountTask | None:
        return (
            await self.db.execute(
                select(ResourceSearchAccountTask).where(
                    ResourceSearchAccountTask.run_id == run_id,
                    ResourceSearchAccountTask.account_id == account_id,
                )
            )
        ).scalar_one_or_none()

    async def _account_tasks(self, run_id: int) -> list[ResourceSearchAccountTask]:
        rows = await self.db.execute(
            select(ResourceSearchAccountTask).where(
                ResourceSearchAccountTask.run_id == run_id
            )
        )
        return list(rows.scalars().all())

    async def _result_count(self, run_id: int) -> int:
        return int(
            (
                await self.db.execute(
                    select(func.count(ResourceSearchResult.id)).where(
                        ResourceSearchResult.run_id == run_id
                    )
                )
            ).scalar_one()
            or 0
        )

    @staticmethod
    def _is_account_eligible(account: TelegramAccount) -> bool:
        return bool(
            account.is_active
            and account.account_type == AccountType.PROMOTER
            and account.status in {AccountStatus.ONLINE, AccountStatus.IDLE}
        )

    @staticmethod
    def _json_list(raw: str | None, item_type: type = str) -> list:
        try:
            values = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(values, list):
            return []
        result = []
        for value in values:
            try:
                result.append(item_type(value))
            except (TypeError, ValueError):
                continue
        return result


async def reconcile_stale_resource_search_runs(
    db: AsyncSession,
    *,
    stale_after_seconds: int = 1800,
    limit: int = 50,
) -> dict:
    """Move abandoned queued/running searches back to the dedicated queue."""

    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=max(120, int(stale_after_seconds)))
    runs = (
        (
            await db.execute(
                select(ResourceSearchRun)
                .where(ResourceSearchRun.status.in_(ACTIVE_SEARCH_STATUSES))
                .order_by(ResourceSearchRun.id.asc())
                .limit(max(1, min(int(limit), 200)))
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    requeued = []
    cancelled = 0
    for run in runs:
        if run.cancel_requested_at:
            run.status = ResourceSearchStatus.CANCELLED.value
            run.completed_at = now
            run.updated_at = now
            task_rows = await db.execute(
                select(ResourceSearchAccountTask).where(
                    ResourceSearchAccountTask.run_id == run.id,
                    ResourceSearchAccountTask.status.in_(ACTIVE_SEARCH_STATUSES),
                )
            )
            for task in task_rows.scalars().all():
                task.status = ResourceSearchStatus.CANCELLED.value
                task.completed_at = now
                task.updated_at = now
            cancelled += 1
            continue
        last_progress = run.heartbeat_at or run.updated_at or run.created_at
        if last_progress and last_progress > cutoff:
            continue
        old_task_id = run.celery_task_id
        new_task_id = uuid.uuid4().hex
        run.status = ResourceSearchStatus.QUEUED.value
        run.celery_task_id = new_task_id
        run.heartbeat_at = None
        run.updated_at = now
        task_rows = await db.execute(
            select(ResourceSearchAccountTask).where(
                ResourceSearchAccountTask.run_id == run.id,
                ResourceSearchAccountTask.status == ResourceSearchStatus.RUNNING.value,
            )
        )
        for task in task_rows.scalars().all():
            task.status = ResourceSearchStatus.QUEUED.value
            task.updated_at = now
        requeued.append(
            {"run_id": run.id, "old_task_id": old_task_id, "task_id": new_task_id}
        )
    await db.commit()
    return {"requeued": requeued, "cancelled": cancelled}


async def cleanup_resource_search_history(
    db: AsyncSession,
    *,
    retention_days: int = 90,
) -> dict:
    """Delete terminal resource-search batches after a controlled retention period."""

    cutoff = datetime.utcnow() - timedelta(days=max(7, int(retention_days)))
    result = await db.execute(
        delete(ResourceSearchRun).where(
            ResourceSearchRun.status.in_(TERMINAL_SEARCH_STATUSES),
            ResourceSearchRun.completed_at.is_not(None),
            ResourceSearchRun.completed_at < cutoff,
        )
    )
    await db.commit()
    return {"deleted": int(result.rowcount or 0), "retention_days": retention_days}
