from __future__ import annotations

import hashlib
import hmac

import pytest

from app.core.automation_settings import save_app_runtime_settings
from app.integrations.sub2api import alerts as alerts_module
from app.integrations.sub2api.alerts import (
    Sub2APIAlertPayload,
    Sub2APIAlertSignatureError,
    deliver_sub2api_alert,
    expected_sub2api_alert_idempotency_key,
    format_sub2api_alert_message,
    parse_telegram_chat_ids,
    verify_sub2api_alert_signature,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str, **_: object) -> None:
        self.sent.append((chat_id, text))


def _payload() -> Sub2APIAlertPayload:
    return Sub2APIAlertPayload.model_validate(
        {
            "schema_version": "1",
            "source": {
                "system": "sub2api",
                "instance_id": "prod",
                "base_url": "https://pipenai.xyz",
            },
            "event": {
                "id": 12,
                "transition": "firing",
                "status": "firing",
                "severity": "P1",
                "title": "P1: 分组容量预警",
                "description": "group_rate_limit_ratio >= 65",
                "fired_at": "2026-07-16T00:00:00Z",
            },
            "rule": {
                "id": 7,
                "name": "分组容量预警",
                "window_minutes": 1,
                "sustained_minutes": 2,
            },
            "scope": {"platform": "openai", "group_id": 101},
            "metric": {
                "type": "group_rate_limit_ratio",
                "operator": ">=",
                "threshold": 65,
                "value": 65,
                "unit": "percent",
                "numerator": 13,
                "denominator": 20,
            },
        }
    )


def test_verify_sub2api_alert_signature_accepts_valid_hmac() -> None:
    secret = "s" * 32
    timestamp = "1000"
    body = b'{"schema_version":"1"}'
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()

    verify_sub2api_alert_signature(
        body,
        timestamp,
        f"sha256={signature}",
        secret,
        now=1000,
        tolerance_seconds=300,
    )


def test_verify_sub2api_alert_signature_rejects_expired_timestamp() -> None:
    with pytest.raises(Sub2APIAlertSignatureError, match="outside"):
        verify_sub2api_alert_signature(
            b"{}",
            "1000",
            "sha256=bad",
            "s" * 32,
            now=1400,
            tolerance_seconds=300,
        )


def test_format_alert_message_contains_group_metric_and_exact_threshold() -> None:
    message = format_sub2api_alert_message(_payload())

    assert "分组 #101" in message
    assert "分组限流比例" in message
    assert "当前：13/20 = 65.00%" in message
    assert "阈值：>= 65.00%" in message


def test_parse_telegram_chat_ids_supports_multiple_configured_targets() -> None:
    assert parse_telegram_chat_ids("-1001, -1002，-1001") == ["-1001", "-1002"]


def test_idempotency_key_is_derived_from_signed_payload() -> None:
    assert expected_sub2api_alert_idempotency_key(_payload()) == "prod:12:firing"


@pytest.mark.asyncio
async def test_delivery_sends_only_once_per_configured_telegram_target(
    test_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await save_app_runtime_settings(
        test_db,
        {
            "notification": {
                "sub2apiAlertsEnabled": True,
                "sub2apiNotifyResolved": True,
                "telegramEnabled": True,
                "telegramChatId": "-1001, -1002",
                "qqEnabled": False,
            }
        },
    )
    telegram = FakeTelegramClient()
    redis = FakeRedis()
    monkeypatch.setattr(alerts_module.settings, "BOT_TOKEN", "test-token")
    monkeypatch.setattr(alerts_module, "get_telegram_client", lambda: telegram)

    first = await deliver_sub2api_alert(_payload(), "prod:12:firing", test_db, redis)
    second = await deliver_sub2api_alert(_payload(), "prod:12:firing", test_db, redis)

    assert first == {"sent": 2, "duplicate": 0, "skipped": 0}
    assert second == {"sent": 0, "duplicate": 2, "skipped": 0}
    assert [chat_id for chat_id, _ in telegram.sent] == ["-1001", "-1002"]
