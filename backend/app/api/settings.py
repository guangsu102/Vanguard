"""
System settings API router.

Provides the lightweight settings contract used by the Vue settings page.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import platform
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.automation_settings import (
    DEFAULT_AI_REPLY_SETTINGS,
    DEFAULT_NOTIFICATION_SETTINGS,
    OwnedGroupAiPersonaFeatureError,
    get_app_runtime_settings,
    mutate_owned_group_ai_persona_feature,
    patch_app_runtime_settings,
    with_owned_group_ai_persona_effective_state,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.security import require_admin
from app.core.runtime_settings import (
    DEFAULT_GROUP_AI_INTERACTION_SETTINGS,
    DEFAULT_OWNED_GROUP_AI_PERSONA_SETTINGS,
    DEFAULT_OWNED_GROUP_MESSAGING_SETTINGS,
    DEFAULT_PRIVATE_REPLY_TEMPLATES,
)
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent

router = APIRouter()
STARTED_AT = time.time()
DEFAULT_SETTINGS: dict[str, Any] = {
    "notification": DEFAULT_NOTIFICATION_SETTINGS,
    "xboard": {
        "enabled": settings.VANGUARD_INTEGRATION_ENABLED,
        "callbackEnabled": settings.VANGUARD_CALLBACK_ENABLED,
        "protocol": "hmac",
        "source": "environment",
    },
    "aiReply": DEFAULT_AI_REPLY_SETTINGS,
    "groupAiInteraction": DEFAULT_GROUP_AI_INTERACTION_SETTINGS,
    "ownedGroupMessaging": DEFAULT_OWNED_GROUP_MESSAGING_SETTINGS,
    "ownedGroupAiPersona": DEFAULT_OWNED_GROUP_AI_PERSONA_SETTINGS,
    "keywordPrivateReply": {
        "enabled": False,
    },
    "privateMessaging": {
        "autoReplyEnabled": False,
        "inboundRepliesEnabled": False,
        "manualReplyEnabled": True,
        "proactiveEnabled": False,
        "templates": DEFAULT_PRIVATE_REPLY_TEMPLATES,
    },
}


class SettingsUpdate(BaseModel):
    """Partial settings update from the frontend."""

    model_config = ConfigDict(extra="ignore")

    notification: dict[str, Any] | None = None
    xboard: dict[str, Any] | None = None
    aiReply: dict[str, Any] | None = None
    groupAiInteraction: dict[str, Any] | None = None
    ownedGroupMessaging: dict[str, Any] | None = None
    keywordPrivateReply: dict[str, Any] | None = None
    privateMessaging: dict[str, Any] | None = None


class OwnedGroupAiPersonaFeatureUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=0)
    enabled: bool


_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _settings_correlation_id(request: Request) -> str:
    supplied = request.headers.get("X-Correlation-ID", "")
    if _SAFE_CORRELATION_ID.fullmatch(supplied):
        return supplied
    return f"persona-setting-{uuid.uuid4()}"


def _persona_feature_error_response(
    exc: OwnedGroupAiPersonaFeatureError,
    *,
    correlation_id: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "retryable": exc.http_status >= 500,
            },
            "correlation_id": correlation_id,
        },
        headers={"X-Correlation-ID": correlation_id},
    )


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _environment_xboard_settings() -> dict[str, Any]:
    return {
        "enabled": settings.VANGUARD_INTEGRATION_ENABLED,
        "callbackEnabled": settings.VANGUARD_CALLBACK_ENABLED,
        "protocol": "hmac",
        "source": "environment",
    }


def _public_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_SETTINGS)
    _deep_merge(merged, raw or {})
    merged["xboard"] = _environment_xboard_settings()
    merged["ownedGroupAiPersona"] = with_owned_group_ai_persona_effective_state(
        merged.get("ownedGroupAiPersona")
    )
    merged.pop("_meta", None)
    return merged


def _format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    if days:
        return f"{days}天 {hours}小时 {minutes}分钟"
    if hours:
        return f"{hours}小时 {minutes}分钟"
    return f"{minutes}分钟"


@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db)) -> dict:
    """Return persisted settings with defaults filled in."""
    raw = await get_app_runtime_settings(db)
    return {
        "code": 0,
        "message": "success",
        "data": _public_settings(raw),
    }


@router.put("")
async def update_settings(
    update: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
) -> dict:
    """Persist partial settings changes."""
    patch = update.model_dump(exclude_none=True)
    patch.pop("xboard", None)
    saved = await patch_app_runtime_settings(db, patch)

    return {
        "code": 0,
        "message": "保存成功",
        "data": _public_settings(saved),
    }


@router.put("/owned-group-ai-persona", response_model=None)
async def update_owned_group_ai_persona_feature(
    update: OwnedGroupAiPersonaFeatureUpdate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
) -> Any:
    """Revisioned, audited update for the account-Persona runtime switch."""

    correlation_id = _settings_correlation_id(request)
    try:
        mutation = await mutate_owned_group_ai_persona_feature(
            db,
            expected_revision=update.expected_revision,
            enabled=update.enabled,
            actor_id=int(current_user["id"]) if current_user.get("id") is not None else None,
        )
        if mutation.changed:
            before = {
                "enabled": bool(mutation.before["enabled"]),
                "revision": int(mutation.before["revision"]),
            }
            after = {
                "enabled": bool(mutation.value["enabled"]),
                "revision": int(mutation.value["revision"]),
            }
            db.add(
                OwnedGroupAuditEvent(
                    event_type="account_persona_feature_updated",
                    resource_type="persona_setting",
                    resource_id=None,
                    actor_id=(
                        int(current_user["id"])
                        if current_user.get("id") is not None
                        else None
                    ),
                    before_state=json.dumps(before, ensure_ascii=False, sort_keys=True),
                    after_state=json.dumps(after, ensure_ascii=False, sort_keys=True),
                    result="success",
                    correlation_id=correlation_id,
                )
            )
        await db.commit()
    except OwnedGroupAiPersonaFeatureError as exc:
        await db.rollback()
        return _persona_feature_error_response(exc, correlation_id=correlation_id)
    except Exception:
        await db.rollback()
        raise

    response.headers["X-Correlation-ID"] = correlation_id
    return {
        "data": mutation.value,
        "correlation_id": correlation_id,
    }


@router.get("/system")
async def get_system_info(db: AsyncSession = Depends(get_db)) -> dict:
    """Return basic runtime and dependency health information."""
    database_status = "connected"
    redis_status = "connected"

    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=1.0)
    except Exception:
        database_status = "error"

    try:
        redis = await asyncio.wait_for(get_redis(), timeout=1.0)
        await asyncio.wait_for(redis.ping(), timeout=1.0)
    except Exception:
        redis_status = "error"

    raw = await get_app_runtime_settings(db)

    return {
        "code": 0,
        "message": "success",
        "data": {
            "version": "1.0.0",
            "pythonVersion": platform.python_version(),
            "database": database_status,
            "redis": redis_status,
            "uptime": _format_uptime(time.time() - STARTED_AT),
            "lastBackup": raw.get("_meta", {}).get("lastBackup"),
        },
    }


@router.get("/logs")
async def get_logs(page: int = 1, pageSize: int = 20) -> dict:
    """Return operation logs.

    Persistent operation logging is not wired yet, so this endpoint returns a
    stable empty page instead of breaking the settings screen.
    """
    _ = (page, pageSize)
    return {
        "code": 0,
        "message": "success",
        "data": {
            "list": [],
            "total": 0,
        },
    }


@router.get("/logs/export")
async def export_logs() -> Response:
    """Export operation logs as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "user", "action", "target", "ip", "timestamp", "status", "details"])

    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="vanguard-operation-logs.csv"'},
    )


@router.post("/logs/clear")
async def clear_logs() -> dict:
    """Clear operation logs."""
    return {
        "code": 0,
        "message": "日志已清空",
        "data": None,
    }


@router.post("/backup")
async def backup_database(db: AsyncSession = Depends(get_db)) -> dict:
    """Record a backup request and return a generated filename."""
    filename = f"vanguard-backup-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.sql"
    await patch_app_runtime_settings(
        db,
        {"_meta": {
        "lastBackup": datetime.utcnow().isoformat(),
        "lastBackupFile": filename,
        }},
    )

    return {
        "code": 0,
        "message": "备份任务已创建",
        "data": {"filename": filename},
    }


@router.post("/restart")
async def restart_service() -> dict:
    """Acknowledge restart requests without restarting the container."""
    return {
        "code": 0,
        "message": "重启请求已接收",
        "data": None,
    }
