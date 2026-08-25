"""
Acquisition API Router

RESTful API for acquisition tracking, messages, triggers, and guide flows.
"""

import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ai.keyword_generator import normalize_keyword_text
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.config import settings
from app.core.database import get_db
from app.modules.acquisition.models import (
    GroupSearchRecord,
    AcquisitionTracking,
    AcquisitionMessage,
    KeywordTrigger,
    TriggerRecord,
    TriggerAction,
    TriggerType,
    GuideFlow,
    GuideState,
    MessageTemplate,
    MessageType,
)


router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class SearchRecordCreate(BaseModel):
    """Group search record creation request."""
    keyword: str = Field(..., description="Search keyword")
    group_id: int = Field(..., description="Group ID")
    group_title: Optional[str] = Field(None, description="Group title")
    member_count: Optional[int] = Field(None, description="Member count")


class MessageCreate(BaseModel):
    """Message creation request."""
    account_id: int = Field(..., description="Account ID")
    group_id: int = Field(..., description="Group ID")
    content: Optional[str] = Field(None, description="Message content")
    message_type: str = Field(default="interaction", description="Message type")
    message_id: Optional[int] = Field(None, description="Telegram message ID")


class TriggerRecordCreate(BaseModel):
    """Trigger record creation request."""
    trigger_id: int = Field(..., description="Trigger ID")
    user_id: int = Field(..., description="User ID")
    group_id: Optional[int] = Field(None, description="Group ID")
    message_id: Optional[int] = Field(None, description="Message ID")
    matched_keyword: str = Field(..., description="Matched keyword")
    user_message: Optional[str] = Field(None, description="User's message")
    action_taken: str = Field(..., description="Action taken")
    reply_content: Optional[str] = Field(None, description="Reply content")


class GuideFlowUpdate(BaseModel):
    """Guide flow update request."""
    user_id: int = Field(..., description="User ID")
    state: str = Field(..., description="New state")
    step: Optional[int] = Field(None, description="Current step")
    steps_completed: Optional[list[str]] = Field(None, description="Completed steps")


class TrackingCreate(BaseModel):
    """Tracking creation request."""
    tracking_code: str = Field(..., description="Tracking code")
    user_id: Optional[int] = Field(None, description="User ID")
    source_type: Optional[str] = Field(None, description="Source type")
    campaign_name: Optional[str] = Field(None, description="Campaign name")
    group_id: Optional[int] = Field(None, description="Group ID")
    keyword: Optional[str] = Field(None, description="Trigger keyword")
    bot_id: Optional[str] = Field(None, description="Bot account ID")


class TrackingUpdate(BaseModel):
    """Tracking update request."""
    user_id: Optional[int] = Field(None, description="User ID")
    converted: Optional[bool] = Field(None, description="Converted flag")
    click_at: Optional[str] = Field(None, description="Click timestamp")
    registered_at: Optional[str] = Field(None, description="Registered timestamp")
    converted_at: Optional[str] = Field(None, description="Converted timestamp")


class KeywordTriggerCreate(BaseModel):
    """Keyword reply trigger creation request."""
    keyword_id: Optional[int] = Field(None, description="Base keyword ID")
    keyword_text: str = Field(..., max_length=255, description="Keyword text")
    trigger_type: str = Field(default="keyword", description="Trigger type")
    action: str = Field(default="send_private", description="Reply action")
    template_id: Optional[int] = Field(None, description="Message template ID")
    reply_content: Optional[str] = Field(None, max_length=5000, description="Inline reply content")
    use_ai_reply: bool = Field(default=True, description="Use AI reply")
    cooldown_seconds: int = Field(default=300, ge=0, description="Cooldown in seconds")
    max_triggers_per_user: int = Field(default=5, ge=1, description="Max triggers per user")
    max_triggers_per_group: int = Field(default=10, ge=1, description="Max triggers per group")
    priority: int = Field(default=0, ge=0, le=999, description="Priority")
    requires_review: bool = Field(default=False, description="Require manual review before execution")
    enabled: bool = Field(default=True)


class KeywordTriggerUpdate(BaseModel):
    """Keyword reply trigger update request."""
    keyword_id: Optional[int] = None
    keyword_text: Optional[str] = Field(None, max_length=255)
    trigger_type: Optional[str] = None
    action: Optional[str] = None
    template_id: Optional[int] = None
    reply_content: Optional[str] = Field(None, max_length=5000)
    use_ai_reply: Optional[bool] = None
    cooldown_seconds: Optional[int] = Field(None, ge=0)
    max_triggers_per_user: Optional[int] = Field(None, ge=1)
    max_triggers_per_group: Optional[int] = Field(None, ge=1)
    priority: Optional[int] = Field(None, ge=0, le=999)
    requires_review: Optional[bool] = None
    enabled: Optional[bool] = None


class KeywordTriggerGenerateRequest(BaseModel):
    """AI-generate marketing trigger keywords into the review queue."""

    category: str = Field(default="demand", max_length=50, description="Generation category")
    count: int = Field(default=20, ge=1, le=50)
    action: str = Field(default="send_private", description="Default action for generated triggers")
    use_ai_reply: bool = Field(default=False)
    cooldown_seconds: int = Field(default=300, ge=0)
    max_triggers_per_user: int = Field(default=5, ge=1)
    max_triggers_per_group: int = Field(default=10, ge=1)
    priority: int = Field(default=100, ge=0, le=999)


class MessageTemplateCreate(BaseModel):
    """Reusable marketing reply template creation request."""

    name: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1, max_length=5000)
    message_type: str = Field(default=MessageType.GUIDE.value, max_length=50)
    template_variables: Optional[str] = Field(
        default="user_name,group_name,bot_name,register_link,keyword",
        max_length=500,
    )
    cooldown_seconds: int = Field(default=300, ge=0)
    max_uses_per_day: int = Field(default=100, ge=1)
    enabled: bool = True


class MessageTemplateUpdate(BaseModel):
    """Reusable marketing reply template update request."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    content: Optional[str] = Field(None, min_length=1, max_length=5000)
    message_type: Optional[str] = Field(None, max_length=50)
    template_variables: Optional[str] = Field(None, max_length=500)
    cooldown_seconds: Optional[int] = Field(None, ge=0)
    max_uses_per_day: Optional[int] = Field(None, ge=1)
    enabled: Optional[bool] = None


class KeywordTriggerBatchTemplateRequest(BaseModel):
    """Bind one reusable template to many keyword triggers."""

    trigger_ids: list[int] = Field(..., min_length=1, max_length=200)
    template_id: int = Field(..., ge=1)
    reply_target: str = Field(default="private", pattern="^(private|group)$")
    enabled: Optional[bool] = None


def _trigger_to_response(trigger: KeywordTrigger) -> dict:
    """Serialize keyword trigger configuration for API responses."""
    template = trigger.__dict__.get("template")
    return {
        "id": trigger.id,
        "keyword_id": trigger.keyword_id,
        "keyword_text": trigger.keyword_text,
        "trigger_type": trigger.trigger_type.value,
        "action": trigger.action.value,
        "template_id": trigger.template_id,
        "template_name": template.name if template else None,
        "reply_content": template.content if template else None,
        "use_ai_reply": trigger.use_ai_reply,
        "cooldown_seconds": trigger.cooldown_seconds,
        "max_triggers_per_user": trigger.max_triggers_per_user,
        "max_triggers_per_group": trigger.max_triggers_per_group,
        "priority": trigger.priority,
        "requires_review": getattr(trigger, "requires_review", False),
        "enabled": trigger.enabled,
        "created_at": trigger.created_at.isoformat() if trigger.created_at else "",
    }


def _template_to_response(template: MessageTemplate, usage_count: int | None = None) -> dict:
    """Serialize a reusable message template for API responses."""
    message_type = template.message_type.value if hasattr(template.message_type, "value") else template.message_type
    return {
        "id": template.id,
        "name": template.name,
        "content": template.content,
        "template_variables": template.template_variables,
        "message_type": message_type,
        "cooldown_seconds": template.cooldown_seconds,
        "max_uses_per_day": template.max_uses_per_day,
        "enabled": template.enabled,
        "usage_count": usage_count or 0,
        "created_at": template.created_at.isoformat() if template.created_at else "",
        "updated_at": template.updated_at.isoformat() if template.updated_at else "",
    }


def _normalize_reply_content(content: Optional[str]) -> Optional[str]:
    """Normalize optional inline reply content."""
    if content is None:
        return None
    content = content.strip()
    return content or None


async def _save_inline_reply_template(
    db: AsyncSession,
    content: Optional[str],
    *,
    template_id: Optional[int] = None,
    keyword_text: str,
) -> Optional[int]:
    """Create or update the inline reply template used by a keyword trigger."""
    content = _normalize_reply_content(content)
    if not content:
        return None

    template = None
    if template_id:
        result = await db.execute(select(MessageTemplate).where(MessageTemplate.id == template_id))
        template = result.scalar_one_or_none()

    if template:
        template.content = content
        template.name = f"关键词回复 - {keyword_text[:40]}"
        template.enabled = True
        template.updated_at = datetime.utcnow()
    else:
        template = MessageTemplate(
            name=f"关键词回复 - {keyword_text[:40]}",
            content=content,
            template_variables="user_name,group_name,bot_name,register_link,keyword",
            message_type=MessageType.GUIDE,
            enabled=True,
        )
        db.add(template)
        await db.flush()

    return template.id


MARKETING_TRIGGER_PROMPTS = {
    "intent": """生成 Telegram 群内“营销触发词”，用于识别用户表达购买、寻找服务、寻找解决方案的自然发言片段。

关键定义：
1. 这是“群内消息触发词”，不是“搜群关键词”
2. 只输出短触发词，每行一个
3. 纯中文2到4字，英文或中英文混合不超过16位
4. 必须是跨行业通用的用户意图，不要绑定VPN、代理、机场、节点或任何单一行业
5. 不要输出行业词、平台词、地区词、群名、频道名、品牌名、竞品名
6. 不要输出广告话术、完整句子、违法灰产色情赌博诈骗相关词

方向示例：求推荐、求方案、找服务、找资源、能定制、能开发、求链接、发我下、谁能做、靠谱吗""",
    "question": """生成 Telegram 群内“营销触发词”，用于识别用户正在咨询开通、使用、配置、教程、客服、售后等问题。

关键定义：
1. 这是“群内消息触发词”，不是“搜群关键词”
2. 只输出短触发词，每行一个
3. 纯中文2到4字，英文或中英文混合不超过16位
4. 覆盖所有行业的咨询意图，不要生成VPN、机场、节点、代理IP相关词
5. 不要输出行业词、平台词、地区词、群名、频道名、品牌名、竞品名
6. 不要输出广告话术、完整句子、违法灰产色情赌博诈骗相关词

方向示例：怎么弄、怎么用、怎么开、怎么配、支持吗、求教程、求客服、有文档、能教吗、用不了""",
    "price": """生成 Telegram 群内“营销触发词”，用于识别用户正在询价、试用、找优惠、下单、付款、售后。

关键定义：
1. 这是“群内消息触发词”，不是“搜群关键词”
2. 只输出短触发词，每行一个
3. 纯中文2到4字，英文或中英文混合不超过16位
4. 覆盖所有行业的价格/交易意图，不要生成VPN、机场、节点、代理IP相关词
5. 不要输出行业词、平台词、地区词、群名、频道名、品牌名、竞品名
6. 不要输出广告话术、完整句子、违法灰产色情赌博诈骗相关词

方向示例：多少钱、求报价、有优惠、能便宜、有折扣、能试用、有试用、怎么付、可退款、有套餐""",
    "pain": """生成 Telegram 群内“营销触发词”，用于识别用户遇到问题、缺资源、不会操作、急需解决方案。

关键定义：
1. 这是“群内消息触发词”，不是“搜群关键词”
2. 只输出短触发词，每行一个
3. 纯中文2到4字，英文或中英文混合不超过16位
4. 覆盖所有行业的痛点/求助意图，不要生成VPN、机场、节点、代理IP相关词
5. 不要输出行业词、平台词、地区词、群名、频道名、品牌名、竞品名
6. 不要输出广告话术、完整句子、违法灰产色情赌博诈骗相关词

方向示例：搞不定、不会弄、没效果、卡住了、缺资源、缺人手、求解法、求帮忙、谁能做、急用""",
    "cooperation": """生成 Telegram 群内“营销触发词”，用于识别用户寻找合作、外包、渠道、货源、定制、代办、资源对接。

关键定义：
1. 这是“群内消息触发词”，不是“搜群关键词”
2. 只输出短触发词，每行一个
3. 纯中文2到4字，英文或中英文混合不超过16位
4. 覆盖所有行业的合作/资源意图，不要生成VPN、机场、节点、代理IP相关词
5. 不要输出行业词、平台词、地区词、群名、频道名、品牌名、竞品名
6. 不要输出广告话术、完整句子、违法灰产色情赌博诈骗相关词

方向示例：找合作、求合作、接单吗、可外包、找外包、找渠道、求渠道、求货源、能代办、可对接""",
    "broad": """生成 Telegram 群内“营销触发词”，用于识别泛需求、轻咨询、求资料、求推荐、求帮忙等弱意图。

关键定义：
1. 这是“群内消息触发词”，不是“搜群关键词”
2. 只输出短触发词，每行一个
3. 纯中文2到4字，英文或中英文混合不超过16位
4. 覆盖所有行业的泛需求，不要生成VPN、机场、节点、代理IP相关词
5. 不要输出行业词、平台词、地区词、群名、频道名、品牌名、竞品名
6. 不要输出广告话术、完整句子、违法灰产色情赌博诈骗相关词

方向示例：有吗、在吗、求一下、私聊我、帮看看、能搞吗、靠谱嘛、哪个好、求一个、发我下""",
}

MARKETING_TRIGGER_FALLBACKS = {
    "intent": [
        "求推荐", "求方案", "求资料", "求工具", "求链接", "找服务", "找资源", "找方案", "能定制", "能开发",
        "谁能做", "有现成", "有案例", "有资源", "有渠道", "发我下", "推荐下", "靠谱吗", "能搞吗", "接单吗",
    ],
    "question": [
        "怎么弄", "怎么用", "怎么开", "怎么配", "怎么接", "怎么买", "怎么付", "怎么试", "支持吗", "有文档",
        "求教程", "有教程", "求客服", "能教吗", "用不了", "打不开", "登不上", "怎么设", "能绑定", "能对接",
    ],
    "price": [
        "多少钱", "价格呢", "求报价", "有优惠", "能便宜", "有折扣", "能试用", "有试用", "月付吗", "年付吗",
        "怎么付", "怎么下", "可退款", "有售后", "有套餐", "便宜点", "可月付", "可年付", "能砍价", "报价下",
    ],
    "pain": [
        "搞不定", "不会弄", "没效果", "卡住了", "缺资源", "缺人手", "求解法", "求帮忙", "谁能做", "急用",
        "出问题", "用不了", "太麻烦", "没头绪", "求救", "帮看看", "不会配", "报错了", "没权限", "太难了",
    ],
    "cooperation": [
        "找合作", "求合作", "接单吗", "可外包", "找外包", "找渠道", "求渠道", "求货源", "能代办", "可对接",
        "招代理", "可分销", "招兼职", "能定制", "能开发", "找团队", "找人做", "可合作", "求对接", "有货源",
    ],
    "broad": [
        "有吗", "在吗", "求一下", "私聊我", "帮看看", "能搞吗", "靠谱嘛", "哪个好", "求一个", "发我下",
        "有推荐", "有人吗", "来一个", "看一下", "了解下", "问一下", "能做吗", "好用吗", "哪家好", "推荐下",
    ],
}

MARKETING_TRIGGER_CATEGORY_ALIASES = {
    "demand": "broad",
    "inquiry": "question",
    "competitor": "cooperation",
}

FORBIDDEN_TRIGGER_TERMS = {
    "博彩", "赌博", "彩票", "盘口", "菠菜", "诈骗", "洗钱", "跑分", "接码", "裸聊", "黄播", "色情", "约炮", "代实名",
    "vpn", "机场", "节点", "翻墙", "梯子", "代理ip", "加速器", "群发", "社工", "频道", "群组", "群",
}


def _normalize_generation_category(category: str) -> str:
    category = MARKETING_TRIGGER_CATEGORY_ALIASES.get(category, category)
    return category if category in MARKETING_TRIGGER_PROMPTS else "broad"


def _validate_trigger_keyword_text(text: str) -> tuple[bool, str | None]:
    compact = re.sub(r"\s+", "", text.strip())
    if not compact:
        return False, "empty"
    if len(compact) < 2:
        return False, "too_short"
    has_latin_or_digit = bool(re.search(r"[A-Za-z0-9]", compact))
    max_length = 16 if has_latin_or_digit else 4
    if len(compact) > max_length:
        return False, "too_long"
    compact_lower = compact.lower()
    if any(term.lower() in compact_lower for term in FORBIDDEN_TRIGGER_TERMS):
        return False, "forbidden"
    if re.search(r"https?://|t\.me/|@", compact, re.IGNORECASE):
        return False, "contact_or_link"
    if re.search(r"[\r\n]", compact):
        return False, "multiline"
    return True, None


def _clean_generated_trigger_line(line: str) -> str:
    text = line.strip()
    text = text.lstrip("0123456789.-*、)）(（ ")
    text = text.strip("`'\"“”‘’，,;； ")
    text = re.split(r"\s*[：:，,；;|]\s*", text, maxsplit=1)[0]
    text = re.split(r"\s+[-–—]\s+", text, maxsplit=1)[0]
    return re.sub(r"\s+", "", text).strip()


def _parse_trigger_generation_response(response: str, existing: set[str], category: str, count: int) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for line in response.splitlines():
        text = _clean_generated_trigger_line(line)
        ok, _reason = _validate_trigger_keyword_text(text)
        if not ok:
            continue
        normalized = normalize_keyword_text(text)
        if normalized in existing or normalized in seen:
            continue
        if text.startswith(("以下", "这里", "触发词", "关键词")) and len(text) > 8:
            continue
        seen.add(normalized)
        results.append(text)
        if len(results) >= count:
            break
    for text in MARKETING_TRIGGER_FALLBACKS[_normalize_generation_category(category)]:
        if len(results) >= count:
            break
        normalized = normalize_keyword_text(text)
        ok, _reason = _validate_trigger_keyword_text(text)
        if ok and normalized not in existing and normalized not in seen:
            seen.add(normalized)
            results.append(text)
    return results[:count]


async def _generate_marketing_trigger_texts(
    *,
    category: str,
    count: int,
    existing_keywords: list[str],
) -> tuple[list[str], bool]:
    category = _normalize_generation_category(category)
    existing = {normalize_keyword_text(item) for item in existing_keywords if item}

    provider = (
        LLMProvider(settings.LLM_PROVIDER)
        if settings.LLM_PROVIDER in {p.value for p in LLMProvider}
        else LLMProvider.OPENAI
    )
    api_key = settings.OPENAI_API_KEY if provider == LLMProvider.OPENAI else settings.ANTHROPIC_API_KEY
    llm_configured = provider == LLMProvider.LOCAL or bool(api_key)
    if not llm_configured:
        return _parse_trigger_generation_response("", existing, category, count), False

    avoid_lines = "\n".join(f"- {item}" for item in existing_keywords[:200])
    prompt = f"""{MARKETING_TRIGGER_PROMPTS[category]}

数据库已有营销触发词如下，禁止重复或生成近似重复：
{avoid_lines or "- 暂无"}

请生成 {count} 个全新的营销触发词，每行一个，只输出触发词。"""

    try:
        llm_client = LLMClient(provider=provider, api_key=api_key)
        response = await llm_client.generate(prompt=prompt, temperature=0.6, max_tokens=500)
        return _parse_trigger_generation_response(response, existing, category, count), True
    except Exception:
        return _parse_trigger_generation_response("", existing, category, count), False


# =============================================================================
# Search Records Endpoints
# =============================================================================

@router.post("/search", status_code=status.HTTP_201_CREATED)
async def create_search_record(
    request: SearchRecordCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a group search operation."""
    record = GroupSearchRecord(
        keyword=request.keyword,
        group_id=request.group_id,
        group_title=request.group_title,
        member_count=request.member_count,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return {
        "code": 0,
        "message": "Search record created",
        "data": {
            "id": record.id,
            "keyword": record.keyword,
            "group_id": record.group_id,
            "found_at": record.found_at.isoformat(),
        }
    }


@router.get("/search")
async def list_search_records(
    keyword: Optional[str] = Query(None, description="Filter by keyword"),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List search records."""
    query = select(GroupSearchRecord)

    if keyword:
        query = query.where(GroupSearchRecord.keyword == keyword)

    query = query.order_by(desc(GroupSearchRecord.found_at)).limit(limit)
    result = await db.execute(query)
    records = result.scalars().all()

    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": r.id,
                "keyword": r.keyword,
                "group_id": r.group_id,
                "group_title": r.group_title,
                "member_count": r.member_count,
                "found_at": r.found_at.isoformat(),
            }
            for r in records
        ]
    }


# =============================================================================
# Messages Endpoints
# =============================================================================

@router.post("/messages", status_code=status.HTTP_201_CREATED)
async def create_message_record(
    request: MessageCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a sent message."""
    message = AcquisitionMessage(
        account_id=request.account_id,
        group_id=request.group_id,
        content=request.content,
        message_type=MessageType(request.message_type),
        message_id=request.message_id,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    return {
        "code": 0,
        "message": "Message recorded",
        "data": {
            "id": message.id,
            "account_id": message.account_id,
            "group_id": message.group_id,
            "sent_at": message.sent_at.isoformat(),
        }
    }


@router.get("/messages/stats")
async def get_message_stats(
    start_date: Optional[str] = Query(None, description="Start date"),
    end_date: Optional[str] = Query(None, description="End date"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get message statistics."""
    query = select(
        MessageType,
        func.count(AcquisitionMessage.id).label("count")
    ).group_by(AcquisitionMessage.message_type)

    result = await db.execute(query)
    stats = {row[0].value: row[1] for row in result.all()}

    total_result = await db.execute(select(func.count(AcquisitionMessage.id)))
    total = total_result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total": total,
            "by_type": stats,
        }
    }


# =============================================================================
# Keyword Trigger Config Endpoints
# =============================================================================

@router.get("/message-templates")
async def list_message_templates(
    message_type: Optional[str] = Query(MessageType.GUIDE.value, description="Filter by template type"),
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    include_inline: bool = Query(False, description="Include per-keyword inline templates"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List reusable marketing reply templates."""
    query = select(MessageTemplate)

    if message_type:
        try:
            message_type_enum = MessageType(message_type)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid message type")
        query = query.where(MessageTemplate.message_type == message_type_enum)

    if enabled is not None:
        query = query.where(MessageTemplate.enabled == enabled)

    if not include_inline:
        query = query.where(~MessageTemplate.name.like("关键词回复 - %"))

    query = query.order_by(desc(MessageTemplate.updated_at), desc(MessageTemplate.id))
    result = await db.execute(query)
    templates = list(result.scalars().all())

    usage_result = await db.execute(
        select(KeywordTrigger.template_id, func.count(KeywordTrigger.id))
        .where(KeywordTrigger.template_id.is_not(None))
        .group_by(KeywordTrigger.template_id)
    )
    usage_counts = {template_id: count for template_id, count in usage_result.all()}

    return {
        "code": 0,
        "message": "success",
        "data": [_template_to_response(item, usage_counts.get(item.id, 0)) for item in templates],
    }


@router.post("/message-templates", status_code=status.HTTP_201_CREATED)
async def create_message_template(
    request: MessageTemplateCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a reusable marketing reply template."""
    try:
        message_type = MessageType(request.message_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid message type")

    template = MessageTemplate(
        name=request.name.strip(),
        content=request.content.strip(),
        template_variables=(request.template_variables or "").strip() or None,
        message_type=message_type,
        cooldown_seconds=request.cooldown_seconds,
        max_uses_per_day=request.max_uses_per_day,
        enabled=request.enabled,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    return {
        "code": 0,
        "message": "Message template created",
        "data": _template_to_response(template),
    }


@router.put("/message-templates/{template_id}")
async def update_message_template(
    template_id: int,
    request: MessageTemplateUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update a reusable marketing reply template."""
    result = await db.execute(select(MessageTemplate).where(MessageTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Message template not found")

    update_data = request.model_dump(exclude_none=True)
    if "message_type" in update_data:
        try:
            update_data["message_type"] = MessageType(update_data["message_type"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid message type")

    for field, value in update_data.items():
        if isinstance(value, str):
            value = value.strip()
        setattr(template, field, value)
    template.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(template)

    usage_result = await db.execute(
        select(func.count(KeywordTrigger.id)).where(KeywordTrigger.template_id == template.id)
    )
    return {
        "code": 0,
        "message": "Message template updated",
        "data": _template_to_response(template, usage_result.scalar() or 0),
    }


@router.delete("/message-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message_template(
    template_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a reusable marketing reply template."""
    result = await db.execute(select(MessageTemplate).where(MessageTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Message template not found")

    trigger_rows = await db.execute(select(KeywordTrigger).where(KeywordTrigger.template_id == template_id))
    for trigger in trigger_rows.scalars().all():
        trigger.template_id = None
    await db.delete(template)
    await db.commit()


@router.get("/keyword-triggers")
async def list_keyword_triggers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: Optional[str] = Query(None, description="Filter by keyword text"),
    action: Optional[str] = Query(None, description="Filter by action"),
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    requires_review: Optional[bool] = Query(None, description="Filter by review status"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List acquisition keyword reply/private-message triggers."""
    query = select(KeywordTrigger).options(selectinload(KeywordTrigger.template))
    count_query = select(func.count(KeywordTrigger.id))

    if keyword:
        pattern = f"%{keyword.strip()}%"
        query = query.where(KeywordTrigger.keyword_text.ilike(pattern))
        count_query = count_query.where(KeywordTrigger.keyword_text.ilike(pattern))

    if action:
        try:
            action_enum = TriggerAction(action)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid trigger action")
        query = query.where(KeywordTrigger.action == action_enum)
        count_query = count_query.where(KeywordTrigger.action == action_enum)

    if enabled is not None:
        query = query.where(KeywordTrigger.enabled == enabled)
        count_query = count_query.where(KeywordTrigger.enabled == enabled)

    if requires_review is not None:
        query = query.where(KeywordTrigger.requires_review == requires_review)
        count_query = count_query.where(KeywordTrigger.requires_review == requires_review)

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    offset = (page - 1) * page_size
    query = query.order_by(desc(KeywordTrigger.priority), desc(KeywordTrigger.id)).offset(offset).limit(page_size)
    result = await db.execute(query)

    return {
        "code": 0,
        "message": "success",
        "data": [_trigger_to_response(trigger) for trigger in result.scalars().all()],
        "total": total,
    }


@router.post("/keyword-triggers/generate", status_code=status.HTTP_201_CREATED)
async def generate_keyword_triggers(
    request: KeywordTriggerGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate marketing trigger keywords and place them into manual review."""
    try:
        action = TriggerAction(request.action)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid trigger action")

    existing_result = await db.execute(select(KeywordTrigger.keyword_text))
    existing_keywords = [item for item in existing_result.scalars().all() if item]
    existing_normalized = {normalize_keyword_text(item) for item in existing_keywords}

    candidate_count = min(50, max(request.count * 3, request.count))
    candidates, llm_configured = await _generate_marketing_trigger_texts(
        category=request.category,
        count=candidate_count,
        existing_keywords=existing_keywords,
    )

    created: list[KeywordTrigger] = []
    skipped_existing: list[str] = []
    skipped_duplicate: list[str] = []
    skipped_invalid: list[str] = []
    seen: set[str] = set()

    for text in candidates:
        if len(created) >= request.count:
            break
        ok, _reason = _validate_trigger_keyword_text(text)
        if not ok:
            skipped_invalid.append(text)
            continue
        normalized = normalize_keyword_text(text)
        if normalized in existing_normalized:
            skipped_existing.append(text)
            continue
        if normalized in seen:
            skipped_duplicate.append(text)
            continue
        seen.add(normalized)
        row = KeywordTrigger(
            keyword_text=text,
            trigger_type=TriggerType.KEYWORD,
            action=action,
            use_ai_reply=request.use_ai_reply or action == TriggerAction.REPLY_AI,
            cooldown_seconds=request.cooldown_seconds,
            max_triggers_per_user=request.max_triggers_per_user,
            max_triggers_per_group=request.max_triggers_per_group,
            priority=request.priority,
            requires_review=True,
            enabled=False,
        )
        db.add(row)
        created.append(row)
        existing_normalized.add(normalized)

    await db.commit()
    for row in created:
        await db.refresh(row)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "requested": request.count,
            "generated": len(candidates),
            "created": len(created),
            "skipped_existing": len(skipped_existing),
            "skipped_duplicate": len(skipped_duplicate),
            "skipped_invalid": len(skipped_invalid),
            "candidate_exhausted": len(created) < request.count,
            "llm_configured": llm_configured,
            "requires_review": True,
            "keywords": [_trigger_to_response(item) for item in created],
        },
    }


@router.post("/keyword-triggers/batch-template")
async def batch_bind_keyword_trigger_template(
    request: KeywordTriggerBatchTemplateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bind one reusable reply template to many marketing keyword triggers."""
    template_result = await db.execute(
        select(MessageTemplate).where(MessageTemplate.id == request.template_id)
    )
    template = template_result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Message template not found")

    trigger_result = await db.execute(
        select(KeywordTrigger)
        .options(selectinload(KeywordTrigger.template))
        .where(KeywordTrigger.id.in_(request.trigger_ids))
    )
    triggers = list(trigger_result.scalars().all())
    found_ids = {trigger.id for trigger in triggers}
    missing_ids = [trigger_id for trigger_id in request.trigger_ids if trigger_id not in found_ids]

    action = TriggerAction.SEND_PRIVATE if request.reply_target == "private" else TriggerAction.REPLY_TEMPLATE
    for trigger in triggers:
        trigger.template_id = template.id
        trigger.action = action
        trigger.use_ai_reply = False
        if request.enabled is not None:
            trigger.enabled = request.enabled and not getattr(trigger, "requires_review", False)

    await db.commit()

    refreshed_result = await db.execute(
        select(KeywordTrigger)
        .options(selectinload(KeywordTrigger.template))
        .where(KeywordTrigger.id.in_(found_ids))
        .order_by(desc(KeywordTrigger.priority), desc(KeywordTrigger.id))
    )
    refreshed = list(refreshed_result.scalars().all())

    return {
        "code": 0,
        "message": "success",
        "data": {
            "updated": len(triggers),
            "missing_ids": missing_ids,
            "template": _template_to_response(template, len(triggers)),
            "triggers": [_trigger_to_response(trigger) for trigger in refreshed],
        },
    }


@router.post("/keyword-triggers", status_code=status.HTTP_201_CREATED)
async def create_keyword_trigger(
    request: KeywordTriggerCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an acquisition keyword reply/private-message trigger."""
    keyword_text = request.keyword_text.strip()
    if not keyword_text:
        raise HTTPException(status_code=400, detail="Keyword text cannot be empty")

    try:
        trigger_type = TriggerType(request.trigger_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid trigger type")

    try:
        action = TriggerAction(request.action)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid trigger action")

    template_id = await _save_inline_reply_template(
        db,
        request.reply_content,
        template_id=request.template_id,
        keyword_text=keyword_text,
    ) or request.template_id

    trigger = KeywordTrigger(
        keyword_id=request.keyword_id,
        keyword_text=keyword_text,
        trigger_type=trigger_type,
        action=action,
        template_id=template_id,
        use_ai_reply=request.use_ai_reply,
        cooldown_seconds=request.cooldown_seconds,
        max_triggers_per_user=request.max_triggers_per_user,
        max_triggers_per_group=request.max_triggers_per_group,
        priority=request.priority,
        requires_review=request.requires_review,
        enabled=request.enabled and not request.requires_review,
    )

    db.add(trigger)
    await db.commit()
    result = await db.execute(
        select(KeywordTrigger)
        .options(selectinload(KeywordTrigger.template))
        .where(KeywordTrigger.id == trigger.id)
    )
    trigger = result.scalar_one()

    return {
        "code": 0,
        "message": "Keyword trigger created",
        "data": _trigger_to_response(trigger),
    }


@router.put("/keyword-triggers/{trigger_id}")
async def update_keyword_trigger(
    trigger_id: int,
    request: KeywordTriggerUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update an acquisition keyword trigger."""
    result = await db.execute(select(KeywordTrigger).where(KeywordTrigger.id == trigger_id))
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Keyword trigger not found")

    update_data = request.model_dump(exclude_none=True)
    if request.reply_content is not None:
        update_data["reply_content"] = request.reply_content
    if "keyword_text" in update_data:
        keyword_text = update_data["keyword_text"].strip()
        if not keyword_text:
            raise HTTPException(status_code=400, detail="Keyword text cannot be empty")
        update_data["keyword_text"] = keyword_text
    else:
        keyword_text = trigger.keyword_text

    if "trigger_type" in update_data:
        try:
            update_data["trigger_type"] = TriggerType(update_data["trigger_type"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid trigger type")

    if "action" in update_data:
        try:
            update_data["action"] = TriggerAction(update_data["action"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid trigger action")

    if "reply_content" in update_data:
        content = update_data.pop("reply_content")
        update_data["template_id"] = await _save_inline_reply_template(
            db,
            content,
            template_id=update_data.get("template_id", trigger.template_id),
            keyword_text=keyword_text,
        )

    if update_data.get("requires_review") is True:
        update_data["enabled"] = False

    for field, value in update_data.items():
        setattr(trigger, field, value)

    await db.commit()
    result = await db.execute(
        select(KeywordTrigger)
        .options(selectinload(KeywordTrigger.template))
        .where(KeywordTrigger.id == trigger.id)
    )
    trigger = result.scalar_one()

    return {
        "code": 0,
        "message": "Keyword trigger updated",
        "data": _trigger_to_response(trigger),
    }


@router.delete("/keyword-triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_keyword_trigger(
    trigger_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an acquisition keyword trigger."""
    result = await db.execute(select(KeywordTrigger).where(KeywordTrigger.id == trigger_id))
    trigger = result.scalar_one_or_none()

    if not trigger:
        raise HTTPException(status_code=404, detail="Keyword trigger not found")

    await db.delete(trigger)
    await db.commit()


# =============================================================================
# Trigger Records Endpoints
# =============================================================================

@router.post("/triggers", status_code=status.HTTP_201_CREATED)
async def create_trigger_record(
    request: TriggerRecordCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Record a trigger event."""
    try:
        action_enum = TriggerAction(request.action_taken)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid action")

    record = TriggerRecord(
        trigger_id=request.trigger_id,
        user_id=request.user_id,
        group_id=request.group_id,
        message_id=request.message_id,
        matched_keyword=request.matched_keyword,
        user_message=request.user_message,
        action_taken=action_enum,
        reply_content=request.reply_content,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return {
        "code": 0,
        "message": "Trigger recorded",
        "data": {
            "id": record.id,
            "trigger_id": record.trigger_id,
            "matched_keyword": record.matched_keyword,
            "action_taken": record.action_taken.value,
            "created_at": record.created_at.isoformat(),
        }
    }


@router.get("/triggers")
async def list_trigger_records(
    trigger_id: Optional[int] = Query(None, description="Filter by trigger ID"),
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List trigger records."""
    query = select(TriggerRecord)

    if trigger_id:
        query = query.where(TriggerRecord.trigger_id == trigger_id)
    if user_id:
        query = query.where(TriggerRecord.user_id == user_id)

    query = query.order_by(desc(TriggerRecord.created_at)).limit(limit)
    result = await db.execute(query)
    records = result.scalars().all()

    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": r.id,
                "trigger_id": r.trigger_id,
                "user_id": r.user_id,
                "group_id": r.group_id,
                "matched_keyword": r.matched_keyword,
                "action_taken": r.action_taken.value,
                "reply_content": r.reply_content,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ]
    }


# =============================================================================
# Guide Flow Endpoints
# =============================================================================

@router.put("/guide-flow")
async def update_guide_flow(
    request: GuideFlowUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create or update a guide flow."""
    result = await db.execute(
        select(GuideFlow).where(GuideFlow.user_id == request.user_id)
    )
    flow = result.scalar_one_or_none()

    if flow:
        flow.state = GuideState(request.state)
        if request.step is not None:
            flow.current_step = request.step
        if request.steps_completed is not None:
            flow.set_completed_steps(request.steps_completed)
        flow.last_message_at = datetime.utcnow()
        if request.state == "closed":
            flow.completed_at = datetime.utcnow()
    else:
        flow = GuideFlow(
            user_id=request.user_id,
            state=GuideState(request.state),
            current_step=request.step or 0,
        )
        if request.steps_completed:
            flow.set_completed_steps(request.steps_completed)
        db.add(flow)

    await db.commit()
    await db.refresh(flow)

    return {
        "code": 0,
        "message": "Guide flow updated",
        "data": {
            "user_id": flow.user_id,
            "state": flow.state.value,
            "current_step": flow.current_step,
            "steps_completed": flow.get_completed_steps(),
        }
    }


@router.get("/guide-flow/{user_id}")
async def get_guide_flow(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get guide flow for a user."""
    result = await db.execute(
        select(GuideFlow).where(GuideFlow.user_id == user_id)
    )
    flow = result.scalar_one_or_none()

    if not flow:
        raise HTTPException(status_code=404, detail="Guide flow not found")

    return {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": flow.user_id,
            "state": flow.state.value,
            "current_step": flow.current_step,
            "steps_completed": flow.get_completed_steps(),
            "started_at": flow.started_at.isoformat(),
            "last_message_at": flow.last_message_at.isoformat(),
        }
    }


# =============================================================================
# Tracking Endpoints
# =============================================================================

@router.post("/track", status_code=status.HTTP_201_CREATED)
async def create_tracking(
    request: TrackingCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new tracking record."""
    tracking = AcquisitionTracking(
        tracking_code=request.tracking_code,
        user_id=request.user_id,
        source_type=request.source_type,
        campaign_name=request.campaign_name,
        group_id=request.group_id,
        keyword=request.keyword,
        bot_id=request.bot_id,
    )
    db.add(tracking)
    await db.commit()
    await db.refresh(tracking)

    return {
        "code": 0,
        "message": "Tracking created",
        "data": {
            "id": tracking.id,
            "tracking_code": tracking.tracking_code,
            "created_at": tracking.created_at.isoformat(),
        }
    }


@router.put("/track/{tracking_code}")
async def update_tracking(
    tracking_code: str,
    request: TrackingUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update a tracking record."""
    result = await db.execute(
        select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == tracking_code)
    )
    tracking = result.scalar_one_or_none()

    if not tracking:
        raise HTTPException(status_code=404, detail="Tracking not found")

    if request.user_id is not None:
        tracking.user_id = request.user_id
    if request.converted is not None:
        tracking.converted = request.converted
    if request.click_at:
        try:
            tracking.click_at = datetime.fromisoformat(request.click_at)
        except ValueError:
            pass
    if request.registered_at:
        try:
            tracking.registered_at = datetime.fromisoformat(request.registered_at)
        except ValueError:
            pass
    if request.converted_at:
        try:
            tracking.converted_at = datetime.fromisoformat(request.converted_at)
        except ValueError:
            pass

    await db.commit()
    await db.refresh(tracking)

    return {
        "code": 0,
        "message": "Tracking updated",
        "data": {
            "tracking_code": tracking.tracking_code,
            "converted": tracking.converted,
        }
    }


@router.get("/track/{tracking_code}")
async def get_tracking(
    tracking_code: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get tracking record by code."""
    result = await db.execute(
        select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == tracking_code)
    )
    tracking = result.scalar_one_or_none()

    if not tracking:
        raise HTTPException(status_code=404, detail="Tracking not found")

    return {
        "code": 0,
        "message": "success",
        "data": {
            "tracking_code": tracking.tracking_code,
            "user_id": tracking.user_id,
            "source_type": tracking.source_type,
            "campaign_name": tracking.campaign_name,
            "group_id": tracking.group_id,
            "keyword": tracking.keyword,
            "converted": tracking.converted,
            "click_at": tracking.click_at.isoformat() if tracking.click_at else None,
            "registered_at": tracking.registered_at.isoformat() if tracking.registered_at else None,
            "converted_at": tracking.converted_at.isoformat() if tracking.converted_at else None,
            "created_at": tracking.created_at.isoformat(),
        }
    }


@router.get("/track")
async def list_tracking(
    converted: Optional[bool] = Query(None, description="Filter by converted"),
    campaign: Optional[str] = Query(None, description="Filter by campaign"),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List tracking records."""
    query = select(AcquisitionTracking)

    if converted is not None:
        query = query.where(AcquisitionTracking.converted == converted)
    if campaign:
        query = query.where(AcquisitionTracking.campaign_name == campaign)

    query = query.order_by(desc(AcquisitionTracking.created_at)).limit(limit)
    result = await db.execute(query)
    records = result.scalars().all()

    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "tracking_code": r.tracking_code,
                "user_id": r.user_id,
                "source_type": r.source_type,
                "campaign_name": r.campaign_name,
                "converted": r.converted,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ]
    }


# =============================================================================
# Statistics Endpoints
# =============================================================================

@router.get("/stats/overview")
async def get_acquisition_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get acquisition statistics overview."""
    total_tracking = await db.execute(select(func.count(AcquisitionTracking.id)))
    total = total_tracking.scalar() or 0

    converted_tracking = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.converted == True)
    )
    converted = converted_tracking.scalar() or 0

    total_messages = await db.execute(select(func.count(AcquisitionMessage.id)))
    messages = total_messages.scalar() or 0

    total_triggers = await db.execute(select(func.count(TriggerRecord.id)))
    triggers = total_triggers.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total_tracking": total,
            "converted_tracking": converted,
            "conversion_rate": round(converted / total * 100, 2) if total > 0 else 0,
            "total_messages": messages,
            "total_triggers": triggers,
        }
    }
