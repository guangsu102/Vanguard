"""Safe Telegram SQLite session-file path handling."""

from __future__ import annotations

from pathlib import Path

_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def resolve_telegram_session_file(
    session_dir: str | Path,
    session_name: str,
    *,
    allow_empty: bool = False,
) -> Path:
    """Return a contained session path or reject unsafe/custom path input."""

    raw_name = str(session_name or "")
    name = raw_name.strip()
    if not name:
        if not allow_empty:
            raise ValueError("Telegram session name is required")
        name = ""
    elif (
        raw_name != name
        or len(name) > 128
        or name in {".", ".."}
        or any(not (char.isalnum() or char in {".", "_", "+", "-"}) for char in name)
        or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError("Telegram session name must be a safe file basename")

    root = Path(session_dir).resolve()
    candidate = (root / f"{name}.session").resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Telegram session path escapes its configured directory") from exc
    return candidate


__all__ = ["resolve_telegram_session_file"]
