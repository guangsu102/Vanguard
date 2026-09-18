"""Persistent execution boundary for self-owned group operations.

The first implementation deliberately stops at a safe adapter boundary.  No
Telegram request is made by the default adapter.  The worker owns the
database state machine, leases, retries and recovery; a later Telethon
adapter can be injected without changing the persisted contract.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import TelegramAccount
from app.core.p0_safety_gate import (
    get_safety_gate_state,
    is_owned_group_execution_enabled,
    is_owned_group_module_enabled,
    precheck_owned_group_resources,
)
from app.core.telegram_chat_lock import acquire_telegram_chat_transaction_lock
from app.modules.guardian.models import ManagedGroupBinding, ManagedGroupBindingStatus
from app.modules.owned_group.contracts import (
    AssetStatus,
    ItemStatus,
    OperationStatus,
    ReasonCode,
    assert_transition,
)
from app.modules.owned_group.lock_queries import owned_group_asset_for_update_query
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import (
    OwnedGroupAdminAssignment,
    OwnedGroupAuditEvent,
    OwnedGroupMembership,
)
from app.modules.owned_group.security import safe_exception_message

IN_FLIGHT_ITEM_STATUSES = {
    ItemStatus.IN_PROGRESS.value,
    ItemStatus.INVITE_SENT.value,
    ItemStatus.WAITING_APPROVAL.value,
    ItemStatus.ADMIN_PROMOTING.value,
}
UNRESOLVED_ITEM_STATUSES = IN_FLIGHT_ITEM_STATUSES | {ItemStatus.UNKNOWN.value}
SUCCESS_ITEM_STATUSES = {
    ItemStatus.MEMBER_VERIFIED.value,
    ItemStatus.ADMIN_VERIFIED.value,
    ItemStatus.SKIPPED_ALREADY_MEMBER.value,
}
FAILED_ITEM_STATUSES = {
    ItemStatus.FAILED_TRANSIENT.value,
    ItemStatus.FAILED_PERMANENT.value,
}

DISSOLVE_OPERATION_TYPE = "dissolve"

_PUBLIC_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TELEGRAM_CHAT_ID_MIN = -(2**63)
_TELEGRAM_CHAT_ID_MAX = 2**63 - 1


@dataclass(frozen=True)
class PreflightResult:
    """Result returned by an adapter before a Telegram group is created."""

    ready: bool
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class GroupCreateResult:
    """Result of creating a group, without exposing credentials or secrets."""

    success: bool
    telegram_chat_id: int | None = None
    telegram_user_id: int | None = None
    telegram_username: str | None = None
    public_link: str | None = None
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class GroupDissolveResult:
    """Terminal outcome of one explicitly confirmed Telegram group deletion."""

    success: bool
    unknown: bool = False
    reason_code: str | None = None
    message: str | None = None

@dataclass(frozen=True)
class ItemExecutionResult:
    """Normalized result of one membership/admin operation."""

    status: str | None = None
    success: bool = False
    transient: bool = False
    reason_code: str | None = None
    error_message: str | None = None
    telegram_user_id: int | None = None
    retry_after_seconds: int | None = None
    # Read-only reconciliation can prove membership while also proving that an
    # administrator assignment is incomplete.  Keep the item failure/retry
    # state separate from the observed membership truth.
    membership_verified: bool = False
    is_admin: bool | None = None
    admin_permissions: dict[str, bool] | None = None
    admin_title: str | None = None


class OwnedGroupTelegramAdapter(Protocol):
    """Side-effect boundary implemented by the future Telethon adapter.

    Implementations must make each call idempotent or reconcile uncertain
    outcomes before retrying.  The protocol intentionally accepts domain
    models so the worker remains independent from a concrete Telegram SDK.
    """

    async def preflight(
        self, asset: OwnedGroupAsset, owner: TelegramAccount | None
    ) -> PreflightResult | dict[str, Any]: ...

    async def create_group(
        self, asset: OwnedGroupAsset, owner: TelegramAccount | None
    ) -> GroupCreateResult | dict[str, Any]: ...

    async def dissolve_group(
        self, asset: OwnedGroupAsset
    ) -> GroupDissolveResult | dict[str, Any]: ...
    async def execute_item(
        self,
        asset: OwnedGroupAsset,
        operation: OwnedGroupOperation,
        item: OwnedGroupOperationItem,
    ) -> ItemExecutionResult | dict[str, Any]: ...

    async def reconcile_item(
        self,
        asset: OwnedGroupAsset,
        operation: OwnedGroupOperation,
        item: OwnedGroupOperationItem,
    ) -> ItemExecutionResult | dict[str, Any] | None: ...


class NoopOwnedGroupTelegramAdapter:
    """Safe default adapter; it never performs a Telegram side effect."""

    async def preflight(
        self, asset: OwnedGroupAsset, owner: TelegramAccount | None
    ) -> PreflightResult:
        return PreflightResult(
            ready=False,
            reason_code="telegram_adapter_not_configured",
            message="Telegram adapter is not configured; no group operation was executed",
        )

    async def create_group(
        self, asset: OwnedGroupAsset, owner: TelegramAccount | None
    ) -> GroupCreateResult:
        return GroupCreateResult(
            success=False,
            reason_code="telegram_adapter_not_configured",
            message="Telegram adapter is not configured; group creation was not attempted",
        )

    async def dissolve_group(self, asset: OwnedGroupAsset) -> GroupDissolveResult:
        return GroupDissolveResult(
            success=False,
            reason_code="telegram_adapter_not_configured",
            message="Telegram adapter is not configured; group dissolution was not attempted",
        )


    async def execute_item(
        self,
        asset: OwnedGroupAsset,
        operation: OwnedGroupOperation,
        item: OwnedGroupOperationItem,
    ) -> ItemExecutionResult:
        return ItemExecutionResult(
            status=ItemStatus.FAILED_PERMANENT.value,
            reason_code="telegram_adapter_not_configured",
            error_message="Telegram adapter is not configured; membership operation was not attempted",
        )

    async def reconcile_item(
        self,
        asset: OwnedGroupAsset,
        operation: OwnedGroupOperation,
        item: OwnedGroupOperationItem,
    ) -> None:
        # Returning None means that the uncertain Telegram outcome remains
        # UNKNOWN and requires an operator or a real adapter to reconcile it.
        return None


def _now() -> datetime:
    return datetime.utcnow()


def _adapter_or_noop(adapter: OwnedGroupTelegramAdapter | None) -> OwnedGroupTelegramAdapter:
    return adapter if adapter is not None else NoopOwnedGroupTelegramAdapter()


def _coerce_preflight(value: PreflightResult | dict[str, Any]) -> PreflightResult:
    if isinstance(value, PreflightResult):
        return value
    return PreflightResult(
        ready=bool(value.get("ready", False)),
        reason_code=value.get("reason_code"),
        message=value.get("message") or value.get("error_message"),
    )


def _coerce_create(value: GroupCreateResult | dict[str, Any]) -> GroupCreateResult:
    if isinstance(value, GroupCreateResult):
        return value
    return GroupCreateResult(
        success=bool(value.get("success", False)),
        telegram_chat_id=value.get("telegram_chat_id") or value.get("chat_id"),
        telegram_user_id=value.get("telegram_user_id") or value.get("user_id"),
        telegram_username=value.get("telegram_username") or value.get("username"),
        public_link=value.get("public_link"),
        reason_code=value.get("reason_code"),
        message=value.get("message") or value.get("error_message"),
    )


def _coerce_dissolve(value: GroupDissolveResult | dict[str, Any]) -> GroupDissolveResult:
    if isinstance(value, GroupDissolveResult):
        return value
    return GroupDissolveResult(
        success=bool(value.get("success", False)),
        unknown=bool(value.get("unknown", False)),
        reason_code=value.get("reason_code"),
        message=value.get("message") or value.get("error_message"),
    )


def _coerce_item(value: ItemExecutionResult | dict[str, Any]) -> ItemExecutionResult:
    if isinstance(value, ItemExecutionResult):
        return value
    status = value.get("status")
    if hasattr(status, "value"):
        status = status.value
    retry_after = value.get("retry_after_seconds")
    try:
        retry_after = int(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    raw_permissions = value.get("admin_permissions")
    admin_permissions = (
        {str(key): bool(flag) for key, flag in raw_permissions.items()}
        if isinstance(raw_permissions, dict)
        else None
    )
    return ItemExecutionResult(
        status=str(status) if status else None,
        success=bool(value.get("success", False)),
        transient=bool(value.get("transient", False)),
        reason_code=value.get("reason_code"),
        error_message=value.get("error_message") or value.get("message"),
        telegram_user_id=value.get("telegram_user_id"),
        retry_after_seconds=retry_after,
        membership_verified=bool(value.get("membership_verified", False)),
        is_admin=(bool(value["is_admin"]) if value.get("is_admin") is not None else None),
        admin_permissions=admin_permissions,
        admin_title=(str(value["admin_title"]) if value.get("admin_title") is not None else None),
    )


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_reconcile_chat_id(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("telegram_chat_id must be a signed 64-bit integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("telegram_chat_id must be a signed 64-bit integer") from exc
    if normalized == 0 or not _TELEGRAM_CHAT_ID_MIN <= normalized <= _TELEGRAM_CHAT_ID_MAX:
        raise ValueError("telegram_chat_id must be a non-zero signed 64-bit integer")
    return normalized


def _normalize_reconcile_username(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    if not normalized:
        return None
    if not _PUBLIC_USERNAME.fullmatch(normalized):
        raise ValueError("telegram_username must be a valid public Telegram username")
    return normalized


async def _ensure_owner_membership(
    db: AsyncSession,
    asset: OwnedGroupAsset,
    *,
    now: datetime,
    telegram_user_id: int | None = None,
) -> None:
    """Persist the group owner as a verified member exactly once.

    The owner is automatically included in every plan and Telegram creates the
    owner as a member.  Keeping that fact in the membership table makes a
    restart/reconcile idempotent and prevents the UI from reporting an empty
    group after the first operation has completed.
    """

    membership = await db.scalar(
        select(OwnedGroupMembership).where(
            OwnedGroupMembership.group_asset_id == asset.id,
            OwnedGroupMembership.resource_type == "user",
            OwnedGroupMembership.resource_id == asset.owner_account_id,
        )
    )
    if membership is None:
        membership = OwnedGroupMembership(
            group_asset_id=asset.id,
            resource_type="user",
            resource_id=asset.owner_account_id,
            status=ItemStatus.SKIPPED_ALREADY_MEMBER.value,
            is_admin=True,
            joined_at=now,
            last_verified_at=now,
        )
        db.add(membership)
    else:
        membership.status = ItemStatus.SKIPPED_ALREADY_MEMBER.value
        membership.is_admin = True
        membership.joined_at = membership.joined_at or now
        membership.last_verified_at = now
        membership.updated_at = now
    if telegram_user_id is not None:
        membership.telegram_user_id = int(telegram_user_id)


async def _persist_item_result(
    db: AsyncSession,
    asset: OwnedGroupAsset,
    item: OwnedGroupOperationItem,
    result: ItemExecutionResult,
    *,
    now: datetime,
) -> None:
    """Upsert membership/admin state after a normalized adapter result."""

    membership = await db.scalar(
        select(OwnedGroupMembership).where(
            OwnedGroupMembership.group_asset_id == asset.id,
            OwnedGroupMembership.resource_type == item.resource_type,
            OwnedGroupMembership.resource_id == item.resource_id,
        )
    )
    if membership is None:
        membership = OwnedGroupMembership(
            group_asset_id=asset.id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
        )
        db.add(membership)

    if result.telegram_user_id is not None:
        membership.telegram_user_id = int(result.telegram_user_id)
    membership_verified = result.membership_verified or item.status in {
        ItemStatus.MEMBER_VERIFIED.value,
        ItemStatus.ADMIN_VERIFIED.value,
        ItemStatus.SKIPPED_ALREADY_MEMBER.value,
    }
    # An admin-contract mismatch is a retryable item failure, but Telegram has
    # still confirmed that the resource joined.  Persist that fact as member
    # truth without incorrectly completing the requested admin assignment.
    membership.status = (
        ItemStatus.MEMBER_VERIFIED.value
        if result.membership_verified and item.status not in SUCCESS_ITEM_STATUSES
        else item.status
    )
    membership.is_admin = (
        bool(result.is_admin)
        if result.is_admin is not None
        else item.status == ItemStatus.ADMIN_VERIFIED.value
    )
    if result.admin_title is not None:
        membership.admin_title = result.admin_title
    elif item.admin_title and membership.is_admin:
        membership.admin_title = item.admin_title
    if result.admin_permissions is not None:
        membership.permissions_snapshot = _safe_json(result.admin_permissions)
    elif item.admin_permissions:
        membership.permissions_snapshot = item.admin_permissions
    if membership_verified:
        membership.joined_at = membership.joined_at or now
        membership.last_verified_at = now
    membership.updated_at = now

    if item.admin_required:
        assignment = await db.scalar(
            select(OwnedGroupAdminAssignment).where(
                OwnedGroupAdminAssignment.group_asset_id == asset.id,
                OwnedGroupAdminAssignment.resource_type == item.resource_type,
                OwnedGroupAdminAssignment.resource_id == item.resource_id,
            )
        )
        if assignment is not None:
            if item.status == ItemStatus.ADMIN_VERIFIED.value:
                assignment.status = "verified"
                assignment.verified_at = assignment.verified_at or now
            elif item.status == ItemStatus.FAILED_PERMANENT.value:
                assignment.status = "failed"
            elif item.status == ItemStatus.FAILED_TRANSIENT.value:
                assignment.status = "pending"
            elif item.status in {
                ItemStatus.MEMBER_VERIFIED.value,
                ItemStatus.ADMIN_PROMOTING.value,
                ItemStatus.INVITE_SENT.value,
                ItemStatus.WAITING_APPROVAL.value,
            }:
                assignment.status = "pending"
            assignment.updated_at = now

    # Recompute rather than increment so a replay/reconcile cannot inflate the
    # asset count.  The owner row is included as a verified membership.
    verified_count = await db.scalar(
        select(func.count(OwnedGroupMembership.id)).where(
            OwnedGroupMembership.group_asset_id == asset.id,
            OwnedGroupMembership.status.in_(
                [
                    ItemStatus.MEMBER_VERIFIED.value,
                    ItemStatus.ADMIN_VERIFIED.value,
                    ItemStatus.SKIPPED_ALREADY_MEMBER.value,
                ]
            ),
        )
    )
    asset.member_count = int(verified_count or 0)
    asset.updated_at = now


def _audit(
    db: AsyncSession,
    *,
    event_type: str,
    asset_id: int | None = None,
    operation_id: int | None = None,
    operation_item_id: int | None = None,
    actor_id: int | None = None,
    before_state: str | None = None,
    after_state: str | None = None,
    result: str = "success",
    reason_code: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Append a redacted audit event; never include tokens or invite URLs."""

    db.add(
        OwnedGroupAuditEvent(
            event_type=event_type,
            group_asset_id=asset_id,
            operation_id=operation_id,
            operation_item_id=operation_item_id,
            actor_id=actor_id,
            before_state=before_state,
            after_state=after_state,
            result=result,
            reason_code=reason_code,
            correlation_id=correlation_id or uuid.uuid4().hex,
        )
    )


async def _claim_owned_group_chat_id(
    db: AsyncSession,
    asset: OwnedGroupAsset,
    telegram_chat_id: int,
    *,
    actor_id: int | None = None,
) -> None:
    """Claim a chat as owned and quarantine a racing legacy auto-binding."""

    chat_id = int(telegram_chat_id)
    await acquire_telegram_chat_transaction_lock(db, chat_id)
    bindings = (
        await db.scalars(
            select(ManagedGroupBinding).where(
                ManagedGroupBinding.telegram_group_id == chat_id
            )
        )
    ).all()
    active_bindings = [
        binding
        for binding in bindings
        if binding.binding_status == ManagedGroupBindingStatus.ACTIVE
    ]
    for binding in active_bindings:
        binding.binding_status = ManagedGroupBindingStatus.INACTIVE
    asset.telegram_chat_id = chat_id
    if active_bindings:
        _audit(
            db,
            event_type="owned_group_legacy_binding_quarantined",
            asset_id=asset.id,
            actor_id=actor_id,
            before_state=f"active_bindings:{len(active_bindings)}",
            after_state="active_bindings:0",
            result="blocked",
            reason_code="owned_group_requires_explicit_governance",
        )


async def enqueue_asset_precheck(
    db: AsyncSession, asset_id: int, *, actor_id: int | None = None
) -> OwnedGroupAsset:
    """Persistently enqueue a draft for preflight.

    Repeating the request while already ``PRECHECKING`` is idempotent.  No
    Telegram call is made here; the worker performs the adapter call later.
    """

    # Serialize operator queue/retry changes for this asset.
    asset = await db.scalar(
        owned_group_asset_for_update_query()
        .where(OwnedGroupAsset.id == asset_id)
        .execution_options(populate_existing=True)
    )
    if asset is None:
        raise LookupError("Owned group asset not found")
    if asset.status == AssetStatus.PRECHECKING.value:
        return asset
    if asset.status == AssetStatus.NEEDS_ATTENTION.value:
        # NEEDS_ATTENTION is also used for an uncertain Telegram create.  Do
        # not let a normal retry button create a second remote group.  A pure
        # local preflight failure is safe to retry; identify that case from the
        # latest audit event rather than from the status alone.
        latest_audit = await db.scalar(
            select(OwnedGroupAuditEvent)
            .where(OwnedGroupAuditEvent.group_asset_id == asset.id)
            .order_by(OwnedGroupAuditEvent.id.desc())
            .limit(1)
        )
        if latest_audit is None or latest_audit.event_type != "owned_group_precheck_failed":
            raise ValueError(
                "Asset requires explicit Telegram reconciliation before another create attempt"
            )
    if asset.status == AssetStatus.CREATE_FAILED.value and asset.telegram_chat_id:
        raise ValueError("Asset has a remote chat; reconcile it before retrying creation")
    if asset.status not in {
        AssetStatus.DRAFT.value,
        AssetStatus.CREATE_FAILED.value,
        AssetStatus.NEEDS_ATTENTION.value,
    }:
        raise ValueError(f"Asset cannot be prechecked from status {asset.status}")
    previous = asset.status
    assert_transition("asset", previous, AssetStatus.PRECHECKING.value)
    asset.status = AssetStatus.PRECHECKING.value
    asset.updated_at = _now()
    _audit(
        db,
        event_type="owned_group_precheck_queued",
        asset_id=asset.id,
        actor_id=actor_id,
        before_state=previous,
        after_state=asset.status,
    )
    await db.flush()
    return asset


async def execute_asset_precheck(
    db: AsyncSession,
    asset_id: int,
    *,
    adapter: OwnedGroupTelegramAdapter | None = None,
    durable_claims: bool = False,
) -> dict[str, Any]:
    """Run the preflight/create state machine for one asset.

    A failed or unavailable adapter always ends in ``NEEDS_ATTENTION`` or
    ``CREATE_FAILED``.  The worker never marks an asset READY without a
    positive adapter result and a Telegram chat id.
    """

    # Serialize the PRECHECKING -> CREATING claim in the database.  Queue
    # concurrency is intentionally one, but a deployment can be accidentally
    # scaled or a delayed Celery tick can overlap.  Without this lock two
    # workers could both commit CREATING and issue CreateChannelRequest.
    asset = await db.scalar(
        owned_group_asset_for_update_query()
        .where(OwnedGroupAsset.id == asset_id)
        .execution_options(populate_existing=True)
    )
    if asset is None:
        return {"status": "not_found", "asset_id": asset_id}
    if asset.status == AssetStatus.DRAFT.value:
        await enqueue_asset_precheck(db, asset_id)
        if durable_claims:
            await db.commit()
            asset = await db.scalar(
                owned_group_asset_for_update_query()
                .where(OwnedGroupAsset.id == asset_id)
                .execution_options(populate_existing=True)
            )
            if asset is None:
                return {"status": "not_found", "asset_id": asset_id}
    if asset.status != AssetStatus.PRECHECKING.value:
        return {"status": asset.status, "asset_id": asset.id}

    owner = await db.get(TelegramAccount, asset.owner_account_id)
    executor = _adapter_or_noop(adapter)
    previous = asset.status
    try:
        preflight = _coerce_preflight(await executor.preflight(asset, owner))
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        preflight = PreflightResult(
            ready=False,
            reason_code="preflight_exception",
            message=safe_exception_message(exc),
        )
    if not preflight.ready:
        asset.status = AssetStatus.NEEDS_ATTENTION.value
        asset.updated_at = _now()
        _audit(
            db,
            event_type="owned_group_precheck_failed",
            asset_id=asset.id,
            before_state=previous,
            after_state=asset.status,
            result="blocked",
            reason_code=preflight.reason_code or "preflight_not_ready",
        )
        await db.flush()
        return {
            "status": asset.status,
            "asset_id": asset.id,
            "reason_code": preflight.reason_code or "preflight_not_ready",
            "message": preflight.message,
        }

    assert_transition("asset", AssetStatus.PRECHECKING.value, AssetStatus.CREATING.value)
    asset.status = AssetStatus.CREATING.value
    asset.updated_at = _now()
    await db.flush()
    if durable_claims:
        await db.commit()
        asset = await db.get(OwnedGroupAsset, asset_id)
        owner = await db.get(TelegramAccount, asset.owner_account_id)
    try:
        created = _coerce_create(await executor.create_group(asset, owner))
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        created = GroupCreateResult(
            success=False,
            reason_code="create_exception",
            message=safe_exception_message(exc),
        )

    if not created.success or not created.telegram_chat_id:
        # An exception may have left a Telegram-side operation uncertain. Keep
        # it operator-visible instead of retrying a potentially duplicate create.
        target = (
            AssetStatus.CREATE_FAILED.value
            if created.reason_code in {"create_rejected", "permission_denied"}
            else AssetStatus.NEEDS_ATTENTION.value
        )
        assert_transition("asset", AssetStatus.CREATING.value, target)
        asset.status = target
        asset.updated_at = _now()
        _audit(
            db,
            event_type="owned_group_create_failed",
            asset_id=asset.id,
            before_state=AssetStatus.CREATING.value,
            after_state=target,
            result="failed",
            reason_code=created.reason_code or "create_failed",
        )
        await db.flush()
        return {
            "status": target,
            "asset_id": asset.id,
            "reason_code": created.reason_code or "create_failed",
            "message": created.message,
        }

    assert_transition("asset", AssetStatus.CREATING.value, AssetStatus.READY.value)
    await _claim_owned_group_chat_id(db, asset, int(created.telegram_chat_id))
    if created.telegram_username:
        asset.telegram_username = created.telegram_username
    if created.public_link:
        asset.public_link = created.public_link
    elif asset.telegram_username:
        asset.public_link = f"https://t.me/{asset.telegram_username}"
    asset.status = AssetStatus.READY.value
    asset.updated_at = _now()
    await _ensure_owner_membership(
        db,
        asset,
        now=asset.updated_at,
        telegram_user_id=created.telegram_user_id,
    )
    _audit(
        db,
        event_type="owned_group_created",
        asset_id=asset.id,
        before_state=AssetStatus.CREATING.value,
        after_state=asset.status,
    )
    await db.flush()
    return {
        "status": asset.status,
        "asset_id": asset.id,
        "telegram_chat_id": asset.telegram_chat_id,
    }


async def reconcile_owned_group_asset(
    db: AsyncSession,
    asset_id: int,
    *,
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
    adapter: OwnedGroupTelegramAdapter | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """Verify an existing Telegram group without ever creating a new one.

    A create RPC can succeed remotely and time out before the returned chat id
    is committed.  Normal precheck retries are deliberately blocked for that
    state because replaying ``CreateChannelRequest`` could create a duplicate.
    This recovery path requires either the already persisted id or an explicit
    administrator-supplied candidate, assigns it *before* calling the adapter,
    and therefore forces the adapter down its existing-group verification path.
    """

    asset = await db.scalar(
        owned_group_asset_for_update_query().where(OwnedGroupAsset.id == asset_id)
    )
    if asset is None:
        return {"status": "not_found", "asset_id": asset_id}
    normalized_chat_id = _normalize_reconcile_chat_id(telegram_chat_id)
    normalized_username = _normalize_reconcile_username(telegram_username)
    if normalized_username is not None and asset.visibility != "public":
        raise ValueError("telegram_username can only be supplied for a public asset")
    if asset.status == AssetStatus.READY.value:
        if normalized_chat_id is not None and normalized_chat_id != int(
            asset.telegram_chat_id or 0
        ):
            raise ValueError("Ready asset is already bound to another Telegram chat")
        if normalized_username is not None:
            raise ValueError("Ready asset does not accept a recovery username")
        return {"status": asset.status, "asset_id": asset.id}
    if asset.status not in {
        AssetStatus.NEEDS_ATTENTION.value,
        AssetStatus.CREATE_FAILED.value,
    }:
        raise ValueError(f"Asset cannot be reconciled from status {asset.status}")

    original_chat_id = int(asset.telegram_chat_id) if asset.telegram_chat_id else None
    candidate_chat_id = normalized_chat_id if normalized_chat_id is not None else original_chat_id
    if not candidate_chat_id:
        raise ValueError("telegram_chat_id is required when no remote chat is persisted")
    if original_chat_id is not None and candidate_chat_id != original_chat_id:
        raise ValueError("Asset is already bound to a different Telegram chat")
    conflict_id = await db.scalar(
        select(OwnedGroupAsset.id).where(
            OwnedGroupAsset.telegram_chat_id == candidate_chat_id,
            OwnedGroupAsset.id != asset.id,
        )
    )
    if conflict_id is not None:
        raise ValueError("Telegram chat is already bound to another owned-group asset")

    executor = _adapter_or_noop(adapter)
    owner = await db.get(TelegramAccount, asset.owner_account_id)
    previous = asset.status
    original_username = asset.telegram_username
    original_public_link = asset.public_link

    def restore_recovery_metadata() -> None:
        # A candidate username is only an operator hint until Telegram confirms
        # it.  Never leave a failed/uncertain asset advertising that hint as if
        # it were a verified public address.
        asset.telegram_username = original_username
        asset.public_link = original_public_link

    assert_transition("asset", previous, AssetStatus.PRECHECKING.value)
    asset.telegram_chat_id = candidate_chat_id
    if normalized_username is not None:
        asset.telegram_username = normalized_username
        asset.public_link = None
    asset.status = AssetStatus.PRECHECKING.value
    asset.updated_at = _now()
    _audit(
        db,
        event_type="owned_group_asset_reconcile_started",
        asset_id=asset.id,
        actor_id=actor_id,
        before_state=previous,
        after_state=asset.status,
    )
    await db.flush()

    try:
        preflight = _coerce_preflight(await executor.preflight(asset, owner))
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        preflight = PreflightResult(
            ready=False,
            reason_code="preflight_exception",
            message=safe_exception_message(exc),
        )
    if not preflight.ready:
        assert_transition("asset", asset.status, AssetStatus.NEEDS_ATTENTION.value)
        asset.status = AssetStatus.NEEDS_ATTENTION.value
        # A user-supplied candidate that has not reached Telegram verification
        # is not durable evidence.  Remove it so a typo can be corrected, while
        # retaining ids that were persisted by the original create attempt.
        if original_chat_id is None:
            asset.telegram_chat_id = None
        restore_recovery_metadata()
        asset.updated_at = _now()
        _audit(
            db,
            event_type="owned_group_asset_reconcile_failed",
            asset_id=asset.id,
            actor_id=actor_id,
            before_state=AssetStatus.PRECHECKING.value,
            after_state=asset.status,
            result="blocked",
            reason_code=preflight.reason_code or "preflight_not_ready",
        )
        await db.flush()
        return {
            "status": asset.status,
            "asset_id": asset.id,
            "reason_code": preflight.reason_code or "preflight_not_ready",
        }

    assert_transition("asset", asset.status, AssetStatus.CREATING.value)
    asset.status = AssetStatus.CREATING.value
    asset.updated_at = _now()
    await db.flush()
    try:
        reconciled = _coerce_create(await executor.create_group(asset, owner))
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        reconciled = GroupCreateResult(
            success=False,
            telegram_chat_id=candidate_chat_id,
            reason_code="reconcile_exception",
            message=safe_exception_message(exc),
        )

    returned_chat_id: int | None
    try:
        returned_chat_id = (
            int(reconciled.telegram_chat_id) if reconciled.telegram_chat_id is not None else None
        )
    except (TypeError, ValueError):
        returned_chat_id = None
    if not reconciled.success or returned_chat_id != candidate_chat_id:
        assert_transition("asset", asset.status, AssetStatus.NEEDS_ATTENTION.value)
        asset.status = AssetStatus.NEEDS_ATTENTION.value
        if original_chat_id is None:
            asset.telegram_chat_id = None
        else:
            asset.telegram_chat_id = original_chat_id
        restore_recovery_metadata()
        asset.updated_at = _now()
        reason_code = reconciled.reason_code or (
            "telegram_chat_id_mismatch" if reconciled.success else "asset_reconcile_failed"
        )
        _audit(
            db,
            event_type="owned_group_asset_reconcile_failed",
            asset_id=asset.id,
            actor_id=actor_id,
            before_state=AssetStatus.CREATING.value,
            after_state=asset.status,
            result="unknown"
            if reason_code
            in {
                ReasonCode.NETWORK_TIMEOUT.value,
                ReasonCode.UNKNOWN_NEEDS_RECONCILE.value,
            }
            else "failed",
            reason_code=reason_code,
        )
        await db.flush()
        return {
            "status": asset.status,
            "asset_id": asset.id,
            "reason_code": reason_code,
        }

    assert_transition("asset", asset.status, AssetStatus.READY.value)
    await _claim_owned_group_chat_id(
        db,
        asset,
        candidate_chat_id,
        actor_id=actor_id,
    )
    if reconciled.telegram_username:
        asset.telegram_username = reconciled.telegram_username
    if reconciled.public_link:
        asset.public_link = reconciled.public_link
    elif asset.visibility == "public" and asset.telegram_username:
        asset.public_link = f"https://t.me/{asset.telegram_username}"
    asset.status = AssetStatus.READY.value
    asset.updated_at = _now()
    await _ensure_owner_membership(
        db,
        asset,
        now=asset.updated_at,
        telegram_user_id=reconciled.telegram_user_id,
    )
    _audit(
        db,
        event_type="owned_group_asset_reconciled",
        asset_id=asset.id,
        actor_id=actor_id,
        before_state=AssetStatus.CREATING.value,
        after_state=asset.status,
    )
    await db.flush()
    return {"status": asset.status, "asset_id": asset.id}


async def claim_next_owned_group_operation(
    db: AsyncSession, *, now: datetime | None = None
) -> OwnedGroupOperation | None:
    """Claim one due queued operation using a database row lock where supported."""

    current = now or _now()
    query = (
        select(OwnedGroupOperation)
        .where(
            OwnedGroupOperation.status == OperationStatus.QUEUED.value,
            or_(
                OwnedGroupOperation.schedule_at.is_(None),
                OwnedGroupOperation.schedule_at <= current,
            ),
        )
        .order_by(OwnedGroupOperation.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    operation = await db.scalar(query)
    if operation is None:
        return None
    assert_transition("operation", operation.status, OperationStatus.RUNNING.value)
    operation.status = OperationStatus.RUNNING.value
    operation.started_at = operation.started_at or current
    operation.updated_at = current
    _audit(
        db,
        event_type="owned_group_operation_started",
        asset_id=operation.group_asset_id,
        operation_id=operation.id,
        before_state=OperationStatus.QUEUED.value,
        after_state=operation.status,
    )
    await db.flush()
    return operation


def _config(operation: OwnedGroupOperation) -> dict[str, Any]:
    try:
        payload = json.loads(operation.config_snapshot or "{}")
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _apply_item_result(
    item: OwnedGroupOperationItem,
    result: ItemExecutionResult,
    *,
    now: datetime,
) -> None:
    """Apply adapter output while respecting the item state machine."""

    target = result.status
    if target and hasattr(target, "value"):
        target = target.value
    if result.success and not target:
        target = (
            ItemStatus.ADMIN_VERIFIED.value
            if item.admin_required
            else ItemStatus.MEMBER_VERIFIED.value
        )
    if result.transient and not target:
        target = ItemStatus.FAILED_TRANSIENT.value
    if not target:
        target = ItemStatus.FAILED_PERMANENT.value

    # A single adapter call may include invite, membership verification and
    # admin promotion. Persist each legal transition rather than jumping over
    # states, which keeps recovery/reconciliation deterministic.
    if target == ItemStatus.ADMIN_VERIFIED.value:
        # A reconcile may learn that membership/admin promotion completed while
        # the row was in any remote/in-flight state.  Walk through the same
        # durable milestones instead of allowing an illegal state jump.
        if item.status == ItemStatus.UNKNOWN.value:
            assert_transition("item", item.status, ItemStatus.IN_PROGRESS.value)
            item.status = ItemStatus.IN_PROGRESS.value
        if item.status in {
            ItemStatus.IN_PROGRESS.value,
            ItemStatus.INVITE_SENT.value,
            ItemStatus.WAITING_APPROVAL.value,
        }:
            assert_transition("item", item.status, ItemStatus.MEMBER_VERIFIED.value)
            item.status = ItemStatus.MEMBER_VERIFIED.value
        if item.status == ItemStatus.MEMBER_VERIFIED.value:
            assert_transition("item", item.status, ItemStatus.ADMIN_PROMOTING.value)
            item.status = ItemStatus.ADMIN_PROMOTING.value
    elif item.status == ItemStatus.UNKNOWN.value and target not in {
        ItemStatus.CANCELLED.value,
        ItemStatus.FAILED_PERMANENT.value,
    }:
        assert_transition("item", item.status, ItemStatus.IN_PROGRESS.value)
        item.status = ItemStatus.IN_PROGRESS.value

    if target != item.status:
        assert_transition("item", item.status, target)
        item.status = target
    item.reason_code = result.reason_code
    item.error_message = (
        safe_exception_message(result.error_message) if result.error_message else None
    )
    item.lease_id = None
    item.lease_expires_at = None
    item.updated_at = now
    if item.status == ItemStatus.FAILED_TRANSIENT.value:
        # Persist bounded exponential backoff so transient failures cannot
        # become a hot retry loop on successive worker ticks.
        backoff_seconds = min(3600, 30 * (2 ** max(item.attempts - 1, 0)))
        if result.retry_after_seconds is not None:
            try:
                # Telegram's FloodWait value is authoritative.  Never cap it
                # to an application-chosen day (or a retry loop can wake while
                # Telegram is still refusing the account).  The adapter already
                # normalizes the value to a positive integer; the extra guard
                # keeps injected/test adapters from turning this into a hot loop.
                retry_after_seconds = int(result.retry_after_seconds)
                if retry_after_seconds > 0:
                    backoff_seconds = max(backoff_seconds, retry_after_seconds)
            except (TypeError, ValueError):
                pass
        item.next_retry_at = now + timedelta(seconds=backoff_seconds)
    else:
        item.next_retry_at = None
    if item.status in {ItemStatus.INVITE_SENT.value, ItemStatus.WAITING_APPROVAL.value}:
        item.invited_at = item.invited_at or now
    if item.status in SUCCESS_ITEM_STATUSES:
        item.verified_at = item.verified_at or now
        if item.status in {ItemStatus.MEMBER_VERIFIED.value, ItemStatus.ADMIN_VERIFIED.value}:
            item.joined_at = item.joined_at or now
    # ``telegram_user_id`` is persisted by ``_persist_item_result`` after the
    # state transition.  It intentionally is not copied onto the operation item
    # because the membership table is the single source of truth for identity.


async def _recompute_operation(db: AsyncSession, operation: OwnedGroupOperation) -> dict[str, Any]:
    items = (
        await db.scalars(
            select(OwnedGroupOperationItem)
            .where(OwnedGroupOperationItem.operation_id == operation.id)
            .order_by(OwnedGroupOperationItem.id)
        )
    ).all()
    completed = sum(
        item.status in SUCCESS_ITEM_STATUSES - {ItemStatus.SKIPPED_ALREADY_MEMBER.value}
        for item in items
    )
    skipped = sum(item.status == ItemStatus.SKIPPED_ALREADY_MEMBER.value for item in items)
    failed = sum(item.status in FAILED_ITEM_STATUSES for item in items)
    unknown = sum(item.status == ItemStatus.UNKNOWN.value for item in items)
    in_flight = sum(item.status in IN_FLIGHT_ITEM_STATUSES for item in items)
    pending = sum(
        item.status in {ItemStatus.PENDING.value, ItemStatus.FAILED_TRANSIENT.value}
        for item in items
    )
    operation.planned_count = len(items)
    operation.completed_count = completed
    operation.skipped_count = skipped
    operation.failed_count = failed
    current = _now()
    operation.updated_at = current

    if operation.status == OperationStatus.STOPPING.value:
        # A stop request is not permission to forget an in-flight Telegram
        # side effect. Keep STOPPING until every uncertain item is reconciled.
        if not unknown and not in_flight:
            operation.status = OperationStatus.STOPPED.value
            operation.finished_at = operation.finished_at or _now()
    elif operation.status == OperationStatus.PAUSED.value:
        pass
    elif unknown or in_flight:
        # Intermediate/uncertain items must stay operator-visible.
        if operation.status == OperationStatus.RUNNING.value:
            assert_transition("operation", operation.status, OperationStatus.UNKNOWN.value)
            operation.status = OperationStatus.UNKNOWN.value
    elif pending:
        if operation.status == OperationStatus.RUNNING.value:
            config = _config(operation)
            try:
                batch_interval_seconds = max(1, int(config.get("batch_interval_seconds", 600)))
            except (TypeError, ValueError):
                batch_interval_seconds = 600
            next_batch_at = current + timedelta(seconds=batch_interval_seconds)
            has_plain_pending = any(item.status == ItemStatus.PENDING.value for item in items)
            next_retry = min(
                (
                    item.next_retry_at
                    for item in items
                    if item.status == ItemStatus.FAILED_TRANSIENT.value and item.next_retry_at
                ),
                default=None,
            )
            assert_transition("operation", operation.status, OperationStatus.QUEUED.value)
            operation.status = OperationStatus.QUEUED.value
            # Every batch gets a quiet period before the operation can be
            # claimed again. Plain pending work may resume after that period;
            # when only transient failures remain, also honor the earliest
            # adapter/server retry time.
            operation.schedule_at = (
                next_batch_at
                if has_plain_pending or next_retry is None
                else max(next_batch_at, next_retry)
            )
    elif failed:
        target = (
            OperationStatus.PARTIAL_COMPLETED.value
            if completed or skipped
            else OperationStatus.FAILED.value
        )
        if operation.status == OperationStatus.RUNNING.value:
            assert_transition("operation", operation.status, target)
            operation.status = target
            operation.finished_at = operation.finished_at or _now()
    elif operation.status == OperationStatus.RUNNING.value:
        target = OperationStatus.COMPLETED.value
        assert_transition("operation", operation.status, target)
        operation.status = target
        operation.finished_at = operation.finished_at or _now()

    await db.flush()
    return {
        "operation_id": operation.id,
        "status": operation.status,
        "planned_count": operation.planned_count,
        "completed_count": operation.completed_count,
        "skipped_count": operation.skipped_count,
        "failed_count": operation.failed_count,
        "unknown_count": unknown,
        "in_flight_count": in_flight,
        "pending_count": pending,
    }


async def _run_owned_group_dissolution(
    db: AsyncSession,
    operation: OwnedGroupOperation,
    *,
    adapter: OwnedGroupTelegramAdapter | None,
) -> dict[str, Any]:
    """Execute one irreversible deletion exactly once.

    The operation row was durably claimed before this function is called. Any
    exception after the RPC boundary becomes UNKNOWN and requires an operator to
    verify Telegram; this path deliberately has no retry or reconcile replay.
    """

    asset = await db.get(OwnedGroupAsset, operation.group_asset_id)
    if asset is None:
        operation.last_error = "owned_group_asset_not_found"
        assert_transition("operation", operation.status, OperationStatus.FAILED.value)
        operation.status = OperationStatus.FAILED.value
        operation.finished_at = _now()
        await db.flush()
        return {"operation_id": operation.id, "status": operation.status}
    if asset.status != AssetStatus.DISSOLVING.value:
        operation.last_error = "owned_group_asset_not_dissolving"
        assert_transition("operation", operation.status, OperationStatus.FAILED.value)
        operation.status = OperationStatus.FAILED.value
        operation.finished_at = _now()
        await db.flush()
        return {"operation_id": operation.id, "status": operation.status}

    executor = _adapter_or_noop(adapter)
    try:
        result = _coerce_dissolve(await executor.dissolve_group(asset))
    except Exception as exc:  # A response may have been lost after remote deletion.
        result = GroupDissolveResult(
            success=False,
            unknown=True,
            reason_code=ReasonCode.UNKNOWN_NEEDS_RECONCILE.value,
            message=safe_exception_message(exc),
        )

    now = _now()
    previous_asset_status = asset.status
    operation.planned_count = 1
    operation.updated_at = now
    if result.success:
        assert_transition("asset", asset.status, AssetStatus.ARCHIVED.value)
        asset.status = AssetStatus.ARCHIVED.value
        asset.archived_at = now
        asset.member_count = 0
        asset.updated_at = now
        assert_transition("operation", operation.status, OperationStatus.COMPLETED.value)
        operation.status = OperationStatus.COMPLETED.value
        operation.completed_count = 1
        operation.failed_count = 0
        operation.last_error = None
        operation.finished_at = now
        _audit(
            db,
            event_type="owned_group_dissolved",
            asset_id=asset.id,
            operation_id=operation.id,
            actor_id=operation.created_by,
            before_state=previous_asset_status,
            after_state=asset.status,
            result="success",
            reason_code=result.reason_code or "telegram_group_deleted",
        )
    else:
        # Whether Telegram refused before the call or its reply was lost, do not
        # return the asset to READY: further writes must stay fenced until an
        # administrator reviews the remote state.
        assert_transition("asset", asset.status, AssetStatus.NEEDS_ATTENTION.value)
        asset.status = AssetStatus.NEEDS_ATTENTION.value
        asset.updated_at = now
        target = OperationStatus.UNKNOWN.value if result.unknown else OperationStatus.FAILED.value
        assert_transition("operation", operation.status, target)
        operation.status = target
        operation.failed_count = 0 if result.unknown else 1
        operation.last_error = safe_exception_message(result.message or result.reason_code or "")
        operation.finished_at = None if result.unknown else now
        _audit(
            db,
            event_type="owned_group_dissolution_unknown" if result.unknown else "owned_group_dissolution_failed",
            asset_id=asset.id,
            operation_id=operation.id,
            actor_id=operation.created_by,
            before_state=previous_asset_status,
            after_state=asset.status,
            result="unknown" if result.unknown else "failed",
            reason_code=result.reason_code or (
                ReasonCode.UNKNOWN_NEEDS_RECONCILE.value if result.unknown else "telegram_group_delete_failed"
            ),
        )
    await db.flush()
    return {
        "operation_id": operation.id,
        "status": operation.status,
        "planned_count": operation.planned_count,
        "completed_count": operation.completed_count,
        "failed_count": operation.failed_count,
    }

async def run_owned_group_operation(
    db: AsyncSession,
    operation_id: int,
    *,
    adapter: OwnedGroupTelegramAdapter | None = None,
    max_items: int | None = None,
    durable_claims: bool = False,
    strict_precheck: bool = False,
) -> dict[str, Any]:
    """Run a persisted operation, never claiming an item twice concurrently."""

    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        return {"status": "not_found", "operation_id": operation_id}
    if operation.operation_type == DISSOLVE_OPERATION_TYPE:
        # A dissolution is a one-shot remote side effect. Never let generic
        # item recomputation turn a terminal/unknown outcome into a new action.
        if operation.status == OperationStatus.QUEUED.value:
            assert_transition("operation", operation.status, OperationStatus.RUNNING.value)
            operation.status = OperationStatus.RUNNING.value
            operation.started_at = operation.started_at or _now()
            operation.updated_at = _now()
        if operation.status != OperationStatus.RUNNING.value:
            return {
                "operation_id": operation.id,
                "status": operation.status,
                "planned_count": operation.planned_count,
                "completed_count": operation.completed_count,
                "failed_count": operation.failed_count,
            }
        if durable_claims:
            await db.flush()
            await db.commit()
            operation = await db.get(OwnedGroupOperation, operation_id)
            if operation is None:
                return {"status": "not_found", "operation_id": operation_id}
            if operation.status != OperationStatus.RUNNING.value:
                return {
                    "operation_id": operation.id,
                    "status": operation.status,
                    "planned_count": operation.planned_count,
                    "completed_count": operation.completed_count,
                    "failed_count": operation.failed_count,
                }
        return await _run_owned_group_dissolution(db, operation, adapter=adapter)
    if operation.status in {
        OperationStatus.COMPLETED.value,
        OperationStatus.PARTIAL_COMPLETED.value,
        OperationStatus.FAILED.value,
        OperationStatus.STOPPED.value,
    }:
        return await _recompute_operation(db, operation)
    if operation.status == OperationStatus.PAUSED.value:
        return await _recompute_operation(db, operation)
    if operation.status == OperationStatus.UNKNOWN.value:
        # UNKNOWN means a prior Telegram call may have succeeded while the
        # process was down. Reconcile explicitly before permitting replay.
        return await _recompute_operation(db, operation)
    if operation.status == OperationStatus.QUEUED.value:
        assert_transition("operation", operation.status, OperationStatus.RUNNING.value)
        operation.status = OperationStatus.RUNNING.value
        operation.started_at = operation.started_at or _now()
    if operation.status == OperationStatus.STOPPING.value:
        return await _recompute_operation(db, operation)
    if operation.status != OperationStatus.RUNNING.value:
        return await _recompute_operation(db, operation)

    if durable_claims:
        await db.flush()
        await db.commit()
        operation = await db.get(OwnedGroupOperation, operation_id)
        if operation is None:
            return {"status": "not_found", "operation_id": operation_id}


    asset = await db.get(OwnedGroupAsset, operation.group_asset_id)
    if asset is None or asset.status != AssetStatus.READY.value:
        operation.last_error = "owned_group_asset_not_ready"
        assert_transition("operation", operation.status, OperationStatus.FAILED.value)
        operation.status = OperationStatus.FAILED.value
        operation.finished_at = _now()
        return await _recompute_operation(db, operation)

    await _ensure_owner_membership(db, asset, now=_now())

    executor = _adapter_or_noop(adapter)
    config = _config(operation)
    try:
        batch_size = max(1, int(config.get("batch_size", 5)))
        max_attempts = max(1, int(config.get("max_attempts", 2)))
    except (TypeError, ValueError):
        batch_size, max_attempts = 5, 2
    limit = min(batch_size, max_items) if max_items is not None else batch_size
    current = _now()
    items = (
        await db.scalars(
            select(OwnedGroupOperationItem)
            .where(
                OwnedGroupOperationItem.operation_id == operation.id,
                OwnedGroupOperationItem.status.in_(
                    [ItemStatus.PENDING.value, ItemStatus.FAILED_TRANSIENT.value]
                ),
                or_(
                    OwnedGroupOperationItem.next_retry_at.is_(None),
                    OwnedGroupOperationItem.next_retry_at <= current,
                ),
            )
            .order_by(OwnedGroupOperationItem.id)
            .limit(limit)
        )
    ).all()

    for item in items:
        await db.refresh(operation)
        # A control request can change PAUSED -> QUEUED while an older worker
        # is still unwinding its current RPC.  Only the worker that owns the
        # RUNNING state may claim the next item; treating any other state as a
        # stop condition prevents two workers from advancing one operation in
        # parallel after resume.
        if operation.status != OperationStatus.RUNNING.value:
            break
        if item.status == ItemStatus.FAILED_TRANSIENT.value and item.attempts >= max_attempts:
            item.status = ItemStatus.FAILED_PERMANENT.value
            item.reason_code = item.reason_code or "max_attempts_exceeded"
            item.error_message = item.error_message or "maximum attempts exceeded"
            item.next_retry_at = None
            item.updated_at = _now()
            continue

        item.status = ItemStatus.IN_PROGRESS.value
        item.attempts += 1
        item.lease_id = f"owned-group:{operation.id}:{item.id}:{uuid.uuid4().hex}"
        item.lease_expires_at = _now() + timedelta(minutes=10)
        item.updated_at = _now()
        operation.updated_at = item.updated_at
        await db.flush()
        if durable_claims:
            await db.commit()
            operation = await db.get(OwnedGroupOperation, operation_id)
            if operation is None:
                return {"status": "not_found", "operation_id": operation_id}
            asset = await db.get(OwnedGroupAsset, operation.group_asset_id)
            if asset is None:
                return {"status": "asset_not_found", "operation_id": operation.id}
            item = await db.get(OwnedGroupOperationItem, item.id)
            if item is None:
                continue
        if durable_claims:
            # After the durable commit the item is intentionally IN_PROGRESS;
            # only abandon it if a concurrent control changed the operation or
            # the lease disappeared.
            if operation.status != OperationStatus.RUNNING.value:
                break
            if item.status != ItemStatus.IN_PROGRESS.value or item.lease_id is None:
                continue
        claimed_lease_id = item.lease_id
        try:
            result: ItemExecutionResult
            if strict_precheck:
                decision = await precheck_owned_group_resources(
                    db,
                    [
                        {"resource_type": "user", "resource_id": int(asset.owner_account_id)},
                        {
                            "resource_type": str(item.resource_type),
                            "resource_id": int(item.resource_id),
                        },
                    ],
                    int(asset.owner_account_id),
                    require_execution=True,
                    require_runtime_ready=True,
                )
                if not decision.allowed:
                    retryable = decision.reason in {
                        "global_stop_enabled",
                        "safety_gate_backend_unavailable",
                        "owned_group_execution_disabled",
                        "resource_eligibility_failed",
                    }
                    result = ItemExecutionResult(
                        status=(
                            ItemStatus.FAILED_TRANSIENT.value
                            if retryable
                            else ItemStatus.FAILED_PERMANENT.value
                        ),
                        transient=retryable,
                        reason_code=decision.reason,
                        error_message="owned-group strict precheck blocked execution",
                        retry_after_seconds=60 if retryable else None,
                    )
                else:
                    result = _coerce_item(await executor.execute_item(asset, operation, item))
            else:
                result = _coerce_item(await executor.execute_item(asset, operation, item))
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            if durable_claims:
                # An exception can arrive after a stop/reconcile worker has
                # already fenced this lease.  Re-read before writing UNKNOWN;
                # otherwise an old transport error can overwrite the newer
                # operator/reconciliation result.
                latest_operation = await db.scalar(
                    select(OwnedGroupOperation)
                    .where(OwnedGroupOperation.id == operation_id)
                    .execution_options(populate_existing=True)
                )
                latest_item = await db.scalar(
                    select(OwnedGroupOperationItem)
                    .where(OwnedGroupOperationItem.id == item.id)
                    .execution_options(populate_existing=True)
                )
                if latest_operation is None or latest_item is None:
                    break
                if (
                    latest_item.lease_id != claimed_lease_id
                    or latest_item.status != ItemStatus.IN_PROGRESS.value
                ):
                    operation = latest_operation
                    item = latest_item
                    continue
            item.status = ItemStatus.UNKNOWN.value
            item.reason_code = ReasonCode.UNKNOWN_NEEDS_RECONCILE.value
            item.error_message = safe_exception_message(exc)
            item.lease_id = None
            item.lease_expires_at = None
            operation.status = OperationStatus.UNKNOWN.value
            operation.last_error = "owned_group_item_execution_uncertain"
            operation.updated_at = _now()
            _audit(
                db,
                event_type="owned_group_item_unknown",
                asset_id=asset.id,
                operation_id=operation.id,
                operation_item_id=item.id,
                before_state=ItemStatus.IN_PROGRESS.value,
                after_state=item.status,
                result="unknown",
                reason_code=item.reason_code,
            )
            break
        if durable_claims:
            # The RPC may have outlived a stop/reconcile or a stale-lease
            # recovery in another worker.  Never let that old result overwrite
            # a newer state.  If the row is already terminal/UNKNOWN, the
            # newer writer owns the outcome; otherwise leave it untouched and
            # require explicit reconciliation.
            # Force a database round trip. ``AsyncSession.get`` may return the
            # identity-map value and miss a stop/reconcile committed by another
            # worker while the Telegram RPC was in flight.
            latest_operation = await db.scalar(
                select(OwnedGroupOperation)
                .where(OwnedGroupOperation.id == operation_id)
                .execution_options(populate_existing=True)
            )
            latest_item = await db.scalar(
                select(OwnedGroupOperationItem)
                .where(OwnedGroupOperationItem.id == item.id)
                .execution_options(populate_existing=True)
            )
            if latest_operation is None or latest_item is None:
                break
            if (
                latest_item.lease_id != claimed_lease_id
                or latest_item.status != ItemStatus.IN_PROGRESS.value
            ):
                operation = latest_operation
                item = latest_item
                continue
            operation = latest_operation
            item = latest_item

        result_now = _now()
        _apply_item_result(item, result, now=result_now)
        await _persist_item_result(db, asset, item, result, now=result_now)
        _audit(
            db,
            event_type="owned_group_item_updated",
            asset_id=asset.id,
            operation_id=operation.id,
            operation_item_id=item.id,
            before_state=ItemStatus.IN_PROGRESS.value,
            after_state=item.status,
            result="success" if item.status in SUCCESS_ITEM_STATUSES else "failed",
            reason_code=item.reason_code,
        )
        await db.flush()

    return await _recompute_operation(db, operation)


async def reconcile_stale_owned_group_operations(
    db: AsyncSession, *, stale_after_seconds: int = 900
) -> dict[str, int]:
    """Quarantine abandoned work before any future Telegram retry.

    A process can die after an item lease is committed while the operation is
    PAUSED/STOPPING (for example, an operator clicked stop during a slow RPC).
    Scanning only RUNNING rows leaves that side effect permanently invisible to
    reconciliation.  Candidate non-terminal states are inspected together and
    any row with an in-flight/UNKNOWN item is moved to UNKNOWN through the
    legal STOPPING transition.
    """

    cutoff = _now() - timedelta(seconds=max(30, int(stale_after_seconds)))
    operations = (
        await db.scalars(
            select(OwnedGroupOperation).where(
                OwnedGroupOperation.status.in_(
                    [
                        OperationStatus.RUNNING.value,
                        OperationStatus.QUEUED.value,
                        OperationStatus.PAUSED.value,
                        OperationStatus.STOPPING.value,
                    ]
                ),
                OwnedGroupOperation.updated_at < cutoff,
            )
        )
    ).all()
    changed = 0
    items_changed = 0
    for operation in operations:
        if operation.operation_type == DISSOLVE_OPERATION_TYPE:
            # A running one-shot deletion may have reached Telegram just before
            # the worker died. Fence the asset and require manual verification;
            # it is never safe to restart the RPC from a heartbeat recovery.
            if operation.status == OperationStatus.QUEUED.value:
                continue
            previous_operation_status = operation.status
            asset = await db.get(OwnedGroupAsset, operation.group_asset_id)
            if asset is not None and asset.status == AssetStatus.DISSOLVING.value:
                previous_asset_status = asset.status
                assert_transition(
                    "asset", previous_asset_status, AssetStatus.NEEDS_ATTENTION.value
                )
                asset.status = AssetStatus.NEEDS_ATTENTION.value
                asset.updated_at = _now()
            if operation.status == OperationStatus.PAUSED.value:
                assert_transition(
                    "operation", operation.status, OperationStatus.STOPPING.value
                )
                operation.status = OperationStatus.STOPPING.value
            assert_transition("operation", operation.status, OperationStatus.UNKNOWN.value)
            operation.status = OperationStatus.UNKNOWN.value
            operation.last_error = "dissolution_worker_heartbeat_stale"
            operation.updated_at = _now()
            _audit(
                db,
                event_type="owned_group_dissolution_unknown",
                asset_id=operation.group_asset_id,
                operation_id=operation.id,
                before_state=previous_operation_status,
                after_state=operation.status,
                result="unknown",
                reason_code="worker_heartbeat_stale",
            )
            changed += 1
            continue
        items = (
            await db.scalars(
                select(OwnedGroupOperationItem).where(
                    OwnedGroupOperationItem.operation_id == operation.id,
                    OwnedGroupOperationItem.status.in_(list(UNRESOLVED_ITEM_STATUSES)),
                )
            )
        ).all()
        unresolved = [item for item in items if item.status in UNRESOLVED_ITEM_STATUSES]
        if (
            operation.status
            in {
                OperationStatus.PAUSED.value,
                OperationStatus.QUEUED.value,
            }
            and not unresolved
        ):
            # There is no remote side effect to quarantine; leave an operator's
            # intentional pause/queue decision intact.
            continue
        if operation.status == OperationStatus.STOPPING.value and not unresolved:
            assert_transition("operation", operation.status, OperationStatus.STOPPED.value)
            operation.status = OperationStatus.STOPPED.value
            operation.finished_at = operation.finished_at or _now()
            operation.updated_at = _now()
            _audit(
                db,
                event_type="owned_group_operation_stopped_recovered",
                asset_id=operation.group_asset_id,
                operation_id=operation.id,
                before_state=OperationStatus.STOPPING.value,
                after_state=operation.status,
                result="recovered",
                reason_code="worker_heartbeat_stale",
            )
            changed += 1
            continue
        previous_operation_status = operation.status
        if operation.status in {
            OperationStatus.PAUSED.value,
            OperationStatus.QUEUED.value,
        }:
            assert_transition("operation", operation.status, OperationStatus.STOPPING.value)
            operation.status = OperationStatus.STOPPING.value
        assert_transition("operation", operation.status, OperationStatus.UNKNOWN.value)
        operation.status = OperationStatus.UNKNOWN.value
        operation.last_error = "worker_heartbeat_stale"
        operation.updated_at = _now()
        for item in items:
            if item.status not in IN_FLIGHT_ITEM_STATUSES:
                # Existing UNKNOWN is already quarantined; terminal/pending
                # rows have no remote effect and must remain untouched.
                continue
            assert_transition("item", item.status, ItemStatus.UNKNOWN.value)
            item.status = ItemStatus.UNKNOWN.value
            item.reason_code = ReasonCode.UNKNOWN_NEEDS_RECONCILE.value
            item.error_message = "worker heartbeat expired; Telegram outcome must be reconciled"
            item.lease_id = None
            item.lease_expires_at = None
            item.updated_at = _now()
            items_changed += 1
        _audit(
            db,
            event_type="owned_group_operation_unknown",
            asset_id=operation.group_asset_id,
            operation_id=operation.id,
            before_state=previous_operation_status,
            after_state=operation.status,
            result="unknown",
            reason_code="worker_heartbeat_stale",
        )
        changed += 1
    await db.flush()
    return {"operations": changed, "items": items_changed}


async def reconcile_stale_owned_group_assets(
    db: AsyncSession, *, stale_after_seconds: int = 900
) -> dict[str, int]:
    """Quarantine assets stuck in a remote precheck/create call.

    Re-running a timed-out create can create a second Telegram group.  The
    safe recovery state is NEEDS_ATTENTION until a concrete adapter performs
    an explicit Telegram-side lookup.
    """

    cutoff = _now() - timedelta(seconds=max(30, int(stale_after_seconds)))
    assets = (
        await db.scalars(
            select(OwnedGroupAsset).where(
                OwnedGroupAsset.status.in_(
                    [AssetStatus.PRECHECKING.value, AssetStatus.CREATING.value]
                ),
                OwnedGroupAsset.updated_at < cutoff,
            )
        )
    ).all()
    changed = 0
    for asset in assets:
        previous = asset.status
        assert_transition("asset", previous, AssetStatus.NEEDS_ATTENTION.value)
        asset.status = AssetStatus.NEEDS_ATTENTION.value
        asset.updated_at = _now()
        _audit(
            db,
            event_type="owned_group_asset_unknown",
            asset_id=asset.id,
            before_state=previous,
            after_state=asset.status,
            result="unknown",
            reason_code="create_heartbeat_stale",
        )
        changed += 1
    await db.flush()
    return {"assets": changed}


async def reconcile_owned_group_operation(
    db: AsyncSession,
    operation_id: int,
    *,
    adapter: OwnedGroupTelegramAdapter | None = None,
) -> dict[str, Any]:
    """Ask the adapter to resolve uncertain item outcomes without guessing."""

    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        return {"status": "not_found", "operation_id": operation_id}
    if operation.operation_type == DISSOLVE_OPERATION_TYPE:
        raise ValueError("Dissolution outcomes require manual Telegram verification and cannot be reconciled here")
    asset = await db.get(OwnedGroupAsset, operation.group_asset_id)
    if asset is None:
        return {"status": "asset_not_found", "operation_id": operation.id}
    items = (
        await db.scalars(
            select(OwnedGroupOperationItem).where(
                OwnedGroupOperationItem.operation_id == operation.id,
                OwnedGroupOperationItem.status.in_(
                    list(IN_FLIGHT_ITEM_STATUSES | {ItemStatus.UNKNOWN.value})
                ),
            )
        )
    ).all()
    executor = _adapter_or_noop(adapter)
    for item in items:
        previous_item_status = item.status
        try:
            raw = await executor.reconcile_item(asset, operation, item)
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            raw = ItemExecutionResult(
                status=ItemStatus.UNKNOWN.value,
                reason_code=ReasonCode.UNKNOWN_NEEDS_RECONCILE.value,
                error_message=safe_exception_message(exc),
            )
        if raw is None:
            continue
        reconciled = _coerce_item(raw)
        result_now = _now()
        _apply_item_result(item, reconciled, now=result_now)
        await _persist_item_result(db, asset, item, reconciled, now=result_now)
        _audit(
            db,
            event_type="owned_group_item_reconciled",
            asset_id=asset.id,
            operation_id=operation.id,
            operation_item_id=item.id,
            before_state=previous_item_status,
            after_state=item.status,
            reason_code=item.reason_code,
        )
    unresolved = any(
        item.status in IN_FLIGHT_ITEM_STATUSES or item.status == ItemStatus.UNKNOWN.value
        for item in items
    )
    if operation.status == OperationStatus.STOPPING.value and not unresolved:
        # Stop is finalized only after all remote outcomes are known.
        assert_transition("operation", operation.status, OperationStatus.STOPPED.value)
        operation.status = OperationStatus.STOPPED.value
        operation.finished_at = operation.finished_at or _now()
        operation.last_error = None
    elif operation.status == OperationStatus.UNKNOWN.value and not unresolved:
        assert_transition("operation", operation.status, OperationStatus.QUEUED.value)
        operation.status = OperationStatus.QUEUED.value
        operation.last_error = None
    await db.flush()
    return await _recompute_operation(db, operation)


async def pause_owned_group_operation(
    db: AsyncSession, operation_id: int, *, actor_id: int | None = None
) -> OwnedGroupOperation:
    return await _control_operation(
        db,
        operation_id,
        OperationStatus.PAUSED.value,
        actor_id=actor_id,
        event_type="owned_group_operation_paused",
    )


async def resume_owned_group_operation(
    db: AsyncSession, operation_id: int, *, actor_id: int | None = None
) -> OwnedGroupOperation:
    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        raise LookupError("Owned group operation not found")
    unresolved = await db.scalar(
        select(func.count(OwnedGroupOperationItem.id)).where(
            OwnedGroupOperationItem.operation_id == operation.id,
            OwnedGroupOperationItem.status.in_(list(UNRESOLVED_ITEM_STATUSES)),
        )
    )
    if int(unresolved or 0) > 0:
        raise ValueError("Reconcile all in-flight or UNKNOWN items before resuming")
    return await _control_operation(
        db,
        operation_id,
        OperationStatus.QUEUED.value,
        actor_id=actor_id,
        event_type="owned_group_operation_resumed",
    )


async def stop_owned_group_operation(
    db: AsyncSession, operation_id: int, *, actor_id: int | None = None
) -> OwnedGroupOperation:
    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        raise LookupError("Owned group operation not found")
    unresolved = await db.scalar(
        select(func.count(OwnedGroupOperationItem.id)).where(
            OwnedGroupOperationItem.operation_id == operation.id,
            OwnedGroupOperationItem.status.in_(list(UNRESOLVED_ITEM_STATUSES)),
        )
    )
    has_unresolved = int(unresolved or 0) > 0
    if operation.status in {
        OperationStatus.RUNNING.value,
        OperationStatus.UNKNOWN.value,
        OperationStatus.STOPPING.value,
    }:
        target = OperationStatus.STOPPING.value
    elif operation.status in {
        OperationStatus.QUEUED.value,
        OperationStatus.PAUSED.value,
        OperationStatus.DRAFT.value,
    }:
        # A paused operation can still have an invite/admin RPC in flight.  It
        # must pass through STOPPING so reconciliation gets a chance to resolve
        # that side effect before the operation becomes terminal.
        target = OperationStatus.STOPPING.value if has_unresolved else OperationStatus.STOPPED.value
    elif operation.status == OperationStatus.STOPPED.value:
        target = OperationStatus.STOPPED.value
    else:
        raise ValueError(f"Operation cannot be stopped from status {operation.status}")
    return await _control_operation(
        db,
        operation_id,
        target,
        actor_id=actor_id,
        event_type="owned_group_operation_stop_requested",
    )


async def retry_owned_group_operation(
    db: AsyncSession, operation_id: int, *, actor_id: int | None = None
) -> OwnedGroupOperation:
    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        raise LookupError("Owned group operation not found")
    if operation.operation_type == DISSOLVE_OPERATION_TYPE:
        raise ValueError("Dissolution operations cannot be retried")
    if operation.status == OperationStatus.UNKNOWN.value:
        raise ValueError("Reconcile UNKNOWN operation before retrying")
    if operation.status not in {
        OperationStatus.FAILED.value,
        OperationStatus.PARTIAL_COMPLETED.value,
        OperationStatus.STOPPED.value,
    }:
        raise ValueError(f"Operation cannot be retried from status {operation.status}")
    items = (
        await db.scalars(
            select(OwnedGroupOperationItem).where(
                OwnedGroupOperationItem.operation_id == operation.id
            )
        )
    ).all()
    for item in items:
        if item.status == ItemStatus.SKIPPED_ALREADY_MEMBER.value:
            continue
        if item.status != ItemStatus.PENDING.value:
            if item.status in {
                ItemStatus.FAILED_TRANSIENT.value,
                ItemStatus.FAILED_PERMANENT.value,
                ItemStatus.CANCELLED.value,
            }:
                assert_transition("item", item.status, ItemStatus.PENDING.value)
                item.status = ItemStatus.PENDING.value
            elif item.status == ItemStatus.UNKNOWN.value or item.status in IN_FLIGHT_ITEM_STATUSES:
                raise ValueError("Reconcile all in-flight or UNKNOWN items before retrying")
        item.next_retry_at = None
        item.reason_code = None
        item.error_message = None
        item.lease_id = None
        item.lease_expires_at = None
        item.updated_at = _now()
    operation.status = OperationStatus.QUEUED.value
    operation.last_error = None
    operation.finished_at = None
    operation.schedule_at = _now()
    operation.updated_at = _now()
    _audit(
        db,
        event_type="owned_group_operation_retry_requested",
        asset_id=operation.group_asset_id,
        operation_id=operation.id,
        actor_id=actor_id,
        after_state=operation.status,
    )
    await db.flush()
    return operation


async def _control_operation(
    db: AsyncSession,
    operation_id: int,
    target: str,
    *,
    actor_id: int | None,
    event_type: str,
) -> OwnedGroupOperation:
    operation = await db.get(OwnedGroupOperation, operation_id)
    if operation is None:
        raise LookupError("Owned group operation not found")
    if operation.operation_type == DISSOLVE_OPERATION_TYPE:
        raise ValueError("Dissolution operations cannot be controlled through generic operation actions")
    if operation.status == target:
        return operation
    if target == OperationStatus.STOPPED.value:
        unresolved = await db.scalar(
            select(func.count(OwnedGroupOperationItem.id)).where(
                OwnedGroupOperationItem.operation_id == operation.id,
                OwnedGroupOperationItem.status.in_(list(UNRESOLVED_ITEM_STATUSES)),
            )
        )
        if int(unresolved or 0) > 0:
            raise ValueError("Cannot stop while in-flight or UNKNOWN items need reconciliation")
    previous = operation.status
    assert_transition("operation", previous, target)
    operation.status = target
    operation.updated_at = _now()
    if target == OperationStatus.STOPPED.value:
        operation.finished_at = operation.finished_at or _now()
        items = (
            await db.scalars(
                select(OwnedGroupOperationItem).where(
                    OwnedGroupOperationItem.operation_id == operation.id,
                    OwnedGroupOperationItem.status.in_(
                        [
                            ItemStatus.PENDING.value,
                            ItemStatus.FAILED_TRANSIENT.value,
                            ItemStatus.UNKNOWN.value,
                        ]
                    ),
                )
            )
        ).all()
        for item in items:
            if item.status == ItemStatus.UNKNOWN.value:
                # UNKNOWN is intentionally kept unknown until reconcile; stop
                # only cancels work that has not produced an uncertain effect.
                continue
            assert_transition("item", item.status, ItemStatus.CANCELLED.value)
            item.status = ItemStatus.CANCELLED.value
            item.updated_at = _now()
    _audit(
        db,
        event_type=event_type,
        asset_id=operation.group_asset_id,
        operation_id=operation.id,
        actor_id=actor_id,
        before_state=previous,
        after_state=target,
    )
    await db.flush()
    return operation


async def run_owned_group_worker_tick(
    db: AsyncSession,
    *,
    limit: int = 10,
    stale_after_seconds: int = 900,
    adapter: OwnedGroupTelegramAdapter | None = None,
    durable_claims: bool = False,
    strict_precheck: bool = False,
) -> dict[str, Any]:
    """Process prechecks and due operations once; safe to run repeatedly."""

    limit = max(1, min(int(limit), 200))
    gate_state = await get_safety_gate_state()
    from app.core.config import settings

    # Recovery is a DB-only quarantine step.  Run it after the module switch is
    # checked but before execution/stop gates so a restart can always move an
    # abandoned lease to UNKNOWN, even while Telegram writes are disabled or a
    # global stop is active.  This never calls the adapter or claims new work.
    if not is_owned_group_module_enabled():
        return {
            "status": "blocked",
            "reason": "owned_group_module_disabled",
            "prechecks": [],
            "operations": [],
            "stale": {"operations": 0, "items": 0},
            "stale_assets": {"assets": 0},
        }
    stale = await reconcile_stale_owned_group_operations(
        db, stale_after_seconds=stale_after_seconds
    )
    stale_assets = await reconcile_stale_owned_group_assets(
        db, stale_after_seconds=stale_after_seconds
    )

    if gate_state.global_stop:
        return {
            "status": "blocked",
            "reason": "global_stop_enabled",
            "details": gate_state.__dict__,
            "prechecks": [],
            "operations": [],
            "stale": stale,
            "stale_assets": stale_assets,
        }
    if not gate_state.backend_available and bool(
        getattr(settings, "P0_SAFETY_GATE_FAIL_CLOSED", False)
    ):
        return {
            "status": "blocked",
            "reason": "safety_gate_backend_unavailable",
            "prechecks": [],
            "operations": [],
            "stale": stale,
            "stale_assets": stale_assets,
        }
    # A custom adapter is only used by tests/staging callers.  The production
    # Celery path passes None and must have the explicit execution flag enabled.
    if adapter is None and not is_owned_group_execution_enabled():
        return {
            "status": "blocked",
            "reason": "owned_group_execution_disabled",
            "prechecks": [],
            "operations": [],
            "stale": stale,
            "stale_assets": stale_assets,
        }
    precheck_assets = (
        await db.scalars(
            select(OwnedGroupAsset)
            .where(OwnedGroupAsset.status == AssetStatus.PRECHECKING.value)
            .order_by(OwnedGroupAsset.id)
            .limit(limit)
        )
    ).all()
    prechecks: list[dict[str, Any]] = []
    for asset in precheck_assets:
        prechecks.append(
            await execute_asset_precheck(
                db, asset.id, adapter=adapter, durable_claims=durable_claims
            )
        )

    operations: list[dict[str, Any]] = []
    for _ in range(limit):
        operation = await claim_next_owned_group_operation(db)
        if operation is None:
            break
        if durable_claims:
            await db.commit()
        operations.append(
            await run_owned_group_operation(
                db,
                operation.id,
                adapter=adapter,
                durable_claims=durable_claims,
                strict_precheck=strict_precheck,
            )
        )
    return {
        "status": "ok",
        "prechecks": prechecks,
        "operations": operations,
        "stale": stale,
        "stale_assets": stale_assets,
    }


__all__ = [
    "DISSOLVE_OPERATION_TYPE",
    "GroupCreateResult",
    "GroupDissolveResult",
    "ItemExecutionResult",
    "NoopOwnedGroupTelegramAdapter",
    "OwnedGroupTelegramAdapter",
    "PreflightResult",
    "claim_next_owned_group_operation",
    "enqueue_asset_precheck",
    "execute_asset_precheck",
    "pause_owned_group_operation",
    "reconcile_owned_group_asset",
    "reconcile_owned_group_operation",
    "reconcile_stale_owned_group_operations",
    "reconcile_stale_owned_group_assets",
    "retry_owned_group_operation",
    "run_owned_group_operation",
    "run_owned_group_worker_tick",
    "resume_owned_group_operation",
    "stop_owned_group_operation",
]
