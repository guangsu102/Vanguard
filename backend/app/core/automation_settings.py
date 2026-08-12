"""Database-backed settings for automation workers."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_models import SystemSetting
from app.core.runtime_settings import (
    DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS,
    DEFAULT_ACCOUNT_RISK_GUARD_SETTINGS,
    DEFAULT_ACCOUNT_WARMUP_POLICY_SETTINGS,
    DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS,
    DEFAULT_GROUP_AI_INTERACTION_SETTINGS,
    DEFAULT_KEYWORD_PRIVATE_REPLY_SETTINGS,
    DEFAULT_PRIVATE_MESSAGING_SETTINGS,
    DEFAULT_PRIVATE_REPLY_TEMPLATES,
    DEFAULT_AD_DELIVERY_EXECUTION_SETTINGS,
    DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS,
    DEFAULT_AD_CAPACITY_SETTINGS,
)

AD_FAILURE_POLICY_SETTING_KEY = "automation.ad_failure_policy"
AUTO_JOIN_SCHEDULER_SETTING_KEY = "automation.auto_join_scheduler"
ACCOUNT_RISK_GUARD_SETTING_KEY = "automation.account_risk_guard"
ACCOUNT_ASSET_POLICY_SETTING_KEY = "automation.account_asset_policy"
ACCOUNT_WARMUP_POLICY_SETTING_KEY = "automation.account_warmup_policy"
AD_DELIVERY_THROTTLE_SETTING_KEY = "automation.ad_delivery_throttle"
AD_DELIVERY_EXECUTION_SETTING_KEY = "automation.ad_delivery_execution"
AD_CAPACITY_SETTING_KEY = "automation.ad_capacity"
APP_SETTINGS_SETTING_KEY = "app.runtime_settings"
DEFAULT_AD_FAILURE_POLICY: dict[str, Any] = {
    "enabled": True,
    "leave_on_group_control_failure": True,
    "group_control_failure_limit": 1,
    "group_control_failure_window_hours": 720,
    "levels": ["A", "B", "C", "UNRATED"],
}


def _int_setting(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def _bool_setting(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _float_setting(value: Any, default: float, *, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def _normalize_string_list(
    value: Any,
    *,
    default: list[str],
    max_items: int = 200,
    max_length: int = 80,
) -> list[str]:
    if value is None:
        items = default
    elif isinstance(value, str):
        items = value.replace("，", ",").replace("\n", ",").split(",")
    elif isinstance(value, list):
        items = value
    else:
        items = default

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        signature = text.casefold()
        if signature in seen:
            continue
        seen.add(signature)
        result.append(text[:max_length])
        if len(result) >= max_items:
            break
    return result


def _normalize_group_ai_overrides(value: Any, *, defaults: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = value if isinstance(value, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for group_id, item in raw.items():
        if not isinstance(item, dict):
            continue
        key = str(group_id or "").strip()
        if not key:
            continue
        topics = _normalize_string_list(
            item.get("topics", item.get("proactiveWarmupTopics", defaults["proactiveWarmupTopics"])),
            default=defaults["proactiveWarmupTopics"],
            max_items=30,
            max_length=80,
        )
        templates = _normalize_string_list(
            item.get("templates", item.get("proactiveWarmupTemplates", defaults["proactiveWarmupTemplates"])),
            default=defaults["proactiveWarmupTemplates"],
            max_items=50,
            max_length=240,
        )
        prompt = str(item.get("prompt", item.get("systemPrompt", "")) or "").strip()[:1000]
        result[key] = {
            "enabled": _bool_setting(item.get("enabled", True), True),
            "topics": topics,
            "templates": templates,
            "prompt": prompt,
        }
        if len(result) >= 500:
            break
    return result


def _normalize_int_map(value: Any, *, default: dict[str, int], min_value: int, max_value: int) -> dict[str, int]:
    raw = value if isinstance(value, dict) else default
    result: dict[str, int] = {}
    for key, fallback in default.items():
        result[str(key)] = _int_setting(raw.get(str(key), raw.get(key, fallback)), fallback, min_value=min_value, max_value=max_value)
    return result


def _normalize_float_map(value: Any, *, default: dict[str, float], min_value: float, max_value: float) -> dict[str, float]:
    raw = value if isinstance(value, dict) else default
    result: dict[str, float] = {}
    for key, fallback in default.items():
        result[str(key)] = _float_setting(raw.get(str(key), raw.get(key, fallback)), fallback, min_value=min_value, max_value=max_value)
    return result


async def _read_setting_payload(db: AsyncSession, key: str) -> dict[str, Any]:
    setting = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
    if setting is None:
        return {}
    try:
        payload = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


async def _save_setting_payload(db: AsyncSession, key: str, payload: dict[str, Any], description: str) -> None:
    setting = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
    if setting is None:
        setting = SystemSetting(key=key, description=description)
        db.add(setting)
    setting.value = json.dumps(payload, ensure_ascii=False)
    await db.commit()


def normalize_auto_join_scheduler_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS

    verification = raw.get("join_verification", raw.get("joinVerification", defaults["join_verification"]))
    if not isinstance(verification, dict):
        verification = {}
    default_verification = defaults["join_verification"]
    unknown_action = str(
        verification.get(
            "unknown_challenge_action",
            verification.get("unknownChallengeAction", default_verification["unknown_challenge_action"]),
        )
        or default_verification["unknown_challenge_action"]
    ).strip()
    if unknown_action not in {"leave", "manual", "wait", "skip"}:
        unknown_action = default_verification["unknown_challenge_action"]

    search_filter = raw.get("search_filter", raw.get("searchFilter", defaults["search_filter"]))
    if not isinstance(search_filter, dict):
        search_filter = {}
    default_filter = defaults["search_filter"]

    cleanup = raw.get("group_capacity_cleanup", raw.get("groupCapacityCleanup", defaults["group_capacity_cleanup"]))
    if not isinstance(cleanup, dict):
        cleanup = {}
    default_cleanup = defaults["group_capacity_cleanup"]

    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "scan_interval_minutes": _int_setting(
            raw.get("scan_interval_minutes", raw.get("scanIntervalMinutes", defaults["scan_interval_minutes"])),
            defaults["scan_interval_minutes"],
            min_value=1,
            max_value=1440,
        ),
        "search_filter": {
            "title_blacklist_enabled": _bool_setting(
                search_filter.get(
                    "title_blacklist_enabled",
                    search_filter.get("titleBlacklistEnabled", default_filter["title_blacklist_enabled"]),
                ),
                default_filter["title_blacklist_enabled"],
            ),
            "title_blacklist": _normalize_string_list(
                search_filter.get("title_blacklist", search_filter.get("titleBlacklist", default_filter["title_blacklist"])),
                default=default_filter["title_blacklist"],
            ),
        },
        "join_verification": {
            "enabled": _bool_setting(verification.get("enabled", default_verification["enabled"]), default_verification["enabled"]),
            "ai_enabled": _bool_setting(
                verification.get("ai_enabled", verification.get("aiEnabled", default_verification["ai_enabled"])),
                default_verification["ai_enabled"],
            ),
            "confidence_threshold": _float_setting(
                verification.get(
                    "confidence_threshold",
                    verification.get("confidenceThreshold", default_verification["confidence_threshold"]),
                ),
                float(default_verification["confidence_threshold"]),
                min_value=0.0,
                max_value=1.0,
            ),
            "post_action_wait_seconds": _int_setting(
                verification.get(
                    "post_action_wait_seconds",
                    verification.get("postActionWaitSeconds", default_verification["post_action_wait_seconds"]),
                ),
                default_verification["post_action_wait_seconds"],
                min_value=0,
                max_value=120,
            ),
            "post_action_recheck_attempts": _int_setting(
                verification.get(
                    "post_action_recheck_attempts",
                    verification.get("postActionRecheckAttempts", default_verification["post_action_recheck_attempts"]),
                ),
                default_verification["post_action_recheck_attempts"],
                min_value=1,
                max_value=10,
            ),
            "post_action_extra_wait_seconds": _float_setting(
                verification.get(
                    "post_action_extra_wait_seconds",
                    verification.get("postActionExtraWaitSeconds", default_verification["post_action_extra_wait_seconds"]),
                ),
                float(default_verification["post_action_extra_wait_seconds"]),
                min_value=0.0,
                max_value=30.0,
            ),
            "message_limit": _int_setting(
                verification.get("message_limit", verification.get("messageLimit", default_verification["message_limit"])),
                default_verification["message_limit"],
                min_value=5,
                max_value=50,
            ),
            "ai_timeout_seconds": _float_setting(
                verification.get("ai_timeout_seconds", verification.get("aiTimeoutSeconds", default_verification["ai_timeout_seconds"])),
                float(default_verification["ai_timeout_seconds"]),
                min_value=1.0,
                max_value=45.0,
            ),
            "action_timeout_seconds": _float_setting(
                verification.get(
                    "action_timeout_seconds",
                    verification.get("actionTimeoutSeconds", default_verification["action_timeout_seconds"]),
                ),
                float(default_verification["action_timeout_seconds"]),
                min_value=1.0,
                max_value=20.0,
            ),
            "pending_sync_min_age_seconds": _int_setting(
                verification.get(
                    "pending_sync_min_age_seconds",
                    verification.get("pendingSyncMinAgeSeconds", default_verification["pending_sync_min_age_seconds"]),
                ),
                default_verification["pending_sync_min_age_seconds"],
                min_value=30,
                max_value=3600,
            ),
            "pending_sync_limit": _int_setting(
                verification.get("pending_sync_limit", verification.get("pendingSyncLimit", default_verification["pending_sync_limit"])),
                default_verification["pending_sync_limit"],
                min_value=1,
                max_value=20,
            ),
            "unknown_challenge_action": unknown_action,
            "allow_button_clicks": _bool_setting(
                verification.get("allow_button_clicks", verification.get("allowButtonClicks", default_verification["allow_button_clicks"])),
                default_verification["allow_button_clicks"],
            ),
            "allow_text_answers": _bool_setting(
                verification.get("allow_text_answers", verification.get("allowTextAnswers", default_verification["allow_text_answers"])),
                default_verification["allow_text_answers"],
            ),
            "answer_profile": str(
                verification.get("answer_profile", verification.get("answerProfile", default_verification["answer_profile"]))
                or default_verification["answer_profile"]
            ).strip()[:500],
        },
        "group_capacity_cleanup": {
            "enabled": _bool_setting(cleanup.get("enabled", default_cleanup["enabled"]), default_cleanup["enabled"]),
            "no_conversion_days": _int_setting(
                cleanup.get("no_conversion_days", cleanup.get("noConversionDays", default_cleanup["no_conversion_days"])),
                default_cleanup["no_conversion_days"],
                min_value=1,
                max_value=365,
            ),
            "min_join_age_days": _int_setting(
                cleanup.get("min_join_age_days", cleanup.get("minJoinAgeDays", default_cleanup["min_join_age_days"])),
                default_cleanup["min_join_age_days"],
                min_value=1,
                max_value=365,
            ),
            "max_cleanup_per_run": _int_setting(
                cleanup.get("max_cleanup_per_run", cleanup.get("maxCleanupPerRun", default_cleanup["max_cleanup_per_run"])),
                default_cleanup["max_cleanup_per_run"],
                min_value=1,
                max_value=15,
            ),
            "interval_hours": _int_setting(
                cleanup.get("interval_hours", cleanup.get("intervalHours", default_cleanup["interval_hours"])),
                default_cleanup["interval_hours"],
                min_value=1,
                max_value=168,
            ),
        },
    }


async def get_auto_join_scheduler_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_auto_join_scheduler_settings(await _read_setting_payload(db, AUTO_JOIN_SCHEDULER_SETTING_KEY))


async def save_auto_join_scheduler_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_auto_join_scheduler_settings(payload)
    await _save_setting_payload(
        db,
        AUTO_JOIN_SCHEDULER_SETTING_KEY,
        normalized,
        "Auto-join scheduler, search filter, and verification settings",
    )
    return normalized


def normalize_group_ai_interaction_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_GROUP_AI_INTERACTION_SETTINGS

    mode = str(raw.get("mode", defaults["mode"]) or defaults["mode"]).strip()
    if mode not in {"assistive", "warmup", "conversion", "off"}:
        mode = defaults["mode"]

    tone = str(raw.get("tone", defaults["tone"]) or defaults["tone"]).strip()
    if tone not in {"natural", "friendly", "professional", "soft"}:
        tone = defaults["tone"]

    system_prompt = str(
        raw.get("systemPrompt", raw.get("system_prompt", defaults["systemPrompt"])) or defaults["systemPrompt"]
    ).strip()
    if not system_prompt:
        system_prompt = defaults["systemPrompt"]

    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "aiEnabled": _bool_setting(
            raw.get("aiEnabled", raw.get("ai_enabled", defaults["aiEnabled"])),
            defaults["aiEnabled"],
        ),
        "dailyTokenBudget": _int_setting(
            raw.get("dailyTokenBudget", raw.get("daily_token_budget", defaults["dailyTokenBudget"])),
            defaults["dailyTokenBudget"],
            min_value=0,
            max_value=10_000_000,
        ),
        "maxRepliesPerGroupPerDay": _int_setting(
            raw.get(
                "maxRepliesPerGroupPerDay",
                raw.get("max_replies_per_group_per_day", defaults["maxRepliesPerGroupPerDay"]),
            ),
            defaults["maxRepliesPerGroupPerDay"],
            min_value=0,
            max_value=10_000,
        ),
        "maxRepliesPerAccountPerDay": _int_setting(
            raw.get(
                "maxRepliesPerAccountPerDay",
                raw.get("max_replies_per_account_per_day", defaults["maxRepliesPerAccountPerDay"]),
            ),
            defaults["maxRepliesPerAccountPerDay"],
            min_value=0,
            max_value=100_000,
        ),
        "cooldownSeconds": _int_setting(
            raw.get("cooldownSeconds", raw.get("cooldown_seconds", defaults["cooldownSeconds"])),
            defaults["cooldownSeconds"],
            min_value=0,
            max_value=86_400,
        ),
        "replyMaxChars": _int_setting(
            raw.get("replyMaxChars", raw.get("reply_max_chars", defaults["replyMaxChars"])),
            defaults["replyMaxChars"],
            min_value=20,
            max_value=500,
        ),
        "blockAiSelfDisclosure": _bool_setting(
            raw.get(
                "blockAiSelfDisclosure",
                raw.get("block_ai_self_disclosure", defaults["blockAiSelfDisclosure"]),
            ),
            defaults["blockAiSelfDisclosure"],
        ),
        "mode": mode,
        "tone": tone,
        "temperature": _float_setting(
            raw.get("temperature", defaults["temperature"]),
            float(defaults["temperature"]),
            min_value=0.0,
            max_value=2.0,
        ),
        "maxTokens": _int_setting(
            raw.get("maxTokens", raw.get("max_tokens", defaults["maxTokens"])),
            defaults["maxTokens"],
            min_value=20,
            max_value=1000,
        ),
        "allowKeywordTriggeredReply": _bool_setting(
            raw.get(
                "allowKeywordTriggeredReply",
                raw.get("allow_keyword_triggered_reply", defaults["allowKeywordTriggeredReply"]),
            ),
            defaults["allowKeywordTriggeredReply"],
        ),
        "allowSemanticTriggeredReply": _bool_setting(
            raw.get(
                "allowSemanticTriggeredReply",
                raw.get("allow_semantic_triggered_reply", defaults["allowSemanticTriggeredReply"]),
            ),
            defaults["allowSemanticTriggeredReply"],
        ),
        "semanticScanWindowMessages": _int_setting(
            raw.get(
                "semanticScanWindowMessages",
                raw.get("semantic_scan_window_messages", defaults["semanticScanWindowMessages"]),
            ),
            defaults["semanticScanWindowMessages"],
            min_value=5,
            max_value=100,
        ),
        "semanticEvaluateEveryMessages": _int_setting(
            raw.get(
                "semanticEvaluateEveryMessages",
                raw.get("semantic_evaluate_every_messages", defaults["semanticEvaluateEveryMessages"]),
            ),
            defaults["semanticEvaluateEveryMessages"],
            min_value=1,
            max_value=100,
        ),
        "semanticMinConfidence": _float_setting(
            raw.get(
                "semanticMinConfidence",
                raw.get("semantic_min_confidence", defaults["semanticMinConfidence"]),
            ),
            float(defaults["semanticMinConfidence"]),
            min_value=0.0,
            max_value=1.0,
        ),
        "semanticMinTextChars": _int_setting(
            raw.get(
                "semanticMinTextChars",
                raw.get("semantic_min_text_chars", defaults["semanticMinTextChars"]),
            ),
            defaults["semanticMinTextChars"],
            min_value=1,
            max_value=80,
        ),
        "semanticAllowedIntents": _normalize_string_list(
            raw.get(
                "semanticAllowedIntents",
                raw.get("semantic_allowed_intents", defaults["semanticAllowedIntents"]),
            ),
            default=defaults["semanticAllowedIntents"],
            max_items=30,
            max_length=80,
        ),
        "semanticBlockedIntents": _normalize_string_list(
            raw.get(
                "semanticBlockedIntents",
                raw.get("semantic_blocked_intents", defaults["semanticBlockedIntents"]),
            ),
            default=defaults["semanticBlockedIntents"],
            max_items=30,
            max_length=80,
        ),
        "semanticDecisionPrompt": str(
            raw.get(
                "semanticDecisionPrompt",
                raw.get("semantic_decision_prompt", defaults["semanticDecisionPrompt"]),
            )
            or defaults["semanticDecisionPrompt"]
        ).strip()[:2000],
        "allowProactiveWarmup": _bool_setting(
            raw.get("allowProactiveWarmup", raw.get("allow_proactive_warmup", defaults["allowProactiveWarmup"])),
            defaults["allowProactiveWarmup"],
        ),
        "proactiveWarmupIntervalMinutes": _int_setting(
            raw.get(
                "proactiveWarmupIntervalMinutes",
                raw.get("proactive_warmup_interval_minutes", defaults["proactiveWarmupIntervalMinutes"]),
            ),
            defaults["proactiveWarmupIntervalMinutes"],
            min_value=1,
            max_value=1440,
        ),
        "proactiveWarmupMaxGroupsPerRun": _int_setting(
            raw.get(
                "proactiveWarmupMaxGroupsPerRun",
                raw.get("proactive_warmup_max_groups_per_run", defaults["proactiveWarmupMaxGroupsPerRun"]),
            ),
            defaults["proactiveWarmupMaxGroupsPerRun"],
            min_value=1,
            max_value=100,
        ),
        "proactiveWarmupMaxPerGroupPerDay": _int_setting(
            raw.get(
                "proactiveWarmupMaxPerGroupPerDay",
                raw.get("proactive_warmup_max_per_group_per_day", defaults["proactiveWarmupMaxPerGroupPerDay"]),
            ),
            defaults["proactiveWarmupMaxPerGroupPerDay"],
            min_value=0,
            max_value=1000,
        ),
        "proactiveWarmupMaxPerAccountPerDay": _int_setting(
            raw.get(
                "proactiveWarmupMaxPerAccountPerDay",
                raw.get("proactive_warmup_max_per_account_per_day", defaults["proactiveWarmupMaxPerAccountPerDay"]),
            ),
            defaults["proactiveWarmupMaxPerAccountPerDay"],
            min_value=0,
            max_value=10000,
        ),
        "proactiveWarmupCooldownSeconds": _int_setting(
            raw.get(
                "proactiveWarmupCooldownSeconds",
                raw.get("proactive_warmup_cooldown_seconds", defaults["proactiveWarmupCooldownSeconds"]),
            ),
            defaults["proactiveWarmupCooldownSeconds"],
            min_value=60,
            max_value=86_400,
        ),
        "proactiveWarmupWindowStartHour": _int_setting(
            raw.get(
                "proactiveWarmupWindowStartHour",
                raw.get("proactive_warmup_window_start_hour", defaults["proactiveWarmupWindowStartHour"]),
            ),
            defaults["proactiveWarmupWindowStartHour"],
            min_value=0,
            max_value=23,
        ),
        "proactiveWarmupWindowEndHour": _int_setting(
            raw.get(
                "proactiveWarmupWindowEndHour",
                raw.get("proactive_warmup_window_end_hour", defaults["proactiveWarmupWindowEndHour"]),
            ),
            defaults["proactiveWarmupWindowEndHour"],
            min_value=0,
            max_value=23,
        ),
        "proactiveWarmupTopics": _normalize_string_list(
            raw.get(
                "proactiveWarmupTopics",
                raw.get("proactive_warmup_topics", defaults["proactiveWarmupTopics"]),
            ),
            default=defaults["proactiveWarmupTopics"],
            max_items=50,
            max_length=80,
        ),
        "proactiveWarmupTemplates": _normalize_string_list(
            raw.get(
                "proactiveWarmupTemplates",
                raw.get("proactive_warmup_templates", defaults["proactiveWarmupTemplates"]),
            ),
            default=defaults["proactiveWarmupTemplates"],
            max_items=100,
            max_length=240,
        ),
        "proactiveWarmupGroupOverrides": _normalize_group_ai_overrides(
            raw.get(
                "proactiveWarmupGroupOverrides",
                raw.get("proactive_warmup_group_overrides", defaults["proactiveWarmupGroupOverrides"]),
            ),
            defaults=defaults,
        ),
        "systemPrompt": system_prompt[:2000],
    }


def normalize_app_runtime_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    ai_reply = raw.get("aiReply", {})
    if not isinstance(ai_reply, dict):
        ai_reply = {}
    group_ai = raw.get("groupAiInteraction", {})
    if not isinstance(group_ai, dict):
        group_ai = {}
    keyword_private = raw.get("keywordPrivateReply", {})
    if not isinstance(keyword_private, dict):
        keyword_private = {}
    private_messaging = raw.get("privateMessaging", {})
    if not isinstance(private_messaging, dict):
        private_messaging = {}

    templates = private_messaging.get("templates", raw.get("privateReplyTemplates", {}))
    if not isinstance(templates, dict):
        templates = {}
    normalized_templates = dict(DEFAULT_PRIVATE_REPLY_TEMPLATES)
    for key in DEFAULT_PRIVATE_REPLY_TEMPLATES:
        value = templates.get(key)
        if isinstance(value, str):
            normalized_templates[key] = value.strip()[:4000]

    return {
        "_meta": raw.get("_meta", {}) if isinstance(raw.get("_meta", {}), dict) else {},
        "site": raw.get("site", {}) if isinstance(raw.get("site", {}), dict) else {},
        "notification": raw.get("notification", {}) if isinstance(raw.get("notification", {}), dict) else {},
        "security": raw.get("security", {}) if isinstance(raw.get("security", {}), dict) else {},
        "xboard": raw.get("xboard", {}) if isinstance(raw.get("xboard", {}), dict) else {},
        "aiReply": {
            "enabled": _bool_setting(ai_reply.get("enabled", False), False),
            "privateOnly": _bool_setting(ai_reply.get("privateOnly", True), True),
            "dailyTokenBudget": _int_setting(ai_reply.get("dailyTokenBudget", 0), 0, min_value=0, max_value=10000000),
            "maxRepliesPerUserPerDay": _int_setting(
                ai_reply.get("maxRepliesPerUserPerDay", 2),
                2,
                min_value=0,
                max_value=10000,
            ),
            "cooldownSeconds": _int_setting(ai_reply.get("cooldownSeconds", 1800), 1800, min_value=0, max_value=86400),
        },
        "groupAiInteraction": normalize_group_ai_interaction_settings(group_ai),
        "keywordPrivateReply": {
            "enabled": _bool_setting(
                keyword_private.get("enabled", DEFAULT_KEYWORD_PRIVATE_REPLY_SETTINGS["enabled"]),
                DEFAULT_KEYWORD_PRIVATE_REPLY_SETTINGS["enabled"],
            ),
        },
        "privateMessaging": {
            "inboundRepliesEnabled": _bool_setting(
                private_messaging.get(
                    "inboundRepliesEnabled",
                    private_messaging.get("inbound_replies_enabled", DEFAULT_PRIVATE_MESSAGING_SETTINGS["inbound_replies_enabled"]),
                ),
                DEFAULT_PRIVATE_MESSAGING_SETTINGS["inbound_replies_enabled"],
            ),
            "proactiveEnabled": _bool_setting(
                private_messaging.get(
                    "proactiveEnabled",
                    private_messaging.get("proactive_enabled", DEFAULT_PRIVATE_MESSAGING_SETTINGS["proactive_enabled"]),
                ),
                DEFAULT_PRIVATE_MESSAGING_SETTINGS["proactive_enabled"],
            ),
            "templates": normalized_templates,
        },
    }


async def get_app_runtime_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_app_runtime_settings(await _read_setting_payload(db, APP_SETTINGS_SETTING_KEY))


async def save_app_runtime_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_app_runtime_settings(payload)
    await _save_setting_payload(db, APP_SETTINGS_SETTING_KEY, normalized, "Admin-managed runtime application settings")
    return normalized


async def is_ai_reply_enabled(db: AsyncSession) -> bool:
    return bool((await get_app_runtime_settings(db))["aiReply"]["enabled"])


async def get_group_ai_interaction_settings(db: AsyncSession) -> dict[str, Any]:
    return (await get_app_runtime_settings(db))["groupAiInteraction"]


async def is_group_ai_interaction_enabled(db: AsyncSession) -> bool:
    settings = await get_group_ai_interaction_settings(db)
    return bool(settings["enabled"] and settings["aiEnabled"])


async def is_keyword_private_reply_enabled(db: AsyncSession) -> bool:
    return bool((await get_app_runtime_settings(db))["keywordPrivateReply"]["enabled"])


async def get_private_messaging_settings(db: AsyncSession) -> dict[str, Any]:
    return (await get_app_runtime_settings(db))["privateMessaging"]


async def get_private_reply_template_settings(db: AsyncSession) -> dict[str, str]:
    return (await get_private_messaging_settings(db))["templates"]


async def is_private_messaging_enabled(db: AsyncSession, *, initiated_by_user: bool = False) -> bool:
    settings = await get_private_messaging_settings(db)
    if initiated_by_user:
        return bool(settings["inboundRepliesEnabled"])
    return bool(settings["proactiveEnabled"])


def normalize_ad_failure_policy(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize advertisement failure policy read from API or database."""
    raw = payload if isinstance(payload, dict) else {}
    levels = raw.get("levels", DEFAULT_AD_FAILURE_POLICY["levels"])
    if not isinstance(levels, list):
        levels = DEFAULT_AD_FAILURE_POLICY["levels"]

    normalized_levels = []
    for item in levels:
        value = str(item or "").strip().upper()
        if value in {"A", "B", "C", "UNRATED"} and value not in normalized_levels:
            normalized_levels.append(value)
    if not normalized_levels:
        normalized_levels = list(DEFAULT_AD_FAILURE_POLICY["levels"])

    return {
        "enabled": bool(raw.get("enabled", DEFAULT_AD_FAILURE_POLICY["enabled"])),
        "leave_on_group_control_failure": bool(
            raw.get(
                "leave_on_group_control_failure",
                raw.get(
                    "leaveOnGroupControlFailure",
                    DEFAULT_AD_FAILURE_POLICY["leave_on_group_control_failure"],
                ),
            )
        ),
        "group_control_failure_limit": _int_setting(
            raw.get(
                "group_control_failure_limit",
                raw.get("groupControlFailureLimit", DEFAULT_AD_FAILURE_POLICY["group_control_failure_limit"]),
            ),
            DEFAULT_AD_FAILURE_POLICY["group_control_failure_limit"],
            min_value=1,
            max_value=20,
        ),
        "group_control_failure_window_hours": _int_setting(
            raw.get(
                "group_control_failure_window_hours",
                raw.get(
                    "groupControlFailureWindowHours",
                    DEFAULT_AD_FAILURE_POLICY["group_control_failure_window_hours"],
                ),
            ),
            DEFAULT_AD_FAILURE_POLICY["group_control_failure_window_hours"],
            min_value=1,
            max_value=720,
        ),
        "levels": normalized_levels,
    }


async def get_ad_failure_policy_settings(db: AsyncSession) -> dict[str, Any]:
    """Read advertisement failure policy from the database."""
    setting = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == AD_FAILURE_POLICY_SETTING_KEY))
    ).scalar_one_or_none()
    if setting is None:
        return dict(DEFAULT_AD_FAILURE_POLICY)
    try:
        payload = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return normalize_ad_failure_policy(payload)


async def save_ad_failure_policy_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist advertisement failure policy to the database."""
    normalized = normalize_ad_failure_policy(payload)
    setting = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == AD_FAILURE_POLICY_SETTING_KEY))
    ).scalar_one_or_none()
    if setting is None:
        setting = SystemSetting(
            key=AD_FAILURE_POLICY_SETTING_KEY,
            description="Advertisement group-control failure leave policy",
        )
        db.add(setting)
    setting.value = json.dumps(normalized, ensure_ascii=False)
    await db.commit()
    return normalized


def normalize_account_risk_guard_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_ACCOUNT_RISK_GUARD_SETTINGS
    actions_raw = raw.get("actions", {})
    if not isinstance(actions_raw, dict):
        actions_raw = {}

    actions: dict[str, dict[str, int]] = {}
    action_hard_limits = {
        "join": 15,
        "group_message": 20,
        "ad_probe": 10,
        "ai_warmup": 10,
        "ad_delivery": 50,
        "channel_create": 1,
    }
    action_min_cooldowns = {
        "join": 1200,
        "group_message": 300,
        "ad_probe": 1800,
        "ai_warmup": 1800,
        "ad_delivery": 300,
        "channel_create": 86400,
    }
    for action, default_budget in defaults["actions"].items():
        item = actions_raw.get(action, {})
        if not isinstance(item, dict):
            item = {}
        daily_limit = _int_setting(
                item.get("daily_limit", item.get("dailyLimit", default_budget["daily_limit"])),
                default_budget["daily_limit"],
                min_value=1,
                max_value=action_hard_limits.get(action, 100000),
            )
        cooldown_seconds = _int_setting(
                item.get("cooldown_seconds", item.get("cooldownSeconds", default_budget["cooldown_seconds"])),
                default_budget["cooldown_seconds"],
                min_value=0,
                max_value=86400,
            )
        actions[action] = {
            "daily_limit": daily_limit,
            "cooldown_seconds": max(action_min_cooldowns.get(action, 0), cooldown_seconds),
        }

    redis_fail_closed = raw.get("redis_fail_closed", raw.get("redisFailClosed", defaults["redis_fail_closed"]))
    if redis_fail_closed is not None:
        redis_fail_closed = _bool_setting(redis_fail_closed, False)

    thresholds = _normalize_float_map(
        raw.get("level_thresholds", raw.get("levelThresholds", defaults["level_thresholds"])),
        default=defaults["level_thresholds"],
        min_value=0.0,
        max_value=100.0,
    )
    thresholds["limited"] = max(thresholds["limited"], thresholds["watch"])
    thresholds["frozen"] = max(thresholds["frozen"], thresholds["limited"])
    thresholds["quarantined"] = max(thresholds["quarantined"], thresholds["frozen"])

    level_multipliers = _normalize_float_map(
        raw.get("level_budget_multipliers", raw.get("levelBudgetMultipliers", defaults["level_budget_multipliers"])),
        default=defaults["level_budget_multipliers"],
        min_value=0.0,
        max_value=2.0,
    )
    score_deltas = _normalize_float_map(
        raw.get("risk_score_deltas", raw.get("riskScoreDeltas", defaults["risk_score_deltas"])),
        default=defaults["risk_score_deltas"],
        min_value=0.0,
        max_value=100.0,
    )

    lifecycle_raw = raw.get("lifecycle", defaults["lifecycle"])
    if not isinstance(lifecycle_raw, dict):
        lifecycle_raw = {}
    lifecycle_defaults = defaults["lifecycle"]
    lifecycle = {
        "default_freeze_seconds": _int_setting(
            lifecycle_raw.get("default_freeze_seconds", lifecycle_raw.get("defaultFreezeSeconds", lifecycle_defaults["default_freeze_seconds"])),
            lifecycle_defaults["default_freeze_seconds"],
            min_value=60,
            max_value=604800,
        ),
        "flood_wait_buffer_seconds": _int_setting(
            lifecycle_raw.get("flood_wait_buffer_seconds", lifecycle_raw.get("floodWaitBufferSeconds", lifecycle_defaults["flood_wait_buffer_seconds"])),
            lifecycle_defaults["flood_wait_buffer_seconds"],
            min_value=0,
            max_value=3600,
        ),
        "peer_flood_freeze_seconds": _int_setting(
            lifecycle_raw.get("peer_flood_freeze_seconds", lifecycle_raw.get("peerFloodFreezeSeconds", lifecycle_defaults["peer_flood_freeze_seconds"])),
            lifecycle_defaults["peer_flood_freeze_seconds"],
            min_value=60,
            max_value=604800,
        ),
        "account_restricted_freeze_seconds": _int_setting(
            lifecycle_raw.get(
                "account_restricted_freeze_seconds",
                lifecycle_raw.get("accountRestrictedFreezeSeconds", lifecycle_defaults["account_restricted_freeze_seconds"]),
            ),
            lifecycle_defaults["account_restricted_freeze_seconds"],
            min_value=60,
            max_value=604800,
        ),
        "group_write_forbidden_freeze_seconds": _int_setting(
            lifecycle_raw.get(
                "group_write_forbidden_freeze_seconds",
                lifecycle_raw.get("groupWriteForbiddenFreezeSeconds", lifecycle_defaults["group_write_forbidden_freeze_seconds"]),
            ),
            lifecycle_defaults["group_write_forbidden_freeze_seconds"],
            min_value=60,
            max_value=604800,
        ),
        "recovery_seconds": _int_setting(
            lifecycle_raw.get("recovery_seconds", lifecycle_raw.get("recoverySeconds", lifecycle_defaults["recovery_seconds"])),
            lifecycle_defaults["recovery_seconds"],
            min_value=60,
            max_value=604800,
        ),
        "post_freeze_score_cap": _float_setting(
            lifecycle_raw.get("post_freeze_score_cap", lifecycle_raw.get("postFreezeScoreCap", lifecycle_defaults["post_freeze_score_cap"])),
            float(lifecycle_defaults["post_freeze_score_cap"]),
            min_value=0.0,
            max_value=100.0,
        ),
        "manual_clear_score_cap": _float_setting(
            lifecycle_raw.get("manual_clear_score_cap", lifecycle_raw.get("manualClearScoreCap", lifecycle_defaults["manual_clear_score_cap"])),
            float(lifecycle_defaults["manual_clear_score_cap"]),
            min_value=0.0,
            max_value=100.0,
        ),
        "decay_interval_hours": _int_setting(
            lifecycle_raw.get("decay_interval_hours", lifecycle_raw.get("decayIntervalHours", lifecycle_defaults["decay_interval_hours"])),
            lifecycle_defaults["decay_interval_hours"],
            min_value=1,
            max_value=720,
        ),
        "decay_points_per_interval": _float_setting(
            lifecycle_raw.get(
                "decay_points_per_interval",
                lifecycle_raw.get("decayPointsPerInterval", lifecycle_defaults["decay_points_per_interval"]),
            ),
            float(lifecycle_defaults["decay_points_per_interval"]),
            min_value=0.0,
            max_value=100.0,
        ),
        "new_account_days": _int_setting(
            lifecycle_raw.get("new_account_days", lifecycle_raw.get("newAccountDays", lifecycle_defaults["new_account_days"])),
            lifecycle_defaults["new_account_days"],
            min_value=0,
            max_value=120,
        ),
        "new_account_multiplier": _float_setting(
            lifecycle_raw.get("new_account_multiplier", lifecycle_raw.get("newAccountMultiplier", lifecycle_defaults["new_account_multiplier"])),
            float(lifecycle_defaults["new_account_multiplier"]),
            min_value=0.0,
            max_value=2.0,
        ),
        "recovery_multiplier": _float_setting(
            lifecycle_raw.get("recovery_multiplier", lifecycle_raw.get("recoveryMultiplier", lifecycle_defaults["recovery_multiplier"])),
            float(lifecycle_defaults["recovery_multiplier"]),
            min_value=0.0,
            max_value=2.0,
        ),
        "healthy_account_days": _int_setting(
            lifecycle_raw.get("healthy_account_days", lifecycle_raw.get("healthyAccountDays", lifecycle_defaults["healthy_account_days"])),
            lifecycle_defaults["healthy_account_days"],
            min_value=0,
            max_value=365,
        ),
        "healthy_account_multiplier": _float_setting(
            lifecycle_raw.get(
                "healthy_account_multiplier",
                lifecycle_raw.get("healthyAccountMultiplier", lifecycle_defaults["healthy_account_multiplier"]),
            ),
            float(lifecycle_defaults["healthy_account_multiplier"]),
            min_value=0.0,
            max_value=2.0,
        ),
        "max_budget_multiplier": _float_setting(
            lifecycle_raw.get("max_budget_multiplier", lifecycle_raw.get("maxBudgetMultiplier", lifecycle_defaults["max_budget_multiplier"])),
            float(lifecycle_defaults["max_budget_multiplier"]),
            min_value=0.0,
            max_value=2.0,
        ),
    }

    group_write_raw = raw.get("group_write_forbidden", raw.get("groupWriteForbidden", defaults["group_write_forbidden"]))
    if not isinstance(group_write_raw, dict):
        group_write_raw = {}
    group_write_defaults = defaults["group_write_forbidden"]
    group_write_forbidden = {
        "leave_after_failures": _int_setting(
            group_write_raw.get(
                "leave_after_failures",
                group_write_raw.get("leaveAfterFailures", group_write_defaults["leave_after_failures"]),
            ),
            group_write_defaults["leave_after_failures"],
            min_value=1,
            max_value=20,
        ),
        "leave_window_hours": _int_setting(
            group_write_raw.get(
                "leave_window_hours",
                group_write_raw.get("leaveWindowHours", group_write_defaults["leave_window_hours"]),
            ),
            group_write_defaults["leave_window_hours"],
            min_value=1,
            max_value=720,
        ),
        "freeze_window_hours": _int_setting(
            group_write_raw.get("freeze_window_hours", group_write_raw.get("freezeWindowHours", group_write_defaults["freeze_window_hours"])),
            group_write_defaults["freeze_window_hours"],
            min_value=1,
            max_value=168,
        ),
        "freeze_distinct_groups": _int_setting(
            group_write_raw.get("freeze_distinct_groups", group_write_raw.get("freezeDistinctGroups", group_write_defaults["freeze_distinct_groups"])),
            group_write_defaults["freeze_distinct_groups"],
            min_value=1,
            max_value=100,
        ),
        "quarantine_window_hours": _int_setting(
            group_write_raw.get(
                "quarantine_window_hours",
                group_write_raw.get("quarantineWindowHours", group_write_defaults["quarantine_window_hours"]),
            ),
            group_write_defaults["quarantine_window_hours"],
            min_value=1,
            max_value=720,
        ),
        "quarantine_distinct_groups": _int_setting(
            group_write_raw.get(
                "quarantine_distinct_groups",
                group_write_raw.get("quarantineDistinctGroups", group_write_defaults["quarantine_distinct_groups"]),
            ),
            group_write_defaults["quarantine_distinct_groups"],
            min_value=1,
            max_value=200,
        ),
    }

    retention_raw = raw.get("retention", defaults["retention"])
    if not isinstance(retention_raw, dict):
        retention_raw = {}
    retention_defaults = defaults["retention"]
    retention = {
        "low_value_detail_retention_days": _int_setting(
            retention_raw.get(
                "low_value_detail_retention_days",
                retention_raw.get("lowValueDetailRetentionDays", retention_defaults["low_value_detail_retention_days"]),
            ),
            retention_defaults["low_value_detail_retention_days"],
            min_value=1,
            max_value=3650,
        ),
        "high_value_detail_retention_days": _int_setting(
            retention_raw.get(
                "high_value_detail_retention_days",
                retention_raw.get("highValueDetailRetentionDays", retention_defaults["high_value_detail_retention_days"]),
            ),
            retention_defaults["high_value_detail_retention_days"],
            min_value=1,
            max_value=3650,
        ),
        "daily_stat_retention_days": _int_setting(
            retention_raw.get("daily_stat_retention_days", retention_raw.get("dailyStatRetentionDays", retention_defaults["daily_stat_retention_days"])),
            retention_defaults["daily_stat_retention_days"],
            min_value=1,
            max_value=3650,
        ),
    }
    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "global_daily_limit": _int_setting(
            raw.get("global_daily_limit", raw.get("globalDailyLimit", defaults["global_daily_limit"])),
            defaults["global_daily_limit"],
            min_value=1,
            max_value=200,
        ),
        "redis_fail_closed": redis_fail_closed,
        "actions": actions,
        "level_thresholds": thresholds,
        "level_budget_multipliers": level_multipliers,
        "risk_score_deltas": score_deltas,
        "lifecycle": lifecycle,
        "group_write_forbidden": group_write_forbidden,
        "retention": retention,
    }


async def get_account_risk_guard_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_account_risk_guard_settings(await _read_setting_payload(db, ACCOUNT_RISK_GUARD_SETTING_KEY))


async def save_account_risk_guard_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_account_risk_guard_settings(payload)
    await _save_setting_payload(
        db,
        ACCOUNT_RISK_GUARD_SETTING_KEY,
        normalized,
        "Account risk guard budgets and cooldowns",
    )
    return normalized


def normalize_account_asset_policy_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS
    default_tiers = defaults["tiers"]
    raw_tiers = raw.get("tiers") if isinstance(raw.get("tiers"), dict) else {}

    normalized_tiers: dict[str, dict[str, Any]] = {}
    for tier, default_policy in default_tiers.items():
        item = raw_tiers.get(tier, {})
        if not isinstance(item, dict):
            item = {}
        normalized_tiers[tier] = {
            "join_multiplier": _float_setting(
                item.get("join_multiplier", item.get("joinMultiplier", default_policy["join_multiplier"])),
                float(default_policy["join_multiplier"]),
                min_value=0.0,
                max_value=3.0,
            ),
            "ad_multiplier": _float_setting(
                item.get("ad_multiplier", item.get("adMultiplier", default_policy["ad_multiplier"])),
                float(default_policy["ad_multiplier"]),
                min_value=0.0,
                max_value=3.0,
            ),
            "run_multiplier": _float_setting(
                item.get("run_multiplier", item.get("runMultiplier", default_policy["run_multiplier"])),
                float(default_policy["run_multiplier"]),
                min_value=0.0,
                max_value=3.0,
            ),
            "probe_multiplier": _float_setting(
                item.get("probe_multiplier", item.get("probeMultiplier", default_policy["probe_multiplier"])),
                float(default_policy["probe_multiplier"]),
                min_value=0.0,
                max_value=3.0,
            ),
            "warmup_days": _int_setting(
                item.get("warmup_days", item.get("warmupDays", default_policy["warmup_days"])),
                int(default_policy["warmup_days"]),
                min_value=0,
                max_value=120,
            ),
            "age_floor_days": _int_setting(
                item.get("age_floor_days", item.get("ageFloorDays", default_policy["age_floor_days"])),
                int(default_policy["age_floor_days"]),
                min_value=0,
                max_value=3650,
            ),
        }

    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "tiers": normalized_tiers,
    }


async def get_account_asset_policy_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_account_asset_policy_settings(await _read_setting_payload(db, ACCOUNT_ASSET_POLICY_SETTING_KEY))


async def save_account_asset_policy_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_account_asset_policy_settings(payload)
    await _save_setting_payload(
        db,
        ACCOUNT_ASSET_POLICY_SETTING_KEY,
        normalized,
        "Promoter account asset tier policy",
    )
    return normalized


def normalize_account_warmup_policy_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_ACCOUNT_WARMUP_POLICY_SETTINGS
    default_tiers = defaults["tiers"]
    raw_tiers = raw.get("tiers") if isinstance(raw.get("tiers"), dict) else {}

    normalized_tiers: dict[str, dict[str, int]] = {}
    for tier, default_policy in default_tiers.items():
        item = raw_tiers.get(tier, {})
        if not isinstance(item, dict):
            item = {}
        normalized_tiers[tier] = {
            "warmup_days": _int_setting(
                item.get("warmup_days", item.get("warmupDays", default_policy["warmup_days"])),
                int(default_policy["warmup_days"]),
                min_value=0,
                max_value=120,
            ),
        }

    raw_stages = raw.get("stages") if isinstance(raw.get("stages"), dict) else {}
    normalized_stages: dict[str, dict[str, Any]] = {}
    for stage, default_stage in defaults["stages"].items():
        item = raw_stages.get(stage, {})
        if not isinstance(item, dict):
            item = {}
        normalized_stages[stage] = {
            "limit_multiplier": _float_setting(
                item.get("limit_multiplier", item.get("limitMultiplier", default_stage["limit_multiplier"])),
                float(default_stage["limit_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "join_multiplier": _float_setting(
                item.get("join_multiplier", item.get("joinMultiplier", default_stage["join_multiplier"])),
                float(default_stage["join_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "ad_multiplier": _float_setting(
                item.get("ad_multiplier", item.get("adMultiplier", default_stage["ad_multiplier"])),
                float(default_stage["ad_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "run_multiplier": _float_setting(
                item.get("run_multiplier", item.get("runMultiplier", default_stage["run_multiplier"])),
                float(default_stage["run_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "probe_multiplier": _float_setting(
                item.get("probe_multiplier", item.get("probeMultiplier", default_stage["probe_multiplier"])),
                float(default_stage["probe_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "private_message_multiplier": _float_setting(
                item.get(
                    "private_message_multiplier",
                    item.get("privateMessageMultiplier", default_stage["private_message_multiplier"]),
                ),
                float(default_stage["private_message_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "group_message_multiplier": _float_setting(
                item.get(
                    "group_message_multiplier",
                    item.get("groupMessageMultiplier", default_stage["group_message_multiplier"]),
                ),
                float(default_stage["group_message_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "profile_update_multiplier": _float_setting(
                item.get(
                    "profile_update_multiplier",
                    item.get("profileUpdateMultiplier", default_stage["profile_update_multiplier"]),
                ),
                float(default_stage["profile_update_multiplier"]),
                min_value=0.0,
                max_value=2.0,
            ),
            "allow_proactive_private_message": _bool_setting(
                item.get(
                    "allow_proactive_private_message",
                    item.get("allowProactivePrivateMessage", default_stage["allow_proactive_private_message"]),
                ),
                bool(default_stage["allow_proactive_private_message"]),
            ),
        }

    minimum_days = _int_setting(
        raw.get("minimum_warmup_days", raw.get("minimumWarmupDays", defaults["minimum_warmup_days"])),
        int(defaults["minimum_warmup_days"]),
        min_value=0,
        max_value=120,
    )
    default_days = _int_setting(
        raw.get("default_warmup_days", raw.get("defaultWarmupDays", defaults["default_warmup_days"])),
        int(defaults["default_warmup_days"]),
        min_value=0,
        max_value=120,
    )
    if default_days < minimum_days:
        default_days = minimum_days

    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "default_warmup_days": default_days,
        "minimum_warmup_days": minimum_days,
        "user_initiated_private_message_multiplier": _float_setting(
            raw.get(
                "user_initiated_private_message_multiplier",
                raw.get(
                    "userInitiatedPrivateMessageMultiplier",
                    defaults["user_initiated_private_message_multiplier"],
                ),
            ),
            float(defaults["user_initiated_private_message_multiplier"]),
            min_value=0.0,
            max_value=2.0,
        ),
        "tiers": normalized_tiers,
        "stages": normalized_stages,
    }


async def get_account_warmup_policy_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_account_warmup_policy_settings(await _read_setting_payload(db, ACCOUNT_WARMUP_POLICY_SETTING_KEY))


async def save_account_warmup_policy_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_account_warmup_policy_settings(payload)
    await _save_setting_payload(
        db,
        ACCOUNT_WARMUP_POLICY_SETTING_KEY,
        normalized,
        "Promoter account managed warmup policy",
    )
    return normalized


def normalize_ad_delivery_throttle_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS
    batch_size_min = _int_setting(
        raw.get("batch_size_min", raw.get("batchSizeMin", defaults["batch_size_min"])),
        defaults["batch_size_min"],
        min_value=1,
        max_value=10000,
    )
    batch_size_max = _int_setting(
        raw.get("batch_size_max", raw.get("batchSizeMax", defaults["batch_size_max"])),
        defaults["batch_size_max"],
        min_value=1,
        max_value=10000,
    )
    if batch_size_max < batch_size_min:
        batch_size_max = batch_size_min

    cooldown_min_seconds = _int_setting(
        raw.get("cooldown_min_seconds", raw.get("cooldownMinSeconds", defaults["cooldown_min_seconds"])),
        defaults["cooldown_min_seconds"],
        min_value=0,
        max_value=86400,
    )
    cooldown_max_seconds = _int_setting(
        raw.get("cooldown_max_seconds", raw.get("cooldownMaxSeconds", defaults["cooldown_max_seconds"])),
        defaults["cooldown_max_seconds"],
        min_value=0,
        max_value=86400,
    )
    if cooldown_max_seconds < cooldown_min_seconds:
        cooldown_max_seconds = cooldown_min_seconds

    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "delivery_interval_seconds": _int_setting(
            raw.get("delivery_interval_seconds", raw.get("deliveryIntervalSeconds", defaults["delivery_interval_seconds"])),
            defaults["delivery_interval_seconds"],
            min_value=0,
            max_value=3600,
        ),
        "batch_window_seconds": _int_setting(
            raw.get("batch_window_seconds", raw.get("batchWindowSeconds", defaults["batch_window_seconds"])),
            defaults["batch_window_seconds"],
            min_value=1,
            max_value=3600,
        ),
        "batch_size_min": batch_size_min,
        "batch_size_max": batch_size_max,
        "cooldown_min_seconds": cooldown_min_seconds,
        "cooldown_max_seconds": cooldown_max_seconds,
    }


async def get_ad_delivery_throttle_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_ad_delivery_throttle_settings(await _read_setting_payload(db, AD_DELIVERY_THROTTLE_SETTING_KEY))


async def save_ad_delivery_throttle_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_ad_delivery_throttle_settings(payload)
    await _save_setting_payload(
        db,
        AD_DELIVERY_THROTTLE_SETTING_KEY,
        normalized,
        "Advertisement delivery throttle settings",
    )
    return normalized


def normalize_ad_delivery_execution_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_AD_DELIVERY_EXECUTION_SETTINGS
    return {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "dispatcher_interval_seconds": _int_setting(
            raw.get("dispatcher_interval_seconds", raw.get("dispatcherIntervalSeconds", defaults["dispatcher_interval_seconds"])),
            defaults["dispatcher_interval_seconds"],
            min_value=1,
            max_value=86400,
        ),
        "max_deliveries_per_run": _int_setting(
            raw.get("max_deliveries_per_run", raw.get("maxDeliveriesPerRun", defaults["max_deliveries_per_run"])),
            defaults["max_deliveries_per_run"],
            min_value=1,
            max_value=20,
        ),
        "max_deliveries_per_account_per_run": _int_setting(
            raw.get(
                "max_deliveries_per_account_per_run",
                raw.get("maxDeliveriesPerAccountPerRun", defaults["max_deliveries_per_account_per_run"]),
            ),
            defaults["max_deliveries_per_account_per_run"],
            min_value=1,
            max_value=5,
        ),
        "group_campaign_cooldown_minutes": _int_setting(
            raw.get("group_campaign_cooldown_minutes", raw.get("groupCampaignCooldownMinutes", defaults["group_campaign_cooldown_minutes"])),
            defaults["group_campaign_cooldown_minutes"],
            min_value=0,
            max_value=10080,
        ),
        "stop_account_after_success": _bool_setting(
            raw.get("stop_account_after_success", raw.get("stopAccountAfterSuccess", defaults["stop_account_after_success"])),
            defaults["stop_account_after_success"],
        ),
        "stop_account_after_failure": _bool_setting(
            raw.get("stop_account_after_failure", raw.get("stopAccountAfterFailure", defaults["stop_account_after_failure"])),
            defaults["stop_account_after_failure"],
        ),
    }


async def get_ad_delivery_execution_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_ad_delivery_execution_settings(await _read_setting_payload(db, AD_DELIVERY_EXECUTION_SETTING_KEY))


async def save_ad_delivery_execution_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_ad_delivery_execution_settings(payload)
    await _save_setting_payload(
        db,
        AD_DELIVERY_EXECUTION_SETTING_KEY,
        normalized,
        "Advertisement delivery execution settings",
    )
    return normalized


def normalize_ad_capacity_settings(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    defaults = DEFAULT_AD_CAPACITY_SETTINGS
    tier_defaults = defaults["tier_daily_capacities"]
    configured_hour_defaults = defaults["hourly_weights"]
    hour_defaults = {
        str(hour): int(configured_hour_defaults.get(str(hour), 1) or 1)
        for hour in range(24)
    }
    normalized = {
        "enabled": _bool_setting(raw.get("enabled", defaults["enabled"]), defaults["enabled"]),
        "timezone_offset_hours": _int_setting(
            raw.get("timezone_offset_hours", raw.get("timezoneOffsetHours", defaults["timezone_offset_hours"])),
            defaults["timezone_offset_hours"],
            min_value=-12,
            max_value=14,
        ),
        "window_start_hour": _int_setting(
            raw.get("window_start_hour", raw.get("windowStartHour", defaults["window_start_hour"])),
            defaults["window_start_hour"],
            min_value=0,
            max_value=23,
        ),
        "window_end_hour": _int_setting(
            raw.get("window_end_hour", raw.get("windowEndHour", defaults["window_end_hour"])),
            defaults["window_end_hour"],
            min_value=0,
            max_value=23,
        ),
        "survival_check_delay_seconds": _int_setting(
            raw.get("survival_check_delay_seconds", raw.get("survivalCheckDelaySeconds", defaults["survival_check_delay_seconds"])),
            defaults["survival_check_delay_seconds"],
            min_value=30,
            max_value=3600,
        ),
        "survival_one_hour_seconds": _int_setting(
            raw.get("survival_one_hour_seconds", raw.get("survivalOneHourSeconds", defaults["survival_one_hour_seconds"])),
            defaults["survival_one_hour_seconds"],
            min_value=300,
            max_value=7200,
        ),
        "survival_twenty_four_hour_seconds": _int_setting(
            raw.get(
                "survival_twenty_four_hour_seconds",
                raw.get("survivalTwentyFourHourSeconds", defaults["survival_twenty_four_hour_seconds"]),
            ),
            defaults["survival_twenty_four_hour_seconds"],
            min_value=3600,
            max_value=172800,
        ),
        "survival_check_batch_size": _int_setting(
            raw.get("survival_check_batch_size", raw.get("survivalCheckBatchSize", defaults["survival_check_batch_size"])),
            defaults["survival_check_batch_size"],
            min_value=1,
            max_value=500,
        ),
        "account_group_daily_cap_default": _int_setting(
            raw.get("account_group_daily_cap_default", raw.get("accountGroupDailyCapDefault", defaults["account_group_daily_cap_default"])),
            defaults["account_group_daily_cap_default"],
            min_value=1,
            max_value=500,
        ),
        "survival_retry_max_attempts": _int_setting(
            raw.get(
                "survival_retry_max_attempts",
                raw.get("survivalRetryMaxAttempts", defaults["survival_retry_max_attempts"]),
            ),
            defaults["survival_retry_max_attempts"],
            min_value=1,
            max_value=10,
        ),
        "survival_retry_base_seconds": _int_setting(
            raw.get(
                "survival_retry_base_seconds",
                raw.get("survivalRetryBaseSeconds", defaults["survival_retry_base_seconds"]),
            ),
            defaults["survival_retry_base_seconds"],
            min_value=60,
            max_value=3600,
        ),
        "account_ad_daily_hard_cap": _int_setting(
            raw.get("account_ad_daily_hard_cap", raw.get("accountAdDailyHardCap", defaults["account_ad_daily_hard_cap"])),
            defaults["account_ad_daily_hard_cap"],
            min_value=1,
            max_value=500,
        ),
        "group_global_daily_hard_cap": _int_setting(
            raw.get(
                "group_global_daily_hard_cap",
                raw.get("groupGlobalDailyHardCap", defaults["group_global_daily_hard_cap"]),
            ),
            defaults["group_global_daily_hard_cap"],
            min_value=1,
            max_value=400,
        ),
        "group_min_interval_seconds": _int_setting(
            raw.get(
                "group_min_interval_seconds",
                raw.get("groupMinIntervalSeconds", defaults["group_min_interval_seconds"]),
            ),
            defaults["group_min_interval_seconds"],
            min_value=60,
            max_value=3600,
        ),
        "max_groups_per_account": _int_setting(
            raw.get("max_groups_per_account", raw.get("maxGroupsPerAccount", defaults["max_groups_per_account"])),
            defaults["max_groups_per_account"],
            min_value=1,
            max_value=1000,
        ),
        "max_new_ad_groups_per_day": _int_setting(
            raw.get("max_new_ad_groups_per_day", raw.get("maxNewAdGroupsPerDay", defaults["max_new_ad_groups_per_day"])),
            defaults["max_new_ad_groups_per_day"],
            min_value=0,
            max_value=500,
        ),
        "leave_on_deleted_ad": _bool_setting(
            raw.get("leave_on_deleted_ad", raw.get("leaveOnDeletedAd", defaults["leave_on_deleted_ad"])),
            defaults["leave_on_deleted_ad"],
        ),
        "block_group_on_probe_failure": _bool_setting(
            raw.get("block_group_on_probe_failure", raw.get("blockGroupOnProbeFailure", defaults["block_group_on_probe_failure"])),
            defaults["block_group_on_probe_failure"],
        ),
        "ad_policy_ai_enabled": _bool_setting(
            raw.get("ad_policy_ai_enabled", raw.get("adPolicyAiEnabled", defaults["ad_policy_ai_enabled"])),
            defaults["ad_policy_ai_enabled"],
        ),
        "ad_policy_ai_model": str(
            raw.get("ad_policy_ai_model", raw.get("adPolicyAiModel", defaults["ad_policy_ai_model"]))
            or defaults["ad_policy_ai_model"]
        ).strip()[:100],
        "ad_policy_ai_timeout_seconds": _int_setting(
            raw.get(
                "ad_policy_ai_timeout_seconds",
                raw.get("adPolicyAiTimeoutSeconds", defaults["ad_policy_ai_timeout_seconds"]),
            ),
            defaults["ad_policy_ai_timeout_seconds"],
            min_value=5,
            max_value=120,
        ),
        "ad_policy_ai_min_confidence": _int_setting(
            raw.get(
                "ad_policy_ai_min_confidence",
                raw.get("adPolicyAiMinConfidence", defaults["ad_policy_ai_min_confidence"]),
            ),
            defaults["ad_policy_ai_min_confidence"],
            min_value=90,
            max_value=100,
        ),
        "ad_policy_ai_require_second_pass": _bool_setting(
            raw.get(
                "ad_policy_ai_require_second_pass",
                raw.get("adPolicyAiRequireSecondPass", defaults["ad_policy_ai_require_second_pass"]),
            ),
            defaults["ad_policy_ai_require_second_pass"],
        ),
        "ad_policy_auto_ttl_days": _int_setting(
            raw.get("ad_policy_auto_ttl_days", raw.get("adPolicyAutoTtlDays", defaults["ad_policy_auto_ttl_days"])),
            defaults["ad_policy_auto_ttl_days"],
            min_value=1,
            max_value=90,
        ),
        "ad_policy_manual_ttl_days": _int_setting(
            raw.get("ad_policy_manual_ttl_days", raw.get("adPolicyManualTtlDays", defaults["ad_policy_manual_ttl_days"])),
            defaults["ad_policy_manual_ttl_days"],
            min_value=1,
            max_value=365,
        ),
        "premium_min_samples": _int_setting(
            raw.get("premium_min_samples", raw.get("premiumMinSamples", defaults["premium_min_samples"])),
            defaults["premium_min_samples"],
            min_value=1,
            max_value=1000,
        ),
        "premium_min_conversions": _int_setting(
            raw.get("premium_min_conversions", raw.get("premiumMinConversions", defaults["premium_min_conversions"])),
            defaults["premium_min_conversions"],
            min_value=1,
            max_value=1000,
        ),
        "premium_survival_rate_percent": _int_setting(
            raw.get(
                "premium_survival_rate_percent",
                raw.get("premiumSurvivalRatePercent", defaults["premium_survival_rate_percent"]),
            ),
            defaults["premium_survival_rate_percent"],
            min_value=50,
            max_value=100,
        ),
        "premium_growth_samples": _int_setting(
            raw.get("premium_growth_samples", raw.get("premiumGrowthSamples", defaults["premium_growth_samples"])),
            defaults["premium_growth_samples"],
            min_value=20,
            max_value=1000,
        ),
        "premium_full_capacity_samples": _int_setting(
            raw.get(
                "premium_full_capacity_samples",
                raw.get("premiumFullCapacitySamples", defaults["premium_full_capacity_samples"]),
            ),
            defaults["premium_full_capacity_samples"],
            min_value=20,
            max_value=5000,
        ),
        "premium_entry_capacity": _int_setting(
            raw.get("premium_entry_capacity", raw.get("premiumEntryCapacity", defaults["premium_entry_capacity"])),
            defaults["premium_entry_capacity"],
            min_value=1,
            max_value=400,
        ),
        "premium_growth_capacity": _int_setting(
            raw.get("premium_growth_capacity", raw.get("premiumGrowthCapacity", defaults["premium_growth_capacity"])),
            defaults["premium_growth_capacity"],
            min_value=1,
            max_value=400,
        ),
        "premium_conversion_capacity_step": _int_setting(
            raw.get(
                "premium_conversion_capacity_step",
                raw.get("premiumConversionCapacityStep", defaults["premium_conversion_capacity_step"]),
            ),
            defaults["premium_conversion_capacity_step"],
            min_value=1,
            max_value=100,
        ),
        "premium_clean_days_auto": _int_setting(
            raw.get("premium_clean_days_auto", raw.get("premiumCleanDaysAuto", defaults["premium_clean_days_auto"])),
            defaults["premium_clean_days_auto"],
            min_value=3,
            max_value=30,
        ),
        "premium_clean_days_verified": _int_setting(
            raw.get(
                "premium_clean_days_verified",
                raw.get("premiumCleanDaysVerified", defaults["premium_clean_days_verified"]),
            ),
            defaults["premium_clean_days_verified"],
            min_value=3,
            max_value=30,
        ),
        "deleted_ad_pause_hours": _int_setting(
            raw.get("deleted_ad_pause_hours", raw.get("deletedAdPauseHours", defaults["deleted_ad_pause_hours"])),
            defaults["deleted_ad_pause_hours"],
            min_value=1,
            max_value=720,
        ),
        "membership_delete_block_count": _int_setting(
            raw.get(
                "membership_delete_block_count",
                raw.get("membershipDeleteBlockCount", defaults["membership_delete_block_count"]),
            ),
            defaults["membership_delete_block_count"],
            min_value=1,
            max_value=20,
        ),
        "warmup_days_before_ads": _int_setting(
            raw.get("warmup_days_before_ads", raw.get("warmupDaysBeforeAds", defaults["warmup_days_before_ads"])),
            defaults["warmup_days_before_ads"],
            min_value=0,
            max_value=90,
        ),
        "warmup_daily_interactions_min": _int_setting(
            raw.get(
                "warmup_daily_interactions_min",
                raw.get("warmupDailyInteractionsMin", defaults["warmup_daily_interactions_min"]),
            ),
            defaults["warmup_daily_interactions_min"],
            min_value=0,
            max_value=100,
        ),
        "warmup_daily_interactions_max": _int_setting(
            raw.get(
                "warmup_daily_interactions_max",
                raw.get("warmupDailyInteractionsMax", defaults["warmup_daily_interactions_max"]),
            ),
            defaults["warmup_daily_interactions_max"],
            min_value=0,
            max_value=100,
        ),
        "mature_daily_interactions_min": _int_setting(
            raw.get(
                "mature_daily_interactions_min",
                raw.get("matureDailyInteractionsMin", defaults["mature_daily_interactions_min"]),
            ),
            defaults["mature_daily_interactions_min"],
            min_value=0,
            max_value=100,
        ),
        "mature_daily_interactions_max": _int_setting(
            raw.get(
                "mature_daily_interactions_max",
                raw.get("matureDailyInteractionsMax", defaults["mature_daily_interactions_max"]),
            ),
            defaults["mature_daily_interactions_max"],
            min_value=0,
            max_value=100,
        ),
        "tier_daily_capacities": _normalize_int_map(
            raw.get("tier_daily_capacities", raw.get("tierDailyCapacities", tier_defaults)),
            default=tier_defaults,
            min_value=0,
            max_value=10000,
        ),
        "hourly_weights": _normalize_int_map(
            raw.get("hourly_weights", raw.get("hourlyWeights", hour_defaults)),
            default=hour_defaults,
            min_value=0,
            max_value=10000,
        ),
    }
    for prefix in ("warmup", "mature"):
        min_key = f"{prefix}_daily_interactions_min"
        max_key = f"{prefix}_daily_interactions_max"
        if normalized[max_key] < normalized[min_key]:
            normalized[max_key] = normalized[min_key]
    tier_hard_caps = {
        "blocked": 0,
        "observing": 0,
        "trial": 1,
        "validated": 20,
        "stable": 80,
        "low": 20,
        "medium": 80,
        "high": 200,
        "premium": 400,
    }
    for tier, hard_cap in tier_hard_caps.items():
        current = int(normalized["tier_daily_capacities"].get(tier, hard_cap) or 0)
        normalized["tier_daily_capacities"][tier] = min(current, hard_cap)
    return normalized


async def get_ad_capacity_settings(db: AsyncSession) -> dict[str, Any]:
    return normalize_ad_capacity_settings(await _read_setting_payload(db, AD_CAPACITY_SETTING_KEY))


async def save_ad_capacity_settings(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_ad_capacity_settings(payload)
    await _save_setting_payload(
        db,
        AD_CAPACITY_SETTING_KEY,
        normalized,
        "Advertisement capacity and survival settings",
    )
    return normalized
