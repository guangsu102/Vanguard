import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.guardian_validation import (
    ensure_managed_group_binding,
    resolve_guardian_group_target,
)
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group
from app.core.security import get_current_user
from app.main import app
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.guardian.sync import ManagedGroupSyncConflict, sync_managed_group_binding


@pytest.fixture(autouse=True)
def override_authentication():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "guardian-id-test",
        "role": "admin",
    }
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def _create_managed_supergroup(test_db, telegram_chat_id: int):
    bot = TelegramAccount(
        identifier=f"@guardian_{abs(telegram_chat_id)}",
        account_type=AccountType.GUARDIAN_BOT,
        session_name=f"guardian_{abs(telegram_chat_id)}",
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
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
        chat_type="supergroup",
    )
    await test_db.commit()
    return result.binding


@pytest.mark.asyncio
async def test_guardian_target_resolves_telegram_chat_to_core_group(test_db):
    telegram_chat_id = -100123450001
    binding = await _create_managed_supergroup(test_db, telegram_chat_id)

    resolved = await resolve_guardian_group_target(test_db, telegram_chat_id)
    ensured = await ensure_managed_group_binding(test_db, telegram_chat_id)

    assert resolved is not None
    assert resolved.core_group_id == binding.group_id
    assert resolved.telegram_chat_id == telegram_chat_id
    assert resolved.managed_binding_id == binding.id
    assert resolved.bot_account_id == binding.bot_account_id
    assert resolved.binding is binding
    assert ensured == resolved


@pytest.mark.asyncio
async def test_managed_group_sync_keys_default_policies_by_core_group_id(test_db):
    telegram_chat_id = -100123450002
    binding = await _create_managed_supergroup(test_db, telegram_chat_id)
    group = await test_db.get(Group, binding.group_id)

    repeated = await sync_managed_group_binding(
        test_db,
        bot_account_id=binding.bot_account_id,
        telegram_group_id=telegram_chat_id,
        title="Existing managed group",
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
        chat_type="supergroup",
    )
    await test_db.commit()

    assert group is not None
    assert group.group_id == telegram_chat_id
    assert repeated.created_group is False
    assert repeated.created_binding is False
    assert repeated.binding.id == binding.id
    for model in (
        GroupVerificationConfig,
        GroupModerationPolicy,
        GroupPunishmentPolicy,
    ):
        core_policy = await test_db.scalar(
            select(model).where(model.group_id == group.id)
        )
        telegram_id_policy = await test_db.scalar(
            select(model).where(model.group_id == telegram_chat_id)
        )
        assert core_policy is not None
        assert telegram_id_policy is None
        assert (
            await test_db.scalar(
                select(func.count()).select_from(model).where(model.group_id == group.id)
            )
        ) == 1


@pytest.mark.asyncio
async def test_sync_rejects_explicit_core_group_for_a_different_telegram_chat(
    test_db,
):
    telegram_chat_id = -100123450020
    other_chat_id = -100123450021
    binding = await _create_managed_supergroup(test_db, telegram_chat_id)
    group = await test_db.get(Group, binding.group_id)
    original_title = group.title

    with pytest.raises(ManagedGroupSyncConflict, match="does not match"):
        await sync_managed_group_binding(
            test_db,
            bot_account_id=binding.bot_account_id,
            telegram_group_id=other_chat_id,
            group_id=group.id,
            title="Must not overwrite the original group",
        )

    assert group.group_id == telegram_chat_id
    assert group.title == original_title
    assert await test_db.scalar(
        select(ManagedGroupBinding).where(
            ManagedGroupBinding.telegram_group_id == other_chat_id
        )
    ) is None


@pytest.mark.asyncio
async def test_guardian_target_rejects_a_corrupt_core_to_chat_mapping(test_db):
    original_chat_id = -100123450022
    corrupt_chat_id = -100123450023
    binding = await _create_managed_supergroup(test_db, original_chat_id)
    binding.telegram_group_id = corrupt_chat_id
    await test_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await resolve_guardian_group_target(test_db, corrupt_chat_id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["reason"] == "guardian_group_id_mismatch"


@pytest.mark.asyncio
async def test_policy_apis_keep_telegram_id_contract_and_persist_core_id(client, test_db):
    telegram_chat_id = -100123450003
    binding = await _create_managed_supergroup(test_db, telegram_chat_id)

    updates = (
        (
            "verification",
            {"group_id": telegram_chat_id, "enable_verification": True},
            "enable_verification",
            True,
        ),
        (
            "moderation",
            {"group_id": telegram_chat_id, "max_messages_per_minute": 9},
            "max_messages_per_minute",
            9,
        ),
        (
            "punishment",
            {"group_id": telegram_chat_id, "warn_threshold": 4},
            "warn_threshold",
            4,
        ),
    )
    for endpoint, payload, field, expected in updates:
        updated = await client.put(f"/api/group-governance/{endpoint}", json=payload)
        assert updated.status_code == 200
        assert updated.json()["data"]["group_id"] == telegram_chat_id
        assert updated.json()["data"][field] == expected

        fetched = await client.get(
            f"/api/group-governance/{endpoint}/{telegram_chat_id}"
        )
        assert fetched.status_code == 200
        assert fetched.json()["data"]["group_id"] == telegram_chat_id
        assert fetched.json()["data"][field] == expected

    for model in (
        GroupVerificationConfig,
        GroupModerationPolicy,
        GroupPunishmentPolicy,
    ):
        core_policy = await test_db.scalar(
            select(model).where(model.group_id == binding.group_id)
        )
        telegram_id_policy = await test_db.scalar(
            select(model).where(model.group_id == telegram_chat_id)
        )
        assert core_policy is not None
        assert telegram_id_policy is None
