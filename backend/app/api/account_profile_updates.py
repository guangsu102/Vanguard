"""Administrator API for durable, serial ad-account profile updates."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountProfileUpdateItem,
    AccountProfileUpdateOperation,
    AccountProfileUpdateOperationStatus,
    TelegramAccount,
)
from app.core.database import get_db
from app.core.security import require_admin
from app.modules.account_profile_update.service import (
    ACTIVE_OPERATION_STATUSES,
    TERMINAL_OPERATION_STATUSES,
    account_is_ad_only_eligible,
    create_profile_update_operation,
    normalize_account_ids,
    normalize_profile_bio,
    operation_snapshot,
)

router = APIRouter(prefix="/profile-updates")


class AccountProfileUpdateCreateRequest(BaseModel):
    account_ids: list[int] = Field(..., min_length=1, max_length=2000)
    profile_bio: str = Field(..., min_length=1, max_length=70)

    @field_validator("account_ids")
    @classmethod
    def validate_account_ids(cls, values: list[int]) -> list[int]:
        try:
            return normalize_account_ids(values)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("profile_bio")
    @classmethod
    def validate_profile_bio(cls, value: str) -> str:
        try:
            return normalize_profile_bio(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def serialize_profile_update_item(item: AccountProfileUpdateItem) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "status": item.status,
        "attempts": item.attempts,
        "reason_code": item.reason_code,
        "error_message": item.error_message,
        "next_retry_at": _utc_iso(item.next_retry_at),
        "remote_attempted_at": _utc_iso(item.remote_attempted_at),
        "started_at": _utc_iso(item.started_at),
        "finished_at": _utc_iso(item.finished_at),
    }


def serialize_profile_update_operation(
    operation: AccountProfileUpdateOperation,
    *,
    include_items: bool = True,
) -> dict:
    data = {
        "id": operation.id,
        "status": operation.status,
        "profile_bio": operation.profile_bio,
        "total_accounts": operation.total_accounts,
        "processed_accounts": operation.processed_accounts,
        "succeeded_accounts": operation.succeeded_accounts,
        "failed_accounts": operation.failed_accounts,
        "cancelled_accounts": operation.cancelled_accounts,
        "skipped_accounts": operation.skipped_accounts,
        "max_attempts": operation.max_attempts,
        "last_error": operation.last_error,
        "cancel_requested_at": _utc_iso(operation.cancel_requested_at),
        "heartbeat_at": _utc_iso(operation.heartbeat_at),
        "started_at": _utc_iso(operation.started_at),
        "finished_at": _utc_iso(operation.finished_at),
        "created_at": _utc_iso(operation.created_at),
    }
    if include_items:
        data["items"] = [
            serialize_profile_update_item(item) for item in operation.items
        ]
    return data


async def _load_operation(
    db: AsyncSession,
    operation_id: int,
) -> AccountProfileUpdateOperation | None:
    return (
        await db.execute(
            select(AccountProfileUpdateOperation)
            .options(selectinload(AccountProfileUpdateOperation.items))
            .where(AccountProfileUpdateOperation.id == operation_id)
        )
    ).scalar_one_or_none()


async def _existing_idempotent_operation(
    db: AsyncSession,
    *,
    idempotency_key: str,
    snapshot_hash: str,
) -> AccountProfileUpdateOperation | None:
    operation = (
        await db.execute(
            select(AccountProfileUpdateOperation).where(
                AccountProfileUpdateOperation.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if operation is not None and operation.snapshot_hash != snapshot_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key 已用于不同的简介批次",
        )
    return operation


@router.post("/operations", status_code=status.HTTP_202_ACCEPTED)
async def create_account_profile_update_operation(
    request: AccountProfileUpdateCreateRequest,
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
    snapshot, snapshot_hash = operation_snapshot(request.account_ids, request.profile_bio)
    del snapshot
    existing = await _existing_idempotent_operation(
        db,
        idempotency_key=key,
        snapshot_hash=snapshot_hash,
    )
    if existing is not None:
        persisted = await _load_operation(db, existing.id)
        assert persisted is not None
        return {
            "code": 0,
            "message": "简介批次已存在",
            "data": serialize_profile_update_operation(persisted),
        }

    accounts = (
        await db.execute(
            select(TelegramAccount)
            .options(selectinload(TelegramAccount.operation_config))
            .where(TelegramAccount.id.in_(request.account_ids))
            .with_for_update(of=TelegramAccount)
        )
    ).scalars().all()
    accounts_by_id = {account.id: account for account in accounts}
    if set(accounts_by_id) != set(request.account_ids):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="所选账号中存在已删除账号，请刷新后重试",
        )
    invalid_ids = [
        account_id
        for account_id in request.account_ids
        if not account_is_ad_only_eligible(accounts_by_id[account_id])
    ]
    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "account_not_ad_only_eligible",
                "account_ids": invalid_ids,
                "message": "仅支持具备有效会话的 promoter + ad_only 广告账号",
            },
        )

    overlapping_ids = (
        await db.execute(
            select(AccountProfileUpdateItem.account_id)
            .join(
                AccountProfileUpdateOperation,
                AccountProfileUpdateOperation.id
                == AccountProfileUpdateItem.operation_id,
            )
            .where(
                AccountProfileUpdateItem.account_id.in_(request.account_ids),
                AccountProfileUpdateOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
            )
        )
    ).scalars().all()
    if overlapping_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "account_profile_update_already_queued",
                "account_ids": sorted({int(value) for value in overlapping_ids if value}),
                "message": "所选账号已有未完成的简介批次",
            },
        )

    try:
        operation, created = await create_profile_update_operation(
            db,
            account_ids=request.account_ids,
            profile_bio=request.profile_bio,
            idempotency_key=key,
            created_by_id=int(current_user["id"]),
            accounts=accounts,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        if str(exc) == "idempotency_key_conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key 已用于不同的简介批次",
            ) from exc
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError:
        await db.rollback()
        existing = await _existing_idempotent_operation(
            db,
            idempotency_key=key,
            snapshot_hash=snapshot_hash,
        )
        if existing is None:
            raise
        operation = existing
        created = False

    persisted = await _load_operation(db, operation.id)
    assert persisted is not None
    return {
        "code": 0,
        "message": "广告账号简介批次已排队" if created else "简介批次已存在",
        "data": serialize_profile_update_operation(persisted),
    }


@router.get("/operations/latest")
async def get_latest_account_profile_update_operation(
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    operation = (
        await db.execute(
            select(AccountProfileUpdateOperation)
            .options(selectinload(AccountProfileUpdateOperation.items))
            .order_by(AccountProfileUpdateOperation.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "code": 0,
        "message": "success",
        "data": (
            serialize_profile_update_operation(operation) if operation is not None else None
        ),
    }


@router.get("/operations/{operation_id}")
async def get_account_profile_update_operation(
    operation_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    operation = await _load_operation(db, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="简介批次不存在")
    return {
        "code": 0,
        "message": "success",
        "data": serialize_profile_update_operation(operation),
    }


@router.post("/operations/{operation_id}/cancel")
async def cancel_account_profile_update_operation(
    operation_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    operation = (
        await db.execute(
            select(AccountProfileUpdateOperation)
            .options(selectinload(AccountProfileUpdateOperation.items))
            .where(AccountProfileUpdateOperation.id == operation_id)
            .with_for_update(of=AccountProfileUpdateOperation)
        )
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(status_code=404, detail="简介批次不存在")
    if operation.status not in TERMINAL_OPERATION_STATUSES:
        now = datetime.utcnow()
        operation.cancel_requested_at = operation.cancel_requested_at or now
        operation.status = AccountProfileUpdateOperationStatus.CANCELLING.value
        operation.updated_at = now
        await db.commit()
        operation = await _load_operation(db, operation_id)
        assert operation is not None
    return {
        "code": 0,
        "message": "简介批次已请求取消",
        "data": serialize_profile_update_operation(operation),
    }
