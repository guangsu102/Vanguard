"""Telegram worker status API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.worker_status import TelegramWorkerRole, TelegramWorkerStatus, TelegramWorkerStatusValue

router = APIRouter()


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str = Field(..., min_length=3, max_length=120)
    role: str
    status: str = TelegramWorkerStatusValue.ONLINE.value
    account_id: Optional[int] = None
    bot_profile_id: Optional[int] = None
    last_error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        TelegramWorkerRole(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        TelegramWorkerStatusValue(value)
        return value


class WorkerStatusResponse(BaseModel):
    id: int
    worker_id: str
    role: str
    account_id: Optional[int] = None
    bot_profile_id: Optional[int] = None
    status: str
    last_heartbeat_at: Optional[str] = None
    heartbeat_age_seconds: Optional[int] = None
    is_stale: bool = False
    last_error: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class WorkerStatusListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[WorkerStatusResponse]
    total: int


def _serialize_worker(item: TelegramWorkerStatus) -> WorkerStatusResponse:
    metadata: dict[str, Any] = {}
    if item.metadata_json:
        try:
            metadata = json.loads(item.metadata_json)
        except Exception:
            metadata = {"raw": item.metadata_json}
    heartbeat_age_seconds = None
    is_stale = False
    if item.last_heartbeat_at:
        heartbeat_age_seconds = max(0, int((datetime.utcnow() - item.last_heartbeat_at).total_seconds()))
        is_stale = heartbeat_age_seconds > 90 and item.status in {
            TelegramWorkerStatusValue.ONLINE.value,
            TelegramWorkerStatusValue.STARTING.value,
            TelegramWorkerStatusValue.DEGRADED.value,
        }
    return WorkerStatusResponse(
        id=item.id,
        worker_id=item.worker_id,
        role=item.role,
        account_id=item.account_id,
        bot_profile_id=item.bot_profile_id,
        status=item.status,
        last_heartbeat_at=item.last_heartbeat_at.isoformat() if item.last_heartbeat_at else None,
        heartbeat_age_seconds=heartbeat_age_seconds,
        is_stale=is_stale,
        last_error=item.last_error,
        metadata=metadata,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
    )


@router.get("", response_model=WorkerStatusListResponse)
async def list_workers(
    role: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> WorkerStatusListResponse:
    query = select(TelegramWorkerStatus)
    count_query = select(func.count(TelegramWorkerStatus.id))
    if role:
        try:
            TelegramWorkerRole(role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid role") from exc
        query = query.where(TelegramWorkerStatus.role == role)
        count_query = count_query.where(TelegramWorkerStatus.role == role)
    if status_filter:
        try:
            TelegramWorkerStatusValue(status_filter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid status") from exc
        query = query.where(TelegramWorkerStatus.status == status_filter)
        count_query = count_query.where(TelegramWorkerStatus.status == status_filter)

    rows = await db.execute(query.order_by(desc(TelegramWorkerStatus.last_heartbeat_at)).limit(limit))
    total = (await db.execute(count_query)).scalar() or 0
    return WorkerStatusListResponse(data=[_serialize_worker(item) for item in rows.scalars().all()], total=total)


@router.post("/heartbeat", response_model=WorkerStatusResponse, status_code=status.HTTP_200_OK)
async def worker_heartbeat(request: WorkerHeartbeatRequest, db: AsyncSession = Depends(get_db)) -> WorkerStatusResponse:
    result = await db.execute(select(TelegramWorkerStatus).where(TelegramWorkerStatus.worker_id == request.worker_id))
    worker = result.scalar_one_or_none()
    now = datetime.utcnow()
    metadata_json = json.dumps(request.metadata, ensure_ascii=False, separators=(",", ":")) if request.metadata else None
    if worker is None:
        worker = TelegramWorkerStatus(
            worker_id=request.worker_id,
            role=request.role,
            account_id=request.account_id,
            bot_profile_id=request.bot_profile_id,
            status=request.status,
            last_heartbeat_at=now,
            last_error=request.last_error,
            metadata_json=metadata_json,
        )
        db.add(worker)
        await db.flush()
    else:
        worker.role = request.role
        worker.account_id = request.account_id
        worker.bot_profile_id = request.bot_profile_id
        worker.status = request.status
        worker.last_heartbeat_at = now
        worker.last_error = request.last_error
        worker.metadata_json = metadata_json
    await db.commit()
    await db.refresh(worker)
    return _serialize_worker(worker)
