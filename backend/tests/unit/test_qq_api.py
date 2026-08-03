from __future__ import annotations

import importlib

import pytest

from app.core.config import settings
from app.core.security import get_current_user
from app.main import app


@pytest.mark.asyncio
async def test_qq_group_registration_update_and_notification_queue(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "QQ_ONEBOT_ENABLED", True)
    monkeypatch.setattr(settings, "QQ_ONEBOT_ACCOUNT_ID", "10001")
    monkeypatch.setattr(settings, "QQ_ONEBOT_ACCESS_TOKEN", "t" * 32)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "admin",
        "role": "admin",
    }

    queued: list[tuple[list[str], str]] = []

    def fake_apply_async(*, args, queue):
        queued.append((args, queue))
        return type("Result", (), {"id": "task-1"})()

    qq_api_module = importlib.import_module("app.api.qq")
    monkeypatch.setattr(qq_api_module.execute_qq_command, "apply_async", fake_apply_async)

    create_response = await client.post(
        "/api/qq/groups",
        json={"group_number": "123456789", "local_name": "Support"},
    )
    assert create_response.status_code == 201
    group = create_response.json()["data"]
    assert group["local_name"] == "Support"

    update_response = await client.patch(
        f"/api/qq/groups/{group['id']}",
        json={"monitoring_enabled": False},
    )
    assert update_response.status_code == 200
    assert update_response.json()["data"]["monitoring_enabled"] is False

    notification_response = await client.post(
        f"/api/qq/groups/{group['id']}/notifications",
        json={"content": "Maintenance at 22:00"},
    )
    assert notification_response.status_code == 202
    assert notification_response.json()["data"]["status"] == "queued"
    assert queued and queued[0][1] == "qq_commands"

    list_response = await client.get("/api/qq/groups")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1


@pytest.mark.asyncio
async def test_qq_group_sync_uses_napcat_account(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "QQ_ONEBOT_ENABLED", True)
    monkeypatch.setattr(settings, "QQ_ONEBOT_ACCOUNT_ID", "10001")
    monkeypatch.setattr(settings, "QQ_ONEBOT_ACCESS_TOKEN", "t" * 32)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "admin",
        "role": "admin",
    }

    class FakeOneBotClient:
        account_id = "10001"

        async def get_login_info(self):
            return {"user_id": 10001, "nickname": "Notifier"}

        async def get_group_list(self):
            return [{"group_id": 123456789, "group_name": "Support"}]

        async def close(self):
            return None

    qq_api_module = importlib.import_module("app.api.qq")
    monkeypatch.setattr(qq_api_module, "OneBotClient", FakeOneBotClient)

    sync_response = await client.post("/api/qq/groups/sync")
    assert sync_response.status_code == 200
    assert sync_response.json()["data"] == {"total": 1}

    list_response = await client.get("/api/qq/groups")
    assert list_response.json()["data"][0]["group_number"] == "123456789"
