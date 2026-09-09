"""Read-only, redacted audit views for self-owned group orchestration.

Audit records are intentionally exposed through a separate router.  This keeps
the mutation API small and makes it difficult to accidentally return encrypted
tokens, session material, or bearer invite links when adding new operation
responses.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent
from app.modules.owned_group.security import redact_sensitive_text, redact_snapshot_text

router = APIRouter()

_AUDIT_READER_ROLES = {"admin", "operator", "auditor"}


async def require_owned_group_audit_reader(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Allow the three roles that may inspect operational history."""

    if current_user.get("role") not in _AUDIT_READER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owned group audit access required",
        )
    return current_user


class OwnedGroupAuditEventResponse(BaseModel):
    id: int
    event_type: str
    group_asset_id: int | None = None
    operation_id: int | None = None
    operation_item_id: int | None = None
    resource_type: str | None = None
    resource_id: int | None = None
    actor_id: int | None = None
    before_state: str | None = None
    after_state: str | None = None
    result: str
    reason_code: str | None = None
    correlation_id: str | None = None
    created_at: datetime


class OwnedGroupAuditEventListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[OwnedGroupAuditEventResponse]
    total: int


class OwnedGroupAuditExportResponse(OwnedGroupAuditEventListResponse):
    exported_at: datetime


def _audit_response(event: OwnedGroupAuditEvent) -> OwnedGroupAuditEventResponse:
    """Convert a row to a response while scrubbing all free-form fields."""

    return OwnedGroupAuditEventResponse(
        id=event.id,
        event_type=redact_sensitive_text(event.event_type, max_length=128),
        group_asset_id=event.group_asset_id,
        operation_id=event.operation_id,
        operation_item_id=event.operation_item_id,
        resource_type=redact_sensitive_text(event.resource_type, max_length=32)
        if event.resource_type
        else None,
        resource_id=event.resource_id,
        actor_id=event.actor_id,
        before_state=redact_snapshot_text(event.before_state),
        after_state=redact_snapshot_text(event.after_state),
        result=redact_sensitive_text(event.result, max_length=32),
        reason_code=redact_sensitive_text(event.reason_code, max_length=128)
        if event.reason_code
        else None,
        correlation_id=redact_sensitive_text(event.correlation_id, max_length=128)
        if event.correlation_id
        else None,
        created_at=event.created_at,
    )


async def _query_audit_events(
    *,
    db: AsyncSession,
    asset_id: int | None,
    operation_id: int | None,
    operation_item_id: int | None,
    event_type: str | None,
    result: str | None,
    resource_type: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[OwnedGroupAuditEvent], int]:
    filters = []
    if asset_id is not None:
        filters.append(OwnedGroupAuditEvent.group_asset_id == asset_id)
    if operation_id is not None:
        filters.append(OwnedGroupAuditEvent.operation_id == operation_id)
    if operation_item_id is not None:
        filters.append(OwnedGroupAuditEvent.operation_item_id == operation_item_id)
    if event_type:
        filters.append(OwnedGroupAuditEvent.event_type == event_type.strip()[:64])
    if result:
        filters.append(OwnedGroupAuditEvent.result == result.strip()[:32])
    if resource_type:
        filters.append(OwnedGroupAuditEvent.resource_type == resource_type.strip()[:16])
    if created_after is not None:
        filters.append(OwnedGroupAuditEvent.created_at >= created_after)
    if created_before is not None:
        filters.append(OwnedGroupAuditEvent.created_at <= created_before)

    query = select(OwnedGroupAuditEvent).order_by(desc(OwnedGroupAuditEvent.id))
    count_query = select(func.count(OwnedGroupAuditEvent.id))
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)
    rows = (
        await db.scalars(query.offset(offset).limit(limit))
    ).all()
    total = int((await db.execute(count_query)).scalar() or 0)
    return rows, total


async def _list_audit_events(
    *,
    db: AsyncSession,
    asset_id: int | None,
    operation_id: int | None,
    operation_item_id: int | None,
    event_type: str | None,
    result: str | None,
    resource_type: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    limit: int,
    offset: int,
) -> OwnedGroupAuditEventListResponse:
    if created_after and created_before and created_after > created_before:
        raise HTTPException(status_code=422, detail="created_after must not exceed created_before")
    rows, total = await _query_audit_events(
        db=db,
        asset_id=asset_id,
        operation_id=operation_id,
        operation_item_id=operation_item_id,
        event_type=event_type,
        result=result,
        resource_type=resource_type,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )
    return OwnedGroupAuditEventListResponse(
        data=[_audit_response(event) for event in rows],
        total=total,
    )


@router.get("/audit-events", response_model=OwnedGroupAuditEventListResponse)
async def list_owned_group_audit_events(
    asset_id: int | None = Query(default=None, gt=0),
    operation_id: int | None = Query(default=None, gt=0),
    operation_item_id: int | None = Query(default=None, gt=0),
    event_type: str | None = Query(default=None, max_length=64),
    result: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=16),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupAuditEventListResponse:
    return await _list_audit_events(
        db=db,
        asset_id=asset_id,
        operation_id=operation_id,
        operation_item_id=operation_item_id,
        event_type=event_type,
        result=result,
        resource_type=resource_type,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )


@router.get("/audit-events/export", response_model=OwnedGroupAuditExportResponse)
async def export_owned_group_audit_events(
    asset_id: int | None = Query(default=None, gt=0),
    operation_id: int | None = Query(default=None, gt=0),
    operation_item_id: int | None = Query(default=None, gt=0),
    event_type: str | None = Query(default=None, max_length=64),
    result: str | None = Query(default=None, max_length=32),
    resource_type: str | None = Query(default=None, max_length=16),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupAuditExportResponse:
    """Return a bounded JSON export with the same redaction guarantees."""

    response = await _list_audit_events(
        db=db,
        asset_id=asset_id,
        operation_id=operation_id,
        operation_item_id=operation_item_id,
        event_type=event_type,
        result=result,
        resource_type=resource_type,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )
    return OwnedGroupAuditExportResponse(
        **response.model_dump(),
        exported_at=datetime.utcnow(),
    )


@router.get("/audit-events/{event_id:int}", response_model=OwnedGroupAuditEventResponse)
async def get_owned_group_audit_event(
    event_id: int,
    _current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupAuditEventResponse:
    event = await db.get(OwnedGroupAuditEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Owned group audit event not found")
    return _audit_response(event)


__all__ = ["router"]
