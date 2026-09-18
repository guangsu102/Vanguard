"""Administrator API for official Telegram @SpamBot checks."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountSpamCheckItem,
    AccountSpamCheckOperation,
    AccountType,
    SpamCheckAccountStatus,
    SpamCheckItemStatus,
    SpamCheckOperationStatus,
    TelegramAccount,
)
from app.core.database import get_db
from app.core.security import require_admin
from app.modules.account_spam.service import (
    ACTIVE_OPERATION_STATUSES,
    MANUAL_ELIGIBLE_STATUSES,
    TERMINAL_ITEM_STATUSES,
    TERMINAL_OPERATION_STATUSES,
    create_spam_check_operation,
    normalize_account_ids,
    operation_snapshot,
    session_is_available,
)

router = APIRouter(prefix="/spam-check")


class AccountSpamCheckCreateRequest(BaseModel):
    account_ids: list[int] = Field(..., min_length=1, max_length=2000)

    @field_validator("account_ids")
    @classmethod
    def validate_account_ids(cls, values: list[int]) -> list[int]:
        try:
            return normalize_account_ids(values)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _serialize_item(item: AccountSpamCheckItem) -> dict:
    return {
        "id": item.id,
        "operation_id": item.operation_id,
        "account_id": item.account_id,
        "status": item.status,
        "result": item.result,
        "attempts": item.attempts,
        "reason_code": item.reason_code,
        "response_summary": item.response_summary,
        "next_retry_at": _utc_iso(item.next_retry_at),
        "checked_at": _utc_iso(item.checked_at),
        "finished_at": _utc_iso(item.finished_at),
    }


def serialize_spam_check_operation(
    operation: AccountSpamCheckOperation,
    *,
    include_items: bool = True,
) -> dict:
    data = {
        "id": operation.id,
        "status": operation.status,
        "total_accounts": operation.total_accounts,
        "processed_accounts": operation.processed_accounts,
        "clear_accounts": operation.clear_accounts,
        "restricted_accounts": operation.restricted_accounts,
        "failed_accounts": operation.failed_accounts,
        "cancelled_accounts": operation.cancelled_accounts,
        "last_error": operation.last_error,
        "created_at": _utc_iso(operation.created_at),
        "started_at": _utc_iso(operation.started_at),
        "finished_at": _utc_iso(operation.finished_at),
    }
    if include_items:
        data["items"] = [
            _serialize_item(item)
            for item in sorted(operation.items, key=lambda value: value.id or 0)
        ]
    return data


async def _load_operation(
    db: AsyncSession,
    operation_id: int,
) -> AccountSpamCheckOperation | None:
    return (
        await db.execute(
            select(AccountSpamCheckOperation)
            .options(selectinload(AccountSpamCheckOperation.items))
            .where(AccountSpamCheckOperation.id == operation_id)
        )
    ).scalar_one_or_none()


async def _load_accounts_for_update(
    db: AsyncSession,
    account_ids: list[int],
) -> dict[int, TelegramAccount]:
    accounts: list[TelegramAccount] = []
    for offset in range(0, len(account_ids), 500):
        rows = await db.execute(
            select(TelegramAccount)
            .where(TelegramAccount.id.in_(account_ids[offset : offset + 500]))
            .order_by(TelegramAccount.id)
            .with_for_update(of=TelegramAccount)
        )
        accounts.extend(rows.scalars().unique().all())
    return {account.id: account for account in accounts}


def _invalid_account_reason(account: TelegramAccount | None) -> str | None:
    if account is None:
        return "account_not_found"
    if account.account_type != AccountType.PROMOTER:
        return "promoter_account_required"
    if not account.is_active:
        return "account_inactive"
    if account.status not in MANUAL_ELIGIBLE_STATUSES:
        return "account_status_not_eligible"
    if not session_is_available(account):
        return "valid_session_required"
    return None


@router.post("/operations", status_code=status.HTTP_202_ACCEPTED)
async def create_account_spam_check_operation(
    request: AccountSpamCheckCreateRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key 长度必须为 8 到 128 个字符",
        )
    account_ids = request.account_ids
    _, snapshot_hash = operation_snapshot(account_ids)
    existing = (
        await db.execute(
            select(AccountSpamCheckOperation)
            .options(selectinload(AccountSpamCheckOperation.items))
            .where(AccountSpamCheckOperation.idempotency_key == key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.snapshot_hash != snapshot_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Idempotency-Key 已用于不同账号集合",
                    "operation": serialize_spam_check_operation(existing),
                },
            )
        return {
            "code": 0,
            "message": "SpamBot 检测任务已存在",
            "data": serialize_spam_check_operation(existing),
        }

    account_by_id = await _load_accounts_for_update(db, account_ids)
    invalid_accounts = []
    for account_id in account_ids:
        reason = _invalid_account_reason(account_by_id.get(account_id))
        if reason:
            invalid_accounts.append(
                {"account_id": account_id, "reason_code": reason}
            )
    if invalid_accounts:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "部分账号当前不能执行 SpamBot 检测",
                "accounts": invalid_accounts,
            },
        )

    active_operations = (
        (
            await db.execute(
                select(AccountSpamCheckOperation)
                .options(selectinload(AccountSpamCheckOperation.items))
                .where(
                    AccountSpamCheckOperation.status.in_(
                        list(ACTIVE_OPERATION_STATUSES)
                    )
                )
                .order_by(AccountSpamCheckOperation.id.desc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    selected = set(account_ids)
    conflicts = []
    for operation in active_operations:
        overlap = sorted(
            selected
            & {
                int(item.account_id)
                for item in operation.items
                if item.account_id is not None
            }
        )
        if overlap:
            conflicts.append(
                {
                    "account_ids": overlap,
                    "operation": serialize_spam_check_operation(operation),
                }
            )
    if conflicts:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "部分账号已有进行中的 SpamBot 检测",
                "conflicts": conflicts,
                "operation": conflicts[0]["operation"],
            },
        )

    try:
        operation, _created = await create_spam_check_operation(
            db,
            account_ids=account_ids,
            idempotency_key=key,
            created_by_id=int(current_user["id"]),
            trigger="manual",
        )
    except ValueError as exc:
        await db.rollback()
        if str(exc) == "idempotency_key_conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key 冲突",
            ) from exc
        raise
    persisted = await _load_operation(db, operation.id)
    return {
        "code": 0,
        "message": "SpamBot 检测任务已排队",
        "data": serialize_spam_check_operation(persisted),
    }


@router.get("/operations/latest")
async def get_latest_account_spam_check_operation(
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    operation = (
        await db.execute(
            select(AccountSpamCheckOperation)
            .options(selectinload(AccountSpamCheckOperation.items))
            .order_by(AccountSpamCheckOperation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "code": 0,
        "message": "success",
        "data": (
            serialize_spam_check_operation(operation)
            if operation is not None
            else None
        ),
    }


@router.get("/operations/{operation_id}")
async def get_account_spam_check_operation(
    operation_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    operation = await _load_operation(db, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="SpamBot 检测任务不存在")
    return {
        "code": 0,
        "message": "success",
        "data": serialize_spam_check_operation(operation),
    }


@router.post("/operations/{operation_id}/cancel")
async def cancel_account_spam_check_operation(
    operation_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    operation = (
        await db.execute(
            select(AccountSpamCheckOperation)
            .options(selectinload(AccountSpamCheckOperation.items))
            .where(AccountSpamCheckOperation.id == operation_id)
            .with_for_update(of=AccountSpamCheckOperation)
        )
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(status_code=404, detail="SpamBot 检测任务不存在")
    if operation.status not in TERMINAL_OPERATION_STATUSES:
        now = datetime.utcnow()
        operation.cancel_requested_at = now
        operation.status = SpamCheckOperationStatus.CANCELLING.value
        for item in operation.items:
            if item.status not in {
                SpamCheckItemStatus.PENDING.value,
                SpamCheckItemStatus.RETRY_WAIT.value,
            }:
                continue
            item.status = SpamCheckItemStatus.CANCELLED.value
            item.reason_code = "operation_cancelled"
            item.next_retry_at = None
            item.finished_at = now
            item.updated_at = now
            account = (
                await db.get(TelegramAccount, item.account_id)
                if item.account_id is not None
                else None
            )
            if (
                account is not None
                and account.spam_check_status
                == SpamCheckAccountStatus.QUEUED.value
            ):
                account.spam_check_status = SpamCheckAccountStatus.UNKNOWN.value
        statuses = Counter(item.status for item in operation.items)
        results = Counter(item.result for item in operation.items if item.result)
        operation.clear_accounts = results["clear"]
        operation.restricted_accounts = results["restricted"]
        operation.failed_accounts = statuses[SpamCheckItemStatus.FAILED.value]
        operation.cancelled_accounts = statuses[SpamCheckItemStatus.CANCELLED.value]
        operation.processed_accounts = sum(
            statuses[item_status] for item_status in TERMINAL_ITEM_STATUSES
        )
        operation.updated_at = now
        if operation.processed_accounts == operation.total_accounts:
            operation.status = SpamCheckOperationStatus.CANCELLED.value
            operation.finished_at = now
        await db.commit()
    persisted = await _load_operation(db, operation.id)
    return {
        "code": 0,
        "message": (
            "SpamBot 检测任务已取消"
            if persisted.status == SpamCheckOperationStatus.CANCELLED.value
            else "已提交取消请求"
        ),
        "data": serialize_spam_check_operation(persisted),
    }
