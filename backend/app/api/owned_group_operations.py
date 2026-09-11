"""Read-only stage-five owned-group operations-centre endpoints."""

from __future__ import annotations

import re
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.owned_group_audit import require_owned_group_audit_reader
from app.core.database import get_db
from app.modules.owned_group.operations_read_model import (
    OwnedGroupOperationsError,
    OwnedGroupOperationsReadModel,
)
from app.modules.owned_group.operations_schemas import (
    OPERATIONS_ERROR_RESPONSES,
    ClassificationStatus,
    MemberKind,
    MemberPageEnvelope,
    MemberQuery,
    MemberSort,
    OperationsCenterEnvelope,
    PresenceStatus,
    TelegramRole,
)
from app.modules.owned_group.security import safe_exception_message

logger = structlog.get_logger().bind(module="owned_group_operations_api")
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _correlation_id(request: Request) -> str:
    existing = getattr(request.state, "owned_group_operations_correlation_id", None)
    if isinstance(existing, str) and existing:
        return existing
    supplied = request.headers.get("X-Correlation-ID", "")
    value = supplied if _SAFE_CORRELATION_ID.fullmatch(supplied) else f"group-ops-{uuid.uuid4()}"
    request.state.owned_group_operations_correlation_id = value
    return value


def _error_response(
    *,
    status_code: int,
    correlation_id: str,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
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


def _validation_details(exc: RequestValidationError | ValidationError) -> dict[str, object]:
    return {
        "validation_errors": [
            {
                "loc": [str(item) for item in error.get("loc", ())],
                "message": str(error.get("msg", "invalid value"))[:200],
                "type": str(error.get("type", "value_error"))[:100],
            }
            for error in exc.errors()
        ]
    }


class OwnedGroupOperationsAPIRoute(APIRoute):
    """Apply one stable error envelope to dependencies and endpoint code."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            correlation_id = _correlation_id(request)
            try:
                response = await original(request)
            except OwnedGroupOperationsError as exc:
                return _error_response(
                    status_code=exc.http_status,
                    correlation_id=correlation_id,
                    code=exc.code,
                    message=exc.message,
                    details=exc.details,
                    retryable=exc.retryable,
                )
            except (RequestValidationError, ValidationError) as exc:
                return _error_response(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    correlation_id=correlation_id,
                    code="MEMBER_QUERY_INVALID",
                    message="Member query parameters are invalid",
                    details=_validation_details(exc),
                )
            except HTTPException as exc:
                unauthenticated = exc.status_code == status.HTTP_401_UNAUTHORIZED or (
                    exc.status_code == status.HTTP_403_FORBIDDEN
                    and str(exc.detail).lower()
                    in {"not authenticated", "invalid authentication credentials"}
                )
                if unauthenticated:
                    return _error_response(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        correlation_id=correlation_id,
                        code="AUTHENTICATION_REQUIRED",
                        message="Authentication is required",
                    )
                if exc.status_code == status.HTTP_403_FORBIDDEN:
                    return _error_response(
                        status_code=status.HTTP_403_FORBIDDEN,
                        correlation_id=correlation_id,
                        code="OWNED_GROUP_ROLE_FORBIDDEN",
                        message="Owned group operations read access is forbidden",
                    )
                code = "MEMBER_QUERY_INVALID" if exc.status_code == 422 else "HTTP_ERROR"
                return _error_response(
                    status_code=exc.status_code,
                    correlation_id=correlation_id,
                    code=code,
                    message="Member query parameters are invalid"
                    if exc.status_code == 422
                    else "Request failed",
                )
            except ResponseValidationError as exc:
                return _unexpected_failure(request, exc, correlation_id)
            except Exception as exc:
                return _unexpected_failure(request, exc, correlation_id)
            response.headers["X-Correlation-ID"] = correlation_id
            return response

        return handler


def _unexpected_failure(request: Request, exc: Exception, correlation_id: str) -> JSONResponse:
    members_endpoint = request.url.path.endswith("/members")
    logger.error(
        "owned_group_members_query_failed"
        if members_endpoint
        else "owned_group_operations_summary_failed",
        path=request.url.path,
        correlation_id=correlation_id,
        error_type=type(exc).__name__,
        error=safe_exception_message(exc, max_length=300),
    )
    return _error_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        correlation_id=correlation_id,
        code="MEMBER_SOURCE_READ_FAILED" if members_endpoint else "OPERATIONS_CENTER_READ_FAILED",
        message="Owned group member sources are temporarily unavailable"
        if members_endpoint
        else "Owned group operations summary is temporarily unavailable",
        retryable=True,
    )


router = APIRouter(
    route_class=OwnedGroupOperationsAPIRoute,
    responses=OPERATIONS_ERROR_RESPONSES,
)


@router.get("/{asset_id}/operations-center", response_model=OperationsCenterEnvelope)
async def get_owned_group_operations_center(
    asset_id: int,
    request: Request,
    current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> OperationsCenterEnvelope:
    correlation_id = _correlation_id(request)
    data = await OwnedGroupOperationsReadModel(db).get_operations_center(
        asset_id,
        role=str(current_user.get("role") or ""),
        correlation_id=correlation_id,
    )
    return OperationsCenterEnvelope(data=data, correlation_id=correlation_id)


@router.get("/{asset_id}/members", response_model=MemberPageEnvelope)
async def list_owned_group_members(
    asset_id: int,
    request: Request,
    member_kind: MemberKind | None = Query(default=None),
    presence_status: PresenceStatus | None = Query(default=None),
    classification_status: ClassificationStatus | None = Query(default=None),
    telegram_role: TelegramRole | None = Query(default=None),
    q: str | None = Query(default=None),
    sort: MemberSort = Query(default="last_observed_desc"),
    offset: int = Query(default=0, ge=0, le=10_000),
    limit: int = Query(default=50, ge=1, le=100),
    _current_user: dict = Depends(require_owned_group_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> MemberPageEnvelope:
    correlation_id = _correlation_id(request)
    query = MemberQuery(
        member_kind=member_kind,
        presence_status=presence_status,
        classification_status=classification_status,
        telegram_role=telegram_role,
        q=q,
        sort=sort,
        offset=offset,
        limit=limit,
    )
    data, total, coverage, summary = await OwnedGroupOperationsReadModel(db).list_members(
        asset_id,
        query,
        correlation_id=correlation_id,
    )
    return MemberPageEnvelope(
        data=data,
        total=total,
        offset=query.offset,
        limit=query.limit,
        coverage=coverage,
        summary=summary,
        correlation_id=correlation_id,
    )


__all__ = ["OwnedGroupOperationsAPIRoute", "router"]
