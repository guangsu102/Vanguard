"""
Moderation Review API Router

RESTful API for violation records and keyword review workflow.

Flow:
1. Violations are recorded by the guardian bot
2. Admin can query violation samples
3. Admin triggers AI to generate keyword suggestions from samples
4. Suggestions enter review queue (PENDING)
5. Admin approves/rejects suggestions
6. Approved keywords are added to the moderation sensitive keyword library
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.ai.moderation_ai import generate_sensitive_keywords
from app.modules.guardian.models import (
    KeywordSuggestion as KeywordSuggestionModel,
    ModerationSensitiveKeyword,
    SensitiveKeywordSource,
    Violation,
    ViolationAction,
    ViolationLevel,
)


router = APIRouter(tags=["审核管理"])


# =============================================================================
# Enums
# =============================================================================

class ReviewStatus(str, Enum):
    """Review status for keyword suggestions."""
    PENDING = "pending"      # 待审核
    APPROVED = "approved"    # 已批准
    REJECTED = "rejected"    # 已拒绝


# =============================================================================
# Request/Response Models
# =============================================================================

class ViolationCreate(BaseModel):
    """Create violation record."""
    user_id: int
    group_id: int
    rule_type: str = Field(..., description="规则类型: keyword, domain, frequency")
    rule_pattern: Optional[str] = None
    content: Optional[str] = None
    action_taken: str = Field(..., description="处理动作: warn, mute, ban, kick")
    action_duration: Optional[int] = None


class ViolationResponse(BaseModel):
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
    data: list[ViolationResponse]
    total: int


class KeywordSuggestionCreate(BaseModel):
    """Create keyword suggestion from samples."""
    samples: list[str] = Field(..., min_length=1, max_length=100, description="违规样本列表")
    category: str = Field(default="competitor", description="关键词分类")
    match_mode: str = Field(default="fuzzy", description="匹配模式")


class KeywordSuggestionResponse(BaseModel):
    """Keyword suggestion item."""
    id: int
    keyword: str
    category: str
    confidence: float
    source_sample: str
    status: str
    created_at: str


class KeywordSuggestionListResponse(BaseModel):
    """Keyword suggestion list response."""
    code: int = 0
    message: str = "success"
    data: list[KeywordSuggestionResponse]
    total: int


class BatchReviewRequest(BaseModel):
    """Batch review request."""
    suggestion_ids: list[int] = Field(..., min_length=1, max_length=50)
    action: str = Field(..., description="操作: approve, reject")


class BatchReviewResponse(BaseModel):
    """Batch review response."""
    code: int = 0
    message: str = "success"
    data: dict


class ReviewStatsResponse(BaseModel):
    """Review statistics response."""
    code: int = 0
    message: str = "success"
    data: dict


# =============================================================================
# Database-backed suggestion storage
# =============================================================================
# =============================================================================
# Violation APIs
# =============================================================================

@router.get("/violations", response_model=ViolationListResponse)
async def list_violations(
    page: int = 1,
    page_size: int = 20,
    group_id: Optional[int] = None,
    rule_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> ViolationListResponse:
    """
    获取违规记录列表

    - 支持按群组、规则类型、时间范围筛选
    - 分页返回
    """
    query = select(Violation)

    if group_id:
        query = query.where(Violation.group_id == group_id)
    if rule_type:
        query = query.where(Violation.rule_type == rule_type)

    # Count total
    count_query = select(func.count(Violation.id))
    if group_id:
        count_query = count_query.where(Violation.group_id == group_id)
    if rule_type:
        count_query = count_query.where(Violation.rule_type == rule_type)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Pagination
    offset = (page - 1) * page_size
    query = query.order_by(desc(Violation.created_at)).offset(offset).limit(page_size)

    result = await db.execute(query)
    violations = result.scalars().all()

    data = [
        ViolationResponse(
            id=v.id,
            user_id=v.user_id,
            group_id=v.group_id,
            rule_type=v.rule_type,
            rule_pattern=v.rule_pattern,
            content=v.content,
            action_taken=v.action_taken.value,
            action_duration=v.action_duration,
            created_at=v.created_at.isoformat(),
        )
        for v in violations
    ]

    return ViolationListResponse(data=data, total=total)


@router.post("/violations", response_model=ViolationResponse, status_code=status.HTTP_201_CREATED)
async def create_violation(
    violation: ViolationCreate,
    db: AsyncSession = Depends(get_db),
) -> ViolationResponse:
    """
    记录违规

    - 由守护 Bot 在检测到违规时调用
    - 保存违规记录用于后续分析
    """
    new_violation = Violation(
        user_id=violation.user_id,
        group_id=violation.group_id,
        rule_type=violation.rule_type,
        rule_pattern=violation.rule_pattern,
        content=violation.content,
        action_taken=violation.action_taken,
        action_duration=violation.action_duration,
    )

    db.add(new_violation)
    await db.commit()
    await db.refresh(new_violation)

    return ViolationResponse(
        id=new_violation.id,
        user_id=new_violation.user_id,
        group_id=new_violation.group_id,
        rule_type=new_violation.rule_type,
        rule_pattern=new_violation.rule_pattern,
        content=new_violation.content,
        action_taken=new_violation.action_taken.value,
        action_duration=new_violation.action_duration,
        created_at=new_violation.created_at.isoformat(),
    )


@router.get("/violations/export")
async def export_violations(
    group_id: Optional[int] = None,
    rule_type: Optional[str] = None,
    limit: int = 1000,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    导出版权样本

    - 用于 AI 分析生成敏感词
    - 返回纯文本内容列表
    """
    query = select(Violation.content).where(Violation.content.isnot(None))

    if group_id:
        query = query.where(Violation.group_id == group_id)
    if rule_type:
        query = query.where(Violation.rule_type == rule_type)

    query = query.order_by(desc(Violation.created_at)).limit(limit)

    result = await db.execute(query)
    contents = [c for c in result.scalars().all() if c]

    return {
        "code": 0,
        "message": "success",
        "data": {
            "samples": contents,
            "count": len(contents),
        }
    }


# =============================================================================
# Keyword Suggestion APIs
# =============================================================================

@router.post("/suggestions/generate", response_model=dict)
async def generate_keyword_suggestions(
    request: KeywordSuggestionCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    AI 生成敏感词候选

    - 根据违规样本，AI 分析生成候选敏感词
    - 候选词进入审核队列
    - 手动触发，消耗 Token
    """
    if not request.samples:
        raise HTTPException(status_code=400, detail="样本列表不能为空")

    llm_client = LLMClient(provider=LLMProvider.OPENAI)

    try:
        suggestions = await generate_sensitive_keywords(
            samples=request.samples,
            category=request.category,
            llm_client=llm_client,
        )

        added_suggestions = []
        for sug in suggestions:
            record = KeywordSuggestionModel(
                keyword=sug.keyword,
                category=request.category,
                confidence=sug.confidence,
                source_sample=sug.source_sample,
                status=ReviewStatus.PENDING.value,
                created_at=datetime.utcnow(),
            )
            db.add(record)
            added_suggestions.append(record)

        await db.commit()
        for record in added_suggestions:
            await db.refresh(record)

        return {
            "code": 0,
            "message": "success",
            "data": {
                "generated_count": len(added_suggestions),
                "suggestions": [
                    {
                        "id": r.id,
                        "keyword": r.keyword,
                        "category": r.category,
                        "confidence": r.confidence,
                        "source_sample": r.source_sample,
                        "status": r.status,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in added_suggestions
                ],
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI生成失败: {str(e)}")


@router.get("/suggestions", response_model=KeywordSuggestionListResponse)
async def list_suggestions(
    page: int = 1,
    page_size: int = 20,
    status_filter: Optional[str] = None,
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> KeywordSuggestionListResponse:
    """
    获取敏感词候选列表

    - 查看待审核的候选词
    - 支持按状态、分类筛选
    """
    query = select(KeywordSuggestionModel)
    count_query = select(func.count(KeywordSuggestionModel.id))

    if status_filter:
        query = query.where(KeywordSuggestionModel.status == status_filter)
        count_query = count_query.where(KeywordSuggestionModel.status == status_filter)
    if category:
        query = query.where(KeywordSuggestionModel.category == category)
        count_query = count_query.where(KeywordSuggestionModel.category == category)

    total = (await db.execute(count_query)).scalar() or 0
    query = query.order_by(desc(KeywordSuggestionModel.confidence), desc(KeywordSuggestionModel.created_at))
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    suggestions = result.scalars().all()

    data = [
        KeywordSuggestionResponse(
            id=s.id,
            keyword=s.keyword,
            category=s.category,
            confidence=s.confidence,
            source_sample=s.source_sample[:100] + "..." if len(s.source_sample) > 100 else s.source_sample,
            status=s.status,
            created_at=s.created_at.isoformat(),
        )
        for s in suggestions
    ]

    return KeywordSuggestionListResponse(data=data, total=total)


@router.post("/suggestions/{suggestion_id}/approve")
async def approve_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    批准敏感词候选

    - 批准后添加到敏感词库
    - 状态变为 APPROVED
    """
    suggestion = await db.get(KeywordSuggestionModel, suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="候选词不存在")

    if suggestion.status != ReviewStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="只能审核待处理的候选词")

    keyword = ModerationSensitiveKeyword(
        text=suggestion.keyword,
        normalized_text=suggestion.keyword.strip().lower(),
        category=suggestion.category,
        source=SensitiveKeywordSource.AI_SUGGESTION,
        level=ViolationLevel.MEDIUM,
        action=ViolationAction.WARN,
        enabled=True,
        confidence=suggestion.confidence,
        source_sample=suggestion.source_sample,
    )

    suggestion.status = ReviewStatus.APPROVED.value
    db.add(keyword)
    await db.commit()
    await db.refresh(keyword)
    await db.refresh(suggestion)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "suggestion_id": suggestion_id,
            "keyword_id": keyword.id,
            "keyword_text": keyword.text,
            "library": "moderation_sensitive_keywords",
        }
    }


@router.post("/suggestions/{suggestion_id}/reject")
async def reject_suggestion(
    suggestion_id: int,
    reason: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    拒绝敏感词候选

    - 拒绝后标记为 REJECTED
    - 可选填写拒绝原因
    """
    suggestion = await db.get(KeywordSuggestionModel, suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="候选词不存在")

    if suggestion.status != ReviewStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="只能审核待处理的候选词")

    suggestion.status = ReviewStatus.REJECTED.value
    if reason:
        suggestion.reject_reason = reason
    await db.commit()
    await db.refresh(suggestion)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "suggestion_id": suggestion_id,
            "status": ReviewStatus.REJECTED.value,
        }
    }


@router.post("/suggestions/batch-review", response_model=BatchReviewResponse)
async def batch_review_suggestions(
    request: BatchReviewRequest,
    db: AsyncSession = Depends(get_db),
) -> BatchReviewResponse:
    """
    批量审核敏感词候选

    - 批量批准或拒绝
    - 批准后批量添加到敏感词库
    """
    if request.action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="action 必须是 approve 或 reject")

    approved_ids = []
    rejected_ids = []
    failed_ids = []

    for suggestion_id in request.suggestion_ids:
        suggestion = await db.get(KeywordSuggestionModel, suggestion_id)

        if not suggestion:
            failed_ids.append({"id": suggestion_id, "reason": "不存在"})
            continue

        if suggestion.status != ReviewStatus.PENDING.value:
            failed_ids.append({"id": suggestion_id, "reason": "状态不是待处理"})
            continue

        if request.action == "approve":
            keyword = ModerationSensitiveKeyword(
                text=suggestion.keyword,
                normalized_text=suggestion.keyword.strip().lower(),
                category=suggestion.category,
                source=SensitiveKeywordSource.AI_SUGGESTION,
                level=ViolationLevel.MEDIUM,
                action=ViolationAction.WARN,
                enabled=True,
                confidence=suggestion.confidence,
                source_sample=suggestion.source_sample,
            )
            db.add(keyword)
            suggestion.status = ReviewStatus.APPROVED.value
            approved_ids.append(suggestion_id)
        else:
            suggestion.status = ReviewStatus.REJECTED.value
            rejected_ids.append(suggestion_id)

    await db.commit()

    return BatchReviewResponse(
        code=0,
        message="success",
        data={
            "approved_count": len(approved_ids),
            "rejected_count": len(rejected_ids),
            "failed_count": len(failed_ids),
            "approved_ids": approved_ids,
            "rejected_ids": rejected_ids,
            "failed": failed_ids,
        }
    )


@router.delete("/suggestions/{suggestion_id}")
async def delete_suggestion(
    suggestion_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    删除敏感词候选
    """
    suggestion = await db.get(KeywordSuggestionModel, suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="候选词不存在")

    await db.delete(suggestion)
    await db.commit()

    return {
        "code": 0,
        "message": "success",
    }


@router.get("/stats", response_model=ReviewStatsResponse)
async def get_review_stats(db: AsyncSession = Depends(get_db)) -> ReviewStatsResponse:
    """
    获取审核统计
    """
    total = (await db.execute(select(func.count(KeywordSuggestionModel.id)))).scalar() or 0
    pending = (await db.execute(select(func.count(KeywordSuggestionModel.id)).where(KeywordSuggestionModel.status == ReviewStatus.PENDING.value))).scalar() or 0
    approved = (await db.execute(select(func.count(KeywordSuggestionModel.id)).where(KeywordSuggestionModel.status == ReviewStatus.APPROVED.value))).scalar() or 0
    rejected = (await db.execute(select(func.count(KeywordSuggestionModel.id)).where(KeywordSuggestionModel.status == ReviewStatus.REJECTED.value))).scalar() or 0

    result = await db.execute(select(KeywordSuggestionModel.category, KeywordSuggestionModel.status, func.count(KeywordSuggestionModel.id)).group_by(KeywordSuggestionModel.category, KeywordSuggestionModel.status))
    by_category = {}
    for category, status_value, count in result.all():
        by_category.setdefault(category, {"pending": 0, "approved": 0, "rejected": 0})
        by_category[category][status_value] = count

    return ReviewStatsResponse(
        code=0,
        message="success",
        data={
            "total": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "by_category": by_category,
        }
    )
