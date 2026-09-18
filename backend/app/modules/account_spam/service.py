"""Persistent worker for official Telegram @SpamBot checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountSpamCheckItem,
    AccountSpamCheckOperation,
    AccountStatus,
    AccountType,
    SpamCheckAccountStatus,
    SpamCheckItemStatus,
    SpamCheckOperationStatus,
    TelegramAccount,
)
from app.core.account.pool import (
    AccountPool,
    get_account_pool,
    invalidate_account_in_all_pools,
)
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
)
from app.core.config import settings
from app.modules.account_spam.classifier import (
    classify_spambot_reply,
    summarize_spambot_reply,
)

ACTIVE_OPERATION_STATUSES = {
    SpamCheckOperationStatus.QUEUED.value,
    SpamCheckOperationStatus.RUNNING.value,
    SpamCheckOperationStatus.CANCELLING.value,
}
TERMINAL_OPERATION_STATUSES = {
    SpamCheckOperationStatus.SUCCEEDED.value,
    SpamCheckOperationStatus.PARTIAL_FAILED.value,
    SpamCheckOperationStatus.FAILED.value,
    SpamCheckOperationStatus.CANCELLED.value,
}
TERMINAL_ITEM_STATUSES = {
    SpamCheckItemStatus.SUCCEEDED.value,
    SpamCheckItemStatus.FAILED.value,
    SpamCheckItemStatus.CANCELLED.value,
}
MANUAL_ELIGIBLE_STATUSES = {
    AccountStatus.ONLINE,
    AccountStatus.IDLE,
    AccountStatus.OFFLINE,
    AccountStatus.RESTRICTED,
}
_PERMANENT_ERRORS = (
    "official_spambot_identity_mismatch",
    "account_no_longer_eligible",
    "account_missing",
    "session",
    "authkey",
    "phonebanned",
    "banned",
)


def normalize_account_ids(account_ids: Iterable[int]) -> list[int]:
    values = [int(account_id) for account_id in account_ids]
    if not 1 <= len(values) <= 2000:
        raise ValueError("account_ids must contain between 1 and 2000 accounts")
    if any(account_id <= 0 for account_id in values):
        raise ValueError("account_ids must be positive")
    if len(set(values)) != len(values):
        raise ValueError("account_ids must be unique")
    return sorted(values)


def operation_snapshot(account_ids: Iterable[int]) -> tuple[str, str]:
    values = normalize_account_ids(account_ids)
    raw = json.dumps(values, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def session_is_available(account: TelegramAccount) -> bool:
    if account.session_string:
        return True
    return (Path(settings.TELEGRAM_SESSION_DIR) / f"{account.session_name}.session").exists()


def account_is_eligible(account: TelegramAccount | None) -> bool:
    return bool(
        account is not None
        and account.account_type == AccountType.PROMOTER
        and account.is_active
        and account.status in MANUAL_ELIGIBLE_STATUSES
        and session_is_available(account)
    )


def restorable_account_status(value: str | None) -> AccountStatus:
    """Return a safe runtime status after clearing a SpamBot-only restriction."""

    try:
        status = AccountStatus(str(value or ""))
    except ValueError:
        return AccountStatus.IDLE
    if status in {AccountStatus.OFFLINE, AccountStatus.ONLINE, AccountStatus.IDLE}:
        return status
    return AccountStatus.IDLE


def safe_worker_error(exc: BaseException) -> str:
    """Return a bounded error without preserving arbitrary Telegram payload text."""

    raw = str(exc or "")
    if isinstance(exc, TelegramExecutionError) and (
        raw.startswith("spambot_")
        or raw.startswith("official_spambot_")
        or raw.startswith("risk_guard_blocked:")
        or raw in {"telegram client unavailable", "account operation lease unavailable"}
    ):
        return summarize_spambot_reply(raw, max_length=240)
    return f"{type(exc).__name__}: account spam check failed"[:240]


def error_is_retryable(message: str) -> bool:
    lowered = str(message or "").lower()
    return not any(marker in lowered for marker in _PERMANENT_ERRORS)


async def create_spam_check_operation(
    db: AsyncSession,
    *,
    account_ids: Iterable[int],
    idempotency_key: str,
    created_by_id: int | None,
    trigger: str = "manual",
) -> tuple[AccountSpamCheckOperation, bool]:
    """Persist one batch; return (operation, created). No broker call is needed."""

    values = normalize_account_ids(account_ids)
    snapshot, snapshot_hash = operation_snapshot(values)
    existing = (
        await db.execute(
            select(AccountSpamCheckOperation).where(
                AccountSpamCheckOperation.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.snapshot_hash != snapshot_hash:
            raise ValueError("idempotency_key_conflict")
        return existing, False

    operation = AccountSpamCheckOperation(
        idempotency_key=idempotency_key,
        account_ids_snapshot=snapshot,
        snapshot_hash=snapshot_hash,
        trigger=trigger,
        status=SpamCheckOperationStatus.QUEUED.value,
        total_accounts=len(values),
        created_by_id=created_by_id,
    )
    db.add(operation)
    await db.flush()
    for account_id in values:
        db.add(
            AccountSpamCheckItem(
                operation_id=operation.id,
                account_id=account_id,
                status=SpamCheckItemStatus.PENDING.value,
            )
        )
    accounts = (
        (
            await db.execute(
                select(TelegramAccount).where(TelegramAccount.id.in_(values))
            )
        )
        .scalars()
        .all()
    )
    for account in accounts:
        if account.spam_check_status in {
            SpamCheckAccountStatus.UNKNOWN.value,
            SpamCheckAccountStatus.ERROR.value,
        }:
            account.spam_check_status = SpamCheckAccountStatus.QUEUED.value
    await db.commit()
    await db.refresh(operation)
    return operation, True


async def enqueue_automatic_spam_check(
    db: AsyncSession,
    *,
    account_id: int,
) -> AccountSpamCheckOperation | None:
    """Persist a non-blocking automatic check for a newly usable session."""

    account = await db.get(TelegramAccount, int(account_id))
    if not account_is_eligible(account):
        return None
    active = (
        await db.execute(
            select(AccountSpamCheckItem.id)
            .join(
                AccountSpamCheckOperation,
                AccountSpamCheckOperation.id == AccountSpamCheckItem.operation_id,
            )
            .where(
                AccountSpamCheckItem.account_id == int(account_id),
                AccountSpamCheckOperation.status.in_(
                    list(ACTIVE_OPERATION_STATUSES)
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return None
    operation, _ = await create_spam_check_operation(
        db,
        account_ids=[int(account_id)],
        idempotency_key=f"auto:{int(account_id)}:{uuid.uuid4().hex}",
        created_by_id=None,
        trigger="automatic",
    )
    return operation


class AccountSpamCheckService:
    """Claim and execute official SpamBot checks one account at a time."""

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
        limit: int = 10,
        stale_after_seconds: int = 300,
    ) -> dict[str, int]:
        recovered = await self.recover_stale_items(
            stale_after_seconds=stale_after_seconds
        )
        cancelled = await self.apply_cancellations()
        reconciled = await self.reconcile_completed_operations()
        processed = 0
        for _ in range(max(1, min(int(limit), 100))):
            claim = await self._claim_next_item(
                lease_seconds=max(90, int(stale_after_seconds))
            )
            if claim is None:
                break
            await self._execute_claim(*claim)
            processed += 1
            cancelled += await self.apply_cancellations()
        return {
            "processed": processed,
            "recovered": recovered,
            "cancelled": cancelled,
            "reconciled": reconciled,
        }

    async def _claim_next_item(
        self,
        *,
        lease_seconds: int,
    ) -> tuple[int, str] | None:
        now = datetime.utcnow()
        item = (
            await self.db.execute(
                select(AccountSpamCheckItem)
                .join(
                    AccountSpamCheckOperation,
                    AccountSpamCheckOperation.id
                    == AccountSpamCheckItem.operation_id,
                )
                .where(
                    AccountSpamCheckOperation.status.in_(
                        [
                            SpamCheckOperationStatus.QUEUED.value,
                            SpamCheckOperationStatus.RUNNING.value,
                        ]
                    ),
                    or_(
                        AccountSpamCheckItem.status
                        == SpamCheckItemStatus.PENDING.value,
                        and_(
                            AccountSpamCheckItem.status
                            == SpamCheckItemStatus.RETRY_WAIT.value,
                            or_(
                                AccountSpamCheckItem.next_retry_at.is_(None),
                                AccountSpamCheckItem.next_retry_at <= now,
                            ),
                        ),
                    ),
                )
                .order_by(
                    AccountSpamCheckOperation.id.asc(),
                    AccountSpamCheckItem.id.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if item is None:
            await self.db.rollback()
            return None
        operation = await self.db.get(
            AccountSpamCheckOperation,
            item.operation_id,
            with_for_update=True,
        )
        if operation is None or operation.cancel_requested_at is not None:
            await self.db.rollback()
            return None

        account = (
            await self.db.execute(
                select(TelegramAccount)
                .where(TelegramAccount.id == item.account_id)
                .with_for_update(of=TelegramAccount)
            )
        ).scalar_one_or_none()
        if account is None:
            item.status = SpamCheckItemStatus.FAILED.value
            item.reason_code = "account_missing"
            item.response_summary = "Account no longer exists"
            item.finished_at = now
            item.updated_at = now
            await self._recompute_operation(operation.id, now=now)
            await self.db.commit()
            return None

        lease_id = uuid.uuid4().hex
        item.status = SpamCheckItemStatus.IN_PROGRESS.value
        item.attempts += 1
        item.account_status_before = (
            account.status.value
            if isinstance(account.status, AccountStatus)
            else str(account.status)
        )
        item.lease_id = lease_id
        item.lease_expires_at = now + timedelta(seconds=lease_seconds)
        item.started_at = item.started_at or now
        item.next_retry_at = None
        item.updated_at = now
        account.spam_check_status = SpamCheckAccountStatus.CHECKING.value
        operation.status = SpamCheckOperationStatus.RUNNING.value
        operation.started_at = operation.started_at or now
        operation.heartbeat_at = now
        operation.updated_at = now
        await self.db.commit()
        return item.id, lease_id

    async def _execute_claim(self, item_id: int, lease_id: str) -> None:
        item = await self.db.get(AccountSpamCheckItem, item_id)
        if item is None or item.lease_id != lease_id:
            return
        operation = await self.db.get(
            AccountSpamCheckOperation,
            item.operation_id,
        )
        if operation is None:
            return
        if operation.cancel_requested_at is not None:
            await self._finish_cancelled(item_id, lease_id)
            return
        account = await self.db.get(TelegramAccount, item.account_id)
        if not account_is_eligible(account):
            await self._finish_error(
                item_id,
                lease_id,
                TelegramExecutionError("account_no_longer_eligible"),
                force_permanent=True,
            )
            return

        wrapper = None
        classified_result: str | None = None
        applied_result: str | None = None
        original_status = account.status
        restore_spambot_restriction = (
            original_status == AccountStatus.RESTRICTED
            and account.restriction_source == "spambot"
        )
        try:
            await self.account_pool.add_account_from_db(account)
            wrapper = await self.account_pool.acquire_by_id(
                account.id,
                purpose="account_spam_check",
                require_session=True,
                raise_on_lease_failure=True,
                allow_restricted=True,
            )
            if wrapper is None:
                raise TelegramExecutionError("account operation lease unavailable")
            reply = await self.telegram_execution.check_spambot_status(
                wrapper,
                source="account_spam_check",
            )
            classified_result = classify_spambot_reply(reply)
            summary = summarize_spambot_reply(reply, max_length=240)
            if classified_result == "unknown":
                await self._finish_unrecognized(item_id, lease_id, summary)
            else:
                if classified_result == "restricted":
                    wrapper.release_status_override = AccountStatus.RESTRICTED
                elif classified_result == "flagged":
                    # An advisory is not a clearance: keep an existing
                    # SpamBot restriction in force on release.
                    wrapper.release_status_override = (
                        AccountStatus.RESTRICTED if restore_spambot_restriction else original_status
                    )
                elif restore_spambot_restriction:
                    wrapper.release_status_override = restorable_account_status(
                        account.restriction_previous_status
                    )
                else:
                    wrapper.release_status_override = original_status
                await self._finish_result(
                    item_id,
                    lease_id,
                    classified_result,
                    summary,
                )
                applied_result = classified_result
        except Exception as exc:
            await self._finish_error(item_id, lease_id, exc)
        finally:
            if wrapper is not None:
                await self.account_pool.release(wrapper)
            if applied_result == "restricted" or (
                applied_result == "clear" and restore_spambot_restriction
            ):
                await invalidate_account_in_all_pools(
                    account.id,
                    reason=f"spambot_{applied_result}",
                )

    async def _finish_result(
        self,
        item_id: int,
        lease_id: str,
        result: str,
        summary: str,
    ) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        account = await self.db.get(TelegramAccount, item.account_id)
        if account is None:
            await self._finish_locked_failure(
                item,
                now=now,
                reason_code="account_missing",
                summary="Account no longer exists",
            )
            return

        item.status = SpamCheckItemStatus.SUCCEEDED.value
        item.result = result
        item.reason_code = (
            "spambot_clear"
            if result == "clear"
            else "spambot_restricted"
            if result == "restricted"
            else "spambot_flagged"
        )
        item.response_summary = summary
        item.checked_at = now
        item.finished_at = now
        item.lease_id = None
        item.lease_expires_at = None
        item.updated_at = now
        account.spam_check_status = result
        account.spam_checked_at = now
        account.spam_check_summary = summary
        if result == "restricted":
            if account.status != AccountStatus.RESTRICTED:
                account.restriction_previous_status = (
                    account.status.value
                    if isinstance(account.status, AccountStatus)
                    else str(account.status)
                )
            if account.restriction_source != "telegram_rpc":
                account.restriction_source = "spambot"
                account.restriction_reason = "spambot_restricted"
                account.restriction_detected_at = now
            account.status = AccountStatus.RESTRICTED
            account.spam_restriction_confirmed_at = now
            await AccountRiskGuard(self.db)._disable_account_automation(account.id, now=now)
        elif result == "clear":
            account.spam_restriction_confirmed_at = None
            if (
                account.status == AccountStatus.RESTRICTED
                and account.restriction_source == "spambot"
            ):
                account.status = restorable_account_status(
                    account.restriction_previous_status
                )
                account.restriction_source = None
                account.restriction_reason = None
                account.restriction_detected_at = None
                account.restriction_previous_status = None
        # "flagged" is a phone-number advisory: record it, but leave account
        # status and restriction bookkeeping untouched in both directions.
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _finish_unrecognized(
        self,
        item_id: int,
        lease_id: str,
        summary: str,
    ) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        item.status = SpamCheckItemStatus.FAILED.value
        item.result = None
        item.reason_code = "response_unrecognized"
        item.response_summary = summary
        item.checked_at = now
        item.finished_at = now
        item.lease_id = None
        item.lease_expires_at = None
        item.updated_at = now
        account = await self.db.get(TelegramAccount, item.account_id)
        if account is not None:
            account.spam_check_status = SpamCheckAccountStatus.ERROR.value
            account.spam_checked_at = now
            account.spam_check_summary = summary
        operation = await self.db.get(AccountSpamCheckOperation, item.operation_id)
        if operation is not None:
            operation.last_error = "response_unrecognized"
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
        summary = safe_worker_error(exc)
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        operation = await self.db.get(
            AccountSpamCheckOperation,
            item.operation_id,
            with_for_update=True,
        )
        retryable = not force_permanent and error_is_retryable(summary)
        should_retry = bool(
            operation is not None
            and operation.cancel_requested_at is None
            and retryable
            and item.attempts < operation.max_attempts
        )
        item.result = None
        item.reason_code = (
            "telegram_temporarily_unavailable"
            if should_retry
            else (
                "account_no_longer_eligible"
                if force_permanent
                else "telegram_check_failed"
            )
        )
        item.response_summary = summary
        item.lease_id = None
        item.lease_expires_at = None
        item.updated_at = now
        account = await self.db.get(TelegramAccount, item.account_id)
        if should_retry:
            item.status = SpamCheckItemStatus.RETRY_WAIT.value
            item.next_retry_at = now + timedelta(
                seconds=min(3600, 310 * (2 ** max(0, item.attempts - 1)))
            )
            if account is not None:
                account.spam_check_status = SpamCheckAccountStatus.QUEUED.value
        else:
            item.status = SpamCheckItemStatus.FAILED.value
            item.next_retry_at = None
            item.checked_at = now
            item.finished_at = now
            if account is not None:
                account.spam_check_status = SpamCheckAccountStatus.ERROR.value
                account.spam_checked_at = now
                account.spam_check_summary = summary
        if operation is not None:
            operation.last_error = summary
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _finish_cancelled(self, item_id: int, lease_id: str) -> None:
        now = datetime.utcnow()
        item = await self._locked_claim(item_id, lease_id)
        if item is None:
            return
        item.status = SpamCheckItemStatus.CANCELLED.value
        item.reason_code = "operation_cancelled"
        item.result = None
        item.lease_id = None
        item.lease_expires_at = None
        item.next_retry_at = None
        item.finished_at = now
        item.updated_at = now
        account = await self.db.get(TelegramAccount, item.account_id)
        if (
            account is not None
            and account.spam_check_status
            in {
                SpamCheckAccountStatus.QUEUED.value,
                SpamCheckAccountStatus.CHECKING.value,
            }
        ):
            account.spam_check_status = SpamCheckAccountStatus.UNKNOWN.value
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def _locked_claim(
        self,
        item_id: int,
        lease_id: str,
    ) -> AccountSpamCheckItem | None:
        item = (
            await self.db.execute(
                select(AccountSpamCheckItem)
                .where(
                    AccountSpamCheckItem.id == item_id,
                    AccountSpamCheckItem.lease_id == lease_id,
                    AccountSpamCheckItem.status
                    == SpamCheckItemStatus.IN_PROGRESS.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            await self.db.rollback()
        return item

    async def _finish_locked_failure(
        self,
        item: AccountSpamCheckItem,
        *,
        now: datetime,
        reason_code: str,
        summary: str,
    ) -> None:
        item.status = SpamCheckItemStatus.FAILED.value
        item.reason_code = reason_code
        item.response_summary = summary
        item.lease_id = None
        item.lease_expires_at = None
        item.checked_at = now
        item.finished_at = now
        item.updated_at = now
        await self._recompute_operation(item.operation_id, now=now)
        await self.db.commit()

    async def recover_stale_items(self, *, stale_after_seconds: int = 300) -> int:
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=max(90, int(stale_after_seconds)))
        items = (
            (
                await self.db.execute(
                    select(AccountSpamCheckItem)
                    .where(
                        AccountSpamCheckItem.status
                        == SpamCheckItemStatus.IN_PROGRESS.value,
                        or_(
                            AccountSpamCheckItem.lease_expires_at <= now,
                            and_(
                                AccountSpamCheckItem.lease_expires_at.is_(None),
                                AccountSpamCheckItem.updated_at <= cutoff,
                            ),
                        ),
                    )
                    .order_by(AccountSpamCheckItem.id.asc())
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
                AccountSpamCheckOperation,
                item.operation_id,
            )
            item.lease_id = None
            item.lease_expires_at = None
            item.updated_at = now
            if operation is None or operation.cancel_requested_at is not None:
                item.status = SpamCheckItemStatus.CANCELLED.value
                item.reason_code = "operation_cancelled"
                item.finished_at = now
            elif item.attempts >= operation.max_attempts:
                item.status = SpamCheckItemStatus.FAILED.value
                item.reason_code = "worker_lease_expired"
                item.response_summary = "SpamBot worker lease expired"
                item.checked_at = now
                item.finished_at = now
                operation.last_error = item.response_summary
            else:
                item.status = SpamCheckItemStatus.RETRY_WAIT.value
                item.reason_code = "worker_lease_expired"
                item.next_retry_at = now
            account = await self.db.get(TelegramAccount, item.account_id)
            if account is not None:
                account.spam_check_status = (
                    SpamCheckAccountStatus.ERROR.value
                    if item.status == SpamCheckItemStatus.FAILED.value
                    else SpamCheckAccountStatus.QUEUED.value
                )
            affected.add(item.operation_id)
        for operation_id in affected:
            await self._recompute_operation(operation_id, now=now)
        if items:
            await self.db.commit()
        else:
            await self.db.rollback()
        return len(items)

    async def reconcile_completed_operations(self, *, limit: int = 200) -> int:
        """Finalize active operations whose items are already all terminal."""

        now = datetime.utcnow()
        nonterminal_item_exists = (
            select(AccountSpamCheckItem.id)
            .where(
                AccountSpamCheckItem.operation_id == AccountSpamCheckOperation.id,
                AccountSpamCheckItem.status.not_in(list(TERMINAL_ITEM_STATUSES)),
            )
            .exists()
        )
        operation_ids = (
            (
                await self.db.execute(
                    select(AccountSpamCheckOperation.id)
                    .where(
                        AccountSpamCheckOperation.status.in_(
                            list(ACTIVE_OPERATION_STATUSES)
                        ),
                        ~nonterminal_item_exists,
                    )
                    .order_by(AccountSpamCheckOperation.id.asc())
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
                    select(AccountSpamCheckOperation)
                    .where(
                        AccountSpamCheckOperation.status.in_(
                            list(ACTIVE_OPERATION_STATUSES)
                        ),
                        AccountSpamCheckOperation.cancel_requested_at.is_not(None),
                    )
                    .order_by(AccountSpamCheckOperation.id.asc())
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        changed = 0
        for operation in operations:
            operation.status = SpamCheckOperationStatus.CANCELLING.value
            items = (
                (
                    await self.db.execute(
                        select(AccountSpamCheckItem)
                        .where(
                            AccountSpamCheckItem.operation_id == operation.id,
                            AccountSpamCheckItem.status.in_(
                                [
                                    SpamCheckItemStatus.PENDING.value,
                                    SpamCheckItemStatus.RETRY_WAIT.value,
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
                item.status = SpamCheckItemStatus.CANCELLED.value
                item.reason_code = "operation_cancelled"
                item.next_retry_at = None
                item.finished_at = now
                item.updated_at = now
                account = await self.db.get(TelegramAccount, item.account_id)
                if (
                    account is not None
                    and account.spam_check_status
                    == SpamCheckAccountStatus.QUEUED.value
                ):
                    account.spam_check_status = SpamCheckAccountStatus.UNKNOWN.value
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
        # AsyncSession is configured with autoflush=False. Flush item transitions
        # before aggregating, otherwise each recompute observes the current item
        # in its previous state and the operation remains one item behind forever.
        await self.db.flush()
        operation = await self.db.get(AccountSpamCheckOperation, operation_id)
        if operation is None:
            return
        rows = (
            await self.db.execute(
                select(
                    AccountSpamCheckItem.status,
                    AccountSpamCheckItem.result,
                ).where(AccountSpamCheckItem.operation_id == operation_id)
            )
        ).all()
        statuses = Counter(status for status, _ in rows)
        results = Counter(result for _, result in rows if result)
        operation.clear_accounts = results["clear"]
        operation.restricted_accounts = results["restricted"]
        operation.failed_accounts = statuses[SpamCheckItemStatus.FAILED.value]
        operation.cancelled_accounts = statuses[SpamCheckItemStatus.CANCELLED.value]
        operation.processed_accounts = sum(
            statuses[item_status] for item_status in TERMINAL_ITEM_STATUSES
        )
        operation.heartbeat_at = now
        operation.updated_at = now
        if operation.processed_accounts < operation.total_accounts:
            operation.status = (
                SpamCheckOperationStatus.CANCELLING.value
                if operation.cancel_requested_at is not None
                else SpamCheckOperationStatus.RUNNING.value
            )
            return
        operation.finished_at = now
        if operation.cancel_requested_at is not None or operation.cancelled_accounts:
            operation.status = SpamCheckOperationStatus.CANCELLED.value
        elif operation.failed_accounts and (
            operation.clear_accounts or operation.restricted_accounts
        ):
            operation.status = SpamCheckOperationStatus.PARTIAL_FAILED.value
        elif operation.failed_accounts:
            operation.status = SpamCheckOperationStatus.FAILED.value
        else:
            operation.status = SpamCheckOperationStatus.SUCCEEDED.value


async def run_account_spam_check_tick(
    db: AsyncSession,
    *,
    limit: int = 10,
    stale_after_seconds: int = 300,
    account_pool: AccountPool | None = None,
    telegram_execution: TelegramExecutionService | None = None,
) -> dict[str, int]:
    return await AccountSpamCheckService(
        db,
        account_pool=account_pool,
        telegram_execution=telegram_execution,
    ).run_tick(
        limit=limit,
        stale_after_seconds=stale_after_seconds,
    )
