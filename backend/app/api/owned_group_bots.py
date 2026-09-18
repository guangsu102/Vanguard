"""Safe registration and verification of Bot API profiles for owned groups.

The legacy ``/guardian-bots`` API stores a ``GuardianBotProfile``.  The owned
group worker intentionally uses a separate ``OwnedBotProfile`` so a raw
guardian account id can never be mistaken for a Bot API resource id.  This
router provides the small bridge an operator needs: select an existing
guardian-bot account, copy its token into encrypted storage (or provide a new
token), and explicitly verify it with the Bot API ``getMe`` call.

No response from this module contains a token or session value.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.owned_groups import require_owned_group_operator
from app.core.account.bot_credentials import resolve_guardian_bot_token
from app.core.account.models import (
    AccountRiskLevel,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.database import get_db
from app.core.ephemeral_secret import decrypt_ephemeral_secret, encrypt_ephemeral_secret
from app.integrations.telegram.client import TelegramAPIError, TelegramClient, TelegramConfig
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupAuditEvent
from app.modules.owned_group.security import safe_exception_message

router = APIRouter()


async def require_owned_group_bot_admin(
    current_user: dict = Depends(require_owned_group_operator),
) -> dict:
    """Bot credential/profile mutations are administrator-only."""

    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owned group bot administration requires admin access",
        )
    return current_user


class OwnedBotProfileRegister(BaseModel):
    """Register a bot account without echoing its credential back to clients."""

    owner_account_id: int = Field(..., gt=0)
    account_id: int = Field(..., gt=0)
    # Optional when the selected account already has a legacy GuardianBotProfile.
    # The value is accepted only on POST and is never included in a response.
    bot_token: str | None = Field(default=None, min_length=10, max_length=255)


class OwnedBotProfileUpdate(BaseModel):
    enabled: bool


class OwnedBotProfileResponse(BaseModel):
    id: int
    owner_account_id: int
    account_id: int
    bot_user_id: int | None = None
    bot_username: str | None = None
    display_name: str | None = None
    status: str
    enabled: bool
    last_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OwnedBotProfileListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[OwnedBotProfileResponse]
    total: int


def _response(profile: OwnedBotProfile) -> OwnedBotProfileResponse:
    return OwnedBotProfileResponse(
        id=profile.id,
        owner_account_id=profile.owner_account_id,
        account_id=profile.account_id,
        bot_user_id=profile.bot_user_id,
        bot_username=profile.bot_username,
        display_name=profile.display_name,
        status=profile.status,
        enabled=bool(profile.enabled),
        last_verified_at=profile.last_verified_at,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _risk_blocked(account: TelegramAccount) -> bool:
    value = getattr(account.risk_level, "value", account.risk_level)
    return str(value or "normal").strip().lower() in {
        AccountRiskLevel.FROZEN.value,
        AccountRiskLevel.QUARANTINED.value,
        AccountRiskLevel.LIMITED.value,
    }


async def _load_registration_inputs(
    db: AsyncSession, request: OwnedBotProfileRegister
) -> tuple[TelegramAccount, TelegramAccount, str, GuardianBotProfile | None]:
    owner = await db.get(TelegramAccount, request.owner_account_id)
    if owner is None or owner.account_type != AccountType.PROMOTER:
        raise HTTPException(status_code=422, detail="owner_account_id must reference a promoter account")
    if not owner.is_active or _risk_blocked(owner):
        raise HTTPException(status_code=422, detail="owner account is inactive or risk-blocked")

    account = await db.get(TelegramAccount, request.account_id)
    if account is None or account.account_type != AccountType.GUARDIAN_BOT:
        raise HTTPException(status_code=422, detail="account_id must reference a guardian bot account")
    if not account.is_active or _risk_blocked(account):
        raise HTTPException(status_code=422, detail="guardian bot account is inactive or risk-blocked")

    legacy = await db.scalar(
        select(GuardianBotProfile).where(GuardianBotProfile.account_id == account.id)
    )
    token = (
        request.bot_token
        or (resolve_guardian_bot_token(legacy.bot_token) if legacy else "")
        or ""
    ).strip()
    if not token:
        raise HTTPException(
            status_code=422,
            detail="bot_token is required when the guardian bot has no registered token",
        )
    return owner, account, token, legacy


@router.get("/bot-profiles", response_model=OwnedBotProfileListResponse)
async def list_owned_bot_profiles(
    owner_account_id: int | None = Query(default=None, gt=0),
    enabled: bool | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> OwnedBotProfileListResponse:
    query = select(OwnedBotProfile).order_by(desc(OwnedBotProfile.id)).offset(offset).limit(limit)
    count_query = select(func.count(OwnedBotProfile.id))
    if owner_account_id is not None:
        query = query.where(OwnedBotProfile.owner_account_id == owner_account_id)
        count_query = count_query.where(OwnedBotProfile.owner_account_id == owner_account_id)
    if enabled is not None:
        query = query.where(OwnedBotProfile.enabled.is_(enabled))
        count_query = count_query.where(OwnedBotProfile.enabled.is_(enabled))
    rows = await db.scalars(query)
    total = int((await db.scalar(count_query)) or 0)
    return OwnedBotProfileListResponse(
        data=[_response(profile) for profile in rows.all()], total=total
    )


@router.get("/bot-profiles/{profile_id:int}", response_model=OwnedBotProfileResponse)
async def get_owned_bot_profile(
    profile_id: int, db: AsyncSession = Depends(get_db)
) -> OwnedBotProfileResponse:
    profile = await db.get(OwnedBotProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Owned bot profile not found")
    return _response(profile)


@router.post(
    "/bot-profiles",
    response_model=OwnedBotProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_owned_bot_profile(
    request: OwnedBotProfileRegister,
    current_user: dict = Depends(require_owned_group_bot_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedBotProfileResponse:
    _owner, account, token, legacy = await _load_registration_inputs(db, request)

    existing = await db.scalar(
        select(OwnedBotProfile).where(OwnedBotProfile.account_id == account.id)
    )
    if existing is not None:
        if existing.owner_account_id != request.owner_account_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This guardian bot is already registered to another owner",
            )
        # A retry of the same registration is safe: rotate the encrypted token
        # and require a fresh explicit verification rather than silently
        # treating stale metadata as valid.
        encrypted = encrypt_ephemeral_secret(token)
        if not encrypted:
            raise HTTPException(status_code=503, detail="bot token encryption unavailable")
        existing.token_ciphertext = encrypted
        existing.status = "pending_verification"
        existing.enabled = bool(account.is_active)
        existing.bot_username = (legacy.bot_username if legacy else None) or existing.bot_username
        existing.bot_user_id = (legacy.bot_user_id if legacy else None) or existing.bot_user_id
        existing.display_name = account.display_name or existing.display_name
        await db.commit()
        await db.refresh(existing)
        db.add(
            OwnedGroupAuditEvent(
                event_type="owned_bot_profile_reregistered",
                resource_type="bot",
                resource_id=existing.id,
                actor_id=int(current_user["id"]) if current_user.get("id") else None,
                result="success",
                reason_code="verification_required",
            )
        )
        await db.commit()
        return _response(existing)

    encrypted = encrypt_ephemeral_secret(token)
    if not encrypted:
        raise HTTPException(status_code=503, detail="bot token encryption unavailable")
    profile = OwnedBotProfile(
        owner_account_id=request.owner_account_id,
        account_id=account.id,
        bot_user_id=legacy.bot_user_id if legacy else None,
        bot_username=legacy.bot_username if legacy else None,
        display_name=account.display_name,
        token_ciphertext=encrypted,
        status="pending_verification",
        enabled=bool(account.is_active),
    )
    db.add(profile)
    await db.flush()
    db.add(
        OwnedGroupAuditEvent(
            event_type="owned_bot_profile_registered",
            resource_type="bot",
            resource_id=profile.id,
            actor_id=int(current_user["id"]) if current_user.get("id") else None,
            result="success",
            reason_code="verification_required",
        )
    )
    await db.commit()
    await db.refresh(profile)
    return _response(profile)


@router.patch("/bot-profiles/{profile_id:int}", response_model=OwnedBotProfileResponse)
async def update_owned_bot_profile(
    profile_id: int,
    request: OwnedBotProfileUpdate,
    current_user: dict = Depends(require_owned_group_bot_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedBotProfileResponse:
    profile = await db.get(OwnedBotProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Owned bot profile not found")
    profile.enabled = bool(request.enabled)
    if not profile.enabled:
        profile.status = "disabled"
    elif profile.status == "disabled":
        # Enabling a profile must not revive an unverified/rotated token.
        profile.status = "pending_verification"
    db.add(
        OwnedGroupAuditEvent(
            event_type="owned_bot_profile_enabled" if profile.enabled else "owned_bot_profile_disabled",
            resource_type="bot",
            resource_id=profile.id,
            actor_id=int(current_user["id"]) if current_user.get("id") else None,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(profile)
    return _response(profile)


@router.post("/bot-profiles/{profile_id:int}/verify", response_model=OwnedBotProfileResponse)
async def verify_owned_bot_profile(
    profile_id: int,
    current_user: dict = Depends(require_owned_group_bot_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedBotProfileResponse:
    profile = await db.get(OwnedBotProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Owned bot profile not found")
    if not profile.enabled:
        raise HTTPException(status_code=409, detail="Owned bot profile is disabled")
    try:
        token = decrypt_ephemeral_secret(profile.token_ciphertext)
    except Exception as exc:
        profile.status = "verification_failed"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "bot_token_invalid", "message": safe_exception_message(exc)},
        ) from exc
    if not token:
        profile.status = "verification_failed"
        await db.commit()
        raise HTTPException(status_code=422, detail={"reason": "bot_token_missing"})

    client = TelegramClient(TelegramConfig(bot_token=token))
    try:
        bot_user = await client.get_me()
    except TelegramAPIError as exc:
        profile.status = "verification_failed"
        profile.last_verified_at = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "bot_verification_failed",
                "message": safe_exception_message(exc),
            },
        ) from exc
    except Exception as exc:  # pragma: no cover - transport-specific failures
        profile.status = "verification_failed"
        profile.last_verified_at = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "reason": "bot_verification_unavailable",
                "message": safe_exception_message(exc),
            },
        ) from exc
    finally:
        await client.close()

    if not bool(getattr(bot_user, "is_bot", True)):
        profile.status = "verification_failed"
        profile.last_verified_at = None
        await db.commit()
        raise HTTPException(status_code=422, detail={"reason": "telegram_identity_not_bot"})

    profile.bot_user_id = int(getattr(bot_user, "user_id", 0) or 0) or None
    profile.bot_username = getattr(bot_user, "username", None) or profile.bot_username
    profile.display_name = getattr(bot_user, "full_name", None) or profile.display_name
    profile.status = "verified"
    profile.last_verified_at = datetime.utcnow()
    db.add(
        OwnedGroupAuditEvent(
            event_type="owned_bot_profile_verified",
            resource_type="bot",
            resource_id=profile.id,
            actor_id=int(current_user["id"]) if current_user.get("id") else None,
            result="success",
        )
    )
    await db.commit()
    await db.refresh(profile)
    return _response(profile)


__all__ = ["router"]
