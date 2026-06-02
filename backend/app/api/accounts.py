"""
Accounts API Router

RESTful API for Telegram account management with cursor pagination.
"""

from datetime import datetime
from typing import Optional
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.core.account.manager import AccountManager
from app.core.account.auth_helper import TelegramAuthHelper
from app.core.account.models import AccountStatus, AccountType, TelegramAccount, TelegramAPIConfig


router = APIRouter()
auth_helper = TelegramAuthHelper()


# =============================================================================
# Request/Response Models
# =============================================================================

class AccountCreate(BaseModel):
    """Account creation request."""
    phone: Optional[str] = Field(None, description="Phone number with country code")
    identifier: Optional[str] = Field(None, description="Unified account identifier")
    display_name: Optional[str] = Field(None, description="Display name")
    account_type: str = Field(default="promoter", description="Account type: promoter/guardian_bot")
    api_config_name: str = Field(default="default", description="API config name")
    country_code: str = Field(default="US", max_length=2, description="Country code (ISO 3166-1 alpha-2)")
    country_name: Optional[str] = Field(None, description="Country name")
    session_name: Optional[str] = Field(None, description="Custom session name")


class AccountUpdate(BaseModel):
    """Account update request."""
    display_name: Optional[str] = Field(None, description="Display name")
    country_code: Optional[str] = Field(None, max_length=2, description="Country code")
    fingerprint_id: Optional[str] = Field(None, description="Device fingerprint ID")
    is_active: Optional[bool] = Field(None, description="Active status")


class AccountResponse(BaseModel):
    """Account response."""
    id: int
    phone: Optional[str] = None
    identifier: str
    display_name: Optional[str] = None
    account_type: str
    status: str
    country_code: str
    country_name: Optional[str] = None
    api_config_name: str
    fingerprint_id: Optional[str] = None
    session_name: str
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
        account_type=account.account_type.value,
        status=account.status.value,
        country_code=account.country_code,
        country_name=account.country_name,
        api_config_name=account.api_config_name,
        fingerprint_id=account.fingerprint_id,
        session_name=account.session_name,
        is_active=account.is_active,
        connection_count=account.connection_count,
        error_count=account.error_count,
        last_active_at=account.last_active_at.isoformat() if account.last_active_at else None,
        last_connected_at=account.last_connected_at.isoformat() if account.last_connected_at else None,
        created_at=account.created_at.isoformat() if account.created_at else "",
        updated_at=account.updated_at.isoformat() if account.updated_at else "",
    )


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


def _proxy_account_key(account: TelegramAccount) -> str:
    """Stable key used for account-scoped sticky proxy sessions."""
    return account.phone or account.session_name or str(account.id)


async def _ensure_promoter_proxy_available(account: TelegramAccount) -> None:
    """Fail fast if a promoter account cannot obtain its required proxy."""
    if account.account_type != AccountType.PROMOTER:
        return
    if not getattr(settings, "PROMOTER_PROXY_REQUIRED", True):
        return

    provider = getattr(settings, "PROXY_PROVIDER", "evomi").lower()
    try:
        if provider == "decodo":
            from app.core.account.decodo import get_decodo_client

            proxies = await get_decodo_client().get_proxy_for_account(account.country_code)
        else:
            from app.core.account.evomi import get_evomi_client

            proxies = await get_evomi_client().get_proxy_for_account(
                account.country_code,
                account_key=_proxy_account_key(account),
            )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Promoter account proxy is required but unavailable: {exc}",
        ) from exc

    if not proxies:
        raise HTTPException(
            status_code=400,
            detail="Promoter account proxy is required but provider returned no proxy",
        )


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
    manager = AccountManager(db)

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
        created = await manager.create_account(
            phone=account.phone,
            identifier=account.identifier,
            display_name=account.display_name,
            account_type=account_type,
            api_config_name=account.api_config_name,
            country_code=account.country_code,
            country_name=account.country_name,
            session_name=account.session_name,
        )
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
    if not update_kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        updated = await manager.update_account(account_id, **update_kwargs)
        return _account_to_response(updated)
    except Exception as e:
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

    await _ensure_promoter_proxy_available(account)

    # Update status to online
    await manager.update_account_status(account_id, AccountStatus.ONLINE)

    return {
        "code": 0,
        "message": "Connection initiated",
        "data": {"account_id": account_id, "status": "online"}
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
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bind proxy to account (placeholder - needs ProxyManager integration)."""
    manager = AccountManager(db)
    account = await manager.get_account(account_id)

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    return {
        "code": 0,
        "message": "Proxy binding queued",
        "data": {"account_id": account_id, "proxy_id": proxy_id}
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
            account = await manager.create_account(
                phone=acc.phone,
                identifier=acc.identifier,
                display_name=acc.display_name,
                account_type=AccountType(acc.account_type),
                api_config_name=acc.api_config_name,
                country_code=acc.country_code,
                country_name=acc.country_name,
                session_name=acc.session_name,
            )
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
        result = await auth_helper.send_code(
            phone=request.phone,
            api_id=api_config.api_id,
            api_hash=api_config.api_hash,
            country_code=request.country_code,
            account_key=request.phone,
        )

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
            manager = AccountManager(db)

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
                account.session_string = result["session_string"]
                account.status = AccountStatus.ONLINE
                account.last_connected_at = datetime.utcnow()
                await db.commit()
                await db.refresh(account)
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
    account_data: AccountCreate,
    session_string: str = Body(..., description="Session string from verify-code or verify-2fa"),
    db: AsyncSession = Depends(get_db),
) -> AccountResponse:
    """
    Complete account creation after successful login.

    This creates the account record in database with the session string.
    """
    manager = AccountManager(db)

    try:
        # Check if account already exists
        existing = await db.execute(
            select(TelegramAccount).where(TelegramAccount.phone == account_data.phone)
        )
        account = existing.scalar_one_or_none()

        if account:
            # Update existing account
            account.session_string = session_string
            account.country_code = account_data.country_code
            account.country_name = account_data.country_name
            account.api_config_name = account_data.api_config_name
            account.identifier = (account_data.identifier or account_data.phone or account.identifier)
            account.display_name = account_data.display_name
            account.account_type = AccountType(account_data.account_type)
            await _ensure_promoter_proxy_available(account)
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)
        else:
            # Create new account
            account = await manager.create_account(
                phone=account_data.phone,
                identifier=account_data.identifier,
                display_name=account_data.display_name,
                account_type=AccountType(account_data.account_type),
                api_config_name=account_data.api_config_name,
                country_code=account_data.country_code,
                country_name=account_data.country_name,
                session_name=account_data.session_name,
            )
            await _ensure_promoter_proxy_available(account)
            account.session_string = session_string
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)

        return _account_to_response(account)

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/import-session", response_model=ImportSessionResponse)
async def import_session_file(
    phone: str = Form(..., description="Phone number with country code"),
    api_config_name: str = Form(default="default", description="API config name"),
    country_code: str = Form(default="US", description="Country code"),
    country_name: Optional[str] = Form(None, description="Country name"),
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
        # Write uploaded file
        with open(temp_session_path, "wb") as f:
            shutil.copyfileobj(session_file.file, f)

        # Import session
        result = await auth_helper.import_session(
            phone=phone,
            session_file_path=str(temp_session_path),
            api_id=api_config.api_id,
            api_hash=api_config.api_hash,
            country_code=country_code,
            account_key=phone,
        )

        # Create or update account in database
        existing = await db.execute(
            select(TelegramAccount).where(TelegramAccount.phone == phone)
        )
        account = existing.scalar_one_or_none()

        if account:
            # Update existing account
            account.country_code = country_code
            account.country_name = country_name
            account.api_config_name = api_config_name
            await _ensure_promoter_proxy_available(account)
            account.session_string = result["session_string"]
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)
        else:
            # Create new account
            account = await manager.create_account(
                phone=phone,
                identifier=phone,
                display_name=phone,
                account_type=AccountType.PROMOTER,
                api_config_name=api_config_name,
                country_code=country_code,
                country_name=country_name,
            )
            await _ensure_promoter_proxy_available(account)
            account.session_string = result["session_string"]
            account.status = AccountStatus.ONLINE
            account.last_connected_at = datetime.utcnow()
            await db.commit()
            await db.refresh(account)

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

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        # Cleanup temp file
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass
