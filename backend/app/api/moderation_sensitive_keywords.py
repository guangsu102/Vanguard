"""
Moderation sensitive keyword API.
"""

from collections.abc import Mapping
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guardian_validation import (
    GuardianGroupTarget,
    require_guardian_operator,
    resolve_guardian_group_target,
)
from app.core.database import get_db
from app.core.group.models import Group
from app.modules.guardian.models import (
    ModerationSensitiveKeyword,
    SensitiveKeywordSource,
    ViolationAction,
    ViolationLevel,
)

router = APIRouter()


class SensitiveKeywordCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=255)
    category: str = Field(default="sensitive", max_length=50)
    source: str = Field(default="manual")
    level: str = Field(default="medium")
    action: str = Field(default="warn")
    group_id: Optional[int] = None
    enabled: bool = True
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_sample: Optional[str] = None


class SensitiveKeywordUpdate(BaseModel):
    category: Optional[str] = Field(None, max_length=50)
    level: Optional[str] = None
    action: Optional[str] = None
    group_id: Optional[int] = None
    enabled: Optional[bool] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    source_sample: Optional[str] = None


class SensitiveKeywordResponse(BaseModel):
    id: int
    text: str
    category: str
    source: str
    level: str
    action: str
    group_id: Optional[int] = None
    enabled: bool
    confidence: float
    source_sample: Optional[str] = None
    created_at: str
    updated_at: str


class SensitiveKeywordListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[SensitiveKeywordResponse]
    total: int


def _normalize_text(value: str) -> str:
    return value.strip().lower()


async def _require_group_target(
    db: AsyncSession, telegram_chat_id: int
) -> GuardianGroupTarget:
    target = await resolve_guardian_group_target(db, telegram_chat_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    return target


async def _telegram_chat_id_map(
    db: AsyncSession, core_group_ids: set[int]
) -> dict[int, int]:
    if not core_group_ids:
        return {}
    rows = await db.execute(
        select(Group.id, Group.group_id).where(Group.id.in_(core_group_ids))
    )
    return {int(core_group_id): int(telegram_chat_id) for core_group_id, telegram_chat_id in rows}


def _serialize(
    item: ModerationSensitiveKeyword,
    telegram_chat_ids: Mapping[int, int] | None = None,
) -> SensitiveKeywordResponse:
    external_group_id = None
    if item.group_id is not None:
        external_group_id = (telegram_chat_ids or {}).get(item.group_id, item.group_id)
    return SensitiveKeywordResponse(
        id=item.id,
        text=item.text,
        category=item.category,
        source=item.source.value,
        level=item.level.value,
        action=item.action.value,
        group_id=external_group_id,
        enabled=item.enabled,
        confidence=float(item.confidence),
        source_sample=item.source_sample,
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
    )


@router.get("", response_model=SensitiveKeywordListResponse)
async def list_sensitive_keywords(
    group_id: Optional[int] = None,
    category: Optional[str] = None,
    enabled: Optional[bool] = None,
    search: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> SensitiveKeywordListResponse:
    core_group_id = None
    if group_id is not None:
        core_group_id = (await _require_group_target(db, group_id)).core_group_id

    query = select(ModerationSensitiveKeyword)
    count_query = select(func.count(ModerationSensitiveKeyword.id))

    if core_group_id is not None:
        query = query.where(ModerationSensitiveKeyword.group_id == core_group_id)
        count_query = count_query.where(ModerationSensitiveKeyword.group_id == core_group_id)
    if category:
        query = query.where(ModerationSensitiveKeyword.category == category)
        count_query = count_query.where(ModerationSensitiveKeyword.category == category)
    if enabled is not None:
        query = query.where(ModerationSensitiveKeyword.enabled == enabled)
        count_query = count_query.where(ModerationSensitiveKeyword.enabled == enabled)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(ModerationSensitiveKeyword.text.ilike(pattern))
        count_query = count_query.where(ModerationSensitiveKeyword.text.ilike(pattern))

    total = (await db.execute(count_query)).scalar() or 0
    rows = await db.execute(
        query.order_by(desc(ModerationSensitiveKeyword.id)).offset((page - 1) * page_size).limit(page_size)
    )
    items = list(rows.scalars().all())
    telegram_chat_ids = await _telegram_chat_id_map(
        db, {item.group_id for item in items if item.group_id is not None}
    )
    return SensitiveKeywordListResponse(
        data=[_serialize(item, telegram_chat_ids) for item in items], total=total
    )


@router.post(
    "",
    response_model=SensitiveKeywordResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_guardian_operator)],
)
async def create_sensitive_keyword(
    request: SensitiveKeywordCreate,
    db: AsyncSession = Depends(get_db),
) -> SensitiveKeywordResponse:
    try:
        source = SensitiveKeywordSource(request.source)
        level = ViolationLevel(request.level)
        action = ViolationAction(request.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    target = None
    if request.group_id is not None:
        target = await _require_group_target(db, request.group_id)

    item = ModerationSensitiveKeyword(
        text=request.text.strip(),
        normalized_text=_normalize_text(request.text),
        category=request.category,
        source=source,
        level=level,
        action=action,
        group_id=target.core_group_id if target else None,
        enabled=request.enabled,
        confidence=request.confidence,
        source_sample=request.source_sample,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    telegram_chat_ids = (
        {target.core_group_id: target.telegram_chat_id} if target is not None else {}
    )
    return _serialize(item, telegram_chat_ids)


@router.put(
    "/{keyword_id:int}",
    response_model=SensitiveKeywordResponse,
    dependencies=[Depends(require_guardian_operator)],
)
async def update_sensitive_keyword(
    keyword_id: int,
    request: SensitiveKeywordUpdate,
    db: AsyncSession = Depends(get_db),
) -> SensitiveKeywordResponse:
    item = await db.get(ModerationSensitiveKeyword, keyword_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sensitive keyword not found")

    data = request.model_dump(exclude_none=True)
    if "group_id" in data and data["group_id"] is not None:
        target = await _require_group_target(db, data["group_id"])
        data["group_id"] = target.core_group_id
    if "level" in data:
        data["level"] = ViolationLevel(data["level"])
    if "action" in data:
        data["action"] = ViolationAction(data["action"])
    for field, value in data.items():
        setattr(item, field, value)

    await db.commit()
    await db.refresh(item)
    telegram_chat_ids = await _telegram_chat_id_map(
        db, {item.group_id} if item.group_id is not None else set()
    )
    return _serialize(item, telegram_chat_ids)


@router.delete(
    "/{keyword_id:int}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_guardian_operator)],
)
async def delete_sensitive_keyword(keyword_id: int, db: AsyncSession = Depends(get_db)) -> None:
    item = await db.get(ModerationSensitiveKeyword, keyword_id)
    if not item:
        raise HTTPException(status_code=404, detail="Sensitive keyword not found")
    await db.delete(item)
    await db.commit()
