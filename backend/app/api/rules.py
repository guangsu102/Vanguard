"""
Rules API Router

RESTful API for moderation rules and whitelist management with cursor pagination.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.guardian.models import ModerationRule, Whitelist, RuleType, ViolationAction, ViolationLevel


router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class RuleCreate(BaseModel):
    """Rule creation request."""
    rule_type: str = Field(..., description="Rule type: keyword, domain, frequency, image")
    pattern: str = Field(..., max_length=255, description="Rule pattern")
    level: str = Field(default="medium", description="Violation level: low, medium, high")
    action: str = Field(default="warn", description="Action: warn, mute, ban, kick")
    group_id: Optional[int] = Field(None, description="Group ID for group-specific rule")
    enabled: bool = Field(default=True)


class RuleUpdate(BaseModel):
    """Rule update request."""
    pattern: Optional[str] = Field(None, max_length=255)
    level: Optional[str] = Field(None, description="Violation level: low, medium, high")
    action: Optional[str] = Field(None, description="Action: warn, mute, ban, kick")
    enabled: Optional[bool] = None


class RuleResponse(BaseModel):
    """Rule response."""
    id: int
    rule_type: str
    pattern: str
    level: str
    action: str
    group_id: Optional[int] = None
    enabled: bool
    created_at: str
    updated_at: str


class RuleListResponse(BaseModel):
    """Rule list response with cursor pagination."""
    code: int = 0
    message: str = "success"
    data: list[RuleResponse]
    total: int
    next_cursor: Optional[str] = None
    has_more: bool = False


class RuleTestRequest(BaseModel):
    """Rule test request."""
    pattern: str = Field(..., description="Pattern to test")
    content: str = Field(..., description="Content to test against")


class RuleTestResponse(BaseModel):
    """Rule test response."""
    code: int = 0
    message: str = "success"
    data: dict


class WhitelistCreate(BaseModel):
    """Whitelist creation request."""
    whitelist_type: str = Field(..., description="Type: user, domain, path")
    value: str = Field(..., max_length=255, description="Whitelist value")
    group_id: Optional[int] = None
    expires_at: Optional[str] = Field(None, description="Expiration datetime ISO format")


class WhitelistResponse(BaseModel):
    """Whitelist response."""
    id: int
    whitelist_type: str
    value: str
    group_id: Optional[int] = None
    expires_at: Optional[str] = None
    created_at: str


class WhitelistListResponse(BaseModel):
    """Whitelist list response with cursor pagination."""
    code: int = 0
    message: str = "success"
    data: list[WhitelistResponse]
    total: int
    next_cursor: Optional[str] = None
    has_more: bool = False


class RuleStatsResponse(BaseModel):
    """Rule statistics response."""
    code: int = 0
    message: str = "success"
    data: dict


# =============================================================================
# Helper Functions
# =============================================================================

def _rule_to_response(rule: ModerationRule) -> RuleResponse:
    """Convert ModerationRule model to response."""
    return RuleResponse(
        id=rule.id,
        rule_type=rule.rule_type.value,
        pattern=rule.pattern,
        level=rule.level.value,
        action=rule.action.value,
        group_id=rule.group_id,
        enabled=rule.enabled,
        created_at=rule.created_at.isoformat() if rule.created_at else "",
        updated_at=rule.updated_at.isoformat() if rule.updated_at else "",
    )


def _whitelist_to_response(wl: Whitelist) -> WhitelistResponse:
    """Convert Whitelist model to response."""
    return WhitelistResponse(
        id=wl.id,
        whitelist_type=wl.whitelist_type,
        value=wl.value,
        group_id=wl.group_id,
        expires_at=wl.expires_at.isoformat() if wl.expires_at else None,
        created_at=wl.created_at.isoformat() if wl.created_at else "",
    )


# =============================================================================
# Rule CRUD Endpoints
# =============================================================================

@router.get("", response_model=RuleListResponse)
async def list_rules(
    cursor: Optional[str] = None,
    limit: int = 20,
    rule_type: Optional[str] = None,
    level: Optional[str] = None,
    enabled: Optional[bool] = None,
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> RuleListResponse:
    """
    Get list of moderation rules with cursor pagination.

    - cursor: Pagination cursor (rule ID from previous response)
    - limit: Number of items per page (max 100)
    - rule_type: Filter by type (keyword, domain, frequency, image)
    - level: Filter by level (low, medium, high)
    - enabled: Filter by enabled status
    - group_id: Filter by group ID (NULL for global rules)
    """
    query = select(ModerationRule)
    count_query = select(func.count(ModerationRule.id))

    # Cursor pagination
    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.where(ModerationRule.id < cursor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    # Filters
    if rule_type:
        try:
            type_enum = RuleType(rule_type)
            query = query.where(ModerationRule.rule_type == type_enum)
            count_query = count_query.where(ModerationRule.rule_type == type_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid rule type: {rule_type}")

    if level:
        try:
            level_enum = ViolationLevel(level)
            query = query.where(ModerationRule.level == level_enum)
            count_query = count_query.where(ModerationRule.level == level_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid level: {level}")

    if enabled is not None:
        query = query.where(ModerationRule.enabled == enabled)
        count_query = count_query.where(ModerationRule.enabled == enabled)

    if group_id is not None:
        query = query.where(ModerationRule.group_id == group_id)
        count_query = count_query.where(ModerationRule.group_id == group_id)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get data with pagination
    query = query.order_by(desc(ModerationRule.id)).limit(limit + 1)
    result = await db.execute(query)
    rules = list(result.scalars().all())

    # Check if there are more results
    has_more = len(rules) > limit
    if has_more:
        rules = rules[:limit]

    # Get next cursor
    next_cursor = str(rules[-1].id) if rules and has_more else None

    return RuleListResponse(
        data=[_rule_to_response(r) for r in rules],
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    rule_data: RuleCreate,
    db: AsyncSession = Depends(get_db),
) -> RuleResponse:
    """Create a new moderation rule."""
    try:
        type_enum = RuleType(rule_data.rule_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid rule type. Must be one of: {[r.value for r in RuleType]}"
        )

    try:
        level_enum = ViolationLevel(rule_data.level)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid level. Must be one of: {[l.value for l in ViolationLevel]}"
        )

    try:
        action_enum = ViolationAction(rule_data.action)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action. Must be one of: {[a.value for a in ViolationAction]}"
        )

    rule = ModerationRule(
        rule_type=type_enum,
        pattern=rule_data.pattern,
        level=level_enum,
        action=action_enum,
        group_id=rule_data.group_id,
        enabled=rule_data.enabled,
    )

    db.add(rule)
    await db.commit()
    await db.refresh(rule)

    return _rule_to_response(rule)


@router.get("/{rule_id}", response_model=RuleResponse)
async def get_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
) -> RuleResponse:
    """Get rule by ID."""
    result = await db.execute(select(ModerationRule).where(ModerationRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    return _rule_to_response(rule)


@router.put("/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: int,
    rule_data: RuleUpdate,
    db: AsyncSession = Depends(get_db),
) -> RuleResponse:
    """Update rule."""
    result = await db.execute(select(ModerationRule).where(ModerationRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    update_data = rule_data.model_dump(exclude_none=True)

    # Handle enum conversions
    if "level" in update_data:
        try:
            update_data["level"] = ViolationLevel(update_data["level"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid level")

    if "action" in update_data:
        try:
            update_data["action"] = ViolationAction(update_data["action"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid action")

    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)

    return _rule_to_response(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete rule."""
    result = await db.execute(select(ModerationRule).where(ModerationRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    await db.delete(rule)
    await db.commit()


@router.post("/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Toggle rule enabled status."""
    result = await db.execute(select(ModerationRule).where(ModerationRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.enabled = not rule.enabled
    await db.commit()
    await db.refresh(rule)

    return {
        "code": 0,
        "message": "Rule toggled",
        "data": {
            "rule_id": rule_id,
            "enabled": rule.enabled,
        }
    }


# =============================================================================
# Rule Test Endpoint
# =============================================================================

@router.post("/test", response_model=RuleTestResponse)
async def test_rule(
    request: RuleTestRequest,
    db: AsyncSession = Depends(get_db),
) -> RuleTestResponse:
    """Test a rule pattern against content."""
    import re

    matched = False
    match_type = None

    # Test keyword pattern (simple contains)
    if request.pattern.lower() in request.content.lower():
        matched = True
        match_type = "keyword_exact"

    # Test regex pattern
    try:
        regex = re.compile(request.pattern, re.IGNORECASE)
        if regex.search(request.content):
            matched = True
            match_type = "regex"
    except re.error:
        pass

    return RuleTestResponse(
        code=0,
        message="success",
        data={
            "pattern": request.pattern,
            "content_length": len(request.content),
            "matched": matched,
            "match_type": match_type,
        }
    )


# =============================================================================
# Whitelist Endpoints
# =============================================================================

@router.get("/whitelist", response_model=WhitelistListResponse)
async def list_whitelist(
    cursor: Optional[str] = None,
    limit: int = 20,
    whitelist_type: Optional[str] = None,
    group_id: Optional[int] = None,
    include_expired: bool = False,
    db: AsyncSession = Depends(get_db),
) -> WhitelistListResponse:
    """
    Get whitelist entries with cursor pagination.

    - cursor: Pagination cursor (whitelist ID from previous response)
    - limit: Number of items per page (max 100)
    - whitelist_type: Filter by type (user, domain, path)
    - group_id: Filter by group ID
    - include_expired: Include expired entries
    """
    query = select(Whitelist)
    count_query = select(func.count(Whitelist.id))

    # Cursor pagination
    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.where(Whitelist.id < cursor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    # Filters
    if whitelist_type:
        query = query.where(Whitelist.whitelist_type == whitelist_type)
        count_query = count_query.where(Whitelist.whitelist_type == whitelist_type)

    if group_id is not None:
        query = query.where(Whitelist.group_id == group_id)
        count_query = count_query.where(Whitelist.group_id == group_id)

    if not include_expired:
        query = query.where(
            (Whitelist.expires_at.is_(None)) |
            (Whitelist.expires_at > datetime.utcnow())
        )

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get data with pagination
    query = query.order_by(desc(Whitelist.id)).limit(limit + 1)
    result = await db.execute(query)
    entries = list(result.scalars().all())

    # Check if there are more results
    has_more = len(entries) > limit
    if has_more:
        entries = entries[:limit]

    # Get next cursor
    next_cursor = str(entries[-1].id) if entries and has_more else None

    return WhitelistListResponse(
        data=[_whitelist_to_response(e) for e in entries],
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("/whitelist", response_model=WhitelistResponse, status_code=status.HTTP_201_CREATED)
async def create_whitelist(
    whitelist_data: WhitelistCreate,
    db: AsyncSession = Depends(get_db),
) -> WhitelistResponse:
    """Create whitelist entry."""
    expires_at = None
    if whitelist_data.expires_at:
        try:
            expires_at = datetime.fromisoformat(whitelist_data.expires_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid datetime format")

    entry = Whitelist(
        whitelist_type=whitelist_data.whitelist_type,
        value=whitelist_data.value,
        group_id=whitelist_data.group_id,
        expires_at=expires_at,
    )

    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    return _whitelist_to_response(entry)


@router.get("/whitelist/{entry_id}", response_model=WhitelistResponse)
async def get_whitelist_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
) -> WhitelistResponse:
    """Get whitelist entry by ID."""
    result = await db.execute(select(Whitelist).where(Whitelist.id == entry_id))
    entry = result.scalar_one_or_none()

    if not entry:
        raise HTTPException(status_code=404, detail="Whitelist entry not found")

    return _whitelist_to_response(entry)


@router.delete("/whitelist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_whitelist_entry(
    entry_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete whitelist entry."""
    result = await db.execute(select(Whitelist).where(Whitelist.id == entry_id))
    entry = result.scalar_one_or_none()

    if not entry:
        raise HTTPException(status_code=404, detail="Whitelist entry not found")

    await db.delete(entry)
    await db.commit()


@router.post("/whitelist/batch-delete")
async def batch_delete_whitelist(
    entry_ids: list[int],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Batch delete whitelist entries."""
    result = await db.execute(select(Whitelist).where(Whitelist.id.in_(entry_ids)))
    entries = result.scalars().all()

    deleted_count = 0
    for entry in entries:
        await db.delete(entry)
        deleted_count += 1

    await db.commit()

    return {
        "code": 0,
        "message": "Batch delete completed",
        "data": {
            "deleted_count": deleted_count,
            "failed_count": len(entry_ids) - deleted_count,
        }
    }


# =============================================================================
# Statistics Endpoints
# =============================================================================

@router.get("/stats", response_model=RuleStatsResponse)
async def get_rule_stats(
    db: AsyncSession = Depends(get_db),
) -> RuleStatsResponse:
    """Get rule statistics."""
    # Total rules
    total_result = await db.execute(select(func.count(ModerationRule.id)))
    total = total_result.scalar() or 0

    # Enabled rules
    enabled_result = await db.execute(
        select(func.count(ModerationRule.id)).where(ModerationRule.enabled == True)
    )
    enabled = enabled_result.scalar() or 0

    # By type
    by_type = {}
    for rtype in RuleType:
        count_result = await db.execute(
            select(func.count(ModerationRule.id)).where(ModerationRule.rule_type == rtype)
        )
        by_type[rtype.value] = count_result.scalar() or 0

    # By level
    by_level = {}
    for level in ViolationLevel:
        count_result = await db.execute(
            select(func.count(ModerationRule.id)).where(ModerationRule.level == level)
        )
        by_level[level.value] = count_result.scalar() or 0

    # By action
    by_action = {}
    for action in ViolationAction:
        count_result = await db.execute(
            select(func.count(ModerationRule.id)).where(ModerationRule.action == action)
        )
        by_action[action.value] = count_result.scalar() or 0

    # Global vs group-specific
    global_result = await db.execute(
        select(func.count(ModerationRule.id)).where(ModerationRule.group_id.is_(None))
    )
    global_rules = global_result.scalar() or 0

    # Whitelist stats
    wl_total_result = await db.execute(select(func.count(Whitelist.id)))
    wl_total = wl_total_result.scalar() or 0

    wl_by_type = {}
    for wtype in ["user", "domain", "path"]:
        count_result = await db.execute(
            select(func.count(Whitelist.id)).where(Whitelist.whitelist_type == wtype)
        )
        wl_by_type[wtype] = count_result.scalar() or 0

    return RuleStatsResponse(
        code=0,
        message="success",
        data={
            "rules": {
                "total": total,
                "enabled": enabled,
                "disabled": total - enabled,
                "global": global_rules,
                "group_specific": total - global_rules,
                "by_type": by_type,
                "by_level": by_level,
                "by_action": by_action,
            },
            "whitelist": {
                "total": wl_total,
                "by_type": wl_by_type,
            }
        }
    )
