"""P0 safety gates for the self-owned promotional-group module.

The module deliberately keeps safety decisions outside the Telegram adapter.
API handlers and workers can use the same checks before they enqueue or execute
an operation.  No credential, session string, token, or invite URL is ever
returned in a decision detail.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.config import settings
from app.core.redis import RedisCache
from app.modules.owned_group.contracts import ResourceType
from app.modules.owned_group.models_extra import OwnedBotProfile

GLOBAL_STOP_REDIS_KEY = "p0:safety_gate:global_stop"
GLOBAL_STOP_REASON_REDIS_KEY = "p0:safety_gate:global_stop_reason"
GLOBAL_STOP_UPDATED_AT_REDIS_KEY = "p0:safety_gate:global_stop_updated_at"


@dataclass
class SafetyGateDecision:
    """A safe, serializable decision returned by a preflight check."""

    allowed: bool
    reason: str = "allowed"
    details: dict[str, Any] | None = None


@dataclass
class SafetyGateState:
    global_stop: bool = False
    global_stop_reason: str = ""
    global_stop_updated_at: str | None = None
    backend_available: bool = True


# Redis is the durable source of truth in a configured deployment.  The
# fallback is intentionally process-local and only keeps tests/development
# deterministic when Redis has not been initialized; it is never presented as
# durable state in API documentation.
_fallback_state = SafetyGateState()


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _is_future(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        return value > datetime.utcnow()
    return value > datetime.now(UTC)


def _has_usable_user_session(account: TelegramAccount) -> bool:
    """Match the credential forms that AccountPool can actually open.

    ``auth_key_base64`` is retained on the legacy account model, but the
    current AccountPool/Telethon path does not consume it.  Treating it as a
    ready session would let a resource pass precheck and then fail only after a
    worker has claimed it.  Session strings and mounted ``.session`` files are
    the two forms the pool supports today.
    """

    raw_session = str(account.session_string or "").strip()
    if raw_session:
        # AccountPool decrypts ``vgs1:`` values immediately before creating a
        # Telethon StringSession.  Treat an undecryptable ciphertext as missing
        # here so an operation cannot pass precheck and fail only after a worker
        # has claimed a resource.  Legacy plaintext sessions remain supported.
        try:
            from app.core.account.session_crypto import decrypt_session_string

            return bool(str(decrypt_session_string(raw_session) or "").strip())
        except Exception:
            return False
    session_name = str(getattr(account, "session_name", "") or "").strip()
    if not session_name:
        return False
    try:
        session_dir = Path(str(_setting("TELEGRAM_SESSION_DIR", "sessions")))
        return (session_dir / f"{session_name}.session").is_file()
    except (OSError, TypeError, ValueError):
        return False


def _state_with_static_kill_switch(state: SafetyGateState) -> SafetyGateState:
    if not bool(_setting("OWNED_GROUP_KILL_SWITCH_ENABLED", False)):
        return state
    return SafetyGateState(
        global_stop=True,
        global_stop_reason="kill_switch_enabled",
        global_stop_updated_at=state.global_stop_updated_at,
        backend_available=state.backend_available,
    )


async def _redis_cache() -> RedisCache:
    return RedisCache()


async def _read_global_stop_state() -> SafetyGateState:
    """Read runtime stop state, tolerating an uninitialized/unavailable Redis."""

    fallback = _state_with_static_kill_switch(_fallback_state)
    cache = await _redis_cache()
    if cache.client is None:
        return SafetyGateState(
            global_stop=fallback.global_stop,
            global_stop_reason=fallback.global_stop_reason,
            global_stop_updated_at=fallback.global_stop_updated_at,
            backend_available=False,
        )
    try:
        enabled = await cache.get(GLOBAL_STOP_REDIS_KEY)
        reason = await cache.get(GLOBAL_STOP_REASON_REDIS_KEY)
        updated_at = await cache.get(GLOBAL_STOP_UPDATED_AT_REDIS_KEY)
    except Exception:
        # A safety read must not turn an API request into a 500.  Keep the last
        # known local state and let the worker's deployment-level fail-closed
        # policy decide whether to execute when Redis is unavailable.
        return SafetyGateState(
            global_stop=fallback.global_stop,
            global_stop_reason=fallback.global_stop_reason,
            global_stop_updated_at=fallback.global_stop_updated_at,
            backend_available=False,
        )
    if enabled is None:
        # Redis is reachable but the gate has not been initialized yet. Do not
        # inherit a stale ``backend_available=False`` from an earlier outage.
        return SafetyGateState(
            global_stop=fallback.global_stop,
            global_stop_reason=fallback.global_stop_reason,
            global_stop_updated_at=fallback.global_stop_updated_at,
            backend_available=True,
        )
    runtime = SafetyGateState(
        global_stop=str(enabled) == "1",
        global_stop_reason=reason or ("manual_stop" if str(enabled) == "1" else ""),
        global_stop_updated_at=updated_at or None,
        backend_available=True,
    )
    return _state_with_static_kill_switch(runtime)


async def _write_global_stop_state(state: SafetyGateState) -> SafetyGateState:
    """Persist stop state to Redis and retain a development fallback."""

    global _fallback_state
    cache = await _redis_cache()
    persisted = False
    if cache.client is not None:
        try:
            # Redis' transactional pipeline makes stop/resume one atomic state
            # change.  In particular, a partially written resume must never
            # clear the stop bit while leaving the API to report a failure.
            pipeline = cache.client.pipeline(transaction=True)
            pipeline.set(GLOBAL_STOP_REDIS_KEY, "1" if state.global_stop else "0")
            pipeline.set(GLOBAL_STOP_REASON_REDIS_KEY, state.global_stop_reason)
            pipeline.set(
                GLOBAL_STOP_UPDATED_AT_REDIS_KEY,
                state.global_stop_updated_at or "",
            )
            results = await pipeline.execute()
            persisted = all(bool(item) for item in results)
        except Exception:
            persisted = False
    resolved = SafetyGateState(
        global_stop=state.global_stop,
        global_stop_reason=state.global_stop_reason,
        global_stop_updated_at=state.global_stop_updated_at,
        backend_available=persisted,
    )
    _fallback_state = resolved
    # Static kill switch cannot be disabled through the runtime endpoint.
    return _state_with_static_kill_switch(resolved)


async def get_safety_gate_state() -> SafetyGateState:
    return await _read_global_stop_state()


def is_safety_gate_enabled() -> bool:
    return bool(_setting("P0_SAFETY_GATE_ENABLED", True))


async def set_global_stop(
    *, enabled: bool, reason: str, operator: str | None = None
) -> SafetyGateState:
    """Persist the operator's runtime stop request.

    ``operator`` is accepted for audit-layer callers but is intentionally not
    stored in the Redis gate value; the owned-group audit event carries actor
    identity without mixing it into the control key.
    """

    del operator
    state = SafetyGateState(
        global_stop=enabled,
        global_stop_reason=reason.strip() or ("manual_stop" if enabled else ""),
        global_stop_updated_at=datetime.now(UTC).isoformat(),
    )
    return await _write_global_stop_state(state)


def is_owned_group_module_enabled() -> bool:
    return bool(_setting("OWNED_GROUP_MODULE_ENABLED", True))


def is_owned_group_execution_enabled() -> bool:
    return (
        is_owned_group_module_enabled()
        and bool(_setting("OWNED_GROUP_EXECUTION_ENABLED", False))
        and not bool(_setting("OWNED_GROUP_KILL_SWITCH_ENABLED", False))
    )


def require_owned_group_module_enabled() -> None:
    """Raise a stable 503 response when the module write path is disabled."""

    if not is_owned_group_module_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_module_disabled"},
        )


async def require_owned_group_execution_enabled() -> None:
    """Raise before any worker/adapter side effect when execution is stopped."""

    require_owned_group_module_enabled()
    state = await get_safety_gate_state()
    if state.global_stop:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "global_stop_enabled", "details": state.__dict__},
        )
    if not state.backend_available and bool(_setting("P0_SAFETY_GATE_FAIL_CLOSED", False)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "safety_gate_backend_unavailable"},
        )
    if not is_owned_group_execution_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "owned_group_execution_disabled"},
        )


def _account_failure(
    account: TelegramAccount | None,
    *,
    require_promoter: bool = True,
    require_runtime_ready: bool = True,
) -> tuple[str, dict[str, Any]] | None:
    if account is None:
        return "account_not_found", {}
    account_type = _enum_value(account.account_type)
    if require_promoter and account_type != AccountType.PROMOTER.value:
        return "account_type_not_promoter", {"account_type": account_type}
    if not bool(account.is_active):
        return "account_inactive", {}
    risk_level = str(account.risk_level or "normal").strip().lower()
    if risk_level in {"frozen", "quarantined"}:
        return "account_risk_blocked", {"risk_level": risk_level}
    if (
        risk_level == "limited"
        or _is_future(account.risk_pause_until)
        or _is_future(account.risk_recovery_until)
    ):
        return "account_cooldown", {"risk_level": risk_level}
    if not require_runtime_ready:
        return None
    account_status = _enum_value(account.status)
    if account_status not in {AccountStatus.ONLINE.value, AccountStatus.IDLE.value}:
        return "account_status_not_ready", {"status": account_status}
    if not _has_usable_user_session(account):
        return "account_session_missing", {}
    return None


async def precheck_owned_group_resources(
    db: AsyncSession,
    resources: Iterable[Mapping[str, Any]],
    owner_account_id: int,
    *,
    require_execution: bool = False,
    require_runtime_ready: bool = True,
) -> SafetyGateDecision:
    """Validate every selected user/Bot before an owned-group operation.

    ``resource_id`` for ``bot`` is the ``OwnedBotProfile.id`` (never a raw
    token or an arbitrary Telegram account id).  The result contains only
    stable identifiers and status/reason fields, so it is safe to return to
    the UI and write to an audit event.
    """

    if not is_owned_group_module_enabled():
        return SafetyGateDecision(False, "owned_group_module_disabled")
    state = await get_safety_gate_state()
    if state.global_stop:
        return SafetyGateDecision(False, "global_stop_enabled", state.__dict__)
    if (
        require_execution
        and not state.backend_available
        and bool(_setting("P0_SAFETY_GATE_FAIL_CLOSED", False))
    ):
        return SafetyGateDecision(False, "safety_gate_backend_unavailable")
    if require_execution and not is_owned_group_execution_enabled():
        return SafetyGateDecision(False, "owned_group_execution_disabled")
    if not is_safety_gate_enabled():
        return SafetyGateDecision(True, "safety_gate_disabled")

    normalized: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    valid_types = {item.value for item in ResourceType}
    for raw in resources:
        resource_type = _enum_value(raw.get("resource_type", "")).strip().lower()
        try:
            resource_id = int(raw.get("resource_id"))
        except (TypeError, ValueError):
            resource_id = 0
        if resource_type not in valid_types or resource_id <= 0:
            violations.append(
                {
                    "resource_type": resource_type or None,
                    "resource_id": resource_id or None,
                    "reason": "resource_reference_invalid",
                }
            )
            continue
        normalized.append({"resource_type": resource_type, "resource_id": resource_id})

    if not any(
        item["resource_type"] == ResourceType.USER.value
        and item["resource_id"] == int(owner_account_id)
        for item in normalized
    ):
        violations.append(
            {
                "resource_type": ResourceType.USER.value,
                "resource_id": int(owner_account_id),
                "reason": "owner_not_selected",
            }
        )

    user_ids = {
        item["resource_id"]
        for item in normalized
        if item["resource_type"] == ResourceType.USER.value
    }
    bot_profile_ids = {
        item["resource_id"]
        for item in normalized
        if item["resource_type"] == ResourceType.BOT.value
    }
    rollout_limit = int(_setting("OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS", 2))
    if len(user_ids) > rollout_limit:
        violations.append(
            {
                "reason": "rollout_account_limit_exceeded",
                "selected_accounts": len(user_ids),
                "max_accounts": rollout_limit,
            }
        )

    account_ids = set(user_ids)
    if bot_profile_ids:
        profiles = (
            await db.scalars(select(OwnedBotProfile).where(OwnedBotProfile.id.in_(bot_profile_ids)))
        ).all()
    else:
        profiles = []
    profiles_by_id = {int(profile.id): profile for profile in profiles}
    account_ids.update(int(profile.account_id) for profile in profiles)
    accounts: dict[int, TelegramAccount] = {}
    if account_ids:
        account_rows = (
            await db.scalars(select(TelegramAccount).where(TelegramAccount.id.in_(account_ids)))
        ).all()
        accounts = {int(account.id): account for account in account_rows}

    for item in normalized:
        resource_type = item["resource_type"]
        resource_id = item["resource_id"]
        if resource_type == ResourceType.USER.value:
            failure = _account_failure(
                accounts.get(resource_id),
                require_runtime_ready=require_runtime_ready,
            )
            if failure:
                reason, details = failure
                violations.append(
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "reason": reason,
                        **details,
                    }
                )
            continue

        profile = profiles_by_id.get(resource_id)
        if profile is None:
            violations.append(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "reason": "bot_profile_not_found",
                }
            )
            continue
        bot_status = str(profile.status or "").strip().lower()
        if not bool(profile.enabled):
            reason = "bot_profile_disabled"
        elif bot_status not in {"verified", "active"}:
            reason = "bot_profile_not_verified"
        elif not str(profile.token_ciphertext or "").strip():
            reason = "bot_token_missing"
        elif profile.account_id not in accounts:
            reason = "bot_account_not_found"
        elif (
            _enum_value(accounts[profile.account_id].account_type) != AccountType.GUARDIAN_BOT.value
        ):
            # Bot profiles must remain on the dedicated Bot API account type;
            # accepting a promoter session here would mix authentication
            # domains and could make a future worker use a user Session as a
            # Bot API credential.
            reason = "bot_account_type_invalid"
        elif not bool(accounts[profile.account_id].is_active):
            reason = "bot_account_inactive"
        else:
            # A registered bot executes through the Bot API token; it must not
            # be forced to have a user-session credential or Telethon ONLINE
            # status like a promoter account.  Evaluate the shared account
            # guard once so a future guard change cannot produce two divergent
            # decisions for the same resource.
            failure = _account_failure(
                accounts[profile.account_id],
                require_promoter=False,
                require_runtime_ready=False,
            )
            reason = failure[0] if failure else ""
        if reason:
            violations.append(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "reason": reason,
                    "status": bot_status,
                }
            )

    if violations:
        return SafetyGateDecision(
            False,
            "resource_eligibility_failed",
            {
                "violations": violations,
                "selected_accounts": len(user_ids),
                "max_accounts": rollout_limit,
            },
        )
    return SafetyGateDecision(
        True,
        "eligible",
        {
            "selected_resources": len(normalized),
            "selected_accounts": len(user_ids),
            "selected_bots": len(bot_profile_ids),
            "max_accounts": rollout_limit,
        },
    )


async def precheck_account_eligibility(
    db: AsyncSession,
    account_id: int,
    *,
    bot_account_id: int | None = None,
    admin_account_id: int | None = None,
) -> SafetyGateDecision:
    """Backward-compatible single-account safety API.

    ``bot_account_id`` accepts either an ``OwnedBotProfile.id`` (preferred) or
    its linked Telegram account id for older callers.  Admin accounts are
    treated as user resources and therefore must be active promoter accounts.
    """

    resources: list[dict[str, Any]] = [
        {"resource_type": ResourceType.USER.value, "resource_id": account_id}
    ]
    if admin_account_id is not None:
        resources.append(
            {"resource_type": ResourceType.USER.value, "resource_id": admin_account_id}
        )
    if bot_account_id is not None:
        profile = await db.scalar(
            select(OwnedBotProfile).where(OwnedBotProfile.id == bot_account_id)
        )
        if profile is None:
            profile = await db.scalar(
                select(OwnedBotProfile).where(OwnedBotProfile.account_id == bot_account_id)
            )
        if profile is None:
            return SafetyGateDecision(False, "bot_profile_not_found")
        resources.append({"resource_type": ResourceType.BOT.value, "resource_id": int(profile.id)})
    decision = await precheck_owned_group_resources(db, resources, account_id)
    # Keep the legacy single-account endpoint's reason shape while retaining
    # the aggregate violation payload for the owned-group operation API.
    if not decision.allowed and decision.reason == "resource_eligibility_failed":
        violations = (decision.details or {}).get("violations") or []
        if len(violations) == 1 and violations[0].get("reason"):
            return SafetyGateDecision(
                False,
                str(violations[0]["reason"]),
                {"violations": violations},
            )
    return decision


def require_global_stop_reason(reason: str) -> None:
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="global stop reason is required",
        )


__all__ = [
    "GLOBAL_STOP_REDIS_KEY",
    "SafetyGateDecision",
    "SafetyGateState",
    "get_safety_gate_state",
    "is_owned_group_execution_enabled",
    "is_owned_group_module_enabled",
    "is_safety_gate_enabled",
    "precheck_account_eligibility",
    "precheck_owned_group_resources",
    "require_global_stop_reason",
    "require_owned_group_execution_enabled",
    "require_owned_group_module_enabled",
    "set_global_stop",
]
