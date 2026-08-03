"""
Growth-side group search keyword API.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai.keyword_generator import (
    KeywordGenerator,
    normalize_keyword_text,
    validate_search_keyword_text,
)
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.config import settings
from app.core.database import get_db
from app.core.keyword.models import KeywordType
from app.modules.acquisition.models import (
    GroupSearchKeyword,
    SearchKeywordSource,
    SearchKeywordStatus,
)
from app.modules.acquisition.search_keyword_registry import (
    add_keyword_signatures,
    build_keyword_signature,
    find_existing_keyword_signatures,
    normalize_group_search_keyword,
    recent_keyword_texts,
)

router = APIRouter()
MAX_GENERATION_ATTEMPTS = 3
MAX_GENERATION_CANDIDATES = 50


class GroupSearchKeywordCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=255)
    keyword_type: str = Field(..., min_length=1, max_length=50)
    match_mode: str = Field(default="fuzzy", max_length=20)
    requires_review: bool = False
    enabled: bool = True
    source: str = Field(default="manual")


class GroupSearchKeywordUpdate(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=255)
    keyword_type: str | None = Field(None, min_length=1, max_length=50)
    match_mode: str | None = Field(None, max_length=20)
    requires_review: bool | None = None
    enabled: bool | None = None
    status: str | None = None


class GenerateGroupSearchKeywordsRequest(BaseModel):
    keyword_type: str = Field(..., min_length=1, max_length=50)
    count: int = Field(default=20, ge=1, le=50)
    auto_approve: bool = True


class GroupSearchKeywordResponse(BaseModel):
    id: int
    text: str
    keyword_type: str
    status: str
    source: str
    match_mode: str
    trigger_count: int
    use_count: int
    used_at: str | None
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
        use_count=item.use_count,
        used_at=item.used_at.isoformat() if item.used_at else None,
        requires_review=item.requires_review,
        enabled=item.enabled,
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
    )


@router.get("", response_model=GroupSearchKeywordListResponse)
async def list_group_search_keywords(
    keyword_type: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    enabled: bool | None = None,
    keyword: str | None = None,
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
    text = request.text.strip()
    normalized_text = normalize_group_search_keyword(text)
    if not normalized_text:
        raise HTTPException(status_code=400, detail="Invalid keyword text")
    duplicates = await find_existing_keyword_signatures(db, [(request.keyword_type, text)])
    if duplicates:
        raise HTTPException(status_code=409, detail="Group search keyword already exists")

    item = GroupSearchKeyword(
        text=text,
        normalized_text=normalized_text,
        keyword_type=request.keyword_type,
        status=SearchKeywordStatus.PENDING if request.requires_review else SearchKeywordStatus.APPROVED,
        source=SearchKeywordSource(request.source),
        match_mode=request.match_mode,
        requires_review=request.requires_review,
        enabled=request.enabled,
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Group search keyword already exists") from exc
    await db.refresh(item)
    await add_keyword_signatures([(item.keyword_type, item.text)])
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
    next_text = data.get("text", item.text)
    next_type = data.get("keyword_type", item.keyword_type)
    if "text" in data or "keyword_type" in data:
        normalized_text = normalize_group_search_keyword(next_text)
        if not normalized_text:
            raise HTTPException(status_code=400, detail="Invalid keyword text")
        existing = await db.execute(
            select(GroupSearchKeyword.id).where(
                GroupSearchKeyword.keyword_type == next_type,
                GroupSearchKeyword.normalized_text == normalized_text,
                GroupSearchKeyword.id != keyword_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="Group search keyword already exists")
        data["normalized_text"] = normalized_text
    if "status" in data:
        data["status"] = SearchKeywordStatus(data["status"])
    for field, value in data.items():
        setattr(item, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Group search keyword already exists") from exc
    await db.refresh(item)
    await add_keyword_signatures([(item.keyword_type, item.text)])
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
    if request.keyword_type not in {item.value for item in KeywordType}:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid keyword_type. Must be one of: {[item.value for item in KeywordType]}",
        )

    provider = (
        LLMProvider(settings.LLM_PROVIDER)
        if settings.LLM_PROVIDER in {p.value for p in LLMProvider}
        else LLMProvider.OPENAI
    )
    api_key = settings.OPENAI_API_KEY if provider == LLMProvider.OPENAI else settings.ANTHROPIC_API_KEY
    llm_client = (
        None
        if provider != LLMProvider.LOCAL and not api_key
        else LLMClient(provider=provider, api_key=api_key)
    )
    generator = KeywordGenerator(llm_client)

    avoid_keywords = await recent_keyword_texts(db, limit=200)

    created = []
    skipped_existing: list[str] = []
    skipped_duplicate: list[str] = []
    skipped_invalid: list[str] = []
    skipped_invalid_reasons: dict[str, int] = {}
    skipped_empty = 0
    considered = 0
    attempts = 0
    created_signatures: set[str] = set()
    status_value = SearchKeywordStatus.APPROVED if request.auto_approve else SearchKeywordStatus.PENDING

    while len(created) < request.count and attempts < MAX_GENERATION_ATTEMPTS:
        attempts += 1
        remaining = request.count - len(created)
        candidate_count = min(
            MAX_GENERATION_CANDIDATES,
            max(20, remaining * 3),
        )
        generated = await generator.generate(
            category=request.keyword_type,
            count=candidate_count,
            avoid_keywords=avoid_keywords,
        )
        if not generated:
            break

        for item in generated:
            if len(created) >= request.count:
                break
            considered += 1
            text = item.text.strip()
            normalized = normalize_keyword_text(text)
            if not normalized:
                skipped_empty += 1
                continue
            is_valid, invalid_reason = validate_search_keyword_text(text)
            if not is_valid:
                skipped_invalid.append(text)
                reason = invalid_reason or "invalid"
                skipped_invalid_reasons[reason] = skipped_invalid_reasons.get(reason, 0) + 1
                avoid_keywords.append(text)
                continue
            signature = build_keyword_signature(request.keyword_type, text)
            if not signature:
                skipped_empty += 1
                continue
            if signature in created_signatures:
                skipped_duplicate.append(text)
                avoid_keywords.append(text)
                continue
            existing = await find_existing_keyword_signatures(db, [(request.keyword_type, text)])
            if signature in existing:
                skipped_existing.append(text)
                avoid_keywords.append(text)
                continue

            row = GroupSearchKeyword(
                text=text,
                normalized_text=normalized,
                keyword_type=request.keyword_type,
                status=status_value,
                source=SearchKeywordSource.AI,
                match_mode="fuzzy",
                requires_review=not request.auto_approve,
                enabled=True,
            )
            db.add(row)
            created.append(row)
            created_signatures.add(signature)
            avoid_keywords.append(text)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Generated keyword conflicts with an existing keyword") from exc
    for row in created:
        await db.refresh(row)
    await add_keyword_signatures((row.keyword_type, row.text) for row in created)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "requested": request.count,
            "generated": considered,
            "attempts": attempts,
            "created": len(created),
            "skipped_existing": len(skipped_existing),
            "skipped_duplicate": len(skipped_duplicate),
            "skipped_invalid": len(skipped_invalid),
            "skipped_invalid_reasons": skipped_invalid_reasons,
            "skipped_empty": skipped_empty,
            "skipped_existing_keywords": skipped_existing[:20],
            "skipped_duplicate_keywords": skipped_duplicate[:20],
            "skipped_invalid_keywords": skipped_invalid[:20],
            "candidate_exhausted": len(created) < request.count,
            "llm_configured": provider == LLMProvider.LOCAL or bool(api_key),
            "auto_approved": request.auto_approve,
            "keywords": [_serialize(item).model_dump() for item in created],
        },
    }
