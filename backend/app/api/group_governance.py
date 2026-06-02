"""
Group governance policy API.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guardian_validation import ensure_managed_group_binding
from app.core.database import get_db
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    VerificationType,
    ViolationAction,
)


router = APIRouter()


class VerificationPolicyPayload(BaseModel):
    group_id: int
    enable_verification: bool = False
    verification_type: str = "captcha"
    questions: Optional[list[dict]] = None
    welcome_message: Optional[str] = None
    timeout_minutes: int = Field(default=5, ge=1, le=120)
    max_attempts: int = Field(default=3, ge=1, le=10)
    whitelist_bypass: bool = True
    auto_kick_unverified: bool = False
    kick_after_minutes: int = Field(default=10, ge=1, le=1440)


class ModerationPolicyPayload(BaseModel):
    group_id: Optional[int] = None
    message_interval_seconds: int = Field(default=10, ge=0, le=3600)
    max_messages_per_minute: int = Field(default=5, ge=1, le=1000)
    max_links_per_hour: int = Field(default=3, ge=0, le=1000)
    new_member_silent_minutes: int = Field(default=5, ge=0, le=1440)
    first_speak_delay_seconds: int = Field(default=30, ge=0, le=86400)
    media_policy: Optional[dict] = None
    link_policy: Optional[dict] = None


class PunishmentPolicyPayload(BaseModel):
    group_id: Optional[int] = None
    warn_threshold: int = Field(default=3, ge=1, le=100)
    mute_on_warn_threshold: bool = True
    mute_duration_seconds: int = Field(default=300, ge=0, le=604800)
    ban_on_warn_threshold: int = Field(default=5, ge=1, le=100)
    repeat_violation_window_hours: int = Field(default=24, ge=1, le=720)
    auto_reset_warning_days: int = Field(default=7, ge=0, le=365)
    severe_violation_direct_action: str = "mute"


def _serialize_verification(config: GroupVerificationConfig) -> dict:
    return {
        "group_id": config.group_id,
        "enable_verification": config.enable_verification,
        "verification_type": config.verification_type.value,
        "questions": config.get_questions(),
        "welcome_message": config.welcome_message,
        "timeout_minutes": config.timeout_minutes,
        "max_attempts": config.max_attempts,
        "whitelist_bypass": config.whitelist_bypass,
        "auto_kick_unverified": config.auto_kick_unverified,
        "kick_after_minutes": config.kick_after_minutes,
        "updated_at": config.updated_at.isoformat() if config.updated_at else "",
    }


def _serialize_moderation(policy: GroupModerationPolicy) -> dict:
    import json

    def parse(value: Optional[str]) -> Optional[dict]:
        if not value:
            return None
        try:
            return json.loads(value)
        except Exception:
            return {"raw": value}

    return {
        "group_id": policy.group_id,
        "message_interval_seconds": policy.message_interval_seconds,
        "max_messages_per_minute": policy.max_messages_per_minute,
        "max_links_per_hour": policy.max_links_per_hour,
        "new_member_silent_minutes": policy.new_member_silent_minutes,
        "first_speak_delay_seconds": policy.first_speak_delay_seconds,
        "media_policy": parse(policy.media_policy),
        "link_policy": parse(policy.link_policy),
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else "",
    }


def _serialize_punishment(policy: GroupPunishmentPolicy) -> dict:
    return {
        "group_id": policy.group_id,
        "warn_threshold": policy.warn_threshold,
        "mute_on_warn_threshold": policy.mute_on_warn_threshold,
        "mute_duration_seconds": policy.mute_duration_seconds,
        "ban_on_warn_threshold": policy.ban_on_warn_threshold,
        "repeat_violation_window_hours": policy.repeat_violation_window_hours,
        "auto_reset_warning_days": policy.auto_reset_warning_days,
        "severe_violation_direct_action": policy.severe_violation_direct_action.value,
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else "",
    }


@router.get("/verification/{group_id}")
async def get_verification_policy(group_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    await ensure_managed_group_binding(db, group_id)
    result = await db.execute(select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == group_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Verification policy not found")
    return {"code": 0, "message": "success", "data": _serialize_verification(config)}


@router.put("/verification")
async def upsert_verification_policy(
    request: VerificationPolicyPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    await ensure_managed_group_binding(db, request.group_id)
    result = await db.execute(select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == request.group_id))
    config = result.scalar_one_or_none()
    if not config:
        config = GroupVerificationConfig(group_id=request.group_id)
        db.add(config)

    config.enable_verification = request.enable_verification
    config.verification_type = VerificationType(request.verification_type)
    config.welcome_message = request.welcome_message
    config.timeout_minutes = request.timeout_minutes
    config.max_attempts = request.max_attempts
    config.whitelist_bypass = request.whitelist_bypass
    config.auto_kick_unverified = request.auto_kick_unverified
    config.kick_after_minutes = request.kick_after_minutes
    config.set_questions(request.questions or [])

    await db.commit()
    await db.refresh(config)
    return {"code": 0, "message": "success", "data": _serialize_verification(config)}


@router.get("/moderation/{group_id}")
async def get_group_moderation_policy(group_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    await ensure_managed_group_binding(db, group_id)
    result = await db.execute(select(GroupModerationPolicy).where(GroupModerationPolicy.group_id == group_id))
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Moderation policy not found")
    return {"code": 0, "message": "success", "data": _serialize_moderation(policy)}


@router.put("/moderation")
async def upsert_group_moderation_policy(
    request: ModerationPolicyPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if request.group_id is None:
        raise HTTPException(status_code=400, detail="group_id is required")
    await ensure_managed_group_binding(db, request.group_id)
    result = await db.execute(select(GroupModerationPolicy).where(GroupModerationPolicy.group_id == request.group_id))
    policy = result.scalar_one_or_none()
    if not policy:
        policy = GroupModerationPolicy(group_id=request.group_id)
        db.add(policy)

    import json

    policy.message_interval_seconds = request.message_interval_seconds
    policy.max_messages_per_minute = request.max_messages_per_minute
    policy.max_links_per_hour = request.max_links_per_hour
    policy.new_member_silent_minutes = request.new_member_silent_minutes
    policy.first_speak_delay_seconds = request.first_speak_delay_seconds
    policy.media_policy = json.dumps(request.media_policy, ensure_ascii=False) if request.media_policy is not None else None
    policy.link_policy = json.dumps(request.link_policy, ensure_ascii=False) if request.link_policy is not None else None

    await db.commit()
    await db.refresh(policy)
    return {"code": 0, "message": "success", "data": _serialize_moderation(policy)}


@router.get("/punishment/{group_id}")
async def get_group_punishment_policy(group_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    await ensure_managed_group_binding(db, group_id)
    result = await db.execute(select(GroupPunishmentPolicy).where(GroupPunishmentPolicy.group_id == group_id))
    policy = result.scalar_one_or_none()
    if not policy:
        raise HTTPException(status_code=404, detail="Punishment policy not found")
    return {"code": 0, "message": "success", "data": _serialize_punishment(policy)}


@router.put("/punishment")
async def upsert_group_punishment_policy(
    request: PunishmentPolicyPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if request.group_id is None:
        raise HTTPException(status_code=400, detail="group_id is required")
    await ensure_managed_group_binding(db, request.group_id)
    result = await db.execute(select(GroupPunishmentPolicy).where(GroupPunishmentPolicy.group_id == request.group_id))
    policy = result.scalar_one_or_none()
    if not policy:
        policy = GroupPunishmentPolicy(group_id=request.group_id)
        db.add(policy)

    policy.warn_threshold = request.warn_threshold
    policy.mute_on_warn_threshold = request.mute_on_warn_threshold
    policy.mute_duration_seconds = request.mute_duration_seconds
    policy.ban_on_warn_threshold = request.ban_on_warn_threshold
    policy.repeat_violation_window_hours = request.repeat_violation_window_hours
    policy.auto_reset_warning_days = request.auto_reset_warning_days
    policy.severe_violation_direct_action = ViolationAction(request.severe_violation_direct_action)

    await db.commit()
    await db.refresh(policy)
    return {"code": 0, "message": "success", "data": _serialize_punishment(policy)}
