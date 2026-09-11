"""Retention-safe helpers for owned-group message generation inputs.

The execution table only keeps the structured summary produced here. Raw
message text, member names and template values are authenticated-encrypted
before entering Redis and expire after the generation worker consumes them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import structlog

from app.core.ephemeral_secret import (
    EphemeralSecretError,
    EphemeralSecretService,
    get_ephemeral_secret_service,
)
from app.core.redis import RedisCache
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError

logger = structlog.get_logger()

PROMPT_CONTEXT_TTL_SECONDS = 3600
PROMPT_CONTEXT_MAX_TTL_SECONDS = 8 * 24 * 3600
ENCRYPTED_CONTEXT_VERSION = 1
_ENCRYPTED_CONTEXT_FIELD = "ciphertext"
_EPHEMERAL_FIELDS = frozenset(
    {
        "instruction",
        "matched_keyword",
        "recent_context",
        "source_text",
        "user_name",
        "variables",
    }
)


def summarize_prompt_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop raw prompt inputs while retaining non-sensitive execution metadata."""

    context = dict(value or {})
    if not context:
        return {}
    summary = {
        key: context[key]
        for key in (
            "keyword_requires_review",
            "request_fingerprint",
            "scheduled_slot",
            "strategy",
            "topic",
            "generation_policy_snapshot",
            "business_snapshot_v1",
        )
        if key in context
    }
    summary.update(
        {
            "instruction_present": bool(context.get("instruction")),
            "matched_keyword_present": bool(context.get("matched_keyword")),
            "recent_context_message_count": (
                len(context.get("recent_context") or ())
                if isinstance(context.get("recent_context"), (list, tuple))
                else 0
            ),
            "source_message_present": bool(context.get("source_text")),
            "user_name_present": bool(context.get("user_name")),
            "variable_keys": (
                sorted(str(key)[:64] for key in context["variables"])
                if isinstance(context.get("variables"), Mapping)
                else []
            ),
        }
    )
    return summary


def requires_ephemeral_prompt_context(value: Mapping[str, Any] | None) -> bool:
    """Return whether generation needs a raw value that must not enter SQL."""

    context = dict(value or {})
    return any(context.get(key) not in (None, "", (), [], {}) for key in _EPHEMERAL_FIELDS)


def summary_requires_ephemeral_prompt_context(value: Mapping[str, Any] | None) -> bool:
    """Detect a persisted summary whose raw companion must be loaded."""

    context = dict(value or {})
    return any(
        (
            bool(context.get("instruction_present")),
            bool(context.get("matched_keyword_present")),
            bool(context.get("source_message_present")),
            bool(context.get("user_name_present")),
            bool(context.get("recent_context_message_count")),
            bool(context.get("variable_keys")),
        )
    )


class EphemeralContextPayloadError(ValueError):
    """Raised when a Redis context envelope is plaintext, malformed or tampered."""


def encrypt_ephemeral_context(
    value: Any,
    *,
    secret_service: EphemeralSecretService | None = None,
) -> dict[str, Any]:
    """Return a versioned authenticated ciphertext safe for persistent Redis."""

    service = secret_service or get_ephemeral_secret_service()
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    ciphertext = service.encrypt(serialized)
    if not ciphertext:
        raise EphemeralContextPayloadError("Encrypted context payload is empty")
    return {
        "version": ENCRYPTED_CONTEXT_VERSION,
        _ENCRYPTED_CONTEXT_FIELD: ciphertext,
    }


def decrypt_ephemeral_context(
    value: Any,
    *,
    secret_service: EphemeralSecretService | None = None,
) -> Any:
    """Authenticate and decode one versioned Redis context envelope."""

    if not isinstance(value, Mapping):
        raise EphemeralContextPayloadError("Context payload is not encrypted")
    if value.get("version") != ENCRYPTED_CONTEXT_VERSION:
        raise EphemeralContextPayloadError("Context payload version is unsupported")
    ciphertext = value.get(_ENCRYPTED_CONTEXT_FIELD)
    if not isinstance(ciphertext, str):
        raise EphemeralContextPayloadError("Context ciphertext is missing")
    service = secret_service or get_ephemeral_secret_service()
    try:
        serialized = service.decrypt(ciphertext)
        return json.loads(serialized or "")
    except (EphemeralSecretError, json.JSONDecodeError) as exc:
        raise EphemeralContextPayloadError("Context ciphertext is invalid") from exc


class OwnedGroupPromptContextStore:
    """Short-lived Redis storage for raw generation inputs.

    Redis is a safety dependency for executions that need raw context.  A
    missing or unavailable value fails closed instead of falling back to SQL.
    """

    def __init__(
        self,
        cache: RedisCache | None = None,
        *,
        secret_service: EphemeralSecretService | None = None,
    ) -> None:
        self.cache = cache or RedisCache()
        self.secret_service = secret_service or get_ephemeral_secret_service()
        self.logger = logger.bind(module="owned_group_prompt_context")

    @staticmethod
    def key(execution_id: int) -> str:
        return f"owned_group:message:prompt_context:{int(execution_id)}"

    async def save(
        self,
        execution_id: int,
        context: Mapping[str, Any],
        *,
        ttl_seconds: int = PROMPT_CONTEXT_TTL_SECONDS,
    ) -> None:
        ttl = max(1, min(int(ttl_seconds), PROMPT_CONTEXT_MAX_TTL_SECONDS))
        try:
            encrypted = encrypt_ephemeral_context(
                dict(context),
                secret_service=self.secret_service,
            )
            stored = await self.cache.set_json(
                self.key(execution_id),
                encrypted,
                ttl=ttl,
            )
        except Exception as exc:
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_UNAVAILABLE",
                "消息生成上下文临时存储不可用",
                http_status=503,
                retryable=True,
            ) from exc
        if not stored:
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_UNAVAILABLE",
                "消息生成上下文临时存储不可用",
                http_status=503,
                retryable=True,
            )

    async def load(self, execution_id: int) -> dict[str, Any]:
        try:
            value = await self.cache.get_json(self.key(execution_id))
        except Exception as exc:
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_UNAVAILABLE",
                "消息生成上下文临时存储不可用",
                http_status=503,
                retryable=True,
            ) from exc
        if value is None:
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_EXPIRED",
                "消息生成上下文已过期，请重新触发",
                http_status=422,
            )
        try:
            decrypted = decrypt_ephemeral_context(
                value,
                secret_service=self.secret_service,
            )
        except EphemeralContextPayloadError as exc:
            await self.discard(execution_id)
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_INVALID",
                "消息生成上下文无效，请重新触发",
                http_status=422,
            ) from exc
        if not isinstance(decrypted, Mapping):
            await self.discard(execution_id)
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_INVALID",
                "消息生成上下文无效，请重新触发",
                http_status=422,
            )
        return dict(decrypted)

    async def discard(self, execution_id: int) -> bool:
        """Best-effort cleanup; TTL remains the privacy backstop."""

        try:
            await self.cache.delete(self.key(execution_id))
            return True
        except Exception as exc:
            self.logger.warning(
                "owned_group_prompt_context_cleanup_failed",
                execution_id=int(execution_id),
                error_type=type(exc).__name__,
            )
            return False


__all__ = [
    "ENCRYPTED_CONTEXT_VERSION",
    "EphemeralContextPayloadError",
    "OwnedGroupPromptContextStore",
    "PROMPT_CONTEXT_MAX_TTL_SECONDS",
    "PROMPT_CONTEXT_TTL_SECONDS",
    "decrypt_ephemeral_context",
    "encrypt_ephemeral_context",
    "requires_ephemeral_prompt_context",
    "summarize_prompt_context",
    "summary_requires_ephemeral_prompt_context",
]
