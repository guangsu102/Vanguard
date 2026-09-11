from __future__ import annotations

import json

from app.modules.owned_group.security import (
    REDACTED,
    REDACTED_INVITE,
    REDACTED_TOKEN,
    is_sensitive_key,
    redact_sensitive_text,
    redact_sensitive_value,
    redact_snapshot_text,
    safe_exception_message,
)


def test_redact_sensitive_text_hides_tokens_and_private_invites_but_keeps_public_links():
    value = (
        "token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd "
        "invite=https://t.me/+AbCdEfGh12345678 "
        "public=https://t.me/my_public_group"
    )
    redacted = redact_sensitive_text(value)
    assert REDACTED_TOKEN in redacted
    assert REDACTED_INVITE in redacted
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd" not in redacted
    assert "+AbCdEfGh12345678" not in redacted
    assert "https://t.me/my_public_group" in redacted


def test_redact_sensitive_value_scrubs_secret_mapping_keys_recursively():
    payload = {
        "token_ciphertext": "vge1:secret",
        "nested": {"session_string": "session-secret", "count": 2},
        "invite_link": "https://t.me/+AbCdEfGh12345678",
    }
    redacted = redact_sensitive_value(payload)
    assert redacted["token_ciphertext"] == REDACTED
    assert redacted["nested"]["session_string"] == REDACTED
    assert redacted["invite_link"] == REDACTED
    assert redacted["nested"]["count"] == 2
    assert is_sensitive_key("bot_token")
    assert not is_sensitive_key("title")


def test_redact_snapshot_text_preserves_json_shape_and_scrubs_values():
    snapshot = json.dumps(
        {
            "batch_size": 5,
            "admin_configs": [{"resource_id": 1, "admin_title": "moderator"}],
            "bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",
        }
    )
    redacted = json.loads(redact_snapshot_text(snapshot) or "{}")
    assert redacted["batch_size"] == 5
    assert redacted["admin_configs"][0]["admin_title"] == "moderator"
    assert redacted["bot_token"] == REDACTED


def test_safe_exception_message_scrubs_composite_secret_aliases():
    raw = (
        "proxy_password=ProxyPlain accessToken=AccessPlain "
        "TELEGRAM_API_HASH=HashPlain jwt_secret=JwtPlain client-secret=ClientPlain"
    )

    redacted = safe_exception_message(RuntimeError(raw))

    for secret in ("ProxyPlain", "AccessPlain", "HashPlain", "JwtPlain", "ClientPlain"):
        assert secret not in redacted
    assert redacted.count(REDACTED) == 5


def test_sensitive_mapping_key_suffixes_cover_password_secret_and_api_hash():
    assert is_sensitive_key("proxyPassword")
    assert is_sensitive_key("jwt_secret")
    assert is_sensitive_key("TELEGRAM_API_HASH")
    assert is_sensitive_key("access_token")
    assert not is_sensitive_key("content_hash")
