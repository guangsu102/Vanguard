import pytest

from app.integrations.telegram.client import TelegramClient, TelegramConfig
from app.modules.guardian.broadcast.broadcaster import GuardianBroadcaster


@pytest.mark.asyncio
async def test_telegram_client_pin_chat_message_sends_bot_api_params(monkeypatch):
    client = TelegramClient(TelegramConfig(bot_token="test-token"))
    calls = []

    async def fake_request(method, params=None, **_kwargs):
        calls.append((method, params))
        return True

    monkeypatch.setattr(client, "_request", fake_request)

    pinned = await client.pin_chat_message(-100123, 42, disable_notification=False)

    assert pinned is True
    assert calls == [
        (
            "pinChatMessage",
            {
                "chat_id": -100123,
                "message_id": 42,
                "disable_notification": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_telegram_client_send_message_can_omit_parse_mode(monkeypatch):
    client = TelegramClient(TelegramConfig(bot_token="test-token"))
    calls = []

    async def fake_request(method, params=None, **_kwargs):
        calls.append((method, params))
        return {"message_id": 7, "chat": {"id": -100123, "type": "supergroup"}}

    monkeypatch.setattr(client, "_request", fake_request)

    sent = await client.send_message(-100123, "plain_text_with_underscores", parse_mode="")

    assert sent.message_id == 7
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["text"] == "plain_text_with_underscores"
    assert "parse_mode" not in calls[0][1]


@pytest.mark.asyncio
async def test_telegram_client_reads_and_sets_default_chat_permissions(monkeypatch):
    client = TelegramClient(TelegramConfig(bot_token="test-token"))
    calls = []

    async def fake_request(method, params=None, **_kwargs):
        calls.append((method, params))
        if method == "getChat":
            return {"permissions": {"can_send_messages": True, "can_send_polls": False}}
        return True

    monkeypatch.setattr(client, "_request", fake_request)

    current = await client.get_chat_permissions(-100123)
    changed = await client.set_chat_permissions(-100123, {"can_send_messages": False})

    assert current == {"can_send_messages": True, "can_send_polls": False}
    assert changed is True
    assert calls[-1] == (
        "setChatPermissions",
        {
            "chat_id": -100123,
            "permissions": {"can_send_messages": False},
            "use_independent_chat_permissions": True,
        },
    )


@pytest.mark.asyncio
async def test_guardian_broadcaster_send_pinned_message_sends_then_pins():
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, message, **kwargs):
            self.calls.append(("send_message", chat_id, message, kwargs))
            return type("SentMessage", (), {"message_id": 88})()

        async def pin_chat_message(self, chat_id, message_id, **kwargs):
            self.calls.append(("pin_chat_message", chat_id, message_id, kwargs))
            return True

    fake_client = FakeClient()
    broadcaster = GuardianBroadcaster(db=None, telegram_client=fake_client)

    result = await broadcaster.send_pinned_message(
        -100123,
        "公告",
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=False,
    )

    assert result.success is True
    assert result.message_id == 88
    assert fake_client.calls == [
        (
            "send_message",
            -100123,
            "公告",
            {"parse_mode": "HTML", "disable_web_page_preview": True},
        ),
        (
            "pin_chat_message",
            -100123,
            88,
            {"disable_notification": False},
        ),
    ]
