"""
Users API Router

RESTful API for user management with cursor pagination.
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, and_, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.user.models import User, UserState
from app.modules.guardian.models import Violation


router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class UserResponse(BaseModel):
    """User response."""
    id: int
    telegram_id: int
    xboard_user_id: Optional[int] = None
    username: Optional[str] = None
    state: str
    warning_count: int
    muted_until: Optional[str] = None
    trial_started_at: Optional[str] = None
    trial_expires_at: Optional[str] = None
    created_at: str
    updated_at: str


class UserListResponse(BaseModel):
    """User list response with cursor pagination."""
    code: int = 0
    message: str = "success"
    data: list[UserResponse]
    total: int
    next_cursor: Optional[str] = None
    has_more: bool = False


class UserStateUpdate(BaseModel):
    """User state update request."""
    state: str = Field(..., description="User state: new, pending, active, silent, churned, blocked")


class UserWarnRequest(BaseModel):
    """User warn request."""
    reason: Optional[str] = Field(None, description="Warning reason")
    duration_hours: int = Field(default=24, ge=1, le=720, description="Mute duration in hours")


class UserMuteRequest(BaseModel):
    """User mute request."""
    duration_hours: int = Field(..., ge=1, le=720, description="Mute duration in hours")
    reason: Optional[str] = None


class ViolationRecordResponse(BaseModel):
    """Violation record response."""
    id: int
    user_id: int
    group_id: int
    rule_type: str
    rule_pattern: Optional[str]
    content: Optional[str]
    action_taken: str
    action_duration: Optional[int]
    created_at: str


class ViolationListResponse(BaseModel):
    """Violation list response."""
    code: int = 0
    message: str = "success"
    data: list[ViolationRecordResponse]
    total: int


class UserStatsResponse(BaseModel):
    """User statistics response."""
    code: int = 0
    message: str = "success"
    data: dict


class UserFunnelResponse(BaseModel):
    """User funnel response."""
    code: int = 0
    message: str = "success"
    data: dict


# =============================================================================
# Helper Functions
# =============================================================================

def _user_to_response(user: User) -> UserResponse:
    """Convert User model to response."""
    return UserResponse(
        id=user.id,
        telegram_id=user.telegram_id,
        xboard_user_id=user.xboard_user_id,
        username=user.username,
        state=user.state.value,
        warning_count=user.warning_count,
        muted_until=user.muted_until.isoformat() if user.muted_until else None,
        trial_started_at=user.trial_started_at.isoformat() if user.trial_started_at else None,
        trial_expires_at=user.trial_expires_at.isoformat() if user.trial_expires_at else None,
        created_at=user.created_at.isoformat() if user.created_at else "",
        updated_at=user.updated_at.isoformat() if user.updated_at else "",
    )


def _violation_to_response(v: Violation) -> ViolationRecordResponse:
    """Convert Violation model to response."""
    return ViolationRecordResponse(
        id=v.id,
        user_id=v.user_id,
        group_id=v.group_id,
        rule_type=v.rule_type,
        rule_pattern=v.rule_pattern,
        content=v.content,
        action_taken=v.action_taken.value,
        action_duration=v.action_duration,
        created_at=v.created_at.isoformat() if v.created_at else "",
    )


# =============================================================================
# User CRUD Endpoints
# =============================================================================

@router.get("", response_model=UserListResponse)
async def list_users(
    cursor: Optional[str] = None,
    limit: int = 20,
    state_filter: Optional[str] = None,
    has_trial: Optional[bool] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> UserListResponse:
    """
    Get list of users with cursor pagination.

    - cursor: Pagination cursor (user ID from previous response)
    - limit: Number of items per page (max 100)
    - state_filter: Filter by state (new, pending, active, silent, churned, blocked)
    - has_trial: Filter by trial status
    - search: Search by username or telegram_id
    """
    query = select(User)
    count_query = select(func.count(User.id))

    # Cursor pagination
    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.where(User.id < cursor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    # Filters
    if state_filter:
        try:
            state_enum = UserState(state_filter)
            query = query.where(User.state == state_enum)
            count_query = count_query.where(User.state == state_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid state: {state_filter}")

    if has_trial is not None:
        if has_trial:
            query = query.where(User.trial_started_at.isnot(None))
            count_query = count_query.where(User.trial_started_at.isnot(None))
        else:
            query = query.where(User.trial_started_at.is_(None))
            count_query = count_query.where(User.trial_started_at.is_(None))

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            (User.username.like(search_pattern)) |
            (User.telegram_id.cast(String).like(search_pattern))
        )
        count_query = count_query.where(
            (User.username.like(search_pattern)) |
            (User.telegram_id.cast(String).like(search_pattern))
        )

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get data with pagination
    query = query.order_by(desc(User.id)).limit(limit + 1)
    result = await db.execute(query)
    users = list(result.scalars().all())

    # Check if there are more results
    has_more = len(users) > limit
    if has_more:
        users = users[:limit]

    # Get next cursor
    next_cursor = str(users[-1].id) if users and has_more else None

    return UserListResponse(
        data=[_user_to_response(u) for u in users],
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Get user by ID."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _user_to_response(user)


@router.get("/telegram/{telegram_id}", response_model=UserResponse)
async def get_user_by_telegram(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Get user by Telegram ID."""
    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _user_to_response(user)


# =============================================================================
# User Operations
# =============================================================================

@router.put("/{user_id}/state")
async def update_user_state(
    user_id: int,
    update: UserStateUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update user state."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        new_state = UserState(update.state)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid state. Must be one of: {[s.value for s in UserState]}"
        )

    user.state = new_state
    await db.commit()
    await db.refresh(user)

    return {
        "code": 0,
        "message": "State updated",
        "data": {"user_id": user_id, "state": user.state.value}
    }


@router.post("/{user_id}/warn")
async def warn_user(
    user_id: int,
    request: UserWarnRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add warning to user and optionally mute."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Increment warning count
    user.warning_count += 1

    # Auto mute if warning threshold reached (configurable)
    if user.warning_count >= 3:
        user.muted_until = datetime.utcnow() + timedelta(hours=request.duration_hours)

    await db.commit()
    await db.refresh(user)

    return {
        "code": 0,
        "message": "Warning added",
        "data": {
            "user_id": user_id,
            "warning_count": user.warning_count,
            "muted_until": user.muted_until.isoformat() if user.muted_until else None,
        }
    }


@router.post("/{user_id}/mute")
async def mute_user(
    user_id: int,
    request: UserMuteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mute user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.muted_until = datetime.utcnow() + timedelta(hours=request.duration_hours)
    await db.commit()
    await db.refresh(user)

    return {
        "code": 0,
        "message": "User muted",
        "data": {
            "user_id": user_id,
            "muted_until": user.muted_until.isoformat(),
        }
    }


@router.post("/{user_id}/unmute")
async def unmute_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Unmute user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.muted_until = None
    user.warning_count = 0  # Reset warnings on unmute
    await db.commit()
    await db.refresh(user)

    return {
        "code": 0,
        "message": "User unmuted",
        "data": {
            "user_id": user_id,
            "warning_count": user.warning_count,
            "muted_until": None,
        }
    }


@router.post("/{user_id}/block")
async def block_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Block user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.state = UserState.BLOCKED
    user.muted_until = None  # Clear any existing mute
    await db.commit()
    await db.refresh(user)

    return {
        "code": 0,
        "message": "User blocked",
        "data": {"user_id": user_id, "state": user.state.value}
    }


@router.post("/{user_id}/unblock")
async def unblock_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Unblock user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.state != UserState.BLOCKED:
        raise HTTPException(status_code=400, detail="User is not blocked")

    user.state = UserState.NEW
    await db.commit()
    await db.refresh(user)

    return {
        "code": 0,
        "message": "User unblocked",
        "data": {"user_id": user_id, "state": user.state.value}
    }


# =============================================================================
# User History
# =============================================================================

@router.get("/{user_id}/violations", response_model=ViolationListResponse)
async def get_user_violations(
    user_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> ViolationListResponse:
    """Get user violation records."""
    # Check user exists
    user_result = await db.execute(select(User).where(User.id == user_id))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    # Get violations
    query = (
        select(Violation)
        .where(Violation.user_id == user_id)
        .order_by(desc(Violation.created_at))
        .limit(limit)
    )
    result = await db.execute(query)
    violations = list(result.scalars().all())

    # Count total
    count_result = await db.execute(
        select(func.count(Violation.id)).where(Violation.user_id == user_id)
    )
    total = count_result.scalar() or 0

    return ViolationListResponse(
        data=[_violation_to_response(v) for v in violations],
        total=total,
    )


@router.get("/{user_id}/actions")
async def get_user_actions(
    user_id: int,
    cursor: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get user action history (placeholder - would need ActionLog model)."""
    # Check user exists
    user_result = await db.execute(select(User).where(User.id == user_id))
    if not user_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="User not found")

    # This is a placeholder - would need ActionLog model
    return {
        "code": 0,
        "message": "success",
        "data": [],
        "total": 0,
        "next_cursor": None,
        "has_more": False,
    }


# =============================================================================
# Statistics Endpoints
# =============================================================================

@router.get("/stats", response_model=UserStatsResponse)
async def get_user_stats(
    db: AsyncSession = Depends(get_db),
) -> UserStatsResponse:
    """Get user statistics."""
    # Total users
    total_result = await db.execute(select(func.count(User.id)))
    total = total_result.scalar() or 0

    # Count by state
    state_counts = {}
    for state in UserState:
        count_result = await db.execute(
            select(func.count(User.id)).where(User.state == state)
        )
        state_counts[state.value] = count_result.scalar() or 0

    # Trial stats
    trial_result = await db.execute(
        select(func.count(User.id)).where(User.trial_started_at.isnot(None))
    )
    with_trial = trial_result.scalar() or 0

    # Muted count
    muted_result = await db.execute(
        select(func.count(User.id)).where(User.muted_until.isnot(None))
    )
    muted = muted_result.scalar() or 0

    # Warning count
    warned_result = await db.execute(
        select(func.count(User.id)).where(User.warning_count > 0)
    )
    warned = warned_result.scalar() or 0

    return UserStatsResponse(
        code=0,
        message="success",
        data={
            "total": total,
            "by_state": state_counts,
            "with_trial": with_trial,
            "muted": muted,
            "warned": warned,
        }
    )


@router.get("/stats/funnel", response_model=UserFunnelResponse)
async def get_user_funnel(
    db: AsyncSession = Depends(get_db),
) -> UserFunnelResponse:
    """Get user conversion funnel."""
    # Get counts for each state
    new_result = await db.execute(
        select(func.count(User.id)).where(User.state == UserState.NEW)
    )
    new_count = new_result.scalar() or 0

    pending_result = await db.execute(
        select(func.count(User.id)).where(User.state == UserState.PENDING)
    )
    pending_count = pending_result.scalar() or 0

    active_result = await db.execute(
        select(func.count(User.id)).where(User.state == UserState.ACTIVE)
    )
    active_count = active_result.scalar() or 0

    silent_result = await db.execute(
        select(func.count(User.id)).where(User.state == UserState.SILENT)
    )
    silent_count = silent_result.scalar() or 0

    churned_result = await db.execute(
        select(func.count(User.id)).where(User.state == UserState.CHURNED)
    )
    churned_count = churned_result.scalar() or 0

    blocked_result = await db.execute(
        select(func.count(User.id)).where(User.state == UserState.BLOCKED)
    )
    blocked_count = blocked_result.scalar() or 0

    total = new_count + pending_count + active_count + silent_count + churned_count + blocked_count

    return UserFunnelResponse(
        code=0,
        message="success",
        data={
            "total": total,
            "stages": {
                "new": {"count": new_count, "percentage": 0},
                "pending": {"count": pending_count, "percentage": 0},
                "active": {"count": active_count, "percentage": 0},
                "silent": {"count": silent_count, "percentage": 0},
                "churned": {"count": churned_count, "percentage": 0},
                "blocked": {"count": blocked_count, "percentage": 0},
            },
            "conversion_rates": {
                "pending_rate": round(pending_count / total * 100, 2) if total > 0 else 0,
                "active_rate": round(active_count / total * 100, 2) if total > 0 else 0,
                "churn_rate": round(churned_count / total * 100, 2) if total > 0 else 0,
            }
        }
    )


@router.get("/stats/daily")
async def get_daily_stats(
    days: int = 7,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get daily user registration statistics."""
    # Placeholder for daily stats - would need created_at grouping
    return {
        "code": 0,
        "message": "success",
        "data": {
            "period_days": days,
            "daily_registrations": [],
        }
    }
