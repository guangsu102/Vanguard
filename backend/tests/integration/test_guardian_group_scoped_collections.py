import pytest

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.guardian.models import (
    ModerationRule,
    ModerationSensitiveKeyword,
    Whitelist,
)
from app.modules.guardian.sync import sync_managed_group_binding


@pytest.fixture(autouse=True)
def override_authentication():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "guardian-collection-test",
        "role": "admin",
    }
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _create_managed_group(test_db, telegram_chat_id: int):
    bot = TelegramAccount(
        identifier=f"@collection_guardian_{abs(telegram_chat_id)}",
        account_type=AccountType.GUARDIAN_BOT,
        session_name=f"collection_guardian_{abs(telegram_chat_id)}",
        api_config_name="default",
        country_code="US",
        status=AccountStatus.OFFLINE,
        is_active=True,
    )
    test_db.add(bot)
    await test_db.flush()
    result = await sync_managed_group_binding(
        test_db,
        bot_account_id=bot.id,
        telegram_group_id=telegram_chat_id,
        title="Existing managed group",
        chat_type="supergroup",
    )
    await test_db.commit()
    return result.binding


@pytest.mark.asyncio
async def test_sensitive_keyword_uses_core_id_and_returns_telegram_id(client, test_db):
    telegram_chat_id = -100123460001
    binding = await _create_managed_group(test_db, telegram_chat_id)

    created = await client.post(
        "/api/moderation-sensitive-keywords",
        json={
            "text": "group-only-spam",
            "category": "ads",
            "group_id": telegram_chat_id,
        },
    )

    assert created.status_code == 201
    created_data = created.json()
    assert created_data["group_id"] == telegram_chat_id
    stored = await test_db.get(ModerationSensitiveKeyword, created_data["id"])
    assert stored is not None
    assert stored.group_id == binding.group_id

    listed = await client.get(
        "/api/moderation-sensitive-keywords",
        params={"group_id": telegram_chat_id},
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["data"][0]["group_id"] == telegram_chat_id

    updated = await client.put(
        f"/api/moderation-sensitive-keywords/{stored.id}",
        json={"category": "fraud"},
    )
    assert updated.status_code == 200
    assert updated.json()["group_id"] == telegram_chat_id

    global_keyword = await client.post(
        "/api/moderation-sensitive-keywords",
        json={"text": "global-spam", "category": "ads"},
    )
    assert global_keyword.status_code == 201
    assert global_keyword.json()["group_id"] is None
    global_stored = await test_db.get(
        ModerationSensitiveKeyword, global_keyword.json()["id"]
    )
    assert global_stored is not None
    assert global_stored.group_id is None

    unfiltered = await client.get("/api/moderation-sensitive-keywords")
    assert unfiltered.status_code == 200
    assert {item["group_id"] for item in unfiltered.json()["data"]} == {
        None,
        telegram_chat_id,
    }


@pytest.mark.asyncio
async def test_rules_and_whitelist_use_core_id_and_return_telegram_id(client, test_db):
    telegram_chat_id = -100123460002
    binding = await _create_managed_group(test_db, telegram_chat_id)

    rule_response = await client.post(
        "/api/rules",
        json={
            "rule_type": "keyword",
            "pattern": "blocked phrase",
            "group_id": telegram_chat_id,
        },
    )
    assert rule_response.status_code == 201
    rule_data = rule_response.json()
    assert rule_data["group_id"] == telegram_chat_id
    stored_rule = await test_db.get(ModerationRule, rule_data["id"])
    assert stored_rule is not None
    assert stored_rule.group_id == binding.group_id

    fetched_rule = await client.get(f"/api/rules/{stored_rule.id}")
    listed_rules = await client.get(
        "/api/rules", params={"group_id": telegram_chat_id}
    )
    assert fetched_rule.status_code == 200
    assert fetched_rule.json()["group_id"] == telegram_chat_id
    assert listed_rules.status_code == 200
    assert listed_rules.json()["data"][0]["group_id"] == telegram_chat_id

    whitelist_response = await client.post(
        "/api/rules/whitelist",
        json={
            "whitelist_type": "user",
            "value": "123456789",
            "group_id": telegram_chat_id,
        },
    )
    assert whitelist_response.status_code == 201
    whitelist_data = whitelist_response.json()
    assert whitelist_data["group_id"] == telegram_chat_id
    stored_whitelist = await test_db.get(Whitelist, whitelist_data["id"])
    assert stored_whitelist is not None
    assert stored_whitelist.group_id == binding.group_id

    fetched_whitelist = await client.get(
        f"/api/rules/whitelist/{stored_whitelist.id}"
    )
    listed_whitelist = await client.get(
        "/api/rules/whitelist", params={"group_id": telegram_chat_id}
    )
    assert fetched_whitelist.status_code == 200
    assert fetched_whitelist.json()["group_id"] == telegram_chat_id
    assert listed_whitelist.status_code == 200
    assert listed_whitelist.json()["data"][0]["group_id"] == telegram_chat_id


@pytest.mark.asyncio
async def test_rules_and_whitelist_preserve_global_scope_and_reject_unmanaged_group(
    client, test_db
):
    global_rule = await client.post(
        "/api/rules",
        json={"rule_type": "domain", "pattern": "example.invalid"},
    )
    global_whitelist = await client.post(
        "/api/rules/whitelist",
        json={"whitelist_type": "domain", "value": "allowed.invalid"},
    )
    assert global_rule.status_code == 201
    assert global_rule.json()["group_id"] is None
    assert global_whitelist.status_code == 201
    assert global_whitelist.json()["group_id"] is None

    stored_rule = await test_db.get(ModerationRule, global_rule.json()["id"])
    stored_whitelist = await test_db.get(Whitelist, global_whitelist.json()["id"])
    assert stored_rule is not None and stored_rule.group_id is None
    assert stored_whitelist is not None and stored_whitelist.group_id is None

    unmanaged_chat_id = -100123469999
    rejected_rule = await client.post(
        "/api/rules",
        json={
            "rule_type": "keyword",
            "pattern": "unmanaged",
            "group_id": unmanaged_chat_id,
        },
    )
    rejected_whitelist = await client.post(
        "/api/rules/whitelist",
        json={
            "whitelist_type": "user",
            "value": "987654321",
            "group_id": unmanaged_chat_id,
        },
    )
    assert rejected_rule.status_code == 404
    assert rejected_rule.json()["detail"] == "Managed group binding not found"
    assert rejected_whitelist.status_code == 404
    assert rejected_whitelist.json()["detail"] == "Managed group binding not found"
