"""Management API for QQ groups connected through NapCat OneBot 11."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.integrations.qq import OneBotAPIError, OneBotClient
from app.modules.qq.models import (
    QQBotConnection,
    QQGroupCommand,
    QQGroupMessage,
    QQManagedGroup,
)
from app.modules.qq.service import QQEventProcessor, ensure_qq_connection
from app.modules.qq.tasks import execute_qq_command

router = APIRouter()


class QQGroupCreate(BaseModel):
    group_number: str = Field(..., min_length=5, max_length=20, pattern=r"^\d+$")
    local_name: str | None = Field(default=None, max_length=255)


class QQGroupUpdate(BaseModel):
    local_name: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, pattern="^(active|inactive|removed)$")
    monitoring_enabled: bool | None = None
    notifications_enabled: bool | None = None
    auto_recall_enabled: bool | None = None


class QQNotificationCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class QQGroupListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[dict[str, Any]]
    total: int


class QQDataResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Any


async def _configured_connection(db: AsyncSession) -> QQBotConnection:
    account_id = (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
    if (
        not settings.QQ_ONEBOT_ENABLED
        or not account_id
        or not settings.QQ_ONEBOT_ACCESS_TOKEN
    ):
        raise HTTPException(status_code=503, detail="NapCat OneBot is not configured")
    return await ensure_qq_connection(db, account_id)


async def _configured_group(db: AsyncSession, group_id: int) -> QQManagedGroup:
    connection = await _configured_connection(db)
    result = await db.execute(
        select(QQManagedGroup).where(
            QQManagedGroup.id == group_id,
            QQManagedGroup.connection_id == connection.id,
        )
    )
    group = result.scalar_one_or_none()
    if group is None:
        raise HTTPException(status_code=404, detail="QQ group not found")
    return group


def _serialize_group(group: QQManagedGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "connection_id": group.connection_id,
        "group_number": group.group_openid,
        "local_name": group.local_name,
        "status": group.status,
        "monitoring_enabled": group.monitoring_enabled,
        "notifications_enabled": group.notifications_enabled,
        "auto_recall_enabled": group.auto_recall_enabled,
        "receive_all_messages_enabled": group.receive_all_messages_enabled,
        "proactive_messages_enabled": group.proactive_messages_enabled,
        "last_message_at": group.last_message_at.isoformat() if group.last_message_at else None,
        "bot_added_at": group.bot_added_at.isoformat() if group.bot_added_at else None,
        "bot_removed_at": group.bot_removed_at.isoformat() if group.bot_removed_at else None,
        "created_at": group.created_at.isoformat(),
        "updated_at": group.updated_at.isoformat(),
    }


def _serialize_command(command: QQGroupCommand) -> dict[str, Any]:
    return {
        "id": command.id,
        "group_id": command.group_id,
        "command_type": command.command_type,
        "status": command.status,
        "provider_message_id": command.provider_message_id,
        "error_message": command.error_message,
        "created_by": command.created_by,
        "created_at": command.created_at.isoformat(),
        "started_at": command.started_at.isoformat() if command.started_at else None,
        "completed_at": command.completed_at.isoformat() if command.completed_at else None,
    }


@router.get("/connection", response_model=QQDataResponse)
async def get_connection_status(db: AsyncSession = Depends(get_db)) -> QQDataResponse:
    account_id = (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
    connection = None
    if account_id:
        result = await db.execute(
            select(QQBotConnection).where(QQBotConnection.app_id == account_id)
        )
        connection = result.scalar_one_or_none()
    configured = bool(
        settings.QQ_ONEBOT_ENABLED
        and account_id
        and settings.QQ_ONEBOT_ACCESS_TOKEN
        and settings.QQ_ONEBOT_HTTP_URL
        and settings.QQ_ONEBOT_WS_URL
    )
    return QQDataResponse(
        data={
            "configured": configured,
            "provider": "napcat_onebot11",
            "account_id": account_id or None,
            "enabled": connection.enabled if connection else settings.QQ_ONEBOT_ENABLED,
            "status": connection.status if connection else "offline",
            "display_name": connection.display_name if connection else None,
            "last_heartbeat_at": (
                connection.last_heartbeat_at.isoformat()
                if connection and connection.last_heartbeat_at
                else None
            ),
            "last_connected_at": (
                connection.last_connected_at.isoformat()
                if connection and connection.last_connected_at
                else None
            ),
            "last_error": connection.last_error if connection else None,
        }
    )


@router.get("/groups", response_model=QQGroupListResponse)
async def list_groups(
    status_filter: str | None = Query(default=None, alias="status"),
    monitoring_enabled: bool | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> QQGroupListResponse:
    query = select(QQManagedGroup)
    count_query = select(func.count(QQManagedGroup.id))
    account_id = (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
    if account_id:
        query = query.join(QQBotConnection).where(QQBotConnection.app_id == account_id)
        count_query = count_query.join(QQBotConnection).where(
            QQBotConnection.app_id == account_id
        )
    else:
        query = query.where(QQManagedGroup.id < 0)
        count_query = count_query.where(QQManagedGroup.id < 0)
    if status_filter:
        query = query.where(QQManagedGroup.status == status_filter)
        count_query = count_query.where(QQManagedGroup.status == status_filter)
    if monitoring_enabled is not None:
        query = query.where(QQManagedGroup.monitoring_enabled == monitoring_enabled)
        count_query = count_query.where(QQManagedGroup.monitoring_enabled == monitoring_enabled)
    rows = await db.execute(
        query.order_by(desc(QQManagedGroup.last_message_at), desc(QQManagedGroup.id))
        .offset(offset)
        .limit(limit)
    )
    total = (await db.execute(count_query)).scalar() or 0
    return QQGroupListResponse(
        data=[_serialize_group(group) for group in rows.scalars().all()],
        total=total,
    )


@router.post("/groups/sync", response_model=QQDataResponse)
async def sync_groups_from_napcat(
    db: AsyncSession = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
) -> QQDataResponse:
    connection = await _configured_connection(db)
    client = OneBotClient()
    try:
        login = await client.get_login_info()
        logged_account = str(login.get("user_id") or "")
        if logged_account != client.account_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"NapCat logged in as QQ {logged_account or 'unknown'}, "
                    f"expected {client.account_id}"
                ),
            )
        group_rows = await client.get_group_list()
        connection.display_name = str(login.get("nickname") or "").strip() or None
        connection.bot_openid = logged_account
        connection.status = "online"
        connection.last_connected_at = datetime.utcnow()
        connection.last_heartbeat_at = datetime.utcnow()
        connection.last_error = None
        groups = await QQEventProcessor(db).sync_groups(connection, group_rows)
        await db.commit()
        return QQDataResponse(
            message="QQ groups synchronized from NapCat",
            data={"total": len(groups)},
        )
    except HTTPException:
        raise
    except OneBotAPIError as exc:
        connection.status = "error"
        connection.last_error = exc.message[:2000]
        await db.commit()
        raise HTTPException(status_code=502, detail=exc.message) from exc
    finally:
        await client.close()


@router.post("/groups", response_model=QQDataResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    request: QQGroupCreate,
    db: AsyncSession = Depends(get_db),
) -> QQDataResponse:
    connection = await _configured_connection(db)
    existing = await db.execute(
        select(QQManagedGroup).where(
            QQManagedGroup.connection_id == connection.id,
            QQManagedGroup.group_openid == request.group_number.strip(),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="QQ group is already registered")
    group = QQManagedGroup(
        connection_id=connection.id,
        group_openid=request.group_number.strip(),
        local_name=request.local_name.strip() if request.local_name else None,
        receive_all_messages_enabled=True,
        proactive_messages_enabled=True,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return QQDataResponse(message="QQ group registered", data=_serialize_group(group))


@router.patch("/groups/{group_id}", response_model=QQDataResponse)
async def update_group(
    group_id: int,
    request: QQGroupUpdate,
    db: AsyncSession = Depends(get_db),
) -> QQDataResponse:
    group = await _configured_group(db, group_id)
    changes = request.model_dump(exclude_unset=True)
    if "local_name" in changes and changes["local_name"] is not None:
        changes["local_name"] = changes["local_name"].strip() or None
    for key, value in changes.items():
        setattr(group, key, value)
    await db.commit()
    await db.refresh(group)
    return QQDataResponse(message="QQ group updated", data=_serialize_group(group))


@router.get("/groups/{group_id}/messages", response_model=QQGroupListResponse)
async def list_group_messages(
    group_id: int,
    member_qq: str | None = None,
    keyword: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> QQGroupListResponse:
    group = await _configured_group(db, group_id)
    query = select(QQGroupMessage).where(QQGroupMessage.group_id == group_id)
    count_query = select(func.count(QQGroupMessage.id)).where(
        QQGroupMessage.group_id == group_id
    )
    if member_qq:
        query = query.where(QQGroupMessage.member_openid == member_qq)
        count_query = count_query.where(QQGroupMessage.member_openid == member_qq)
    if keyword:
        pattern = f"%{keyword}%"
        query = query.where(QQGroupMessage.content.ilike(pattern))
        count_query = count_query.where(QQGroupMessage.content.ilike(pattern))
    rows = await db.execute(
        query.order_by(desc(QQGroupMessage.occurred_at)).offset(offset).limit(limit)
    )
    total = (await db.execute(count_query)).scalar() or 0
    return QQGroupListResponse(
        data=[
            QQEventProcessor.serialize_message(message, group)
            for message in rows.scalars().all()
        ],
        total=total,
    )


async def _queue_command(command: QQGroupCommand, db: AsyncSession) -> None:
    try:
        execute_qq_command.apply_async(args=[command.id], queue="qq_commands")
    except Exception as exc:
        command.status = "failed"
        command.completed_at = datetime.utcnow()
        command.error_message = f"QQ command queue unavailable: {exc}"[:2000]
        await db.commit()
        raise HTTPException(status_code=503, detail="QQ command queue unavailable") from exc
    command.status = "queued"
    await db.commit()


@router.post(
    "/groups/{group_id}/notifications",
    response_model=QQDataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_group_notification(
    group_id: int,
    request: QQNotificationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> QQDataResponse:
    group = await _configured_group(db, group_id)
    if group.status != "active" or not group.notifications_enabled:
        raise HTTPException(status_code=400, detail="QQ group notifications are disabled")
    command = QQGroupCommand(
        group_id=group.id,
        command_type="notification",
        payload_json=json.dumps(
            {"content": request.content.strip()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        created_by=current_user.get("id"),
    )
    db.add(command)
    await db.commit()
    await _queue_command(command, db)
    return QQDataResponse(message="QQ group notification queued", data=_serialize_command(command))


@router.post(
    "/messages/{message_id}/recall",
    response_model=QQDataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def recall_group_message(
    message_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> QQDataResponse:
    connection = await _configured_connection(db)
    message = await db.get(QQGroupMessage, message_id)
    if message is None or message.group.connection_id != connection.id:
        raise HTTPException(status_code=404, detail="QQ message not found")
    if message.recalled_at is not None:
        raise HTTPException(status_code=400, detail="QQ message has already been recalled")
    command = QQGroupCommand(
        group_id=message.group_id,
        command_type="recall",
        payload_json=json.dumps(
            {"message_id": message.provider_message_id},
            separators=(",", ":"),
        ),
        created_by=current_user.get("id"),
    )
    db.add(command)
    await db.commit()
    await _queue_command(command, db)
    return QQDataResponse(message="QQ message recall queued", data=_serialize_command(command))


@router.get("/commands", response_model=QQGroupListResponse)
async def list_commands(
    group_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> QQGroupListResponse:
    connection = await _configured_connection(db)
    query = select(QQGroupCommand).join(QQManagedGroup).where(
        QQManagedGroup.connection_id == connection.id
    )
    count_query = select(func.count(QQGroupCommand.id)).join(QQManagedGroup).where(
        QQManagedGroup.connection_id == connection.id
    )
    if group_id is not None:
        query = query.where(QQGroupCommand.group_id == group_id)
        count_query = count_query.where(QQGroupCommand.group_id == group_id)
    rows = await db.execute(query.order_by(desc(QQGroupCommand.created_at)).limit(limit))
    total = (await db.execute(count_query)).scalar() or 0
    return QQGroupListResponse(
        data=[_serialize_command(command) for command in rows.scalars().all()],
        total=total,
    )
