"""Encryption helpers for persisted Telegram StringSession values."""

from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

SESSION_CIPHER_PREFIX = "vgs1:"


class SessionCryptoError(ValueError):
    """Raised when an encrypted session cannot be decrypted."""


class SessionCryptoService:
    """Encrypt and decrypt Telegram session strings with legacy plaintext fallback."""

    def __init__(self, key_material: Optional[str] = None):
        settings = get_settings()
        self.key_material = key_material or settings.TELEGRAM_SESSION_ENCRYPTION_KEY or settings.SECRET_KEY
        digest = hashlib.sha256(self.key_material.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @staticmethod
    def is_encrypted(value: Optional[str]) -> bool:
        return bool(value and value.startswith(SESSION_CIPHER_PREFIX))

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        if self.is_encrypted(value):
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return f"{SESSION_CIPHER_PREFIX}{token}"

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        if not self.is_encrypted(value):
            return value
        token = value[len(SESSION_CIPHER_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise SessionCryptoError("Telegram session string could not be decrypted") from exc


_default_service: SessionCryptoService | None = None


def get_session_crypto_service() -> SessionCryptoService:
    global _default_service
    if _default_service is None:
        _default_service = SessionCryptoService()
    return _default_service


def encrypt_session_string(value: Optional[str]) -> Optional[str]:
    return get_session_crypto_service().encrypt(value)


def decrypt_session_string(value: Optional[str]) -> Optional[str]:
    return get_session_crypto_service().decrypt(value)
