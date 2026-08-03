"""Managed-account warmup policy helpers.

This module is intentionally pure: callers provide a normalized policy, an
account-like object, and the current time. It returns the warmup context used by
both dynamic acquisition frequency and the account risk guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Optional

from app.core.account.models import AccountAssetTier, AccountType, AccountWarmupStage


WARMUP_STAGE_ORDER = (
    AccountWarmupStage.OBSERVE.value,
    AccountWarmupStage.SEED.value,
    AccountWarmupStage.SOFT.value,
    AccountWarmupStage.RAMP.value,
    AccountWarmupStage.NORMAL.value,
)

ACTION_MULTIPLIER_KEYS = {
    "join": "join_multiplier",
    "ad_delivery": "ad_multiplier",
    "ad_probe": "probe_multiplier",
    "ad_run": "run_multiplier",
    "private_message": "private_message_multiplier",
    "group_message": "group_message_multiplier",
    "ad_probe": "probe_multiplier",
    "ai_warmup": "group_message_multiplier",
    "profile_update": "profile_update_multiplier",
}


@dataclass(frozen=True)
class AccountWarmupContext:
    enabled: bool
    stage: str
    managed_started_at: Optional[datetime]
    managed_age_days: int
    warmup_days: int
    remaining_days: int
    limit_multiplier: float
    action_multiplier: float
    allow_proactive_private_message: bool
    reason: str

    @property
    def is_limited(self) -> bool:
        return self.enabled and self.stage not in {AccountWarmupStage.NORMAL.value}

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "stage": self.stage,
            "managed_started_at": self.managed_started_at.isoformat() if self.managed_started_at else None,
            "managed_age_days": self.managed_age_days,
            "warmup_days": self.warmup_days,
            "remaining_days": self.remaining_days,
            "limit_multiplier": self.limit_multiplier,
            "action_multiplier": self.action_multiplier,
            "allow_proactive_private_message": self.allow_proactive_private_message,
            "reason": self.reason,
        }


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _float_value(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _account_asset_tier(account: Any) -> str:
    raw = _enum_value(getattr(account, "asset_tier", AccountAssetTier.UNKNOWN.value))
    valid = {tier.value for tier in AccountAssetTier}
    return raw if raw in valid else AccountAssetTier.UNKNOWN.value


def _is_promoter_account(account: Any) -> bool:
    raw = _enum_value(getattr(account, "account_type", AccountType.PROMOTER.value))
    return raw == AccountType.PROMOTER.value


def account_warmup_days(policy: dict[str, Any], account: Any) -> int:
    tiers = policy.get("tiers") if isinstance(policy.get("tiers"), dict) else {}
    tier_policy = tiers.get(_account_asset_tier(account), {})
    if not isinstance(tier_policy, dict):
        tier_policy = {}
    default_days = int(policy.get("default_warmup_days", 15) or 15)
    minimum_days = int(policy.get("minimum_warmup_days", 0) or 0)
    try:
        days = int(tier_policy.get("warmup_days", default_days))
    except (TypeError, ValueError):
        days = default_days
    return max(0, max(minimum_days, days))


def account_managed_started_at(account: Any, now: datetime) -> Optional[datetime]:
    if account is None:
        return None
    started_at = getattr(account, "managed_started_at", None)
    if started_at is not None:
        return started_at
    created_at = getattr(account, "created_at", None)
    if created_at is not None:
        return created_at
    return now


def account_managed_age_days(account: Any, now: datetime) -> int:
    started_at = account_managed_started_at(account, now)
    if started_at is None:
        return 0
    return max(0, int((now - started_at).total_seconds() // 86400))


def account_warmup_stage(policy: dict[str, Any], account: Any, now: datetime) -> str:
    if account is None or not policy.get("enabled", True) or not _is_promoter_account(account):
        return AccountWarmupStage.NORMAL.value
    if getattr(account, "risk_pause_until", None) and account.risk_pause_until > now:
        return AccountWarmupStage.COOLDOWN.value
    if _enum_value(getattr(account, "status", "")) in {"error", "banned"}:
        return AccountWarmupStage.COOLDOWN.value
    if _enum_value(getattr(account, "risk_level", "")) in {"frozen", "quarantined"}:
        return AccountWarmupStage.COOLDOWN.value

    total_days = account_warmup_days(policy, account)
    if total_days <= 0:
        return AccountWarmupStage.NORMAL.value

    age_days = account_managed_age_days(account, now)
    if age_days >= total_days:
        hold_until = getattr(account, "warmup_hold_until", None)
        if hold_until and hold_until > now:
            return AccountWarmupStage.RAMP.value
        return AccountWarmupStage.NORMAL.value

    observe_until = min(1, total_days)
    seed_until = min(total_days, max(observe_until + 1, math.ceil(total_days * 0.2)))
    soft_until = min(total_days, max(seed_until + 1, math.ceil(total_days * 0.45)))

    if age_days < observe_until:
        return AccountWarmupStage.OBSERVE.value
    if age_days < seed_until:
        return AccountWarmupStage.SEED.value
    if age_days < soft_until:
        return AccountWarmupStage.SOFT.value
    return AccountWarmupStage.RAMP.value


def _stage_policy(policy: dict[str, Any], stage: str) -> dict[str, Any]:
    stages = policy.get("stages") if isinstance(policy.get("stages"), dict) else {}
    item = stages.get(stage) or stages.get(AccountWarmupStage.NORMAL.value) or {}
    return item if isinstance(item, dict) else {}


def account_warmup_context(
    policy: dict[str, Any],
    account: Any,
    now: datetime,
    *,
    action: Any = None,
    details: Optional[dict[str, Any]] = None,
) -> AccountWarmupContext:
    enabled = bool(policy.get("enabled", True))
    stage = account_warmup_stage(policy, account, now)
    stage_policy = _stage_policy(policy, stage)
    started_at = account_managed_started_at(account, now)
    warmup_days = account_warmup_days(policy, account) if enabled and account is not None else 0
    age_days = account_managed_age_days(account, now)
    remaining_days = max(0, warmup_days - age_days)

    limit_multiplier = _float_value(stage_policy.get("limit_multiplier"), 1.0)
    action_value = _enum_value(action)
    multiplier_key = ACTION_MULTIPLIER_KEYS.get(action_value, "limit_multiplier")
    action_multiplier = _float_value(stage_policy.get(multiplier_key), limit_multiplier)
    allow_proactive_private_message = bool(stage_policy.get("allow_proactive_private_message", stage == AccountWarmupStage.NORMAL.value))

    if action_value == "private_message" and (details or {}).get("initiated_by_user"):
        action_multiplier = max(
            action_multiplier,
            _float_value(policy.get("user_initiated_private_message_multiplier"), 1.0),
        )

    reason = "account_warmup_normal"
    if stage == AccountWarmupStage.COOLDOWN.value:
        reason = "account_warmup_cooldown"
    elif stage != AccountWarmupStage.NORMAL.value:
        reason = f"account_warmup_{stage}"

    return AccountWarmupContext(
        enabled=enabled,
        stage=stage,
        managed_started_at=started_at,
        managed_age_days=age_days,
        warmup_days=warmup_days,
        remaining_days=remaining_days,
        limit_multiplier=limit_multiplier,
        action_multiplier=action_multiplier,
        allow_proactive_private_message=allow_proactive_private_message,
        reason=reason,
    )


def account_warmup_block_reason(context: AccountWarmupContext, action: Any, details: Optional[dict[str, Any]] = None) -> Optional[str]:
    if not context.enabled:
        return None
    action_value = _enum_value(action)
    if action_value == "private_message" and (details or {}).get("initiated_by_user"):
        return None
    if action_value == "private_message" and not context.allow_proactive_private_message:
        return f"account_warmup_{context.stage}_proactive_private_blocked"
    if context.action_multiplier <= 0:
        return f"account_warmup_{context.stage}_{action_value}_blocked"
    return None
