"""Build a read-only summary of normalized automation limits."""

from __future__ import annotations

from typing import Any


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _source(key: str, value: int, *, active: bool = True) -> dict[str, Any]:
    return {"key": key, "value": value, "active": active}


def _limit(
    key: str,
    unit: str,
    formula: str,
    sources: list[dict[str, Any]],
    dynamic_factors: list[str] | None = None,
) -> dict[str, Any]:
    values = [int(source["value"]) for source in sources if source["active"]]
    value = None
    if values:
        value = min(values) if formula == "min" else max(values)
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "formula": formula,
        "sources": sources,
        "dynamicFactors": dynamic_factors or [],
    }


def build_effective_limit_summary(
    *,
    risk_guard: dict[str, Any],
    ad_execution: dict[str, Any],
    ad_throttle: dict[str, Any],
    ad_capacity: dict[str, Any],
) -> dict[str, Any]:
    """Return only limits that are active in the v2 delivery policy."""

    risk_enabled = bool(risk_guard.get("enabled", True))
    actions = risk_guard.get("actions") if isinstance(risk_guard.get("actions"), dict) else {}
    join_daily = _int_value((actions.get("join") or {}).get("daily_limit"))
    group_message_daily = _int_value((actions.get("group_message") or {}).get("daily_limit"))
    outbound_hard_cap = _int_value(
        risk_guard.get("account_outbound_message_hard_cap_default"), 30
    )

    items = [
        _limit(
            "account_join_daily",
            "count_per_day",
            "min",
            [_source("risk.actions.join.daily_limit", join_daily, active=risk_enabled)],
            [
                "account.max_groups_per_day",
                "asset.join_multiplier",
                "warmup.join_multiplier",
                "risk.level_multiplier",
                "time_window.join_multiplier",
            ],
        ),
        _limit(
            "account_group_message_daily",
            "count_per_day",
            "min",
            [
                _source(
                    "risk.actions.group_message.daily_limit",
                    group_message_daily,
                    active=risk_enabled,
                )
            ],
            [
                "account.max_messages_per_day",
                "asset.action_multiplier",
                "warmup.group_message_multiplier",
                "risk.level_multiplier",
            ],
        ),
        _limit(
            "account_outbound_message_daily_hard_cap",
            "count_per_day",
            "min",
            [
                _source(
                    "risk.account_outbound_message_hard_cap_default",
                    outbound_hard_cap,
                    active=risk_enabled,
                )
            ],
            ["account.max_messages_per_day"],
        ),
        _limit(
            "growth_account_ad_min_interval",
            "seconds",
            "max",
            [
                _source(
                    "ads.throttle.growth_min_interval_seconds",
                    _int_value(ad_throttle.get("growth_min_interval_seconds")),
                )
            ],
        ),
        _limit(
            "growth_group_global_cooldown",
            "seconds",
            "max",
            [
                _source(
                    "ads.execution.growth_group_global_cooldown_seconds",
                    _int_value(ad_execution.get("growth_group_global_cooldown_seconds")),
                )
            ],
            ["group.last_ad_at"],
        ),
    ]

    return {
        "source": "normalized_runtime_settings_v2",
        "riskGuardEnabled": risk_enabled,
        "items": items,
        "accountDynamicFields": {
            "outboundDailyHardCap": "max_messages_per_day",
            "joinDaily": "join_dynamic_daily_limit",
        },
        "retiredLimits": [
            "risk.global_daily_limit",
            "risk.group_write_daily_limit",
            "risk.actions.ad_delivery.daily_limit",
            "campaign.max_sends_per_account_per_day",
            "campaign.max_sends_per_group_per_day",
            "ads.capacity.account_ad_daily_hard_cap",
            "ads.capacity.group_global_daily_hard_cap",
            "ads.capacity.group_min_interval_seconds",
            "ads.capacity.tier_daily_capacities",
            "system.ads.max_deliveries_per_run",
            "system.ads.max_deliveries_per_account_per_run",
            "system.ads.account_group_daily_cap",
        ],
    }
