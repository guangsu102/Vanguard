"""Encryption helpers for short-lived workflow secrets."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

EPHEMERAL_SECRET_PREFIX = "vge1:"


class EphemeralSecretError(ValueError):
    """Raised when an encrypted workflow secret cannot be decrypted."""


class EphemeralSecretService:
    """Encrypt short-lived values without exposing them through API payloads."""

    def __init__(self, key_material: str | None = None):
        settings = get_settings()
        material = (
            key_material
            or settings.TELEGRAM_SESSION_ENCRYPTION_KEY
            or settings.JWT_SECRET
        )
        digest = hashlib.sha256(f"ephemeral:{material}".encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    @staticmethod
    def is_encrypted(value: str | None) -> bool:
        return bool(value and value.startswith(EPHEMERAL_SECRET_PREFIX))

    def encrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        if self.is_encrypted(value):
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return f"{EPHEMERAL_SECRET_PREFIX}{token}"

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        if not self.is_encrypted(value):
            raise EphemeralSecretError("Workflow secret is not encrypted")
        token = value[len(EPHEMERAL_SECRET_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise EphemeralSecretError(
                "Workflow secret could not be decrypted"
            ) from exc


_default_service: EphemeralSecretService | None = None


def get_ephemeral_secret_service() -> EphemeralSecretService:
    global _default_service
    if _default_service is None:
        _default_service = EphemeralSecretService()
    return _default_service


def encrypt_ephemeral_secret(value: str | None) -> str | None:
    return get_ephemeral_secret_service().encrypt(value)


def decrypt_ephemeral_secret(value: str | None) -> str | None:
    return get_ephemeral_secret_service().decrypt(value)
