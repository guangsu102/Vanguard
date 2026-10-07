from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.ephemeral_secret import decrypt_ephemeral_secret
from app.core.security import get_current_user
from app.main import app
from app.modules.acquisition.models import AdCreative
from app.modules.qq.models import QQAdSchedule, QQAutomationLog, QQBotConnection


def authenticate(role="admin"):
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": role, "role": role}


@pytest.mark.asyncio
async def test_account_credentials_are_encrypted_and_never_returned(client, test_db):
    authenticate()
    result = await client.post(
        "/api/qq/automation/accounts",
        json={
            "account_number": "987654321",
            "http_url": "http://qq-one:3000",
            "access_token": "secret" * 8,
        },
    )
    assert result.status_code == 201
    assert "token" not in result.text and "secret" not in result.text
    account = await test_db.scalar(
        select(QQBotConnection).where(QQBotConnection.app_id == "987654321")
    )
    assert account.access_token_encrypted.startswith("vge1:")
    assert decrypt_ephemeral_secret(account.access_token_encrypted) == "secret" * 8
    result = await client.patch(
        f"/api/qq/automation/accounts/{account.id}", json={"max_sends_per_day": None}
    )
    assert result.status_code == 422


@pytest.mark.asyncio
async def test_non_admin_cannot_configure_qq_account(client):
    authenticate("viewer")
    result = await client.post(
        "/api/qq/automation/accounts",
        json={
            "account_number": "987654321",
            "http_url": "http://qq-one:3000",
            "access_token": "s" * 32,
        },
    )
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_campaign_and_shared_creative_binding_are_separate_from_telegram(client, test_db):
    authenticate()
    account = QQBotConnection(app_id="987654321")
    creative = AdCreative(name="Shared", content="广告", enabled=True)
    test_db.add_all([account, creative])
    await test_db.commit()
    result = await client.post(
        "/api/qq/automation/campaigns",
        json={
            "name": "QQ plan",
            "targets": [{"group_number": "123456789"}],
        },
    )
    assert result.status_code == 201
    campaign = result.json()["data"]
    assert not campaign["enabled"] and campaign["targets"][0]["group_number"] == "123456789"
    payload = {
        "connection_id": account.id,
        "campaign_id": campaign["id"],
        "creative_ids": [creative.id],
    }
    first = await client.post("/api/qq/automation/bindings", json=payload)
    second = await client.post("/api/qq/automation/bindings", json=payload)
    assert first.json()["data"]["created"] == 1
    assert second.json()["data"]["created"] == 0
    listed = await client.get("/api/qq/automation/bindings")
    assert len(listed.json()["data"]) == 1


@pytest.mark.asyncio
async def test_telegram_file_id_cannot_be_bound_as_qq_image(client, test_db):
    authenticate()
    account = QQBotConnection(app_id="987654321")
    creative = AdCreative(
        name="TG image", content="hello", creative_type="image", media_url="AgAC_TG_file_id"
    )
    test_db.add_all([account, creative])
    await test_db.commit()
    campaign = (await client.post("/api/qq/automation/campaigns", json={"name": "QQ plan"})).json()[
        "data"
    ]
    result = await client.post(
        "/api/qq/automation/bindings",
        json={
            "connection_id": account.id,
            "campaign_id": campaign["id"],
            "creative_ids": [creative.id],
        },
    )
    assert result.status_code == 422
    assert "file_id" in result.text


@pytest.mark.asyncio
async def test_unknown_delivery_requires_explicit_remote_reconciliation(client, test_db):
    authenticate()
    account = QQBotConnection(app_id="987654321")
    test_db.add(account)
    await test_db.commit()
    campaign = (await client.post("/api/qq/automation/campaigns", json={"name": "QQ plan"})).json()[
        "data"
    ]
    schedule = QQAdSchedule(
        connection_id=account.id,
        campaign_id=campaign["id"],
        group_number="123456789",
        status="paused",
    )
    log = QQAutomationLog(
        connection_id=account.id,
        campaign_id=campaign["id"],
        group_number="123456789",
        operation_type="ad",
        operation_key="test-ad",
        status="unknown",
    )
    test_db.add_all([schedule, log])
    await test_db.commit()
    result = await client.post(f"/api/qq/automation/schedules/{schedule.id}/resume", json={})
    assert result.status_code == 409
    result = await client.post(
        f"/api/qq/automation/schedules/{schedule.id}/resume", json={"confirmed_not_sent": True}
    )
    assert result.status_code == 200
    assert log.status == "confirmed_not_sent"


@pytest.mark.parametrize(
    "values",
    [
        {"send_mode": "scheduled", "scheduled_times": []},
        {"send_mode": "scheduled", "scheduled_times": ["25:00"]},
        {"timezone": "invalid-zone"},
        {"targets": [{"group_number": "123456789"}, {"group_number": "123456789"}]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_campaign_is_rejected(client, values):
    authenticate()
    result = await client.post("/api/qq/automation/campaigns", json={"name": "QQ plan", **values})
    assert result.status_code == 422
