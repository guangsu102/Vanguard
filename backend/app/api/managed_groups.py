"""
Managed group binding API for guardian bots.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guardian_validation import ensure_guardian_bot_account
from app.core.database import get_db
from app.core.group.manager import GroupManager
from app.core.group.models import Group
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
    VerificationType,
)


router = APIRouter()


class ManagedGroupBindingCreate(BaseModel):
    group_id: Optional[int] = Field(None, description="内部群ID")
    telegram_group_id: int = Field(..., description="Telegram群组ID")
    title: Optional[str] = None
    username: Optional[str] = None
    member_count: int = 0
    bot_account_id: int
    binding_status: str = Field(default="active")
    bot_role: str = Field(default="admin")
    permissions_snapshot: Optional[dict] = None


class ManagedGroupBindingUpdate(BaseModel):
    binding_status: Optional[str] = None
    bot_role: Optional[str] = None
    permissions_snapshot: Optional[dict] = None
    last_synced_at: Optional[datetime] = None


class ManagedGroupBindingResponse(BaseModel):
    id: int
    group_id: int
    telegram_group_id: int
    title: Optional[str] = None
    username: Optional[str] = None
    member_count: int
    bot_account_id: int
    bot_identifier: str
    bot_display_name: Optional[str] = None
    binding_status: str
    bot_role: str
    permissions_snapshot: Optional[dict] = None
    bound_at: str
    last_synced_at: Optional[str] = None


class ManagedGroupBindingListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[ManagedGroupBindingResponse]
    total: int


def _parse_permissions(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    import json

    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def _serialize_binding(binding: ManagedGroupBinding) -> ManagedGroupBindingResponse:
    return ManagedGroupBindingResponse(
        id=binding.id,
        group_id=binding.group_id,
        telegram_group_id=binding.telegram_group_id,
        title=binding.group.title if binding.group else None,
        username=binding.group.username if binding.group else None,
        member_count=binding.group.member_count if binding.group else 0,
        bot_account_id=binding.bot_account_id,
        bot_identifier=binding.bot_account.identifier if binding.bot_account else "",
        bot_display_name=binding.bot_account.display_name if binding.bot_account else None,
        binding_status=binding.binding_status.value,
        bot_role=binding.bot_role.value,
        permissions_snapshot=_parse_permissions(binding.permissions_snapshot),
        bound_at=binding.bound_at.isoformat() if binding.bound_at else "",
        last_synced_at=binding.last_synced_at.isoformat() if binding.last_synced_at else None,
    )


async def _ensure_default_group_governance(db: AsyncSession, telegram_group_id: int) -> None:
    verification = await db.execute(
        select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == telegram_group_id)
    )
    if verification.scalar_one_or_none() is None:
        db.add(
            GroupVerificationConfig(
                group_id=telegram_group_id,
                enable_verification=False,
                verification_type=VerificationType.CAPTCHA,
                timeout_minutes=5,
                max_attempts=3,
                whitelist_bypass=True,
                auto_kick_unverified=False,
                kick_after_minutes=10,
            )
        )

    moderation = await db.execute(
        select(GroupModerationPolicy).where(GroupModerationPolicy.group_id == telegram_group_id)
    )
    if moderation.scalar_one_or_none() is None:
        db.add(GroupModerationPolicy(group_id=telegram_group_id))

    punishment = await db.execute(
        select(GroupPunishmentPolicy).where(GroupPunishmentPolicy.group_id == telegram_group_id)
    )
    if punishment.scalar_one_or_none() is None:
        db.add(GroupPunishmentPolicy(group_id=telegram_group_id))


@router.get("", response_model=ManagedGroupBindingListResponse)
async def list_managed_groups(
    bot_account_id: Optional[int] = None,
    binding_status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupBindingListResponse:
    query = select(ManagedGroupBinding)
    count_query = select(func.count(ManagedGroupBinding.id))

    if bot_account_id is not None:
        query = query.where(ManagedGroupBinding.bot_account_id == bot_account_id)
        count_query = count_query.where(ManagedGroupBinding.bot_account_id == bot_account_id)

    if binding_status:
        try:
            enum_value = ManagedGroupBindingStatus(binding_status)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid binding_status")
        query = query.where(ManagedGroupBinding.binding_status == enum_value)
        count_query = count_query.where(ManagedGroupBinding.binding_status == enum_value)

    rows = await db.execute(query.order_by(desc(ManagedGroupBinding.id)).limit(limit))
    total = (await db.execute(count_query)).scalar() or 0
    return ManagedGroupBindingListResponse(
        data=[_serialize_binding(item) for item in rows.scalars().all()],
        total=total,
    )


@router.post("", response_model=ManagedGroupBindingResponse, status_code=status.HTTP_201_CREATED)
async def create_managed_group_binding(
    request: ManagedGroupBindingCreate,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupBindingResponse:
    await ensure_guardian_bot_account(db, request.bot_account_id)

    group_manager = GroupManager(db)
    group: Optional[Group] = None
    if request.group_id:
        group = await group_manager.get_group(request.group_id)
    else:
        group = await group_manager.get_group_by_telegram_id(request.telegram_group_id)

    if not group:
        group = await group_manager.add_group(
            group_id=request.telegram_group_id,
            title=request.title,
            username=request.username,
            member_count=request.member_count,
            discovery_source="guardian_binding",
            source_keyword=None,
        )

    exists = await db.execute(select(ManagedGroupBinding).where(ManagedGroupBinding.group_id == group.id))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="This group already has a primary guardian bot")

    import json

    binding = ManagedGroupBinding(
        group_id=group.id,
        telegram_group_id=request.telegram_group_id,
        bot_account_id=request.bot_account_id,
        binding_status=ManagedGroupBindingStatus(request.binding_status),
        bot_role=ManagedGroupBotRole(request.bot_role),
        permissions_snapshot=json.dumps(request.permissions_snapshot, ensure_ascii=False) if request.permissions_snapshot else None,
        last_synced_at=datetime.utcnow(),
    )
    db.add(binding)
    await _ensure_default_group_governance(db, request.telegram_group_id)
    await db.commit()
    await db.refresh(binding)
    return _serialize_binding(binding)


@router.put("/{binding_id:int}", response_model=ManagedGroupBindingResponse)
async def update_managed_group_binding(
    binding_id: int,
    request: ManagedGroupBindingUpdate,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupBindingResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")

    data = request.model_dump(exclude_none=True)
    if "binding_status" in data:
        data["binding_status"] = ManagedGroupBindingStatus(data["binding_status"])
    if "bot_role" in data:
        data["bot_role"] = ManagedGroupBotRole(data["bot_role"])
    if "permissions_snapshot" in data:
        import json

        data["permissions_snapshot"] = json.dumps(data["permissions_snapshot"], ensure_ascii=False)

    for field, value in data.items():
        setattr(binding, field, value)

    await db.commit()
    await db.refresh(binding)
    return _serialize_binding(binding)


@router.delete("/{binding_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_managed_group_binding(binding_id: int, db: AsyncSession = Depends(get_db)) -> None:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    await db.delete(binding)
    await db.commit()
