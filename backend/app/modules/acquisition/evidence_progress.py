"""Retry hints only: persisted progress never grants permission or proves absence."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.modules.acquisition.group_qualification import looks_like_ad


def date(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime):
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (TypeError, ValueError, OverflowError):
        return None


def evidence_maturity(snapshot: dict, now: datetime) -> datetime | None:
    """Return the next useful live read, including evidence that matured while queued."""
    due = []
    for item in snapshot.get("evidence", []):
        if not (
            item.get("source") == "recent_promotional_message"
            and item.get("sender_role") == "ordinary"
            and type(item.get("sender_id")) is int
            and item["sender_id"] > 0
            and type(item.get("message_id")) is int
            and item["message_id"] > 0
            and item.get("accessible") is True
            and not item.get("system_account")
            and not item.get("topic_id")
            and not item.get("warning_reply_ids")
            and looks_like_ad(str(item.get("text") or ""))
        ):
            continue
        original = date(item.get("date"))
        observed = date(item.get("observed_at")) or date(snapshot.get("collected_at"))
        if original is None or observed is None or observed > now:
            continue
        edited = date(item.get("edited_at")) or original
        anchor = max(original, edited)
        if anchor > observed:
            continue
        maturity = anchor + timedelta(hours=24)
        if observed < maturity and original >= now - timedelta(hours=72):
            due.append(max(now + timedelta(seconds=1), maturity))
    return min(due, default=None)


def progress_retry(snapshot: dict, now: datetime) -> tuple[str, datetime] | None:
    progress = snapshot.get("collection_progress") or {}
    if progress.get("pending_message_ids"):
        return "identity_budget_resume", now + timedelta(minutes=15)
    deadlines = [
        date(item.get("retry_at"))
        for sender, item in progress.get("permission_failures", {}).items()
        if isinstance(item, dict)
        and (snapshot.get("identity_errors") or {}).get(sender) == "ChatAdminRequiredError"
    ]
    deadlines = [item for item in deadlines if item and item > now]
    if deadlines:
        return "identity_permission_backoff", min(deadlines)
    return None
