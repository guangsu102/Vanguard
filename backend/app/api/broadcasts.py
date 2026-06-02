"""
Broadcast API Router

RESTful API for broadcasting messages to multiple groups.
"""

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Text, DateTime, select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, Base
from app.core.scheduler.tasks import execute_broadcast_record


router = APIRouter()


class BroadcastRecord(Base):
    """Broadcast record model."""
    __tablename__ = "broadcast_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    broadcast_type = Column(String(50), nullable=False)
    target_groups = Column(Text, nullable=False)
    target_group_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class BroadcastCreate(BaseModel):
    """Broadcast creation request."""
    content: str = Field(..., description="Broadcast content")
    target_groups: list[int] = Field(..., description="List of target group IDs")
    broadcast_type: str = Field(default="node_update", description="Broadcast type")


class BroadcastResponse(BaseModel):
    """Broadcast response."""
    id: int
    content: str
    broadcast_type: str
    target_group_count: int
    success_count: int
    failed_count: int
    status: str
    created_at: str
    completed_at: Optional[str] = None


# =============================================================================
# Broadcast Endpoints
# =============================================================================

@router.post("", status_code=201)
async def create_broadcast(
    request: BroadcastCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Create a new broadcast.

    This endpoint creates a broadcast record and queues it for sending.
    """
    target_groups_json = json.dumps(request.target_groups)

    broadcast = BroadcastRecord(
        content=request.content,
        broadcast_type=request.broadcast_type,
        target_groups=target_groups_json,
        target_group_count=len(request.target_groups),
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)

    return {
        "code": 0,
        "message": "Broadcast created",
        "data": {
            "id": broadcast.id,
            "content": broadcast.content,
            "broadcast_type": broadcast.broadcast_type,
            "target_group_count": broadcast.target_group_count,
            "status": broadcast.status,
            "created_at": broadcast.created_at.isoformat(),
        }
    }


@router.get("")
async def list_broadcasts(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    broadcast_type: Optional[str] = Query(None, description="Filter by type"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List broadcast records."""
    query = select(BroadcastRecord)
    count_query = select(func.count(BroadcastRecord.id))

    if broadcast_type:
        query = query.where(BroadcastRecord.broadcast_type == broadcast_type)
        count_query = count_query.where(BroadcastRecord.broadcast_type == broadcast_type)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(desc(BroadcastRecord.created_at)).offset(offset).limit(limit)
    result = await db.execute(query)
    broadcasts = result.scalars().all()

    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": b.id,
                "content": b.content[:100] + "..." if len(b.content) > 100 else b.content,
                "broadcast_type": b.broadcast_type,
                "target_group_count": b.target_group_count,
                "success_count": b.success_count,
                "failed_count": b.failed_count,
                "status": b.status,
                "created_at": b.created_at.isoformat(),
                "completed_at": b.completed_at.isoformat() if b.completed_at else None,
            }
            for b in broadcasts
        ],
        "total": total,
    }


@router.get("/{broadcast_id}")
async def get_broadcast(
    broadcast_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get broadcast details."""
    result = await db.execute(
        select(BroadcastRecord).where(BroadcastRecord.id == broadcast_id)
    )
    broadcast = result.scalar_one_or_none()

    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    return {
        "code": 0,
        "message": "success",
        "data": {
            "id": broadcast.id,
            "content": broadcast.content,
            "broadcast_type": broadcast.broadcast_type,
            "target_groups": json.loads(broadcast.target_groups),
            "target_group_count": broadcast.target_group_count,
            "success_count": broadcast.success_count,
            "failed_count": broadcast.failed_count,
            "status": broadcast.status,
            "created_at": broadcast.created_at.isoformat(),
            "completed_at": broadcast.completed_at.isoformat() if broadcast.completed_at else None,
        }
    }


@router.post("/{broadcast_id}/execute", status_code=status.HTTP_202_ACCEPTED)
async def execute_broadcast(
    broadcast_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Queue a broadcast for worker execution.
    """
    result = await db.execute(
        select(BroadcastRecord).where(BroadcastRecord.id == broadcast_id)
    )
    broadcast = result.scalar_one_or_none()

    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    if broadcast.status in {"queued", "sending"}:
        raise HTTPException(status_code=400, detail="Broadcast already in progress")

    try:
        async_result = execute_broadcast_record.apply_async(args=[broadcast_id], queue="broadcast")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Broadcast execution queue unavailable: {exc}") from exc

    broadcast.status = "queued"
    await db.commit()

    return {
        "code": 0,
        "message": "Broadcast queued for execution",
        "data": {
            "id": broadcast.id,
            "status": broadcast.status,
            "queued": True,
            "task_name": "execute_broadcast_record",
            "task_id": async_result.id,
        }
    }
