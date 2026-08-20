from app.core.effective_limits import build_effective_limit_summary


def _summary(*, risk_enabled: bool = True):
    return build_effective_limit_summary(
        risk_guard={
            "enabled": risk_enabled,
            "global_daily_limit": 30,
            "group_write_daily_limit": 8,
            "actions": {
                "join": {"daily_limit": 6},
                "group_message": {"daily_limit": 4},
                "ad_delivery": {"daily_limit": 5},
            },
        },
        ad_execution={
            "group_campaign_cooldown_minutes": 4320,
        },
        ad_throttle={
            "delivery_interval_seconds": 9000,
            "cooldown_min_seconds": 10800,
        },
        ad_capacity={
            "account_ad_daily_hard_cap": 3,
            "group_global_daily_hard_cap": 400,
            "group_min_interval_seconds": 259200,
        },
    )


def _items(summary):
    return {item["key"]: item for item in summary["items"]}


def test_effective_limits_combine_caps_with_correct_formula():
    items = _items(_summary())

    assert items["account_join_daily"]["value"] == 6
    assert items["account_group_message_daily"]["value"] == 4
    assert items["account_ad_daily"]["value"] == 3
    assert items["account_ad_per_run"]["value"] == 1
    assert {
        source["key"] for source in items["account_ad_per_run"]["sources"]
    } == {"system.ads.max_deliveries_per_run", "system.ads.max_deliveries_per_account_per_run"}
    assert items["account_ad_min_interval"]["value"] == 10800
    assert items["group_ad_min_interval"]["value"] == 259200


def test_disabled_risk_guard_removes_risk_caps_from_static_summary():
    items = _items(_summary(risk_enabled=False))

    assert items["account_join_daily"]["value"] is None
    assert items["account_group_message_daily"]["value"] is None
    assert items["account_ad_daily"]["value"] == 3
    assert all(
        source["active"] is False
        for source in items["account_ad_daily"]["sources"]
        if source["key"].startswith("risk.")
    )
