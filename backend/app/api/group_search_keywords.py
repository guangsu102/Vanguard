"""
Growth-side group search keyword API.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.keyword_generator import KeywordGenerator
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.config import settings
from app.core.database import get_db
from app.modules.acquisition.models import (
    GroupSearchKeyword,
    SearchKeywordSource,
    SearchKeywordStatus,
)


router = APIRouter()


class GroupSearchKeywordCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=255)
    keyword_type: str = Field(..., min_length=1, max_length=50)
    match_mode: str = Field(default="fuzzy", max_length=20)
    requires_review: bool = True
    enabled: bool = True
    source: str = Field(default="manual")


class GroupSearchKeywordUpdate(BaseModel):
    text: Optional[str] = Field(None, min_length=1, max_length=255)
    keyword_type: Optional[str] = Field(None, min_length=1, max_length=50)
    match_mode: Optional[str] = Field(None, max_length=20)
    requires_review: Optional[bool] = None
    enabled: Optional[bool] = None
    status: Optional[str] = None


class GenerateGroupSearchKeywordsRequest(BaseModel):
    keyword_type: str = Field(..., min_length=1, max_length=50)
    count: int = Field(default=20, ge=1, le=50)
    auto_approve: bool = False


class GroupSearchKeywordResponse(BaseModel):
    id: int
    text: str
    keyword_type: str
    status: str
    source: str
    match_mode: str
    trigger_count: int
    requires_review: bool
    enabled: bool
    created_at: str
    updated_at: str


class GroupSearchKeywordListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[GroupSearchKeywordResponse]
    total: int


def _serialize(item: GroupSearchKeyword) -> GroupSearchKeywordResponse:
    return GroupSearchKeywordResponse(
        id=item.id,
        text=item.text,
        keyword_type=item.keyword_type,
        status=item.status.value,
        source=item.source.value,
        match_mode=item.match_mode,
        trigger_count=item.trigger_count,
        requires_review=item.requires_review,
        enabled=item.enabled,
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
    )


@router.get("", response_model=GroupSearchKeywordListResponse)
async def list_group_search_keywords(
    keyword_type: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    enabled: Optional[bool] = None,
    keyword: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> GroupSearchKeywordListResponse:
    query = select(GroupSearchKeyword)
    count_query = select(func.count(GroupSearchKeyword.id))

    if keyword_type:
        query = query.where(GroupSearchKeyword.keyword_type == keyword_type)
        count_query = count_query.where(GroupSearchKeyword.keyword_type == keyword_type)
    if status_filter:
        query = query.where(GroupSearchKeyword.status == SearchKeywordStatus(status_filter))
        count_query = count_query.where(GroupSearchKeyword.status == SearchKeywordStatus(status_filter))
    if enabled is not None:
        query = query.where(GroupSearchKeyword.enabled == enabled)
        count_query = count_query.where(GroupSearchKeyword.enabled == enabled)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        query = query.where(GroupSearchKeyword.text.ilike(pattern))
        count_query = count_query.where(GroupSearchKeyword.text.ilike(pattern))

    total = (await db.execute(count_query)).scalar() or 0
    rows = await db.execute(query.order_by(desc(GroupSearchKeyword.id)).offset((page - 1) * page_size).limit(page_size))
    return GroupSearchKeywordListResponse(data=[_serialize(item) for item in rows.scalars().all()], total=total)


@router.post("", response_model=GroupSearchKeywordResponse, status_code=status.HTTP_201_CREATED)
async def create_group_search_keyword(
    request: GroupSearchKeywordCreate,
    db: AsyncSession = Depends(get_db),
) -> GroupSearchKeywordResponse:
    item = GroupSearchKeyword(
        text=request.text.strip(),
        keyword_type=request.keyword_type,
        status=SearchKeywordStatus.PENDING if request.requires_review else SearchKeywordStatus.APPROVED,
        source=SearchKeywordSource(request.source),
        match_mode=request.match_mode,
        requires_review=request.requires_review,
        enabled=request.enabled,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _serialize(item)


@router.put("/{keyword_id:int}", response_model=GroupSearchKeywordResponse)
async def update_group_search_keyword(
    keyword_id: int,
    request: GroupSearchKeywordUpdate,
    db: AsyncSession = Depends(get_db),
) -> GroupSearchKeywordResponse:
    item = await db.get(GroupSearchKeyword, keyword_id)
    if not item:
        raise HTTPException(status_code=404, detail="Group search keyword not found")

    data = request.model_dump(exclude_none=True)
    if "text" in data:
        data["text"] = data["text"].strip()
    if "status" in data:
        data["status"] = SearchKeywordStatus(data["status"])
    for field, value in data.items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    return _serialize(item)


@router.delete("/{keyword_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_search_keyword(keyword_id: int, db: AsyncSession = Depends(get_db)) -> None:
    item = await db.get(GroupSearchKeyword, keyword_id)
    if not item:
        raise HTTPException(status_code=404, detail="Group search keyword not found")
    await db.delete(item)
    await db.commit()


@router.post("/generate")
async def generate_group_search_keywords(
    request: GenerateGroupSearchKeywordsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = LLMProvider(settings.LLM_PROVIDER) if settings.LLM_PROVIDER in {p.value for p in LLMProvider} else LLMProvider.OPENAI
    api_key = settings.OPENAI_API_KEY if provider == LLMProvider.OPENAI else settings.ANTHROPIC_API_KEY
    generator = KeywordGenerator(LLMClient(provider=provider, api_key=api_key))

    existing_rows = await db.execute(select(GroupSearchKeyword.text))
    existing = {value.lower() for value in existing_rows.scalars().all()}
    generated = await generator.generate(category=request.keyword_type, count=request.count)
    created = []
    status_value = SearchKeywordStatus.APPROVED if request.auto_approve else SearchKeywordStatus.PENDING

    for item in generated:
        text = item.text.strip()
        if not text or text.lower() in existing:
            continue
        row = GroupSearchKeyword(
            text=text,
            keyword_type=request.keyword_type,
            status=status_value,
            source=SearchKeywordSource.AI,
            match_mode="fuzzy",
            requires_review=not request.auto_approve,
            enabled=True,
        )
        db.add(row)
        created.append(row)
        existing.add(text.lower())

    await db.commit()
    for row in created:
        await db.refresh(row)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "created": len(created),
            "auto_approved": request.auto_approve,
            "keywords": [_serialize(item).model_dump() for item in created],
        },
    }
