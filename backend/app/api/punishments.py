"""
Punishment API Router

RESTful API for recording and querying violations and punishments.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.guardian.models import Violation, ViolationAction


router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class PunishmentRecord(BaseModel):
    """Punishment record request."""
    user_id: int = Field(..., description="User ID")
    group_id: int = Field(..., description="Group ID")
    rule_type: str = Field(..., description="Rule type that was violated")
    action: str = Field(..., description="Action taken: warn/mute/ban/kick")
    content: Optional[str] = Field(None, description="Content that triggered violation")
    duration: Optional[int] = Field(None, description="Duration in seconds for temporary actions")


class PunishmentResponse(BaseModel):
    """Punishment record response."""
    id: int
    user_id: int
    group_id: int
    rule_type: str
    rule_pattern: Optional[str] = None
    content: Optional[str] = None
    action: str
    action_duration: Optional[int] = None
    created_at: str


class PunishmentListResponse(BaseModel):
    """Punishment list response."""
    code: int = 0
    message: str = "success"
    data: list[PunishmentResponse]
    total: int


# =============================================================================
# Punishment Endpoints
# =============================================================================

@router.post("", status_code=201)
async def record_punishment(
    request: PunishmentRecord,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Record a punishment/violation.

    This endpoint is called by the Guardian Bot when a user violates rules.
    """
    try:
        action_enum = ViolationAction(request.action)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action. Must be one of: {[a.value for a in ViolationAction]}"
        )

    violation = Violation(
        user_id=request.user_id,
        group_id=request.group_id,
        rule_type=request.rule_type,
        content=request.content,
        action_taken=action_enum,
        action_duration=request.duration,
    )

    db.add(violation)
    await db.commit()
    await db.refresh(violation)

    return {
        "code": 0,
        "message": "Punishment recorded",
        "data": {
            "id": violation.id,
            "user_id": violation.user_id,
            "group_id": violation.group_id,
            "rule_type": violation.rule_type,
            "action": violation.action_taken.value,
            "action_duration": violation.action_duration,
            "created_at": violation.created_at.isoformat(),
        }
    }


@router.get("/history", response_model=PunishmentListResponse)
async def get_punishment_history(
    user_id: int = Query(..., description="User ID"),
    group_id: Optional[int] = Query(None, description="Group ID"),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> PunishmentListResponse:
    """
    Get user's punishment history.

    Returns a list of violations/punishments for the specified user.
    """
    query = select(Violation).where(Violation.user_id == user_id)
    count_query = select(func.count(Violation.id)).where(Violation.user_id == user_id)

    if group_id is not None:
        query = query.where(Violation.group_id == group_id)
        count_query = count_query.where(Violation.group_id == group_id)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(desc(Violation.created_at)).limit(limit)
    result = await db.execute(query)
    violations = result.scalars().all()

    return PunishmentListResponse(
        data=[
            PunishmentResponse(
                id=v.id,
                user_id=v.user_id,
                group_id=v.group_id,
                rule_type=v.rule_type,
                rule_pattern=v.rule_pattern,
                content=v.content,
                action=v.action_taken.value,
                action_duration=v.action_duration,
                created_at=v.created_at.isoformat(),
            )
            for v in violations
        ],
        total=total,
    )


@router.get("/count")
async def get_punishment_count(
    user_id: int = Query(..., description="User ID"),
    group_id: Optional[int] = Query(None, description="Group ID"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get count of punishments for a user."""
    query = select(func.count(Violation.id)).where(Violation.user_id == user_id)

    if group_id is not None:
        query = query.where(Violation.group_id == group_id)

    result = await db.execute(query)
    count = result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {"count": count}
    }


@router.get("/stats/by-action")
async def get_punishment_stats_by_action(
    group_id: Optional[int] = Query(None, description="Group ID"),
    start_date: Optional[str] = Query(None, description="Start date ISO format"),
    end_date: Optional[str] = Query(None, description="End date ISO format"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get punishment statistics grouped by action type."""
    query = select(
        Violation.action_taken,
        func.count(Violation.id).label("count")
    ).group_by(Violation.action_taken)

    if group_id is not None:
        query = query.where(Violation.group_id == group_id)

    if start_date:
        try:
            start = datetime.fromisoformat(start_date)
            query = query.where(Violation.created_at >= start)
        except ValueError:
            pass

    if end_date:
        try:
            end = datetime.fromisoformat(end_date)
            query = query.where(Violation.created_at <= end)
        except ValueError:
            pass

    result = await db.execute(query)
    stats = {row[0].value: row[1] for row in result.all()}

    return {
        "code": 0,
        "message": "success",
        "data": stats
    }


@router.get("/stats/by-rule-type")
async def get_punishment_stats_by_rule(
    group_id: Optional[int] = Query(None, description="Group ID"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get punishment statistics grouped by rule type."""
    query = select(
        Violation.rule_type,
        func.count(Violation.id).label("count")
    ).group_by(Violation.rule_type)

    if group_id is not None:
        query = query.where(Violation.group_id == group_id)

    result = await db.execute(query)
    stats = {row[0]: row[1] for row in result.all()}

    return {
        "code": 0,
        "message": "success",
        "data": stats
    }
