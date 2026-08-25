from app.core.effective_limits import build_effective_limit_summary


def _summary(*, risk_enabled: bool = True):
    return build_effective_limit_summary(
        risk_guard={
            "enabled": risk_enabled,
            "account_outbound_message_hard_cap_default": 30,
            "actions": {
                "join": {"daily_limit": 6},
                "group_message": {"daily_limit": 4},
            },
        },
        ad_execution={
            "growth_group_global_cooldown_seconds": 86400,
        },
        ad_throttle={
            "growth_min_interval_seconds": 9000,
            "ad_only_min_interval_seconds": 3000,
        },
        ad_capacity={
            "account_ad_daily_hard_cap": 3,
            "group_global_daily_hard_cap": 400,
            "group_min_interval_seconds": 259200,
        },
    )


def _items(summary):
    return {item["key"]: item for item in summary["items"]}


def test_effective_limits_only_report_active_v2_limits():
    summary = _summary()
    items = _items(summary)

    assert items["account_join_daily"]["value"] == 6
    assert items["account_group_message_daily"]["value"] == 4
    assert items["account_outbound_message_daily_hard_cap"]["value"] == 30
    assert items["growth_account_ad_min_interval"]["value"] == 9000
    assert items["growth_group_global_cooldown"]["value"] == 86400

    retired_item_keys = {
        "account_ad_daily",
        "account_ad_per_run",
        "account_group_ad_daily",
        "group_global_ad_daily",
        "group_ad_min_interval",
    }
    assert retired_item_keys.isdisjoint(items)
    assert "ads.capacity.group_global_daily_hard_cap" in summary["retiredLimits"]


def test_disabled_risk_guard_only_disables_risk_guard_limits():
    items = _items(_summary(risk_enabled=False))

    assert items["account_join_daily"]["value"] is None
    assert items["account_group_message_daily"]["value"] is None
    assert items["account_outbound_message_daily_hard_cap"]["value"] is None
    assert items["growth_account_ad_min_interval"]["value"] == 9000
    assert items["growth_group_global_cooldown"]["value"] == 86400