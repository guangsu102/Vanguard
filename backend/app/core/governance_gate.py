"""Independent runtime gate for self-owned group Guardian governance.

This gate intentionally does not read or mutate the P0 owned-group global
stop. Provisioning and Guardian governance are separate execution domains:
callers may always read governance state, while bind, reconcile, and owned-
group Guardian event paths must call
``require_owned_group_governance_available`` before performing work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.redis import RedisCache

GOVERNANCE_STOP_REDIS_KEY = "owned_group:governance:stop"
GOVERNANCE_STOP_REASON_REDIS_KEY = "owned_group:governance:stop_reason"
GOVERNANCE_STOP_UPDATED_AT_REDIS_KEY = "owned_group:governance:stop_updated_at"


@dataclass
class GovernanceGateState:
    """Safe state exposed to API and worker callers."""

    governance_stop: bool = False
    reason: str = ""
    updated_at: str | None = None
    backend_available: bool = True


# Redis is the deployment source of truth. The process-local fallback keeps
# tests and development deterministic and preserves the last operator choice
# during a transient Redis outage; ``backend_available`` makes durability
# explicit to callers.
_fallback_state = GovernanceGateState()


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _enabled_value(value: Any) -> bool:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


async def _redis_cache() -> RedisCache:
    return RedisCache()


def is_owned_group_governance_enabled() -> bool:
    """Return the static opt-in flag without consulting runtime stop state."""

    return bool(_setting("OWNED_GROUP_GOVERNANCE_ENABLED", False))


async def get_governance_gate_state() -> GovernanceGateState:
    """Read governance stop state without enforcing the feature flag.

    GET/status paths may call this while the feature is disabled. No state is
    written and no Telegram request is made.
    """

    fallback = _fallback_state
    cache = await _redis_cache()
    if cache.client is None:
        return GovernanceGateState(
            governance_stop=fallback.governance_stop,
            reason=fallback.reason,
            updated_at=fallback.updated_at,
            backend_available=False,
        )

    try:
        enabled = await cache.get(GOVERNANCE_STOP_REDIS_KEY)
        reason = await cache.get(GOVERNANCE_STOP_REASON_REDIS_KEY)
        updated_at = await cache.get(GOVERNANCE_STOP_UPDATED_AT_REDIS_KEY)
    except Exception:
        return GovernanceGateState(
            governance_stop=fallback.governance_stop,
            reason=fallback.reason,
            updated_at=fallback.updated_at,
            backend_available=False,
        )

    if enabled is None:
        return GovernanceGateState(
            governance_stop=fallback.governance_stop,
            reason=fallback.reason,
            updated_at=fallback.updated_at,
            backend_available=True,
        )

    stopped = _enabled_value(enabled)
    return GovernanceGateState(
        governance_stop=stopped,
        reason=str(reason or ("manual_stop" if stopped else "")),
        updated_at=str(updated_at) if updated_at else None,
        backend_available=True,
    )


async def _write_governance_gate_state(
    state: GovernanceGateState,
) -> GovernanceGateState:
    global _fallback_state

    cache = await _redis_cache()
    persisted = False
    if cache.client is not None:
        try:
            pipeline = cache.client.pipeline(transaction=True)
            pipeline.set(
                GOVERNANCE_STOP_REDIS_KEY,
                "1" if state.governance_stop else "0",
            )
            pipeline.set(GOVERNANCE_STOP_REASON_REDIS_KEY, state.reason)
            pipeline.set(GOVERNANCE_STOP_UPDATED_AT_REDIS_KEY, state.updated_at or "")
            results = await pipeline.execute()
            persisted = len(results) == 3 and all(bool(item) for item in results)
        except Exception:
            persisted = False

    resolved = GovernanceGateState(
        governance_stop=state.governance_stop,
        reason=state.reason,
        updated_at=state.updated_at,
        backend_available=persisted,
    )
    _fallback_state = resolved
    return resolved


async def set_governance_stop(
    *, enabled: bool, reason: str, operator: str | None = None
) -> GovernanceGateState:
    """Persist an operator governance stop/resume decision independently."""

    del operator
    normalized_reason = str(reason or "").strip()[:255]
    if not normalized_reason:
        normalized_reason = "manual_stop" if enabled else "manual_resume"
    return await _write_governance_gate_state(
        GovernanceGateState(
            governance_stop=bool(enabled),
            reason=normalized_reason,
            updated_at=datetime.now(UTC).isoformat(),
        )
    )


async def require_owned_group_governance_available() -> GovernanceGateState:
    """Reject mutating/event paths when governance is disabled or stopped."""

    if not is_owned_group_governance_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "governance_feature_disabled",
                "message": "Owned-group Guardian governance is disabled",
                "retryable": False,
            },
        )

    state = await get_governance_gate_state()
    if not state.backend_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "governance_gate_backend_unavailable",
                "message": "Owned-group Guardian governance gate is unavailable",
                "retryable": True,
            },
        )
    if state.governance_stop:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "governance_stop_enabled",
                "message": "Owned-group Guardian governance is stopped",
                "retryable": True,
                "stop_reason": state.reason,
                "updated_at": state.updated_at,
            },
        )
    return state


__all__ = [
    "GOVERNANCE_STOP_REASON_REDIS_KEY",
    "GOVERNANCE_STOP_REDIS_KEY",
    "GOVERNANCE_STOP_UPDATED_AT_REDIS_KEY",
    "GovernanceGateState",
    "get_governance_gate_state",
    "is_owned_group_governance_enabled",
    "require_owned_group_governance_available",
    "set_governance_stop",
]
