"""Integration tests for XBoard API endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignExecution,
    CampaignScope,
    CampaignTracking,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.config import settings
from app.core.user.models import User, UserState
from app.integrations.xboard.models import XBoardCallback, XBoardEvent
from app.modules.acquisition.models import AcquisitionTracking
from app.modules.guardian.models import CouponDistribution


def _build_signature(method: str, path: str, query_string: str, timestamp: str, request_id: str, raw_body: str, secret: str | None = None) -> str:
    signing_string = "\n".join([method.upper(), path, query_string, timestamp, request_id, raw_body])
    return hmac.new(
        (secret or settings.VANGUARD_SIGNING_SECRET).encode("utf-8"),
        signing_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _xboard_headers(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query_string: str = "",
    request_id: str | None = None,
    app_id: str | None = None,
    secret: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    request_id = request_id or f"req_test_{uuid.uuid4().hex}"
    raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else ""
    return {
        "X-App-Id": app_id or settings.VANGUARD_APP_ID,
        "X-Timestamp": timestamp,
        "X-Request-Id": request_id,
        "X-Signature": _build_signature(method, path, query_string, timestamp, request_id, raw_body, secret),
    }


@pytest.mark.asyncio
async def test_event_ingest_signature_and_idempotency(client, test_db):
    payload = {
        "event_id": "evt_20260523_000001",
        "trace_id": "trace_20260523_abc123",
        "event_type": "user.registered",
        "tracking_code": "ref_xxx",
        "tg_user_id": 123456789,
        "external_user_id": "9988",
        "occurred_at": "2026-05-23T10:00:00Z",
        "payload": {"source": "telegram"},
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == 0
    assert data["data"]["accepted"] is True
    assert data["data"]["idempotent"] is False

    user = (await test_db.execute(select(User).where(User.telegram_id == 123456789))).scalar_one_or_none()
    assert user is None or user.state == UserState.PENDING

    headers2 = _xboard_headers("POST", "/api/v1/events/ingest", payload)
    resp2 = await client.post("/api/v1/events/ingest", json=payload, headers=headers2)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["data"]["idempotent"] is True

    event_row = (await test_db.execute(select(XBoardEvent).where(XBoardEvent.event_id == payload["event_id"])) ).scalar_one_or_none()
    assert event_row is not None


@pytest.mark.asyncio
async def test_event_ingest_rejects_duplicate_request_id(client):
    payload = {
        "event_id": "evt_duplicate_request_id",
        "trace_id": "trace_duplicate_request_id",
        "event_type": "user.lead_created",
        "occurred_at": "2026-05-23T10:00:00Z",
        "payload": {},
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload, request_id="req_duplicate_request_id")
    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200

    payload["event_id"] = "evt_duplicate_request_id_second"
    headers2 = _xboard_headers("POST", "/api/v1/events/ingest", payload, request_id="req_duplicate_request_id")
    resp2 = await client.post("/api/v1/events/ingest", json=payload, headers=headers2)
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["code"] == 4005


@pytest.mark.asyncio
async def test_event_ingest_rejects_invalid_signature(client):
    payload = {
        "event_id": "evt_bad_sig",
        "trace_id": "trace_bad_sig",
        "event_type": "user.activated",
        "occurred_at": "2026-05-23T10:00:00Z",
        "payload": {},
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)
    headers["X-Signature"] = "deadbeef"

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == 4001


@pytest.mark.asyncio
async def test_event_ingest_rejects_unsupported_event_type(client):
    payload = {
        "event_id": "evt_bad_type",
        "trace_id": "trace_bad_type",
        "event_type": "unknown.event",
        "occurred_at": "2026-05-23T10:00:00Z",
        "payload": {},
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_event_ingest_rejects_expired_timestamp(client):
    payload = {
        "event_id": "evt_expired_ts",
        "trace_id": "trace_expired_ts",
        "event_type": "user.activated",
        "occurred_at": "2026-05-23T10:00:00Z",
        "payload": {},
    }
    timestamp = str(int(time.time() * 1000) - (settings.VANGUARD_TIMESTAMP_TOLERANCE + 10) * 1000)
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "X-App-Id": settings.VANGUARD_APP_ID,
        "X-Timestamp": timestamp,
        "X-Request-Id": "req_expired_ts",
        "X-Signature": _build_signature("POST", "/api/v1/events/ingest", "", timestamp, "req_expired_ts", raw_body),
    }

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == 4003


@pytest.mark.asyncio
async def test_tracking_clicked_persists_traffic_source(client, test_db):
    payload = {
        "event_id": "evt_tracking_clicked_1",
        "trace_id": "trace_tracking_clicked_1",
        "event_type": "tracking.clicked",
        "tracking_code": "ref_click_1",
        "tg_user_id": 123456789,
        "tg_group_id": -1001234567890,
        "occurred_at": "2026-05-23T10:08:00Z",
        "payload": {
            "source": "telegram",
            "campaign_name": "spring_launch",
            "keyword": "ref_click_1",
            "bot_id": "bot_001",
        },
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["accepted"] is True

    tracking = (await test_db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == "ref_click_1"))).scalar_one_or_none()
    assert tracking is not None
    assert tracking.click_at is not None
    assert tracking.source_type == "telegram"
    assert tracking.campaign_name == "spring_launch"
    assert tracking.group_id == -1001234567890
    assert tracking.bot_id == "bot_001"


@pytest.mark.asyncio
async def test_user_status_rejects_empty_lookup(client):
    headers = _xboard_headers("GET", "/api/v1/users/status")
    resp = await client.get("/api/v1/users/status", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == 4002


@pytest.mark.asyncio
async def test_user_status_uses_user_and_tracking(client, test_db):
    user = User(
        telegram_id=123456789,
        username="alice",
        state=UserState.ACTIVE,
        xboard_user_id=9988,
    )
    test_db.add(user)
    await test_db.flush()
    tracking = AcquisitionTracking(
        user_id=user.id,
        tracking_code="ref_xxx",
        campaign_name="spring_launch",
        source_type="telegram",
        group_id=-1001234567890,
        keyword="节点",
        bot_id="bot_001",
        registered_at=user.created_at,
        converted_at=user.updated_at,
        converted=True,
        coupon_status="issued",
    )
    test_db.add(tracking)
    await test_db.flush()

    query_1 = "tg_user_id=123456789&trace_id=trace_status_1"
    headers = _xboard_headers("GET", "/api/v1/users/status", query_string=query_1)
    resp = await client.get("/api/v1/users/status", params={"tg_user_id": 123456789, "trace_id": "trace_status_1"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["registered"] is True
    assert data["activated"] is True
    assert data["xboard_user_id"] == 9988
    assert data["order_status"] == 3
    assert data["coupon_status"] is None

    query_2 = "tracking_code=ref_xxx&trace_id=trace_status_2"
    headers_tracking = _xboard_headers("GET", "/api/v1/users/status", query_string=query_2)
    resp_tracking = await client.get("/api/v1/users/status", params={"tracking_code": "ref_xxx", "trace_id": "trace_status_2"}, headers=headers_tracking)
    assert resp_tracking.status_code == 200
    tracking_data = resp_tracking.json()["data"]
    assert tracking_data["tracking_code"] == "ref_xxx"
    assert tracking_data["registered"] is True
    assert tracking_data["coupon_status"] == "issued"
    assert tracking_data["tg_user_id"] == 123456789
    assert tracking_data["xboard_user_id"] == 9988

    uuid_source = str(uuid.uuid4())
    uuid_tracking = AcquisitionTracking(
        user_id=user.id,
        tracking_code="ref_uuid_source",
        campaign_name="uuid_launch",
        source_type="telegram",
        group_id=-1001234567890,
        keyword="uuid",
        bot_id="bot_002",
        registered_at=user.created_at,
        converted_at=user.updated_at,
        converted=True,
        external_user_id=uuid_source,
    )
    test_db.add(uuid_tracking)
    await test_db.flush()

    query_3 = f"external_user_id={uuid_source}&trace_id=trace_status_3"
    headers_uuid = _xboard_headers("GET", "/api/v1/users/status", query_string=query_3)
    resp_uuid = await client.get("/api/v1/users/status", params={"external_user_id": uuid_source, "trace_id": "trace_status_3"}, headers=headers_uuid)
    assert resp_uuid.status_code == 200
    uuid_data = resp_uuid.json()["data"]
    assert uuid_data["tg_user_id"] == 123456789
    assert uuid_data["registered"] is True
    assert uuid_data["activated"] is True
    assert uuid_data["xboard_user_id"] == 9988
    assert uuid_data["order_status"] == 3


@pytest.mark.asyncio
async def test_user_status_external_user_id_returns_tracking_without_user(client, test_db):
    external_user_id = str(uuid.uuid4())
    tracking = AcquisitionTracking(
        tracking_code="ref_external_only",
        campaign_name="external_launch",
        source_type="telegram",
        group_id=-1001234567890,
        keyword="external",
        bot_id="bot_002",
        external_user_id=external_user_id,
        registered_at=datetime(2026, 5, 23, 10, 0),
        converted=True,
        converted_at=datetime(2026, 5, 23, 10, 20),
        coupon_status="issued",
    )
    test_db.add(tracking)
    await test_db.flush()

    query = f"external_user_id={external_user_id}&trace_id=trace_status_external_only"
    headers = _xboard_headers("GET", "/api/v1/users/status", query_string=query)
    resp = await client.get(
        "/api/v1/users/status",
        params={"external_user_id": external_user_id, "trace_id": "trace_status_external_only"},
        headers=headers,
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["tracking_code"] == "ref_external_only"
    assert data["registered"] is True
    assert data["activated"] is True
    assert data["coupon_status"] == "issued"


@pytest.mark.asyncio
async def test_tracking_converted_updates_tracking_and_user_state(client, test_db):
    user = User(
        telegram_id=123456789,
        username="alice",
        state=UserState.PENDING,
        xboard_user_id=None,
    )
    test_db.add(user)
    await test_db.flush()

    payload = {
        "event_id": "evt_tracking_converted_1",
        "trace_id": "trace_tracking_converted_1",
        "event_type": "tracking.converted",
        "tracking_code": "ref_uuid_1",
        "tg_user_id": 123456789,
        "external_user_id": str(uuid.uuid4()),
        "occurred_at": "2026-05-23T10:10:00Z",
        "payload": {
            "source": "telegram",
            "campaign_name": "spring_launch",
            "keyword": "ref_uuid_1",
            "bot_id": "bot_001",
            "coupon_status": "issued",
            "trial_granted": True,
        },
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["accepted"] is True
    assert data["idempotent"] is False

    updated_user = (await test_db.execute(select(User).where(User.telegram_id == 123456789))).scalar_one_or_none()
    assert updated_user is not None
    assert updated_user.state == UserState.ACTIVE
    assert updated_user.xboard_user_id is None

    tracking = (await test_db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == "ref_uuid_1"))).scalar_one_or_none()
    assert tracking is not None
    assert tracking.converted_at is not None
    assert tracking.converted is True
    assert tracking.coupon_status == "issued"
    assert tracking.trial_granted is True
    assert tracking.user_id == updated_user.id
    assert updated_user.state == UserState.ACTIVE


@pytest.mark.asyncio
async def test_coupon_report_idempotency_and_state_updates(client, test_db):
    user = User(
        telegram_id=123456789,
        username="alice",
        state=UserState.PENDING,
        xboard_user_id=None,
    )
    test_db.add(user)
    await test_db.flush()

    tracking = AcquisitionTracking(
        user_id=user.id,
        tracking_code="ref_xxx",
        campaign_name="spring_launch",
        source_type="telegram",
        group_id=-1001234567890,
        keyword="coupon",
        bot_id="bot_001",
        registered_at=user.created_at,
        converted=False,
    )
    test_db.add(tracking)
    await test_db.flush()

    payload = {
        "event_id": "evt_coupon_1",
        "trace_id": "trace_coupon_1",
        "tg_user_id": 123456789,
        "external_user_id": "9988",
        "tracking_code": "ref_xxx",
        "coupon_code": "VIP100",
        "coupon_status": "issued",
        "issued_at": "2026-05-23T10:05:00Z",
        "payload": {"source_event_id": "evt_20260523_000001"},
    }
    headers = _xboard_headers("POST", "/api/v1/coupons/report", payload)

    resp = await client.post("/api/v1/coupons/report", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["idempotent"] is False

    headers2 = _xboard_headers("POST", "/api/v1/coupons/report", payload)
    resp2 = await client.post("/api/v1/coupons/report", json=payload, headers=headers2)
    assert resp2.status_code == 200
    assert resp2.json()["data"]["idempotent"] is True

    row = (await test_db.execute(select(XBoardEvent).where(XBoardEvent.event_id == "evt_coupon_1"))).scalar_one_or_none()
    assert row is not None
    assert row.event_type == "coupon.issued"

    updated_user = (await test_db.execute(select(User).where(User.telegram_id == 123456789))).scalar_one_or_none()
    assert updated_user is not None
    assert updated_user.state == UserState.ACTIVE

    updated_tracking = (await test_db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == "ref_xxx"))).scalar_one_or_none()
    assert updated_tracking is not None
    assert updated_tracking.coupon_status == "issued"
    campaign_tracking = (await test_db.execute(select(CampaignTracking).where(CampaignTracking.keyword == "ref_xxx"))).scalar_one_or_none()
    assert campaign_tracking is not None
    assert campaign_tracking.coupon_granted is True


@pytest.mark.asyncio
async def test_user_registered_updates_user_and_tracking(client, test_db):
    payload = {
        "event_id": "evt_user_registered_1",
        "trace_id": "trace_user_registered_1",
        "event_type": "user.registered",
        "tracking_code": "ref_registered_1",
        "tg_user_id": 123456789,
        "external_user_id": "9988",
        "occurred_at": "2026-05-23T10:12:00Z",
        "payload": {
            "source": "telegram",
            "campaign_name": "spring_launch",
            "keyword": "ref_registered_1",
            "bot_id": "bot_001",
        },
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["accepted"] is True

    user = (await test_db.execute(select(User).where(User.telegram_id == 123456789))).scalar_one_or_none()
    assert user is not None
    assert user.state == UserState.PENDING

    tracking = (await test_db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == "ref_registered_1"))).scalar_one_or_none()
    assert tracking is not None
    assert tracking.registered_at is not None
    assert tracking.user_id == user.id


@pytest.mark.asyncio
async def test_user_registered_triggers_after_register_campaign(client, test_db):
    campaign = Campaign(
        name="xboard-after-register",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.AFTER_REGISTER,
        distribution_mode=CampaignDistributionMode.WELCOME,
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    payload = {
        "event_id": "evt_user_registered_campaign_1",
        "trace_id": "trace_user_registered_campaign_1",
        "event_type": "user.registered",
        "tracking_code": "ref_registered_campaign_1",
        "tg_user_id": 456789123,
        "external_user_id": "8899",
        "occurred_at": "2026-05-23T10:12:00Z",
        "payload": {
            "source": "telegram",
            "campaign_name": "spring_launch",
            "keyword": "ref_registered_campaign_1",
            "bot_id": "bot_001",
        },
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["campaign_results"][0]["campaign_id"] == campaign.id
    assert data["campaign_results"][0]["reward_granted"] is True

    user = (await test_db.execute(select(User).where(User.telegram_id == 456789123))).scalar_one()
    execution = (
        await test_db.execute(select(CampaignExecution).where(CampaignExecution.campaign_id == campaign.id))
    ).scalar_one()
    assert execution.user_id == user.id
    assert execution.reward_granted is True

    distribution = (
        await test_db.execute(select(CouponDistribution).where(CouponDistribution.campaign_id == campaign.id))
    ).scalar_one()
    assert distribution.user_id == user.id


@pytest.mark.asyncio
async def test_user_activated_updates_user_and_tracking(client, test_db):
    user = User(
        telegram_id=123456789,
        username="alice",
        state=UserState.PENDING,
        xboard_user_id=None,
    )
    test_db.add(user)
    await test_db.flush()

    payload = {
        "event_id": "evt_user_activated_1",
        "trace_id": "trace_user_activated_1",
        "event_type": "user.activated",
        "tracking_code": "ref_activated_1",
        "tg_user_id": 123456789,
        "external_user_id": "9988",
        "occurred_at": "2026-05-23T10:20:00Z",
        "payload": {
            "source": "telegram",
            "campaign_name": "spring_launch",
            "keyword": "ref_activated_1",
            "bot_id": "bot_001",
            "xboard_user_id": 9988,
        },
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["accepted"] is True

    updated_user = (await test_db.execute(select(User).where(User.telegram_id == 123456789))).scalar_one_or_none()
    assert updated_user is not None
    assert updated_user.state == UserState.ACTIVE
    assert updated_user.xboard_user_id == 9988

    tracking = (await test_db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == "ref_activated_1"))).scalar_one_or_none()
    assert tracking is not None
    assert tracking.converted_at is not None
    assert tracking.user_id == updated_user.id


@pytest.mark.asyncio
async def test_event_ingest_external_user_id_uuid_is_accepted(client, test_db):
    payload = {
        "event_id": "evt_uuid_external_1",
        "trace_id": "trace_uuid_external_1",
        "event_type": "tracking.converted",
        "tracking_code": "ref_uuid_external",
        "tg_user_id": 123456789,
        "external_user_id": str(uuid.uuid4()),
        "occurred_at": "2026-05-23T10:15:00Z",
        "payload": {
            "source": "telegram",
            "campaign_name": "uuid_campaign",
            "keyword": "ref_uuid_external",
            "bot_id": "bot_002",
        },
    }
    headers = _xboard_headers("POST", "/api/v1/events/ingest", payload)

    resp = await client.post("/api/v1/events/ingest", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["accepted"] is True

    row = (await test_db.execute(select(XBoardEvent).where(XBoardEvent.event_id == "evt_uuid_external_1"))).scalar_one_or_none()
    assert row is not None
    assert row.event_type == "tracking.converted"


@pytest.mark.asyncio
async def test_xboard_callback_idempotency_and_user_activation(client, test_db):
    user = User(
        telegram_id=123456789,
        username="alice",
        state=UserState.PENDING,
        xboard_user_id=None,
    )
    test_db.add(user)
    await test_db.flush()
    tracking = AcquisitionTracking(
        user_id=user.id,
        tracking_code="ref_callback_1",
        source_type="telegram",
        external_user_id="9988",
    )
    test_db.add(tracking)
    await test_db.flush()

    payload = {
        "callback_id": "cb_20260523_0001",
        "trace_id": "trace_xb_20260523_xxx",
        "event_type": "user.activated",
        "tg_user_id": 123456789,
        "external_user_id": "9988",
        "status": "activated",
        "occurred_at": "2026-05-23T10:30:00+08:00",
        "payload": {"xboard_user_id": 9988},
    }
    headers = _xboard_headers(
        "POST",
        "/api/v1/xboard/callback/status",
        payload,
        app_id=settings.VANGUARD_CALLBACK_APP_ID,
        secret=settings.VANGUARD_CALLBACK_SIGNING_SECRET,
    )

    resp = await client.post("/api/v1/xboard/callback/status", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["idempotent"] is False

    headers2 = _xboard_headers(
        "POST",
        "/api/v1/xboard/callback/status",
        payload,
        app_id=settings.VANGUARD_CALLBACK_APP_ID,
        secret=settings.VANGUARD_CALLBACK_SIGNING_SECRET,
    )
    resp2 = await client.post("/api/v1/xboard/callback/status", json=payload, headers=headers2)
    assert resp2.status_code == 200
    assert resp2.json()["data"]["idempotent"] is True

    updated_user = (await test_db.execute(select(User).where(User.telegram_id == 123456789))).scalar_one_or_none()
    assert updated_user is not None
    assert updated_user.state == UserState.ACTIVE
    assert updated_user.xboard_user_id == 9988

    callback_row = (await test_db.execute(select(XBoardCallback).where(XBoardCallback.callback_id == "cb_20260523_0001"))).scalar_one_or_none()
    assert callback_row is not None
    updated_tracking = (await test_db.execute(select(AcquisitionTracking).where(AcquisitionTracking.tracking_code == "ref_callback_1"))).scalar_one_or_none()
    assert updated_tracking is not None
    assert updated_tracking.converted is True
    assert updated_tracking.converted_at is not None
