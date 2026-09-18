"""Security helpers for the self-owned group orchestration boundary.

The owned-group worker and its audit API handle values that may originate from
Telegram exceptions or adapter payloads.  Those values must never put session
material, bot tokens, or complete invite URLs into logs/audit responses.  This
module intentionally contains only deterministic, dependency-free redaction
helpers so it can be used from both async workers and HTTP handlers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
REDACTED_TOKEN = "[REDACTED_TOKEN]"
REDACTED_INVITE = "[REDACTED_INVITE_LINK]"

# Telegram Bot API tokens are commonly rendered as ``123456789:AA...`` or as
# the ``bot<token>`` segment of a Bot API URL.  Keep the expression deliberately
# conservative so normal numeric IDs and ordinary prose are not hidden.
_BOT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:bot)?\d{5,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_])"
)

# Only private invite forms are secret.  A public username/link is intentionally
# retained for normal operational context; ``+hash`` and ``joinchat`` links are
# bearer credentials and must be removed in their entirety.
_INVITE_LINK_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?:\+[A-Za-z0-9_-]{8,128}|joinchat/[A-Za-z0-9_-]{8,128})"
    r"(?:\?[A-Za-z0-9_=&%-]*)?"
)
_TG_INVITE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])tg://join\?invite=[A-Za-z0-9_-]{8,128}(?![A-Za-z0-9_])"
)
_URI_CREDENTIAL_RE = re.compile(
    r"(?i)\b((?:https?|socks[45]?)://)[^\s/:@]+:[^\s/@]+@"
)

# Keys are compared after normalizing punctuation/case.  The list includes
# aliases used by SQL models, Telethon, Bot API, and common exception payloads.
_SECRET_KEY_PARTS = {
    "token",
    "bottoken",
    "tokenciphertext",
    "session",
    "sessionstring",
    "authkey",
    "authkeybase64",
    "invite",
    "invitelink",
    "linkciphertext",
    "access_token",
    "accesstoken",
    "apikey",
    "password",
    "proxypassword",
    "api_hash",
    "telegram_api_hash",
    "secret",
}

_SECRET_KEY_SUFFIXES = {
    "token",
    "password",
    "secret",
    "tokenciphertext",
    "sessionstring",
    "authkeybase64",
    "invitelink",
    "apihash",
}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:(?:telegram|proxy|access|client|jwt)[_ -]?)?"
    r"(?:api[_ -]?(?:key|hash)|bot[_ -]?token|access[_ -]?token|password|secret|"
    r"session(?:[_ -]?string)?|auth[_ -]?key(?:[_ -]?base64)?|invite[_ -]?link))"
    r"\s*[:=]\s*([^\s,;]+)"
)


def _normalized_key(key: object) -> str:
    # Treat snake_case, kebab-case, and camel-ish aliases uniformly.
    return re.sub(r"[^a-z0-9]", "", str(key).strip().lower())


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key conventionally contains secret material."""

    normalized = _normalized_key(key)
    if normalized in {part.replace("_", "") for part in _SECRET_KEY_PARTS}:
        return True
    return any(normalized.endswith(part) for part in _SECRET_KEY_SUFFIXES)


def redact_sensitive_text(value: object, *, max_length: int = 2000) -> str:
    """Redact bearer credentials from arbitrary text and bound its size.

    ``str(exc)`` is intentionally accepted here because third-party Telegram
    exceptions are not guaranteed to expose structured fields.  The returned
    value is suitable for ``error_message``, structured logs, and audit fields.
    """

    text = str(value or "")
    text = _BOT_TOKEN_RE.sub(REDACTED_TOKEN, text)
    text = _INVITE_LINK_RE.sub(REDACTED_INVITE, text)
    text = _TG_INVITE_RE.sub(REDACTED_INVITE, text)
    text = _URI_CREDENTIAL_RE.sub(rf"\1{REDACTED}@", text)
    # Avoid accidentally persisting a long opaque value introduced by a
    # ``session=``/``token=`` query or exception string.  Keep the key and hide
    # only the value, without changing unrelated prose.
    text = _SECRET_ASSIGNMENT_RE.sub(rf"\1={REDACTED}", text)
    if len(text) > max_length:
        return text[: max(0, max_length - 3)] + "..."
    return text


def redact_sensitive_value(value: Any, *, max_length: int = 2000) -> Any:
    """Recursively redact secret keys and secret-looking strings.

    Mapping keys are retained (they are useful to operators), while values of
    known secret fields are replaced.  Lists/tuples are preserved as lists so
    the result remains JSON serializable for API responses.
    """

    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if is_sensitive_key(key)
                else redact_sensitive_value(item, max_length=max_length)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_value(item, max_length=max_length) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, max_length=max_length)
    return value


def redact_snapshot_text(value: str | None, *, max_length: int = 2000) -> str | None:
    """Redact a JSON snapshot when possible, falling back to text redaction."""

    if value is None:
        return None
    raw = str(value)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return redact_sensitive_text(raw, max_length=max_length)
    redacted = redact_sensitive_value(parsed, max_length=max_length)
    try:
        encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return redact_sensitive_text(raw, max_length=max_length)
    return redact_sensitive_text(encoded, max_length=max_length)


def safe_exception_message(exc: BaseException | object, *, max_length: int = 2000) -> str:
    """Return a bounded, credential-free exception message."""

    return redact_sensitive_text(exc, max_length=max_length)


__all__ = [
    "REDACTED",
    "REDACTED_INVITE",
    "REDACTED_TOKEN",
    "is_sensitive_key",
    "redact_sensitive_text",
    "redact_sensitive_value",
    "redact_snapshot_text",
    "safe_exception_message",
]
