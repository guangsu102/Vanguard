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


def _combined_value(sources: list[dict[str, Any]], formula: str) -> int | None:
    values = [int(source["value"]) for source in sources if source["active"]]
    if not values:
        return None
    return min(values) if formula == "min" else max(values)


def _limit(
    key: str,
    unit: str,
    formula: str,
    sources: list[dict[str, Any]],
    dynamic_factors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "value": _combined_value(sources, formula),
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
    """Return static hard limits and the factors that can lower them at runtime."""
    risk_enabled = bool(risk_guard.get("enabled", True))
    actions = risk_guard.get("actions") if isinstance(risk_guard.get("actions"), dict) else {}

    global_daily = _int_value(risk_guard.get("global_daily_limit"))
    group_write_daily = _int_value(risk_guard.get("group_write_daily_limit"))
    join_daily = _int_value((actions.get("join") or {}).get("daily_limit"))
    group_message_daily = _int_value((actions.get("group_message") or {}).get("daily_limit"))
    ad_daily = _int_value((actions.get("ad_delivery") or {}).get("daily_limit"))

    items = [
        _limit(
            "account_join_daily",
            "count_per_day",
            "min",
            [
                _source("risk.global_daily_limit", global_daily, active=risk_enabled),
                _source("risk.actions.join.daily_limit", join_daily, active=risk_enabled),
            ],
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
                _source("risk.global_daily_limit", global_daily, active=risk_enabled),
                _source("risk.group_write_daily_limit", group_write_daily, active=risk_enabled),
                _source("risk.actions.group_message.daily_limit", group_message_daily, active=risk_enabled),
            ],
            [
                "asset.action_multiplier",
                "warmup.group_message_multiplier",
                "risk.level_multiplier",
            ],
        ),
        _limit(
            "account_ad_daily",
            "count_per_day",
            "min",
            [
                _source("risk.global_daily_limit", global_daily, active=risk_enabled),
                _source("risk.actions.ad_delivery.daily_limit", ad_daily, active=risk_enabled),
                _source(
                    "ads.capacity.account_ad_daily_hard_cap",
                    _int_value(ad_capacity.get("account_ad_daily_hard_cap")),
                ),
            ],
            [
                "campaign.max_sends_per_account_per_day",
                "account.max_messages_per_day",
                "asset.ad_multiplier",
                "warmup.ad_multiplier",
                "risk.level_multiplier",
                "account.health_score",
                "probe.quality_multiplier",
                "time_window.ad_multiplier",
            ],
        ),
        _limit(
            "account_ad_per_run",
            "count_per_run",
            "min",
            [
                _source(
                    "ads.execution.max_deliveries_per_run",
                    _int_value(ad_execution.get("max_deliveries_per_run")),
                ),
                _source(
                    "ads.execution.max_deliveries_per_account_per_run",
                    _int_value(ad_execution.get("max_deliveries_per_account_per_run")),
                ),
            ],
            ["account.health_score", "warmup.run_multiplier", "risk.level_multiplier"],
        ),
        _limit(
            "account_group_ad_daily",
            "count_per_day",
            "min",
            [
                _source(
                    "ads.capacity.account_group_daily_cap_default",
                    _int_value(ad_capacity.get("account_group_daily_cap_default")),
                )
            ],
            ["campaign.max_sends_per_group_per_day", "group.ad_tier", "group.ad_policy"],
        ),
        _limit(
            "group_global_ad_daily",
            "count_per_day",
            "min",
            [
                _source(
                    "ads.capacity.group_global_daily_hard_cap",
                    _int_value(ad_capacity.get("group_global_daily_hard_cap")),
                )
            ],
            ["group.tier_daily_capacity", "group.evidence_capacity", "group.ad_policy"],
        ),
        _limit(
            "account_ad_min_interval",
            "seconds",
            "max",
            [
                _source(
                    "ads.throttle.delivery_interval_seconds",
                    _int_value(ad_throttle.get("delivery_interval_seconds")),
                ),
                _source(
                    "ads.throttle.cooldown_min_seconds",
                    _int_value(ad_throttle.get("cooldown_min_seconds")),
                ),
            ],
        ),
        _limit(
            "group_ad_min_interval",
            "seconds",
            "max",
            [
                _source(
                    "ads.capacity.group_min_interval_seconds",
                    _int_value(ad_capacity.get("group_min_interval_seconds")),
                ),
                _source(
                    "ads.execution.group_campaign_cooldown_minutes",
                    _int_value(ad_execution.get("group_campaign_cooldown_minutes")) * 60,
                ),
            ],
            ["campaign.interval_minutes", "group.last_ad_at"],
        ),
    ]

    return {
        "source": "normalized_runtime_settings",
        "riskGuardEnabled": risk_enabled,
        "items": items,
        "accountDynamicFields": {
            "adDaily": "dynamic_daily_limit",
            "adPerRun": "dynamic_run_limit",
            "joinDaily": "join_dynamic_daily_limit",
        },
    }
