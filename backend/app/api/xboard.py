"""
XBoard integration API endpoints.

The current XBoard source is the protocol authority: HMAC signed /api/v1/...
requests and AcquisitionTracking as Vanguard's Telegram attribution source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campaign.models import CampaignTracking
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import RedisCache
from app.core.user.models import User, UserState
from app.integrations.xboard.models import XBoardCallback, XBoardEvent
from app.modules.acquisition.models import AcquisitionTracking

router = APIRouter()

SUPPORTED_EVENT_TYPES = {
    "user.lead_created",
    "tracking.clicked",
    "tracking.converted",
    "user.registered",
    "user.activated",
    "coupon.issued",
    "coupon.failed",
}

EVENT_TO_USER_STATE = {
    "user.registered": UserState.PENDING,
    "user.activated": UserState.ACTIVE,
}

SUPPORTED_COUPON_STATUSES = {"issued", "failed"}
MUTATING_PATHS = {"/api/v1/events/ingest", "/api/v1/coupons/report", "/api/v1/xboard/callback/status"}
_REQUEST_ID_FALLBACK: dict[str, float] = {}


class EventIngestRequest(BaseModel):
    event_id: str
    trace_id: str
    event_type: str
    tracking_code: Optional[str] = None
    tg_user_id: Optional[int] = None
    tg_group_id: Optional[int] = None
    external_user_id: Optional[str] = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if value not in SUPPORTED_EVENT_TYPES:
            raise ValueError("unsupported event_type")
        return value


class CouponReportRequest(BaseModel):
    event_id: str
    trace_id: str
    tg_user_id: Optional[int] = None
    external_user_id: Optional[str] = None
    tracking_code: Optional[str] = None
    coupon_code: str
    coupon_status: str
    issued_at: Optional[datetime] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("coupon_status")
    @classmethod
    def validate_coupon_status(cls, value: str) -> str:
        if value not in SUPPORTED_COUPON_STATUSES:
            raise ValueError("unsupported coupon_status")
        return value


class CallbackRequest(BaseModel):
    callback_id: str
    trace_id: str
    event_type: str
    tg_user_id: Optional[int] = None
    external_user_id: Optional[str] = None
    status: Optional[str] = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


def _parse_signed_payload(model: type[BaseModel], raw_body: str) -> BaseModel:
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise _error(4002, "invalid json", status_code=422) from exc

    trace_id = data.get("trace_id") if isinstance(data, dict) else None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise _error(4002, "invalid parameters", trace_id=trace_id, status_code=422) from exc


def _response(data: Any = None, trace_id: Optional[str] = None) -> dict[str, Any]:
    return {"code": 0, "message": "ok", "data": data, "trace_id": trace_id}


def _error(code: int, message: str, trace_id: Optional[str] = None, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, "data": None, "trace_id": trace_id})


async def _consume_request_id(cache_key: str) -> bool:
    cache = RedisCache()
    if cache.client is not None:
        if await cache.exists(cache_key):
            return False
        await cache.set(cache_key, "1", ttl=settings.VANGUARD_TIMESTAMP_TOLERANCE)
        return True

    now = time.time()
    expired_keys = [key for key, expires_at in _REQUEST_ID_FALLBACK.items() if expires_at <= now]
    for key in expired_keys:
        _REQUEST_ID_FALLBACK.pop(key, None)
    if cache_key in _REQUEST_ID_FALLBACK:
        return False
    _REQUEST_ID_FALLBACK[cache_key] = now + settings.VANGUARD_TIMESTAMP_TOLERANCE
    return True


async def _validate_inbound_headers(
    request: Request,
    x_app_id: str | None,
    x_timestamp: str | None,
    x_request_id: str | None,
    x_signature: str | None,
    *,
    expected_app_id: str,
    signing_secret: str,
    consume_request_id: bool,
) -> tuple[str, str, str]:
    if not settings.VANGUARD_INTEGRATION_ENABLED:
        raise _error(4004, "xboard integration disabled")

    if not all([x_app_id, x_timestamp, x_request_id, x_signature]):
        raise _error(4002, "header missing")

    if x_app_id != expected_app_id:
        raise _error(4001, "invalid app id")

    try:
        timestamp_ms = int(x_timestamp)
    except ValueError as exc:
        raise _error(4002, "invalid timestamp") from exc

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    if abs(now_ms - timestamp_ms) > settings.VANGUARD_TIMESTAMP_TOLERANCE * 1000:
        raise _error(4003, "timestamp expired")

    body = await request.body()
    raw_body = body.decode("utf-8") if body else ""
    query_string = request.url.query
    signing_string = "\n".join([request.method.upper(), request.url.path, query_string, x_timestamp, x_request_id, raw_body])
    expected = hmac.new(signing_secret.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise _error(4001, "invalid signature")

    if consume_request_id:
        cache_key = f"xboard:request_id:{x_app_id}:{x_request_id}"
        if not await _consume_request_id(cache_key):
            raise _error(4005, "duplicate request id", status_code=409)

    return x_timestamp, x_request_id, raw_body


def _source_from_payload(payload: dict[str, Any]) -> Optional[str]:
    return payload.get("source") or payload.get("channel")


def _utc_naive(value: datetime) -> datetime:
    """Store API datetimes consistently in UTC-naive DB columns."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _optional_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    return _utc_naive(value) if value is not None else None


async def _get_or_create_user(db: AsyncSession, tg_user_id: Optional[int], payload: dict[str, Any] | None = None) -> Optional[User]:
    if tg_user_id is None:
        return None
    result = await db.execute(select(User).where(User.telegram_id == tg_user_id))
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        telegram_id=tg_user_id,
        username=(payload or {}).get("username"),
        state=UserState.NEW,
    )
    db.add(user)
    await db.flush()
    return user


async def _get_acquisition_tracking(db: AsyncSession, tracking_code: Optional[str]) -> Optional[AcquisitionTracking]:
    if not tracking_code:
        return None
    result = await db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == tracking_code))
    return result.scalar_one_or_none()


async def _get_or_create_acquisition_tracking(
    db: AsyncSession,
    tracking_code: Optional[str],
    user: Optional[User],
    event: EventIngestRequest | CouponReportRequest,
) -> Optional[AcquisitionTracking]:
    if not tracking_code:
        return None
    tracking = await _get_acquisition_tracking(db, tracking_code)
    if tracking is None:
        tracking = AcquisitionTracking(
            tracking_code=tracking_code,
            user_id=user.id if user else None,
            source_type=_source_from_payload(event.payload),
            campaign_name=event.payload.get("campaign_name"),
            group_id=getattr(event, "tg_group_id", None),
            keyword=event.payload.get("keyword"),
            bot_id=event.payload.get("bot_id"),
            external_user_id=event.external_user_id,
        )
        db.add(tracking)
        await db.flush()
    else:
        if user is not None and tracking.user_id is None:
            tracking.user_id = user.id
        tracking.source_type = tracking.source_type or _source_from_payload(event.payload)
        tracking.campaign_name = tracking.campaign_name or event.payload.get("campaign_name")
        tracking.group_id = tracking.group_id or getattr(event, "tg_group_id", None)
        tracking.keyword = tracking.keyword or event.payload.get("keyword")
        tracking.bot_id = tracking.bot_id or event.payload.get("bot_id")
        tracking.external_user_id = tracking.external_user_id or event.external_user_id
    return tracking


async def _get_campaign_tracking_by_code(db: AsyncSession, tracking_code: Optional[str]) -> Optional[CampaignTracking]:
    if not tracking_code:
        return None
    result = await db.execute(select(CampaignTracking).where(CampaignTracking.keyword == tracking_code))
    return result.scalar_one_or_none()


async def _ensure_campaign_tracking(
    db: AsyncSession,
    tracking_code: Optional[str],
    user: Optional[User],
    event_payload: dict[str, Any],
    *,
    group_id: Optional[int] = None,
) -> Optional[CampaignTracking]:
    if not tracking_code or user is None:
        return None
    tracking = await _get_campaign_tracking_by_code(db, tracking_code)
    if tracking is None:
        tracking = CampaignTracking(
            user_id=user.id,
            campaign_name=event_payload.get("campaign_name"),
            source=_source_from_payload(event_payload),
            group_id=group_id,
            keyword=tracking_code,
            bot_id=event_payload.get("bot_id"),
        )
        db.add(tracking)
        await db.flush()
    elif tracking.user_id is None:
        tracking.user_id = user.id
    return tracking


def _apply_user_state(user: Optional[User], event_type: str, external_user_id: Optional[str], payload: dict[str, Any]) -> None:
    if user is None:
        return
    if user.state == UserState.BLOCKED:
        return
    new_state = EVENT_TO_USER_STATE.get(event_type)
    if new_state:
        if not (new_state == UserState.PENDING and user.state == UserState.ACTIVE):
            user.state = new_state
    if event_type == "tracking.converted":
        user.state = UserState.ACTIVE
    xboard_user_id = payload.get("xboard_user_id") or external_user_id
    if xboard_user_id is not None and str(xboard_user_id).isdigit():
        user.xboard_user_id = int(xboard_user_id)


def _apply_acquisition_event(tracking: Optional[AcquisitionTracking], payload: EventIngestRequest) -> None:
    if tracking is None:
        return
    occurred_at = _utc_naive(payload.occurred_at)
    tracking.last_event_at = occurred_at
    if payload.event_type in {"user.lead_created", "tracking.clicked"}:
        tracking.click_at = tracking.click_at or occurred_at
    elif payload.event_type == "user.registered":
        tracking.registered_at = tracking.registered_at or occurred_at
    elif payload.event_type in {"tracking.converted", "user.activated"}:
        tracking.registered_at = tracking.registered_at or occurred_at
        tracking.converted_at = tracking.converted_at or occurred_at
        tracking.converted = True
    elif payload.event_type == "coupon.issued":
        tracking.coupon_status = "issued"
        tracking.coupon_code = payload.payload.get("coupon_code") or tracking.coupon_code
    elif payload.event_type == "coupon.failed":
        tracking.coupon_status = "failed"
    if payload.payload.get("coupon_status") in SUPPORTED_COUPON_STATUSES:
        tracking.coupon_status = payload.payload["coupon_status"]
    if payload.payload.get("trial_granted") is True:
        tracking.trial_granted = True


def _apply_campaign_event(tracking: Optional[CampaignTracking], payload: EventIngestRequest) -> None:
    if tracking is None:
        return
    occurred_at = _utc_naive(payload.occurred_at)
    if payload.event_type == "user.registered":
        tracking.registered_at = tracking.registered_at or occurred_at
    elif payload.event_type in {"tracking.converted", "user.activated"}:
        tracking.converted_at = tracking.converted_at or occurred_at
        tracking.validity_started_at = tracking.validity_started_at or occurred_at
    if payload.payload.get("coupon_status") == "issued" or payload.event_type == "coupon.issued":
        tracking.coupon_granted = True
    if payload.payload.get("trial_granted") is True:
        tracking.trial_granted = True


async def _apply_callback_tracking(db: AsyncSession, payload: CallbackRequest, user: Optional[User]) -> None:
    tracking_code = payload.payload.get("tracking_code")
    tracking = await _get_acquisition_tracking(db, tracking_code)
    if tracking is None and payload.external_user_id:
        result = await db.execute(select(AcquisitionTracking).where(AcquisitionTracking.external_user_id == payload.external_user_id))
        tracking = result.scalar_one_or_none()
    if tracking is not None:
        occurred_at = _utc_naive(payload.occurred_at)
        if user is not None and tracking.user_id is None:
            tracking.user_id = user.id
        tracking.external_user_id = tracking.external_user_id or payload.external_user_id
        tracking.last_event_at = occurred_at
        if payload.event_type == "user.registered":
            tracking.registered_at = tracking.registered_at or occurred_at
        elif payload.event_type == "user.activated":
            tracking.converted_at = tracking.converted_at or occurred_at
            tracking.converted = True


def _apply_acquisition_status(data: dict[str, Any], tracking: AcquisitionTracking) -> None:
    data["tracking_code"] = tracking.tracking_code
    data["external_user_id"] = tracking.external_user_id or data["external_user_id"]
    data["registered"] = tracking.registered_at is not None
    data["activated"] = tracking.converted
    data["coupon_status"] = tracking.coupon_status
    data["last_event_at"] = (
        tracking.last_event_at.isoformat()
        if tracking.last_event_at
        else tracking.converted_at.isoformat()
        if tracking.converted_at
        else tracking.registered_at.isoformat()
        if tracking.registered_at
        else None
    )


@router.post("/events/ingest")
async def ingest_event(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict[str, Any]:
    _, _, raw_body = await _validate_inbound_headers(
        request,
        x_app_id,
        x_timestamp,
        x_request_id,
        x_signature,
        expected_app_id=settings.VANGUARD_APP_ID,
        signing_secret=settings.VANGUARD_SIGNING_SECRET,
        consume_request_id=request.url.path in MUTATING_PATHS,
    )
    payload = _parse_signed_payload(EventIngestRequest, raw_body)

    existing = await db.execute(select(XBoardEvent).where(XBoardEvent.event_id == payload.event_id))
    event = existing.scalar_one_or_none()
    if event:
        return _response({"accepted": True, "idempotent": True, "xboard_event_id": event.event_id}, trace_id=payload.trace_id)

    db.add(
        XBoardEvent(
            event_id=payload.event_id,
            trace_id=payload.trace_id,
            event_type=payload.event_type,
            request_body=raw_body,
            accepted=True,
        )
    )

    user = await _get_or_create_user(db, payload.tg_user_id, payload.payload)
    acquisition_tracking = await _get_or_create_acquisition_tracking(db, payload.tracking_code, user, payload)
    campaign_tracking: CampaignTracking | None = None
    if payload.event_type in {"coupon.issued", "coupon.failed"}:
        campaign_tracking = await _ensure_campaign_tracking(db, payload.tracking_code, user, payload.payload, group_id=payload.tg_group_id)

    _apply_user_state(user, payload.event_type, payload.external_user_id, payload.payload)
    _apply_acquisition_event(acquisition_tracking, payload)
    _apply_campaign_event(campaign_tracking, payload)

    campaign_results = []
    if payload.event_type == "user.registered" and user is not None:
        from app.core.campaign.runner import CampaignRunner

        runner = CampaignRunner(db)
        executions = await runner.trigger_for_registration(
            user,
            occurred_at=_utc_naive(payload.occurred_at),
            metadata={
                "source": _source_from_payload(payload.payload) or "xboard:user.registered",
                "tracking_code": payload.tracking_code,
                "keyword": payload.payload.get("keyword"),
                "bot_id": payload.payload.get("bot_id"),
                "external_user_id": payload.external_user_id,
            },
        )
        campaign_results = [asdict(item) for item in executions]

    return _response(
        {
            "accepted": True,
            "idempotent": False,
            "xboard_event_id": payload.event_id,
            "campaign_results": campaign_results,
        },
        trace_id=payload.trace_id,
    )


@router.get("/users/status")
async def user_status(
    request: Request,
    tg_user_id: Optional[int] = None,
    tracking_code: Optional[str] = None,
    external_user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict[str, Any]:
    await _validate_inbound_headers(
        request,
        x_app_id,
        x_timestamp,
        x_request_id,
        x_signature,
        expected_app_id=settings.VANGUARD_APP_ID,
        signing_secret=settings.VANGUARD_SIGNING_SECRET,
        consume_request_id=True,
    )

    if not any([tg_user_id, tracking_code, external_user_id]):
        raise _error(4002, "at least one query parameter required", trace_id=trace_id)

    data: dict[str, Any] = {
        "tg_user_id": tg_user_id,
        "external_user_id": external_user_id,
        "tracking_code": tracking_code,
        "registered": False,
        "activated": False,
        "coupon_status": None,
        "last_event_at": None,
        "xboard_user_id": None,
        "order_status": None,
    }

    user: Optional[User] = None
    if tg_user_id is not None:
        result = await db.execute(select(User).where(User.telegram_id == tg_user_id))
        user = result.scalar_one_or_none()

    acquisition_tracking: Optional[AcquisitionTracking] = None
    if tracking_code:
        acquisition_tracking = await _get_acquisition_tracking(db, tracking_code)
        if acquisition_tracking is None:
            campaign_tracking = await _get_campaign_tracking_by_code(db, tracking_code)
            if campaign_tracking:
                data["tracking_code"] = campaign_tracking.keyword
                data["registered"] = campaign_tracking.registered_at is not None
                data["activated"] = campaign_tracking.converted_at is not None
                data["coupon_status"] = "issued" if campaign_tracking.coupon_granted else None
                data["last_event_at"] = campaign_tracking.converted_at.isoformat() if campaign_tracking.converted_at else None
                if campaign_tracking.user_id and user is None:
                    user = await db.get(User, campaign_tracking.user_id)

    if acquisition_tracking is not None:
        _apply_acquisition_status(data, acquisition_tracking)
        if acquisition_tracking.user_id and user is None:
            user = await db.get(User, acquisition_tracking.user_id)

    if external_user_id and user is None:
        if external_user_id.isdigit():
            result = await db.execute(select(User).where(User.xboard_user_id == int(external_user_id)))
            user = result.scalar_one_or_none()

    if external_user_id and acquisition_tracking is None:
        result = await db.execute(select(AcquisitionTracking).where(AcquisitionTracking.external_user_id == external_user_id))
        tracking_by_external = result.scalar_one_or_none()
        if tracking_by_external:
            _apply_acquisition_status(data, tracking_by_external)
            if tracking_by_external.user_id and user is None:
                user = await db.get(User, tracking_by_external.user_id)

    if user is not None:
        data.update(
            {
                "tg_user_id": user.telegram_id,
                "registered": True if user.state in {UserState.PENDING, UserState.ACTIVE} else data["registered"],
                "activated": user.state == UserState.ACTIVE or data["activated"],
                "xboard_user_id": user.xboard_user_id,
                "external_user_id": str(user.xboard_user_id) if user.xboard_user_id is not None else data["external_user_id"],
                "order_status": 3 if user.state == UserState.ACTIVE else 0,
            }
        )
        if user.trial_started_at:
            data["last_event_at"] = data["last_event_at"] or user.trial_started_at.isoformat()

    return _response(data, trace_id=trace_id)


@router.post("/coupons/report")
async def report_coupon(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict[str, Any]:
    _, _, raw_body = await _validate_inbound_headers(
        request,
        x_app_id,
        x_timestamp,
        x_request_id,
        x_signature,
        expected_app_id=settings.VANGUARD_APP_ID,
        signing_secret=settings.VANGUARD_SIGNING_SECRET,
        consume_request_id=request.url.path in MUTATING_PATHS,
    )
    payload = _parse_signed_payload(CouponReportRequest, raw_body)

    existing = await db.execute(select(XBoardEvent).where(XBoardEvent.event_id == payload.event_id))
    event = existing.scalar_one_or_none()
    if event:
        return _response({"accepted": True, "idempotent": True, "xboard_event_id": event.event_id}, trace_id=payload.trace_id)

    db.add(
        XBoardEvent(
            event_id=payload.event_id,
            trace_id=payload.trace_id,
            event_type=f"coupon.{payload.coupon_status}",
            request_body=raw_body,
            accepted=True,
        )
    )

    user = await _get_or_create_user(db, payload.tg_user_id, payload.payload)
    if user and payload.coupon_status == "issued":
        user.state = UserState.ACTIVE if user.state != UserState.BLOCKED else user.state
        if payload.external_user_id and payload.external_user_id.isdigit():
            user.xboard_user_id = int(payload.external_user_id)

    acquisition_tracking = await _get_or_create_acquisition_tracking(db, payload.tracking_code, user, payload)
    if acquisition_tracking is not None:
        acquisition_tracking.coupon_status = payload.coupon_status
        acquisition_tracking.coupon_code = payload.coupon_code
        acquisition_tracking.external_user_id = acquisition_tracking.external_user_id or payload.external_user_id
        coupon_time = _optional_utc_naive(payload.issued_at) or datetime.utcnow()
        acquisition_tracking.last_event_at = coupon_time
        if payload.coupon_status == "issued":
            acquisition_tracking.converted = True
            acquisition_tracking.converted_at = acquisition_tracking.converted_at or coupon_time

    campaign_tracking = await _ensure_campaign_tracking(db, payload.tracking_code, user, payload.payload)
    if campaign_tracking and payload.coupon_status == "issued":
        coupon_time = _optional_utc_naive(payload.issued_at) or datetime.utcnow()
        campaign_tracking.coupon_granted = True
        campaign_tracking.converted_at = campaign_tracking.converted_at or coupon_time
        campaign_tracking.validity_started_at = campaign_tracking.validity_started_at or coupon_time

    return _response({"accepted": True, "idempotent": False, "xboard_event_id": payload.event_id}, trace_id=payload.trace_id)


@router.post("/xboard/callback/status")
async def xboard_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_app_id: str | None = Header(default=None, alias="X-App-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict[str, Any]:
    if not settings.VANGUARD_CALLBACK_ENABLED:
        raise _error(4004, "xboard callback disabled")

    _, _, raw_body = await _validate_inbound_headers(
        request,
        x_app_id,
        x_timestamp,
        x_request_id,
        x_signature,
        expected_app_id=settings.VANGUARD_CALLBACK_APP_ID,
        signing_secret=settings.VANGUARD_CALLBACK_SIGNING_SECRET,
        consume_request_id=request.url.path in MUTATING_PATHS,
    )
    payload = _parse_signed_payload(CallbackRequest, raw_body)

    existing = await db.execute(select(XBoardCallback).where(XBoardCallback.callback_id == payload.callback_id))
    callback = existing.scalar_one_or_none()
    if callback:
        return _response({"accepted": True, "idempotent": True, "callback_id": callback.callback_id}, trace_id=payload.trace_id)

    db.add(
        XBoardCallback(
            callback_id=payload.callback_id,
            trace_id=payload.trace_id,
            event_type=payload.event_type,
            request_body=raw_body,
        )
    )

    user = await _get_or_create_user(db, payload.tg_user_id, payload.payload)
    _apply_user_state(user, payload.event_type, payload.external_user_id, payload.payload)
    await _apply_callback_tracking(db, payload, user)

    return _response({"accepted": True, "idempotent": False, "callback_id": payload.callback_id}, trace_id=payload.trace_id)
