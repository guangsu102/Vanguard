from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.account.models import AccountStatus, AccountType, GuardianBotProfile, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupAuditEvent


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9301,
        "username": "owned-bot-test",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_accounts(test_db):
    owner = TelegramAccount(
        identifier="owned-bot-owner",
        session_name="owned-bot-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="owner-session",
    )
    bot_account = TelegramAccount(
        identifier="owned-bot-account",
        session_name="owned-bot-account",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, bot_account])
    await test_db.flush()
    return owner, bot_account


@pytest.mark.asyncio
async def test_register_owned_bot_encrypts_token_and_returns_safe_shape(client, test_db):
    owner, bot_account = await _seed_accounts(test_db)
    test_db.add(
        GuardianBotProfile(
            account_id=bot_account.id,
            bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            bot_username="legacy_bot",
            bot_user_id=777,
        )
    )
    await test_db.commit()

    response = await client.post(
        "/api/owned-groups/bot-profiles",
        json={"owner_account_id": owner.id, "account_id": bot_account.id},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_verification"
    assert "bot_token" not in body
    profile = await test_db.scalar(select(OwnedBotProfile))
    assert profile is not None
    assert profile.token_ciphertext != "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert "123456789:" not in profile.token_ciphertext
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.event_type == "owned_bot_profile_registered"
        )
    )
    assert audit is not None


@pytest.mark.asyncio
async def test_verify_owned_bot_uses_get_me_without_returning_token(
    client, test_db, monkeypatch
):
    owner, bot_account = await _seed_accounts(test_db)
    test_db.add(
        GuardianBotProfile(
            account_id=bot_account.id,
            bot_token="123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        )
    )
    await test_db.commit()
    register = await client.post(
        "/api/owned-groups/bot-profiles",
        json={"owner_account_id": owner.id, "account_id": bot_account.id},
    )
    profile_id = register.json()["id"]

    class FakeClient:
        def __init__(self, config):
            assert config.bot_token == "123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBB"

        async def get_me(self):
            return SimpleNamespace(user_id=888, username="verified_bot", is_bot=True, full_name="Verified Bot")

        async def close(self):
            return None

    module = importlib.import_module("app.api.owned_group_bots")

    monkeypatch.setattr(module, "TelegramClient", FakeClient)
    verify = await client.post(f"/api/owned-groups/bot-profiles/{profile_id}/verify")
    assert verify.status_code == 200
    body = verify.json()
    assert body["status"] == "verified"
    assert body["bot_user_id"] == 888
    assert body["bot_username"] == "verified_bot"
    assert "BBBB" not in verify.text


@pytest.mark.asyncio
async def test_owned_bot_registration_rejects_owner_mismatch(client, test_db):
    owner, bot_account = await _seed_accounts(test_db)
    other_owner = TelegramAccount(
        identifier="owned-bot-other-owner",
        session_name="owned-bot-other-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="other-session",
    )
    test_db.add(other_owner)
    test_db.add(
        GuardianBotProfile(
            account_id=bot_account.id,
            bot_token="123456789:CCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        )
    )
    await test_db.commit()
    first = await client.post(
        "/api/owned-groups/bot-profiles",
        json={"owner_account_id": owner.id, "account_id": bot_account.id},
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/owned-groups/bot-profiles",
        json={"owner_account_id": other_owner.id, "account_id": bot_account.id},
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_operator_cannot_mutate_owned_bot_profiles(client, test_db):
    owner, bot_account = await _seed_accounts(test_db)
    test_db.add(
        GuardianBotProfile(
            account_id=bot_account.id,
            bot_token="123456789:DDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        )
    )
    await test_db.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9302,
        "username": "owned-bot-operator",
        "role": "operator",
    }

    response = await client.post(
        "/api/owned-groups/bot-profiles",
        json={"owner_account_id": owner.id, "account_id": bot_account.id},
    )

    assert response.status_code == 403
    assert await test_db.scalar(select(OwnedBotProfile)) is None
