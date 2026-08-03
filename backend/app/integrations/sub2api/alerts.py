"""Signed Sub2API alert intake and configured channel delivery."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.automation_settings import get_app_runtime_settings
from app.core.config import settings
from app.integrations.qq.client import OneBotClient
from app.integrations.telegram.client import get_telegram_client
from app.modules.qq.models import QQBotConnection, QQManagedGroup


class Sub2APIAlertSource(BaseModel):
    model_config = ConfigDict(extra="allow")

    system: str
    instance_id: str
    base_url: str | None = None


class Sub2APIAlertEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(gt=0)
    transition: Literal["firing", "resolved"]
    status: str
    severity: str
    title: str
    description: str = ""
    fired_at: datetime
    resolved_at: datetime | None = None


class Sub2APIAlertRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(gt=0)
    name: str
    window_minutes: int = 1
    sustained_minutes: int = 1


class Sub2APIAlertMetric(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    operator: str
    threshold: float
    value: float | None = None
    unit: str | None = None
    numerator: float | None = None
    denominator: float | None = None


class Sub2APIAlertPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(pattern=r"^1$")
    source: Sub2APIAlertSource
    event: Sub2APIAlertEvent
    rule: Sub2APIAlertRule
    scope: dict[str, Any] = Field(default_factory=dict)
    metric: Sub2APIAlertMetric


class Sub2APIAlertSignatureError(ValueError):
    """Raised when an inbound alert signature cannot be trusted."""


class Sub2APIAlertDeliveryError(RuntimeError):
    """Raised when one or more selected channels cannot accept an alert."""


def verify_sub2api_alert_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    secret: str,
    *,
    now: int | None = None,
    tolerance_seconds: int | None = None,
) -> None:
    try:
        signed_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise Sub2APIAlertSignatureError("invalid webhook timestamp") from exc

    current = int(time.time()) if now is None else int(now)
    tolerance = tolerance_seconds or settings.SUB2API_ALERT_TIMESTAMP_TOLERANCE
    if abs(current - signed_at) > tolerance:
        raise Sub2APIAlertSignatureError("webhook timestamp is outside the accepted window")

    normalized = str(signature or "").strip().lower()
    if normalized.startswith("sha256="):
        normalized = normalized[7:]
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(normalized, expected):
        raise Sub2APIAlertSignatureError("invalid webhook signature")


def format_sub2api_alert_message(payload: Sub2APIAlertPayload) -> str:
    transition = payload.event.transition.strip().lower()
    transition_label = "恢复" if transition == "resolved" else "触发"
    scope = payload.scope or {}
    group_name = str(scope.get("group_name") or "").strip()
    group_id = scope.get("group_id")
    group_label = group_name or (f"分组 #{group_id}" if group_id else "全局")
    platform = str(scope.get("platform") or "").strip()

    metric_names = {
        "group_rate_limit_ratio": "分组限流比例",
        "group_concurrency_usage_ratio": "分组并发占用率",
    }
    metric_name = metric_names.get(payload.metric.type, payload.metric.type)
    value = "无数据" if payload.metric.value is None else f"{payload.metric.value:.2f}"
    threshold = f"{payload.metric.operator} {payload.metric.threshold:.2f}"
    if payload.metric.unit == "percent":
        value += "%"
        threshold += "%"
    if payload.metric.numerator is not None and payload.metric.denominator is not None:
        value = f"{payload.metric.numerator:g}/{payload.metric.denominator:g} = {value}"

    lines = [
        f"[{payload.event.severity}][{transition_label}] {payload.rule.name}",
        f"分组：{group_label}",
    ]
    if platform:
        lines.append(f"平台：{platform}")
    lines.extend(
        [
            f"指标：{metric_name}",
            f"当前：{value}",
            f"阈值：{threshold}，持续 {payload.rule.sustained_minutes} 分钟",
            f"时间：{payload.event.fired_at.isoformat()}",
        ]
    )
    if payload.event.description:
        lines.append(f"详情：{payload.event.description}")
    if payload.source.base_url:
        lines.append(f"查看：{payload.source.base_url.rstrip('/')}/admin/ops")
    return "\n".join(lines)[:1800]


def parse_telegram_chat_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").replace("，", ",").replace("\n", ",").split(",")
    result: list[str] = []
    for item in raw:
        chat_id = str(item or "").strip()
        if chat_id and chat_id not in result:
            result.append(chat_id)
    return result[:20]


def expected_sub2api_alert_idempotency_key(payload: Sub2APIAlertPayload) -> str:
    return f"{payload.source.instance_id.strip()}:{payload.event.id}:{payload.event.transition}"


async def deliver_sub2api_alert(
    payload: Sub2APIAlertPayload,
    idempotency_key: str,
    db: AsyncSession,
    redis: Redis,
) -> dict[str, int]:
    raw_settings = await get_app_runtime_settings(db)
    notification = raw_settings.get("notification")
    notification = notification if isinstance(notification, dict) else {}

    if not bool(notification.get("sub2apiAlertsEnabled", False)):
        return {"sent": 0, "duplicate": 0, "skipped": 1}
    if payload.event.transition == "resolved" and not bool(
        notification.get("sub2apiNotifyResolved", True)
    ):
        return {"sent": 0, "duplicate": 0, "skipped": 1}

    message = format_sub2api_alert_message(payload)
    sent = 0
    duplicate = 0
    skipped = 0
    errors: list[str] = []

    if bool(notification.get("telegramEnabled", False)):
        chat_ids = parse_telegram_chat_ids(notification.get("telegramChatId"))
        if not chat_ids:
            skipped += 1
        elif not settings.BOT_TOKEN:
            errors.append("Telegram Bot Token is not configured")
        else:
            telegram = get_telegram_client()
            for chat_id in chat_ids:
                state = await _acquire_delivery(redis, idempotency_key, "telegram", chat_id)
                if state == "sent":
                    duplicate += 1
                    continue
                if state == "processing":
                    errors.append(f"Telegram {chat_id} delivery is already processing")
                    continue
                try:
                    await telegram.send_message(chat_id, message, parse_mode=None)
                    await _mark_delivery_sent(redis, idempotency_key, "telegram", chat_id)
                    sent += 1
                except Exception as exc:
                    await _release_delivery(redis, idempotency_key, "telegram", chat_id)
                    errors.append(f"Telegram {chat_id}: {exc}")

    if bool(notification.get("qqEnabled", False)):
        account_id = (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
        groups_result = await db.execute(
            select(QQManagedGroup).join(QQBotConnection).where(
                QQBotConnection.app_id == account_id,
                QQManagedGroup.status == "active",
                QQManagedGroup.notifications_enabled.is_(True),
            )
        )
        groups = list(groups_result.scalars().all())
        if not groups:
            skipped += 1
        elif not settings.QQ_ONEBOT_ENABLED:
            errors.append("NapCat OneBot is not enabled")
        else:
            qq = OneBotClient()
            try:
                for group in groups:
                    target = group.group_openid
                    state = await _acquire_delivery(redis, idempotency_key, "qq", target)
                    if state == "sent":
                        duplicate += 1
                        continue
                    if state == "processing":
                        errors.append(f"QQ group {target} delivery is already processing")
                        continue
                    try:
                        await qq.send_group_message(target, message)
                        await _mark_delivery_sent(redis, idempotency_key, "qq", target)
                        sent += 1
                    except Exception as exc:
                        await _release_delivery(redis, idempotency_key, "qq", target)
                        errors.append(f"QQ group {target}: {exc}")
            finally:
                await qq.close()

    if errors:
        raise Sub2APIAlertDeliveryError("; ".join(errors)[:2000])
    return {"sent": sent, "duplicate": duplicate, "skipped": skipped}


def _delivery_redis_key(idempotency_key: str, channel: str, target: str) -> str:
    digest = hashlib.sha256(f"{idempotency_key}:{channel}:{target}".encode()).hexdigest()
    return f"sub2api:alert:delivery:{digest}"


async def _acquire_delivery(
    redis: Redis,
    idempotency_key: str,
    channel: str,
    target: str,
) -> str:
    key = _delivery_redis_key(idempotency_key, channel, target)
    existing = await redis.get(key)
    if existing == "sent":
        return "sent"
    acquired = await redis.set(key, "processing", nx=True, ex=300)
    if acquired:
        return "acquired"
    existing = await redis.get(key)
    return "sent" if existing == "sent" else "processing"


async def _mark_delivery_sent(
    redis: Redis,
    idempotency_key: str,
    channel: str,
    target: str,
) -> None:
    key = _delivery_redis_key(idempotency_key, channel, target)
    await redis.set(
        key,
        "sent",
        ex=settings.SUB2API_ALERT_IDEMPOTENCY_TTL_SECONDS,
    )


async def _release_delivery(
    redis: Redis,
    idempotency_key: str,
    channel: str,
    target: str,
) -> None:
    await redis.delete(_delivery_redis_key(idempotency_key, channel, target))
