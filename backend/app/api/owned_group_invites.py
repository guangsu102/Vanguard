"""Authenticated access to self-owned group invite links.

Invite URLs are bearer credentials.  They are encrypted at rest and are only
decrypted for this narrowly scoped response; they are never returned from the
asset/operation APIs or written to the audit event.  Mutating actions are
 administrator-only and are delegated to the same guarded Telethon adapter used
 by the worker; this router never sends an unguarded Telegram RPC itself.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.ephemeral_secret import decrypt_ephemeral_secret
from app.core.p0_safety_gate import (
    require_owned_group_execution_enabled,
    require_owned_group_module_enabled,
)
from app.core.security import get_current_user
from app.modules.owned_group.contracts import AssetStatus
from app.modules.owned_group.lock_queries import owned_group_asset_for_update_query
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent, OwnedGroupInviteLink
from app.modules.owned_group.security import redact_sensitive_text

router = APIRouter()

# Only Telegram links that can safely be copied are returned.  In particular,
# do not turn an arbitrary decrypted database value into an operator-visible
# credential if a row was corrupted or manually tampered with.
_PUBLIC_LINK_RE = re.compile(
    r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/[A-Za-z][A-Za-z0-9_]{4,31}/?$",
    re.IGNORECASE,
)
_PRIVATE_LINK_RE = re.compile(
    r"^(?:https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:\+[A-Za-z0-9_-]{8,128}|"
    r"joinchat/[A-Za-z0-9_-]{8,128})|tg://join\?invite=[A-Za-z0-9_-]{8,128})"
    r"(?:\?[A-Za-z0-9_=&%-]*)?$",
    re.IGNORECASE,
)


class OwnedGroupInviteLinkResponse(BaseModel):
    id: int | None = None
    group_asset_id: int
    link_type: str
    link: str | None = None
    is_active: bool
    status: str
    available: bool
    created_at: datetime | None = None
    revoked_at: datetime | None = None


class OwnedGroupInviteLinkListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[OwnedGroupInviteLinkResponse]
    total: int


class OwnedGroupInviteLinkMutationResponse(BaseModel):
    """Metadata-only response for revoke/rotate operations.

    The replacement URL is deliberately not returned from a mutation response;
    the caller must make a separate explicit, non-cacheable GET to reveal it.
    """

    code: int = 0
    message: str = "success"
    data: dict[str, Any]


class OwnedGroupInviteLinkRegenerateRequest(BaseModel):
    request_needed: bool | None = None


def _actor_id(current_user: dict) -> int | None:
    try:
        value = current_user.get("id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_public_link(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    # ``redact_sensitive_text`` catches bearer-style links even if a legacy row
    # accidentally put one in ``public_link``.  Do not return its placeholder
    # as a copyable URL.
    if redact_sensitive_text(raw, max_length=255) != raw:
        return None
    return raw if _PUBLIC_LINK_RE.fullmatch(raw) else None


def _safe_private_link(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    # A valid private invite is deliberately classified as sensitive by the
    # generic redactor.  Validation below is the intended allow-list for this
    # one authenticated, explicit reveal endpoint.
    return raw if _PRIVATE_LINK_RE.fullmatch(raw) else None


def _audit_link_view(db: AsyncSession, asset_id: int, current_user: dict) -> None:
    """Record access without putting the bearer URL into audit state."""

    db.add(
        OwnedGroupAuditEvent(
            event_type="owned_group_invite_link_viewed",
            group_asset_id=asset_id,
            actor_id=_actor_id(current_user),
            result="success",
            reason_code="invite_link_read",
        )
    )


async def require_owned_group_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Require an administrator for remote invite-link mutations."""

    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owned group invite-link administration requires admin access",
        )
    require_owned_group_module_enabled()
    return current_user


async def _mutation_adapter(db: AsyncSession):
    """Build the concrete guarded adapter after all execution gates pass."""

    # This check reads the Redis-backed global stop immediately before the
    # operation.  The adapter repeats it immediately before each Telegram RPC.
    await require_owned_group_execution_enabled()
    try:
        from app.modules.owned_group.factory import build_owned_group_adapter

        adapter = build_owned_group_adapter(db)
    except Exception as exc:  # pragma: no cover - environment-specific imports
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_adapter_unavailable"},
        ) from exc
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_execution_disabled"},
        )
    if not callable(getattr(adapter, "revoke_invite_link", None)) or not callable(
        getattr(adapter, "regenerate_invite_link", None)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_adapter_unavailable"},
        )
    return adapter


def _mutation_error(exc: Exception) -> tuple[int, str]:
    """Map adapter failures to a safe HTTP status and stable reason code."""

    reason = str(getattr(exc, "reason_code", "") or "invite_link_mutation_failed")
    # Only expose a short, machine-oriented reason.  Never pass through the
    # adapter's human message, which may contain Telegram URLs or credentials.
    reason = re.sub(r"[^a-z0-9_.-]", "_", reason.lower())[:80] or "invite_link_mutation_failed"
    if reason in {
        "global_stop_enabled",
        "safety_gate_backend_unavailable",
        "resource_precheck_unavailable",
        "owned_group_execution_disabled",
        "owned_group_module_disabled",
        "telegram_client_unavailable",
        "account_acquire_failed",
        "account_unavailable",
        "owner_account_not_found",
        "invite_link_persistence_unavailable",
        "invite_link_persistence_failed",
        "invite_link_encryption_failed",
        "bot_peer_unavailable",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE, reason
    if reason in {
        "public_link_not_revocable",
        "public_link_not_regenerable",
        "invite_link_not_active",
        "invite_link_invalid",
        "owned_group_chat_missing",
        "invite_link_missing",
    }:
        return status.HTTP_409_CONFLICT, reason
    return status.HTTP_503_SERVICE_UNAVAILABLE, reason


def _audit_link_mutation(
    db: AsyncSession,
    *,
    event_type: str,
    asset_id: int,
    actor_id: int | None,
    invite_id: int | None,
    result: str,
    reason_code: str,
) -> None:
    """Record only identifiers/status; never include plaintext or ciphertext."""

    db.add(
        OwnedGroupAuditEvent(
            event_type=event_type,
            group_asset_id=asset_id,
            actor_id=actor_id,
            before_state=(f"invite_id={invite_id}" if invite_id is not None else None),
            after_state="remote_confirmed" if result == "success" else "not_confirmed",
            result=result,
            reason_code=reason_code,
        )
    )


@router.get(
    "/{asset_id:int}/invite-links",
    response_model=OwnedGroupInviteLinkListResponse,
)
async def list_owned_group_invite_links(
    asset_id: int,
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupInviteLinkListResponse:
    """Return public/current invite links for any authenticated observer.

    Reading or copying a link does not change its Telegram or local state.  The
    response is explicitly non-cacheable because private invite links are bearer
    credentials.  Invalid/corrupt encrypted rows are represented as unavailable
    without exposing ciphertext or the decryption exception.
    """

    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"

    asset = await db.get(OwnedGroupAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owned group asset not found")

    links: list[OwnedGroupInviteLinkResponse] = []
    # A draft may contain a user-supplied public_link value, but it is not a
    # Telegram credential until the remote group is actually READY.  Also keep
    # public links out of private-group responses even if a legacy row was
    # populated inconsistently.
    public_link = (
        _safe_public_link(asset.public_link)
        if asset.status == AssetStatus.READY.value and asset.visibility == "public"
        else None
    )
    if public_link:
        links.append(
            OwnedGroupInviteLinkResponse(
                id=None,
                group_asset_id=asset.id,
                link_type="public",
                link=public_link,
                is_active=True,
                status="active",
                available=True,
                created_at=asset.created_at,
            )
        )

    rows = []
    if asset.status == AssetStatus.READY.value:
        rows = (
            await db.scalars(
                select(OwnedGroupInviteLink)
                .where(
                    OwnedGroupInviteLink.group_asset_id == asset.id,
                    OwnedGroupInviteLink.is_active.is_(True),
                )
                .order_by(desc(OwnedGroupInviteLink.created_at), desc(OwnedGroupInviteLink.id))
            )
        ).all()
    for row in rows:
        try:
            decrypted = decrypt_ephemeral_secret(row.link_ciphertext)
        except Exception:
            decrypted = None
        safe_link = _safe_private_link(decrypted)
        links.append(
            OwnedGroupInviteLinkResponse(
                id=row.id,
                group_asset_id=row.group_asset_id,
                link_type=str(row.link_type),
                link=safe_link,
                is_active=bool(row.is_active),
                status="active" if safe_link else "unavailable",
                available=bool(safe_link),
                created_at=row.created_at,
                revoked_at=row.revoked_at,
            )
        )

    _audit_link_view(db, asset.id, current_user)
    return OwnedGroupInviteLinkListResponse(data=links, total=len(links))


@router.post(
    "/{asset_id:int}/invite-links/{link_id:int}/revoke",
    response_model=OwnedGroupInviteLinkMutationResponse,
)
async def revoke_owned_group_invite_link(
    asset_id: int,
    link_id: int,
    current_user: dict = Depends(require_owned_group_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupInviteLinkMutationResponse:
    """Revoke an active private invite both remotely and in local state."""

    # Serialize all invite mutations for one asset.  Without this row lock two
    # administrators could revoke the same link and publish two replacements.
    asset = await db.scalar(
        owned_group_asset_for_update_query()
        .where(OwnedGroupAsset.id == asset_id)
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owned group asset not found")
    if asset.status != AssetStatus.READY.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "owned_group_asset_not_ready"},
        )
    invite = await db.scalar(
        select(OwnedGroupInviteLink)
        .where(OwnedGroupInviteLink.id == link_id)
        .with_for_update()
    )
    if invite is None or int(invite.group_asset_id) != int(asset.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite link not found")
    if not invite.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "invite_link_not_active"},
        )
    adapter = await _mutation_adapter(db)
    try:
        result = await adapter.revoke_invite_link(asset, invite)
    except Exception as exc:
        code, reason = _mutation_error(exc)
        _audit_link_mutation(
            db,
            event_type="owned_group_invite_link_revoke_failed",
            asset_id=asset.id,
            actor_id=_actor_id(current_user),
            invite_id=invite.id,
            result="blocked",
            reason_code=reason,
        )
        raise HTTPException(status_code=code, detail={"reason": reason}) from exc
    _audit_link_mutation(
        db,
        event_type="owned_group_invite_link_revoked",
        asset_id=asset.id,
        actor_id=_actor_id(current_user),
        invite_id=invite.id,
        result="success",
        reason_code="invite_link_revoked",
    )
    await db.flush()
    return OwnedGroupInviteLinkMutationResponse(
        data={"invite_id": result.get("invite_id", invite.id), "status": "revoked"}
    )


@router.post(
    "/{asset_id:int}/invite-links/regenerate",
    response_model=OwnedGroupInviteLinkMutationResponse,
)
async def regenerate_owned_group_invite_link(
    asset_id: int,
    request: OwnedGroupInviteLinkRegenerateRequest | None = None,
    current_user: dict = Depends(require_owned_group_admin),
    db: AsyncSession = Depends(get_db),
) -> OwnedGroupInviteLinkMutationResponse:
    """Rotate the primary private invite using the guarded Telethon adapter."""

    # The asset row is the per-group invite-rotation mutex.  PostgreSQL keeps
    # the lock until the request transaction commits after this response.
    asset = await db.scalar(
        owned_group_asset_for_update_query()
        .where(OwnedGroupAsset.id == asset_id)
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Owned group asset not found")
    if asset.status != AssetStatus.READY.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "owned_group_asset_not_ready"},
        )
    active = await db.scalar(
        select(OwnedGroupInviteLink)
        .where(
            OwnedGroupInviteLink.group_asset_id == asset.id,
            OwnedGroupInviteLink.is_active.is_(True),
        )
        .order_by(desc(OwnedGroupInviteLink.created_at), desc(OwnedGroupInviteLink.id))
    )
    adapter = await _mutation_adapter(db)
    try:
        result = await adapter.regenerate_invite_link(
            asset,
            invite=active,
            request_needed=request.request_needed if request is not None else None,
            created_by=_actor_id(current_user),
        )
    except Exception as exc:
        code, reason = _mutation_error(exc)
        _audit_link_mutation(
            db,
            event_type="owned_group_invite_link_regenerate_failed",
            asset_id=asset.id,
            actor_id=_actor_id(current_user),
            invite_id=active.id if active is not None else None,
            result="blocked",
            reason_code=reason,
        )
        raise HTTPException(status_code=code, detail={"reason": reason}) from exc
    _audit_link_mutation(
        db,
        event_type="owned_group_invite_link_regenerated",
        asset_id=asset.id,
        actor_id=_actor_id(current_user),
        invite_id=active.id if active is not None else None,
        result="success",
        reason_code="invite_link_regenerated",
    )
    await db.flush()
    return OwnedGroupInviteLinkMutationResponse(
        data={
            "invite_id": result.get("invite_id"),
            "status": "active",
            "link_type": result.get("link_type"),
        }
    )


__all__ = [
    "OwnedGroupInviteLinkMutationResponse",
    "OwnedGroupInviteLinkRegenerateRequest",
    "OwnedGroupInviteLinkListResponse",
    "OwnedGroupInviteLinkResponse",
    "list_owned_group_invite_links",
    "regenerate_owned_group_invite_link",
    "revoke_owned_group_invite_link",
    "router",
]
