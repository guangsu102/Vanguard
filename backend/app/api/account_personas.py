"""Admin API for account-level owned-group AI Personas."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, undefer_group
from sqlalchemy.orm.attributes import set_committed_value

from app.core.account.models import (
    AccountOperationConfig,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.persona import (
    PERSONA_MAX_TOTAL_BYTES,
    PERSONA_PROMPT_TEMPLATE_VERSION,
    AccountPersonaError,
    AccountPersonaService,
    AccountPersonaState,
    PersonaSnapshotResult,
    PersonaV1,
)
from app.core.automation_settings import (
    get_owned_group_ai_persona_settings,
    get_owned_group_messaging_settings,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.group.models import Group
from app.core.p0_safety_gate import _has_usable_user_session
from app.core.persona_observability import (
    record_persona_account_mismatch,
    record_persona_preview,
    record_persona_update,
    refresh_persona_configured_gauge,
)
from app.core.security import get_current_user, require_admin
from app.modules.guardian.models import ManagedGroupBinding, ManagedGroupBindingStatus
from app.modules.owned_group.messaging_models import GroupAccountMessagePolicy
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent, OwnedGroupMembership

_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PREVIEW_UNSAFE_TEXT_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)
_AUDIT_READER_ROLES = frozenset({"admin", "operator", "auditor"})
PERSONA_AUDIT_EVENT_TYPES = frozenset(
    {
        "account_persona_created",
        "account_persona_updated",
        "account_persona_reset",
        "account_persona_preview_generated",
        "account_persona_preview_failed",
    }
)
PersonaAuditEventType = Literal[
    "account_persona_created",
    "account_persona_updated",
    "account_persona_reset",
    "account_persona_preview_generated",
    "account_persona_preview_failed",
]

_ERROR_MESSAGES = {
    "ACCOUNT_NOT_FOUND": "Account not found",
    "PERSONA_REVISION_CONFLICT": "Persona revision conflict",
    "PERSONA_ACCOUNT_TYPE_UNSUPPORTED": "Account type does not support Persona",
    "PERSONA_ACCOUNT_MODE_UNSUPPORTED": "Account operation mode does not support Persona",
    "PERSONA_OPERATION_CONFIG_MISSING": "Account operation configuration is missing",
    "PERSONA_CONFIG_INVALID": "Stored Persona configuration is invalid",
    "PERSONA_VALIDATION_FAILED": "Persona validation failed",
    "PERSONA_PROMPT_INJECTION_REJECTED": "Persona contains a prohibited instruction",
    "PERSONA_AUDIT_READER_REQUIRED": "Persona audit reader role is required",
    "OWNED_GROUP_ASSET_NOT_FOUND": "Owned group asset not found",
    "PERSONA_ACCOUNT_MISMATCH": "Persona preview target account mismatch",
    "PERSONA_POLICY_MODE_UNSUPPORTED": "Persona preview requires an AI policy mode",
    "PERSONA_TOPIC_NOT_ALLOWED": "Persona preview topic is not allowed",
    "PERSONA_TOPIC_FORBIDDEN": "Persona preview topic is forbidden",
    "AI_PROVIDER_UNSAFE": "AI Provider is not authorized for Persona preview",
}


def _enum_value(value: Any) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def _correlation_id(request: Request) -> str:
    existing = getattr(request.state, "account_persona_correlation_id", None)
    if existing:
        return existing
    supplied = request.headers.get("X-Correlation-ID", "")
    value = supplied if _SAFE_CORRELATION_ID.fullmatch(supplied) else f"persona-{uuid.uuid4()}"
    request.state.account_persona_correlation_id = value
    return value


def _safe_error_details(details: dict[str, Any] | None) -> dict[str, Any]:
    raw = details or {}
    safe: dict[str, Any] = {}
    for key in (
        "field",
        "type",
        "current_revision",
        "configured",
        "persona_hash",
        "repair_action",
    ):
        value = raw.get(key)
        if key == "persona_hash" and value is not None:
            value = value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None
        if value is None and key not in raw:
            continue
        if isinstance(value, (str, int, bool)) or value is None:
            safe[key] = value
    field_errors = raw.get("field_errors")
    if isinstance(field_errors, list):
        safe["field_errors"] = [
            {
                "field": str(item.get("field", ""))[:200],
                "type": str(item.get("type", "value_error"))[:100],
            }
            for item in field_errors[:50]
            if isinstance(item, dict)
        ]
    return safe


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
                "details": _safe_error_details(details),
                "retryable": retryable,
            },
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id},
    )


class AccountPersonaRoute(APIRoute):
    """Normalize dependency, validation, domain, and unexpected failures."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            correlation_id = _correlation_id(request)
            try:
                response = await original(request)
            except AccountPersonaError as exc:
                code = str(exc.code)
                return _error_response(
                    status_code=int(getattr(exc, "http_status", exc.status_code)),
                    correlation_id=correlation_id,
                    code=code,
                    message=_ERROR_MESSAGES.get(code, "Persona request failed"),
                    details=exc.details,
                )
            except RequestValidationError as exc:
                field_errors = []
                injection_rejected = False
                for item in exc.errors():
                    error_type = str(item.get("type", "value_error"))
                    injection_rejected = injection_rejected or (
                        error_type == "persona_prompt_injection_rejected"
                    )
                    location = [str(part) for part in item.get("loc", ()) if part != "body"]
                    field_errors.append(
                        {"field": ".".join(location)[:200], "type": error_type[:100]}
                    )
                code = (
                    "PERSONA_PROMPT_INJECTION_REJECTED"
                    if injection_rejected
                    else "PERSONA_VALIDATION_FAILED"
                )
                return _error_response(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    correlation_id=correlation_id,
                    code=code,
                    message=_ERROR_MESSAGES[code],
                    details={"field_errors": field_errors},
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_403_FORBIDDEN:
                    code, message = "ADMIN_REQUIRED", "该操作需要管理员权限"
                elif exc.status_code == status.HTTP_401_UNAUTHORIZED:
                    code, message = "AUTHENTICATION_REQUIRED", "需要登录后访问"
                else:
                    code, message = "HTTP_ERROR", "Request failed"
                return _error_response(
                    status_code=exc.status_code,
                    correlation_id=correlation_id,
                    code=code,
                    message=message,
                )
            except Exception as exc:
                structlog.get_logger().error(
                    "account_persona_api_error",
                    path=request.url.path,
                    correlation_id=correlation_id,
                    error_type=type(exc).__name__,
                )
                return _error_response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    correlation_id=correlation_id,
                    code="INTERNAL_ERROR",
                    message="服务暂时不可用",
                    retryable=True,
                )
            response.headers["X-Correlation-ID"] = correlation_id
            return response

        return handler


router = APIRouter(route_class=AccountPersonaRoute)


class PersonaPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=0)
    persona: PersonaV1


class PersonaResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=0)


def _normalize_preview_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return _PREVIEW_UNSAFE_TEXT_RE.sub("", normalized).strip()


class PersonaPreviewContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: StrictInt = Field(gt=0)
    user_name: StrictStr = Field(max_length=80)
    text: StrictStr = Field(min_length=1, max_length=500)

    @field_validator("user_name", "text", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> Any:
        return _normalize_preview_text(value) if isinstance(value, str) else value


class PersonaPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owned_group_asset_id: StrictInt = Field(gt=0)
    content_category: Literal["community", "promotion"]
    trigger_type: Literal["manual"]
    topic: StrictStr = Field(min_length=1, max_length=100)
    sample_context: tuple[PersonaPreviewContextMessage, ...] = Field(
        default_factory=tuple,
        max_length=20,
    )
    draft_persona: PersonaV1 | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _normalize_topic(cls, value: Any) -> Any:
        return _normalize_preview_text(value) if isinstance(value, str) else value

    @field_validator("sample_context")
    @classmethod
    def _validate_context_total(
        cls,
        value: tuple[PersonaPreviewContextMessage, ...],
    ) -> tuple[PersonaPreviewContextMessage, ...]:
        if sum(len(item.text) for item in value) > 4000:
            raise ValueError("sample_context text exceeds 4000 characters")
        return value


async def require_persona_audit_reader(
    current_user: dict = Depends(get_current_user),
) -> dict:
    if current_user.get("role") not in _AUDIT_READER_ROLES:
        raise AccountPersonaError(
            "PERSONA_AUDIT_READER_REQUIRED",
            _ERROR_MESSAGES["PERSONA_AUDIT_READER_REQUIRED"],
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return current_user


def _success(request: Request, data: Any) -> dict[str, Any]:
    return {
        "data": jsonable_encoder(data),
        "correlation_id": _correlation_id(request),
    }


async def _load_account(
    db: AsyncSession,
    account_id: int,
    *,
    for_update: bool,
) -> TelegramAccount:
    query = (
        select(TelegramAccount)
        .options(
            undefer_group("account_persona"),
            lazyload(TelegramAccount.operation_config),
        )
        .where(TelegramAccount.id == int(account_id))
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    account = await db.scalar(query)
    if account is None:
        raise AccountPersonaError(
            "ACCOUNT_NOT_FOUND",
            _ERROR_MESSAGES["ACCOUNT_NOT_FOUND"],
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # This is deliberately the second lock in every write transaction.
    config_query = select(AccountOperationConfig).where(
        AccountOperationConfig.account_id == int(account_id)
    )
    if for_update:
        config_query = config_query.with_for_update()
    operation_config = await db.scalar(config_query)
    set_committed_value(account, "operation_config", operation_config)
    return account


def _applicability(account: TelegramAccount) -> tuple[bool, str | None, str | None]:
    operation_config = account.__dict__.get("operation_config")
    operation_mode = (
        _enum_value(operation_config.operation_mode) if operation_config is not None else None
    )
    if _enum_value(account.account_type) != AccountType.PROMOTER.value:
        return False, "PERSONA_ACCOUNT_TYPE_UNSUPPORTED", operation_mode
    if operation_config is None:
        return False, "PERSONA_OPERATION_CONFIG_MISSING", None
    if operation_mode != "growth":
        return False, "PERSONA_ACCOUNT_MODE_UNSUPPORTED", operation_mode
    return True, None, operation_mode


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def _safe_hash(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _persona_audit_snapshot(
    *,
    configured: bool,
    revision: int,
    persona_hash: str | None,
    persona: PersonaV1 | dict[str, Any] | None,
    changed_fields: list[str] | None = None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "configured": bool(configured),
        "revision": max(0, int(revision)),
        "persona_hash": _safe_hash(persona_hash),
    }
    system_prompt: Any = None
    if isinstance(persona, PersonaV1):
        system_prompt = persona.system_prompt
        snapshot["schema_version"] = persona.schema_version
    elif isinstance(persona, dict):
        system_prompt = persona.get("system_prompt")
        schema_version = persona.get("schema_version")
        if type(schema_version) is int:
            snapshot["schema_version"] = schema_version
    if isinstance(system_prompt, str):
        snapshot["system_prompt_sha256"] = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        snapshot["system_prompt_length"] = len(system_prompt)
    if changed_fields is not None:
        allowed = set(PersonaV1.model_fields)
        snapshot["changed_fields"] = [field for field in changed_fields if field in allowed]
    return snapshot


def _changed_fields(before: PersonaV1 | None, after: PersonaV1) -> list[str]:
    before_data = before.model_dump(mode="json") if before is not None else {}
    after_data = after.model_dump(mode="json")
    return [name for name, value in after_data.items() if before_data.get(name) != value]


def _json_state(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _actor_summaries(
    db: AsyncSession,
    actor_ids: set[int],
    current_user: dict,
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    current_id = current_user.get("id")
    if current_id is not None and int(current_id) in actor_ids:
        result[int(current_id)] = {
            "id": int(current_id),
            "username": str(current_user.get("username") or "")[:120] or None,
        }
    remaining = sorted(actor_ids - set(result))
    if not remaining:
        return result
    parameters = {f"actor_{index}": actor_id for index, actor_id in enumerate(remaining)}
    placeholders = ", ".join(f":actor_{index}" for index in range(len(remaining)))
    rows = (
        await db.execute(
            text(f"SELECT id, username FROM admin_user WHERE id IN ({placeholders})"),
            parameters,
        )
    ).all()
    for actor_id, username in rows:
        result[int(actor_id)] = {
            "id": int(actor_id),
            "username": str(username or "")[:120] or None,
        }
    for actor_id in remaining:
        result.setdefault(actor_id, {"id": actor_id, "username": None})
    return result


def _account_runtime_blockers(
    account: TelegramAccount,
    operation_config: AccountOperationConfig | None,
) -> list[str]:
    reasons: list[str] = []
    if _enum_value(account.account_type) != AccountType.PROMOTER.value:
        reasons.append("PERSONA_ACCOUNT_TYPE_UNSUPPORTED")
    if not bool(account.is_active):
        reasons.append("ACCOUNT_INACTIVE")
    if not _has_usable_user_session(account):
        reasons.append("ACCOUNT_SESSION_MISSING")
    if _enum_value(account.status) in {AccountStatus.ERROR.value, AccountStatus.BANNED.value}:
        reasons.append("ACCOUNT_STATUS_BLOCKED")
    if _enum_value(account.risk_level) not in {
        AccountRiskLevel.NORMAL.value,
        AccountRiskLevel.WATCH.value,
    }:
        reasons.append("ACCOUNT_RISK_BLOCKED")
    if account.risk_pause_until is not None and account.risk_pause_until > datetime.utcnow():
        reasons.append("ACCOUNT_RISK_PAUSE_ACTIVE")
    if operation_config is None:
        reasons.append("PERSONA_OPERATION_CONFIG_MISSING")
    else:
        if not bool(operation_config.enabled):
            reasons.append("OPERATION_CONFIG_DISABLED")
        if _enum_value(operation_config.operation_mode) != "growth":
            reasons.append("PERSONA_ACCOUNT_MODE_UNSUPPORTED")
    return reasons


async def _preview_targets(
    db: AsyncSession,
    account: TelegramAccount,
) -> list[dict[str, Any]]:
    """Return only exact-account, valid stage-two mappings with an AI category."""

    rows = (
        await db.execute(
            select(
                GroupAccountMessagePolicy,
                OwnedGroupAsset,
                Group,
                ManagedGroupBinding,
                OwnedGroupMembership,
            )
            .join(
                OwnedGroupAsset,
                OwnedGroupAsset.id == GroupAccountMessagePolicy.owned_group_asset_id,
            )
            .join(Group, Group.id == OwnedGroupAsset.core_group_id)
            .join(ManagedGroupBinding, ManagedGroupBinding.id == OwnedGroupAsset.managed_binding_id)
            .join(
                OwnedGroupMembership,
                and_(
                    OwnedGroupMembership.group_asset_id == OwnedGroupAsset.id,
                    OwnedGroupMembership.resource_type == "user",
                    OwnedGroupMembership.resource_id == int(account.id),
                ),
            )
            .where(GroupAccountMessagePolicy.account_id == int(account.id))
            .order_by(GroupAccountMessagePolicy.id)
        )
    ).all()
    messaging_runtime = await get_owned_group_messaging_settings(db)
    operation_config = account.__dict__.get("operation_config")
    common_reasons = _account_runtime_blockers(account, operation_config)
    targets: list[dict[str, Any]] = []
    for policy, asset, group, binding, membership in rows:
        categories: list[str] = []
        if _enum_value(policy.mode) == "ai":
            categories.append("community")
        promotion = policy.promotion_config if isinstance(policy.promotion_config, dict) else {}
        if _enum_value(promotion.get("mode")) == "ai":
            categories.append("promotion")
        if not categories:
            continue

        mapping_valid = (
            _enum_value(asset.status) == "ready"
            and asset.archived_at is None
            and asset.core_group_id is not None
            and asset.telegram_chat_id is not None
            and asset.managed_binding_id is not None
            and int(policy.owned_group_asset_id) == int(asset.id)
            and int(policy.core_group_id) == int(asset.core_group_id)
            and int(group.id) == int(asset.core_group_id)
            and int(group.group_id) == int(asset.telegram_chat_id)
            and int(binding.id) == int(asset.managed_binding_id)
            and int(binding.group_id) == int(asset.core_group_id)
            and int(binding.telegram_group_id) == int(asset.telegram_chat_id)
            and _enum_value(binding.binding_status) == ManagedGroupBindingStatus.ACTIVE.value
            and int(membership.group_asset_id) == int(asset.id)
            and membership.resource_type == "user"
            and int(membership.resource_id) == int(account.id)
        )
        if not mapping_valid:
            continue

        reasons = list(common_reasons)
        if not bool(policy.enabled):
            reasons.append("POLICY_DISABLED")
        if _enum_value(asset.governance_status) != "managed":
            reasons.append("GOVERNANCE_NOT_MANAGED")
        if not bool(settings.OWNED_GROUP_MESSAGING_ENABLED):
            reasons.append("OWNED_GROUP_MESSAGING_DISABLED")
        if not bool(messaging_runtime.get("enabled", False)):
            reasons.append("OWNED_GROUP_MESSAGING_RUNTIME_DISABLED")
        if bool(messaging_runtime.get("dryRun", True)):
            reasons.append("OWNED_GROUP_MESSAGING_DRY_RUN")
        membership_status = _enum_value(membership.status)
        if membership_status not in {
            "member_verified",
            "admin_verified",
            "skipped_already_member",
        }:
            reasons.append("ACCOUNT_MEMBERSHIP_NOT_VERIFIED")
        if membership.telegram_user_id is None:
            reasons.append("ACCOUNT_IDENTITY_UNBOUND")
        if (
            membership.last_verified_at is None
            or membership.last_verified_at < datetime.utcnow() - timedelta(hours=24)
        ):
            reasons.append("ACCOUNT_MEMBERSHIP_STALE")
        reasons = list(dict.fromkeys(reasons))
        targets.append(
            {
                "owned_group_asset_id": int(asset.id),
                "group_name": str(group.title or "")[:255],
                "policy_id": int(policy.id),
                "policy_revision": int(policy.revision),
                "available_categories": categories,
                "governance_status": _enum_value(asset.governance_status),
                "runtime_send_eligible": not reasons,
                "blocking_reasons": reasons,
            }
        )
    return targets


async def _persona_payload(
    db: AsyncSession,
    account: TelegramAccount,
    current_user: dict,
    *,
    state: AccountPersonaState | None = None,
) -> dict[str, Any]:
    applicable, blocking_reason, operation_mode = _applicability(account)
    if state is None:
        try:
            state = AccountPersonaService.get(account)
        except AccountPersonaError as exc:
            if exc.code != "PERSONA_CONFIG_INVALID":
                raise
            revision = account.ai_persona_revision
            raise AccountPersonaError(
                "PERSONA_CONFIG_INVALID",
                _ERROR_MESSAGES["PERSONA_CONFIG_INVALID"],
                details={
                    "current_revision": revision if type(revision) is int else 0,
                    "persona_hash": _safe_hash(account.ai_persona_hash),
                    "repair_action": "reset",
                },
            ) from exc
    feature = await get_owned_group_ai_persona_settings(db)
    actor_map = await _actor_summaries(
        db,
        {int(state.updated_by)} if state.updated_by is not None else set(),
        current_user,
    )
    return {
        "account_id": int(account.id),
        "account_type": _enum_value(account.account_type),
        "operation_mode": operation_mode,
        "persona_applicable": applicable,
        "blocking_reason": blocking_reason,
        "configured": state.configured,
        "revision": state.revision,
        "persona_hash": state.persona_hash,
        "persona": state.persona.model_dump(mode="json") if state.persona is not None else None,
        "updated_at": _iso_utc(state.updated_at),
        "updated_by": actor_map.get(int(state.updated_by))
        if state.updated_by is not None
        else None,
        "feature": {
            "static_enabled": bool(feature.get("staticEnabled", False)),
            "runtime_enabled": bool(feature.get("enabled", False)),
            "effective_enabled": bool(feature.get("effectiveEnabled", False)),
            "runtime_will_apply": bool(feature.get("effectiveEnabled", False)) and applicable,
        },
        "limits": {
            "max_total_bytes": PERSONA_MAX_TOTAL_BYTES,
            "max_system_prompt_chars": 1000,
        },
        "preview_targets": await _preview_targets(db, account),
    }


async def _validated_preview_policy(
    db: AsyncSession,
    account: TelegramAccount,
    payload: PersonaPreviewRequest,
) -> GroupAccountMessagePolicy:
    asset_exists = await db.scalar(
        select(OwnedGroupAsset.id).where(
            OwnedGroupAsset.id == int(payload.owned_group_asset_id)
        )
    )
    if asset_exists is None:
        raise AccountPersonaError(
            "OWNED_GROUP_ASSET_NOT_FOUND",
            _ERROR_MESSAGES["OWNED_GROUP_ASSET_NOT_FOUND"],
            status_code=status.HTTP_404_NOT_FOUND,
        )

    policy = await db.scalar(
        select(GroupAccountMessagePolicy).where(
            GroupAccountMessagePolicy.owned_group_asset_id
            == int(payload.owned_group_asset_id),
            GroupAccountMessagePolicy.account_id == int(account.id),
        )
    )
    if policy is None:
        raise AccountPersonaError(
            "PERSONA_ACCOUNT_MISMATCH",
            _ERROR_MESSAGES["PERSONA_ACCOUNT_MISMATCH"],
        )

    targets = await _preview_targets(db, account)
    target = next(
        (
            item
            for item in targets
            if int(item["owned_group_asset_id"]) == int(payload.owned_group_asset_id)
        ),
        None,
    )
    if target is None or int(target["policy_id"]) != int(policy.id):
        raise AccountPersonaError(
            "PERSONA_ACCOUNT_MISMATCH",
            _ERROR_MESSAGES["PERSONA_ACCOUNT_MISMATCH"],
        )
    if payload.content_category not in target["available_categories"]:
        raise AccountPersonaError(
            "PERSONA_POLICY_MODE_UNSUPPORTED",
            _ERROR_MESSAGES["PERSONA_POLICY_MODE_UNSUPPORTED"],
        )

    allowed_topics = {
        _normalize_preview_text(item).casefold()
        for item in (policy.allowed_topics or ())
        if isinstance(item, str) and _normalize_preview_text(item)
    }
    if payload.topic.casefold() not in allowed_topics:
        raise AccountPersonaError(
            "PERSONA_TOPIC_NOT_ALLOWED",
            _ERROR_MESSAGES["PERSONA_TOPIC_NOT_ALLOWED"],
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return policy


def _validate_preview_persona_topic(
    payload: PersonaPreviewRequest,
    effective: PersonaSnapshotResult,
) -> None:
    normalized_topic = payload.topic.casefold()
    if any(
        _normalize_preview_text(item).casefold() in normalized_topic
        for item in effective.persona.forbidden_topics
        if _normalize_preview_text(item)
    ):
        raise AccountPersonaError(
            "PERSONA_TOPIC_FORBIDDEN",
            _ERROR_MESSAGES["PERSONA_TOPIC_FORBIDDEN"],
            status_code=status.HTTP_400_BAD_REQUEST,
        )


def _preview_audit_snapshot(
    effective: PersonaSnapshotResult,
) -> dict[str, Any]:
    source = _enum_value(effective.source)
    snapshot = _persona_audit_snapshot(
        configured=source == "configured",
        revision=int(effective.revision),
        persona_hash=effective.persona_hash,
        persona=None,
    )
    snapshot["schema_version"] = int(effective.persona.schema_version)
    return snapshot


def _encode_cursor(created_at: datetime, event_id: int) -> str:
    raw = json.dumps(
        [created_at.replace(tzinfo=None).isoformat(timespec="microseconds"), int(event_id)],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError
        created_at = datetime.fromisoformat(payload[0])
        event_id = payload[1]
        if created_at.tzinfo is not None or type(event_id) is not int or event_id <= 0:
            raise ValueError
        return created_at, event_id
    except Exception as exc:
        raise AccountPersonaError(
            "PERSONA_VALIDATION_FAILED",
            _ERROR_MESSAGES["PERSONA_VALIDATION_FAILED"],
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details={"field_errors": [{"field": "cursor", "type": "invalid_cursor"}]},
        ) from exc


def _safe_audit_state(raw: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    if not raw:
        return None, []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None, []
    if not isinstance(value, dict):
        return None, []
    result: dict[str, Any] = {}
    if type(value.get("configured")) is bool:
        result["configured"] = value["configured"]
    if type(value.get("revision")) is int and value["revision"] >= 0:
        result["revision"] = value["revision"]
    persona_hash = value.get("persona_hash")
    if persona_hash is None or _safe_hash(persona_hash) is not None:
        result["persona_hash"] = persona_hash
    if type(value.get("schema_version")) is int:
        result["schema_version"] = value["schema_version"]
    prompt_hash = _safe_hash(value.get("system_prompt_sha256"))
    if prompt_hash is not None:
        result["system_prompt_sha256"] = prompt_hash
    if type(value.get("system_prompt_length")) is int and value["system_prompt_length"] >= 0:
        result["system_prompt_length"] = value["system_prompt_length"]
    allowed_fields = set(PersonaV1.model_fields)
    changed_fields = value.get("changed_fields")
    safe_changed = (
        [item for item in changed_fields if isinstance(item, str) and item in allowed_fields]
        if isinstance(changed_fields, list)
        else []
    )
    return result or None, safe_changed


@router.get("/{account_id}/ai-persona")
async def get_account_persona(
    account_id: int,
    request: Request,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    account = await _load_account(db, account_id, for_update=False)
    return _success(request, await _persona_payload(db, account, current_user))


@router.put("/{account_id}/ai-persona")
async def put_account_persona(
    account_id: int,
    payload: PersonaPutRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    observed_revision = int(payload.expected_revision)
    observed_hash: str | None = None
    try:
        account = await _load_account(db, account_id, for_update=True)
        before = AccountPersonaService.get(account)
        state = await AccountPersonaService.put(
            db,
            account,
            payload.persona,
            expected_revision=payload.expected_revision,
            actor_id=int(current_user["id"]) if current_user.get("id") is not None else None,
        )
        if state.changed:
            if state.persona is None:
                raise AccountPersonaError(
                    "PERSONA_CONFIG_INVALID",
                    _ERROR_MESSAGES["PERSONA_CONFIG_INVALID"],
                )
            changed_fields = _changed_fields(before.persona, state.persona)
            db.add(
                OwnedGroupAuditEvent(
                    event_type=(
                        "account_persona_updated"
                        if before.configured
                        else "account_persona_created"
                    ),
                    group_asset_id=None,
                    resource_type="account_persona",
                    resource_id=int(account.id),
                    actor_id=(
                        int(current_user["id"]) if current_user.get("id") is not None else None
                    ),
                    before_state=_json_state(
                        _persona_audit_snapshot(
                            configured=before.configured,
                            revision=before.revision,
                            persona=before.persona,
                            persona_hash=before.persona_hash,
                        )
                    ),
                    after_state=_json_state(
                        _persona_audit_snapshot(
                            configured=state.configured,
                            revision=state.revision,
                            persona=state.persona,
                            persona_hash=state.persona_hash,
                            changed_fields=changed_fields,
                        )
                    ),
                    result="success",
                    reason_code=None,
                    correlation_id=_correlation_id(request),
                )
            )
            await db.flush()
        await db.commit()
        observed_revision = int(state.revision)
        observed_hash = state.persona_hash
    except Exception as exc:
        await db.rollback()
        record_persona_update(
            account_id=int(account_id),
            revision=observed_revision,
            persona_hash=observed_hash,
            result="failed",
            reason_code=getattr(exc, "code", "INTERNAL_ERROR"),
            request_id=_correlation_id(request),
        )
        raise
    record_persona_update(
        account_id=int(account_id),
        revision=observed_revision,
        persona_hash=observed_hash,
        result="success",
        request_id=_correlation_id(request),
    )
    await refresh_persona_configured_gauge(db)
    return _success(
        request,
        await _persona_payload(db, account, current_user, state=state),
    )


@router.post("/{account_id}/ai-persona/reset")
async def reset_account_persona(
    account_id: int,
    payload: PersonaResetRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    observed_revision = int(payload.expected_revision)
    observed_hash: str | None = None
    try:
        account = await _load_account(db, account_id, for_update=True)
        before_snapshot = _persona_audit_snapshot(
            configured=account.ai_persona is not None,
            revision=(
                account.ai_persona_revision
                if type(account.ai_persona_revision) is int and account.ai_persona_revision >= 0
                else 0
            ),
            persona=account.ai_persona,
            persona_hash=account.ai_persona_hash,
        )
        state = await AccountPersonaService.reset(
            db,
            account,
            expected_revision=payload.expected_revision,
            actor_id=int(current_user["id"]) if current_user.get("id") is not None else None,
        )
        if state.changed:
            changed_fields = list(PersonaV1.model_fields)
            db.add(
                OwnedGroupAuditEvent(
                    event_type="account_persona_reset",
                    group_asset_id=None,
                    resource_type="account_persona",
                    resource_id=int(account.id),
                    actor_id=(
                        int(current_user["id"]) if current_user.get("id") is not None else None
                    ),
                    before_state=_json_state(before_snapshot),
                    after_state=_json_state(
                        _persona_audit_snapshot(
                            configured=False,
                            revision=state.revision,
                            persona=None,
                            persona_hash=None,
                            changed_fields=changed_fields,
                        )
                    ),
                    result="success",
                    reason_code=None,
                    correlation_id=_correlation_id(request),
                )
            )
            await db.flush()
        await db.commit()
        observed_revision = int(state.revision)
    except Exception as exc:
        await db.rollback()
        record_persona_update(
            account_id=int(account_id),
            revision=observed_revision,
            persona_hash=observed_hash,
            result="failed",
            reason_code=getattr(exc, "code", "INTERNAL_ERROR"),
            request_id=_correlation_id(request),
        )
        raise
    record_persona_update(
        account_id=int(account_id),
        revision=observed_revision,
        persona_hash=None,
        result="success",
        request_id=_correlation_id(request),
    )
    await refresh_persona_configured_gauge(db)
    return _success(
        request,
        await _persona_payload(db, account, current_user, state=state),
    )


@router.post("/{account_id}/ai-persona/preview")
async def preview_account_persona(
    account_id: int,
    payload: PersonaPreviewRequest,
    request: Request,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    observation_started = time.perf_counter()
    effective: PersonaSnapshotResult | None = None
    policy: GroupAccountMessagePolicy | None = None
    observed_core_group_id: int | None = None
    try:
        account = await _load_account(db, account_id, for_update=False)
        applicable, blocking_reason, _operation_mode = _applicability(account)
        if not applicable:
            code = blocking_reason or "PERSONA_ACCOUNT_MODE_UNSUPPORTED"
            raise AccountPersonaError(code, _ERROR_MESSAGES[code])

        base_state = AccountPersonaService.get(account)
        effective = (
            AccountPersonaService.snapshot_draft(
                account_id=int(account.id),
                payload=payload.draft_persona,
            )
            if payload.draft_persona is not None
            else AccountPersonaService.snapshot_for_new_execution(
                account,
                feature_enabled=True,
            )
        )
        policy = await _validated_preview_policy(db, account, payload)
        observed_core_group_id = int(policy.core_group_id)
        _validate_preview_persona_topic(payload, effective)

        db.add(
            OwnedGroupAuditEvent(
                event_type="account_persona_preview_failed",
                group_asset_id=int(payload.owned_group_asset_id),
                resource_type="account_persona",
                resource_id=int(account.id),
                actor_id=(
                    int(current_user["id"]) if current_user.get("id") is not None else None
                ),
                before_state=_json_state(
                    _persona_audit_snapshot(
                        configured=base_state.configured,
                        revision=base_state.revision,
                        persona_hash=base_state.persona_hash,
                        persona=None,
                    )
                ),
                after_state=_json_state(_preview_audit_snapshot(effective)),
                result="failed",
                reason_code="AI_PROVIDER_UNSAFE",
                correlation_id=_correlation_id(request),
            )
        )
        await db.flush()
        await db.commit()
    except Exception as exc:
        await db.rollback()
        record_persona_preview(
            account_id=int(account_id),
            asset_id=int(payload.owned_group_asset_id),
            core_group_id=observed_core_group_id,
            content_category=str(payload.content_category),
            persona_source=(
                str(effective.source.value) if effective is not None else "unknown"
            ),
            revision=int(effective.revision) if effective is not None else 0,
            persona_hash=(
                str(effective.persona_hash) if effective is not None else None
            ),
            prompt_template_version=PERSONA_PROMPT_TEMPLATE_VERSION,
            result="failed",
            reason_code=getattr(exc, "code", "INTERNAL_ERROR"),
            duration_ms=int((time.perf_counter() - observation_started) * 1000),
            request_id=_correlation_id(request),
        )
        if getattr(exc, "code", None) == "PERSONA_ACCOUNT_MISMATCH":
            record_persona_account_mismatch(
                account_id=int(account_id),
                persona_source=(
                    str(effective.source.value) if effective is not None else "unknown"
                ),
                revision=int(effective.revision) if effective is not None else 0,
                persona_hash=(
                    str(effective.persona_hash) if effective is not None else None
                ),
                request_id=_correlation_id(request),
            )
        raise

    # The external-provider disclosure required for Persona preview has not
    # been authorized.  Keep the HTTP contract explicit without constructing a
    # Prompt, creating an execution, or touching an LLM/Telegram integration.
    record_persona_preview(
        account_id=int(account_id),
        asset_id=int(payload.owned_group_asset_id),
        core_group_id=observed_core_group_id,
        content_category=str(payload.content_category),
        persona_source=str(effective.source.value),
        revision=int(effective.revision),
        persona_hash=str(effective.persona_hash),
        prompt_template_version=PERSONA_PROMPT_TEMPLATE_VERSION,
        result="failed",
        reason_code="AI_PROVIDER_UNSAFE",
        duration_ms=int((time.perf_counter() - observation_started) * 1000),
        request_id=_correlation_id(request),
    )
    raise AccountPersonaError(
        "AI_PROVIDER_UNSAFE",
        _ERROR_MESSAGES["AI_PROVIDER_UNSAFE"],
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@router.get("/{account_id}/ai-persona/audit-events")
async def list_account_persona_audit_events(
    account_id: int,
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    event_type: PersonaAuditEventType | None = Query(default=None),
    current_user: dict = Depends(require_persona_audit_reader),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    exists = await db.scalar(
        select(TelegramAccount.id).where(TelegramAccount.id == int(account_id))
    )
    if exists is None:
        raise AccountPersonaError(
            "ACCOUNT_NOT_FOUND",
            _ERROR_MESSAGES["ACCOUNT_NOT_FOUND"],
            status_code=status.HTTP_404_NOT_FOUND,
        )
    query = select(OwnedGroupAuditEvent).where(
        OwnedGroupAuditEvent.resource_type == "account_persona",
        OwnedGroupAuditEvent.resource_id == int(account_id),
        OwnedGroupAuditEvent.event_type.in_(PERSONA_AUDIT_EVENT_TYPES),
    )
    if event_type is not None:
        query = query.where(OwnedGroupAuditEvent.event_type == event_type)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                OwnedGroupAuditEvent.created_at < cursor_time,
                and_(
                    OwnedGroupAuditEvent.created_at == cursor_time,
                    OwnedGroupAuditEvent.id < cursor_id,
                ),
            )
        )
    events = list(
        (
            await db.scalars(
                query.order_by(
                    OwnedGroupAuditEvent.created_at.desc(),
                    OwnedGroupAuditEvent.id.desc(),
                ).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(events) > limit
    page = events[:limit]
    actor_ids = {int(item.actor_id) for item in page if item.actor_id is not None}
    actor_map = await _actor_summaries(db, actor_ids, current_user)
    items: list[dict[str, Any]] = []
    reason_pattern = re.compile(r"^[A-Z0-9_]{1,64}$")
    for event in page:
        before, before_changed = _safe_audit_state(event.before_state)
        after, after_changed = _safe_audit_state(event.after_state)
        changed_fields = after_changed or before_changed
        reason_code = (
            event.reason_code
            if isinstance(event.reason_code, str) and reason_pattern.fullmatch(event.reason_code)
            else None
        )
        items.append(
            {
                "id": int(event.id),
                "event_type": str(event.event_type),
                "account_id": int(account_id),
                "asset_id": (
                    int(event.group_asset_id) if event.group_asset_id is not None else None
                ),
                "actor": (
                    actor_map.get(int(event.actor_id)) if event.actor_id is not None else None
                ),
                "created_at": _iso_utc(event.created_at),
                "before": before,
                "after": after,
                "changed_fields": changed_fields,
                "result": (
                    str(event.result) if str(event.result) in {"success", "failed"} else "failed"
                ),
                "reason_code": reason_code,
            }
        )
    next_cursor = (
        _encode_cursor(page[-1].created_at, int(page[-1].id)) if has_more and page else None
    )
    return _success(request, {"items": items, "next_cursor": next_cursor})


__all__ = [
    "PERSONA_AUDIT_EVENT_TYPES",
    "PersonaPreviewContextMessage",
    "PersonaPreviewRequest",
    "PersonaPutRequest",
    "PersonaResetRequest",
    "require_persona_audit_reader",
    "router",
]
