"""Administrator API for official Telegram Managed Bot provisioning."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import ManagedBotProvision
from app.core.database import get_db
from app.core.security import require_admin
from app.modules.managed_bot_provision.service import (
    create_managed_bot_provision,
    list_manager_capabilities,
    serialize_provision,
)

router = APIRouter(prefix="/managed-provisions")


class ManagedBotProvisionCreate(BaseModel):
    owner_account_id: int = Field(..., gt=0)
    manager_bot_profile_id: int = Field(..., gt=0)
    display_name: str = Field(..., min_length=1, max_length=64)
    username: str = Field(..., min_length=5, max_length=33)


_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    "IDEMPOTENCY_KEY_INVALID": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Idempotency-Key 长度必须为 8 到 128 个字符",
    ),
    "IDEMPOTENCY_KEY_CONFLICT": (
        status.HTTP_409_CONFLICT,
        "Idempotency-Key 已用于其他创建请求",
    ),
    "USERNAME_OPERATION_CONFLICT": (
        status.HTTP_409_CONFLICT,
        "该 Bot 用户名已有其他创建任务",
    ),
    "USERNAME_INVALID": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Bot 用户名格式无效，必须为 5 到 32 位并以 bot 结尾",
    ),
    "USERNAME_SUFFIX_MISSING": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Bot 用户名必须以 bot 结尾",
    ),
    "NAME_INVALID": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Bot 展示名称长度必须为 1 到 64 个字符",
    ),
    "ACCOUNT_NOT_ELIGIBLE": (
        status.HTTP_409_CONFLICT,
        "所选用户账号当前不可用于创建 Bot",
    ),
    "MANAGER_INVALID": (
        status.HTTP_409_CONFLICT,
        "所选 Manager Bot 当前不可用",
    ),
    "MANAGER_PERMISSION_MISSING": (
        status.HTTP_409_CONFLICT,
        "Manager Bot 尚未开启 Bot Management Mode",
    ),
}


def _value_error_response(exc: ValueError) -> HTTPException:
    candidate = str(exc)
    if candidate in _ERROR_RESPONSES:
        error_code = candidate
        status_code, message = _ERROR_RESPONSES[candidate]
    else:
        error_code = "PROVISION_REQUEST_INVALID"
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        message = "Managed Bot 创建请求无效"
    return HTTPException(
        status_code=status_code,
        detail={"code": error_code, "message": message},
    )


def _dispatch_tick_best_effort() -> None:
    """Wake a worker without making broker availability part of the HTTP result."""

    try:
        from app.modules.managed_bot_provision.tasks import (
            managed_bot_provision_tick,
        )

        managed_bot_provision_tick.apply_async(
            kwargs={"limit": 1, "stale_after_seconds": 300},
            queue="owned_group",
        )
    except Exception:
        # The 15-second beat task is the durable fallback. Never include broker
        # exception text here because transports can echo connection credentials.
        pass


@router.get("/capability")
async def get_managed_bot_provision_capability(
    _current_user: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    capability = await list_manager_capabilities(db)
    return {"code": 0, "message": "success", "data": capability}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_managed_bot_provision_operation(
    request: ManagedBotProvisionCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        operation, created = await create_managed_bot_provision(
            db,
            owner_account_id=request.owner_account_id,
            manager_bot_profile_id=request.manager_bot_profile_id,
            display_name=request.display_name,
            username=request.username,
            idempotency_key=idempotency_key,
            created_by_id=int(current_user["id"]),
        )
    except ValueError as exc:
        raise _value_error_response(exc) from None

    _dispatch_tick_best_effort()
    return {
        "code": 0,
        "message": "Managed Bot 创建任务已排队" if created else "Managed Bot 创建任务已存在",
        "data": serialize_provision(operation),
    }


@router.get("/{operation_id:int}")
async def get_managed_bot_provision_operation(
    operation_id: int,
    _current_user: dict[str, Any] = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    operation = await db.get(ManagedBotProvision, int(operation_id))
    if operation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PROVISION_NOT_FOUND", "message": "Managed Bot 创建任务不存在"},
        )
    return {
        "code": 0,
        "message": "success",
        "data": serialize_provision(operation),
    }


__all__ = ["router"]
