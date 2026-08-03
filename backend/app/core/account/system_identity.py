"""Risk identity helpers for system-level Bot API actions."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace


def bot_risk_identity(name: str = "bot_api") -> SimpleNamespace:
    """Return a stable pseudo-account for Bot API risk auditing."""
    # Negative IDs avoid colliding with real TelegramAccount primary keys while
    # still giving Redis and audit code a stable identifier.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    account_id = -(int(digest[:12], 16) % 2_000_000_000 or 1)
    return SimpleNamespace(
        account_id=account_id,
        session_name=name,
        country_code="SYSTEM",
        current_proxy_country="SYSTEM",
        fingerprint_id=f"system:{name}",
        proxy_mode="system",
        static_proxy_id=None,
    )

