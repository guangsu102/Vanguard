from datetime import datetime, timedelta, timezone

import jwt

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from unittest.mock import AsyncMock, MagicMock

from app.api.auth import LoginRequest, login
from app.core.config import settings
from app.core.security import create_access_token, get_current_user, verify_access_token


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


@pytest.mark.asyncio
async def test_get_current_user_rejects_inactive_user():
    row_result = MagicMock()
    row_result.fetchone.return_value = (
        7,
        "disabled-admin",
        "admin",
        None,
        None,
        datetime.utcnow(),
        False,
    )
    db = AsyncMock()
    db.execute.return_value = row_result
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=create_access_token({"sub": "7"}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user(credentials=credentials, db=db)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "User not found or inactive"


@pytest.mark.asyncio
async def test_login_rejects_inactive_user_before_issuing_token():
    row_result = MagicMock()
    row_result.fetchone.return_value = (
        8,
        "disabled-admin",
        "unused-password-hash",
        "admin",
        None,
        None,
        False,
    )
    db = AsyncMock()
    db.execute.return_value = row_result

    with pytest.raises(HTTPException) as exc_info:
        await login(LoginRequest(username="disabled-admin", password="secret"), db=db)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "用户名或密码错误"
