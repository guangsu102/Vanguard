"""HMAC-authenticated inbound event endpoints for Sub2API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.integrations.sub2api.alerts import (
    Sub2APIAlertDeliveryError,
    Sub2APIAlertPayload,
    Sub2APIAlertSignatureError,
    deliver_sub2api_alert,
    expected_sub2api_alert_idempotency_key,
    verify_sub2api_alert_signature,
)
from app.integrations.sub2api.announcements import (
    Sub2APIAnnouncementDeliveryError,
    Sub2APIAnnouncementPayload,
    deliver_sub2api_announcement,
    expected_sub2api_announcement_idempotency_key,
)

router = APIRouter()


@router.post("/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_sub2api_alert(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    secret = str(settings.SUB2API_ALERT_WEBHOOK_SECRET or "").strip()
    if len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sub2API alert webhook secret is not configured",
        )

    body = await request.body()
    if not body or len(body) > 64 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid alert payload size"
        )

    timestamp = request.headers.get("X-Sub2API-Timestamp", "")
    signature = request.headers.get("X-Sub2API-Signature", "")
    try:
        verify_sub2api_alert_signature(body, timestamp, signature, secret)
    except Sub2APIAlertSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Idempotency-Key"
        )

    try:
        payload = Sub2APIAlertPayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()
        ) from exc
    if payload.source.system.strip().lower() != "sub2api":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid alert source"
        )
    if idempotency_key != expected_sub2api_alert_idempotency_key(payload):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key does not match signed payload",
        )

    try:
        result = await deliver_sub2api_alert(payload, idempotency_key, db, redis)
    except Sub2APIAlertDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return {"code": 0, "message": "accepted", "data": result}


@router.post("/announcements", status_code=status.HTTP_202_ACCEPTED)
async def receive_sub2api_announcement(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    secret = str(settings.SUB2API_ALERT_WEBHOOK_SECRET or "").strip()
    if len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sub2API webhook secret is not configured",
        )

    body = await request.body()
    if not body or len(body) > 64 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid announcement payload size",
        )

    timestamp = request.headers.get("X-Sub2API-Timestamp", "")
    signature = request.headers.get("X-Sub2API-Signature", "")
    try:
        verify_sub2api_alert_signature(body, timestamp, signature, secret)
    except Sub2APIAlertSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key or len(idempotency_key) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Idempotency-Key",
        )

    try:
        payload = Sub2APIAnnouncementPayload.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc
    if payload.source.system.strip().lower() != "sub2api":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid announcement source",
        )
    if idempotency_key != expected_sub2api_announcement_idempotency_key(payload):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key does not match signed payload",
        )

    try:
        result = await deliver_sub2api_announcement(payload, idempotency_key, db, redis)
    except Sub2APIAnnouncementDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return {"code": 0, "message": "accepted", "data": result}
