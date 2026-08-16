"""
Accounts API Router

RESTful API for Telegram account management with cursor pagination.
"""

from datetime import datetime
from typing import Optional
import shutil
import tempfile
from pathlib import Path

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.core.account.manager import AccountManager
from app.core.account.auth_helper import TelegramAuthHelper
from app.core.account.models import (
    AccountAssetTier,
    AccountEnvironmentEvent,
    AccountRiskDailyStat,
    AccountRiskEvent,
    AccountStatus,
    AccountType,
    AccountWarmupStage,
    ProxyMode,
    TelegramAccount,
    TelegramAPIConfig,
)
from app.core.account.pool import get_account_pool, invalidate_account_in_all_pools
from app.core.account.proxy_policy_events import publish_account_proxy_policy_changed
from app.core.account.proxy_resolver import normalize_proxy_mode, resolve_auth_proxy
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.session_crypto import encrypt_session_string
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.account.environment_guard import AccountEnvironmentGuard
from app.core.group.membership_sync import sync_account_joined_groups
from app.core.network.fingerprint import FingerprintManager
from app.core.security import require_admin


router = APIRouter()
auth_helper = TelegramAuthHelper()
MAX_STATIC_PROXY_BINDINGS = 3
logger = structlog.get_logger()


def _fingerprint_key_for_account(
    *,
    phone: Optional[str] = None,
    identifier: Optional[str] = None,
    session_name: Optional[str] = None,
    account_id: Optional[int] = None,
) -> str:
    """Build the stable key used to derive an account device fingerprint."""
    return (phone or identifier or session_name or (str(account_id) if account_id else None) or "telegram-account").strip()


def _build_telegram_device_profile(
    *,
    phone: Optional[str] = None,
    identifier: Optional[str] = None,
    session_name: Optional[str] = None,
    account_id: Optional[int] = None,
    existing: Optional[TelegramAccount] = None,
) -> dict[str, str]:
    """Return the stable Telegram device metadata for login and persistence."""
    key = existing.fingerprint_id if existing and existing.fingerprint_id else _fingerprint_key_for_account(
        phone=phone,
        identifier=identifier,
        session_name=session_name,
        account_id=account_id,
    )
    return FingerprintManager().generate_telegram_device_profile(
        key,
        device_model=existing.device_model if existing else None,
        system_version=existing.system_version if existing else None,
        app_version=existing.app_version if existing else None,
    )


def _apply_device_profile(account: TelegramAccount, profile: dict[str, str]) -> None:
    """Persist generated Telegram device metadata on an account."""
    account.fingerprint_id = account.fingerprint_id or profile["fingerprint_id"]
    account.device_model = profile["device_model"]
    account.system_version = profile["system_version"]
    account.app_version = profile["app_version"]


def _normalize_account_asset_tier(value: Optional[str]) -> str:
    raw = (value or AccountAssetTier.UNKNOWN.value).strip()
    try:
        return AccountAssetTier(raw).value
    except ValueError as exc:
        valid = "/".join(item.value for item in AccountAssetTier)
        raise HTTPException(status_code=400, detail=f"asset_tier must be one of {valid}") from exc


# =============================================================================
# Request/Response Models
# =============================================================================

class AccountCreate(BaseModel):
    """Account creation request."""
    phone: Optional[str] = Field(None, description="Phone number with country code")
    identifier: Optional[str] = Field(None, description="Unified account identifier")
    display_name: Optional[str] = Field(None, description="Display name")
    profile_bio: Optional[str] = Field(None, max_length=70, description="Telegram public bio")
    account_type: str = Field(default="promoter", description="Account type: promoter/guardian_bot")
    registered_at: Optional[datetime] = Field(None, description="Known Telegram account registration time")
    asset_note: Optional[str] = Field(None, max_length=255, description="Asset source or batch note")
    managed_started_at: Optional[datetime] = Field(None, description="When Vanguard started managing this account")
    warmup_hold_until: Optional[datetime] = Field(None, description="Manual warmup hold deadline")
    warmup_note: Optional[str] = Field(None, max_length=255, description="Managed warmup note")
    api_config_name: str = Field(default="default", description="API config name")
    country_code: str = Field(default="US", max_length=2, description="Country code (ISO 3166-1 alpha-2)")
    country_name: Optional[str] = Field(None, description="Country name")
    session_name: Optional[str] = Field(None, description="Custom session name")
    proxy_mode: str = Field(default="dynamic", description="Proxy mode: dynamic/static/none")
    static_proxy_id: Optional[int] = Field(None, description="Static proxy ID to bind before login")


class CompleteLoginRequest(AccountCreate):
    """Complete account login request."""
    session_string: str = Field(..., description="Session string from verify-code or verify-2fa")


class AccountUpdate(BaseModel):
    """Account update request."""
    display_name: Optional[str] = Field(None, description="Display name")
    profile_bio: Optional[str] = Field(None, max_length=70, description="Telegram public bio")
    registered_at: Optional[datetime] = Field(None, description="Known Telegram account registration time")
    asset_note: Optional[str] = Field(None, max_length=255, description="Asset source or batch note")
    managed_started_at: Optional[datetime] = Field(None, description="When Vanguard started managing this account")
    warmup_hold_until: Optional[datetime] = Field(None, description="Manual warmup hold deadline")
    warmup_note: Optional[str] = Field(None, max_length=255, description="Managed warmup note")
    country_code: Optional[str] = Field(None, max_length=2, description="Country code")
    fingerprint_id: Optional[str] = Field(None, description="Device fingerprint ID")
    is_active: Optional[bool] = Field(None, description="Active status")
    proxy_mode: Optional[str] = Field(None, description="Proxy mode: dynamic/static/none")
    static_proxy_id: Optional[int] = Field(None, description="Static proxy ID")


class AccountProxyBindRequest(BaseModel):
    """Bind account to a static proxy."""
    proxy_id: int = Field(..., description="Static proxy ID")


class AccountProxyPolicyRequest(BaseModel):
    """Update account proxy policy."""
    proxy_mode: str = Field(..., description="Proxy mode: dynamic/static/none")
    static_proxy_id: Optional[int] = Field(None, description="Static proxy ID")


class AccountProfileBioSyncRequest(BaseModel):
    """Update and sync Telegram public bio."""
    profile_bio: Optional[str] = Field(None, max_length=70, description="Optional bio override before sync")


class AccountResponse(BaseModel):
    """Account response."""
    id: int
    phone: Optional[str] = None
    identifier: str
    display_name: Optional[str] = None
    profile_bio: Optional[str] = None
    profile_bio_synced_at: Optional[str] = None
    account_type: str
    asset_tier: str = AccountAssetTier.UNKNOWN.value
    registered_at: Optional[str] = None
    asset_verified_at: Optional[str] = None
    asset_note: Optional[str] = None
    managed_started_at: Optional[str] = None
    warmup_stage: str = AccountWarmupStage.OBSERVE.value
    warmup_stage_updated_at: Optional[str] = None
    warmup_hold_until: Optional[str] = None
    warmup_note: Optional[str] = None
    status: str
    country_code: str
    country_name: Optional[str] = None
    api_config_name: str
    fingerprint_id: Optional[str] = None
    session_name: str
    proxy_mode: str
    static_proxy_id: Optional[int] = None
    static_proxy_address: Optional[str] = None
    is_active: bool
    connection_count: int
    error_count: int
    last_active_at: Optional[str] = None
    last_connected_at: Optional[str] = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class AccountListResponse(BaseModel):
    """Account list response with cursor pagination."""
    code: int = 0
    message: str = "success"
    data: list[AccountResponse]
    total: int
    next_cursor: Optional[str] = None
    has_more: bool = False



class AccountRiskSummaryResponse(BaseModel):
    """Account risk and environment summary."""
    code: int = 0
    message: str = "success"
    data: dict


class AccountRiskEventsResponse(BaseModel):
    """Recent account risk and environment events."""
    code: int = 0
    message: str = "success"
    data: dict


class AccountRiskManualAdjustRequest(BaseModel):
    """Admin risk lifecycle adjustment."""
    score_delta: Optional[float] = Field(None, ge=-100, le=100)
    set_score: Optional[float] = Field(None, ge=0, le=100)
    target_level: Optional[str] = Field(None, description="normal/watch/limited/frozen/quarantined")
    clear_pause: bool = Field(default=False)
    reason: str = Field(default="manual_adjust", max_length=255)

class AccountManualBanRequest(BaseModel):
    """Admin request to permanently disable an account for failover."""

    reason: str = Field(default="manual_ban", max_length=255)

class AccountStatsResponse(BaseModel):
    """Account statistics response."""
    code: int = 0
    message: str = "success"
    data: dict


class AccountBatchImportRequest(BaseModel):
    """Batch import request."""
    accounts: list[AccountCreate] = Field(..., min_length=1, max_length=100)


class AccountBatchImportResponse(BaseModel):
    """Batch import response."""
    code: int = 0
    message: str = "success"
    data: dict


# =============================================================================
# API Config Models
# =============================================================================

class APIConfigCreate(BaseModel):
    """API config creation request."""
    name: str = Field(..., max_length=50, description="Config name")
    api_id: str = Field(..., description="Telegram API ID")
    api_hash: str = Field(..., description="Telegram API Hash")
    description: Optional[str] = Field(None, max_length=200)


class APIConfigResponse(BaseModel):
    """API config response."""
    id: int
    name: str
    api_id: str
    api_hash: str
    description: Optional[str] = None
    account_count: int
    created_at: str
    updated_at: str


# =============================================================================
# Helper Functions
# =============================================================================

def _account_to_response(account: TelegramAccount) -> AccountResponse:
    """Convert TelegramAccount model to response."""
    return AccountResponse(
        id=account.id,
        phone=account.phone,
        identifier=account.identifier,
        display_name=account.display_name,
        profile_bio=account.profile_bio,
        profile_bio_synced_at=account.profile_bio_synced_at.isoformat() if account.profile_bio_synced_at else None,
        account_type=account.account_type.value,
        asset_tier=account.asset_tier or AccountAssetTier.UNKNOWN.value,
        registered_at=account.registered_at.isoformat() if account.registered_at else None,
        asset_verified_at=account.asset_verified_at.isoformat() if account.asset_verified_at else None,
        asset_note=account.asset_note,
        managed_started_at=account.managed_started_at.isoformat() if account.managed_started_at else None,
        warmup_stage=account.warmup_stage or AccountWarmupStage.OBSERVE.value,
        warmup_stage_updated_at=account.warmup_stage_updated_at.isoformat() if account.warmup_stage_updated_at else None,
        warmup_hold_until=account.warmup_hold_until.isoformat() if account.warmup_hold_until else None,
        warmup_note=account.warmup_note,
        status=account.status.value,
        country_code=account.country_code,
        country_name=account.country_name,
        api_config_name=account.api_config_name,
        fingerprint_id=account.fingerprint_id,
        session_name=account.session_name,
        proxy_mode=getattr(account.proxy_mode, "value", str(account.proxy_mode or ProxyMode.DYNAMIC)),
        static_proxy_id=account.static_proxy_id,
        static_proxy_address=(
            f"{account.__dict__['static_proxy'].host}:{account.__dict__['static_proxy'].port}"
            if account.__dict__.get("static_proxy") is not None
            else None
        ),
        is_active=account.is_active,
        connection_count=account.connection_count,
        error_count=account.error_count,
        last_active_at=account.last_active_at.isoformat() if account.last_active_at else None,
        last_connected_at=account.last_connected_at.isoformat() if account.last_connected_at else None,
        created_at=account.created_at.isoformat() if account.created_at else "",
        updated_at=account.updated_at.isoformat() if account.updated_at else "",
    )



def _risk_event_to_dict(event: AccountRiskEvent) -> dict:
    return {
        "id": event.id,
        "account_id": event.account_id,
        "action": event.action,
        "status": event.status,
        "reason": event.reason,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "fingerprint_id": event.fingerprint_id,
        "proxy_mode": event.proxy_mode,
        "proxy_id": event.proxy_id,
        "proxy_country": event.proxy_country,
        "details": event.details,
        "created_at": event.created_at.isoformat() if event.created_at else "",
    }


def _environment_event_to_dict(event: AccountEnvironmentEvent) -> dict:
    return {
        "id": event.id,
        "account_id": event.account_id,
        "event_type": event.event_type,
        "status": event.status,
        "reason": event.reason,
        "proxy_mode": event.proxy_mode,
        "proxy_id": event.proxy_id,
        "proxy_country": event.proxy_country,
        "fingerprint_id": event.fingerprint_id,
        "device_model": event.device_model,
        "system_version": event.system_version,
        "app_version": event.app_version,
        "details": event.details,
        "created_at": event.created_at.isoformat() if event.created_at else "",
    }

async def _get_api_config_or_default(
    manager: AccountManager,
    config_name: str,
) -> TelegramAPIConfig | None:
    """Get an API config, creating the default one from env when needed."""
    config = await manager.get_api_config(config_name)
    if config or config_name != "default":
        return config

    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        return None

    return await manager.create_api_config(
        name="default",
        api_id=str(settings.TELEGRAM_API_ID),
        api_hash=settings.TELEGRAM_API_HASH,
        description="Default config from TELEGRAM_API_ID/TELEGRAM_API_HASH",
    )


async def _resolve_account_proxy_for_policy(
    *,
    db: AsyncSession,
    account_type: AccountType,
    proxy_mode: ProxyMode,
    country_code: str,
    account_key: str,
    static_proxy_id: Optional[int],
) -> tuple | None:
    """Resolve an account proxy policy into a Telethon proxy tuple."""
    try:
        resolved = await resolve_auth_proxy(
            db=db,
            account_type=account_type,
            proxy_mode=proxy_mode,
            country_code=country_code,
            account_key=account_key,
            static_proxy_id=static_proxy_id,
            provider=getattr(settings, "PROXY_PROVIDER", "evomi").lower(),
            proxy_required=bool(getattr(settings, "PROMOTER_PROXY_REQUIRED", True)),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Account proxy is unavailable: {exc}",
        ) from exc

    return resolved.to_telethon() if resolved else None


async def _ensure_static_proxy_capacity(
    db: AsyncSession,
    static_proxy_id: Optional[int],
    *,
    exclude_account_id: Optional[int] = None,
) -> None:
    """Ensure one static proxy is not bound to more than the allowed account count."""
    if static_proxy_id is None:
        return

    query = select(func.count(TelegramAccount.id)).where(
        TelegramAccount.static_proxy_id == static_proxy_id
    )
    if exclude_account_id is not None:
        query = query.where(TelegramAccount.id != exclude_account_id)

    count = (await db.execute(query)).scalar() or 0
    if count >= MAX_STATIC_PROXY_BINDINGS:
        raise HTTPException(
            status_code=400,
            detail=f"Static proxy {static_proxy_id} already has {MAX_STATIC_PROXY_BINDINGS} bound accounts",
        )


async def _ensure_account_proxy_available(account: TelegramAccount, db: AsyncSession) -> None:
    """Fail fast if the account's configured proxy policy cannot be satisfied."""
    await _resolve_account_proxy_for_policy(
        db=db,
        account_type=account.account_type,
        proxy_mode=normalize_proxy_mode(account.proxy_mode),
        country_code=account.country_code,
        account_key=account.phone or account.session_name or str(account.id),
        static_proxy_id=account.static_proxy_id,
    )


async def _propagate_account_proxy_policy_change(account: TelegramAccount) -> None:
    """Disconnect stale clients locally and notify every other process."""
    proxy_mode = normalize_proxy_mode(account.proxy_mode)
    static_proxy_id = account.static_proxy_id if proxy_mode == ProxyMode.STATIC else None
    await invalidate_account_in_all_pools(
        account.id,
        reason="proxy_policy_updated",
    )
    try:
        await publish_account_proxy_policy_changed(
            account.id,
            proxy_mode.value,
            static_proxy_id,
        )
    except Exception as exc:
        logger.exception(
            "account_proxy_policy_propagation_failed",
            account_id=account.id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Proxy policy was saved, but existing connections could not be invalidated "
                "across all processes. Retry the operation."
            ),
        ) from exc


async def _sync_promoter_joined_groups(account: TelegramAccount, db: AsyncSession) -> None:
    """Mirror the groups already joined by a logged-in promoter account."""
    await sync_account_joined_groups(db, account)


# =============================================================================
# Account CRUD Endpoints
# =============================================================================

@router.get("", response_model=AccountListResponse)
async def list_accounts(
    cursor: Optional[str] = None,
    limit: int = 20,
    status_filter: Optional[str] = None,
    country_code: Optional[str] = None,
    api_config_name: Optional[str] = None,
    account_type: Optional[str] = None,
    asset_tier: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> AccountListResponse:
    """
    Get list of Telegram accounts with cursor pagination.

    - cursor: Pagination cursor (account ID from previous response)
    - limit: Number of items per page (max 100)
    - status_filter: Filter by status (offline, online, working, idle, error, banned)
    - country_code: Filter by country code
    - api_config_name: Filter by API config name
    - search: Search by phone number
    """
    # Build query
    query = select(TelegramAccount)
    count_query = select(func.count(TelegramAccount.id))

    # Cursor pagination (using ID)
    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.where(TelegramAccount.id < cursor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    # Filters
    if status_filter:
        try:
            status_enum = AccountStatus(status_filter)
            query = query.where(TelegramAccount.status == status_enum)
            count_query = count_query.where(TelegramAccount.status == status_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status_filter}")

    if country_code:
        query = query.where(TelegramAccount.country_code == country_code.upper())
        count_query = count_query.where(TelegramAccount.country_code == country_code.upper())

    if api_config_name:
        query = query.where(TelegramAccount.api_config_name == api_config_name)
        count_query = count_query.where(TelegramAccount.api_config_name == api_config_name)

    if account_type:
        try:
            account_type_enum = AccountType(account_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid account_type: {account_type}")
        query = query.where(TelegramAccount.account_type == account_type_enum)
        count_query = count_query.where(TelegramAccount.account_type == account_type_enum)

    if asset_tier:
        normalized_asset_tier = _normalize_account_asset_tier(asset_tier)
        query = query.where(TelegramAccount.asset_tier == normalized_asset_tier)
        count_query = count_query.where(TelegramAccount.asset_tier == normalized_asset_tier)

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            or_(
                TelegramAccount.phone.like(search_pattern),
                TelegramAccount.identifier.like(search_pattern),
                TelegramAccount.display_name.like(search_pattern),
            )
        )
        count_query = count_query.where(
            or_(
                TelegramAccount.phone.like(search_pattern),
                TelegramAccount.identifier.like(search_pattern),
                TelegramAccount.display_name.like(search_pattern),
            )
        )

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get data with pagination
    query = query.order_by(desc(TelegramAccount.id)).limit(limit + 1)
    result = await db.execute(query)
    accounts = list(result.scalars().all())

    # Check if there are more results
    has_more = len(accounts) > limit
    if has_more:
        accounts = accounts[:limit]

    # Get next cursor
    next_cursor = str(accounts[-1].id) if accounts and has_more else None

    return AccountListResponse(
        data=[_account_to_response(a) for a in accounts],
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    account: AccountCreate,
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    """Create a new Telegram account."""
    manager = AccountManager(db)

    try:
        try:
            account_type = AccountType(account.account_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid account_type: {account.account_type}")
        try:
            proxy_mode = normalize_proxy_mode(account.proxy_mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {account.proxy_mode}")
        await _resolve_account_proxy_for_policy(
            db=db,
            account_type=account_type,
            proxy_mode=proxy_mode,
            country_code=account.country_code,
            account_key=account.phone or account.identifier or account.session_name or "new-account",
            static_proxy_id=account.static_proxy_id,
        )
        if proxy_mode == ProxyMode.STATIC:
            await _ensure_static_proxy_capacity(db, account.static_proxy_id)
        created = await manager.create_account(
            phone=account.phone,
            identifier=account.identifier,
            display_name=account.display_name,
            profile_bio=(account.profile_bio or "").strip() or None,
            asset_tier=AccountAssetTier.UNKNOWN.value,
            registered_at=account.registered_at,
            asset_note=(account.asset_note or "").strip()[:255] or None,
            managed_started_at=account.managed_started_at,
            warmup_hold_until=account.warmup_hold_until,
            warmup_note=(account.warmup_note or "").strip()[:255] or None,
            account_type=account_type,
            api_config_name=account.api_config_name,
            country_code=account.country_code,
            country_name=account.country_name,
            session_name=account.session_name,
            proxy_mode=proxy_mode,
            static_proxy_id=account.static_proxy_id if proxy_mode == ProxyMode.STATIC else None,
        )
        _apply_device_profile(
            created,
            _build_telegram_device_profile(
                phone=created.phone,
                identifier=created.identifier,
                session_name=created.session_name,
                account_id=created.id,
                existing=created,
            ),
        )
        await db.commit()
        await db.refresh(created)
        return _account_to_response(created)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{account_id:int}", response_model=AccountResponse)
async def get_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    """Get account by ID."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return _account_to_response(account)


@router.put("/{account_id:int}", response_model=AccountResponse)
async def update_account(
    account_id: int,
    account: AccountUpdate,
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    """Update account."""
    manager = AccountManager(db)

    update_kwargs = account.model_dump(exclude_none=True)
    proxy_policy_updated = bool({"proxy_mode", "static_proxy_id"} & update_kwargs.keys())
    if not update_kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        current = await manager.get_account(account_id)
        if not current:
            raise HTTPException(status_code=404, detail="Account not found")
        if "profile_bio" in update_kwargs:
            update_kwargs["profile_bio"] = (update_kwargs["profile_bio"] or "").strip()
        if "asset_note" in update_kwargs:
            update_kwargs["asset_note"] = (update_kwargs["asset_note"] or "").strip()[:255]
        if "warmup_note" in update_kwargs:
            update_kwargs["warmup_note"] = (update_kwargs["warmup_note"] or "").strip()[:255]

        proxy_mode = None
        if "proxy_mode" in update_kwargs:
            try:
                proxy_mode = normalize_proxy_mode(update_kwargs["proxy_mode"])
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {update_kwargs['proxy_mode']}")
            update_kwargs["proxy_mode"] = proxy_mode

        if "proxy_mode" in update_kwargs or "static_proxy_id" in update_kwargs:
            next_proxy_mode = proxy_mode or normalize_proxy_mode(current.proxy_mode)
            next_static_proxy_id = (
                update_kwargs.get("static_proxy_id")
                if next_proxy_mode == ProxyMode.STATIC
                else None
            )
            await _resolve_account_proxy_for_policy(
                db=db,
                account_type=current.account_type,
                proxy_mode=next_proxy_mode,
                country_code=update_kwargs.get("country_code") or current.country_code,
                account_key=current.phone or current.session_name or str(current.id),
                static_proxy_id=next_static_proxy_id,
            )
            if next_proxy_mode == ProxyMode.STATIC:
                await _ensure_static_proxy_capacity(
                    db,
                    next_static_proxy_id,
                    exclude_account_id=current.id,
                )
            update_kwargs["static_proxy_id"] = next_static_proxy_id

        updated = await manager.update_account(account_id, **update_kwargs)
        if proxy_policy_updated:
            await _propagate_account_proxy_policy_change(updated)
        return _account_to_response(updated)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Account not found")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{account_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete account."""
    manager = AccountManager(db)

    try:
        await manager.delete_account(account_id)
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Account not found")
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Account Operations
# =============================================================================

@router.post("/{account_id:int}/connect")
async def connect_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Connect account to Telegram (placeholder - actual connection handled by AccountPool)."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    await _ensure_account_proxy_available(account, db)

    # Update status to online
    await manager.update_account_status(account_id, AccountStatus.ONLINE)

    return {
        "code": 0,
        "message": "Connection initiated",
        "data": {"account_id": account_id, "status": "online"}
    }


@router.post("/{account_id:int}/profile-bio/sync", response_model=AccountResponse)
async def sync_account_profile_bio(
    account_id: int,
    request: Optional[AccountProfileBioSyncRequest] = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    """Sync stored public bio to the Telegram account profile."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if request is not None and request.profile_bio is not None:
        account.profile_bio = request.profile_bio.strip()
        account.profile_bio_synced_at = None
        await db.commit()
        await db.refresh(account)

    await _ensure_account_proxy_available(account, db)

    account_pool = get_account_pool()
    await account_pool.add_account_from_db(account)
    wrapper = None
    try:
        wrapper = await account_pool.acquire_by_id(account.id, purpose="profile_bio_sync")
        if wrapper is None:
            raise HTTPException(status_code=400, detail="Account session unavailable; login account before syncing bio")

        ok = await TelegramExecutionService(AccountRiskGuard(db)).update_profile_bio(
            wrapper,
            account.profile_bio or "",
            source="account_profile_bio_sync",
        )
        if not ok:
            raise HTTPException(status_code=400, detail="Telegram client unavailable")

        account.profile_bio_synced_at = datetime.utcnow()
        account.status = AccountStatus.ONLINE
        account.last_connected_at = account.profile_bio_synced_at
        await db.commit()
        await db.refresh(account)
        await AccountEnvironmentGuard(db).record_event(
            account,
            "profile_update",
            details={"source": "profile_bio_sync", "bio_length": len(account.profile_bio or "")},
        )
        await db.refresh(account)
        return _account_to_response(account)
    finally:
        if wrapper is not None:
            await account_pool.release(wrapper)


def _daily_stat_to_dict(item: AccountRiskDailyStat) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "stat_date": item.stat_date.isoformat() if item.stat_date else "",
        "action": item.action,
        "status": item.status,
        "target_type": item.target_type,
        "count": item.count,
        "last_reason": item.last_reason,
        "first_seen_at": item.first_seen_at.isoformat() if item.first_seen_at else "",
        "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else "",
    }


@router.post("/{account_id:int}/disconnect")
async def disconnect_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Disconnect account from Telegram."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Update status to offline
    await manager.update_account_status(account_id, AccountStatus.OFFLINE)

    return {
        "code": 0,
        "message": "Disconnected",
        "data": {"account_id": account_id, "status": "offline"}
    }


@router.post("/{account_id:int}/bind-proxy")
async def bind_proxy(
    account_id: int,
    request: AccountProxyBindRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bind a static proxy to an account."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    await _resolve_account_proxy_for_policy(
        db=db,
        account_type=account.account_type,
        proxy_mode=ProxyMode.STATIC,
        country_code=account.country_code,
        account_key=account.phone or account.session_name or str(account.id),
        static_proxy_id=request.proxy_id,
    )
    await _ensure_static_proxy_capacity(db, request.proxy_id, exclude_account_id=account.id)
    account.proxy_mode = ProxyMode.STATIC
    account.static_proxy_id = request.proxy_id
    await db.commit()
    await db.refresh(account)
    await _propagate_account_proxy_policy_change(account)

    return {
        "code": 0,
        "message": "Proxy bound",
        "data": {"account_id": account_id, "proxy_id": request.proxy_id, "proxy_mode": account.proxy_mode.value},
    }


@router.put("/{account_id:int}/proxy-policy")
async def update_proxy_policy(
    account_id: int,
    request: AccountProxyPolicyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update account proxy mode and optional static proxy binding."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        proxy_mode = normalize_proxy_mode(request.proxy_mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {request.proxy_mode}")

    static_proxy_id = request.static_proxy_id if proxy_mode == ProxyMode.STATIC else None
    await _resolve_account_proxy_for_policy(
        db=db,
        account_type=account.account_type,
        proxy_mode=proxy_mode,
        country_code=account.country_code,
        account_key=account.phone or account.session_name or str(account.id),
        static_proxy_id=static_proxy_id,
    )
    if proxy_mode == ProxyMode.STATIC:
        await _ensure_static_proxy_capacity(db, static_proxy_id, exclude_account_id=account.id)
    account.proxy_mode = proxy_mode
    account.static_proxy_id = static_proxy_id
    await db.commit()
    await db.refresh(account)
    await _propagate_account_proxy_policy_change(account)

    return {
        "code": 0,
        "message": "Proxy policy updated",
        "data": {
            "account_id": account_id,
            "proxy_mode": account.proxy_mode.value,
            "static_proxy_id": account.static_proxy_id,
        },
    }


@router.put("/{account_id:int}/fingerprint")
async def update_fingerprint(
    account_id: int,
    fingerprint_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bind device fingerprint to account."""
    manager = AccountManager(db)

    try:
        await manager.bind_fingerprint(account_id, fingerprint_id)
        return {
            "code": 0,
            "message": "Fingerprint updated",
            "data": {"account_id": account_id, "fingerprint_id": fingerprint_id}
        }
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Account not found")
        raise HTTPException(status_code=400, detail=str(e))



@router.get("/{account_id:int}/risk-summary", response_model=AccountRiskSummaryResponse)
async def get_account_risk_summary(
    account_id: int,
    db: AsyncSession = Depends(get_db),
) -> AccountRiskSummaryResponse:
    """Get latest risk and runtime environment summary for one account."""
    account = await db.get(TelegramAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    latest_risk = (
        await db.execute(
            select(AccountRiskEvent)
            .where(AccountRiskEvent.account_id == account_id)
            .order_by(AccountRiskEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_environment = (
        await db.execute(
            select(AccountEnvironmentEvent)
            .where(AccountEnvironmentEvent.account_id == account_id)
            .order_by(AccountEnvironmentEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    blocked_count = (
        await db.execute(
            select(func.count(AccountRiskEvent.id)).where(
                AccountRiskEvent.account_id == account_id,
                AccountRiskEvent.status == "block",
            )
        )
    ).scalar() or 0
    failure_count = (
        await db.execute(
            select(func.count(AccountRiskEvent.id)).where(
                AccountRiskEvent.account_id == account_id,
                AccountRiskEvent.status == "failure",
            )
        )
    ).scalar() or 0
    today_stats = (
        await db.execute(
            select(AccountRiskDailyStat)
            .where(
                AccountRiskDailyStat.account_id == account_id,
                AccountRiskDailyStat.stat_date == datetime.utcnow().date(),
            )
            .order_by(AccountRiskDailyStat.action, AccountRiskDailyStat.status)
        )
    ).scalars().all()

    return AccountRiskSummaryResponse(
        data={
            "account_id": account.id,
            "risk_score": account.risk_score,
            "risk_level": account.risk_level,
            "risk_pause_until": account.risk_pause_until.isoformat() if account.risk_pause_until else None,
            "risk_recovery_until": account.risk_recovery_until.isoformat() if account.risk_recovery_until else None,
            "risk_reason": account.risk_reason,
            "last_risk_event_at": account.last_risk_event_at.isoformat() if account.last_risk_event_at else None,
            "last_risk_decay_at": account.last_risk_decay_at.isoformat() if account.last_risk_decay_at else None,
            "asset_tier": account.asset_tier or AccountAssetTier.UNKNOWN.value,
            "registered_at": account.registered_at.isoformat() if account.registered_at else None,
            "asset_verified_at": account.asset_verified_at.isoformat() if account.asset_verified_at else None,
            "asset_note": account.asset_note,
            "managed_started_at": account.managed_started_at.isoformat() if account.managed_started_at else None,
            "warmup_stage": account.warmup_stage or AccountWarmupStage.OBSERVE.value,
            "warmup_stage_updated_at": account.warmup_stage_updated_at.isoformat() if account.warmup_stage_updated_at else None,
            "warmup_hold_until": account.warmup_hold_until.isoformat() if account.warmup_hold_until else None,
            "warmup_note": account.warmup_note,
            "blocked_count": blocked_count,
            "failure_count": failure_count,
            "today_usage": [_daily_stat_to_dict(item) for item in today_stats],
            "fingerprint_id": account.fingerprint_id,
            "device_model": account.device_model,
            "system_version": account.system_version,
            "app_version": account.app_version,
            "latest_risk_event": _risk_event_to_dict(latest_risk) if latest_risk else None,
            "latest_environment_event": _environment_event_to_dict(latest_environment) if latest_environment else None,
        }
    )


@router.get("/{account_id:int}/risk-events", response_model=AccountRiskEventsResponse)
async def list_account_risk_events(
    account_id: int,
    limit: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AccountRiskEventsResponse:
    """List recent risk and environment events for one account."""
    account = await db.get(TelegramAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    risk_events = (
        await db.execute(
            select(AccountRiskEvent)
            .where(AccountRiskEvent.account_id == account_id)
            .order_by(AccountRiskEvent.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    environment_events = (
        await db.execute(
            select(AccountEnvironmentEvent)
            .where(AccountEnvironmentEvent.account_id == account_id)
            .order_by(AccountEnvironmentEvent.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    return AccountRiskEventsResponse(
        data={
            "risk_events": [_risk_event_to_dict(event) for event in risk_events],
            "environment_events": [_environment_event_to_dict(event) for event in environment_events],
        }
    )


@router.post("/{account_id:int}/risk/manual-adjust", response_model=AccountRiskSummaryResponse)
async def manual_adjust_account_risk(
    account_id: int,
    request: AccountRiskManualAdjustRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
) -> AccountRiskSummaryResponse:
    """Manually lower/clear account risk with admin audit."""
    if request.score_delta is not None and request.set_score is not None:
        raise HTTPException(status_code=400, detail="score_delta and set_score cannot both be set")
    guard = AccountRiskGuard(db)
    try:
        account = await guard.manual_adjust_risk(
            account_id,
            score_delta=request.score_delta,
            set_score=request.set_score,
            target_level=request.target_level,
            clear_pause=request.clear_pause,
            reason=request.reason,
            operator=current_user.get("username"),
        )
    except ValueError as exc:
        if str(exc) == "account_not_found":
            raise HTTPException(status_code=404, detail="Account not found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return AccountRiskSummaryResponse(
        data={
            "account_id": account.id,
            "risk_score": account.risk_score,
            "risk_level": account.risk_level,
            "risk_pause_until": account.risk_pause_until.isoformat() if account.risk_pause_until else None,
            "risk_recovery_until": account.risk_recovery_until.isoformat() if account.risk_recovery_until else None,
            "risk_reason": account.risk_reason,
            "last_risk_event_at": account.last_risk_event_at.isoformat() if account.last_risk_event_at else None,
            "last_risk_decay_at": account.last_risk_decay_at.isoformat() if account.last_risk_decay_at else None,
            "asset_tier": account.asset_tier or AccountAssetTier.UNKNOWN.value,
            "registered_at": account.registered_at.isoformat() if account.registered_at else None,
            "asset_verified_at": account.asset_verified_at.isoformat() if account.asset_verified_at else None,
            "asset_note": account.asset_note,
            "managed_started_at": account.managed_started_at.isoformat() if account.managed_started_at else None,
            "warmup_stage": account.warmup_stage or AccountWarmupStage.OBSERVE.value,
            "warmup_stage_updated_at": account.warmup_stage_updated_at.isoformat() if account.warmup_stage_updated_at else None,
            "warmup_hold_until": account.warmup_hold_until.isoformat() if account.warmup_hold_until else None,
            "warmup_note": account.warmup_note,
            "blocked_count": 0,
            "failure_count": 0,
            "today_usage": [],
            "fingerprint_id": account.fingerprint_id,
            "device_model": account.device_model,
            "system_version": account.system_version,
            "app_version": account.app_version,
            "latest_risk_event": None,
            "latest_environment_event": None,
        }
    )

@router.post("/{account_id:int}/manual-ban", response_model=AccountResponse)
async def manually_ban_account(
    account_id: int,
    request: AccountManualBanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
) -> AccountResponse:
    """Manually ban and deactivate an account, making its groups eligible for failover."""
    guard = AccountRiskGuard(db)
    try:
        account = await guard.manual_ban_account(
            account_id,
            reason=request.reason.strip() or "manual_ban",
            operator=current_user.get("username"),
        )
    except ValueError as exc:
        if str(exc) == "account_not_found":
            raise HTTPException(status_code=404, detail="Account not found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await invalidate_account_in_all_pools(account.id, reason="manual_account_ban")
    return _account_to_response(account)

# =============================================================================
# Batch Operations
# =============================================================================

@router.post("/batch-import", response_model=AccountBatchImportResponse)
async def batch_import_accounts(
    request: AccountBatchImportRequest,
    db: AsyncSession = Depends(get_db),
) -> AccountBatchImportResponse:
    """Batch import accounts."""
    manager = AccountManager(db)

    created = []
    failed = []

    for acc in request.accounts:
        try:
            account_type = AccountType(acc.account_type)
            proxy_mode = normalize_proxy_mode(acc.proxy_mode)
            await _resolve_account_proxy_for_policy(
                db=db,
                account_type=account_type,
                proxy_mode=proxy_mode,
                country_code=acc.country_code,
                account_key=acc.phone or acc.identifier or acc.session_name or "batch-account",
                static_proxy_id=acc.static_proxy_id,
            )
            if proxy_mode == ProxyMode.STATIC:
                await _ensure_static_proxy_capacity(db, acc.static_proxy_id)
            account = await manager.create_account(
                phone=acc.phone,
                identifier=acc.identifier,
                display_name=acc.display_name,
                profile_bio=(acc.profile_bio or "").strip() or None,
                asset_tier=AccountAssetTier.UNKNOWN.value,
                registered_at=acc.registered_at,
                asset_note=(acc.asset_note or "").strip()[:255] or None,
                managed_started_at=acc.managed_started_at,
                warmup_hold_until=acc.warmup_hold_until,
                warmup_note=(acc.warmup_note or "").strip()[:255] or None,
                account_type=account_type,
                api_config_name=acc.api_config_name,
                country_code=acc.country_code,
                country_name=acc.country_name,
                session_name=acc.session_name,
                proxy_mode=proxy_mode,
                static_proxy_id=acc.static_proxy_id if proxy_mode == ProxyMode.STATIC else None,
            )
            _apply_device_profile(
                account,
                _build_telegram_device_profile(
                    phone=account.phone,
                    identifier=account.identifier,
                    session_name=account.session_name,
                    account_id=account.id,
                    existing=account,
                ),
            )
            await db.commit()
            await db.refresh(account)
            created.append({"phone": acc.phone, "id": account.id})
        except Exception as e:
            failed.append({"phone": acc.phone, "error": str(e)})

    return AccountBatchImportResponse(
        code=0,
        message="Batch import completed",
        data={
            "created_count": len(created),
            "failed_count": len(failed),
            "created": created,
            "failed": failed,
        }
    )


@router.post("/batch-delete")
async def batch_delete_accounts(
    account_ids: list[int],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Batch delete accounts."""
    manager = AccountManager(db)

    deleted = []
    failed = []

    for account_id in account_ids:
        try:
            await manager.delete_account(account_id)
            deleted.append(account_id)
        except Exception as e:
            failed.append({"id": account_id, "error": str(e)})

    return {
        "code": 0,
        "message": "Batch delete completed",
        "data": {
            "deleted_count": len(deleted),
            "failed_count": len(failed),
            "deleted": deleted,
            "failed": failed,
        }
    }


# =============================================================================
# API Config Endpoints
# =============================================================================

@router.get("/configs")
async def list_api_configs(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all API configurations."""
    manager = AccountManager(db)
    configs = await manager.list_api_configs()

    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": c.id,
                "name": c.name,
                "api_id": c.api_id,
                "api_hash": c.api_hash,
                "description": c.description,
                "account_count": c.account_count,
                "created_at": c.created_at.isoformat() if c.created_at else "",
                "updated_at": c.updated_at.isoformat() if c.updated_at else "",
            }
            for c in configs
        ]
    }


@router.post("/configs", response_model=APIConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_api_config(
    config: APIConfigCreate,
    db: AsyncSession = Depends(get_db),
) -> APIConfigResponse:
    """Create a new API configuration."""
    manager = AccountManager(db)

    try:
        created = await manager.create_api_config(
            name=config.name,
            api_id=config.api_id,
            api_hash=config.api_hash,
            description=config.description,
        )
        return APIConfigResponse(
            id=created.id,
            name=created.name,
            api_id=created.api_id,
            api_hash=created.api_hash,
            description=created.description,
            account_count=created.account_count,
            created_at=created.created_at.isoformat(),
            updated_at=created.updated_at.isoformat(),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/configs/{config_name}")
async def delete_api_config(
    config_name: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete an API configuration."""
    manager = AccountManager(db)

    try:
        await manager.delete_api_config(config_name)
        return {"code": 0, "message": "Config deleted"}
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Config not found")
        if "in use" in str(e).lower():
            raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Statistics Endpoints
# =============================================================================

@router.get("/stats", response_model=AccountStatsResponse)
async def get_account_stats(
    db: AsyncSession = Depends(get_db),
) -> AccountStatsResponse:
    """Get account statistics."""
    manager = AccountManager(db)
    stats = await manager.get_account_health_stats()
    country_dist = await manager.get_country_distribution()

    return AccountStatsResponse(
        code=0,
        message="success",
        data={
            **stats,
            "country_distribution": country_dist,
        }
    )


@router.get("/stats/health")
async def get_account_health(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get account health breakdown."""
    manager = AccountManager(db)
    stats = await manager.get_account_health_stats()

    return {
        "code": 0,
        "message": "success",
        "data": {
            "summary": stats,
            "health_score": (
                stats["online"] / stats["total"] * 100 if stats["total"] > 0 else 0
            ),
            "error_rate": (
                stats["error"] / stats["total"] * 100 if stats["total"] > 0 else 0
            ),
        }
    }


@router.get("/stats/countries")
async def get_country_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get account distribution by country."""
    manager = AccountManager(db)
    distribution = await manager.get_country_distribution()

    total = sum(distribution.values())

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total": total,
            "distribution": {
                country: {
                    "count": count,
                    "percentage": round(count / total * 100, 2) if total > 0 else 0,
                }
                for country, count in distribution.items()
            }
        }
    }


# =============================================================================
# Account Authentication Endpoints (Multi-step Login)
# =============================================================================

class SendCodeRequest(BaseModel):
    """Send verification code request."""
    phone: str = Field(..., description="Phone number with country code (e.g., +1234567890)")
    api_config_name: str = Field(default="default", description="API config name")
    country_code: str = Field(default="US", max_length=2, description="Country code")
    proxy_mode: str = Field(default="dynamic", description="Proxy mode: dynamic/static/none")
    static_proxy_id: Optional[int] = Field(None, description="Static proxy ID")


class SendCodeResponse(BaseModel):
    """Send verification code response."""
    code: int = 0
    message: str = "success"
    data: dict


class VerifyCodeRequest(BaseModel):
    """Verify code request."""
    session_id: str = Field(..., description="Session ID from send-code")
    code: str = Field(..., description="Verification code from SMS/Telegram")


class VerifyCodeResponse(BaseModel):
    """Verify code response."""
    code: int = 0
    message: str = "success"
    data: dict


class Verify2FARequest(BaseModel):
    """Verify 2FA request."""
    session_id: str = Field(..., description="Session ID from verify-code")
    password: str = Field(..., description="2FA password")


class Verify2FAResponse(BaseModel):
    """Verify 2FA response."""
    code: int = 0
    message: str = "success"
    data: dict


class ImportSessionRequest(BaseModel):
    """Import session request."""
    phone: str = Field(..., description="Phone number with country code")
    api_config_name: str = Field(default="default", description="API config name")
    country_code: str = Field(default="US", max_length=2, description="Country code")
    country_name: Optional[str] = Field(None, description="Country name")
    proxy_mode: str = Field(default="dynamic", description="Proxy mode: dynamic/static/none")
    static_proxy_id: Optional[int] = Field(None, description="Static proxy ID")


class ImportSessionResponse(BaseModel):
    """Import session response."""
    code: int = 0
    message: str = "success"
    data: dict


@router.post("/auth/send-code", response_model=SendCodeResponse)
async def send_verification_code(
    request: SendCodeRequest,
    db: AsyncSession = Depends(get_db),
) -> SendCodeResponse:
    """
    Step 1: Send verification code to phone number.

    This initiates the login process. Telegram will send a verification code
    via SMS or Telegram app.
    """
    manager = AccountManager(db)

    # Get API config
    api_config = await _get_api_config_or_default(manager, request.api_config_name)
    if not api_config:
        raise HTTPException(status_code=404, detail=f"API config '{request.api_config_name}' not found")

    try:
        try:
            proxy_mode = normalize_proxy_mode(request.proxy_mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {request.proxy_mode}")
        proxy = await _resolve_account_proxy_for_policy(
            db=db,
            account_type=AccountType.PROMOTER,
            proxy_mode=proxy_mode,
            country_code=request.country_code,
            account_key=request.phone,
            static_proxy_id=request.static_proxy_id,
        )
        if proxy_mode == ProxyMode.STATIC:
            await _ensure_static_proxy_capacity(db, request.static_proxy_id)
        device_profile = _build_telegram_device_profile(phone=request.phone)
        result = await auth_helper.send_code(
            phone=request.phone,
            api_id=api_config.api_id,
            api_hash=api_config.api_hash,
            country_code=request.country_code,
            account_key=request.phone,
            proxy=proxy,
            proxy_required=False,
            device_profile=device_profile,
        )
        result["proxy_mode"] = proxy_mode.value
        result["device_profile"] = device_profile
        result["static_proxy_id"] = request.static_proxy_id if proxy_mode == ProxyMode.STATIC else None

        return SendCodeResponse(
            code=0,
            message="Verification code sent",
            data=result,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/verify-code", response_model=VerifyCodeResponse)
async def verify_verification_code(
    request: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
) -> VerifyCodeResponse:
    """
    Step 2: Verify the code sent to phone.

    If successful, returns user info and session.
    If 2FA is enabled, returns requires_2fa=true.
    """
    try:
        result = await auth_helper.verify_code(
            session_id=request.session_id,
            code=request.code,
        )

        # If login successful (no 2FA), create account in database
        if result.get("status") == "success":
            # Extract session_id to get phone and api_config
            # session_id format: "{phone}_{phone_code_hash}"
            phone = request.session_id.split("_")[0]

            # Check if account already exists
            existing = await db.execute(
                select(TelegramAccount).where(TelegramAccount.phone == phone)
            )
            account = existing.scalar_one_or_none()

            if account:
                # Update existing account
                account.session_string = encrypt_session_string(result["session_string"])
                _apply_device_profile(
                    account,
                    result.get("device_profile") or _build_telegram_device_profile(
                        phone=account.phone,
                        identifier=account.identifier,
                        session_name=account.session_name,
                        account_id=account.id,
                        existing=account,
                    ),
                )
                account.status = AccountStatus.ONLINE
                account.last_connected_at = datetime.utcnow()
                await db.commit()
                await db.refresh(account)
                await AccountEnvironmentGuard(db).record_event(account, "login", details={"source": "verify_code"})
                await _sync_promoter_joined_groups(account, db)
            else:
                # Create new account (will be completed after getting more info from frontend)
                pass

            return VerifyCodeResponse(
                code=0,
                message="Login successful",
                data=result,
            )
        else:
            # 2FA required
            return VerifyCodeResponse(
                code=0,
                message="2FA required",
                data=result,
            )

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/verify-2fa", response_model=Verify2FAResponse)
async def verify_2fa_password(
    request: Verify2FARequest,
    db: AsyncSession = Depends(get_db),
) -> Verify2FAResponse:
    """
    Step 3: Verify 2FA password (if required).

    Only needed if verify-code returned requires_2fa=true.
    """
    try:
        result = await auth_helper.verify_2fa(
            session_id=request.session_id,
            password=request.password,
        )

        return Verify2FAResponse(
            code=0,
            message="Login successful",
            data=result,
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/complete-login", response_model=AccountResponse)
async def complete_account_login(
    account_data: CompleteLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    """
    Complete account creation after successful login.

    This creates the account record in database with the session string.
    """
    manager = AccountManager(db)
    session_string = account_data.session_string

    try:
        # Check if account already exists
        existing = await db.execute(
            select(TelegramAccount).where(TelegramAccount.phone == account_data.phone)
        )
        account = existing.scalar_one_or_none()
        account_already_existed = account is not None

        if account:
            # Update existing account
            try:
                proxy_mode = normalize_proxy_mode(account_data.proxy_mode)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {account_data.proxy_mode}")
            account.session_string = encrypt_session_string(session_string)
            _apply_device_profile(
                account,
                _build_telegram_device_profile(
                    phone=account_data.phone,
                    identifier=account_data.identifier,
                    session_name=account_data.session_name,
                    account_id=account.id,
                    existing=account,
                ),
            )
            account.country_code = account_data.country_code
            account.country_name = account_data.country_name
            account.api_config_name = account_data.api_config_name
            account.identifier = (account_data.identifier or account_data.phone or account.identifier)
            account.display_name = account_data.display_name
            if account_data.profile_bio is not None:
                account.profile_bio = account_data.profile_bio.strip() or None
                account.profile_bio_synced_at = None
            if "registered_at" in account_data.model_fields_set:
                account.registered_at = account_data.registered_at
            if "asset_note" in account_data.model_fields_set:
                account.asset_note = (account_data.asset_note or "").strip()[:255] or None
            if "managed_started_at" in account_data.model_fields_set:
                account.managed_started_at = account_data.managed_started_at
            elif account.managed_started_at is None:
                account.managed_started_at = datetime.utcnow()
            if "warmup_hold_until" in account_data.model_fields_set:
                account.warmup_hold_until = account_data.warmup_hold_until
            if "warmup_note" in account_data.model_fields_set:
                account.warmup_note = (account_data.warmup_note or "").strip()[:255] or None
            account.account_type = AccountType(account_data.account_type)
            account.proxy_mode = proxy_mode
            account.static_proxy_id = account_data.static_proxy_id if proxy_mode == ProxyMode.STATIC else None
            await _ensure_account_proxy_available(account, db)
            if proxy_mode == ProxyMode.STATIC:
                await _ensure_static_proxy_capacity(
                    db,
                    account.static_proxy_id,
                    exclude_account_id=account.id,
                )
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)
        else:
            # Create new account
            try:
                proxy_mode = normalize_proxy_mode(account_data.proxy_mode)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {account_data.proxy_mode}")
            await _resolve_account_proxy_for_policy(
                db=db,
                account_type=AccountType(account_data.account_type),
                proxy_mode=proxy_mode,
                country_code=account_data.country_code,
                account_key=account_data.phone or account_data.identifier or account_data.session_name or "new-account",
                static_proxy_id=account_data.static_proxy_id,
            )
            if proxy_mode == ProxyMode.STATIC:
                await _ensure_static_proxy_capacity(db, account_data.static_proxy_id)
            account = await manager.create_account(
                phone=account_data.phone,
                identifier=account_data.identifier,
                display_name=account_data.display_name,
                profile_bio=(account_data.profile_bio or "").strip() or None,
                asset_tier=AccountAssetTier.UNKNOWN.value,
                registered_at=account_data.registered_at,
                asset_note=(account_data.asset_note or "").strip()[:255] or None,
                managed_started_at=account_data.managed_started_at,
                warmup_hold_until=account_data.warmup_hold_until,
                warmup_note=(account_data.warmup_note or "").strip()[:255] or None,
                account_type=AccountType(account_data.account_type),
                api_config_name=account_data.api_config_name,
                country_code=account_data.country_code,
                country_name=account_data.country_name,
                session_name=account_data.session_name,
                proxy_mode=proxy_mode,
                static_proxy_id=account_data.static_proxy_id if proxy_mode == ProxyMode.STATIC else None,
            )
            account.session_string = encrypt_session_string(session_string)
            _apply_device_profile(
                account,
                _build_telegram_device_profile(
                    phone=account.phone,
                    identifier=account.identifier,
                    session_name=account.session_name,
                    account_id=account.id,
                    existing=account,
                ),
            )
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)

        if account_already_existed:
            await _propagate_account_proxy_policy_change(account)
        await AccountEnvironmentGuard(db).record_event(account, "login", details={"source": "complete_login"})
        await _sync_promoter_joined_groups(account, db)
        return _account_to_response(account)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/import-session", response_model=ImportSessionResponse)
async def import_session_file(
    phone: str = Form(..., description="Phone number with country code"),
    api_config_name: str = Form(default="default", description="API config name"),
    country_code: str = Form(default="US", description="Country code"),
    country_name: Optional[str] = Form(None, description="Country name"),
    profile_bio: Optional[str] = Form(None, description="Telegram public bio"),
    proxy_mode: str = Form(default="dynamic", description="Proxy mode: dynamic/static/none"),
    static_proxy_id: Optional[int] = Form(None, description="Static proxy ID"),
    session_file: UploadFile = File(..., description="Session file (.session)"),
    db: AsyncSession = Depends(get_db),
) -> ImportSessionResponse:
    """
    Import existing Telegram session file.

    Upload a .session file to import an already logged-in account.
    """
    manager = AccountManager(db)

    # Get API config
    api_config = await _get_api_config_or_default(manager, api_config_name)
    if not api_config:
        raise HTTPException(status_code=404, detail=f"API config '{api_config_name}' not found")

    # Validate file extension
    if not session_file.filename.endswith(".session"):
        raise HTTPException(status_code=400, detail="File must be a .session file")

    # Save uploaded file to temporary location
    temp_dir = Path(tempfile.mkdtemp())
    temp_session_path = temp_dir / session_file.filename

    try:
        try:
            resolved_proxy_mode = normalize_proxy_mode(proxy_mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid proxy_mode: {proxy_mode}")
        telethon_proxy = await _resolve_account_proxy_for_policy(
            db=db,
            account_type=AccountType.PROMOTER,
            proxy_mode=resolved_proxy_mode,
            country_code=country_code,
            account_key=phone,
            static_proxy_id=static_proxy_id,
        )
        existing = (
            await db.execute(select(TelegramAccount).where(TelegramAccount.phone == phone))
        ).scalar_one_or_none()
        if resolved_proxy_mode == ProxyMode.STATIC:
            await _ensure_static_proxy_capacity(
                db,
                static_proxy_id,
                exclude_account_id=existing.id if existing else None,
            )
        # Write uploaded file
        with open(temp_session_path, "wb") as f:
            shutil.copyfileobj(session_file.file, f)

        device_profile = _build_telegram_device_profile(phone=phone, existing=existing)

        # Import session
        result = await auth_helper.import_session(
            phone=phone,
            session_file_path=str(temp_session_path),
            api_id=api_config.api_id,
            api_hash=api_config.api_hash,
            country_code=country_code,
            account_key=phone,
            proxy=telethon_proxy,
            proxy_required=False,
            device_profile=device_profile,
        )

        # Create or update account in database
        account_already_existed = existing is not None
        if existing:
            # Update existing account
            existing.country_code = country_code
            existing.country_name = country_name
            existing.api_config_name = api_config_name
            if profile_bio is not None:
                existing.profile_bio = profile_bio.strip()[:70]
                existing.profile_bio_synced_at = None
            existing.proxy_mode = resolved_proxy_mode
            existing.static_proxy_id = static_proxy_id if resolved_proxy_mode == ProxyMode.STATIC else None
            await _ensure_account_proxy_available(existing, db)
            _apply_device_profile(existing, result.get("device_profile") or device_profile)
            existing.session_string = encrypt_session_string(result["session_string"])
            if existing.managed_started_at is None:
                existing.managed_started_at = datetime.utcnow()
            existing.status = AccountStatus.ONLINE
            existing.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(existing)
            account = existing
        else:
            # Create new account
            account = await manager.create_account(
                phone=phone,
                identifier=phone,
                display_name=phone,
                profile_bio=(profile_bio or "").strip()[:70] or None,
                account_type=AccountType.PROMOTER,
                asset_tier=AccountAssetTier.UNKNOWN.value,
                api_config_name=api_config_name,
                country_code=country_code,
                country_name=country_name,
                proxy_mode=resolved_proxy_mode,
                static_proxy_id=static_proxy_id if resolved_proxy_mode == ProxyMode.STATIC else None,
            )
            _apply_device_profile(account, result.get("device_profile") or device_profile)
            account.session_string = encrypt_session_string(result["session_string"])
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)

        if account_already_existed:
            await _propagate_account_proxy_policy_change(account)
        await AccountEnvironmentGuard(db).record_event(account, "import", details={"source": "import_session"})
        await _sync_promoter_joined_groups(account, db)
        return ImportSessionResponse(
            code=0,
            message="Session imported successfully",
            data={
                "account_id": account.id,
                "phone": account.phone,
                "user_id": result.get("user_id"),
                "username": result.get("username"),
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        # Cleanup temp file
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
