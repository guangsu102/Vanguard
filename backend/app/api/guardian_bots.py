"""
Guardian bot account management API.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.manager import AccountManager
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotHealthStatus,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.database import get_db


router = APIRouter()


class GuardianBotCreate(BaseModel):
    identifier: str = Field(..., min_length=3, max_length=120)
    display_name: Optional[str] = Field(None, max_length=120)
    bot_token: str = Field(..., min_length=10, max_length=255)
    bot_username: Optional[str] = Field(None, max_length=120)
    bot_user_id: Optional[int] = None
    country_code: str = Field(default="US", min_length=2, max_length=2)
    country_name: Optional[str] = Field(None, max_length=50)
    api_config_name: str = Field(default="default", max_length=50)
    enabled: bool = True


class GuardianBotUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=120)
    bot_token: Optional[str] = Field(None, min_length=10, max_length=255)
    bot_username: Optional[str] = Field(None, max_length=120)
    bot_user_id: Optional[int] = None
    health_status: Optional[str] = None
    sync_status: Optional[str] = Field(None, max_length=30)
    permissions_snapshot: Optional[dict] = None
    last_heartbeat_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    enabled: Optional[bool] = None
    is_active: Optional[bool] = None
    status: Optional[str] = None


class GuardianBotResponse(BaseModel):
    id: int
    account_id: int
    identifier: str
    display_name: Optional[str] = None
    account_type: str
    status: str
    is_active: bool
    bot_username: Optional[str] = None
    bot_user_id: Optional[int] = None
    health_status: str
    sync_status: str
    permissions_snapshot: Optional[dict] = None
    last_heartbeat_at: Optional[str] = None
    last_synced_at: Optional[str] = None
    enabled: bool
    created_at: str
    updated_at: str


class GuardianBotListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[GuardianBotResponse]
    total: int


def _serialize_bot(profile: GuardianBotProfile) -> GuardianBotResponse:
    account = profile.account
    permissions = None
    if profile.permissions_snapshot:
        import json

        try:
            permissions = json.loads(profile.permissions_snapshot)
        except Exception:
            permissions = {"raw": profile.permissions_snapshot}

    return GuardianBotResponse(
        id=profile.id,
        account_id=profile.account_id,
        identifier=account.identifier,
        display_name=account.display_name,
        account_type=account.account_type.value,
        status=account.status.value,
        is_active=account.is_active,
        bot_username=profile.bot_username,
        bot_user_id=profile.bot_user_id,
        health_status=profile.health_status.value,
        sync_status=profile.sync_status,
        permissions_snapshot=permissions,
        last_heartbeat_at=profile.last_heartbeat_at.isoformat() if profile.last_heartbeat_at else None,
        last_synced_at=profile.last_synced_at.isoformat() if profile.last_synced_at else None,
        enabled=profile.enabled,
        created_at=profile.created_at.isoformat() if profile.created_at else "",
        updated_at=profile.updated_at.isoformat() if profile.updated_at else "",
    )


@router.get("", response_model=GuardianBotListResponse)
async def list_guardian_bots(
    enabled: Optional[bool] = None,
    health_status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> GuardianBotListResponse:
    query = select(GuardianBotProfile)
    count_query = select(func.count(GuardianBotProfile.id))

    if enabled is not None:
        query = query.where(GuardianBotProfile.enabled == enabled)
        count_query = count_query.where(GuardianBotProfile.enabled == enabled)

    if health_status:
        try:
            enum_value = GuardianBotHealthStatus(health_status)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid health_status")
        query = query.where(GuardianBotProfile.health_status == enum_value)
        count_query = count_query.where(GuardianBotProfile.health_status == enum_value)

    if search:
        pattern = f"%{search.strip()}%"
        query = query.join(TelegramAccount, GuardianBotProfile.account_id == TelegramAccount.id)
        count_query = count_query.join(TelegramAccount, GuardianBotProfile.account_id == TelegramAccount.id)
        query = query.where(
            (TelegramAccount.identifier.ilike(pattern))
            | (TelegramAccount.display_name.ilike(pattern))
            | (GuardianBotProfile.bot_username.ilike(pattern))
        )
        count_query = count_query.where(
            (TelegramAccount.identifier.ilike(pattern))
            | (TelegramAccount.display_name.ilike(pattern))
            | (GuardianBotProfile.bot_username.ilike(pattern))
        )

    rows = await db.execute(query.order_by(desc(GuardianBotProfile.id)).limit(limit))
    total = (await db.execute(count_query)).scalar() or 0
    return GuardianBotListResponse(data=[_serialize_bot(item) for item in rows.scalars().all()], total=total)


@router.post("", response_model=GuardianBotResponse, status_code=status.HTTP_201_CREATED)
async def create_guardian_bot(request: GuardianBotCreate, db: AsyncSession = Depends(get_db)) -> GuardianBotResponse:
    manager = AccountManager(db)
    identifier = request.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")

    account = await manager.create_account(
        phone=None,
        identifier=identifier,
        account_type=AccountType.GUARDIAN_BOT,
        api_config_name=request.api_config_name,
        country_code=request.country_code,
        country_name=request.country_name,
        display_name=request.display_name,
        session_name=f"guardian_bot_{identifier.replace('@', '').replace(' ', '_')}",
    )
    account.status = AccountStatus.IDLE
    account.is_active = request.enabled

    profile = GuardianBotProfile(
        account_id=account.id,
        bot_token=request.bot_token,
        bot_username=request.bot_username,
        bot_user_id=request.bot_user_id,
        enabled=request.enabled,
        health_status=GuardianBotHealthStatus.UNKNOWN,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return _serialize_bot(profile)


@router.get("/{profile_id:int}", response_model=GuardianBotResponse)
async def get_guardian_bot(profile_id: int, db: AsyncSession = Depends(get_db)) -> GuardianBotResponse:
    profile = await db.get(GuardianBotProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Guardian bot not found")
    return _serialize_bot(profile)


@router.put("/{profile_id:int}", response_model=GuardianBotResponse)
async def update_guardian_bot(
    profile_id: int,
    request: GuardianBotUpdate,
    db: AsyncSession = Depends(get_db),
) -> GuardianBotResponse:
    profile = await db.get(GuardianBotProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Guardian bot not found")

    account = profile.account
    data = request.model_dump(exclude_none=True)

    if "display_name" in data:
        account.display_name = data.pop("display_name")
    if "enabled" in data:
        profile.enabled = data.pop("enabled")
    if "is_active" in data:
        account.is_active = data.pop("is_active")
    if "status" in data:
        try:
            account.status = AccountStatus(data.pop("status"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid account status")
    if "health_status" in data:
        try:
            profile.health_status = GuardianBotHealthStatus(data.pop("health_status"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid health_status")
    if "permissions_snapshot" in data:
        import json

        profile.permissions_snapshot = json.dumps(data.pop("permissions_snapshot"), ensure_ascii=False)

    for field, value in data.items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)
    return _serialize_bot(profile)
