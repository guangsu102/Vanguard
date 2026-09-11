from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.account.models import AccountStatus, AccountType, GuardianBotProfile, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedBotProfile

governance = importlib.import_module("app.modules.owned_group.governance")


@pytest.fixture(autouse=True)
def authenticated_admin():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9401,
        "username": "governance-api-test",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed(test_db, *, chat_id: int = -10092001):
    owner = TelegramAccount(
        identifier=f"api-owner-{abs(chat_id)}",
        session_name=f"api-owner-{abs(chat_id)}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="owner-session",
    )
    bot = TelegramAccount(
        identifier=f"api-bot-{abs(chat_id)}",
        session_name=f"api-bot-{abs(chat_id)}",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        risk_level="normal",
        is_active=True,
        display_name="API Guardian",
    )
    test_db.add_all([owner, bot])
    await test_db.flush()
    test_db.add_all(
        [
            GuardianBotProfile(
                account_id=bot.id,
                bot_token="123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                bot_username="api_guardian",
                bot_user_id=992001,
                enabled=True,
            ),
            OwnedBotProfile(
                owner_account_id=owner.id,
                account_id=bot.id,
                token_ciphertext="encrypted",
                bot_username="api_guardian",
                bot_user_id=992001,
                status="verified",
                enabled=True,
            ),
        ]
    )
    asset = OwnedGroupAsset(
        internal_name=f"api-asset-{abs(chat_id)}",
        title="API Governance Group",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
        telegram_chat_id=chat_id,
    )
    test_db.add(asset)
    await test_db.commit()
    await test_db.refresh(asset)
    return asset, bot


def _allow_gate(monkeypatch):
    monkeypatch.setattr(
        governance,
        "require_owned_group_governance_available",
        AsyncMock(return_value=SimpleNamespace(governance_stop=False)),
    )


def _install_client(monkeypatch, *, can_pin: bool = True):
    class FakeClient:
        def __init__(self, _config):
            pass

        async def get_me(self):
            return SimpleNamespace(user_id=992001, username="api_guardian", is_bot=True)

        async def get_chat(self, _chat_id):
            return SimpleNamespace(type="supergroup")

        async def get_chat_member(self, _chat_id, _user_id):
            return {
                "status": "administrator",
                "can_delete_messages": True,
                "can_restrict_members": True,
                "can_invite_users": True,
                "can_pin_messages": can_pin,
            }

        async def close(self):
            return None

    monkeypatch.setattr(governance, "TelegramClient", FakeClient)


@pytest.mark.asyncio
async def test_governance_get_is_available_to_auditor_and_is_read_only(client, test_db):
    asset, _bot = await _seed(test_db)
    app.dependency_overrides[get_current_user] = lambda: {"id": 9402, "role": "auditor"}

    response = await client.get(f"/api/owned-groups/{asset.id}/governance")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["governance_status"] == "disabled"
    assert body["telegram_chat_id"] == -10092001
    assert body["capabilities"]["ban"] is False


@pytest.mark.asyncio
async def test_governance_write_rejects_auditor(client, test_db):
    asset, bot = await _seed(test_db)
    app.dependency_overrides[get_current_user] = lambda: {"id": 9403, "role": "auditor"}

    response = await client.post(
        f"/api/owned-groups/{asset.id}/governance/bind",
        json={"guardian_bot_account_id": bot.id},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_governance_bind_api_returns_stable_envelope(client, test_db, monkeypatch):
    asset, bot = await _seed(test_db)
    _allow_gate(monkeypatch)
    _install_client(monkeypatch)

    response = await client.post(
        f"/api/owned-groups/{asset.id}/governance/bind",
        headers={"X-Correlation-ID": "api-bind-correlation"},
        json={"guardian_bot_account_id": bot.id},
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    body = response.json()["data"]
    assert body["governance_status"] == "managed"
    assert body["correlation_id"] == "api-bind-correlation"
    assert "token" not in response.text.lower()


@pytest.mark.asyncio
async def test_governance_permission_error_is_structured_and_redacted(
    client, test_db, monkeypatch
):
    asset, bot = await _seed(test_db)
    _allow_gate(monkeypatch)
    _install_client(monkeypatch, can_pin=False)

    response = await client.post(
        f"/api/owned-groups/{asset.id}/governance/bind",
        json={"guardian_bot_account_id": bot.id},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "guardian_permissions_missing"
    assert detail["retryable"] is True
    assert detail["missing_permissions"] == ["can_pin_messages"]
    assert "BBBB" not in response.text


@pytest.mark.asyncio
async def test_governance_candidates_are_backend_filtered(client, test_db):
    asset, bot = await _seed(test_db)

    response = await client.get(
        f"/api/owned-groups/{asset.id}/governance/candidates"
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["guardian_bot_account_id"] == bot.id
    assert "token" not in response.text.lower()


@pytest.mark.asyncio
async def test_governance_gate_error_uses_public_envelope(
    client,
    test_db,
    monkeypatch,
):
    asset, bot = await _seed(test_db, chat_id=-10092002)
    monkeypatch.setattr(
        governance,
        "require_owned_group_governance_available",
        AsyncMock(
            side_effect=HTTPException(
                status_code=503,
                detail={
                    "reason": "governance_feature_disabled",
                    "message": "Owned-group Guardian governance is disabled",
                    "retryable": False,
                },
            )
        ),
    )

    response = await client.post(
        f"/api/owned-groups/{asset.id}/governance/bind",
        headers={"X-Correlation-ID": "api-gate-correlation"},
        json={"guardian_bot_account_id": bot.id},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "reason": "governance_feature_disabled",
        "message": "Owned-group Guardian governance is disabled",
        "retryable": False,
        "asset_id": asset.id,
        "correlation_id": "api-gate-correlation",
    }
