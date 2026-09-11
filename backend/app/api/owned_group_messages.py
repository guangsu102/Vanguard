"""Stage-two owned-group AI/template message API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_policy_service import (
    OwnedGroupMessagingPolicyService,
)
from app.modules.owned_group.messaging_schemas import (
    MESSAGING_ERROR_RESPONSES,
    EligibleAccountsEnvelope,
    ExecutionApprove,
    ExecutionEnvelope,
    ExecutionListEnvelope,
    ExecutionReject,
    ManualExecutionCreate,
    ManualExecutionEnvelope,
    MessagePreviewEnvelope,
    MessagePreviewRequest,
    OwnedGroupTemplateEnvelope,
    OwnedGroupTemplateListEnvelope,
    OwnedGroupTemplateWrite,
    PolicyCreate,
    PolicyEnvelope,
    PolicyListEnvelope,
    PolicyUpdate,
)
from app.modules.owned_group.security import safe_exception_message

_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _correlation_id(request: Request) -> str:
    existing = getattr(request.state, "owned_group_message_correlation_id", None)
    if existing:
        return existing
    supplied = request.headers.get("X-Correlation-ID", "")
    value = supplied if _SAFE_CORRELATION_ID.fullmatch(supplied) else f"msg-{uuid.uuid4()}"
    request.state.owned_group_message_correlation_id = value
    return value


def _error_response(
    *,
    status_code: int,
    correlation_id: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "retryable": retryable,
            },
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id},
    )


class OwnedGroupMessagingRoute(APIRoute):
    """Keep dependency, validation and domain failures on one stable envelope."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            correlation_id = _correlation_id(request)
            try:
                response = await original(request)
            except OwnedGroupMessagingError as exc:
                return _error_response(
                    status_code=exc.http_status,
                    correlation_id=exc.correlation_id or correlation_id,
                    code=exc.code,
                    message=exc.message,
                    details=exc.details,
                    retryable=exc.retryable,
                )
            except RequestValidationError as exc:
                details = {
                    "validation_errors": [
                        {
                            "loc": [str(item) for item in error.get("loc", ())],
                            "message": str(error.get("msg", "invalid value")),
                            "type": str(error.get("type", "value_error")),
                        }
                        for error in exc.errors()
                    ]
                }
                return _error_response(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    correlation_id=correlation_id,
                    code="REQUEST_VALIDATION_FAILED",
                    message="请求参数校验失败",
                    details=details,
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_403_FORBIDDEN:
                    code, message = "ADMIN_REQUIRED", "该写操作需要管理员权限"
                elif exc.status_code == status.HTTP_401_UNAUTHORIZED:
                    code, message = "AUTHENTICATION_REQUIRED", "需要登录后访问"
                else:
                    code, message = "HTTP_ERROR", str(exc.detail)
                return _error_response(
                    status_code=exc.status_code,
                    correlation_id=correlation_id,
                    code=code,
                    message=message,
                    details={},
                )
            except Exception as exc:
                import structlog

                structlog.get_logger().error(
                    "owned_group_messaging_api_error",
                    path=request.url.path,
                    correlation_id=correlation_id,
                    error_type=type(exc).__name__,
                    error=safe_exception_message(exc, max_length=500),
                )
                return _error_response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    correlation_id=correlation_id,
                    code="INTERNAL_ERROR",
                    message="服务暂时不可用",
                    details={},
                    retryable=True,
                )
            response.headers["X-Correlation-ID"] = correlation_id
            return response

        return handler


router = APIRouter(
    route_class=OwnedGroupMessagingRoute,
    responses=MESSAGING_ERROR_RESPONSES,
)


def _success(request: Request, data: Any, **extra: Any) -> dict[str, Any]:
    return {
        "data": jsonable_encoder(data),
        **jsonable_encoder(extra),
        "correlation_id": _correlation_id(request),
    }


def _service_result(value: Any) -> Any:
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return value.as_dict()
    return value


@router.get(
    "/{asset_id}/messages/eligible-accounts",
    response_model=EligibleAccountsEnvelope,
)
async def list_eligible_accounts(
    asset_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await OwnedGroupMessagingPolicyService(db).eligible_accounts(asset_id)
    return _success(request, result["accounts"], asset=result["asset"])


@router.get(
    "/{asset_id}/messages/policies",
    response_model=PolicyListEnvelope,
)
async def list_message_policies(
    asset_id: int,
    request: Request,
    enabled: bool | None = Query(default=None),
    mode: Literal["ai", "template", "off"] | None = Query(default=None),
    account_id: int | None = Query(default=None, gt=0),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).list_policies(
        asset_id,
        enabled=enabled,
        mode=mode,
        account_id=account_id,
    )
    return _success(request, data)


@router.get(
    "/{asset_id}/messages/policies/{policy_id}",
    response_model=PolicyEnvelope,
)
async def get_message_policy(
    asset_id: int,
    policy_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).get_policy_response(asset_id, policy_id)
    return _success(request, data)


@router.post(
    "/{asset_id}/messages/policies",
    status_code=status.HTTP_201_CREATED,
    response_model=PolicyEnvelope,
)
async def create_message_policy(
    asset_id: int,
    body: PolicyCreate,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).create_policy(
        asset_id,
        body,
        actor=admin,
        correlation_id=_correlation_id(request),
    )
    return _success(request, data)


@router.put(
    "/{asset_id}/messages/policies/{policy_id}",
    response_model=PolicyEnvelope,
)
async def update_message_policy(
    asset_id: int,
    policy_id: int,
    body: PolicyUpdate,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).update_policy(
        asset_id,
        policy_id,
        body,
        actor=admin,
        correlation_id=_correlation_id(request),
    )
    return _success(request, data)


@router.post(
    "/{asset_id}/messages/policies/{policy_id}/preview",
    response_model=MessagePreviewEnvelope,
)
async def preview_message_policy(
    asset_id: int,
    policy_id: int,
    body: MessagePreviewRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    policy = await OwnedGroupMessagingPolicyService(db).get_policy(asset_id, policy_id)
    try:
        from app.modules.owned_group.messaging_content_service import (
            preview_owned_group_message,
        )
    except ImportError as exc:
        raise OwnedGroupMessagingError(
            "AI_PROVIDER_UNAVAILABLE",
            "消息内容服务尚不可用",
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            retryable=True,
        ) from exc
    data = await preview_owned_group_message(
        db=db,
        asset_id=asset_id,
        policy=policy,
        request=body,
        actor_id=current_user.get("id"),
        correlation_id=_correlation_id(request),
    )
    return _success(request, _service_result(data))


@router.post(
    "/{asset_id}/messages/policies/{policy_id}/executions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ManualExecutionEnvelope,
)
async def create_manual_message_execution(
    asset_id: int,
    policy_id: int,
    body: ManualExecutionCreate,
    request: Request,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    policy = await OwnedGroupMessagingPolicyService(db).get_policy(asset_id, policy_id)
    try:
        from app.modules.owned_group.messaging_execution_service import (
            create_manual_execution,
        )
    except ImportError as exc:
        raise OwnedGroupMessagingError(
            "EXECUTION_SERVICE_UNAVAILABLE",
            "消息执行服务尚不可用",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=True,
        ) from exc
    data = await create_manual_execution(
        db=db,
        asset_id=asset_id,
        policy=policy,
        request=body,
        idempotency_key=idempotency_key,
        actor_id=admin.get("id"),
        correlation_id=_correlation_id(request),
    )
    return _success(request, _service_result(data))


@router.get(
    "/{asset_id}/messages/executions",
    response_model=ExecutionListEnvelope,
)
async def list_message_executions(
    asset_id: int,
    request: Request,
    policy_id: int | None = Query(default=None, gt=0),
    account_id: int | None = Query(default=None, gt=0),
    trigger_type: Literal["scheduled", "keyword", "reply", "manual"] | None = Query(default=None),
    content_category: Literal["community", "promotion"] | None = Query(default=None),
    execution_status: Literal[
        "queued",
        "generating",
        "pending_review",
        "ready_to_send",
        "sending",
        "sent",
        "skipped",
        "failed",
        "rejected",
        "expired",
        "cancelled",
    ]
    | None = Query(default=None, alias="status"),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).list_executions(
        asset_id,
        policy_id=policy_id,
        account_id=account_id,
        trigger_type=trigger_type,
        content_category=content_category,
        status=execution_status,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
        include_sensitive=current_user.get("role") == "admin",
    )
    return _success(request, data)


@router.get(
    "/{asset_id}/messages/executions/{execution_id}",
    response_model=ExecutionEnvelope,
)
async def get_message_execution(
    asset_id: int,
    execution_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).get_execution_response(
        asset_id,
        execution_id,
        include_sensitive=current_user.get("role") == "admin",
    )
    return _success(request, data)


@router.post(
    "/{asset_id}/messages/executions/{execution_id}/approve",
    response_model=ExecutionEnvelope,
)
async def approve_message_execution(
    asset_id: int,
    execution_id: int,
    body: ExecutionApprove,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).approve_execution(
        asset_id,
        execution_id,
        body,
        actor=admin,
        correlation_id=_correlation_id(request),
    )
    return _success(request, data)


@router.post(
    "/{asset_id}/messages/executions/{execution_id}/reject",
    response_model=ExecutionEnvelope,
)
async def reject_message_execution(
    asset_id: int,
    execution_id: int,
    body: ExecutionReject,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).reject_execution(
        asset_id,
        execution_id,
        body,
        actor=admin,
        correlation_id=_correlation_id(request),
    )
    return _success(request, data)


@router.get(
    "/{asset_id}/messages/templates",
    response_model=OwnedGroupTemplateListEnvelope,
)
async def list_owned_group_message_templates(
    asset_id: int,
    request: Request,
    message_type: Literal["interaction", "qa", "share", "guide"] | None = Query(default=None),
    content_category: Literal["community", "promotion"] | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).list_templates(
        asset_id,
        message_type=message_type,
        content_category=content_category,
        enabled=enabled,
    )
    return _success(request, data)


@router.post(
    "/{asset_id}/messages/templates",
    status_code=status.HTTP_201_CREATED,
    response_model=OwnedGroupTemplateEnvelope,
)
async def create_owned_group_message_template(
    asset_id: int,
    body: OwnedGroupTemplateWrite,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).create_template(asset_id, body)
    return _success(request, data)


@router.put(
    "/{asset_id}/messages/templates/{template_id}",
    response_model=OwnedGroupTemplateEnvelope,
)
async def update_owned_group_message_template(
    asset_id: int,
    template_id: int,
    body: OwnedGroupTemplateWrite,
    request: Request,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    data = await OwnedGroupMessagingPolicyService(db).update_template(
        asset_id,
        template_id,
        body,
    )
    return _success(request, data)
