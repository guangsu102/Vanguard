"""Durable, serial Telegram profile updates for ad-only promoter accounts."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountOperationMode,
    AccountProfileUpdateItem,
    AccountProfileUpdateItemStatus,
    AccountProfileUpdateOperation,
    AccountProfileUpdateOperationStatus,
    AccountProfileUpdateQueueLease,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.operation_lease import AccountOperationLeaseBusy
from app.core.account.pool import AccountPool, get_account_pool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionError, TelegramExecutionService
from app.core.config import settings

ACTIVE_OPERATION_STATUSES = {
    AccountProfileUpdateOperationStatus.QUEUED.value,
    AccountProfileUpdateOperationStatus.RUNNING.value,
    AccountProfileUpdateOperationStatus.CANCELLING.value,
}
TERMINAL_OPERATION_STATUSES = {
    AccountProfileUpdateOperationStatus.SUCCEEDED.value,
    AccountProfileUpdateOperationStatus.PARTIAL_FAILED.value,
    AccountProfileUpdateOperationStatus.FAILED.value,
    AccountProfileUpdateOperationStatus.CANCELLED.value,
}
TERMINAL_ITEM_STATUSES = {
    AccountProfileUpdateItemStatus.SUCCEEDED.value,
    AccountProfileUpdateItemStatus.FAILED.value,
    AccountProfileUpdateItemStatus.CANCELLED.value,
    AccountProfileUpdateItemStatus.SKIPPED.value,
}
ELIGIBLE_ACCOUNT_STATUSES = {
    AccountStatus.ONLINE,
    AccountStatus.IDLE,
    AccountStatus.OFFLINE,
}
_QUEUE_LEASE_ID = 1
_QUEUE_LEASE_SECONDS = 900


def normalize_account_ids(account_ids: Iterable[int]) -> list[int]:
    values = [int(account_id) for account_id in account_ids]
    if not 1 <= len(values) <= 2000:
        raise ValueError("account_ids must contain between 1 and 2000 accounts")
    if any(account_id <= 0 for account_id in values):
        raise ValueError("account_ids must be positive")
    if len(set(values)) != len(values):
        raise ValueError("account_ids must be unique")
    return sorted(values)


def normalize_profile_bio(value: str, *, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized and not allow_empty:
        raise ValueError("profile_bio must not be empty")
    if len(normalized) > 70:
        raise ValueError("profile_bio must not exceed 70 characters")
    return normalized


def profile_bio_hash(value: str | None) -> str:
    normalized = normalize_profile_bio(value or "", allow_empty=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def operation_snapshot(account_ids: Iterable[int], profile_bio: str) -> tuple[str, str]:
    values = normalize_account_ids(account_ids)
    normalized_bio = normalize_profile_bio(profile_bio)
    raw = json.dumps(
        {"account_ids": values, "profile_bio": normalized_bio},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def session_is_available(account: TelegramAccount) -> bool:
    if account.session_string:
        return True
    return (Path(settings.TELEGRAM_SESSION_DIR) / f"{account.session_name}.session").exists()


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def account_is_ad_only_eligible(account: TelegramAccount | None) -> bool:
    if account is None or not account.is_active:
        return False
    if _enum_value(account.account_type) != AccountType.PROMOTER.value:
        return False
    if _enum_value(account.status) not in {status.value for status in ELIGIBLE_ACCOUNT_STATUSES}:
        return False
    config = account.operation_config
    return bool(
        config is not None
        and config.operation_mode == AccountOperationMode.AD_ONLY.value
        and session_is_available(account)
    )


def safe_worker_error(exc: BaseException) -> str:
    raw = str(exc or "")
    if isinstance(exc, AccountOperationLeaseBusy):
        return "account operation lease busy"
    if isinstance(exc, TelegramExecutionError) and (
        raw.startswith("risk_guard_blocked:")
        or raw in {"telegram client unavailable", "account operation lease unavailable"}
    ):
        return raw[:240]
    return f"{type(exc).__name__}: profile update failed"[:240]


def _retry_delay_seconds(exc: BaseException, attempts: int) -> int:
    raw = str(exc or "").lower()
    explicit_seconds = getattr(exc, "seconds", None)
    if isinstance(explicit_seconds, int) and explicit_seconds > 0:
        return max(60, min(explicit_seconds, 86_400))
    if "profile_update_daily_budget" in raw:
        return 86_400
    if "profile_update_cooldown" in raw:
        return 3_600
    if raw.startswith("risk_guard_blocked:"):
        return 300
    if isinstance(exc, AccountOperationLeaseBusy):
        return 60
    return min(3_600, 60 * (2 ** max(0, attempts - 1)))


def _error_is_permanent(exc: BaseException) -> bool:
    raw = str(exc or "").lower()
    return any(
        marker in raw
        for marker in (
            "account_no_longer_ad_only_eligible",
            "account_missing",
            "session",
            "authkey",
            "phonebanned",
            "banned",
        )
    )


def _preflight_reason(
    account: TelegramAccount | None,
    item: AccountProfileUpdateItem,
) -> str | None:
    if account is None:
        return "account_missing"
    if not account_is_ad_only_eligible(account):
        return "account_no_longer_ad_only_eligible"
    current_hash = profile_bio_hash(account.profile_bio)
    if current_hash != item.baseline_bio_hash:
        if current_hash == item.desired_bio_hash and account.profile_bio_synced_at is not None:
            return "profile_already_synced"
        return "profile_changed_since_queued"
    return None


async def create_profile_update_operation(
    db: AsyncSession,
    *,
    account_ids: Iterable[int],
    profile_bio: str,
    idempotency_key: str,
    created_by_id: int | None,
    accounts: Iterable[TelegramAccount],
) -> tuple[AccountProfileUpdateOperation, bool]:
    """Create a preflighted batch without doing any Telegram RPC."""
    values = normalize_account_ids(account_ids)
    normalized_bio = normalize_profile_bio(profile_bio)
    snapshot, snapshot_hash = operation_snapshot(values, normalized_bio)
    existing = (
        await db.execute(
            select(AccountProfileUpdateOperation).where(
                AccountProfileUpdateOperation.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.snapshot_hash != snapshot_hash:
            raise ValueError("idempotency_key_conflict")
        return existing, False

    account_by_id = {account.id: account for account in accounts}
    if set(account_by_id) != set(values):
        raise ValueError("account_not_found")
    if any(not account_is_ad_only_eligible(account_by_id[item]) for item in values):
        raise ValueError("account_not_ad_only_eligible")

    operation = AccountProfileUpdateOperation(
        idempotency_key=idempotency_key,
        account_ids_snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        profile_bio=normalized_bio,
        status=AccountProfileUpdateOperationStatus.QUEUED.value,
        total_accounts=len(values),
        created_by_id=created_by_id,
    )
    db.add(operation)
    await db.flush()
    desired_hash = profile_bio_hash(normalized_bio)
    for account_id in values:
        account = account_by_id[account_id]
        db.add(
            AccountProfileUpdateItem(
                operation_id=operation.id,
                account_id=account_id,
                baseline_bio_hash=profile_bio_hash(account.profile_bio),
                desired_bio_hash=desired_hash,
                status=AccountProfileUpdateItemStatus.PENDING.value,
            )
        )
    return operation, True


class AccountProfileUpdateService:
    """Claim, revalidate, and execute at most one profile mutation per tick."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        account_pool: AccountPool | None = None,
        telegram_execution: TelegramExecutionService | None = None,
    ) -> None:
        self.db = db
        self.account_pool = account_pool or get_account_pool()
        self.telegram_execution = telegram_execution or TelegramExecutionService(
            AccountRiskGuard(db)
        )

    async def run_tick(
        self,
        *,
        limit: int = 1,
        stale_after_seconds: int = 300,
    ) -> dict[str, int]:
        """Recover bookkeeping, then perform no more than one external update."""
        del limit  # A caller may not increase the one-real-update safety bound.
        recovered = await self.recover_stale_items(
            stale_after_seconds=stale_after_seconds
        )
        cancelled = await self.apply_cancellations()
        reconciled = await self.reconcile_completed_operations()
        queue_lease_id = await self._acquire_queue_lease(
            lease_seconds=max(_QUEUE_LEASE_SECONDS, int(stale_after_seconds))
        )
        if queue_lease_id is None:
            return {
                "processed": 0,
                "recovered": recovered,
                "cancelled": cancelled,
                "reconciled": reconciled,
                "queue_busy": 1,
            }

        processed = 0
        try:
            claim = await self._claim_next_item(
                lease_seconds=max(90, int(stale_after_seconds))
            )
            if claim is not None:
                await self._execute_claim(*claim, queue_lease_id=queue_lease_id)
                processed = 1
                cancelled += await self.apply_cancellations()
                reconciled += await self.reconcile_completed_operations()
        finally:
            await self._release_queue_lease(queue_lease_id)
        return {
            "processed": processed,
            "recovered": recovered,
            "cancelled": cancelled,
            "reconciled": reconciled,
            "queue_busy": 0,
        }

    async def _acquire_queue_lease(self, *, lease_seconds: int) -> str | None:
        now = datetime.utcnow()
        lease = (
            await self.db.execute(
                select(AccountProfileUpdateQueueLease)
                .where(AccountProfileUpdateQueueLease.id == _QUEUE_LEASE_ID)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if lease is None:
            # Metadata-created test databases have no migration seed row.
            lease = AccountProfileUpdateQueueLease(id=_QUEUE_LEASE_ID)
            self.db.add(lease)
            try:
                await self.db.flush()
            except IntegrityError:
                await self.db.rollback()
                return None
        if lease.lease_id and lease.lease_expires_at and lease.lease_expires_at > now:
            await self.db.rollback()
            return None

        lease_id = uuid.uuid4().hex
        lease.lease_id = lease_id
        lease.lease_expires_at = now + timedelta(seconds=max(90, lease_seconds))
        lease.heartbeat_at = now
        lease.updated_at = now
        await self.db.commit()
        return lease_id

    async def _release_queue_lease(self, lease_id: str) -> None:
        lease = await self.db.get(
            AccountProfileUpdateQueueLease,
            _QUEUE_LEASE_ID,
            with_for_update=True,
        )
        if lease is None or lease.lease_id != lease_id:
            await self.db.rollback()
            return
        now = datetime.utcnow()
        lease.lease_id = None
        lease.lease_expires_at = None
        lease.heartbeat_at = now
        lease.updated_at = now
        await self.db.commit()

    async def _claim_next_item(
        self,
        *,
        lease_seconds: int,
    ) -> tuple[int, str] | None:
        now = datetime.utcnow()
        item = (
            await self.db.execute(
                select(AccountProfileUpdateItem)
                .join(
                    AccountProfileUpdateOperation,
                    AccountProfileUpdateOperation.id
                    == AccountProfileUpdateItem.operation_id,
                )
                .where(
                    AccountProfileUpdateOperation.status.in_(
                        [
                            AccountProfileUpdateOperationStatus.QUEUED.value,
                            AccountProfileUpdateOperationStatus.RUNNING.value,
                        ]
                    ),
                    or_(
                        AccountProfileUpdateItem.status
                        == AccountProfileUpdateItemStatus.PENDING.value,
                        and_(
                            AccountProfileUpdateItem.status
                            == AccountProfileUpdateItemStatus.RETRY_WAIT.value,
                            or_(
                                AccountProfileUpdateItem.next_retry_at.is_(None),
                                AccountProfileUpdateItem.next_retry_at <= now,
                            ),
                        ),
                    ),
                )
                .order_by(
                    AccountProfileUpdateOperation.id.asc(),
                    AccountProfileUpdateItem.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if item is None:
            await self.db.rollback()
            return None
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return None
        if operation.cancel_requested_at is not None:
            await self._finish_locked_cancelled(item, operation, now=now)
            return None
        account = await self._load_account(item.account_id, for_update=True)
        reason = _preflight_reason(account, item)
        if reason == "profile_already_synced":
            await self._finish_locked_succeeded_without_remote(item, operation, now=now)
            return None
        if reason is not None:
            await self._finish_locked_skipped(item, operation, reason=reason, now=now)
            return None

        lease_id = uuid.uuid4().hex
        item.status = AccountProfileUpdateItemStatus.IN_PROGRESS.value
        item.attempts += 1
        item.lease_id = lease_id
        item.lease_expires_at = now + timedelta(seconds=max(90, lease_seconds))
        item.started_at = item.started_at or now
        item.next_retry_at = None
        item.reason_code = None
        item.error_message = None
        item.updated_at = now
        operation.status = AccountProfileUpdateOperationStatus.RUNNING.value
        operation.started_at = operation.started_at or now
        operation.heartbeat_at = now
        operation.updated_at = now
        await self.db.commit()
        return item.id, lease_id

    async def _execute_claim(
        self,
        item_id: int,
        lease_id: str,
        *,
        queue_lease_id: str,
    ) -> None:
        item = await self.db.get(AccountProfileUpdateItem, item_id)
        if item is None or item.lease_id != lease_id:
            return
        operation = await self.db.get(AccountProfileUpdateOperation, item.operation_id)
        if operation is None:
            return
        if operation.cancel_requested_at is not None:
            await self._finish_cancelled(item_id, lease_id)
            return
        account = await self._load_account(item.account_id)
        reason = _preflight_reason(account, item)
        if reason == "profile_already_synced":
            await self._finish_succeeded_without_remote(item_id, lease_id)
            return
        if reason is not None:
            await self._finish_skipped(item_id, lease_id, reason=reason)
            return

        wrapper = None
        try:
            assert account is not None
            await self.account_pool.add_account_from_db(account)
            wrapper = await self.account_pool.acquire_by_id(
                account.id,
                purpose="account_profile_update",
                require_session=True,
                raise_on_lease_failure=True,
            )
            if wrapper is None:
                raise TelegramExecutionError("account operation lease unavailable")
            profile_bio = await self._begin_remote_attempt(
                item_id,
                lease_id,
                queue_lease_id=queue_lease_id,
            )
            if profile_bio is None:
                return
            updated = await self.telegram_execution.update_profile_bio(
                wrapper,
                profile_bio,
                source="account_profile_update_batch",
            )
            if not updated:
                raise TelegramExecutionError("telegram client unavailable")
            await self._finish_success(item_id, lease_id)
        except Exception as exc:
            await self._finish_error(item_id, lease_id, exc)
        finally:
            if wrapper is not None:
                await self.account_pool.release(wrapper)

    async def _begin_remote_attempt(
        self,
        item_id: int,
        lease_id: str,
        *,
        queue_lease_id: str,
    ) -> str | None:
        """Repeat all mutable preconditions immediately before the Telegram RPC."""
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return None
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return None
        if operation.cancel_requested_at is not None:
            await self._finish_locked_cancelled(item, operation, now=now)
            return None
        account = await self._load_account(item.account_id, for_update=True)
        reason = _preflight_reason(account, item)
        if reason == "profile_already_synced":
            await self._finish_locked_succeeded_without_remote(item, operation, now=now)
            return None
        if reason is not None:
            await self._finish_locked_skipped(item, operation, reason=reason, now=now)
            return None
        queue_lease = await self.db.get(
            AccountProfileUpdateQueueLease,
            _QUEUE_LEASE_ID,
            with_for_update=True,
        )
        if (
            queue_lease is None
            or queue_lease.lease_id != queue_lease_id
            or queue_lease.lease_expires_at is None
            or queue_lease.lease_expires_at <= now
        ):
            await self._finish_locked_retry(
                item,
                operation,
                reason="queue_lease_lost",
                error_message="profile update queue lease lost",
                delay_seconds=60,
                now=now,
            )
            return None
        queue_lease.lease_expires_at = now + timedelta(seconds=_QUEUE_LEASE_SECONDS)
        queue_lease.heartbeat_at = now
        queue_lease.updated_at = now
        item.remote_attempted_at = now
        item.updated_at = now
        operation.heartbeat_at = now
        operation.updated_at = now
        await self.db.commit()
        return operation.profile_bio

    async def _finish_success(self, item_id: int, lease_id: str) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return
        account = await self._load_account(item.account_id, for_update=True)
        item.status = AccountProfileUpdateItemStatus.SUCCEEDED.value
        item.reason_code = "profile_updated"
        item.error_message = None
        item.next_retry_at = None
        item.lease_id = None
        item.lease_expires_at = None
        item.finished_at = now
        item.updated_at = now
        if account is not None:
            # Do not overwrite a local edit made after the final preflight.
            current_hash = profile_bio_hash(account.profile_bio)
            if current_hash in {item.baseline_bio_hash, item.desired_bio_hash}:
                account.profile_bio = operation.profile_bio
                account.profile_bio_synced_at = now
                account.last_connected_at = now
                if _enum_value(account.status) in {
                    AccountStatus.ONLINE.value,
                    AccountStatus.IDLE.value,
                    AccountStatus.OFFLINE.value,
                }:
                    account.status = AccountStatus.ONLINE
            else:
                item.reason_code = "profile_updated_local_changed_after_dispatch"
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _finish_error(
        self,
        item_id: int,
        lease_id: str,
        exc: BaseException,
        *,
        force_permanent: bool = False,
    ) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return
        summary = safe_worker_error(exc)
        should_retry = bool(
            operation.cancel_requested_at is None
            and not force_permanent
            and not _error_is_permanent(exc)
            and item.attempts < operation.max_attempts
        )
        if should_retry:
            reason = (
                "risk_guard_blocked"
                if summary.startswith("risk_guard_blocked:")
                else "telegram_temporarily_unavailable"
            )
            await self._finish_locked_retry(
                item,
                operation,
                reason=reason,
                error_message=summary,
                delay_seconds=_retry_delay_seconds(exc, item.attempts),
                now=now,
            )
            return
        item.status = AccountProfileUpdateItemStatus.FAILED.value
        item.reason_code = (
            "account_no_longer_ad_only_eligible"
            if force_permanent or _error_is_permanent(exc)
            else "profile_update_failed"
        )
        item.error_message = summary
        item.next_retry_at = None
        item.lease_id = None
        item.lease_expires_at = None
        item.finished_at = now
        item.updated_at = now
        operation.last_error = summary
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _finish_cancelled(self, item_id: int, lease_id: str) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return
        await self._finish_locked_cancelled(item, operation, now=now)

    async def _finish_skipped(self, item_id: int, lease_id: str, *, reason: str) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return
        await self._finish_locked_skipped(item, operation, reason=reason, now=now)

    async def _finish_succeeded_without_remote(self, item_id: int, lease_id: str) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        operation = await self.db.get(
            AccountProfileUpdateOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None:
            await self.db.rollback()
            return
        await self._finish_locked_succeeded_without_remote(item, operation, now=now)

    async def _finish_locked_cancelled(
        self,
        item: AccountProfileUpdateItem,
        operation: AccountProfileUpdateOperation,
        *,
        now: datetime,
    ) -> None:
        item.status = AccountProfileUpdateItemStatus.CANCELLED.value
        item.reason_code = "operation_cancelled"
        item.error_message = None
        item.next_retry_at = None
        item.lease_id = None
        item.lease_expires_at = None
        item.finished_at = now
        item.updated_at = now
        operation.status = AccountProfileUpdateOperationStatus.CANCELLING.value
        await self._recompute_operation(operation.id, now=now)
        await self.db.commit()

    async def _finish_locked_skipped(
        self,
        item: AccountProfileUpdateItem,
        operation: AccountProfileUpdateOperation,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        item.status = AccountProfileUpdateItemStatus.SKIPPED.value
        item.reason_code = reason
        item.error_message = None
        item.next_retry_at = None
        item.lease_id = None
        item.lease_expires_at = None
        item.finished_at = now
        item.updated_at = now
        operation.last_error = reason
        await self._recompute_operation(operation.id, now=now)
        await self.db.commit()

    async def _finish_locked_succeeded_without_remote(
        self,
        item: AccountProfileUpdateItem,
        operation: AccountProfileUpdateOperation,
        *,
        now: datetime,
    ) -> None:
        item.status = AccountProfileUpdateItemStatus.SUCCEEDED.value
        item.reason_code = "profile_already_synced"
        item.error_message = None
        item.next_retry_at = None
        item.lease_id = None
        item.lease_expires_at = None
        item.finished_at = now
        item.updated_at = now
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _finish_locked_retry(
        self,
        item: AccountProfileUpdateItem,
        operation: AccountProfileUpdateOperation,
        *,
        reason: str,
        error_message: str,
        delay_seconds: int,
        now: datetime,
    ) -> None:
        item.status = AccountProfileUpdateItemStatus.RETRY_WAIT.value
        item.reason_code = reason
        item.error_message = error_message[:240]
        item.next_retry_at = now + timedelta(seconds=max(1, delay_seconds))
        item.lease_id = None
        item.lease_expires_at = None
        item.updated_at = now
        operation.last_error = item.error_message
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _locked_claim(
        self,
        item_id: int,
        lease_id: str,
    ) -> AccountProfileUpdateItem | None:
        item = (
            await self.db.execute(
                select(AccountProfileUpdateItem)
                .where(
                    AccountProfileUpdateItem.id == item_id,
                    AccountProfileUpdateItem.lease_id == lease_id,
                    AccountProfileUpdateItem.status
                    == AccountProfileUpdateItemStatus.IN_PROGRESS.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            await self.db.rollback()
        return item

    async def _load_account(
        self,
        account_id: int | None,
        *,
        for_update: bool = False,
    ) -> TelegramAccount | None:
        if account_id is None:
            return None
        statement = (
            select(TelegramAccount)
            .options(selectinload(TelegramAccount.operation_config))
            .where(TelegramAccount.id == account_id)
        )
        if for_update:
            statement = statement.with_for_update(of=TelegramAccount)
        return (await self.db.execute(statement)).scalar_one_or_none()

    async def recover_stale_items(self, *, stale_after_seconds: int = 300) -> int:
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=max(90, int(stale_after_seconds)))
        items = (
            (
                await self.db.execute(
                    select(AccountProfileUpdateItem)
                    .where(
                        AccountProfileUpdateItem.status
                        == AccountProfileUpdateItemStatus.IN_PROGRESS.value,
                        or_(
                            AccountProfileUpdateItem.lease_expires_at <= now,
                            and_(
                                AccountProfileUpdateItem.lease_expires_at.is_(None),
                                AccountProfileUpdateItem.updated_at <= cutoff,
                            ),
                        ),
                    )
                    .order_by(AccountProfileUpdateItem.id.asc())
                    .limit(200)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        affected: set[int] = set()
        for item in items:
            operation = await self.db.get(
                AccountProfileUpdateOperation,
                item.operation_id,
            )
            item.lease_id = None
            item.lease_expires_at = None
            item.updated_at = now
            if operation is None or operation.cancel_requested_at is not None:
                item.status = AccountProfileUpdateItemStatus.CANCELLED.value
                item.reason_code = "operation_cancelled"
                item.error_message = None
                item.next_retry_at = None
                item.finished_at = now
            elif item.attempts >= operation.max_attempts:
                item.status = AccountProfileUpdateItemStatus.FAILED.value
                item.reason_code = "worker_lease_expired"
                item.error_message = "profile update worker lease expired"
                item.next_retry_at = None
                item.finished_at = now
                operation.last_error = item.error_message
            else:
                item.status = AccountProfileUpdateItemStatus.RETRY_WAIT.value
                item.reason_code = "worker_lease_expired"
                item.error_message = "profile update worker lease expired"
                item.next_retry_at = now
            affected.add(item.operation_id)
        for operation_id in affected:
            await self._recompute_operation(operation_id, now=now)
        if items:
            await self.db.commit()
        else:
            await self.db.rollback()
        return len(items)

    async def reconcile_completed_operations(self, *, limit: int = 200) -> int:
        now = datetime.utcnow()
        nonterminal_item_exists = (
            select(AccountProfileUpdateItem.id)
            .where(
                AccountProfileUpdateItem.operation_id
                == AccountProfileUpdateOperation.id,
                AccountProfileUpdateItem.status.not_in(list(TERMINAL_ITEM_STATUSES)),
            )
            .exists()
        )
        operation_ids = (
            (
                await self.db.execute(
                    select(AccountProfileUpdateOperation.id)
                    .where(
                        AccountProfileUpdateOperation.status.in_(
                            list(ACTIVE_OPERATION_STATUSES)
                        ),
                        ~nonterminal_item_exists,
                    )
                    .order_by(AccountProfileUpdateOperation.id.asc())
                    .limit(max(1, min(int(limit), 1000)))
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for operation_id in operation_ids:
            await self._recompute_operation(operation_id, now=now)
        if operation_ids:
            await self.db.commit()
        else:
            await self.db.rollback()
        return len(operation_ids)

    async def apply_cancellations(self) -> int:
        now = datetime.utcnow()
        operations = (
            (
                await self.db.execute(
                    select(AccountProfileUpdateOperation)
                    .where(
                        AccountProfileUpdateOperation.status.in_(
                            list(ACTIVE_OPERATION_STATUSES)
                        ),
                        AccountProfileUpdateOperation.cancel_requested_at.is_not(None),
                    )
                    .order_by(AccountProfileUpdateOperation.id.asc())
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        changed = 0
        for operation in operations:
            operation.status = AccountProfileUpdateOperationStatus.CANCELLING.value
            items = (
                (
                    await self.db.execute(
                        select(AccountProfileUpdateItem)
                        .where(
                            AccountProfileUpdateItem.operation_id == operation.id,
                            AccountProfileUpdateItem.status.in_(
                                [
                                    AccountProfileUpdateItemStatus.PENDING.value,
                                    AccountProfileUpdateItemStatus.RETRY_WAIT.value,
                                ]
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for item in items:
                item.status = AccountProfileUpdateItemStatus.CANCELLED.value
                item.reason_code = "operation_cancelled"
                item.error_message = None
                item.next_retry_at = None
                item.finished_at = now
                item.updated_at = now
                changed += 1
            await self._recompute_operation(operation.id, now=now)
        if operations:
            await self.db.commit()
        else:
            await self.db.rollback()
        return changed

    async def _recompute_operation(
        self,
        operation_id: int,
        *,
        now: datetime,
    ) -> None:
        await self.db.flush()
        operation = await self.db.get(AccountProfileUpdateOperation, operation_id)
        if operation is None:
            return
        rows = (
            await self.db.execute(
                select(AccountProfileUpdateItem.status).where(
                    AccountProfileUpdateItem.operation_id == operation_id
                )
            )
        ).scalars().all()
        statuses = Counter(rows)
        operation.succeeded_accounts = statuses[
            AccountProfileUpdateItemStatus.SUCCEEDED.value
        ]
        operation.failed_accounts = statuses[AccountProfileUpdateItemStatus.FAILED.value]
        operation.cancelled_accounts = statuses[
            AccountProfileUpdateItemStatus.CANCELLED.value
        ]
        operation.skipped_accounts = statuses[AccountProfileUpdateItemStatus.SKIPPED.value]
        operation.processed_accounts = sum(
            statuses[item_status] for item_status in TERMINAL_ITEM_STATUSES
        )
        operation.heartbeat_at = now
        operation.updated_at = now
        if operation.processed_accounts < operation.total_accounts:
            operation.status = (
                AccountProfileUpdateOperationStatus.CANCELLING.value
                if operation.cancel_requested_at is not None
                else AccountProfileUpdateOperationStatus.RUNNING.value
            )
            return
        operation.finished_at = now
        if operation.cancel_requested_at is not None or operation.cancelled_accounts:
            operation.status = AccountProfileUpdateOperationStatus.CANCELLED.value
        elif operation.failed_accounts or operation.skipped_accounts:
            operation.status = (
                AccountProfileUpdateOperationStatus.PARTIAL_FAILED.value
                if operation.succeeded_accounts
                else AccountProfileUpdateOperationStatus.FAILED.value
            )
        else:
            operation.status = AccountProfileUpdateOperationStatus.SUCCEEDED.value


async def run_account_profile_update_tick(
    db: AsyncSession,
    *,
    limit: int = 1,
    stale_after_seconds: int = 300,
    account_pool: AccountPool | None = None,
    telegram_execution: TelegramExecutionService | None = None,
) -> dict[str, int]:
    return await AccountProfileUpdateService(
        db,
        account_pool=account_pool,
        telegram_execution=telegram_execution,
    ).run_tick(
        limit=limit,
        stale_after_seconds=stale_after_seconds,
    )
