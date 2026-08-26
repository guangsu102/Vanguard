from app.core.automation_settings import (
    DEFAULT_NOTIFICATION_SETTINGS,
    normalize_account_asset_policy_settings,
    normalize_account_risk_guard_settings,
    normalize_account_warmup_policy_settings,
    normalize_ad_capacity_settings,
    normalize_ad_delivery_execution_settings,
    normalize_ad_delivery_throttle_settings,
    normalize_app_runtime_settings,
    normalize_group_ai_interaction_settings,
)


def test_normalize_account_risk_guard_settings_configures_group_leave_policy():
    config = normalize_account_risk_guard_settings(
        {
            "groupWriteForbidden": {
                "leaveAfterFailures": 0,
                "leaveWindowHours": 1000,
            }
        }
    )

    assert config["group_write_forbidden"]["leave_after_failures"] == 1
    assert config["group_write_forbidden"]["leave_window_hours"] == 720


def test_normalize_account_risk_guard_settings_enforces_acquisition_hard_caps():
    config = normalize_account_risk_guard_settings(
        {
            "globalDailyLimit": 999,
            "groupWriteDailyLimit": 999,
            "actions": {
                "join": {"dailyLimit": 999, "cooldownSeconds": 0},
                "group_message": {"dailyLimit": 999, "cooldownSeconds": 0},
                "ad_probe": {"dailyLimit": 999, "cooldownSeconds": 0},
                "ai_warmup": {"dailyLimit": 999, "cooldownSeconds": 0},
                "ad_delivery": {"dailyLimit": 999, "cooldownSeconds": 0},
            },
        }
    )

    assert config["account_outbound_message_hard_cap_default"] == 30
    assert config["global_daily_limit"] == 30
    assert config["group_write_daily_limit"] == 8
    assert config["actions"]["join"] == {"daily_limit": 6, "cooldown_seconds": 7200}
    assert config["actions"]["group_message"] == {
        "daily_limit": 4,
        "cooldown_seconds": 7200,
    }
    assert config["actions"]["ad_probe"] == {
        "daily_limit": 10,
        "cooldown_seconds": 3600,
    }
    assert config["actions"]["ai_warmup"] == {
        "daily_limit": 1,
        "cooldown_seconds": 21600,
    }
    assert config["actions"]["ad_delivery"] == {
        "daily_limit": 999,
        "cooldown_seconds": 0,
    }

def test_normalize_ad_capacity_settings_defaults_match_evidence_based_plan():
    config = normalize_ad_capacity_settings(None)

    assert config["enabled"] is True
    assert config["timezone_offset_hours"] == 8
    assert config["window_start_hour"] == 9
    assert config["window_end_hour"] == 2
    assert config["survival_check_delay_seconds"] == 120
    assert config["survival_one_hour_seconds"] == 3600
    assert config["survival_twenty_four_hour_seconds"] == 86400
    assert "account_ad_daily_hard_cap" not in config
    assert "account_group_daily_cap_default" not in config
    assert "group_global_daily_hard_cap" not in config
    assert "group_min_interval_seconds" not in config
    assert config["max_groups_per_account"] == 400
    assert config["max_new_ad_groups_per_day"] == 2
    assert config["leave_on_deleted_ad"] is True
    assert config["ad_policy_auto_probe_enabled"] is False
    assert config["ad_policy_auto_probe_daily_limit"] == 1
    assert config["ad_policy_auto_probe_daily_limit_per_account"] == 10
    assert config["ad_policy_auto_probe_interval_hours"] == 24
    assert "warmup_days_before_ads" not in config
    assert config["warmup_daily_interactions_min"] == 0
    assert config["warmup_daily_interactions_max"] == 1
    assert config["mature_daily_interactions_min"] == 0
    assert config["mature_daily_interactions_max"] == 1
    assert config["premium_min_samples"] == 20
    assert config["premium_growth_samples"] == 100
    assert config["premium_full_capacity_samples"] == 1000
    assert "premium_entry_capacity" not in config
    assert "premium_growth_capacity" not in config
    assert "premium_conversion_capacity_step" not in config
    assert config["premium_clean_days_verified"] == 3
    assert "tier_daily_capacities" not in config
    assert config["hourly_weights"]["2"] == 1
    assert config["hourly_weights"]["15"] > config["hourly_weights"]["9"]
    assert config["hourly_weights"]["1"] < config["hourly_weights"]["23"]


def test_normalize_ad_capacity_settings_accepts_camel_case_and_clamps_values():
    config = normalize_ad_capacity_settings(
        {
            "survivalCheckDelaySeconds": 10,
            "accountGroupDailyCapDefault": 500,
            "maxGroupsPerAccount": 150,
            "maxNewAdGroupsPerDay": 40,
            "adPolicyAutoProbeEnabled": True,
            "adPolicyAutoProbeDailyLimit": 99,
            "adPolicyAutoProbeIntervalHours": 0,
            "warmupDaysBeforeAds": 20,
            "warmupDailyInteractionsMin": 4,
            "warmupDailyInteractionsMax": 9,
            "tierDailyCapacities": {
                "blocked": 0,
                "low": 8,
                "medium": 70,
                "high": 180,
                "premium": 450,
            },
            "hourlyWeights": {
                "2": 18,
                "9": 1,
                "10": 2,
                "11": 3,
                "12": 4,
                "13": 5,
                "14": 6,
                "15": 7,
                "16": 8,
                "17": 9,
                "18": 10,
                "19": 11,
                "20": 12,
                "21": 13,
                "22": 14,
                "23": 15,
                "0": 16,
                "1": 17,
            },
        }
    )

    assert config["survival_check_delay_seconds"] == 30
    assert "account_group_daily_cap_default" not in config
    assert config["max_groups_per_account"] == 150
    assert config["max_new_ad_groups_per_day"] == 2
    assert "warmup_days_before_ads" not in config
    assert config["ad_policy_auto_probe_enabled"] is True
    assert config["ad_policy_auto_probe_daily_limit"] == 20
    assert config["ad_policy_auto_probe_daily_limit_per_account"] == 20
    assert config["ad_policy_auto_probe_interval_hours"] == 1
    assert config["warmup_daily_interactions_min"] == 4
    assert config["warmup_daily_interactions_max"] == 9
    assert "tier_daily_capacities" not in config
    assert config["hourly_weights"]["2"] == 18


def test_normalize_ad_capacity_settings_ignores_retired_capacity_curve():
    config = normalize_ad_capacity_settings(
        {
            "premiumEntryCapacity": 400,
            "premiumGrowthCapacity": 400,
            "premiumConversionCapacityStep": 100,
        }
    )

    assert "premium_entry_capacity" not in config
    assert "premium_growth_capacity" not in config
    assert "premium_conversion_capacity_step" not in config

def test_normalize_ad_delivery_throttle_settings_ignores_internal_batch_size():
    config = normalize_ad_delivery_throttle_settings(
        {
            "deliveryIntervalSeconds": 0,
            "batchSizeMin": 100,
            "batchSizeMax": 100,
            "cooldownMinSeconds": 0,
            "cooldownMaxSeconds": 0,
        }
    )

    assert config["growth_min_interval_seconds"] == 1800
    assert config["growth_max_interval_seconds"] == 10800
    assert "ad_only_min_interval_seconds" not in config
    assert "ad_only_max_interval_seconds" not in config
    assert "batch_size_min" not in config
    assert "batch_size_max" not in config
    assert "delivery_interval_seconds" not in config
    assert "batch_window_seconds" not in config
    assert "cooldown_min_seconds" not in config
    assert "cooldown_max_seconds" not in config


def test_normalize_ad_delivery_throttle_settings_accepts_half_hour_minimum():
    config = normalize_ad_delivery_throttle_settings(
        {
            "growth_min_interval_seconds": 1800,
            "growth_max_interval_seconds": 10800,
        }
    )

    assert config["growth_min_interval_seconds"] == 1800
    assert config["growth_max_interval_seconds"] == 10800


def test_normalize_ad_delivery_execution_settings_ignores_internal_run_caps():
    config = normalize_ad_delivery_execution_settings(
        {
            "maxDeliveriesPerRun": 10000,
            "maxDeliveriesPerAccountPerRun": 300,
        }
    )

    assert "max_deliveries_per_run" not in config
    assert "max_deliveries_per_account_per_run" not in config
    assert config["dispatcher_interval_seconds"] == 60
    assert config["dispatcher_batch_size"] == 100
    assert config["max_parallel_accounts"] == 3
    assert config["job_lease_seconds"] == 300
    assert config["growth_group_global_cooldown_seconds"] == 86400
    assert "group_campaign_cooldown_minutes" not in config
    assert "stop_account_after_success" not in config
    assert "stop_account_after_failure" not in config


def test_normalize_account_asset_policy_ignores_legacy_warmup_days():
    config = normalize_account_asset_policy_settings(
        {
            "tiers": {
                "year_2": {"warmupDays": 3},
                "year_3_plus": {"warmupDays": 0},
            }
        }
    )

    assert "warmup_days" not in config["tiers"]["year_2"]
    assert "warmup_days" not in config["tiers"]["year_3_plus"]

def test_normalize_account_warmup_policy_settings_defaults_and_camel_case():
    config = normalize_account_warmup_policy_settings(
        {
            "defaultWarmupDays": 3,
            "minimumWarmupDays": 5,
            "userInitiatedPrivateMessageMultiplier": 1.5,
            "tiers": {
                "year_3_plus": {"warmupDays": 6},
            },
            "stages": {
                "observe": {
                    "joinMultiplier": 0.2,
                    "adMultiplier": 0,
                    "allowProactivePrivateMessage": False,
                },
            },
        }
    )

    assert config["enabled"] is True
    assert config["default_warmup_days"] == 7
    assert config["minimum_warmup_days"] == 7
    assert config["user_initiated_private_message_multiplier"] == 1.5
    assert config["tiers"]["year_3_plus"]["warmup_days"] == 7
    assert config["stages"]["observe"]["join_multiplier"] == 0.2
    assert config["stages"]["observe"]["ad_multiplier"] == 0
    assert config["stages"]["observe"]["allow_proactive_private_message"] is False


def test_normalize_group_ai_interaction_settings_defaults_and_clamps():
    config = normalize_group_ai_interaction_settings(
        {
            "enabled": True,
            "ai_enabled": True,
            "mode": "unknown",
            "tone": "friendly",
            "temperature": 9,
            "max_tokens": 5,
            "maxRepliesPerGroupPerDay": -1,
            "reply_max_chars": 999,
            "block_ai_self_disclosure": False,
            "allow_keyword_triggered_reply": False,
            "allow_proactive_warmup": True,
            "proactive_warmup_interval_minutes": 0,
            "proactiveWarmupMaxGroupsPerRun": 101,
            "proactive_warmup_max_per_group_per_day": 1001,
            "proactiveWarmupMaxPerAccountPerDay": 10001,
            "proactive_warmup_cooldown_seconds": 10,
            "proactiveWarmupWindowStartHour": -1,
            "proactive_warmup_window_end_hour": 24,
            "system_prompt": " 自然一点 ",
        }
    )

    assert config["enabled"] is True
    assert "aiEnabled" not in config
    assert config["mode"] == "assistive"
    assert config["tone"] == "friendly"
    assert config["temperature"] == 2.0
    assert config["maxTokens"] == 20
    assert config["maxRepliesPerGroupPerDay"] == 0
    assert config["replyMaxChars"] == 500
    assert config["blockAiSelfDisclosure"] is False
    assert config["allowKeywordTriggeredReply"] is False
    assert config["allowProactiveWarmup"] is True
    assert config["proactiveWarmupIntervalMinutes"] == 1
    assert config["proactiveWarmupMaxGroupsPerRun"] == 100
    assert config["proactiveWarmupMaxPerGroupPerDay"] == 1000
    assert config["proactiveWarmupMaxPerAccountPerDay"] == 10000
    assert config["proactiveWarmupCooldownSeconds"] == 60
    assert config["proactiveWarmupWindowStartHour"] == 0
    assert config["proactiveWarmupWindowEndHour"] == 23
    assert config["systemPrompt"] == "自然一点"


def test_group_ai_legacy_double_switch_is_migrated_to_one_switch():
    disabled = normalize_group_ai_interaction_settings(
        {"enabled": True, "aiEnabled": False}
    )
    enabled = normalize_group_ai_interaction_settings({"enabled": True})

    assert disabled["enabled"] is False
    assert enabled["enabled"] is True
    assert "aiEnabled" not in disabled


def test_app_runtime_settings_drop_fields_without_runtime_consumers():
    config = normalize_app_runtime_settings(
        {
            "site": {"siteName": "unused"},
            "security": {"require2FA": True},
            "xboard": {"enabled": False},
            "notification": {
                "telegramEnabled": True,
                "emailEnabled": True,
                "webhookUrl": "https://unused.example",
            },
            "aiReply": {
                "enabled": True,
                "privateOnly": True,
                "dailyTokenBudget": 1000,
            },
        }
    )

    assert "site" not in config
    assert "security" not in config
    assert "xboard" not in config
    assert "emailEnabled" not in config["notification"]
    assert "webhookUrl" not in config["notification"]
    assert config["aiReply"] == {"enabled": True}


def test_app_runtime_settings_inherit_environment_telegram_chat_id(monkeypatch):
    monkeypatch.setitem(
        DEFAULT_NOTIFICATION_SETTINGS,
        "telegramChatId",
        "-1001234567890",
    )

    inherited = normalize_app_runtime_settings({})
    explicitly_cleared = normalize_app_runtime_settings(
        {"notification": {"telegramChatId": ""}}
    )

    assert inherited["notification"]["telegramChatId"] == "-1001234567890"
    assert explicitly_cleared["notification"]["telegramChatId"] == ""
