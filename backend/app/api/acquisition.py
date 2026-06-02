"""
Acquisition API Router

RESTful API for acquisition tracking, messages, triggers, and guide flows.
"""

from datetime import datetime
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    ConversationContext,
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
        "reply_content": template.content if template else None,
        "use_ai_reply": trigger.use_ai_reply,
        "cooldown_seconds": trigger.cooldown_seconds,
        "max_triggers_per_user": trigger.max_triggers_per_user,
        "max_triggers_per_group": trigger.max_triggers_per_group,
        "priority": trigger.priority,
        "enabled": trigger.enabled,
        "created_at": trigger.created_at.isoformat() if trigger.created_at else "",
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

@router.get("/keyword-triggers")
async def list_keyword_triggers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: Optional[str] = Query(None, description="Filter by keyword text"),
    action: Optional[str] = Query(None, description="Filter by action"),
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
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
        enabled=request.enabled,
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
