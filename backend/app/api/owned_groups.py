"""API contract and persistence endpoints for self-owned group orchestration.

Telegram side effects are intentionally not started by this first slice.  The
draft and operation records are created with immutable snapshots so the worker
can be added without changing the public contract.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountType, TelegramAccount
from app.core.database import get_db
from app.core.p0_safety_gate import (
    owned_group_account_failure,
    precheck_owned_group_resources,
    require_owned_group_module_enabled,
)
from app.core.security import get_current_user, require_admin
from app.modules.owned_group.contracts import (
    DEFAULT_OPERATION_CONFIG,
    AssetStatus,
    ItemStatus,
    OperationStatus,
    ReasonCode,
    ResourceType,
    assert_transition,
    build_config_snapshot,
    build_selection_snapshot,
)
from app.modules.owned_group.messaging_contracts import TERMINAL_EXECUTION_STATUSES
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import (
    OwnedGroupAdminAssignment,
    OwnedGroupAuditEvent,
)
from app.modules.owned_group.security import redact_sensitive_text, redact_sensitive_value

router = APIRouter()

_PUBLIC_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_VISIBILITIES = {"public", "private"}
_INVITE_MODES = {"direct_invite", "link_self_join", "manual_approval"}
_PUBLIC_LINK = re.compile(
    r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z][A-Za-z0-9_]{4,31}/?$",
    re.IGNORECASE,
)
_ADMIN_PERMISSION_KEYS = {
    "change_info",
    "post_messages",
    "edit_messages",
    "delete_messages",
    "ban_users",
    "invite_users",
    "pin_messages",
    "add_admins",
    "anonymous",
    "manage_call",
    "other",
    "manage_topics",
    "post_stories",
    "edit_stories",
    "delete_stories",
}


async def require_owned_group_operator(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Allow only administrators and operators to mutate owned-group plans."""

    if current_user.get("role") not in {"admin", "operator"}:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "owned_group_role_forbidden",
                "message": "Owned group operator access required",
                "retryable": False,
            },
        )
    # Keep every operator-scoped write path (including Bot profile and control
    # routers that reuse this dependency) behind the same module switch.  A
    # disabled module must not accept a write merely because the caller has a
    # valid role.
    require_owned_group_module_enabled()
    return current_user


def _normalize_required_text(value: str, field_name: str) -> str:
    """Return a trimmed required value and reject whitespace-only input."""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


class OwnedGroupDraftCreate(BaseModel):
    internal_name: str = Field(..., min_length=1, max_length=120)
    title: str = Field(..., min_length=1, max_length=255)
    about: str | None = Field(default=None, max_length=4096)
    visibility: str = Field(default="public")
    telegram_username: str | None = Field(default=None, max_length=64)
    owner_account_id: int = Field(..., gt=0)
    invite_mode: str = Field(default="direct_invite")

    @field_validator("internal_name", "title")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_required_text(value, info.field_name or "value")

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in _VISIBILITIES:
            raise ValueError("visibility must be public or private")
        return value

    @field_validator("invite_mode")
    @classmethod
    def validate_invite_mode(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in _INVITE_MODES:
            raise ValueError("unsupported invite_mode")
        return value

    def model_post_init(self, __context: object) -> None:
        if self.visibility == "public":
            if not self.telegram_username or not _PUBLIC_USERNAME.fullmatch(self.telegram_username):
                raise ValueError("a valid telegram_username is required for a public group")
        else:
            self.telegram_username = None


class OwnedGroupResourceSelection(BaseModel):
    resource_type: ResourceType
    resource_id: int = Field(..., gt=0)
    admin_required: bool = False
    admin_permissions: dict[str, bool] = Field(default_factory=dict)
    # Telegram custom administrator titles are limited to 16 characters.  Fail
    # at request validation instead of discovering it after an invitation RPC.
    admin_title: str | None = Field(default=None, max_length=16)

    @field_validator("admin_title")
    @classmethod
    def normalize_admin_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_admin_contract(self) -> OwnedGroupResourceSelection:
        if not self.admin_required:
            # Do not freeze unused privilege fields into the immutable snapshot.
            self.admin_permissions = {}
            self.admin_title = None
            return self
        unknown = set(self.admin_permissions) - _ADMIN_PERMISSION_KEYS
        if unknown:
            raise ValueError("admin_permissions contains unsupported permissions")
        if not any(self.admin_permissions.values()):
            raise ValueError("at least one admin permission is required")
        return self


class OwnedGroupOperationCreate(BaseModel):
    resources: list[OwnedGroupResourceSelection] = Field(..., min_length=1, max_length=2000)
    batch_size: int = Field(default=DEFAULT_OPERATION_CONFIG["batch_size"], ge=1, le=200)
    batch_interval_seconds: int = Field(
        default=DEFAULT_OPERATION_CONFIG["batch_interval_seconds"], ge=1, le=86400
    )
    max_parallelism: int = Field(default=1, ge=1, le=1)
    max_attempts: int = Field(default=2, ge=1, le=5)
    schedule_at: datetime | None = None

class OwnedGroupDissolutionCreate(BaseModel):
    """Double-confirmation payload for an irreversible remote Telegram action."""

    confirmation: str = Field(..., min_length=10, max_length=64)

    @field_validator("confirmation")
    @classmethod
    def normalize_confirmation(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("confirmation must not be blank")
        return normalized



class OwnedGroupAssetResponse(BaseModel):
    id: int
    internal_name: str
    telegram_chat_id: int | None = None
    title: str
    about: str | None
    visibility: str
    telegram_username: str | None
    public_link: str | None
    owner_account_id: int
    invite_mode: str
    status: str
    member_count: int
    core_group_id: int | None = None
    managed_binding_id: int | None = None
    guardian_bot_account_id: int | None = None
    governance_status: str = "disabled"
    governance_pending_at: datetime | None = None
    governance_enabled_at: datetime | None = None
    governance_last_checked_at: datetime | None = None
    governance_last_error_code: str | None = None
    governance_last_error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    # True only when the asset is needs_attention because its latest dissolve
    # operation ended without a verified remote outcome (unknown/failed).
    pending_dissolution_review: bool = False


class OwnedGroupAssetListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[OwnedGroupAssetResponse]
    total: int


class OwnedGroupOperationResponse(BaseModel):
    id: int
    group_asset_id: int
    operation_type: str
    status: str
    planned_count: int
    selection_snapshot_hash: str
    config_snapshot_hash: str
    idempotency_key: str
    created_at: datetime


class OwnedGroupOperationSummaryResponse(OwnedGroupOperationResponse):
    """Progress fields safe for authenticated read-only observers."""

    completed_count: int
    skipped_count: int
    failed_count: int
    schedule_at: datetime | None = None
    updated_at: datetime


class OwnedGroupOperationListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[OwnedGroupOperationSummaryResponse]
    total: int


class OwnedGroupOperationDetailResponse(OwnedGroupOperationSummaryResponse):
    """An operation detail without credentials, leases, or invite URLs."""

    selection_snapshot: list[dict[str, Any]]
    config_snapshot: dict[str, Any]
    items_total: int
    stop_reason: str | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class OwnedGroupOperationItemResponse(BaseModel):
    id: int
    operation_id: int
    resource_type: str
    resource_id: int
    status: str
    attempts: int
    reason_code: str | None = None
    error_message: str | None = None
    next_retry_at: datetime | None = None
    invited_at: datetime | None = None
    joined_at: datetime | None = None
    verified_at: datetime | None = None
    admin_required: bool
    admin_permissions: dict[str, bool] | None = None
    admin_title: str | None = None
    created_at: datetime
    updated_at: datetime


class OwnedGroupOperationItemListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[OwnedGroupOperationItemResponse]
    total: int


class OwnedGroupPrecheckResponse(BaseModel):
    """Dry-run eligibility result; this endpoint never writes or calls Telegram."""

    asset_id: int
    allowed: bool
    reason: str
    details: dict[str, Any] | None = None
    selection_snapshot: list[dict[str, Any]]
    selection_snapshot_hash: str
    config_snapshot: dict[str, Any]
    config_snapshot_hash: str
    owner_auto_included: bool


def _asset_response(
    asset: OwnedGroupAsset,
    *,
    pending_dissolution_review: bool = False,
) -> OwnedGroupAssetResponse:
    public_link = None
    if asset.status == AssetStatus.READY.value and asset.visibility == "public":
        candidate = str(asset.public_link or "").strip()
        if _PUBLIC_LINK.fullmatch(candidate):
            public_link = candidate
    return OwnedGroupAssetResponse(
        id=asset.id,
        internal_name=asset.internal_name,
        telegram_chat_id=asset.telegram_chat_id,
        title=asset.title,
        about=asset.about,
        visibility=asset.visibility,
        telegram_username=asset.telegram_username,
        # Only a verified public asset may expose a public username URL.  Never
        # echo a legacy/private/draft value from this general asset endpoint;
        # private bearer links have their own explicit no-store endpoint.
        public_link=public_link,
        owner_account_id=asset.owner_account_id,
        invite_mode=asset.invite_mode,
        status=asset.status,
        member_count=asset.member_count,
        core_group_id=asset.core_group_id,
        managed_binding_id=asset.managed_binding_id,
        guardian_bot_account_id=asset.guardian_bot_account_id,
        governance_status=asset.governance_status,
        governance_pending_at=asset.governance_pending_at,
        governance_enabled_at=asset.governance_enabled_at,
        governance_last_checked_at=asset.governance_last_checked_at,
        governance_last_error_code=asset.governance_last_error_code,
        governance_last_error_message=(
            redact_sensitive_text(asset.governance_last_error_message, max_length=500)
            if asset.governance_last_error_message
            else None
        ),
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        pending_dissolution_review=pending_dissolution_review,
    )


def _operation_response(operation: OwnedGroupOperation) -> OwnedGroupOperationResponse:
    return OwnedGroupOperationResponse(
        id=operation.id,
        group_asset_id=operation.group_asset_id,
        operation_type=operation.operation_type,
        status=operation.status,
        planned_count=operation.planned_count,
        selection_snapshot_hash=operation.selection_snapshot_hash,
        config_snapshot_hash=operation.config_snapshot_hash,
        idempotency_key=operation.idempotency_key,
        created_at=operation.created_at,
    )


def _operation_summary_response(
    operation: OwnedGroupOperation,
) -> OwnedGroupOperationSummaryResponse:
    return OwnedGroupOperationSummaryResponse(
        **_operation_response(operation).model_dump(),
        completed_count=operation.completed_count,
        skipped_count=operation.skipped_count,
        failed_count=operation.failed_count,
        schedule_at=operation.schedule_at,
        updated_at=operation.updated_at,
    )


def _decode_json_object(raw: str | None) -> dict[str, Any]:
    """Decode a persisted object while keeping a malformed row observable but safe."""

    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _decode_json_list(raw: str | None) -> list[dict[str, Any]]:
    """Decode a persisted selection without exposing arbitrary scalar values."""

    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


async def _operation_detail_response(
    db: AsyncSession,
    operation: OwnedGroupOperation,
) -> OwnedGroupOperationDetailResponse:
    items_total = int(
        (
            await db.scalar(
                select(func.count(OwnedGroupOperationItem.id)).where(
                    OwnedGroupOperationItem.operation_id == operation.id
                )
            )
        )
        or 0
    )
    summary = _operation_summary_response(operation)
    return OwnedGroupOperationDetailResponse(
        **summary.model_dump(),
        selection_snapshot=redact_sensitive_value(_decode_json_list(operation.selection_snapshot)),
        config_snapshot=redact_sensitive_value(_decode_json_object(operation.config_snapshot)),
        items_total=items_total,
        stop_reason=(
            redact_sensitive_text(operation.stop_reason) if operation.stop_reason else None
        ),
        last_error=(redact_sensitive_text(operation.last_error) if operation.last_error else None),
        started_at=operation.started_at,
        finished_at=operation.finished_at,
    )


def _operation_item_response(item: OwnedGroupOperationItem) -> OwnedGroupOperationItemResponse:
    permissions = _decode_json_object(item.admin_permissions)
    # Permission snapshots are boolean maps by contract. Ignore malformed
    # values rather than coercing arbitrary persisted data into a privilege.
    normalized_permissions = {
        str(key): value for key, value in permissions.items() if isinstance(value, bool)
    }
    return OwnedGroupOperationItemResponse(
        id=item.id,
        operation_id=item.operation_id,
        resource_type=item.resource_type,
        resource_id=item.resource_id,
        status=item.status,
        attempts=item.attempts,
        reason_code=(redact_sensitive_text(item.reason_code) if item.reason_code else None),
        error_message=(redact_sensitive_text(item.error_message) if item.error_message else None),
        next_retry_at=item.next_retry_at,
        invited_at=item.invited_at,
        joined_at=item.joined_at,
        verified_at=item.verified_at,
        admin_required=item.admin_required,
        admin_permissions=normalized_permissions or None,
        admin_title=item.admin_title,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _validate_filter(value: str | None, allowed: set[str], field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return normalized


def _operation_status_values() -> set[str]:
    return {item.value for item in OperationStatus}


def _item_status_values() -> set[str]:
    return {item.value for item in ItemStatus}


def _resource_type_values() -> set[str]:
    return {item.value for item in ResourceType}


def _resource_key(resource: OwnedGroupResourceSelection) -> tuple[str, int]:
    return resource.resource_type.value, resource.resource_id


def _admin_config_snapshot(
    resources: list[OwnedGroupResourceSelection],
) -> list[dict[str, Any]]:
    """Copy every explicitly submitted admin configuration into the snapshot."""

    configs = [
        {
            "resource_type": item.resource_type.value,
            "resource_id": item.resource_id,
            "admin_required": bool(item.admin_required),
            "admin_permissions": {
                key: bool(value)
                for key, value in sorted(item.admin_permissions.items(), key=lambda pair: pair[0])
            },
            "admin_title": item.admin_title,
        }
        for item in resources
    ]
    return sorted(configs, key=lambda item: (str(item["resource_type"]), int(item["resource_id"])))


def _snapshot_json(value: object) -> str:
    """Serialize a persisted snapshot deterministically and without mutable aliases."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _build_selection_with_owner(
    resources: list[OwnedGroupResourceSelection],
    owner_account_id: int,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Build the fixed plan and inject the owner exactly once."""

    try:
        selection, selection_hash = build_selection_snapshot(
            {"resource_type": item.resource_type.value, "resource_id": item.resource_id}
            for item in resources
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    owner_key = (ResourceType.USER.value, owner_account_id)
    owner_auto_included = not any(
        (item["resource_type"], item["resource_id"]) == owner_key for item in selection
    )
    if owner_auto_included:
        if len(selection) >= 2000:
            raise HTTPException(
                status_code=422,
                detail="At most 2000 resources, including the group owner, may be planned",
            )
        selection.append({"resource_type": owner_key[0], "resource_id": owner_key[1]})
        selection, selection_hash = build_selection_snapshot(selection)
    return selection, selection_hash, owner_auto_included


def _build_operation_snapshots(
    request: OwnedGroupOperationCreate,
    owner_account_id: int,
) -> tuple[list[dict[str, Any]], str, bool, dict[str, Any], str]:
    """Build the exact immutable snapshots persisted for an operation.

    Keeping this calculation in one helper is important for idempotency: a
    replay must compare the same normalized selection/config hashes that the
    first request stored, including implicit owner insertion and admin
    settings.
    """

    selection, selection_hash, owner_auto_included = _build_selection_with_owner(
        request.resources, owner_account_id
    )
    config_payload = request.model_dump(exclude={"resources", "schedule_at"})
    config_payload["owner_auto_included"] = owner_auto_included
    config_payload["admin_configs"] = _admin_config_snapshot(request.resources)
    try:
        config, config_hash = build_config_snapshot(config_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return selection, selection_hash, owner_auto_included, config, config_hash


def _canonical_schedule(value: datetime | None) -> str | None:
    """Normalize schedule values for idempotency comparisons.

    Database ``DateTime`` columns are currently timezone-naive.  Convert an
    aware request to UTC-naive before comparing so equivalent offsets do not
    create a false payload mismatch.
    """

    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    else:
        value = value.replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _idempotency_payload_matches(
    operation: OwnedGroupOperation,
    *,
    selection_hash: str,
    config_hash: str,
    schedule_at: datetime | None,
) -> bool:
    return (
        operation.selection_snapshot_hash == selection_hash
        and operation.config_snapshot_hash == config_hash
        and _canonical_schedule(operation.schedule_at) == _canonical_schedule(schedule_at)
    )


def _idempotency_payload_mismatch(operation: OwnedGroupOperation) -> HTTPException:
    """Return a stable, non-sensitive conflict response for key reuse."""

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "reason": "idempotency_key_payload_mismatch",
            "message": "Idempotency-Key was already used with a different request",
            "operation_id": operation.id,
            "group_asset_id": operation.group_asset_id,
        },
    )


def _is_admin_assignment_conflict(exc: IntegrityError) -> bool:
    message = str(exc).lower()
    return "uq_owned_group_admin_resource" in message or (
        "owned_group_admin_assignments" in message and "unique" in message
    )


def _is_idempotency_conflict(exc: IntegrityError) -> bool:
    """Recognize a concurrent unique-key race without swallowing other errors."""

    message = str(exc).lower()
    return "idempotency_key" in message and (
        "unique" in message or "duplicate" in message or "constraint" in message
    )


@router.get("", response_model=OwnedGroupAssetListResponse)
async def list_owned_group_assets(
    status_filter: str | None = Query(default=None, alias="status"),
    visibility: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupAssetListResponse:
    query = select(OwnedGroupAsset).order_by(desc(OwnedGroupAsset.id)).offset(offset).limit(limit)
    count_query = select(func.count(OwnedGroupAsset.id))
    if status_filter:
        query = query.where(OwnedGroupAsset.status == status_filter)
        count_query = count_query.where(OwnedGroupAsset.status == status_filter)
    if visibility:
        if visibility not in _VISIBILITIES:
            raise HTTPException(status_code=400, detail="Invalid visibility")
        query = query.where(OwnedGroupAsset.visibility == visibility)
        count_query = count_query.where(OwnedGroupAsset.visibility == visibility)
    rows = await db.execute(query)
    assets = rows.scalars().all()
    total = int((await db.execute(count_query)).scalar() or 0)
    review_ids = await _pending_dissolution_review_asset_ids(
        db, [asset.id for asset in assets]
    )
    return OwnedGroupAssetListResponse(
        data=[
            _asset_response(
                asset,
                pending_dissolution_review=(
                    asset.status == AssetStatus.NEEDS_ATTENTION.value
                    and asset.id in review_ids
                ),
            )
            for asset in assets
        ],
        total=total,
    )


def _apply_operation_filters(
    query: Any,
    count_query: Any,
    *,
    group_asset_id: int | None,
    status_filter: str | None,
) -> tuple[Any, Any]:
    if group_asset_id is not None:
        query = query.where(OwnedGroupOperation.group_asset_id == group_asset_id)
        count_query = count_query.where(OwnedGroupOperation.group_asset_id == group_asset_id)
    if status_filter is not None:
        normalized_status = _validate_filter(
            status_filter, _operation_status_values(), "operation status"
        )
        query = query.where(OwnedGroupOperation.status == normalized_status)
        count_query = count_query.where(OwnedGroupOperation.status == normalized_status)
    return query, count_query


async def _list_owned_group_operations(
    db: AsyncSession,
    *,
    group_asset_id: int | None,
    status_filter: str | None,
    limit: int,
    offset: int,
) -> OwnedGroupOperationListResponse:
    query = select(OwnedGroupOperation)
    count_query = select(func.count(OwnedGroupOperation.id))
    query, count_query = _apply_operation_filters(
        query,
        count_query,
        group_asset_id=group_asset_id,
        status_filter=status_filter,
    )
    rows = await db.scalars(
        query.order_by(desc(OwnedGroupOperation.id)).offset(offset).limit(limit)
    )
    total = int((await db.scalar(count_query)) or 0)
    return OwnedGroupOperationListResponse(
        data=[_operation_summary_response(operation) for operation in rows.all()],
        total=total,
    )


@router.get("/operations", response_model=OwnedGroupOperationListResponse)
async def list_all_owned_group_operations(
    group_asset_id: int | None = Query(default=None, gt=0),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationListResponse:
    """List operation summaries for authenticated observers."""

    if group_asset_id is not None and await db.get(OwnedGroupAsset, group_asset_id) is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    return await _list_owned_group_operations(
        db,
        group_asset_id=group_asset_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get("/{asset_id:int}/operations", response_model=OwnedGroupOperationListResponse)
async def list_asset_owned_group_operations(
    asset_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationListResponse:
    """List operation summaries belonging to one asset."""

    if await db.get(OwnedGroupAsset, asset_id) is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    return await _list_owned_group_operations(
        db,
        group_asset_id=asset_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


@router.post("/drafts", response_model=OwnedGroupAssetResponse, status_code=status.HTTP_201_CREATED)
async def create_owned_group_draft(
    request: OwnedGroupDraftCreate,
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupAssetResponse:
    require_owned_group_module_enabled()
    owner = await db.get(TelegramAccount, request.owner_account_id)
    if not owner or owner.account_type != AccountType.PROMOTER:
        raise HTTPException(
            status_code=400, detail="owner_account_id must reference a user account"
        )
    if not owner.is_active:
        raise HTTPException(status_code=400, detail="owner account is inactive")
    owner_failure = owned_group_account_failure(owner)
    if owner_failure:
        reason, details = owner_failure
        raise HTTPException(
            status_code=400,
            detail={
                "reason": reason,
                "message": "群主账号当前不可用于自建群编排",
                "details": details,
            },
        )

    asset = OwnedGroupAsset(
        internal_name=request.internal_name.strip(),
        title=request.title.strip(),
        about=request.about.strip() if request.about else None,
        visibility=request.visibility,
        telegram_username=request.telegram_username,
        public_link=(
            f"https://t.me/{request.telegram_username}" if request.telegram_username else None
        ),
        owner_account_id=owner.id,
        invite_mode=request.invite_mode,
        status=AssetStatus.DRAFT.value,
        created_by=int(current_user["id"]),
    )
    db.add(asset)
    await db.flush()
    return _asset_response(asset)


@router.get("/{asset_id:int}", response_model=OwnedGroupAssetResponse)
async def get_owned_group_asset(
    asset_id: int, db: AsyncSession = Depends(get_db)
) -> OwnedGroupAssetResponse:
    asset = await db.get(OwnedGroupAsset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    review_ids = await _pending_dissolution_review_asset_ids(db, [asset.id])
    return _asset_response(
        asset,
        pending_dissolution_review=(
            asset.status == AssetStatus.NEEDS_ATTENTION.value
            and asset.id in review_ids
        ),
    )


_NONTERMINAL_DELETE_BLOCKING_OPERATION_STATUSES = {
    OperationStatus.DRAFT.value,
    OperationStatus.QUEUED.value,
    OperationStatus.RUNNING.value,
    OperationStatus.PAUSED.value,
    OperationStatus.STOPPING.value,
    OperationStatus.UNKNOWN.value,
}

_DISSOLVE_OPERATION_TYPE = "dissolve"

# A dissolution whose remote outcome was never verified (unknown) or was
# explicitly refused (failed) leaves the asset fenced in needs_attention until
# an administrator reviews the real Telegram state.
_DISSOLUTION_REVIEW_OPERATION_STATUSES = {
    OperationStatus.UNKNOWN.value,
    OperationStatus.FAILED.value,
}


async def _pending_dissolution_review_asset_ids(
    db: AsyncSession, asset_ids: list[int]
) -> set[int]:
    """Return asset ids whose latest dissolve operation awaits manual review.

    Only the latest dissolve operation per asset decides the flag: a failed
    historical attempt followed by a completed retry must not keep the asset
    flagged forever.
    """

    if not asset_ids:
        return set()
    latest_ids = (
        select(func.max(OwnedGroupOperation.id).label("op_id"))
        .where(
            OwnedGroupOperation.group_asset_id.in_(asset_ids),
            OwnedGroupOperation.operation_type == _DISSOLVE_OPERATION_TYPE,
        )
        .group_by(OwnedGroupOperation.group_asset_id)
        .subquery()
    )
    rows = await db.execute(
        select(OwnedGroupOperation.group_asset_id).where(
            OwnedGroupOperation.id.in_(select(latest_ids.c.op_id)),
            OwnedGroupOperation.status.in_(
                sorted(_DISSOLUTION_REVIEW_OPERATION_STATUSES)
            ),
        )
    )
    return {int(row) for row in rows.scalars().all()}


def _owned_group_delete_conflict(reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"reason": reason, "message": message, "retryable": False},
    )


@router.delete("/{asset_id:int}")
async def delete_failed_owned_group_draft(
    asset_id: int,
    confirm_no_telegram_group: bool = Query(default=False),
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete a failed local-only draft after strict side-effect checks.

    This endpoint never calls Telegram. The explicit query confirmation records
    the administrator's assertion that no Telegram group exists for the draft.
    """

    require_owned_group_module_enabled()
    if not confirm_no_telegram_group:
        raise _owned_group_delete_conflict(
            "delete_confirmation_required",
            "必须确认 Telegram 中不存在对应群组",
        )

    asset = (
        (
            await db.execute(
                select(OwnedGroupAsset)
                .where(OwnedGroupAsset.id == asset_id)
                .with_for_update(of=OwnedGroupAsset)
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")
    if str(asset.status) != AssetStatus.NEEDS_ATTENTION.value:
        raise _owned_group_delete_conflict(
            "asset_status_not_deletable",
            "仅可删除 needs_attention 状态的失败草稿",
        )

    linked_fields = {
        "telegram_chat_id": asset.telegram_chat_id,
        "core_group_id": asset.core_group_id,
        "managed_binding_id": asset.managed_binding_id,
        "guardian_bot_account_id": asset.guardian_bot_account_id,
    }
    if any(value is not None for value in linked_fields.values()):
        raise _owned_group_delete_conflict(
            "asset_has_telegram_or_governance_binding",
            "资产已存在 Telegram 或治理绑定，不能作为本地草稿删除",
        )
    if str(asset.governance_status or "disabled") != "disabled":
        raise _owned_group_delete_conflict(
            "asset_governance_not_disabled",
            "资产治理状态未停用，不能删除",
        )

    blocking_operation_id = await db.scalar(
        select(OwnedGroupOperation.id)
        .where(
            OwnedGroupOperation.group_asset_id == asset_id,
            OwnedGroupOperation.status.in_(sorted(_NONTERMINAL_DELETE_BLOCKING_OPERATION_STATUSES)),
        )
        .order_by(OwnedGroupOperation.id)
        .limit(1)
        .with_for_update(of=OwnedGroupOperation)
    )
    if blocking_operation_id is not None:
        raise _owned_group_delete_conflict(
            "asset_operation_not_terminal",
            "资产仍存在未结束的编排任务",
        )

    policy_id = await db.scalar(
        select(GroupAccountMessagePolicy.id)
        .where(GroupAccountMessagePolicy.owned_group_asset_id == asset_id)
        .limit(1)
    )
    execution_id = await db.scalar(
        select(GroupAccountMessageExecution.id)
        .where(GroupAccountMessageExecution.owned_group_asset_id == asset_id)
        .limit(1)
    )
    if policy_id is not None or execution_id is not None:
        raise _owned_group_delete_conflict(
            "asset_has_message_configuration",
            "资产已接入群内消息功能，不能删除",
        )

    operation_ids = list(
        (
            await db.scalars(
                select(OwnedGroupOperation.id).where(OwnedGroupOperation.group_asset_id == asset_id)
            )
        ).all()
    )
    audit_events = (
        (
            await db.scalars(
                select(OwnedGroupAuditEvent)
                .where(OwnedGroupAuditEvent.group_asset_id == asset_id)
                .with_for_update(of=OwnedGroupAuditEvent)
            )
        )
        .unique()
        .all()
    )
    for event in audit_events:
        event.group_asset_id = None
        if event.operation_id in operation_ids:
            event.operation_id = None
            event.operation_item_id = None

    db.add(
        OwnedGroupAuditEvent(
            event_type="asset_failed_draft_deleted",
            group_asset_id=None,
            resource_type="owned_group_asset",
            resource_id=asset_id,
            actor_id=int(current_user["id"]),
            before_state=json.dumps(
                {
                    "id": asset_id,
                    "status": str(asset.status),
                    "internal_name": asset.internal_name,
                    "operation_count": len(operation_ids),
                    "confirmation": "no_telegram_group",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            after_state=json.dumps({"deleted": True}, sort_keys=True),
            result="success",
            reason_code="failed_local_draft_deleted",
        )
    )
    try:
        await db.delete(asset)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _owned_group_delete_conflict(
            "asset_delete_dependency_conflict",
            "资产仍被其他记录引用，删除已取消",
        ) from exc

    return {
        "code": 0,
        "message": "失败草稿已删除",
        "data": {"id": asset_id},
    }
@router.post(
    "/{asset_id:int}/dissolution",
    response_model=OwnedGroupOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_owned_group_dissolution(
    asset_id: int,
    request: OwnedGroupDissolutionCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationResponse:
    """Queue one administrator-confirmed Telegram group dissolution.

    This endpoint never contacts Telegram. The dedicated single-file worker owns
    the remote call after persisting the intent and fencing the asset state.
    """

    require_owned_group_module_enabled()
    if request.confirmation.upper() != f"DISSOLVE {asset_id}":
        raise _owned_group_delete_conflict(
            "dissolution_confirmation_invalid",
            f"请输入确认短语 DISSOLVE {asset_id}",
        )

    asset = (
        (
            await db.execute(
                select(OwnedGroupAsset)
                .where(OwnedGroupAsset.id == asset_id)
                .with_for_update(of=OwnedGroupAsset)
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")

    selection, selection_hash = build_selection_snapshot([])
    config, config_hash = build_config_snapshot(
        {
            "operation": _DISSOLVE_OPERATION_TYPE,
            "asset_id": int(asset.id),
            "telegram_chat_id": int(asset.telegram_chat_id or 0),
            "batch_size": 1,
            "batch_interval_seconds": 60,
            "max_parallelism": 1,
            "max_attempts": 1,
        }
    )
    existing = await db.scalar(
        select(OwnedGroupOperation).where(OwnedGroupOperation.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if (
            existing.group_asset_id != asset.id
            or existing.operation_type != _DISSOLVE_OPERATION_TYPE
        ):
            raise _owned_group_delete_conflict(
                "idempotency_key_conflict",
                "Idempotency-Key 已用于另一项群组操作",
            )
        if not _idempotency_payload_matches(
            existing,
            selection_hash=selection_hash,
            config_hash=config_hash,
            schedule_at=None,
        ):
            raise _idempotency_payload_mismatch(existing)
        return _operation_response(existing)

    if asset.status != AssetStatus.READY.value:
        raise _owned_group_delete_conflict(
            "asset_not_ready_for_dissolution",
            "仅 ready 状态且已创建 Telegram 群的资产可以解散",
        )
    if not asset.telegram_chat_id:
        raise _owned_group_delete_conflict(
            "telegram_group_missing",
            "该资产没有可核验的 Telegram 群 ID，不能发起解散",
        )
    if str(asset.governance_status or "disabled") != "disabled":
        raise _owned_group_delete_conflict(
            "governance_must_be_disabled",
            "请先停用 Guardian 治理后再解散群",
        )

    blocking_operation_id = await db.scalar(
        select(OwnedGroupOperation.id)
        .where(
            OwnedGroupOperation.group_asset_id == asset.id,
            OwnedGroupOperation.status.in_(sorted(_NONTERMINAL_DELETE_BLOCKING_OPERATION_STATUSES)),
        )
        .order_by(OwnedGroupOperation.id)
        .limit(1)
        .with_for_update(of=OwnedGroupOperation)
    )
    if blocking_operation_id is not None:
        raise _owned_group_delete_conflict(
            "owned_group_operation_active",
            "请先完成、停止并核验所有进行中的成员编排任务",
        )

    active_policy_id = await db.scalar(
        select(GroupAccountMessagePolicy.id)
        .where(
            GroupAccountMessagePolicy.owned_group_asset_id == asset.id,
            GroupAccountMessagePolicy.enabled.is_(True),
        )
        .limit(1)
        .with_for_update(of=GroupAccountMessagePolicy)
    )
    if active_policy_id is not None:
        raise _owned_group_delete_conflict(
            "message_policy_must_be_disabled",
            "请先停用群内消息策略后再解散群",
        )
    active_execution_id = await db.scalar(
        select(GroupAccountMessageExecution.id)
        .where(
            GroupAccountMessageExecution.owned_group_asset_id == asset.id,
            GroupAccountMessageExecution.status.not_in(TERMINAL_EXECUTION_STATUSES),
        )
        .limit(1)
        .with_for_update(of=GroupAccountMessageExecution)
    )
    if active_execution_id is not None:
        raise _owned_group_delete_conflict(
            "message_execution_active",
            "请先处理或取消全部未结束的群内消息任务",
        )

    assert_transition("asset", asset.status, AssetStatus.DISSOLVING.value)
    asset.status = AssetStatus.DISSOLVING.value
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type=_DISSOLVE_OPERATION_TYPE,
        status=OperationStatus.QUEUED.value,
        selection_snapshot=_snapshot_json(selection),
        selection_snapshot_hash=selection_hash,
        config_snapshot=_snapshot_json(config),
        config_snapshot_hash=config_hash,
        idempotency_key=idempotency_key,
        planned_count=1,
        created_by=int(current_user["id"]),
    )
    db.add(operation)
    try:
        await db.flush()
    except IntegrityError as exc:
        if not _is_idempotency_conflict(exc):
            raise
        # The database unique key is the final arbiter if two administrators
        # submit the same key concurrently. Return the durable first intent;
        # never queue a second irreversible remote request.
        await db.rollback()
        concurrent = await db.scalar(
            select(OwnedGroupOperation).where(
                OwnedGroupOperation.idempotency_key == idempotency_key
            )
        )
        if concurrent is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key is already being processed",
            ) from exc
        if (
            concurrent.group_asset_id != asset_id
            or concurrent.operation_type != _DISSOLVE_OPERATION_TYPE
        ):
            raise _owned_group_delete_conflict(
                "idempotency_key_conflict",
                "Idempotency-Key 已用于另一项群组操作",
            ) from exc
        if not _idempotency_payload_matches(
            concurrent,
            selection_hash=selection_hash,
            config_hash=config_hash,
            schedule_at=None,
        ):
            raise _idempotency_payload_mismatch(concurrent) from exc
        return _operation_response(concurrent)
    db.add(
        OwnedGroupAuditEvent(
            event_type="owned_group_dissolution_queued",
            group_asset_id=asset.id,
            operation_id=operation.id,
            actor_id=int(current_user["id"]),
            before_state=AssetStatus.READY.value,
            after_state=AssetStatus.DISSOLVING.value,
            result="queued",
            reason_code="administrator_confirmed",
        )
    )
    return _operation_response(operation)


class OwnedGroupDissolutionResolutionCreate(BaseModel):
    """Administrator verdict after reviewing an uncertain Telegram dissolution."""

    outcome: Literal["archived", "still_exists"]
    confirmation: str = Field(..., min_length=10, max_length=64)

    @field_validator("confirmation")
    @classmethod
    def normalize_confirmation(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("confirmation must not be blank")
        return normalized


@router.post("/{asset_id:int}/dissolution/resolution")
async def resolve_owned_group_dissolution(
    asset_id: int,
    request: OwnedGroupDissolutionResolutionCreate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Resolve a dissolution that ended without a verified remote outcome.

    The administrator checks the real Telegram state out of band, then either:
    - ``archived``: the group is gone -> archive the asset locally and close
      the dissolve operation as completed;
    - ``still_exists``: the group survived -> restore the asset to ready and
      close the dissolve operation as failed so a new dissolution may be queued.

    This endpoint never contacts Telegram. The typed confirmation phrase
    prevents a stray click from finalizing an irreversible bookkeeping verdict.
    """

    require_owned_group_module_enabled()
    expected_phrase = (
        f"CONFIRM DISSOLVED {asset_id}"
        if request.outcome == "archived"
        else f"CONFIRM EXISTS {asset_id}"
    )
    if request.confirmation.upper() != expected_phrase:
        raise _owned_group_delete_conflict(
            "dissolution_resolution_confirmation_invalid",
            f"请输入确认短语 {expected_phrase}",
        )

    asset = (
        (
            await db.execute(
                select(OwnedGroupAsset)
                .where(OwnedGroupAsset.id == asset_id)
                .with_for_update(of=OwnedGroupAsset)
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")

    latest_operation = (
        (
            await db.execute(
                select(OwnedGroupOperation)
                .where(
                    OwnedGroupOperation.group_asset_id == asset_id,
                    OwnedGroupOperation.operation_type == _DISSOLVE_OPERATION_TYPE,
                )
                .order_by(desc(OwnedGroupOperation.id))
                .limit(1)
                .with_for_update(of=OwnedGroupOperation)
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if latest_operation is None:
        raise _owned_group_delete_conflict(
            "dissolution_operation_missing",
            "该资产没有解散任务，无需人工核验",
        )

    target_asset_status = (
        AssetStatus.ARCHIVED.value
        if request.outcome == "archived"
        else AssetStatus.READY.value
    )
    target_operation_status = (
        OperationStatus.COMPLETED.value
        if request.outcome == "archived"
        else OperationStatus.FAILED.value
    )
    reason_code = (
        "administrator_confirmed_dissolved"
        if request.outcome == "archived"
        else "administrator_confirmed_group_still_exists"
    )

    def _resolution_response() -> dict[str, Any]:
        return {
            "code": 0,
            "message": (
                "已确认群组解散，资产归档"
                if request.outcome == "archived"
                else "已确认群仍存在，资产恢复为可用"
            ),
            "data": {
                "id": asset.id,
                "status": str(asset.status),
                "operation_id": latest_operation.id,
                "operation_status": str(latest_operation.status),
            },
        }

    if latest_operation.status in _DISSOLUTION_REVIEW_OPERATION_STATUSES:
        prior_resolution = await db.scalar(
            select(OwnedGroupAuditEvent.id)
            .where(
                OwnedGroupAuditEvent.event_type
                == "owned_group_dissolution_manually_resolved",
                OwnedGroupAuditEvent.operation_id == latest_operation.id,
                OwnedGroupAuditEvent.reason_code == reason_code,
            )
            .limit(1)
        )
        if prior_resolution is not None and str(asset.status) == target_asset_status:
            # Idempotent replay of the same administrator verdict.
            return _resolution_response()
    if str(asset.status) != AssetStatus.NEEDS_ATTENTION.value:
        raise _owned_group_delete_conflict(
            "asset_not_awaiting_dissolution_review",
            "仅解散结果不确定（needs_attention）的资产需要人工核验",
        )
    if latest_operation.status not in _DISSOLUTION_REVIEW_OPERATION_STATUSES:
        raise _owned_group_delete_conflict(
            "dissolution_operation_not_reviewable",
            "解散任务尚未结束或已有结论，无法人工核验",
        )

    now = datetime.utcnow()
    assert_transition("asset", asset.status, target_asset_status)
    previous_asset_status = str(asset.status)
    asset.status = target_asset_status
    asset.updated_at = now
    if request.outcome == "archived":
        asset.archived_at = now
        asset.member_count = 0
    if str(latest_operation.status) != target_operation_status:
        assert_transition("operation", latest_operation.status, target_operation_status)
        latest_operation.status = target_operation_status
    latest_operation.updated_at = now
    if request.outcome == "archived":
        latest_operation.completed_count = 1
        latest_operation.failed_count = 0
    else:
        latest_operation.failed_count = 1
        latest_operation.last_error = "administrator_confirmed_group_still_exists"
    if latest_operation.finished_at is None:
        latest_operation.finished_at = now

    db.add(
        OwnedGroupAuditEvent(
            event_type="owned_group_dissolution_manually_resolved",
            group_asset_id=asset.id,
            operation_id=latest_operation.id,
            actor_id=int(current_user["id"]),
            before_state=previous_asset_status,
            after_state=str(asset.status),
            result="success",
            reason_code=reason_code,
        )
    )
    await db.flush()
    return _resolution_response()




@router.post(
    "/{asset_id:int}/operations/precheck",
    response_model=OwnedGroupPrecheckResponse,
)
async def precheck_owned_group_operation(
    asset_id: int,
    request: OwnedGroupOperationCreate,
    _current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupPrecheckResponse:
    """Strict, read-only resource check with no Telegram or queue side effect.

    For ``user`` resources, ``resource_id`` is ``TelegramAccount.id``. For
    ``bot`` resources, it is ``OwnedBotProfile.id`` rather than the linked
    Telegram account id. The shared gate verifies runtime status and stored
    session/token readiness without returning any credential values.
    """

    asset = await db.get(OwnedGroupAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Owned group asset not found")

    selection, selection_hash, owner_auto_included, config, config_hash = (
        _build_operation_snapshots(request, asset.owner_account_id)
    )
    decision = await precheck_owned_group_resources(
        db,
        selection,
        asset.owner_account_id,
        require_runtime_ready=True,
    )
    return OwnedGroupPrecheckResponse(
        asset_id=asset.id,
        allowed=decision.allowed,
        reason=decision.reason,
        details=redact_sensitive_value(decision.details) if decision.details else None,
        selection_snapshot=selection,
        selection_snapshot_hash=selection_hash,
        config_snapshot=config,
        config_snapshot_hash=config_hash,
        owner_auto_included=owner_auto_included,
    )


async def _owned_group_operation_or_404(
    db: AsyncSession,
    operation_id: int,
    *,
    asset_id: int | None = None,
) -> OwnedGroupOperation:
    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None or (asset_id is not None and operation.group_asset_id != asset_id):
        raise HTTPException(status_code=404, detail="Owned group operation not found")
    return operation


@router.get(
    "/{asset_id:int}/operations/{operation_id:int}",
    response_model=OwnedGroupOperationDetailResponse,
)
async def get_asset_owned_group_operation(
    asset_id: int,
    operation_id: int,
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationDetailResponse:
    """Read one operation and enforce its parent-asset identity."""

    operation = await _owned_group_operation_or_404(db, operation_id, asset_id=asset_id)
    return await _operation_detail_response(db, operation)


async def _list_owned_group_operation_items(
    db: AsyncSession,
    operation: OwnedGroupOperation,
    *,
    status_filter: str | None,
    resource_type: str | None,
    limit: int,
    offset: int,
) -> OwnedGroupOperationItemListResponse:
    query = select(OwnedGroupOperationItem).where(
        OwnedGroupOperationItem.operation_id == operation.id
    )
    count_query = select(func.count(OwnedGroupOperationItem.id)).where(
        OwnedGroupOperationItem.operation_id == operation.id
    )
    if status_filter is not None:
        normalized_status = _validate_filter(status_filter, _item_status_values(), "item status")
        query = query.where(OwnedGroupOperationItem.status == normalized_status)
        count_query = count_query.where(OwnedGroupOperationItem.status == normalized_status)
    if resource_type is not None:
        normalized_type = _validate_filter(resource_type, _resource_type_values(), "resource type")
        query = query.where(OwnedGroupOperationItem.resource_type == normalized_type)
        count_query = count_query.where(OwnedGroupOperationItem.resource_type == normalized_type)
    rows = await db.scalars(query.order_by(OwnedGroupOperationItem.id).offset(offset).limit(limit))
    total = int((await db.scalar(count_query)) or 0)
    return OwnedGroupOperationItemListResponse(
        data=[_operation_item_response(item) for item in rows.all()],
        total=total,
    )


@router.get(
    "/operations/{operation_id:int}/items",
    response_model=OwnedGroupOperationItemListResponse,
)
async def list_owned_group_operation_items(
    operation_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    resource_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationItemListResponse:
    """List item progress for authenticated observers."""

    operation = await _owned_group_operation_or_404(db, operation_id)
    return await _list_owned_group_operation_items(
        db,
        operation,
        status_filter=status_filter,
        resource_type=resource_type,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{asset_id:int}/operations/{operation_id:int}/items",
    response_model=OwnedGroupOperationItemListResponse,
)
async def list_asset_owned_group_operation_items(
    asset_id: int,
    operation_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    resource_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationItemListResponse:
    """List item progress after checking asset/operation ownership."""

    operation = await _owned_group_operation_or_404(db, operation_id, asset_id=asset_id)
    return await _list_owned_group_operation_items(
        db,
        operation,
        status_filter=status_filter,
        resource_type=resource_type,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{asset_id:int}/operations",
    response_model=OwnedGroupOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_owned_group_operation(
    asset_id: int,
    request: OwnedGroupOperationCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    current_user: dict = Depends(require_owned_group_operator),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupOperationResponse:
    require_owned_group_module_enabled()
    asset = await db.get(OwnedGroupAsset, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Owned group asset not found")

    existing = await db.scalar(
        select(OwnedGroupOperation).where(OwnedGroupOperation.idempotency_key == idempotency_key)
    )
    if existing:
        if existing.group_asset_id != asset_id:
            raise HTTPException(
                status_code=409, detail="Idempotency-Key is already used by another asset"
            )
        try:
            (
                _selection,
                selection_hash,
                _owner_auto_included,
                _config,
                config_hash,
            ) = _build_operation_snapshots(request, asset.owner_account_id)
        except HTTPException as exc:
            # A malformed/duplicate resource list is still a payload mismatch
            # when the key has already been consumed.
            if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
                raise _idempotency_payload_mismatch(existing) from exc
            raise
        if not _idempotency_payload_matches(
            existing,
            selection_hash=selection_hash,
            config_hash=config_hash,
            schedule_at=request.schedule_at,
        ):
            raise _idempotency_payload_mismatch(existing)
        return _operation_response(existing)

    if asset.status != AssetStatus.READY.value:
        raise HTTPException(
            status_code=409, detail="The Telegram group is not ready for membership operations"
        )

    selection, selection_hash, owner_auto_included, config, config_hash = (
        _build_operation_snapshots(request, asset.owner_account_id)
    )
    precheck = await precheck_owned_group_resources(
        db,
        selection,
        asset.owner_account_id,
        require_runtime_ready=False,
    )
    if not precheck.allowed:
        if precheck.reason == "owned_group_module_disabled":
            require_owned_group_module_enabled()
        if precheck.reason in {"global_stop_enabled", "owned_group_execution_disabled"}:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": precheck.reason, "details": precheck.details},
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": precheck.reason, "details": precheck.details},
        )
    request_item_by_resource = {_resource_key(item): item for item in request.resources}
    owner_key = (ResourceType.USER.value, asset.owner_account_id)
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status=OperationStatus.QUEUED.value,
        selection_snapshot=_snapshot_json(selection),
        selection_snapshot_hash=selection_hash,
        config_snapshot=_snapshot_json(config),
        config_snapshot_hash=config_hash,
        idempotency_key=idempotency_key,
        planned_count=len(selection),
        skipped_count=1
        if owner_key in {(item["resource_type"], item["resource_id"]) for item in selection}
        else 0,
        schedule_at=request.schedule_at,
        created_by=int(current_user["id"]),
    )
    db.add(operation)
    try:
        await db.flush()
    except IntegrityError as exc:
        if not _is_idempotency_conflict(exc):
            raise
        # Two requests may pass the preflight lookup concurrently.  The
        # database unique key is the final arbiter; resolve that race back to
        # the already-created operation instead of returning a 500.
        await db.rollback()
        concurrent = await db.scalar(
            select(OwnedGroupOperation).where(
                OwnedGroupOperation.idempotency_key == idempotency_key
            )
        )
        if concurrent is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key is already being processed",
            ) from exc
        if concurrent.group_asset_id != asset_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key is already used by another asset",
            ) from exc
        if not _idempotency_payload_matches(
            concurrent,
            selection_hash=selection_hash,
            config_hash=config_hash,
            schedule_at=request.schedule_at,
        ):
            raise _idempotency_payload_mismatch(concurrent) from exc
        return _operation_response(concurrent)
    existing_assignment_rows = await db.scalars(
        select(OwnedGroupAdminAssignment).where(
            OwnedGroupAdminAssignment.group_asset_id == asset.id
        )
    )
    existing_assignment_keys = {
        (assignment.resource_type, assignment.resource_id)
        for assignment in existing_assignment_rows
    }
    for item in selection:
        resource_key = (str(item["resource_type"]), int(item["resource_id"]))
        request_item = request_item_by_resource.get(resource_key)
        is_owner = resource_key == owner_key
        owner_item = OwnedGroupOperationItem(
            operation_id=operation.id,
            resource_type=item["resource_type"],
            resource_id=item["resource_id"],
            status=(
                ItemStatus.SKIPPED_ALREADY_MEMBER.value if is_owner else ItemStatus.PENDING.value
            ),
            reason_code=(ReasonCode.ALREADY_MEMBER.value if is_owner else None),
            admin_required=bool(request_item and request_item.admin_required and not is_owner),
            admin_permissions=(
                _snapshot_json(request_item.admin_permissions)
                if request_item and request_item.admin_required and not is_owner
                else None
            ),
            admin_title=(
                request_item.admin_title
                if request_item and request_item.admin_required and not is_owner
                else None
            ),
        )
        db.add(owner_item)
        if request_item and request_item.admin_required and not is_owner:
            if resource_key in existing_assignment_keys:
                continue
            db.add(
                OwnedGroupAdminAssignment(
                    group_asset_id=asset.id,
                    resource_type=item["resource_type"],
                    resource_id=item["resource_id"],
                    permissions_snapshot=_snapshot_json(request_item.admin_permissions),
                    admin_title=request_item.admin_title,
                    status="pending",
                    created_by=int(current_user["id"]),
                )
            )
            existing_assignment_keys.add(resource_key)
    try:
        await db.flush()
    except IntegrityError as exc:
        if not _is_admin_assignment_conflict(exc):
            raise
        # A concurrent operation may have inserted this asset/resource pair
        # after the preflight query.  Roll back the transaction and surface an
        # explicit conflict instead of leaking a database error as HTTP 500.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An admin assignment for one of these resources already exists",
        ) from exc
    return _operation_response(operation)
