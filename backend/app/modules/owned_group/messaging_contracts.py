"""Stable domain contracts for stage-two owned-group messaging.

The HTTP layer, trigger producers, content generator and execution worker import
from this module.  Keep it free of SQLAlchemy models so those components can be
tested without initializing the database metadata.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from enum import Enum, StrEnum
from typing import Any


class _StringEnum(StrEnum):
    def __str__(self) -> str:
        return self.value


class MessageMode(_StringEnum):
    AI = "ai"
    TEMPLATE = "template"
    OFF = "off"


class MessageTriggerType(_StringEnum):
    SCHEDULED = "scheduled"
    KEYWORD = "keyword"
    REPLY = "reply"
    MANUAL = "manual"


class MessageContentCategory(_StringEnum):
    COMMUNITY = "community"
    PROMOTION = "promotion"


class MessagePurpose(_StringEnum):
    COMMUNITY_AI = "community_ai"
    TEMPLATE = "template"


class MessageExecutionStatus(_StringEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    PENDING_REVIEW = "pending_review"
    READY_TO_SEND = "ready_to_send"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        MessageExecutionStatus.SENT.value,
        MessageExecutionStatus.SKIPPED.value,
        MessageExecutionStatus.FAILED.value,
        MessageExecutionStatus.REJECTED.value,
        MessageExecutionStatus.EXPIRED.value,
        MessageExecutionStatus.CANCELLED.value,
    }
)

RESERVING_EXECUTION_STATUSES = frozenset(
    {
        MessageExecutionStatus.PENDING_REVIEW.value,
        MessageExecutionStatus.READY_TO_SEND.value,
        MessageExecutionStatus.SENDING.value,
        MessageExecutionStatus.SENT.value,
    }
)

UNSENT_CANCELLABLE_STATUSES = frozenset(
    {
        MessageExecutionStatus.QUEUED.value,
        MessageExecutionStatus.GENERATING.value,
        MessageExecutionStatus.PENDING_REVIEW.value,
        MessageExecutionStatus.READY_TO_SEND.value,
    }
)

EXECUTION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    MessageExecutionStatus.QUEUED.value: frozenset({MessageExecutionStatus.GENERATING.value}),
    MessageExecutionStatus.GENERATING.value: frozenset(
        {
            MessageExecutionStatus.PENDING_REVIEW.value,
            MessageExecutionStatus.READY_TO_SEND.value,
            MessageExecutionStatus.SKIPPED.value,
            MessageExecutionStatus.FAILED.value,
        }
    ),
    MessageExecutionStatus.PENDING_REVIEW.value: frozenset(
        {
            MessageExecutionStatus.READY_TO_SEND.value,
            MessageExecutionStatus.REJECTED.value,
            MessageExecutionStatus.EXPIRED.value,
            MessageExecutionStatus.CANCELLED.value,
        }
    ),
    MessageExecutionStatus.READY_TO_SEND.value: frozenset(
        {
            MessageExecutionStatus.SENDING.value,
            MessageExecutionStatus.SKIPPED.value,
            MessageExecutionStatus.CANCELLED.value,
        }
    ),
    MessageExecutionStatus.SENDING.value: frozenset(
        {
            MessageExecutionStatus.SENT.value,
            MessageExecutionStatus.READY_TO_SEND.value,
            MessageExecutionStatus.FAILED.value,
        }
    ),
}

MESSAGE_AUDIT_EVENT_TYPES = frozenset(
    {
        "message_policy_created",
        "message_policy_updated",
        "message_policy_enabled",
        "message_policy_disabled",
        "message_preview_generated",
        "message_execution_created",
        "message_review_approved",
        "message_review_rejected",
        "message_execution_sent",
        "message_execution_skipped",
        "message_execution_failed",
    }
)

ALLOWED_OWNED_GROUP_TEMPLATE_VARIABLES = frozenset(
    {
        "group_name",
        "account_name",
        "user_name",
        "current_date",
        "current_time",
        "promotion_url",
        "promotion_cta",
    }
)

OWNED_GROUP_MESSAGING_RUNTIME_DEFAULTS: Mapping[str, int | bool] = {
    "enabled": False,
    "dry_run": True,
    "global_group_daily_limit": 20,
    "global_account_daily_limit": 30,
    "min_group_cooldown_seconds": 300,
    "dedupe_window_seconds": 21600,
    "review_ttl_hours": 24,
    "max_attempts": 3,
}


def default_trigger_config() -> dict[str, Any]:
    """Return a new safe trigger configuration (never a shared mutable value)."""
    return {
        "version": 1,
        "scheduled": {
            "enabled": False,
            "timezone": "Asia/Shanghai",
            "weekdays": [1, 2, 3, 4, 5, 6, 7],
            "times": [],
            "jitter_seconds": 0,
            "content_category": MessageContentCategory.COMMUNITY.value,
        },
        "keyword": {
            "enabled": False,
            "trigger_ids": [],
            "reply_to_source": True,
            "content_category": MessageContentCategory.COMMUNITY.value,
        },
        "reply": {
            "enabled": False,
            "strategy": "directed",
            "semantic_min_confidence": 0.75,
            "context_messages": 6,
            "content_category": MessageContentCategory.COMMUNITY.value,
        },
        "manual": {
            "enabled": True,
            "allowed_content_categories": [
                MessageContentCategory.COMMUNITY.value,
                MessageContentCategory.PROMOTION.value,
            ],
        },
        "dedupe_window_seconds": 21600,
    }


def default_promotion_config() -> dict[str, Any]:
    return {
        "mode": MessageMode.OFF.value,
        "default_template_id": None,
        "destination_url": None,
        "cta_text": None,
    }


class OwnedGroupMessagingError(Exception):
    """Expected domain failure with a stable HTTP/error contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = dict(details or {})
        self.retryable = retryable
        self.correlation_id = correlation_id

    def to_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"([!?.,，。！？；;：:])\1+")


def normalize_message_content(content: str) -> str:
    """Canonical form used for execution de-duplication."""
    value = unicodedata.normalize("NFKC", content)
    value = _ZERO_WIDTH_RE.sub("", value)
    value = "".join(ch.lower() if ch.isascii() else ch for ch in value)
    value = _WHITESPACE_RE.sub(" ", value).strip()
    return _PUNCTUATION_RE.sub(r"\1", value)


def hash_message_content(content: str) -> str:
    return hashlib.sha256(normalize_message_content(content).encode("utf-8")).hexdigest()


def value_of(value: Any) -> Any:
    """Return an enum's wire value while accepting already-serialized values."""
    return value.value if isinstance(value, Enum) else value
