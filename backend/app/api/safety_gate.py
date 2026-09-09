"""P0 safety gate API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.core.p0_safety_gate import (
    get_safety_gate_state,
    precheck_account_eligibility,
    require_global_stop_reason,
    set_global_stop,
)
from app.core.security import require_admin

router = APIRouter()


class SafetyGatePrecheckRequest(BaseModel):
    account_id: int = Field(..., gt=0)
    bot_account_id: int | None = Field(default=None, gt=0)
    admin_account_id: int | None = Field(default=None, gt=0)


class SafetyGateStopRequest(BaseModel):
    enabled: bool = True
    reason: str = Field(default="", max_length=240)


@router.get("/state")
async def read_safety_gate_state(current_user: dict = Depends(require_admin)) -> dict:
    del current_user
    state = await get_safety_gate_state()
    return {"code": 0, "message": "success", "data": state.__dict__}


@router.post("/precheck")
async def precheck_safety_gate(
    request: SafetyGatePrecheckRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    decision = await precheck_account_eligibility(
        db,
        request.account_id,
        bot_account_id=request.bot_account_id,
        admin_account_id=request.admin_account_id,
    )
    if not decision.allowed:
        raise HTTPException(status_code=400, detail={"reason": decision.reason, "details": decision.details})
    return {"code": 0, "message": "success", "data": {"allowed": True, "reason": decision.reason, "details": decision.details}}


@router.post("/stop")
async def update_global_stop(
    request: SafetyGateStopRequest,
    current_user: dict = Depends(require_admin),
) -> dict:
    if request.enabled:
        require_global_stop_reason(request.reason)
    reason = request.reason.strip() or ("manual_resume" if not request.enabled else "manual_stop")
    state = await set_global_stop(
        enabled=request.enabled,
        reason=reason,
        operator=current_user.get("username"),
    )
    if not state.backend_available and settings.P0_SAFETY_GATE_FAIL_CLOSED:
        raise HTTPException(
            status_code=503,
            detail={"reason": "safety_gate_backend_unavailable"},
        )
    return {"code": 0, "message": "success", "data": state.__dict__}
