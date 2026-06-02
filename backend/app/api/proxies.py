"""
Proxies API Router

RESTful API for proxy management with cursor pagination.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.account.models import Proxy, ProxyType
from app.core.scheduler.tasks import validate_proxy_batch


router = APIRouter()


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


class ProxyUpdate(BaseModel):
    """Proxy update request."""
    address: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)
    protocol: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    status: Optional[str] = None


class ProxyResponse(BaseModel):
    """Proxy response."""
    id: int
    address: str
    port: int
    protocol: str
    username: Optional[str] = None
    latency: Optional[int] = None
    status: str
    bindAccountId: Optional[int] = None
    bindAccountPhone: Optional[str] = None
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

def _proxy_to_response(proxy: Proxy) -> ProxyResponse:
    """Convert Proxy model to response."""
    # Map is_active to status
    if proxy.is_active:
        if proxy.consecutive_failures >= 3:
            status = "error"
        else:
            status = "active"
    else:
        status = "inactive"

    return ProxyResponse(
        id=proxy.id,
        address=proxy.host,
        port=proxy.port,
        protocol=proxy.protocol,
        username=proxy.username,
        latency=proxy.avg_latency if proxy.avg_latency > 0 else None,
        status=status,
        bindAccountId=None,  # TODO: Add relationship to accounts
        bindAccountPhone=None,  # TODO: Add relationship to accounts
        lastCheckedAt=proxy.last_checked.isoformat() if proxy.last_checked else None,
        createdAt=proxy.created_at.isoformat() if proxy.created_at else "",
        updatedAt=proxy.updated_at.isoformat() if proxy.updated_at else "",
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

    return ProxyListResponse(
        code=0,
        message="success",
        data={
            "list": [_proxy_to_response(p) for p in proxies],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        }
    )


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_proxy(
    proxy_data: ProxyCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new proxy."""
    proxy = Proxy(
        proxy_type=ProxyType.DATACENTER,  # Default type
        host=proxy_data.address,
        port=proxy_data.port,
        country="US",  # Default country
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


@router.get("/{proxy_id}", response_model=dict)
async def get_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get proxy by ID."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    return {
        "code": 0,
        "message": "success",
        "data": _proxy_to_response(proxy)
    }


@router.put("/{proxy_id}", response_model=dict)
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
    for field, value in update_data.items():
        setattr(proxy, field, value)

    await db.commit()
    await db.refresh(proxy)

    return {
        "code": 0,
        "message": "success",
        "data": _proxy_to_response(proxy)
    }


@router.delete("/{proxy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete proxy."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    await db.delete(proxy)
    await db.commit()


# =============================================================================
# Proxy Operations
# =============================================================================

@router.post("/{proxy_id}/test")
async def test_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Test proxy connectivity."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    # Placeholder - actual test would be performed by ProxyPool
    # For now, return a mock latency
    import random
    latency = random.randint(50, 500)

    # Update proxy latency
    proxy.avg_latency = latency
    proxy.last_checked = datetime.utcnow()
    await db.commit()

    return {
        "code": 0,
        "message": "success",
        "data": {
            "latency": latency,
        }
    }


@router.post("/refresh-status")
async def refresh_status(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Refresh all proxy statuses."""
    result = await db.execute(select(Proxy).where(Proxy.is_active == True))
    proxies = result.scalars().all()

    # Placeholder - actual refresh would test all proxies
    import random
    for proxy in proxies:
        proxy.avg_latency = random.randint(50, 500)
        proxy.last_checked = datetime.utcnow()

    await db.commit()

    return {
        "code": 0,
        "message": "success",
        "data": {
            "refreshed_count": len(proxies)
        }
    }


@router.post("/{proxy_id}/toggle")
async def toggle_proxy(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Toggle proxy active status."""
    result = await db.execute(select(Proxy).where(Proxy.id == proxy_id))
    proxy = result.scalar_one_or_none()

    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    proxy.is_active = not proxy.is_active
    await db.commit()
    await db.refresh(proxy)

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
            proxy = Proxy(
                proxy_type=ProxyType.DATACENTER,
                host=proxy_data.address,
                port=proxy_data.port,
                country="US",
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
