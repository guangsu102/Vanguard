"""Management API for the Telegram private chat inbox."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.automation_settings import get_private_messaging_settings
from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.modules.private_chat.models import PrivateChatConversation, PrivateChatMessage
from app.modules.private_chat.service import (
    publish_private_chat_event,
    queue_outbound_private_message,
    serialize_conversation,
    serialize_private_message,
)

router = APIRouter()


class PrivateChatListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[dict[str, Any]]
    total: int


class PrivateChatDataResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Any


class ConversationUpdateRequest(BaseModel):
    status: Literal["open", "closed"] | None = None
    handling_mode: Literal["auto", "human"] | None = None
    assigned_admin_id: int | None = None


class SendPrivateChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    client_request_id: str = Field(min_length=8, max_length=64)


async def _get_conversation(
    db: AsyncSession,
    conversation_id: int,
) -> PrivateChatConversation:
    conversation = await db.get(PrivateChatConversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Private chat conversation not found")
    return conversation


@router.get("/conversations", response_model=PrivateChatListResponse)
async def list_conversations(
    account_id: int | None = None,
    status_filter: Literal["open", "closed"] | None = Query(default=None, alias="status"),
    handling_mode: Literal["auto", "human"] | None = None,
    unread_only: bool = False,
    keyword: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PrivateChatListResponse:
    query = select(PrivateChatConversation)
    count_query = select(func.count(PrivateChatConversation.id))

    filters = []
    if account_id is not None:
        filters.append(PrivateChatConversation.account_id == account_id)
    if status_filter is not None:
        filters.append(PrivateChatConversation.status == status_filter)
    if handling_mode is not None:
        filters.append(PrivateChatConversation.handling_mode == handling_mode)
    if unread_only:
        filters.append(PrivateChatConversation.unread_count > 0)
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        pattern = f"%{normalized_keyword}%"
        filters.append(
            or_(
                PrivateChatConversation.peer_display_name.ilike(pattern),
                PrivateChatConversation.peer_username.ilike(pattern),
                PrivateChatConversation.last_message_preview.ilike(pattern),
            )
        )
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    rows = await db.execute(
        query.order_by(
            PrivateChatConversation.last_message_at.desc(),
            PrivateChatConversation.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    total = (await db.execute(count_query)).scalar() or 0
    return PrivateChatListResponse(
        data=[serialize_conversation(item) for item in rows.scalars().unique().all()],
        total=total,
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=PrivateChatListResponse,
)
async def list_conversation_messages(
    conversation_id: int,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> PrivateChatListResponse:
    await _get_conversation(db, conversation_id)
    query = select(PrivateChatMessage).where(
        PrivateChatMessage.conversation_id == conversation_id
    )
    if before_id is not None:
        query = query.where(PrivateChatMessage.id < before_id)

    rows = await db.execute(
        query.order_by(PrivateChatMessage.id.desc()).limit(limit)
    )
    messages = list(reversed(rows.scalars().all()))
    total = (
        await db.execute(
            select(func.count(PrivateChatMessage.id)).where(
                PrivateChatMessage.conversation_id == conversation_id
            )
        )
    ).scalar() or 0
    return PrivateChatListResponse(
        data=[serialize_private_message(item) for item in messages],
        total=total,
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=PrivateChatDataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_conversation_message(
    conversation_id: int,
    request: SendPrivateChatMessageRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PrivateChatDataResponse:
    conversation = await _get_conversation(db, conversation_id)
    if conversation.status != "open":
        raise HTTPException(status_code=409, detail="Private chat conversation is closed")

    private_settings = await get_private_messaging_settings(db)
    if not private_settings["manualReplyEnabled"]:
        raise HTTPException(status_code=403, detail="Manual private replies are disabled")

    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message content cannot be blank")
    try:
        conversation, message, created = await queue_outbound_private_message(
            db,
            conversation,
            content=content,
            operator_id=int(current_user["id"]),
            client_request_id=request.client_request_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await db.commit()
    await db.refresh(message)
    await db.refresh(conversation)
    message_data = serialize_private_message(message)
    conversation_data = serialize_conversation(conversation)
    if created:
        await publish_private_chat_event(
            "telegram:private-conversation", conversation_data
        )
        await publish_private_chat_event("telegram:private-message", message_data)
    return PrivateChatDataResponse(
        message="Message queued for delivery" if created else "Message already queued",
        data=message_data,
    )


@router.get("/summary", response_model=PrivateChatDataResponse)
async def get_private_chat_summary(
    db: AsyncSession = Depends(get_db),
) -> PrivateChatDataResponse:
    row = (
        await db.execute(
            select(
                func.count(PrivateChatConversation.id),
                func.coalesce(func.sum(PrivateChatConversation.unread_count), 0),
                func.count(PrivateChatConversation.id).filter(
                    PrivateChatConversation.status == "open"
                ),
            )
        )
    ).one()
    return PrivateChatDataResponse(
        data={
            "conversation_count": int(row[0] or 0),
            "unread_count": int(row[1] or 0),
            "open_count": int(row[2] or 0),
        }
    )


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=PrivateChatDataResponse,
)
async def mark_conversation_read(
    conversation_id: int,
    _current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PrivateChatDataResponse:
    conversation = await _get_conversation(db, conversation_id)
    conversation.unread_count = 0
    await db.commit()
    await db.refresh(conversation)
    data = serialize_conversation(conversation)
    await publish_private_chat_event("telegram:private-conversation", data)
    return PrivateChatDataResponse(message="Conversation marked as read", data=data)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=PrivateChatDataResponse,
)
async def update_conversation(
    conversation_id: int,
    request: ConversationUpdateRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> PrivateChatDataResponse:
    conversation = await _get_conversation(db, conversation_id)
    changes = request.model_dump(exclude_unset=True)

    if "status" in changes:
        conversation.status = changes["status"]
    if "handling_mode" in changes:
        conversation.handling_mode = changes["handling_mode"]
        if changes["handling_mode"] == "human" and "assigned_admin_id" not in changes:
            conversation.assigned_admin_id = int(current_user["id"])
        elif changes["handling_mode"] == "auto":
            conversation.assigned_admin_id = None
    if "assigned_admin_id" in changes:
        conversation.assigned_admin_id = changes["assigned_admin_id"]

    await db.commit()
    await db.refresh(conversation)
    data = serialize_conversation(conversation)
    await publish_private_chat_event("telegram:private-conversation", data)
    return PrivateChatDataResponse(message="Conversation updated", data=data)
