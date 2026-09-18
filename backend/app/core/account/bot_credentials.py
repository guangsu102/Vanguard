"""Bot credential helpers with legacy plaintext compatibility.

Guardian bot tokens were historically stored as plaintext.  New writes use the
same encrypted envelope as owned-group workflow secrets, while reads continue
to accept existing rows until they are rotated or migrated.
"""

from __future__ import annotations

from app.core.ephemeral_secret import (
    decrypt_ephemeral_secret,
    encrypt_ephemeral_secret,
    get_ephemeral_secret_service,
)


def encrypt_guardian_bot_token(value: str | None) -> str | None:
    """Encrypt a Guardian Bot token without double-encrypting it."""

    return encrypt_ephemeral_secret(value)


def resolve_guardian_bot_token(value: str | None) -> str | None:
    """Return a usable token from encrypted or legacy plaintext storage."""

    if not value:
        return value
    if get_ephemeral_secret_service().is_encrypted(value):
        return decrypt_ephemeral_secret(value)
    return value


__all__ = ["encrypt_guardian_bot_token", "resolve_guardian_bot_token"]
