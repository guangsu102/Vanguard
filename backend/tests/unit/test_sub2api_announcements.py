from __future__ import annotations

import pytest

from app.core.automation_settings import save_app_runtime_settings
from app.integrations.sub2api import announcements as announcements_module
from app.integrations.sub2api.announcements import (
    Sub2APIAnnouncementDeliveryError,
    Sub2APIAnnouncementPayload,
    deliver_sub2api_announcement,
    expected_sub2api_announcement_idempotency_key,
    format_sub2api_announcement_message,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


class FakeTelegramClient:
    def __init__(self, *, fail_first_pin: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.pinned: list[tuple[str, int, bool]] = []
        self.fail_first_pin = fail_first_pin

    async def send_message(self, chat_id: str, text: str, **_: object):
        self.sent.append((chat_id, text))
        return type("SentMessage", (), {"message_id": 77})()

    async def pin_chat_message(
        self,
        chat_id: str,
        message_id: int,
        *,
        disable_notification: bool,
    ) -> bool:
        if self.fail_first_pin:
            self.fail_first_pin = False
            raise RuntimeError("temporary pin failure")
        self.pinned.append((chat_id, message_id, disable_notification))
        return True


def _payload() -> Sub2APIAnnouncementPayload:
    return Sub2APIAnnouncementPayload.model_validate(
        {
            "schema_version": "1",
            "type": "announcement.published",
            "source": {
                "system": "sub2api",
                "instance_id": "prod",
                "base_url": "https://pipenai.xyz",
            },
            "event": {
                "id": 42,
                "revision": 3,
                "published_at": "2026-07-16T08:00:00Z",
            },
            "announcement": {
                "id": 42,
                "title": "维护公告",
                "content": "今晚进行系统维护。",
                "audience": "public",
            },
        }
    )


def test_announcement_idempotency_key_is_bound_to_revision() -> None:
    assert (
        expected_sub2api_announcement_idempotency_key(_payload())
        == "prod:announcement:42:published:3"
    )


def test_announcement_message_is_plain_and_contains_site_link() -> None:
    message = format_sub2api_announcement_message(_payload())
    assert message.startswith("[公告] 维护公告")
    assert "今晚进行系统维护" in message
    assert "https://pipenai.xyz" in message


@pytest.mark.asyncio
async def test_telegram_announcement_sends_and_pins_only_once(
    test_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await save_app_runtime_settings(
        test_db,
        {
            "notification": {
                "sub2apiAnnouncementsEnabled": True,
                "telegramAnnouncementsEnabled": True,
                "telegramAnnouncementChatId": "-1001",
                "telegramAnnouncementPin": True,
                "telegramAnnouncementPinSilent": True,
                "qqAnnouncementsEnabled": False,
            }
        },
    )
    telegram = FakeTelegramClient()
    redis = FakeRedis()
    monkeypatch.setattr(announcements_module.settings, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(announcements_module, "get_telegram_client", lambda: telegram)

    key = "prod:announcement:42:published:3"
    first = await deliver_sub2api_announcement(_payload(), key, test_db, redis)
    second = await deliver_sub2api_announcement(_payload(), key, test_db, redis)

    assert first == {"sent": 1, "duplicate": 0, "skipped": 0}
    assert second == {"sent": 0, "duplicate": 1, "skipped": 0}
    assert len(telegram.sent) == 1
    assert telegram.pinned == [("-1001", 77, True)]


@pytest.mark.asyncio
async def test_pin_retry_does_not_send_duplicate_announcement(
    test_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await save_app_runtime_settings(
        test_db,
        {
            "notification": {
                "sub2apiAnnouncementsEnabled": True,
                "telegramAnnouncementsEnabled": True,
                "telegramAnnouncementChatId": "-1001",
                "telegramAnnouncementPin": True,
                "telegramAnnouncementPinSilent": False,
                "qqAnnouncementsEnabled": False,
            }
        },
    )
    telegram = FakeTelegramClient(fail_first_pin=True)
    redis = FakeRedis()
    monkeypatch.setattr(announcements_module.settings, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(announcements_module, "get_telegram_client", lambda: telegram)

    key = "prod:announcement:42:published:3"
    with pytest.raises(Sub2APIAnnouncementDeliveryError, match="pin failure"):
        await deliver_sub2api_announcement(_payload(), key, test_db, redis)

    retried = await deliver_sub2api_announcement(_payload(), key, test_db, redis)
    assert retried == {"sent": 1, "duplicate": 0, "skipped": 0}
    assert len(telegram.sent) == 1
    assert telegram.pinned == [("-1001", 77, False)]
