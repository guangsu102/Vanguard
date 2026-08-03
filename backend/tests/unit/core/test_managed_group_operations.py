import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.managed_groups import (
    ManagedChannelCreateRequest,
    ManagedChannelMessageRequest,
    ManagedChannelUsernameRequest,
    ManagedGroupMuteAllRequest,
    _fallback_unmuted_permissions,
    _lockdown_permissions,
    create_managed_channel,
    refresh_managed_channel_status,
    send_managed_channel_message,
    set_managed_group_mute_all,
    update_managed_channel_username,
)
from app.core.account.models import AccountStatus, AccountType, GuardianBotProfile, TelegramAccount
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.guardian.sync import sync_managed_group_binding

managed_groups_module = importlib.import_module("app.api.managed_groups")


def test_lockdown_permissions_disable_every_member_send_capability():
    permissions = _lockdown_permissions()

    assert permissions
    assert all(value is False for value in permissions.values())
    assert permissions["can_send_messages"] is False
    assert permissions["can_add_web_page_previews"] is False


def test_fallback_unmuted_permissions_do_not_grant_group_management():
    permissions = _fallback_unmuted_permissions()

    assert permissions["can_send_messages"] is True
    assert permissions["can_change_info"] is False
    assert permissions["can_pin_messages"] is False
    assert permissions["can_manage_topics"] is False


@pytest.mark.asyncio
async def test_channel_binding_records_type_without_group_only_policies(test_db):
    bot = TelegramAccount(
        phone=None,
        identifier="@channel_guardian",
        account_type=AccountType.GUARDIAN_BOT,
        api_config_name="default",
        country_code="US",
        session_name="guardian_channel_test",
        status=AccountStatus.IDLE,
    )
    test_db.add(bot)
    await test_db.flush()

    result = await sync_managed_group_binding(
        test_db,
        bot_account_id=bot.id,
        telegram_group_id=-100123456,
        title="Product Updates",
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
        chat_type="channel",
        permissions_snapshot={"bot_assignment_complete": True},
    )
    await test_db.commit()

    snapshot = json.loads(result.binding.permissions_snapshot)
    assert snapshot["chat_type"] == "channel"
    assert snapshot["bot_assignment_complete"] is True
    assert (
        await test_db.execute(
            select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == -100123456)
        )
    ).scalar_one_or_none() is None
    assert (
        await test_db.execute(
            select(GroupModerationPolicy).where(GroupModerationPolicy.group_id == -100123456)
        )
    ).scalar_one_or_none() is None
    assert (
        await test_db.execute(
            select(GroupPunishmentPolicy).where(GroupPunishmentPolicy.group_id == -100123456)
        )
    ).scalar_one_or_none() is None


async def _create_guardian_binding(test_db, *, telegram_id: int, chat_type: str):
    bot = TelegramAccount(
        phone=None,
        identifier=f"@guardian_{abs(telegram_id)}",
        account_type=AccountType.GUARDIAN_BOT,
        api_config_name="default",
        country_code="US",
        session_name=f"guardian_{abs(telegram_id)}",
        status=AccountStatus.IDLE,
    )
    test_db.add(bot)
    await test_db.flush()
    test_db.add(GuardianBotProfile(account_id=bot.id, bot_token="123456:test-token", enabled=True))
    result = await sync_managed_group_binding(
        test_db,
        bot_account_id=bot.id,
        telegram_group_id=telegram_id,
        title="Managed Asset",
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
        chat_type=chat_type,
    )
    await test_db.commit()
    return result.binding


@pytest.mark.asyncio
async def test_mute_all_saves_then_restores_previous_permissions(test_db, monkeypatch):
    binding = await _create_guardian_binding(test_db, telegram_id=-1007001, chat_type="supergroup")

    class FakeBotClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_chat_permissions(self, _chat_id):
            return {"can_send_messages": True, "can_send_polls": False}

        async def close(self):
            return None

    permission_update = AsyncMock(return_value=True)
    monkeypatch.setattr(managed_groups_module, "TelegramClient", FakeBotClient)
    monkeypatch.setattr(
        managed_groups_module,
        "_assert_bot_admin_permission",
        AsyncMock(return_value={"status": "administrator", "can_restrict_members": True}),
    )
    monkeypatch.setattr(
        managed_groups_module.TelegramExecutionService,
        "set_default_chat_permissions",
        permission_update,
    )

    muted = await set_managed_group_mute_all(
        binding.id,
        ManagedGroupMuteAllRequest(muted=True),
        test_db,
    )
    await test_db.refresh(binding)
    muted_snapshot = json.loads(binding.permissions_snapshot)

    assert muted.data["all_members_muted"] is True
    assert muted_snapshot["permissions_before_mute_all"] == {
        "can_send_messages": True,
        "can_send_polls": False,
    }
    assert permission_update.await_args_list[0].args[2] == _lockdown_permissions()

    restored = await set_managed_group_mute_all(
        binding.id,
        ManagedGroupMuteAllRequest(muted=False),
        test_db,
    )

    assert restored.data["all_members_muted"] is False
    assert permission_update.await_args_list[1].args[2] == {
        "can_send_messages": True,
        "can_send_polls": False,
    }


@pytest.mark.asyncio
async def test_channel_message_requires_channel_binding_and_returns_message_id(
    test_db, monkeypatch
):
    binding = await _create_guardian_binding(test_db, telegram_id=-1007002, chat_type="channel")

    class FakeBotClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def close(self):
            return None

    send_message = AsyncMock(return_value=88)
    monkeypatch.setattr(managed_groups_module, "TelegramClient", FakeBotClient)
    monkeypatch.setattr(
        managed_groups_module,
        "_assert_bot_admin_permission",
        AsyncMock(return_value={"status": "administrator", "can_post_messages": True}),
    )
    monkeypatch.setattr(
        managed_groups_module.TelegramExecutionService, "send_bot_message", send_message
    )

    response = await send_managed_channel_message(
        binding.id,
        ManagedChannelMessageRequest(content="Product update", parse_mode=""),
        test_db,
    )

    assert response.data["message_id"] == 88
    assert send_message.await_args.args[1:3] == (-1007002, "Product update")


@pytest.mark.asyncio
async def test_create_channel_uses_user_session_and_returns_active_binding(test_db, monkeypatch):
    creator = TelegramAccount(
        phone="+15550007003",
        identifier="+15550007003",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="channel_creator",
        session_string="session",
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
    )
    guardian = TelegramAccount(
        phone=None,
        identifier="@channel_create_guardian",
        account_type=AccountType.GUARDIAN_BOT,
        api_config_name="default",
        country_code="US",
        session_name="channel_create_guardian",
        status=AccountStatus.IDLE,
    )
    test_db.add_all([creator, guardian])
    await test_db.flush()
    test_db.add(
        GuardianBotProfile(
            account_id=guardian.id,
            bot_token="123456:test-token",
            bot_username="channel_create_guardian",
            enabled=True,
        )
    )
    await test_db.commit()

    class FakeBotClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_me(self):
            return SimpleNamespace(user_id=9001, username="channel_create_guardian")

        async def close(self):
            return None

    user_request_types = []

    class FakeUserClient:
        async def get_entity(self, username):
            return SimpleNamespace(id=9001, username=username)

        async def __call__(self, request):
            user_request_types.append(type(request).__name__)
            return True

    wrapper = SimpleNamespace(account_id=creator.id, client=FakeUserClient())
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    created_channel = SimpleNamespace(id=7003, title="Updates")
    monkeypatch.setattr(managed_groups_module, "TelegramClient", FakeBotClient)
    monkeypatch.setattr(managed_groups_module, "get_account_pool", lambda: pool)
    monkeypatch.setattr(
        managed_groups_module.TelegramExecutionService,
        "create_channel",
        AsyncMock(return_value=created_channel),
    )
    monkeypatch.setattr("telethon.utils.get_peer_id", lambda _channel: -1007003)

    response = await create_managed_channel(
        ManagedChannelCreateRequest(
            creator_account_id=creator.id,
            bot_account_id=guardian.id,
            title="Updates",
            about="Product news",
            is_public=True,
            username="pipenai_updates",
        ),
        test_db,
    )

    assert response.data["binding"]["chat_type"] == "channel"
    assert response.data["binding"]["binding_status"] == "active"
    assert response.data["bot_assignment_complete"] is True
    assert response.data["binding"]["permissions_snapshot"]["channel_visibility"] == "public"
    assert response.data["binding"]["username"] == "pipenai_updates"
    assert response.data["public_username_complete"] is True
    assert "EditAdminRequest" in user_request_types
    assert "InviteToChannelRequest" not in user_request_types
    pool.acquire_by_id.assert_awaited_once_with(creator.id, purpose="managed_channel_create")
    pool.release.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
async def test_update_channel_username_uses_recorded_creator_account(test_db, monkeypatch):
    creator = TelegramAccount(
        phone="+15550007004",
        identifier="+15550007004",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="channel_username_creator",
        session_string="session",
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(creator)
    await test_db.flush()
    binding = await _create_guardian_binding(test_db, telegram_id=-1007004, chat_type="channel")
    snapshot = json.loads(binding.permissions_snapshot)
    snapshot["creator_account_id"] = creator.id
    binding.permissions_snapshot = json.dumps(snapshot)
    await test_db.commit()

    wrapper = SimpleNamespace(account_id=creator.id, client=SimpleNamespace())
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    update_username = AsyncMock(return_value=True)
    monkeypatch.setattr(managed_groups_module, "get_account_pool", lambda: pool)
    monkeypatch.setattr(
        managed_groups_module.TelegramExecutionService,
        "update_channel_username",
        update_username,
    )

    response = await update_managed_channel_username(
        binding.id,
        ManagedChannelUsernameRequest(username="pipenai_news"),
        test_db,
    )

    assert response.data["username"] == "pipenai_news"
    assert binding.group.username == "pipenai_news"
    update_username.assert_awaited_once_with(wrapper, -1007004, "pipenai_news")
    pool.release.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
async def test_refresh_channel_status_requires_real_bot_post_permission(test_db, monkeypatch):
    binding = await _create_guardian_binding(test_db, telegram_id=-1007005, chat_type="channel")
    snapshot = json.loads(binding.permissions_snapshot)
    snapshot["channel_status_error"] = "stale error"
    binding.permissions_snapshot = json.dumps(snapshot)
    await test_db.commit()

    class FakeBotClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_me(self):
            return SimpleNamespace(user_id=9005, username="guardian_1007005")

        async def get_chat_member(self, _chat_id, _user_id):
            return {"status": "administrator", "can_post_messages": True}

        async def close(self):
            return None

    monkeypatch.setattr(managed_groups_module, "TelegramClient", FakeBotClient)

    response = await refresh_managed_channel_status(binding.id, test_db)

    assert response.data["bot_assignment_complete"] is True
    assert response.data["binding"]["binding_status"] == "active"
    assert response.data["binding"]["bot_role"] == "admin"
    assert "channel_status_error" not in response.data["binding"]["permissions_snapshot"]
