"""Recover a legacy receipt namespace only from that exact Telegram message."""

from datetime import UTC, datetime
from typing import Any

from app.modules.acquisition.qualification_identity import entity_identity, peer_identity


def verified_receipt_identity(log: Any, entity: Any, message: Any, own_id: int) -> dict:
    actual = entity_identity(entity)
    stored = peer_identity(log.telegram_group_id)
    if (
        actual is None
        or stored is None
        or actual[0] != stored[0]
        or (stored[1] is not None and stored[1] != actual[1])
    ):
        raise ValueError("receipt_peer_mismatch")
    if (
        log.status != "success"
        or not log.telegram_message_id
        or message is None
        or getattr(message, "id", None) != log.telegram_message_id
        or entity_identity(getattr(message, "peer_id", None)) != actual
    ):
        raise ValueError("receipt_message_mismatch")
    if getattr(message, "sender_id", None) != own_id:
        raise ValueError("receipt_sender_mismatch")
    sent = log.sent_at
    observed = getattr(message, "date", None)
    if not isinstance(sent, datetime) or not isinstance(observed, datetime):
        raise ValueError("receipt_time_missing")
    sent = sent.replace(tzinfo=UTC) if sent.tzinfo is None else sent
    observed = observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed
    if abs((observed - sent).total_seconds()) > 120:
        raise ValueError("receipt_time_mismatch")
    return {
        "telegram_group_id": -actual[0] - (1_000_000_000_000 if actual[1] == "channel" else 0),
        "raw_peer_id": actual[0],
        "group_type": (
            "supergroup"
            if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
            else "channel"
            if actual[1] == "channel"
            else "basic_group"
        ),
        "receipt_identity_evidence": {
            "version": 1,
            "source": "telegram_exact_message_readback",
            "log_id": log.id,
            "account_id": log.account_id,
            "message_id": message.id,
            "message_date": observed.isoformat(),
            "checked_at": datetime.now(UTC).isoformat(),
        },
    }
