"""
Runtime settings helpers shared by API and background modules.

The persisted settings file is intentionally lightweight JSON so runtime
workers can enforce admin switches without needing a database migration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SETTINGS_FILE = Path("/app/uploads/settings.json")


def load_runtime_settings() -> dict[str, Any]:
    """Load raw persisted runtime settings."""
    if not SETTINGS_FILE.exists():
        return {}

    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def is_ai_reply_enabled() -> bool:
    """Return True only when admins explicitly enable AI auto replies."""
    ai_reply = load_runtime_settings().get("aiReply", {})
    if not isinstance(ai_reply, dict):
        return False
    return bool(ai_reply.get("enabled", False))
