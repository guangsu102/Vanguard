"""Independent, account-scoped qualification optimizations; no business grants."""
from __future__ import annotations

from app.core.config import settings

_FLAGS = {
    "reuse": "REVIEW_READ_REUSE",
    "resume": "REVIEW_READ_RESUME",
    "demand_identity": "REVIEW_READ_DEMAND_IDENTITY",
    "minimal_plan": "REVIEW_READ_MINIMAL_PLAN",
    "push": "REVIEW_READ_PUSH",
    "fast_path": "REVIEW_READ_FAST_PATH",
    "fact_handoff": "REVIEW_READ_FACT_HANDOFF",
    "dependency_resume": "REVIEW_READ_DEPENDENCY_RESUME",
    "public_share": "REVIEW_READ_PUBLIC_SHARE",
}


def enabled_for(account_id: int | None) -> bool:
    if account_id is None or not settings.REVIEW_READ_OPTIMIZATION_ENABLED:
        return False
    # A malformed allowlist never silently turns into an all-account rollout.
    try:
        allowed = {int(value.strip()) for value in settings.REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS.split(",")
                   if value.strip()}
    except ValueError:
        return False
    return account_id in allowed


def stage_enabled(account_id: int | None, stage: str) -> bool:
    flag = _FLAGS.get(stage)
    return bool(flag and enabled_for(account_id) and getattr(settings, flag, False))


def growth_enabled_for(account_id: int | None) -> bool:
    """Pure settings check usable by legacy-role metrics without engine models."""
    import re

    growth_ids = str(getattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "") or "").strip()
    return bool(getattr(settings, "GROWTH_EFFICIENCY_ENABLED", False) and account_id is not None
            and growth_ids and re.fullmatch(r"\d+(?:\s*,\s*\d+)*", growth_ids)
            and account_id in {int(value.strip()) for value in growth_ids.split(",") if int(value.strip()) > 0})


def measurement_stage(account_id: int | None) -> str:
    # New-engine accounts have an independent rollout allowlist.
    if growth_enabled_for(account_id):
        return "phase2"
    if any(stage_enabled(account_id, stage) for stage in (
        "fast_path", "push", "demand_identity", "fact_handoff", "dependency_resume", "public_share",
    )):
        return "phase2"
    if any(stage_enabled(account_id, stage) for stage in ("reuse", "resume", "minimal_plan", "demand_identity")):
        return "phase1"
    return "baseline"

