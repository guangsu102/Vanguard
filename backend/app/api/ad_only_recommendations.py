"""Admin API for ad-only recommendations and recoverable handovers."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.automation_settings import (
    get_ad_only_recommendation_settings,
    save_ad_only_recommendation_settings,
)
from app.core.database import get_db
from app.core.scheduler.tasks import (
    evaluate_ad_only_candidates_task,
    execute_ad_only_handover_task,
    rollback_ad_only_handover_task,
)
from app.core.security import require_admin
from app.modules.acquisition.ad_only_recommendation import (
    AdOnlyRecommendationService,
    AdOnlyWorkflowError,
)
from app.modules.acquisition.models import AdCreative, GroupAdHandover

router = APIRouter(prefix="/ad-only")


class AdOnlySettingsUpdate(BaseModel):
    recommendation_enabled: bool = False
    handover_execution_enabled: bool = False
    min_consecutive_samples: int = Field(10, ge=1, le=100)
    required_send_success_percent: int = Field(100, ge=50, le=100)
    required_survival_24h_percent: int = Field(100, ge=50, le=100)
    peer_ad_min_messages: int = Field(1, ge=1, le=50)
    peer_ad_min_senders: int = Field(1, ge=1, le=50)
    peer_ad_min_survival_hours: int = Field(24, ge=1, le=168)
    peer_ad_lookback_days: int = Field(14, ge=1, le=90)
    risk_lookback_days: int = Field(30, ge=1, le=180)
    recommendation_ttl_days: int = Field(7, ge=1, le=30)
    evaluation_interval_minutes: int = Field(60, ge=15, le=1440)


class AssessmentDecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "defer"]
    note: str | None = Field(None, max_length=500)


class HandoverPreflightRequest(BaseModel):
    assessment_id: int = Field(..., gt=0)
    target_account_id: int = Field(..., gt=0)
    creative_id: int = Field(..., gt=0)
    invite_link: str = Field(..., min_length=4, max_length=512)
    send_mode: Literal["interval", "scheduled"]
    interval_minutes: int = Field(180, ge=1, le=10080)
    scheduled_times: list[str] = Field(default_factory=list, max_length=24)


class HandoverCreateRequest(HandoverPreflightRequest):
    idempotency_key: str = Field(..., min_length=8, max_length=64)


def _workflow_error(exc: AdOnlyWorkflowError) -> HTTPException:
    detail = str(exc)
    if detail.endswith("_not_found") or detail in {
        "assessment_not_found",
        "handover_not_found",
        "group_not_found",
    }:
        return HTTPException(status_code=404, detail=detail)
    conflicts = {
        "assessment_is_not_latest",
        "assessment_expired",
        "assessment_approval_required",
        "active_handover_already_exists",
        "group_already_handed_over",
        "handover_not_retryable",
        "completed_handover_cannot_be_rolled_back",
        "handover_execution_disabled",
    }
    return HTTPException(
        status_code=409 if detail in conflicts else 400,
        detail=detail,
    )


def _queue(task: Any, *, kwargs: dict[str, Any]) -> str:
    try:
        result = task.apply_async(kwargs=kwargs, queue="automation")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Automation queue unavailable: {exc}",
        ) from exc
    return str(result.id)


@router.get("/settings")
async def get_settings(
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return {
        "code": 0,
        "message": "success",
        "data": await get_ad_only_recommendation_settings(db),
    }


@router.put("/settings")
async def update_settings(
    request: AdOnlySettingsUpdate,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    saved = await save_ad_only_recommendation_settings(
        db, request.model_dump()
    )
    return {"code": 0, "message": "success", "data": saved}


@router.post("/evaluations", status_code=status.HTTP_202_ACCEPTED)
async def queue_evaluation(
    limit: int = Query(default=200, ge=1, le=1000),
    force: bool = Query(default=False),
    _current_user: dict = Depends(require_admin),
) -> dict[str, Any]:
    task_id = _queue(
        evaluate_ad_only_candidates_task,
        kwargs={"limit": limit, "force": force},
    )
    return {
        "code": 0,
        "message": "Ad-only candidate evaluation queued",
        "data": {"task_id": task_id},
    }


@router.get("/candidates")
async def list_candidates(
    assessment_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await AdOnlyRecommendationService(db).list_latest_assessments(
        status=assessment_status,
        limit=limit,
    )
    return {"code": 0, "message": "success", "data": data}


@router.get("/groups/{group_id}/history")
async def get_group_history(
    group_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await AdOnlyRecommendationService(db).assessment_history(
        group_id, limit=limit
    )
    return {"code": 0, "message": "success", "data": data}


@router.post("/assessments/{assessment_id}/decision")
async def decide_assessment(
    assessment_id: int,
    request: AssessmentDecisionRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        data = await AdOnlyRecommendationService(db).decide_assessment(
            assessment_id,
            decision=request.decision,
            actor_user_id=int(current_user["id"]),
            note=request.note,
        )
    except AdOnlyWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return {"code": 0, "message": "success", "data": data}


@router.get("/options")
async def get_handover_options(
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    account_rows = await db.execute(
        select(TelegramAccount, AccountOperationConfig)
        .join(
            AccountOperationConfig,
            AccountOperationConfig.account_id == TelegramAccount.id,
        )
        .where(
            TelegramAccount.account_type == AccountType.PROMOTER,
            TelegramAccount.is_active,
            TelegramAccount.status.notin_(
                [AccountStatus.ERROR, AccountStatus.BANNED]
            ),
            TelegramAccount.risk_level.in_(
                [AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value]
            ),
            AccountOperationConfig.enabled,
            AccountOperationConfig.auto_ads_enabled,
            AccountOperationConfig.operation_mode
            == AccountOperationMode.AD_ONLY.value,
        )
        .order_by(TelegramAccount.id.asc())
    )
    creative_rows = await db.execute(
        select(AdCreative)
        .where(AdCreative.enabled)
        .order_by(AdCreative.name.asc(), AdCreative.id.asc())
    )
    return {
        "code": 0,
        "message": "success",
        "data": {
            "accounts": [
                {
                    "id": account.id,
                    "label": account.display_name
                    or account.identifier
                    or account.phone
                    or f"account-{account.id}",
                    "status": getattr(account.status, "value", account.status),
                    "risk_level": account.risk_level,
                    "max_messages_per_day": operation.max_messages_per_day,
                }
                for account, operation in account_rows.all()
            ],
            "creatives": [
                {"id": creative.id, "name": creative.name}
                for creative in creative_rows.scalars().all()
            ],
        },
    }


@router.post("/handovers/preflight")
async def preflight_handover(
    request: HandoverPreflightRequest,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        values = await AdOnlyRecommendationService(db).preflight_handover(
            **request.model_dump()
        )
    except AdOnlyWorkflowError as exc:
        raise _workflow_error(exc) from exc
    return {
        "code": 0,
        "message": "Preflight passed",
        "data": {
            "assessment_id": values["assessment"].id,
            "group_id": values["group"].id,
            "target_account_id": values["target"].id,
            "creative_id": values["creative"].id,
            "send_mode": values["send_mode"],
            "interval_minutes": values["interval_minutes"],
            "scheduled_times": values["scheduled_times"],
            "estimated_daily_sends": values["estimated_daily_sends"],
            "existing_daily_sends": values["existing_daily_sends"],
            "hard_cap": values["hard_cap"],
            "invite_kind": values["invite_kind"],
        },
    }


@router.post("/handovers", status_code=status.HTTP_202_ACCEPTED)
async def create_handover(
    request: HandoverCreateRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    service = AdOnlyRecommendationService(db)
    try:
        handover, created = await service.create_handover(
            **request.model_dump(),
            requested_by_user_id=int(current_user["id"]),
        )
    except AdOnlyWorkflowError as exc:
        raise _workflow_error(exc) from exc
    task_id = None
    if created or handover.status == "queued":
        task_id = _queue(
            execute_ad_only_handover_task,
            kwargs={"handover_id": handover.id},
        )
    return {
        "code": 0,
        "message": "Ad-only handover queued" if created else "Idempotent replay",
        "data": {
            "task_id": task_id,
            "created": created,
            "handover": service.handover_payload(handover),
        },
    }


@router.get("/handovers")
async def list_handovers(
    group_id: int | None = Query(default=None, gt=0),
    handover_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await AdOnlyRecommendationService(db).list_handovers(
        group_id=group_id,
        status=handover_status,
        limit=limit,
    )
    return {"code": 0, "message": "success", "data": data}


@router.post("/handovers/{handover_id}/retry", status_code=202)
async def retry_handover(
    handover_id: int,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    service = AdOnlyRecommendationService(db)
    try:
        handover = await service.prepare_retry(
            handover_id, actor_user_id=int(current_user["id"])
        )
    except AdOnlyWorkflowError as exc:
        raise _workflow_error(exc) from exc
    task_id = _queue(
        execute_ad_only_handover_task,
        kwargs={"handover_id": handover.id},
    )
    return {
        "code": 0,
        "message": "Handover retry queued",
        "data": {
            "task_id": task_id,
            "handover": service.handover_payload(handover),
        },
    }


@router.post("/handovers/{handover_id}/rollback", status_code=202)
async def rollback_handover(
    handover_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    handover = await db.get(GroupAdHandover, handover_id)
    if handover is None:
        raise HTTPException(status_code=404, detail="handover_not_found")
    if handover.status in {"completed", "rolled_back", "cancelled", "running"}:
        raise HTTPException(
            status_code=409,
            detail="handover_not_rollbackable",
        )
    task_id = _queue(
        rollback_ad_only_handover_task,
        kwargs={"handover_id": handover.id},
    )
    return {
        "code": 0,
        "message": "Handover rollback queued",
        "data": {"task_id": task_id, "handover_id": handover.id},
    }
