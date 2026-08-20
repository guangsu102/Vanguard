from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings
from app.core.security import create_access_token, verify_access_token


def _token(payload: dict) -> str:
    return jwt.encode(
        payload,
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def test_verify_access_token_returns_payload_for_valid_token():
    payload = verify_access_token(_token({"sub": "123", "role": "admin"}))

    assert payload is not None
    assert payload["sub"] == "123"
    assert payload["role"] == "admin"


def test_verify_access_token_rejects_missing_subject():
    assert verify_access_token(_token({"role": "admin"})) is None


def test_verify_access_token_rejects_invalid_token():
    assert verify_access_token("not-a-jwt") is None


def test_access_token_default_expiration_uses_configured_hours(monkeypatch):
    monkeypatch.setattr(settings, "JWT_EXPIRATION_HOURS", 2)
    issued_at = datetime.now(timezone.utc)

    token = create_access_token({"sub": "123"})
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
    expires_at = datetime.fromtimestamp(payload["exp"], timezone.utc)
    assert timedelta(hours=2, seconds=-1) <= expires_at - issued_at <= timedelta(hours=2, seconds=1)
