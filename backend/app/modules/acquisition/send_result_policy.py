"""Send-result policy. No message-history reads or survival evidence."""
from __future__ import annotations

from datetime import datetime, timedelta

POLICY = "send_result_v1"
COUNTED = ("success", "unknown", "sending")
GROUP_ERRORS = frozenset({"ChatWriteForbiddenError", "ChatRestrictedError",
                         "UserBannedInChannelError", "ChatSendPlainForbiddenError",
                         "ChannelPrivateError", "ChatForbiddenError", "ChatAdminRequiredError"})
CONTENT_ERRORS = frozenset({"MessageEmptyError", "MessageTooLongError", "EntitiesTooLongError",
                           "EntityBoundsInvalidError", "MessageEntityInvalidError"})
LEGACY_EXITS = frozenset({"permanent_mute", "temporary_mute", "deleted_at_minimum"})
EXIT_REASON = "send_failure_threshold"


def classify(exc: Exception) -> str:
    name = type(exc).__name__
    if name in GROUP_ERRORS:
        return "permission_changed"
    if name == "UserNotParticipantError":
        return "not_member"
    if name in CONTENT_ERRORS:
        return "content_invalid"
    if name in {"SlowModeWaitError", "FloodWaitError", "FloodPremiumWaitError"}:
        return "rate_limited"
    if isinstance(exc, (TimeoutError, OSError)) or name in {
        "ServerError", "RpcCallFailError", "TimedOutError", "InterdcCallErrorError",
    }:
        return "unknown"
    return "failed"


def next_due(sent: datetime, gap: int, outcome: str, retry: int, wait: int = 0) -> datetime:
    return sent + timedelta(seconds=max(gap, retry if outcome == "permission_changed" else 0, wait))
