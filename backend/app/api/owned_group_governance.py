"""Authenticated API for explicitly attaching Guardian governance to owned groups."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.owned_group_audit import require_owned_group_audit_reader
from app.api.owned_groups import require_owned_group_operator
from app.core.database import get_db
from app.modules.owned_group.governance import (
    GovernanceServiceError,
    bind_governance,
    get_governance_status,
    list_eligible_guardian_bots,
    new_correlation_id,
    reconcile_governance,
)

router = APIRouter()


class GovernanceBindRequest(BaseModel):
    guardian_bot_account_id: int = Field(..., gt=0)


class GovernanceResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: dict[str, Any]


class GovernanceCandidateListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[dict[str, Any]]


def _raise_service_error(
    exc: GovernanceServiceError,
    *,
    asset_id: int,
    correlation_id: str,
) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail=exc.detail(asset_id=asset_id, correlation_id=correlation_id),
    ) from exc


@router.get("/{asset_id:int}/governance", response_model=GovernanceResponse)
async def read_owned_group_governance(
    asset_id: int,
    _current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> GovernanceResponse:
    """Return stored state only; this endpoint never probes Telegram or writes."""

    try:
        data = await get_governance_status(db, asset_id)
    except GovernanceServiceError as exc:
        correlation = new_correlation_id(asset_id)
        _raise_service_error(exc, asset_id=asset_id, correlation_id=correlation)
    return GovernanceResponse(data=data)


@router.get(
    "/{asset_id:int}/governance/candidates",
    response_model=GovernanceCandidateListResponse,
)
async def list_owned_group_governance_candidates(
    asset_id: int,
    _current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> GovernanceCandidateListResponse:
    """Return backend-filtered candidates without exposing either bot credential."""

    try:
        data = await list_eligible_guardian_bots(db, asset_id)
    except GovernanceServiceError as exc:
        correlation = new_correlation_id(asset_id)
        _raise_service_error(exc, asset_id=asset_id, correlation_id=correlation)
    return GovernanceCandidateListResponse(data=data)


@router.post("/{asset_id:int}/governance/bind", response_model=GovernanceResponse)
async def bind_owned_group_governance(
    asset_id: int,
    request: GovernanceBindRequest,
    current_user: dict = Depends(require_owned_group_operator),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    db: AsyncSession = Depends(get_db),
) -> GovernanceResponse:
    correlation = new_correlation_id(asset_id, x_correlation_id)
    try:
        data = await bind_governance(
            db,
            asset_id,
            request.guardian_bot_account_id,
            current_user,
            correlation,
        )
    except GovernanceServiceError as exc:
        _raise_service_error(exc, asset_id=asset_id, correlation_id=correlation)
    return GovernanceResponse(data=data)


@router.post("/{asset_id:int}/governance/reconcile", response_model=GovernanceResponse)
async def reconcile_owned_group_governance(
    asset_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    db: AsyncSession = Depends(get_db),
) -> GovernanceResponse:
    correlation = new_correlation_id(asset_id, x_correlation_id)
    try:
        data = await reconcile_governance(db, asset_id, current_user, correlation)
    except GovernanceServiceError as exc:
        _raise_service_error(exc, asset_id=asset_id, correlation_id=correlation)
    return GovernanceResponse(data=data)


__all__ = ["router"]
