"""Operational controls for the self-owned group worker.

This router only mutates persisted state and queues work for the worker.  It
does not call Telegram directly.  The control endpoints are kept separate
from the draft/selection API so the execution boundary can evolve without
changing the creation contract.
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.owned_groups import require_owned_group_operator
from app.core.database import get_db
from app.core.p0_safety_gate import (
    precheck_owned_group_resources,
    require_owned_group_execution_enabled,
)
from app.modules.owned_group.models import OwnedGroupAsset, OwnedGroupOperation
from app.modules.owned_group.security import redact_sensitive_text
from app.modules.owned_group.worker import (
    enqueue_asset_precheck,
    pause_owned_group_operation,
    reconcile_owned_group_asset,
    reconcile_owned_group_operation,
    resume_owned_group_operation,
    retry_owned_group_operation,
    stop_owned_group_operation,
)

router = APIRouter()
_PUBLIC_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TELEGRAM_CHAT_ID_MIN = -(2**63)
_TELEGRAM_CHAT_ID_MAX = 2**63 - 1


class OwnedGroupControlResponse(BaseModel):
    id: int
    group_asset_id: int | None = None
    status: str
    message: str
    reason_code: str | None = None
    updated_at: datetime | None = None


class OwnedGroupOperationDetailResponse(OwnedGroupControlResponse):
    planned_count: int
    completed_count: int
    skipped_count: int
    failed_count: int
    last_error: str | None = None
    schedule_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class OwnedGroupAssetReconcileRequest(BaseModel):
    """Candidate remote group for a create-timeout recovery.

    The value may be omitted when the create worker persisted the Telegram id
    before a later setup RPC failed.
    """

    telegram_chat_id: int | None = Field(default=None)
    # Optional replacement for a public username whose first assignment failed
    # (for example because another group claimed it).  It is only a candidate;
    # Telegram verification in the guarded adapter is still authoritative.
    telegram_username: str | None = Field(default=None, max_length=64)

    @field_validator("telegram_chat_id")
    @classmethod
    def validate_chat_id(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not _TELEGRAM_CHAT_ID_MIN <= value <= _TELEGRAM_CHAT_ID_MAX:
            raise ValueError("telegram_chat_id must be a non-zero signed 64-bit integer")
        if value == 0:
            raise ValueError("telegram_chat_id must be a non-zero signed 64-bit integer")
        return value

    @field_validator("telegram_username")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized.startswith("@"):
            normalized = normalized[1:]
        if not normalized:
            return None
        if not _PUBLIC_USERNAME.fullmatch(normalized):
            raise ValueError("telegram_username must be a valid public Telegram username")
        return normalized


async def require_owned_group_admin(
    current_user: dict = Depends(require_owned_group_operator),
) -> dict:
    """Restrict manual remote-chat binding to administrators."""

    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Owned group admin access required")
    return current_user


def _asset_control_response(
    asset: OwnedGroupAsset, message: str, *, reason_code: str | None = None
) -> OwnedGroupControlResponse:
    return OwnedGroupControlResponse(
        id=asset.id,
        group_asset_id=asset.id,
        status=asset.status,
        message=message,
        reason_code=reason_code,
        updated_at=asset.updated_at,
    )


def _operation_control_response(
    operation: OwnedGroupOperation, message: str
) -> OwnedGroupControlResponse:
    return OwnedGroupControlResponse(
        id=operation.id,
        group_asset_id=operation.group_asset_id,
        status=operation.status,
        message=message,
        updated_at=operation.updated_at,
    )


def _operation_detail(operation: OwnedGroupOperation) -> OwnedGroupOperationDetailResponse:
    return OwnedGroupOperationDetailResponse(
        id=operation.id,
        group_asset_id=operation.group_asset_id,
        status=operation.status,
        message="success",
        updated_at=operation.updated_at,
        planned_count=operation.planned_count,
        completed_count=operation.completed_count,
        skipped_count=operation.skipped_count,
        failed_count=operation.failed_count,
        # ``last_error`` may originate at the Telegram boundary.  Keep the
        # control endpoint subject to the same token/session/invite redaction as
        # the main operation-detail API.
        last_error=(redact_sensitive_text(operation.last_error) if operation.last_error else None),
        schedule_at=operation.schedule_at,
        started_at=operation.started_at,
        finished_at=operation.finished_at,
    )


@router.post(
    "/{asset_id:int}/precheck",
    response_model=OwnedGroupControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_owned_group_precheck(
    asset_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    """Move a draft into PRECHECKING for the persistent worker.

    The worker tick performs the actual adapter preflight.  Returning 202
    while the asset is already PRECHECKING is intentionally idempotent.
    """

    asset_row = await db.get(OwnedGroupAsset, asset_id)
    if asset_row is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    decision = await precheck_owned_group_resources(
        db,
        [{"resource_type": "user", "resource_id": asset_row.owner_account_id}],
        asset_row.owner_account_id,
        require_runtime_ready=True,
    )
    if not decision.allowed:
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if decision.reason in {"owned_group_module_disabled", "global_stop_enabled"}
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise HTTPException(
            status_code=code,
            detail={"reason": decision.reason, "details": decision.details},
        )
    try:
        asset = await enqueue_asset_precheck(
            db, asset_id, actor_id=int(current_user.get("id")) if current_user.get("id") else None
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _asset_control_response(asset, "Owned group precheck queued")


async def _mutating_adapter(db: AsyncSession):
    """Build the guarded concrete adapter for an explicitly mutating recovery."""

    await require_owned_group_execution_enabled()
    try:
        from app.modules.owned_group.factory import build_owned_group_adapter

        adapter = build_owned_group_adapter(db)
    except Exception as exc:  # pragma: no cover - runtime dependency failures
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_adapter_unavailable"},
        ) from exc
    if (
        adapter is None
        or not callable(getattr(adapter, "preflight", None))
        or not callable(getattr(adapter, "create_group", None))
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_adapter_unavailable"},
        )
    return adapter


@router.post(
    "/{asset_id:int}/reconcile",
    response_model=OwnedGroupControlResponse,
)
async def reconcile_owned_group_asset_control(
    asset_id: int,
    payload: OwnedGroupAssetReconcileRequest | None = None,
    current_user: dict = Depends(require_owned_group_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    """Verify/recover a known remote group; this path never creates a group."""

    if await db.get(OwnedGroupAsset, asset_id) is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    adapter = await _mutating_adapter(db)
    try:
        result = await reconcile_owned_group_asset(
            db,
            asset_id,
            telegram_chat_id=payload.telegram_chat_id if payload else None,
            telegram_username=payload.telegram_username if payload else None,
            adapter=adapter,
            actor_id=int(current_user.get("id")) if current_user.get("id") else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": redact_sensitive_text(str(exc))},
        ) from exc
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    asset = await db.get(OwnedGroupAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    reason_code = str(result["reason_code"]) if result.get("reason_code") else None
    message = (
        "Owned group asset reconciled"
        if asset.status == "ready"
        else "Owned group asset still requires attention"
    )
    return _asset_control_response(asset, message, reason_code=reason_code)


@router.get("/operations/{operation_id:int}", response_model=OwnedGroupOperationDetailResponse)
async def get_owned_group_operation(
    operation_id: int,
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationDetailResponse:
    """Read operation progress; all authenticated users may observe it."""

    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Owned group operation not found")
    return _operation_detail(operation)


async def _mutate_operation(
    operation_id: int,
    action,
    message: str,
    current_user: dict,
    db: AsyncSession,
) -> OwnedGroupControlResponse:
    try:
        operation = await action(
            db,
            operation_id,
            actor_id=int(current_user.get("id")) if current_user.get("id") else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _operation_control_response(operation, message)


@router.post(
    "/operations/{operation_id:int}/pause",
    response_model=OwnedGroupControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def pause_owned_group(
    operation_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    return await _mutate_operation(
        operation_id, pause_owned_group_operation, "Owned group operation paused", current_user, db
    )


@router.post(
    "/operations/{operation_id:int}/resume",
    response_model=OwnedGroupControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_owned_group(
    operation_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    return await _mutate_operation(
        operation_id,
        resume_owned_group_operation,
        "Owned group operation resumed",
        current_user,
        db,
    )


@router.post(
    "/operations/{operation_id:int}/stop",
    response_model=OwnedGroupControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def stop_owned_group(
    operation_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    return await _mutate_operation(
        operation_id,
        stop_owned_group_operation,
        "Owned group operation stop requested",
        current_user,
        db,
    )


@router.post(
    "/operations/{operation_id:int}/retry",
    response_model=OwnedGroupControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_owned_group(
    operation_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    return await _mutate_operation(
        operation_id,
        retry_owned_group_operation,
        "Owned group operation retry queued",
        current_user,
        db,
    )


@router.post(
    "/operations/{operation_id:int}/reconcile",
    response_model=OwnedGroupControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reconcile_owned_group(
    operation_id: int,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupControlResponse:
    # Resolve the identifier before constructing the Telegram boundary.  A
    # missing operation should be a cheap deterministic 404 even when a worker
    # dependency (Telethon/account pool) is unavailable.
    if await db.get(OwnedGroupOperation, operation_id) is None:
        raise HTTPException(status_code=404, detail="Owned group operation not found")
    try:
        # Reconciliation is read-only, so it remains available while the
        # mutating execution switch is off.  It still uses the concrete adapter
        # and Redis-backed safety wiring rather than silently falling back to a
        # no-op adapter, which would leave every item UNKNOWN forever.
        from app.modules.owned_group.factory import build_owned_group_adapter

        try:
            adapter = build_owned_group_adapter(db, allow_read_only=True)
        except Exception as exc:  # pragma: no cover - runtime dependency failures
            # Do not leak import/credential/transport details through a control
            # endpoint.  A missing adapter must fail closed instead of silently
            # recording another unresolved no-op reconciliation.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": "owned_group_adapter_unavailable"},
            ) from exc
        if adapter is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": "owned_group_adapter_unavailable"},
            )
        result = await reconcile_owned_group_operation(db, operation_id, adapter=adapter)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Owned group operation not found")
    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Owned group operation not found")
    return _operation_control_response(operation, "Owned group operation reconciliation queued")


__all__ = ["router"]
