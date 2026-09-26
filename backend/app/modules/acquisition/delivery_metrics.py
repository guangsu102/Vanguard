"""Classify scheduling deferrals without rewriting the original delivery ledger."""

from app.core.account.rpc_governor import deferred_error


def pre_request_deferred(error: str | None, message_id: int | None = None) -> bool:
    """A receipt or an unknown send outcome must never be hidden as a local wait."""
    return message_id is None and error is not None and deferred_error(error) is not None
