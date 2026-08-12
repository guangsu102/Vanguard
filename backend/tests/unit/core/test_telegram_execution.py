from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.telegram_execution import TelegramExecutionService


class FakeClient:
    def __init__(self) -> None:
        self.send_message_calls = []
        self.send_file_calls = []
        self.request_calls = []
        self.get_entity_called = False

    async def send_message(self, *args, **kwargs):
        self.send_message_calls.append((args, kwargs))
        return SimpleNamespace(id=321, message_id=321)

    async def send_file(self, *args, **kwargs):
        self.send_file_calls.append((args, kwargs))
        return SimpleNamespace(id=654, message_id=654)

    async def delete_message(self, *args, **kwargs):
        self.send_message_calls.append(("delete", args, kwargs))
        return True

    async def pin_chat_message(self, *args, **kwargs):
        self.send_message_calls.append(("pin", args, kwargs))
        return True

    async def restrict_chat_member(self, *args, **kwargs):
        self.send_message_calls.append(("restrict", args, kwargs))
        return True

    async def set_chat_permissions(self, *args, **kwargs):
        self.send_message_calls.append(("set_chat_permissions", args, kwargs))
        return True

    async def ban_chat_member(self, *args, **kwargs):
        self.send_message_calls.append(("ban", args, kwargs))
        return True

    async def unban_chat_member(self, *args, **kwargs):
        self.send_message_calls.append(("unban", args, kwargs))
        return True

    async def get_entity(self, target):
        self.get_entity_called = True
        return SimpleNamespace(
            id=100, title="Group", username=target, broadcast=False, megagroup=True
        )

    async def __call__(self, request):
        self.request_calls.append(request)
        return None


@pytest.mark.asyncio
async def test_auto_join_blocked_by_risk_guard_does_not_touch_telegram_client():
    client = FakeClient()
    account = SimpleNamespace(account_id=1, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(
            return_value=SimpleNamespace(allowed=False, reason="join_frozen")
        ),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    with pytest.raises(RuntimeError, match="risk_guard_blocked:join_frozen"):
        await service.join_group(account, SimpleNamespace(username="demo_group", group_id=1001))

    assert client.get_entity_called is False


@pytest.mark.asyncio
async def test_ad_delivery_blocked_by_risk_guard_does_not_send_message():
    client = FakeClient()
    account = SimpleNamespace(account_id=2, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(
            return_value=SimpleNamespace(allowed=False, reason="ad_delivery_daily_budget")
        ),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    with pytest.raises(RuntimeError, match="risk_guard_blocked:ad_delivery_daily_budget"):
        await service.send_ad(account, 123, "hello", source="test")
    assert client.send_message_calls == []
    assert client.send_file_calls == []


@pytest.mark.asyncio
async def test_private_message_blocked_by_risk_guard_does_not_send_message():
    client = FakeClient()
    account = SimpleNamespace(account_id=3, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(
            return_value=SimpleNamespace(allowed=False, reason="private_message_daily_budget")
        ),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    with pytest.raises(RuntimeError, match="risk_guard_blocked:private_message_daily_budget"):
        await service.send_private_message(account, 999, "hello", source="test")
    assert client.send_message_calls == []


@pytest.mark.asyncio
async def test_flood_wait_records_failure_and_freezes_account():
    client = FakeClient()
    account = SimpleNamespace(account_id=4, client=client)
    guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_success=AsyncMock(),
        record_failure=AsyncMock(),
    )
    service = TelegramExecutionService(guard)

    class FloodWaitError(RuntimeError):
        seconds = 120

    client.send_message = AsyncMock(side_effect=FloodWaitError("Flood wait"))

    with pytest.raises(FloodWaitError):
        await service.send_private_message(account, 1, "hello", source="test")

    guard.record_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_moderation_blocked_by_risk_guard_does_not_touch_telegram_client():
    client = FakeClient()
    account = SimpleNamespace(account_id=5, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(
            return_value=SimpleNamespace(allowed=False, reason="moderation_daily_budget")
        ),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    with pytest.raises(RuntimeError, match="risk_guard_blocked:moderation_daily_budget"):
        await service.delete_message(account, 100, 55, source="test")

    assert client.send_message_calls == []


@pytest.mark.asyncio
async def test_delete_message_supports_telethon_delete_messages_api():
    client = SimpleNamespace(delete_messages=AsyncMock(return_value=True))
    account = SimpleNamespace(account_id=6, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    deleted = await service.delete_message(account, "@target_group", 55, source="test")

    assert deleted is True
    client.delete_messages.assert_awaited_once_with("@target_group", [55], revoke=True)
    risk_guard.record_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_pin_uses_execution_layer_and_calls_pin():
    client = FakeClient()
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    reply_markup = {
        "inline_keyboard": [[{"text": "Join channel", "url": "https://t.me/PipenAIChannel"}]]
    }
    message_id = await service.send_pinned_bot_message(
        client, 100, "hello", reply_markup=reply_markup, source="test"
    )

    assert message_id == 321
    assert ("pin", (100, 321), {"disable_notification": True}) in client.send_message_calls
    assert client.send_message_calls[0][0] == (100, "hello")
    assert client.send_message_calls[0][1]["reply_markup"] == reply_markup


@pytest.mark.asyncio
async def test_update_profile_bio_uses_telegram_profile_request():
    client = FakeClient()
    account = SimpleNamespace(account_id=6, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    ok = await service.update_profile_bio(account, "AI tools learner", source="test")

    assert ok is True
    assert client.request_calls
    assert getattr(client.request_calls[0], "about", None) == "AI tools learner"
    risk_guard.check_and_reserve.assert_awaited_once()
    risk_guard.record_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_default_chat_permissions_uses_moderation_guard():
    client = FakeClient()
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    changed = await service.set_default_chat_permissions(
        client,
        -100123,
        {"can_send_messages": False},
        source="test_mute_all",
    )

    assert changed is True
    assert (
        "set_chat_permissions",
        (-100123, {"can_send_messages": False}),
        {"use_independent_chat_permissions": True},
    ) in client.send_message_calls
    risk_guard.record_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_channel_uses_broadcast_channel_request():
    channel = SimpleNamespace(id=123, title="News", broadcast=True, megagroup=False)

    class ChannelClient(FakeClient):
        async def __call__(self, request):
            self.request_calls.append(request)
            return SimpleNamespace(chats=[channel])

    client = ChannelClient()
    account = SimpleNamespace(account_id=7, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    created = await service.create_channel(account, "News", about="Updates")

    assert created is channel
    request = client.request_calls[0]
    assert request.title == "News"
    assert request.about == "Updates"
    assert request.broadcast is True
    assert request.megagroup is False
    risk_guard.record_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_channel_username_uses_owner_session():
    class UsernameClient(FakeClient):
        async def __call__(self, request):
            self.request_calls.append(request)
            return True

    client = UsernameClient()
    account = SimpleNamespace(account_id=8, client=client)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard)

    updated = await service.update_channel_username(account, -100123, "pipenai_news")

    assert updated is True
    assert client.get_entity_called is True
    request = client.request_calls[-1]
    assert request.username == "pipenai_news"
    risk_guard.record_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_leave_group_by_id_resolves_entity_before_leaving():
    entity = SimpleNamespace(id=123, megagroup=True)
    client = SimpleNamespace(get_input_entity=AsyncMock(return_value=entity))
    account = SimpleNamespace(account_id=9, client=client)
    service = TelegramExecutionService()
    service.leave_group = AsyncMock()

    await service.leave_group_by_id(account, -100123, source="test_write_forbidden")

    client.get_input_entity.assert_awaited_once_with(-100123)
    service.leave_group.assert_awaited_once_with(
        account,
        entity,
        group_id=-100123,
        source="test_write_forbidden",
    )
