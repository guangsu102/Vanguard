import jwt

from app.core.config import settings
from app.core.security import verify_access_token


def _token(payload: dict) -> str:
    return jwt.encode(
        payload,
        settings.JWT_SECRET or settings.SECRET_KEY,
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
