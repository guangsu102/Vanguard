"""
Proxies API Router

RESTful API for proxy management with cursor pagination.
"""

import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.account.models import Proxy, ProxyMode, ProxyType, TelegramAccount
from app.core.account.pool import invalidate_account_in_all_pools
from app.core.account.proxy_policy_events import publish_account_proxy_policy_changed
from app.core.network.proxy_pool import ProxyPool
from app.core.scheduler.tasks import validate_proxy_batch


router = APIRouter()
MAX_STATIC_PROXY_BINDINGS = 3


async def _bound_static_accounts(db: AsyncSession, proxy_id: int) -> list[TelegramAccount]:
    return list(
        (
            await db.execute(
                select(TelegramAccount).where(TelegramAccount.static_proxy_id == proxy_id)
            )
        ).scalars().all()
    )


async def _invalidate_bound_static_accounts(accounts: list[TelegramAccount], *, reason: str) -> None:
    for account in accounts:
        await invalidate_account_in_all_pools(account.id, reason=reason)
        proxy_mode = getattr(account.proxy_mode, "value", str(account.proxy_mode))
        static_proxy_id = account.static_proxy_id if proxy_mode == ProxyMode.STATIC.value else None
        await publish_account_proxy_policy_changed(
            account.id,
            proxy_mode,
            static_proxy_id,
        )


# =============================================================================
# Request/Response Models
# =============================================================================

class ProxyCreate(BaseModel):
    """Proxy creation request."""
    address: str = Field(..., description="Proxy address/host")
    port: int = Field(..., ge=1, le=65535, description="Proxy port")
    protocol: str = Field(default="http", description="Protocol: http, https, socks5")
    username: Optional[str] = Field(None, description="Proxy username")
    password: Optional[str] = Field(None, description="Proxy password")
    proxy_type: str = Field(default="datacenter", description="Proxy type: residential/datacenter/mobile")
    country: str = Field(default="US", max_length=2, description="Country code")
    country_name: Optional[str] = Field(None, description="Country name")


class ProxyUpdate(BaseModel):
    """Proxy update request."""
    address: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)
    protocol: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    proxy_type: Optional[str] = None
    country: Optional[str] = Field(None, max_length=2)
    country_name: Optional[str] = None
    status: Optional[str] = None


class ProxyResponse(BaseModel):
    """Proxy response."""
    id: int
    address: str
    port: int
    protocol: str
    username: Optional[str] = None
    proxy_type: str
    country: str
    countryName: Optional[str] = None
    latency: Optional[int] = None
    status: str
    bindAccountId: Optional[int] = None
    bindAccountPhone: Optional[str] = None
    bindAccountCount: int = 0
    bindAccounts: list[dict] = Field(default_factory=list)
    remainingBindSlots: int = MAX_STATIC_PROXY_BINDINGS
    lastCheckedAt: Optional[str] = None
    createdAt: str
    updatedAt: str


class ProxyListResponse(BaseModel):
    """Proxy list response with pagination."""
    code: int = 0
    message: str = "success"
    data: dict


class ProxyHealthResponse(BaseModel):
    """Proxy health statistics response."""
    code: int = 0
    message: str = "success"
    data: dict


class ProxyBatchImportRequest(BaseModel):
    """Batch import request."""
    proxies: list[ProxyCreate] = Field(..., min_length=1, max_length=100)


class ProxyBatchImportResponse(BaseModel):
    """Batch import response."""
    code: int = 0
    message: str = "success"
    data: dict


# =============================================================================
# Helper Functions
# =============================================================================

def _proxy_to_response(proxy: Proxy, bound_accounts: Optional[list[TelegramAccount]] = None) -> ProxyResponse:
    """Convert Proxy model to response."""
    bound_accounts = bound_accounts or []
    first_bound_account = bound_accounts[0] if bound_accounts else None
    # Health failures take precedence over a manual inactive state because the
    # health checker also disables proxies after repeated failures.
    if proxy.consecutive_failures >= 3:
        status = "error"
    elif proxy.is_active:
        status = "active"
    else:
        status = "inactive"

    return ProxyResponse(
        id=proxy.id,
        address=proxy.host,
        port=proxy.port,
        protocol=proxy.protocol,
        username=proxy.username,
        proxy_type=proxy.proxy_type.value,
        country=proxy.country,
        countryName=proxy.country_name,
        latency=proxy.avg_latency if proxy.avg_latency > 0 else None,
        status=status,
        bindAccountId=first_bound_account.id if first_bound_account else None,
        bindAccountPhone=first_bound_account.phone if first_bound_account else None,
        bindAccountCount=len(bound_accounts),
        bindAccounts=[
            {
                "id": account.id,
                "phone": account.phone,
                "identifier": account.identifier,
                "status": getattr(account.status, "value", str(account.status)),
            }
            for account in bound_accounts
        ],
        remainingBindSlots=max(MAX_STATIC_PROXY_BINDINGS - len(bound_accounts), 0),
        lastCheckedAt=proxy.last_checked.isoformat() if proxy.last_checked else None,
        createdAt=proxy.created_at.isoformat() if proxy.created_at else "",
        updatedAt=proxy.updated_at.isoformat() if proxy.updated_at else "",
    )


def _csv_response(filename: str, rows: list[dict], fieldnames: list[str]) -> Response:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =============================================================================
# Proxy CRUD Endpoints
# =============================================================================

@router.get("", response_model=ProxyListResponse)
async def list_proxies(
    page: int = 1,
    pageSize: int = 20,
    protocol: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> ProxyListResponse:
    """
    Get list of proxies with pagination.

    - page: Page number (starts from 1)
    - pageSize: Number of items per page
    - protocol: Filter by protocol (http, https, socks5)
    - status: Filter by status (active, inactive, error)
    - keyword: Search by address/port
    """
    query = select(Proxy)
    count_query = select(func.count(Proxy.id))

    # Filters
    if protocol:
        query = query.where(Proxy.protocol == protocol)
        count_query = count_query.where(Proxy.protocol == protocol)

    if status:
        if status == "active":
            query = query.where(Proxy.is_active == True, Proxy.consecutive_failures < 3)
            count_query = count_query.where(Proxy.is_active == True, Proxy.consecutive_failures < 3)
        elif status == "inactive":
            query = query.where(Proxy.is_active == False)
            count_query = count_query.where(Proxy.is_active == False)
        elif status == "error":
            query = query.where(Proxy.consecutive_failures >= 3)
            count_query = count_query.where(Proxy.consecutive_failures >= 3)

    if keyword:
        search_pattern = f"%{keyword}%"
        query = query.where(Proxy.host.like(search_pattern))
        count_query = count_query.where(Proxy.host.like(search_pattern))

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get data with pagination
    offset = (page - 1) * pageSize
    query = query.order_by(desc(Proxy.id)).offset(offset).limit(pageSize)
    result = await db.execute(query)
    proxies = list(result.scalars().all())
    proxy_ids = [p.id for p in proxies]
    bound_accounts: dict[int, list[TelegramAccount]] = {}
    if proxy_ids:
        account_result = await db.execute(
            select(TelegramAccount).where(TelegramAccount.static_proxy_id.in_(proxy_ids))
        )
        for account in account_result.scalars().all():
            if account.static_proxy_id is not None:
                bound_accounts.setdefault(account.static_proxy_id, []).append(account)

    return ProxyListResponse(
        code=0,
        message="success",
        data={
            "list": [_proxy_to_response(p, bound_accounts.get(p.id, [])) for p in proxies],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        }
    )


@router.get("/export")
async def export_proxies(
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export proxy inventory as CSV without sensitive credentials."""
    result = await db.execute(select(Proxy).order_by(desc(Proxy.id)))
    proxies = list(result.scalars().all())
    proxy_ids = [proxy.id for proxy in proxies]
    bound_accounts: dict[int, list[TelegramAccount]] = {}
    if proxy_ids:
        account_result = await db.execute(
            select(TelegramAccount).where(TelegramAccount.static_proxy_id.in_(proxy_ids))
        )
        for account in account_result.scalars().all():
            if account.static_proxy_id is not None:
                bound_accounts.setdefault(account.static_proxy_id, []).append(account)

    rows = []
    for proxy in proxies:
        response = _proxy_to_response(proxy, bound_accounts.get(proxy.id, [])).model_dump()
        rows.append({
            "id": response["id"],
            "address": response["address"],
            "port": response["port"],
            "protocol": response["protocol"],
            "proxy_type": response["proxy_type"],
            "country": response["country"],
            "country_name": response["countryName"] or "",
            "latency": response["latency"] or "",
            "status": response["status"],
            "bind_account_count": response["bindAccountCount"],
            "remaining_bind_slots": response["remainingBindSlots"],
            "last_checked_at": response["lastCheckedAt"] or "",
            "created_at": response["createdAt"],
            "updated_at": response["updatedAt"],
        })

    return _csv_response(
        "vanguard-proxies.csv",
        rows,
        [
            "id",
            "address",
            "port",
            "protocol",
            "proxy_type",
            "country",
            "country_name",
            "latency",
            "status",
            "bind_account_count",
            "remaining_bind_slots",
            "last_checked_at",
            "created_at",
            "updated_at",
        ],
    )


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_proxy(
    proxy_data: ProxyCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new proxy."""
    try:
        proxy_type = ProxyType(proxy_data.proxy_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid proxy_type: {proxy_data.proxy_type}")
    proxy = Proxy(
        proxy_type=proxy_type,
        host=proxy_data.address,
        port=proxy_data.port,
        country=proxy_data.country.upper(),
        country_name=proxy_data.country_name,
        protocol=proxy_data.protocol,
        username=proxy_data.username,
        password=proxy_data.password,
        is_active=True,
    )

    db.add(proxy)
    await db.commit()
    await db.refresh(proxy)

    return {
        "code": 0,
        "message": "success",
        "data": _proxy_to_response(proxy)
    }


@router.get("/{proxy_id:int}", response_model=dict)
async def get_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get proxy by ID."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    bound = list((
        await db.execute(select(TelegramAccount).where(TelegramAccount.static_proxy_id == proxy.id))
    ).scalars().all())

    return {
        "code": 0,
        "message": "success",
        "data": _proxy_to_response(proxy, bound)
    }


@router.put("/{proxy_id:int}", response_model=dict)
async def update_proxy(
    proxy_id: int,
    proxy_data: ProxyUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update proxy."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    bound_accounts = await _bound_static_accounts(db, proxy_id)
    update_data = proxy_data.model_dump(exclude_none=True)

    # Map frontend fields to backend fields
    if "address" in update_data:
        proxy.host = update_data.pop("address")
    if "status" in update_data:
        status_value = update_data.pop("status")
        if status_value == "active":
            proxy.is_active = True
            proxy.consecutive_failures = 0
        elif status_value == "inactive":
            proxy.is_active = False
        elif status_value == "error":
            proxy.consecutive_failures = 3

    # Apply remaining updates
    if "proxy_type" in update_data:
        try:
            update_data["proxy_type"] = ProxyType(update_data["proxy_type"])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid proxy_type: {update_data['proxy_type']}")
    if "country" in update_data:
        update_data["country"] = update_data["country"].upper()
    # Passwords are intentionally omitted from proxy responses. Treat a blank
    # edit value as "keep the existing password", never as a credential reset.
    if "password" in update_data and not str(update_data["password"] or "").strip():
        update_data.pop("password")
    for field, value in update_data.items():
        setattr(proxy, field, value)

    await db.commit()
    await db.refresh(proxy)
    await _invalidate_bound_static_accounts(bound_accounts, reason="static_proxy_updated")

    return {
        "code": 0,
        "message": "success",
        "data": _proxy_to_response(proxy)
    }


@router.delete("/{proxy_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete proxy."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    bound_accounts = await _bound_static_accounts(db, proxy_id)
    await db.delete(proxy)
    await db.commit()
    await _invalidate_bound_static_accounts(bound_accounts, reason="static_proxy_deleted")


# =============================================================================
# Proxy Operations
# =============================================================================

@router.post("/{proxy_id:int}/test")
async def test_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Test proxy connectivity."""
    pool = ProxyPool(db)
    await pool.sync_from_db()
    result = await pool.health_check(proxy_id=proxy_id)
    item = result.get(proxy_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    if not item.get("success"):
        raise HTTPException(status_code=400, detail=item.get("error") or "Proxy test failed")

    return {
        "code": 0,
        "message": "success",
        "data": {
            "latency": item.get("latency", -1),
            "status": item.get("status"),
        }
    }


@router.post("/refresh-status")
async def refresh_status(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Refresh all proxy statuses."""
    pool = ProxyPool(db)
    await pool.sync_from_db()
    result = await pool.validate_batch([p.id for p in await pool.list_proxies(active_only=False)])

    return {
        "code": 0,
        "message": "success",
        "data": {
            "refreshed_count": result["total"],
            "valid": result["valid"],
            "invalid": result["invalid"],
        }
    }


@router.post("/{proxy_id:int}/toggle")
async def toggle_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Toggle proxy active status."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    bound_accounts = await _bound_static_accounts(db, proxy_id)
    proxy.is_active = not proxy.is_active
    await db.commit()
    await db.refresh(proxy)
    await _invalidate_bound_static_accounts(bound_accounts, reason="static_proxy_toggled")

    return {
        "code": 0,
        "message": "Proxy status toggled",
        "data": {
            "proxy_id": proxy_id,
            "is_active": proxy.is_active,
        }
    }


# =============================================================================
# Batch Operations
# =============================================================================

@router.post("/batch-import", response_model=ProxyBatchImportResponse)
async def batch_import_proxies(
    request: ProxyBatchImportRequest,
    db: AsyncSession = Depends(get_db),
) -> ProxyBatchImportResponse:
    """Batch import proxies."""
    created = []
    failed = []

    for proxy_data in request.proxies:
        try:
            proxy_type = ProxyType(proxy_data.proxy_type)
            proxy = Proxy(
                proxy_type=proxy_type,
                host=proxy_data.address,
                port=proxy_data.port,
                country=proxy_data.country.upper(),
                country_name=proxy_data.country_name,
                protocol=proxy_data.protocol,
                username=proxy_data.username,
                password=proxy_data.password,
                is_active=True,
            )
            db.add(proxy)
            created.append({"address": proxy_data.address, "port": proxy_data.port})
        except Exception as e:
            failed.append({
                "address": proxy_data.address,
                "port": proxy_data.port,
                "error": str(e)
            })

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        return ProxyBatchImportResponse(
            code=0,
            message="Batch import failed",
            data={
                "created_count": 0,
                "failed_count": len(request.proxies),
                "created": [],
                "failed": [{"error": str(e)}],
            }
        )

    # Get created proxies' IDs
    result = await db.execute(
        select(Proxy).where(
            Proxy.host.in_([c["address"] for c in created])
        )
    )
    created_proxies = result.scalars().all()

    return ProxyBatchImportResponse(
        code=0,
        message="Batch import completed",
        data={
            "created_count": len(created_proxies),
            "failed_count": len(failed),
            "created": [
                {"id": p.id, "address": p.host, "port": p.port}
                for p in created_proxies
            ],
            "failed": failed,
        }
    )


@router.post("/batch-delete")
async def batch_delete_proxies(
    proxy_ids: list[int],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Batch delete proxies."""
    result = await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
    proxies = result.scalars().all()

    deleted_count = 0
    for proxy in proxies:
        await db.delete(proxy)
        deleted_count += 1

    await db.commit()

    return {
        "code": 0,
        "message": "Batch delete completed",
        "data": {
            "deleted_count": deleted_count,
            "failed_count": len(proxy_ids) - deleted_count,
        }
    }


@router.post("/batch-validate", status_code=status.HTTP_202_ACCEPTED)
async def batch_validate_proxies(
    proxy_ids: Optional[list[int]] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Batch validate proxies."""
    if proxy_ids:
        result = await db.execute(select(Proxy).where(Proxy.id.in_(proxy_ids)))
    else:
        result = await db.execute(select(Proxy).where(Proxy.is_active == True))

    proxies = list(result.scalars().all())
    selected_proxy_ids = [proxy.id for proxy in proxies]

    try:
        async_result = validate_proxy_batch.apply_async(args=[selected_proxy_ids], queue="proxy_validation")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Proxy validation queue unavailable: {exc}") from exc

    return {
        "code": 0,
        "message": "Batch validation initiated",
        "data": {
            "total_proxies": len(proxies),
            "queued": True,
            "status": "queued",
            "task_name": "validate_proxy_batch",
            "task_id": async_result.id,
        }
    }


# =============================================================================
# Health & Statistics Endpoints
# =============================================================================

@router.get("/health", response_model=ProxyHealthResponse)
async def proxy_health(
    db: AsyncSession = Depends(get_db),
) -> ProxyHealthResponse:
    """Get proxy health statistics."""
    # Count by status
    total_result = await db.execute(select(func.count(Proxy.id)))
    total = total_result.scalar() or 0

    active_result = await db.execute(
        select(func.count(Proxy.id)).where(Proxy.is_active == True)
    )
    active = active_result.scalar() or 0

    # Average metrics
    avg_latency_result = await db.execute(
        select(func.avg(Proxy.avg_latency)).where(Proxy.avg_latency > 0)
    )
    avg_latency = avg_latency_result.scalar() or 0

    avg_success_result = await db.execute(
        select(func.avg(Proxy.success_rate))
    )
    avg_success = avg_success_result.scalar() or 0

    # Count by type
    type_counts = {}
    for ptype in ProxyType:
        count_result = await db.execute(
            select(func.count(Proxy.id)).where(Proxy.proxy_type == ptype)
        )
        type_counts[ptype.value] = count_result.scalar() or 0

    # Count by country
    country_result = await db.execute(
        select(Proxy.country, func.count(Proxy.id))
        .group_by(Proxy.country)
        .order_by(func.count(Proxy.id).desc())
        .limit(10)
    )
    top_countries = {row[0]: row[1] for row in country_result.all()}

    return ProxyHealthResponse(
        code=0,
        message="success",
        data={
            "total": total,
            "active": active,
            "inactive": total - active,
            "avg_latency_ms": round(avg_latency, 2),
            "avg_success_rate": round(avg_success * 100, 2),
            "by_type": type_counts,
            "top_countries": top_countries,
        }
    )


@router.get("/stats")
async def proxy_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get proxy usage statistics."""
    # Total counts
    total_result = await db.execute(select(func.count(Proxy.id)))
    total = total_result.scalar() or 0

    # Health breakdown
    healthy_result = await db.execute(
        select(func.count(Proxy.id)).where(
            Proxy.is_active == True,
            Proxy.consecutive_failures < 3
        )
    )
    healthy = healthy_result.scalar() or 0

    unhealthy_result = await db.execute(
        select(func.count(Proxy.id)).where(
            Proxy.consecutive_failures >= 3
        )
    )
    unhealthy = unhealthy_result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total": total,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "health_rate": round(healthy / total * 100, 2) if total > 0 else 0,
        }
    }
