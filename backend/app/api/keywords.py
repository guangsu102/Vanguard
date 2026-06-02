"""
Keywords API Router

RESTful API for keyword management.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.ai.keyword_generator import KeywordGenerator
from app.core.keyword.models import Keyword, KeywordType, KeywordStatus, MatchMode


router = APIRouter()


class KeywordCreate(BaseModel):
    """Keyword creation request."""
    text: str | None = Field(None, max_length=255, description="Keyword text")
    word: str | None = Field(None, max_length=255, description="Frontend keyword text")
    type: str = Field(default="demand", description="Keyword type: demand, inquiry, price, competitor")
    match_mode: str | None = Field(default=None, description="Match mode: exact, fuzzy, regex")
    matchMode: str | None = Field(default=None, description="Frontend match mode")


class KeywordUpdate(BaseModel):
    """Keyword update request."""
    text: str | None = None
    word: str | None = None
    type: str | None = None
    status: str | None = Field(None, description="Status: pending, approved, executing, completed, discarded")
    match_mode: str | None = None
    matchMode: str | None = None


class KeywordResponse(BaseModel):
    """Keyword response."""
    id: int
    text: str
    type: str
    status: str
    match_mode: str
    trigger_count: int
    created_at: str


class KeywordListResponse(BaseModel):
    """Keyword list response."""
    code: int = 0
    message: str = "success"
    data: list[KeywordResponse]
    total: int


class KeywordGenerateRequest(BaseModel):
    """AI生成关键词请求"""
    category: str = Field(default="demand", description="关键词分类: demand, inquiry, price, competitor")
    count: int = Field(default=20, ge=1, le=50, description="生成数量")
    exclude_existing: bool = Field(default=True, description="是否排除已存在的关键词")


class KeywordBatchGenerateRequest(BaseModel):
    """AI批量生成关键词请求（群搜索场景）"""
    counts: dict[str, int] = Field(
        default={"demand": 10, "inquiry": 8, "price": 5, "competitor": 7},
        description="各分类生成数量"
    )


class GeneratedKeywordItem(BaseModel):
    """生成的关键词项"""
    text: str
    type: str
    is_new: bool = True  # 是否为新关键词


class KeywordGenerateResponse(BaseModel):
    """AI生成关键词响应"""
    code: int = 0
    message: str = "success"
    data: dict


class KeywordBatchAddRequest(BaseModel):
    """批量添加关键词请求"""
    keywords: list[str] | None = Field(None, description="关键词列表")
    words: list[str] | None = Field(None, description="Frontend keyword list")
    category: str = Field(default="demand", description="关键词分类")
    type: str | None = Field(None, description="Frontend keyword type")
    match_mode: str | None = Field(default=None, description="匹配模式")
    matchMode: str | None = Field(default=None, description="Frontend match mode")


class KeywordBatchAddResponse(BaseModel):
    """批量添加响应"""
    code: int = 0
    message: str = "success"
    data: dict


class KeywordModerationListResponse(BaseModel):
    """Keyword moderation queue response."""
    code: int = 0
    message: str = "success"
    data: list[KeywordResponse]
    total: int


KEYWORD_TYPE_ALIASES = {
    "blacklist": KeywordType.COMPETITOR,
    "whitelist": KeywordType.DEMAND,
}

MATCH_MODE_ALIASES = {
    "contains": MatchMode.FUZZY,
}

STATUS_ALIASES = {
    "active": KeywordStatus.APPROVED,
    "inactive": KeywordStatus.DISCARDED,
}


def normalize_keyword_type(value: str) -> KeywordType:
    """Normalize frontend and backend keyword type names."""
    normalized = (value or "demand").strip()
    if normalized in KEYWORD_TYPE_ALIASES:
        return KEYWORD_TYPE_ALIASES[normalized]
    try:
        return KeywordType(normalized)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid keyword type: {value}")


def normalize_match_mode(value: str | None) -> MatchMode:
    """Normalize frontend and backend match mode names."""
    normalized = (value or "fuzzy").strip()
    if normalized in MATCH_MODE_ALIASES:
        return MATCH_MODE_ALIASES[normalized]
    try:
        return MatchMode(normalized)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid match mode: {value}")


def normalize_keyword_status(value: str) -> KeywordStatus:
    """Normalize frontend and backend keyword status names."""
    normalized = value.strip()
    if normalized in STATUS_ALIASES:
        return STATUS_ALIASES[normalized]
    try:
        return KeywordStatus(normalized)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {value}")


def serialize_keyword(keyword: Keyword) -> KeywordResponse:
    """Serialize a keyword model for API responses."""
    return KeywordResponse(
        id=keyword.id,
        text=keyword.text,
        type=keyword.type.value,
        status=keyword.status.value,
        match_mode=keyword.match_mode.value,
        trigger_count=keyword.trigger_count,
        created_at=keyword.created_at.isoformat(),
    )


@router.get("", response_model=KeywordListResponse)
async def list_keywords(
    page: int = 1,
    page_size: int = Query(default=20, alias="page_size"),
    pageSize: int | None = Query(default=None),
    keyword_type: str | None = Query(default=None, alias="keyword_type"),
    type: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status_filter"),
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> KeywordListResponse:
    """Get list of keywords."""
    page_size = pageSize or page_size
    keyword_type = keyword_type or type
    status_filter = status_filter or status
    query = select(Keyword)

    if keyword_type:
        query = query.where(Keyword.type == normalize_keyword_type(keyword_type))

    if keyword:
        query = query.where(Keyword.text.ilike(f"%{keyword.strip()}%"))

    if status_filter:
        query = query.where(Keyword.status == normalize_keyword_status(status_filter))

    # Count total
    count_query = select(func.count(Keyword.id))
    if keyword_type:
        count_query = count_query.where(Keyword.type == normalize_keyword_type(keyword_type))
    if keyword:
        count_query = count_query.where(Keyword.text.ilike(f"%{keyword.strip()}%"))
    if status_filter:
        count_query = count_query.where(Keyword.status == normalize_keyword_status(status_filter))

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Pagination
    offset = (page - 1) * page_size
    query = query.order_by(Keyword.trigger_count.desc()).offset(offset).limit(page_size)

    result = await db.execute(query)
    keywords = result.scalars().all()

    data = [serialize_keyword(kw) for kw in keywords]

    return KeywordListResponse(data=data, total=total)


@router.post("", response_model=KeywordResponse, status_code=status.HTTP_201_CREATED)
async def create_keyword(
    keyword: KeywordCreate,
    db: AsyncSession = Depends(get_db),
) -> KeywordResponse:
    """Create a new keyword."""
    text = (keyword.text or keyword.word or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Keyword text cannot be empty")

    new_keyword = Keyword(
        text=text,
        type=normalize_keyword_type(keyword.type),
        status=KeywordStatus.PENDING,
        match_mode=normalize_match_mode(keyword.match_mode or keyword.matchMode),
    )

    db.add(new_keyword)
    await db.commit()
    await db.refresh(new_keyword)

    return serialize_keyword(new_keyword)


@router.post("/generate", response_model=KeywordGenerateResponse)
async def generate_keywords(
    request: KeywordGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> KeywordGenerateResponse:
    """
    AI生成关键词（单个分类）

    - 用户在页面点击生成后调用此接口
    - 返回生成的关键词列表（需要用户确认后才加入数据库）
    - 自动排除已存在的关键词
    """
    # 验证分类
    if request.category not in [c.value for c in KeywordType]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category. Must be one of: {[c.value for c in KeywordType]}"
        )

    # 获取已存在的关键词文本（用于去重）
    existing_keywords: set[str] = set()
    if request.exclude_existing:
        result = await db.execute(select(Keyword.text))
        existing_keywords = {kw.lower() for kw in result.scalars().all()}

    # 初始化LLM客户端和关键词生成器
    llm_client = LLMClient(provider=LLMProvider.OPENAI)
    generator = KeywordGenerator(llm_client=llm_client)

    # 调用AI生成
    keywords = await generator.generate(
        category=request.category,
        count=request.count,
    )

    # 过滤已存在的关键词
    filtered_keywords = [
        GeneratedKeywordItem(
            text=kw.text,
            type=kw.type.value,
            is_new=True,
        )
        for kw in keywords
        if kw.text.lower() not in existing_keywords
    ]

    # 检查是否有重复生成（AI自己产生的重复）
    seen = set()
    unique_keywords = []
    for kw in filtered_keywords:
        if kw.text.lower() not in seen:
            seen.add(kw.text.lower())
            unique_keywords.append(kw)

    return KeywordGenerateResponse(
        code=0,
        message="success",
        data={
            "category": request.category,
            "total_generated": len(keywords),
            "total_unique": len(unique_keywords),
            "total_excluded": len(keywords) - len(unique_keywords),
            "keywords": [kw.model_dump() for kw in unique_keywords],
        }
    )


@router.post("/generate/batch", response_model=KeywordGenerateResponse)
async def batch_generate_keywords(
    request: KeywordBatchGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> KeywordGenerateResponse:
    """
    AI批量生成关键词（群搜索场景）

    - 一次性生成多个分类的关键词
    - 自动去重（去除已存在的关键词和生成过程中的重复）
    - 返回所有关键词，不直接入库
    """
    # 验证分类
    valid_counts = {}
    for cat, count in request.counts.items():
        if cat in [c.value for c in KeywordType]:
            valid_counts[cat] = min(count, 50)  # 限制最大50个
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid category '{cat}'. Must be one of: {[c.value for c in KeywordType]}"
            )

    # 获取已存在的关键词文本（用于去重）
    result = await db.execute(select(Keyword.text))
    existing_keywords: set[str] = {kw.lower() for kw in result.scalars().all()}

    # 初始化LLM客户端和关键词生成器
    llm_client = LLMClient(provider=LLMProvider.OPENAI)
    generator = KeywordGenerator(llm_client=llm_client)

    # 批量生成
    all_results = await generator.generate_all(counts=valid_counts)

    # 合并并去重
    seen = set()
    all_keywords: list[GeneratedKeywordItem] = []
    total_generated = 0
    total_excluded = 0

    for category, keywords in all_results.items():
        for kw in keywords:
            total_generated += 1
            kw_text_lower = kw.text.lower()

            # 排除已存在的
            if kw_text_lower in existing_keywords:
                total_excluded += 1
                continue

            # 排除本次生成中的重复
            if kw_text_lower in seen:
                continue

            seen.add(kw_text_lower)
            all_keywords.append(GeneratedKeywordItem(
                text=kw.text,
                type=kw.type.value,
                is_new=True,
            ))

    return KeywordGenerateResponse(
        code=0,
        message="success",
        data={
            "categories": list(valid_counts.keys()),
            "counts_per_category": valid_counts,
            "total_generated": total_generated,
            "total_unique": len(all_keywords),
            "total_excluded": total_excluded,
            "keywords": [kw.model_dump() for kw in all_keywords],
        }
    )


@router.post("/batch-add", response_model=KeywordBatchAddResponse)
async def batch_add_keywords(
    request: KeywordBatchAddRequest,
    db: AsyncSession = Depends(get_db),
) -> KeywordBatchAddResponse:
    """
    批量添加关键词（用户确认后调用）

    - 将用户选中的AI生成关键词批量加入数据库
    - 状态为 pending，等待审核
    """
    request.keywords = request.keywords or request.words
    request.category = request.type or request.category
    request.match_mode = normalize_match_mode(request.match_mode or request.matchMode)

    if not request.keywords:
        raise HTTPException(status_code=400, detail="Keywords list cannot be empty")

    if len(request.keywords) > 100:
        raise HTTPException(status_code=400, detail="Cannot add more than 100 keywords at once")

    # 验证分类
    if request.category not in [c.value for c in KeywordType]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category. Must be one of: {[c.value for c in KeywordType]}"
        )

    try:
        ktype = KeywordType(request.category)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid keyword type")

    # 获取已存在的关键词
    result = await db.execute(select(Keyword.text))
    existing_keywords: set[str] = {kw.lower() for kw in result.scalars().all()}

    # 添加新关键词
    added = []
    skipped = []

    for kw_text in request.keywords:
        kw_text_clean = kw_text.strip()
        if not kw_text_clean:
            continue

        if kw_text_clean.lower() in existing_keywords:
            skipped.append(kw_text_clean)
            continue

        keyword = Keyword(
            text=kw_text_clean,
            type=ktype,
            status=KeywordStatus.PENDING,
            match_mode=request.match_mode,
        )
        db.add(keyword)
        added.append(kw_text_clean)
        existing_keywords.add(kw_text_clean.lower())

    await db.commit()

    return KeywordBatchAddResponse(
        code=0,
        message="success",
        data={
            "added_count": len(added),
            "skipped_count": len(skipped),
            "added_keywords": added,
            "skipped_keywords": skipped,
        }
    )


@router.get("/moderation", response_model=KeywordModerationListResponse)
async def list_moderation_keywords(
    page: int = 1,
    page_size: int = Query(default=20, alias="page_size"),
    pageSize: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> KeywordModerationListResponse:
    """Get keywords waiting for moderation."""
    page_size = pageSize or page_size
    count_query = select(func.count(Keyword.id)).where(Keyword.status == KeywordStatus.PENDING)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    offset = (page - 1) * page_size
    result = await db.execute(
        select(Keyword)
        .where(Keyword.status == KeywordStatus.PENDING)
        .order_by(Keyword.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    return KeywordModerationListResponse(
        data=[serialize_keyword(keyword) for keyword in result.scalars().all()],
        total=total,
    )


@router.post("/moderation/{keyword_id}/approve", response_model=dict)
async def approve_moderation_keyword(
    keyword_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve a pending keyword."""
    result = await db.execute(select(Keyword).where(Keyword.id == keyword_id))
    keyword = result.scalar_one_or_none()
    if not keyword:
        raise HTTPException(status_code=404, detail="Keyword not found")

    keyword.status = KeywordStatus.APPROVED
    await db.commit()
    await db.refresh(keyword)

    return {
        "code": 0,
        "message": "success",
        "data": serialize_keyword(keyword).model_dump(),
    }


@router.post("/moderation/{keyword_id}/reject", response_model=dict)
async def reject_moderation_keyword(
    keyword_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reject a pending keyword."""
    result = await db.execute(select(Keyword).where(Keyword.id == keyword_id))
    keyword = result.scalar_one_or_none()
    if not keyword:
        raise HTTPException(status_code=404, detail="Keyword not found")

    keyword.status = KeywordStatus.DISCARDED
    await db.commit()

    return {
        "code": 0,
        "message": "success",
        "data": None,
    }


@router.get("/stats/summary", response_model=dict)
async def get_keyword_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取关键词统计信息"""
    # 总数
    total_result = await db.execute(select(func.count(Keyword.id)))
    total = total_result.scalar() or 0

    # 各状态统计
    status_counts = {}
    for status in KeywordStatus:
        count_result = await db.execute(
            select(func.count(Keyword.id)).where(Keyword.status == status)
        )
        status_counts[status.value] = count_result.scalar() or 0

    # 各分类统计
    type_counts = {}
    for ktype in KeywordType:
        count_result = await db.execute(
            select(func.count(Keyword.id)).where(Keyword.type == ktype)
        )
        type_counts[ktype.value] = count_result.scalar() or 0

    # 总触发次数
    trigger_result = await db.execute(select(func.sum(Keyword.trigger_count)))
    total_triggers = trigger_result.scalar() or 0

    return {
        "code": 0,
        "data": {
            "total_keywords": total,
            "total_triggers": total_triggers,
            "by_status": status_counts,
            "by_type": type_counts,
        }
    }


@router.get("/{keyword_id}", response_model=KeywordResponse)
async def get_keyword(
    keyword_id: int,
    db: AsyncSession = Depends(get_db),
) -> KeywordResponse:
    """Get keyword by ID."""
    result = await db.execute(select(Keyword).where(Keyword.id == keyword_id))
    keyword = result.scalar_one_or_none()

    if not keyword:
        raise HTTPException(status_code=404, detail="Keyword not found")

    return serialize_keyword(keyword)


@router.put("/{keyword_id}", response_model=KeywordResponse)
async def update_keyword(
    keyword_id: int,
    keyword: KeywordUpdate,
    db: AsyncSession = Depends(get_db),
) -> KeywordResponse:
    """Update keyword."""
    result = await db.execute(select(Keyword).where(Keyword.id == keyword_id))
    db_keyword = result.scalar_one_or_none()

    if not db_keyword:
        raise HTTPException(status_code=404, detail="Keyword not found")

    text = keyword.text if keyword.text is not None else keyword.word
    if text is not None:
        text = text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Keyword text cannot be empty")
        db_keyword.text = text
    if keyword.status is not None:
        db_keyword.status = normalize_keyword_status(keyword.status)
    if keyword.type is not None:
        db_keyword.type = normalize_keyword_type(keyword.type)
    match_mode = keyword.match_mode if keyword.match_mode is not None else keyword.matchMode
    if match_mode is not None:
        db_keyword.match_mode = normalize_match_mode(match_mode)

    await db.commit()
    await db.refresh(db_keyword)

    return serialize_keyword(db_keyword)


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_keyword(
    keyword_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete keyword."""
    result = await db.execute(select(Keyword).where(Keyword.id == keyword_id))
    keyword = result.scalar_one_or_none()

    if not keyword:
        raise HTTPException(status_code=404, detail="Keyword not found")

    await db.delete(keyword)
    await db.commit()
