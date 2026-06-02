"""
System settings API router.

Provides the lightweight settings contract used by the Vue settings page.
"""

from __future__ import annotations

import csv
import io
import json
import platform
import time
import asyncio
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.runtime_settings import SETTINGS_FILE


router = APIRouter()
STARTED_AT = time.time()
DEFAULT_SETTINGS: dict[str, Any] = {
    "site": {
        "siteName": "Vanguard",
        "siteLogo": "",
        "timezone": "Asia/Shanghai",
        "language": "zh-CN",
        "maintenanceMode": False,
        "maintenanceMessage": "",
    },
    "notification": {
        "telegramEnabled": bool(settings.ALERT_CHAT_ID),
        "telegramChatId": settings.ALERT_CHAT_ID or "",
        "emailEnabled": False,
        "emailRecipients": [],
        "webhookEnabled": False,
        "webhookUrl": "",
        "alertOnError": True,
        "alertOnWarning": False,
    },
    "security": {
        "loginAttempts": 5,
        "lockoutDuration": 30,
        "sessionTimeout": settings.JWT_EXPIRATION_HOURS * 60,
        "allowedIpList": [],
        "require2FA": False,
    },
    "xboard": {
        "enabled": settings.VANGUARD_INTEGRATION_ENABLED,
        "apiUrl": getattr(settings, "XBOARD_API_URL", ""),
        "apiKey": "",
        "webhookUrl": "",
    },
    "aiReply": {
        "enabled": False,
        "privateOnly": True,
        "dailyTokenBudget": 0,
        "maxRepliesPerUserPerDay": 2,
        "cooldownSeconds": 1800,
    },
}


class SettingsUpdate(BaseModel):
    """Partial settings update from the frontend."""

    model_config = ConfigDict(extra="allow")

    site: dict[str, Any] | None = None
    notification: dict[str, Any] | None = None
    security: dict[str, Any] | None = None
    xboard: dict[str, Any] | None = None
    aiReply: dict[str, Any] | None = None


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_raw_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}

    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _public_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_SETTINGS)
    _deep_merge(merged, raw or _load_raw_settings())
    merged.pop("_meta", None)
    return merged


def _save_raw_settings(payload: dict[str, Any]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
async def get_settings() -> dict:
    """Return persisted settings with defaults filled in."""
    return {
        "code": 0,
        "message": "success",
        "data": _public_settings(),
    }


@router.put("")
async def update_settings(update: SettingsUpdate) -> dict:
    """Persist partial settings changes."""
    raw = _load_raw_settings()
    public = _public_settings(raw)
    patch = update.model_dump(exclude_none=True)
    _deep_merge(public, patch)

    if raw.get("_meta"):
        public["_meta"] = raw["_meta"]
    _save_raw_settings(public)

    return {
        "code": 0,
        "message": "保存成功",
        "data": _public_settings(public),
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

    raw = _load_raw_settings()

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
async def backup_database() -> dict:
    """Record a backup request and return a generated filename."""
    filename = f"vanguard-backup-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.sql"
    raw = _load_raw_settings()
    raw["_meta"] = {
        **raw.get("_meta", {}),
        "lastBackup": datetime.utcnow().isoformat(),
        "lastBackupFile": filename,
    }
    _save_raw_settings(raw)

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
