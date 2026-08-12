"""
Runtime settings helpers shared by API and background modules.

The persisted settings file is intentionally lightweight JSON so runtime
workers can enforce admin switches without needing a database migration.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

SETTINGS_FILE = Path("/app/uploads/settings.json")
DEFAULT_AUTO_JOIN_TITLE_BLACKLIST = [
    "机场",
    "VPN",
    "节点",
    "免备案",
    "IDC",
    "服务器",
    "云服务器",
    "阿里云",
    "腾讯云",
    "华为云",
    "刷单",
    "数据",
    "租机",
    "借条",
    "股票",
    "配资",
    "担保",
    "电诈",
    "私房",
    "羊毛",
    "黑帽",
    "灰产",
    "四件套",
    "护照",
    "实名",
    "银行卡",
    "币盘",
    "交易所",
    "群发",
    "打粉",
    "引流",
    "云控",
    "协议号",
    "跑分",
    "博彩",
    "盘口",
    "资金盘",
]
DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "scan_interval_minutes": 5,
    "search_filter": {
        "title_blacklist_enabled": True,
        "title_blacklist": DEFAULT_AUTO_JOIN_TITLE_BLACKLIST,
    },
    "join_verification": {
        "enabled": True,
        "ai_enabled": True,
        "confidence_threshold": 0.72,
        "post_action_wait_seconds": 8,
        "post_action_recheck_attempts": 3,
        "post_action_extra_wait_seconds": 12,
        "message_limit": 20,
        "ai_timeout_seconds": 45,
        "action_timeout_seconds": 5,
        "pending_sync_min_age_seconds": 120,
        "pending_sync_limit": 5,
        "unknown_challenge_action": "leave",
        "allow_button_clicks": True,
        "allow_text_answers": True,
        "answer_profile": "中文用户，主要为了学习交流、找资料、行业沟通。",
    },
    "group_capacity_cleanup": {
        "enabled": True,
        "no_conversion_days": 30,
        "min_join_age_days": 30,
        "max_cleanup_per_run": 2,
        "interval_hours": 24,
    },
}
DEFAULT_KEYWORD_PRIVATE_REPLY_SETTINGS: dict[str, Any] = {
    "enabled": False,
}
DEFAULT_GROUP_AI_INTERACTION_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "aiEnabled": False,
    "dailyTokenBudget": 0,
    "maxRepliesPerGroupPerDay": 3,
    "maxRepliesPerAccountPerDay": 20,
    "cooldownSeconds": 900,
    "replyMaxChars": 120,
    "blockAiSelfDisclosure": True,
    "mode": "assistive",
    "tone": "natural",
    "temperature": 0.6,
    "maxTokens": 180,
    "allowKeywordTriggeredReply": False,
    "allowSemanticTriggeredReply": True,
    "semanticScanWindowMessages": 100,
    "semanticEvaluateEveryMessages": 100,
    "semanticMinConfidence": 0.78,
    "semanticMinTextChars": 4,
    "semanticAllowedIntents": [
        "question",
        "buying_interest",
        "problem",
        "recommendation_request",
        "experience_request",
    ],
    "semanticBlockedIntents": [
        "smalltalk",
        "thanks",
        "emoji",
        "command",
        "spam",
        "ad",
        "sensitive",
    ],
    "semanticDecisionPrompt": (
        "你需要从最近的Telegram群聊消息中，选择最值得自然回复的一条真实用户消息。"
        "只有当消息明确表达问题、需求、使用障碍、推荐请求或可自然接话的经验讨论时才回复；"
        "闲聊、表情、感谢、广告、命令、敏感内容、低质量短句都不要回复。"
    ),
    "allowProactiveWarmup": False,
    "proactiveWarmupIntervalMinutes": 30,
    "proactiveWarmupMaxGroupsPerRun": 5,
    "proactiveWarmupMaxPerGroupPerDay": 2,
    "proactiveWarmupMaxPerAccountPerDay": 20,
    "proactiveWarmupCooldownSeconds": 3600,
    "proactiveWarmupWindowStartHour": 9,
    "proactiveWarmupWindowEndHour": 2,
    "proactiveWarmupTopics": [
        "节点稳定性",
        "工具使用体验",
        "账号风控经验",
        "自动化效率",
        "群内常见问题",
    ],
    "proactiveWarmupTemplates": [
        "最近大家用节点稳定吗？有没有哪种线路体验比较好？",
        "你们平时会怎么判断一个工具到底稳不稳定？",
        "群里有人最近遇到账号风控吗？一般怎么处理比较稳？",
        "感觉自动化最麻烦的还是细节限制，大家一般怎么控频率？",
        "这个问题我也挺关心的，想听听群里有没有实际经验。",
    ],
    "proactiveWarmupGroupOverrides": {},
    "systemPrompt": "你是一个中文Telegram社群客服助手，回复要简洁、自然、友好，不要提及你是AI。",
}
DEFAULT_PRIVATE_MESSAGING_SETTINGS: dict[str, Any] = {
    "inbound_replies_enabled": True,
    "proactive_enabled": False,
}
DEFAULT_ACCOUNT_RISK_GUARD_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "global_daily_limit": 200,
    "redis_fail_closed": None,
    "actions": {
        "search": {"daily_limit": 100, "cooldown_seconds": 30},
        "join": {"daily_limit": 15, "cooldown_seconds": 1200},
        "private_message": {"daily_limit": 20, "cooldown_seconds": 300},
        "group_message": {"daily_limit": 20, "cooldown_seconds": 300},
        "ad_probe": {"daily_limit": 10, "cooldown_seconds": 1800},
        "ai_warmup": {"daily_limit": 10, "cooldown_seconds": 1800},
        "moderation": {"daily_limit": 60, "cooldown_seconds": 15},
        "ad_delivery": {"daily_limit": 50, "cooldown_seconds": 300},
        "profile_update": {"daily_limit": 5, "cooldown_seconds": 3600},
        "reaction": {"daily_limit": 120, "cooldown_seconds": 10},
        "forward": {"daily_limit": 25, "cooldown_seconds": 120},
        "pin": {"daily_limit": 20, "cooldown_seconds": 120},
        "bot_message": {"daily_limit": 500, "cooldown_seconds": 1},
        "bot_pin": {"daily_limit": 100, "cooldown_seconds": 5},
        "channel_create": {"daily_limit": 1, "cooldown_seconds": 86400},
    },
    "level_thresholds": {
        "watch": 20.0,
        "limited": 45.0,
        "frozen": 70.0,
        "quarantined": 90.0,
    },
    "level_budget_multipliers": {
        "normal": 1.0,
        "watch": 0.7,
        "limited": 0.45,
        "frozen": 0.0,
        "quarantined": 0.0,
    },
    "risk_score_deltas": {
        "group_write_forbidden": 4.0,
        "platform_group_write_forbidden": 12.0,
        "flood_wait": 15.0,
        "peer_flood": 35.0,
        "account_banned": 50.0,
        "account_restricted": 50.0,
        "generic_failure": 5.0,
        "block": 1.0,
    },
    "lifecycle": {
        "default_freeze_seconds": 3600,
        "flood_wait_buffer_seconds": 60,
        "peer_flood_freeze_seconds": 86400,
        "account_restricted_freeze_seconds": 86400,
        "group_write_forbidden_freeze_seconds": 43200,
        "recovery_seconds": 86400,
        "post_freeze_score_cap": 69.0,
        "manual_clear_score_cap": 44.0,
        "decay_interval_hours": 24,
        "decay_points_per_interval": 8.0,
        "new_account_days": 3,
        "new_account_multiplier": 0.3,
        "recovery_multiplier": 0.5,
        "healthy_account_days": 14,
        "healthy_account_multiplier": 1.0,
        "max_budget_multiplier": 1.0,
    },
    "group_write_forbidden": {
        "leave_after_failures": 2,
        "leave_window_hours": 24,
        "freeze_window_hours": 2,
        "freeze_distinct_groups": 5,
        "quarantine_window_hours": 24,
        "quarantine_distinct_groups": 10,
    },
    "retention": {
        "low_value_detail_retention_days": 14,
        "high_value_detail_retention_days": 90,
        "daily_stat_retention_days": 370,
    },
}
DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "tiers": {
        "unknown": {
            "join_multiplier": 0.6,
            "ad_multiplier": 0.5,
            "run_multiplier": 0.5,
            "probe_multiplier": 0.7,
            "warmup_days": 18,
            "age_floor_days": 0,
        },
        "month_1": {
            "join_multiplier": 0.4,
            "ad_multiplier": 0.25,
            "run_multiplier": 0.25,
            "probe_multiplier": 0.45,
            "warmup_days": 25,
            "age_floor_days": 30,
        },
        "month_3_6": {
            "join_multiplier": 0.7,
            "ad_multiplier": 0.6,
            "run_multiplier": 0.6,
            "probe_multiplier": 0.75,
            "warmup_days": 18,
            "age_floor_days": 120,
        },
        "year_1": {
            "join_multiplier": 1.0,
            "ad_multiplier": 1.0,
            "run_multiplier": 1.0,
            "probe_multiplier": 1.0,
            "warmup_days": 12,
            "age_floor_days": 365,
        },
        "year_2": {
            "join_multiplier": 1.15,
            "ad_multiplier": 1.2,
            "run_multiplier": 1.15,
            "probe_multiplier": 1.1,
            "warmup_days": 9,
            "age_floor_days": 730,
        },
        "year_3_plus": {
            "join_multiplier": 1.3,
            "ad_multiplier": 1.35,
            "run_multiplier": 1.25,
            "probe_multiplier": 1.15,
            "warmup_days": 7,
            "age_floor_days": 1095,
        },
    },
}
DEFAULT_ACCOUNT_WARMUP_POLICY_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "default_warmup_days": 15,
    "minimum_warmup_days": 5,
    "user_initiated_private_message_multiplier": 1.0,
    "tiers": {
        "unknown": {"warmup_days": 15},
        "month_1": {"warmup_days": 18},
        "month_3_6": {"warmup_days": 12},
        "year_1": {"warmup_days": 9},
        "year_2": {"warmup_days": 7},
        "year_3_plus": {"warmup_days": 7},
    },
    "stages": {
        "observe": {
            "limit_multiplier": 0.08,
            "join_multiplier": 0.0,
            "ad_multiplier": 0.0,
            "run_multiplier": 0.0,
            "probe_multiplier": 0.1,
            "private_message_multiplier": 0.0,
            "group_message_multiplier": 0.05,
            "profile_update_multiplier": 0.2,
            "allow_proactive_private_message": False,
        },
        "seed": {
            "limit_multiplier": 0.15,
            "join_multiplier": 0.15,
            "ad_multiplier": 0.0,
            "run_multiplier": 0.0,
            "probe_multiplier": 0.25,
            "private_message_multiplier": 0.0,
            "group_message_multiplier": 0.15,
            "profile_update_multiplier": 0.5,
            "allow_proactive_private_message": False,
        },
        "soft": {
            "limit_multiplier": 0.35,
            "join_multiplier": 0.35,
            "ad_multiplier": 0.25,
            "run_multiplier": 0.25,
            "probe_multiplier": 0.45,
            "private_message_multiplier": 0.1,
            "group_message_multiplier": 0.35,
            "profile_update_multiplier": 0.75,
            "allow_proactive_private_message": False,
        },
        "ramp": {
            "limit_multiplier": 0.65,
            "join_multiplier": 0.65,
            "ad_multiplier": 0.65,
            "run_multiplier": 0.65,
            "probe_multiplier": 0.75,
            "private_message_multiplier": 0.25,
            "group_message_multiplier": 0.65,
            "profile_update_multiplier": 1.0,
            "allow_proactive_private_message": False,
        },
        "normal": {
            "limit_multiplier": 1.0,
            "join_multiplier": 1.0,
            "ad_multiplier": 1.0,
            "run_multiplier": 1.0,
            "probe_multiplier": 1.0,
            "private_message_multiplier": 1.0,
            "group_message_multiplier": 1.0,
            "profile_update_multiplier": 1.0,
            "allow_proactive_private_message": True,
        },
        "cooldown": {
            "limit_multiplier": 0.0,
            "join_multiplier": 0.0,
            "ad_multiplier": 0.0,
            "run_multiplier": 0.0,
            "probe_multiplier": 0.0,
            "private_message_multiplier": 0.0,
            "group_message_multiplier": 0.0,
            "profile_update_multiplier": 0.0,
            "allow_proactive_private_message": False,
        },
    },
}
DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "delivery_interval_seconds": 3600,
    "batch_window_seconds": 3600,
    "batch_size_min": 1,
    "batch_size_max": 1,
    "cooldown_min_seconds": 3600,
    "cooldown_max_seconds": 10800,
}
DEFAULT_AD_DELIVERY_EXECUTION_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "dispatcher_interval_seconds": 60,
    "max_deliveries_per_run": 1,
    "max_deliveries_per_account_per_run": 1,
    "group_campaign_cooldown_minutes": 1440,
    "stop_account_after_success": True,
    "stop_account_after_failure": True,
}
DEFAULT_AD_CAPACITY_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "timezone_offset_hours": 8,
    "window_start_hour": 9,
    "window_end_hour": 2,
    "survival_check_delay_seconds": 120,
    "survival_one_hour_seconds": 3600,
    "survival_twenty_four_hour_seconds": 86400,
    "survival_check_batch_size": 50,
    "survival_retry_max_attempts": 3,
    "survival_retry_base_seconds": 300,
    "account_ad_daily_hard_cap": 5,
    "account_group_daily_cap_default": 3,
    "group_global_daily_hard_cap": 400,
    "group_min_interval_seconds": 3600,
    "max_groups_per_account": 400,
    "max_new_ad_groups_per_day": 3,
    "leave_on_deleted_ad": True,
    "block_group_on_probe_failure": True,
    "ad_policy_ai_enabled": True,
    "ad_policy_ai_model": "gpt-5.6-luna",
    "ad_policy_ai_timeout_seconds": 45,
    "ad_policy_ai_min_confidence": 95,
    "ad_policy_ai_require_second_pass": True,
    "ad_policy_auto_ttl_days": 7,
    "ad_policy_manual_ttl_days": 30,
    "premium_min_samples": 20,
    "premium_min_conversions": 1,
    "premium_growth_samples": 100,
    "premium_full_capacity_samples": 1000,
    "premium_entry_capacity": 20,
    "premium_growth_capacity": 50,
    "premium_conversion_capacity_step": 20,
    "premium_survival_rate_percent": 95,
    "premium_clean_days_auto": 5,
    "premium_clean_days_verified": 3,
    "deleted_ad_pause_hours": 72,
    "membership_delete_block_count": 2,
    "warmup_days_before_ads": 15,
    "warmup_daily_interactions_min": 0,
    "warmup_daily_interactions_max": 1,
    "mature_daily_interactions_min": 0,
    "mature_daily_interactions_max": 1,
    "tier_daily_capacities": {
        "blocked": 0,
        "observing": 0,
        "trial": 1,
        "validated": 3,
        "stable": 10,
        "low": 3,
        "medium": 10,
        "high": 20,
        "premium": 400,
    },
    "hourly_weights": {
        "9": 16,
        "10": 22,
        "11": 24,
        "12": 24,
        "13": 22,
        "14": 32,
        "15": 36,
        "16": 36,
        "17": 32,
        "18": 30,
        "19": 30,
        "20": 28,
        "21": 26,
        "22": 18,
        "23": 12,
        "0": 8,
        "1": 4,
    },
}
DEFAULT_PRIVATE_REPLY_TEMPLATES: dict[str, str] = {
    "startWelcome": (
        "你好 {user_name}！\n\n"
        "欢迎了解我们的服务。\n\n"
        "你可以直接回复想咨询的问题，也可以点击下面的链接注册体验：\n"
        "{register_link}"
    ),
    "help": (
        "可用命令：\n"
        "/start - 开始咨询\n"
        "/help - 查看帮助\n"
        "/register - 获取注册链接\n"
        "/status - 查看当前状态\n\n"
        "也可以直接告诉我你想了解什么。"
    ),
    "register": "点击下面的链接注册：\n{register_link}\n\n注册后如有问题，可以继续私聊我。",
    "statusFound": "您的当前状态：{status}",
    "statusPending": "正在查询您的状态，请稍候。如果一直没有结果，可以直接回复你的注册手机号或邮箱。",
    "unknownCommand": "暂时不支持这个命令。你可以发送 /help 查看可用命令，或直接描述你的问题。",
    "thanks": "不客气！有需要随时找我。",
    "usageHelp": "可以发送 /help 查看帮助，也可以直接告诉我你想了解什么。",
    "registerIntent": "点击下方链接注册体验：\n{register_link}\n\n注册后有问题可以继续问我。",
    "priceIntent": "套餐和价格以注册页展示为准，你可以先打开链接查看：\n{register_link}",
    "nodeIntent": "服务线路、速度和稳定性以实际可用节点为准，你可以先注册体验：\n{register_link}",
    "default": "收到您的消息了。请直接告诉我你想了解的问题，或发送 /help 查看可用命令。",
    "guideWelcome": "很高兴认识你！有什么可以帮助你的吗？",
    "guideIntroduce": "我们提供稳定易用的服务，适合日常使用和多设备场景。你可以先注册体验。",
    "guideInviteRegister": "新用户可以先注册体验：\n{register_link}",
    "guideConfirm": "注册完成后可以回来告诉我，有问题我继续协助你。",
    "guideTimeout": "看起来你可能暂时离开了。有需要随时回来找我。\n\n注册链接：\n{register_link}",
    "guideNoNeed": "好的，没问题。有需要时随时找我。",
    "guideConfirmSuccess": "太好了，注册成功后就可以开始体验了。有问题随时找我。",
    "guideRegisterReminder": "可以点击这个链接完成注册：\n{register_link}",
    "guideFallback": "没问题，有其他问题随时问我。",
    "triggerInvite": (
        "您好，看到你在群里关注「{keyword}」。\n\n"
        "如果需要进一步了解，可以点击链接查看：\n"
        "{register_link}"
    ),
}
_TEMPLATE_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _normalize_string_list(value: Any, *, default: list[str], max_items: int = 200) -> list[str]:
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
        result.append(text[:80])
        if len(result) >= max_items:
            break
    return result


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

def _int_setting(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def _float_setting(value: Any, default: float, *, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, min_value), max_value)


def _normalize_float_map(value: Any, *, default: dict[str, float], min_value: float, max_value: float) -> dict[str, float]:
    raw = value if isinstance(value, dict) else default
    result: dict[str, float] = {}
    for key, fallback in default.items():
        result[str(key)] = _float_setting(raw.get(str(key), raw.get(key, fallback)), fallback, min_value=min_value, max_value=max_value)
    return result


def render_runtime_template(template: str, variables: Mapping[str, Any]) -> str:
    """Render a lightweight runtime template without failing on unknown braces."""
    if not isinstance(template, str):
        return ""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            return match.group(0)
        value = variables.get(key)
        return "" if value is None else str(value)

    return _TEMPLATE_PLACEHOLDER_PATTERN.sub(replace, template).strip()


def load_runtime_settings() -> dict[str, Any]:
    """Load raw persisted runtime settings."""
    if not SETTINGS_FILE.exists():
        return {}

    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_runtime_settings(payload: dict[str, Any]) -> None:
    """Persist raw runtime settings."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_auto_join_scheduler_settings() -> dict[str, Any]:
    """Return validated auto-join scheduler settings."""
    raw = load_runtime_settings()
    automation = raw.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}
    scheduler = automation.get("autoJoinScheduler", {})
    if not isinstance(scheduler, dict):
        scheduler = {}

    enabled = scheduler.get("enabled", DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["enabled"])
    interval = scheduler.get(
        "scan_interval_minutes",
        scheduler.get("scanIntervalMinutes", DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["scan_interval_minutes"]),
    )
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["scan_interval_minutes"]

    verification = scheduler.get(
        "join_verification",
        scheduler.get("joinVerification", DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["join_verification"]),
    )
    if not isinstance(verification, dict):
        verification = {}
    default_verification = DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["join_verification"]

    try:
        confidence_threshold = float(
            verification.get("confidence_threshold", verification.get("confidenceThreshold", default_verification["confidence_threshold"]))
        )
    except (TypeError, ValueError):
        confidence_threshold = float(default_verification["confidence_threshold"])

    try:
        post_action_wait_seconds = int(
            verification.get(
                "post_action_wait_seconds",
                verification.get("postActionWaitSeconds", default_verification["post_action_wait_seconds"]),
            )
        )
    except (TypeError, ValueError):
        post_action_wait_seconds = int(default_verification["post_action_wait_seconds"])

    try:
        post_action_recheck_attempts = int(
            verification.get(
                "post_action_recheck_attempts",
                verification.get("postActionRecheckAttempts", default_verification["post_action_recheck_attempts"]),
            )
        )
    except (TypeError, ValueError):
        post_action_recheck_attempts = int(default_verification["post_action_recheck_attempts"])

    try:
        post_action_extra_wait_seconds = float(
            verification.get(
                "post_action_extra_wait_seconds",
                verification.get("postActionExtraWaitSeconds", default_verification["post_action_extra_wait_seconds"]),
            )
        )
    except (TypeError, ValueError):
        post_action_extra_wait_seconds = float(default_verification["post_action_extra_wait_seconds"])

    try:
        message_limit = int(
            verification.get("message_limit", verification.get("messageLimit", default_verification["message_limit"]))
        )
    except (TypeError, ValueError):
        message_limit = int(default_verification["message_limit"])

    try:
        ai_timeout_seconds = float(
            verification.get(
                "ai_timeout_seconds",
                verification.get("aiTimeoutSeconds", default_verification["ai_timeout_seconds"]),
            )
        )
    except (TypeError, ValueError):
        ai_timeout_seconds = float(default_verification["ai_timeout_seconds"])

    try:
        action_timeout_seconds = float(
            verification.get(
                "action_timeout_seconds",
                verification.get("actionTimeoutSeconds", default_verification["action_timeout_seconds"]),
            )
        )
    except (TypeError, ValueError):
        action_timeout_seconds = float(default_verification["action_timeout_seconds"])

    try:
        pending_sync_min_age_seconds = int(
            verification.get(
                "pending_sync_min_age_seconds",
                verification.get("pendingSyncMinAgeSeconds", default_verification["pending_sync_min_age_seconds"]),
            )
        )
    except (TypeError, ValueError):
        pending_sync_min_age_seconds = int(default_verification["pending_sync_min_age_seconds"])

    try:
        pending_sync_limit = int(
            verification.get("pending_sync_limit", verification.get("pendingSyncLimit", default_verification["pending_sync_limit"]))
        )
    except (TypeError, ValueError):
        pending_sync_limit = int(default_verification["pending_sync_limit"])

    unknown_action = str(
        verification.get(
            "unknown_challenge_action",
            verification.get("unknownChallengeAction", default_verification["unknown_challenge_action"]),
        )
        or default_verification["unknown_challenge_action"]
    ).strip()
    if unknown_action not in {"leave", "manual", "wait", "skip"}:
        unknown_action = default_verification["unknown_challenge_action"]

    join_verification = {
        "enabled": bool(verification.get("enabled", default_verification["enabled"])),
        "ai_enabled": bool(verification.get("ai_enabled", verification.get("aiEnabled", default_verification["ai_enabled"]))),
        "confidence_threshold": min(max(confidence_threshold, 0.0), 1.0),
        "post_action_wait_seconds": min(max(post_action_wait_seconds, 0), 120),
        "post_action_recheck_attempts": min(max(post_action_recheck_attempts, 1), 10),
        "post_action_extra_wait_seconds": min(max(post_action_extra_wait_seconds, 0.0), 30.0),
        "message_limit": min(max(message_limit, 5), 50),
        "ai_timeout_seconds": min(max(ai_timeout_seconds, 1.0), 45.0),
        "action_timeout_seconds": min(max(action_timeout_seconds, 1.0), 20.0),
        "pending_sync_min_age_seconds": min(max(pending_sync_min_age_seconds, 30), 3600),
        "pending_sync_limit": min(max(pending_sync_limit, 1), 20),
        "unknown_challenge_action": unknown_action,
        "allow_button_clicks": bool(
            verification.get("allow_button_clicks", verification.get("allowButtonClicks", default_verification["allow_button_clicks"]))
        ),
        "allow_text_answers": bool(
            verification.get("allow_text_answers", verification.get("allowTextAnswers", default_verification["allow_text_answers"]))
        ),
        "answer_profile": str(
            verification.get(
                "answer_profile",
                verification.get("answerProfile", default_verification["answer_profile"]),
            )
            or default_verification["answer_profile"]
        ).strip()[:500],
    }

    search_filter = scheduler.get(
        "search_filter",
        scheduler.get("searchFilter", DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["search_filter"]),
    )
    if not isinstance(search_filter, dict):
        search_filter = {}
    default_filter = DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["search_filter"]
    title_blacklist = _normalize_string_list(
        search_filter.get(
            "title_blacklist",
            search_filter.get("titleBlacklist", default_filter["title_blacklist"]),
        ),
        default=default_filter["title_blacklist"],
    )
    normalized_search_filter = {
        "title_blacklist_enabled": bool(
            search_filter.get(
                "title_blacklist_enabled",
                search_filter.get("titleBlacklistEnabled", default_filter["title_blacklist_enabled"]),
            )
        ),
        "title_blacklist": title_blacklist,
    }

    cleanup = scheduler.get(
        "group_capacity_cleanup",
        scheduler.get("groupCapacityCleanup", DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["group_capacity_cleanup"]),
    )
    if not isinstance(cleanup, dict):
        cleanup = {}
    default_cleanup = DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["group_capacity_cleanup"]
    group_capacity_cleanup = {
        "enabled": bool(cleanup.get("enabled", default_cleanup["enabled"])),
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
    }

    return {
        "enabled": bool(enabled),
        "scan_interval_minutes": min(max(interval, 1), 1440),
        "search_filter": normalized_search_filter,
        "join_verification": join_verification,
        "group_capacity_cleanup": group_capacity_cleanup,
    }


def save_auto_join_scheduler_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Merge and persist auto-join scheduler settings."""
    raw = load_runtime_settings()
    automation = raw.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}
    scheduler = automation.get("autoJoinScheduler", {})
    if not isinstance(scheduler, dict):
        scheduler = {}

    scheduler.update(
        {
            "enabled": bool(config.get("enabled", DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["enabled"])),
            "scan_interval_minutes": int(
                config.get(
                    "scan_interval_minutes",
                    DEFAULT_AUTO_JOIN_SCHEDULER_SETTINGS["scan_interval_minutes"],
                )
            ),
        }
    )
    if "join_verification" in config or "joinVerification" in config:
        verification = config.get("join_verification", config.get("joinVerification"))
        if isinstance(verification, dict):
            existing = scheduler.get("join_verification", {})
            if not isinstance(existing, dict):
                existing = {}
            existing.update(verification)
            scheduler["join_verification"] = existing
    if "search_filter" in config or "searchFilter" in config:
        search_filter = config.get("search_filter", config.get("searchFilter"))
        if isinstance(search_filter, dict):
            existing_filter = scheduler.get("search_filter", {})
            if not isinstance(existing_filter, dict):
                existing_filter = {}
            existing_filter.update(search_filter)
            scheduler["search_filter"] = existing_filter
    if "group_capacity_cleanup" in config or "groupCapacityCleanup" in config:
        cleanup = config.get("group_capacity_cleanup", config.get("groupCapacityCleanup"))
        if isinstance(cleanup, dict):
            existing_cleanup = scheduler.get("group_capacity_cleanup", {})
            if not isinstance(existing_cleanup, dict):
                existing_cleanup = {}
            existing_cleanup.update(cleanup)
            scheduler["group_capacity_cleanup"] = existing_cleanup
    automation["autoJoinScheduler"] = scheduler
    raw["automation"] = automation
    save_runtime_settings(raw)
    return get_auto_join_scheduler_settings()


def is_ai_reply_enabled() -> bool:
    """Return True only when admins explicitly enable AI auto replies."""
    ai_reply = load_runtime_settings().get("aiReply", {})
    if not isinstance(ai_reply, dict):
        return False
    return bool(ai_reply.get("enabled", False))


def get_keyword_private_reply_settings() -> dict[str, Any]:
    """Return runtime settings for keyword-triggered private replies."""
    raw = load_runtime_settings().get("keywordPrivateReply", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(
            raw.get(
                "enabled",
                DEFAULT_KEYWORD_PRIVATE_REPLY_SETTINGS["enabled"],
            )
        ),
    }


def is_keyword_private_reply_enabled() -> bool:
    """Return whether keyword-triggered private messages may be sent."""
    return get_keyword_private_reply_settings()["enabled"]


def get_private_messaging_settings() -> dict[str, Any]:
    """Return runtime settings for acquisition private message sending."""
    raw = load_runtime_settings().get("privateMessaging", {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "inbound_replies_enabled": bool(
            raw.get(
                "inbound_replies_enabled",
                raw.get(
                    "inboundRepliesEnabled",
                    raw.get("enabled", DEFAULT_PRIVATE_MESSAGING_SETTINGS["inbound_replies_enabled"]),
                ),
            )
        ),
        "proactive_enabled": bool(
            raw.get(
                "proactive_enabled",
                raw.get("proactiveEnabled", DEFAULT_PRIVATE_MESSAGING_SETTINGS["proactive_enabled"]),
            )
        ),
    }


def get_private_reply_template_settings() -> dict[str, str]:
    """Return validated templates for user-initiated private replies."""
    raw_settings = load_runtime_settings()
    private_messaging = raw_settings.get("privateMessaging", {})
    if not isinstance(private_messaging, dict):
        private_messaging = {}

    templates = private_messaging.get("templates", raw_settings.get("privateReplyTemplates", {}))
    if not isinstance(templates, dict):
        templates = {}

    normalized = dict(DEFAULT_PRIVATE_REPLY_TEMPLATES)
    for key in DEFAULT_PRIVATE_REPLY_TEMPLATES:
        if key not in templates:
            continue
        value = templates.get(key)
        if isinstance(value, str):
            normalized[key] = value.strip()[:4000]
    return normalized


def is_private_messaging_enabled(*, initiated_by_user: bool = False) -> bool:
    """Return whether acquisition private messages may be sent."""
    settings = get_private_messaging_settings()
    if initiated_by_user:
        return settings["inbound_replies_enabled"]
    return settings["proactive_enabled"]


def get_ad_delivery_throttle_settings() -> dict[str, Any]:
    """Return validated per-account advertisement delivery throttle settings."""
    raw_settings = load_runtime_settings()
    automation = raw_settings.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}
    raw = automation.get("adDeliveryThrottle", raw_settings.get("adDeliveryThrottle", {}))
    if not isinstance(raw, dict):
        raw = {}

    batch_size_min = _int_setting(
        raw.get("batch_size_min", raw.get("batchSizeMin", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["batch_size_min"])),
        DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["batch_size_min"],
        min_value=1,
        max_value=10000,
    )
    batch_size_max = _int_setting(
        raw.get("batch_size_max", raw.get("batchSizeMax", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["batch_size_max"])),
        DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["batch_size_max"],
        min_value=1,
        max_value=10000,
    )
    if batch_size_max < batch_size_min:
        batch_size_max = batch_size_min

    cooldown_min_seconds = _int_setting(
        raw.get(
            "cooldown_min_seconds",
            raw.get("cooldownMinSeconds", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["cooldown_min_seconds"]),
        ),
        DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["cooldown_min_seconds"],
        min_value=0,
        max_value=86400,
    )
    cooldown_max_seconds = _int_setting(
        raw.get(
            "cooldown_max_seconds",
            raw.get("cooldownMaxSeconds", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["cooldown_max_seconds"]),
        ),
        DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["cooldown_max_seconds"],
        min_value=0,
        max_value=86400,
    )
    if cooldown_max_seconds < cooldown_min_seconds:
        cooldown_max_seconds = cooldown_min_seconds

    return {
        "enabled": bool(raw.get("enabled", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["enabled"])),
        "delivery_interval_seconds": _int_setting(
            raw.get(
                "delivery_interval_seconds",
                raw.get("deliveryIntervalSeconds", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["delivery_interval_seconds"]),
            ),
            DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["delivery_interval_seconds"],
            min_value=0,
            max_value=3600,
        ),
        "batch_window_seconds": _int_setting(
            raw.get(
                "batch_window_seconds",
                raw.get("batchWindowSeconds", DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["batch_window_seconds"]),
            ),
            DEFAULT_AD_DELIVERY_THROTTLE_SETTINGS["batch_window_seconds"],
            min_value=1,
            max_value=3600,
        ),
        "batch_size_min": batch_size_min,
        "batch_size_max": batch_size_max,
        "cooldown_min_seconds": cooldown_min_seconds,
        "cooldown_max_seconds": cooldown_max_seconds,
    }


def save_ad_delivery_throttle_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Merge and persist advertisement delivery throttle settings."""
    raw = load_runtime_settings()
    automation = raw.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}

    current = get_ad_delivery_throttle_settings()
    throttle = automation.get("adDeliveryThrottle", {})
    if not isinstance(throttle, dict):
        throttle = {}

    throttle.update(
        {
            "enabled": _bool_setting(config.get("enabled", current["enabled"]), current["enabled"]),
            "delivery_interval_seconds": _int_setting(
                config.get("delivery_interval_seconds", config.get("deliveryIntervalSeconds", current["delivery_interval_seconds"])),
                current["delivery_interval_seconds"],
                min_value=0,
                max_value=3600,
            ),
            "batch_window_seconds": _int_setting(
                config.get("batch_window_seconds", config.get("batchWindowSeconds", current["batch_window_seconds"])),
                current["batch_window_seconds"],
                min_value=1,
                max_value=3600,
            ),
            "batch_size_min": _int_setting(
                config.get("batch_size_min", config.get("batchSizeMin", current["batch_size_min"])),
                current["batch_size_min"],
                min_value=1,
                max_value=10000,
            ),
            "batch_size_max": _int_setting(
                config.get("batch_size_max", config.get("batchSizeMax", current["batch_size_max"])),
                current["batch_size_max"],
                min_value=1,
                max_value=10000,
            ),
            "cooldown_min_seconds": _int_setting(
                config.get("cooldown_min_seconds", config.get("cooldownMinSeconds", current["cooldown_min_seconds"])),
                current["cooldown_min_seconds"],
                min_value=0,
                max_value=86400,
            ),
            "cooldown_max_seconds": _int_setting(
                config.get("cooldown_max_seconds", config.get("cooldownMaxSeconds", current["cooldown_max_seconds"])),
                current["cooldown_max_seconds"],
                min_value=0,
                max_value=86400,
            ),
        }
    )
    if throttle["batch_size_max"] < throttle["batch_size_min"]:
        throttle["batch_size_max"] = throttle["batch_size_min"]
    if throttle["cooldown_max_seconds"] < throttle["cooldown_min_seconds"]:
        throttle["cooldown_max_seconds"] = throttle["cooldown_min_seconds"]

    automation["adDeliveryThrottle"] = throttle
    raw["automation"] = automation
    save_runtime_settings(raw)
    return get_ad_delivery_throttle_settings()


def get_ad_delivery_execution_settings() -> dict[str, Any]:
    """Return validated advertisement delivery execution settings."""
    raw_settings = load_runtime_settings()
    automation = raw_settings.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}
    raw = automation.get("adDeliveryExecution", raw_settings.get("adDeliveryExecution", {}))
    if not isinstance(raw, dict):
        raw = {}

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


def save_ad_delivery_execution_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Merge and persist advertisement delivery execution settings."""
    raw = load_runtime_settings()
    automation = raw.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}

    current = get_ad_delivery_execution_settings()
    execution = automation.get("adDeliveryExecution", {})
    if not isinstance(execution, dict):
        execution = {}

    execution.update(
        {
            "enabled": _bool_setting(config.get("enabled", current["enabled"]), current["enabled"]),
            "dispatcher_interval_seconds": _int_setting(
                config.get("dispatcher_interval_seconds", config.get("dispatcherIntervalSeconds", current["dispatcher_interval_seconds"])),
                current["dispatcher_interval_seconds"],
                min_value=1,
                max_value=86400,
            ),
            "max_deliveries_per_run": _int_setting(
                config.get("max_deliveries_per_run", config.get("maxDeliveriesPerRun", current["max_deliveries_per_run"])),
                current["max_deliveries_per_run"],
                min_value=1,
                max_value=20,
            ),
            "max_deliveries_per_account_per_run": _int_setting(
                config.get(
                    "max_deliveries_per_account_per_run",
                    config.get("maxDeliveriesPerAccountPerRun", current["max_deliveries_per_account_per_run"]),
                ),
                current["max_deliveries_per_account_per_run"],
                min_value=1,
                max_value=5,
            ),
            "group_campaign_cooldown_minutes": _int_setting(
                config.get("group_campaign_cooldown_minutes", config.get("groupCampaignCooldownMinutes", current["group_campaign_cooldown_minutes"])),
                current["group_campaign_cooldown_minutes"],
                min_value=0,
                max_value=10080,
            ),
            "stop_account_after_success": _bool_setting(
                config.get("stop_account_after_success", config.get("stopAccountAfterSuccess", current["stop_account_after_success"])),
                current["stop_account_after_success"],
            ),
            "stop_account_after_failure": _bool_setting(
                config.get("stop_account_after_failure", config.get("stopAccountAfterFailure", current["stop_account_after_failure"])),
                current["stop_account_after_failure"],
            ),
        }
    )

    automation["adDeliveryExecution"] = execution
    raw["automation"] = automation
    save_runtime_settings(raw)
    return get_ad_delivery_execution_settings()



def get_account_risk_guard_settings() -> dict[str, Any]:
    """Return validated account risk guard settings."""
    raw_settings = load_runtime_settings()
    automation = raw_settings.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}
    raw = automation.get("accountRiskGuard", raw_settings.get("accountRiskGuard", {}))
    if not isinstance(raw, dict):
        raw = {}

    defaults = DEFAULT_ACCOUNT_RISK_GUARD_SETTINGS
    actions_raw = raw.get("actions", {})
    if not isinstance(actions_raw, dict):
        actions_raw = {}
    actions: dict[str, dict[str, int]] = {}
    for action, default_budget in defaults["actions"].items():
        item = actions_raw.get(action, {})
        if not isinstance(item, dict):
            item = {}
        actions[action] = {
            "daily_limit": _int_setting(
                item.get("daily_limit", item.get("dailyLimit", default_budget["daily_limit"])),
                default_budget["daily_limit"],
                min_value=1,
                max_value=100000,
            ),
            "cooldown_seconds": _int_setting(
                item.get("cooldown_seconds", item.get("cooldownSeconds", default_budget["cooldown_seconds"])),
                default_budget["cooldown_seconds"],
                min_value=0,
                max_value=86400,
            ),
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


def save_account_risk_guard_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Merge and persist account risk guard settings."""
    raw = load_runtime_settings()
    automation = raw.get("automation", {})
    if not isinstance(automation, dict):
        automation = {}

    current = get_account_risk_guard_settings()
    guard = automation.get("accountRiskGuard", {})
    if not isinstance(guard, dict):
        guard = {}

    guard["enabled"] = _bool_setting(config.get("enabled", current["enabled"]), current["enabled"])
    guard["global_daily_limit"] = _int_setting(
        config.get("global_daily_limit", config.get("globalDailyLimit", current["global_daily_limit"])),
        current["global_daily_limit"],
        min_value=1,
        max_value=200,
    )

    if "redis_fail_closed" in config or "redisFailClosed" in config:
        redis_fail_closed = config.get("redis_fail_closed", config.get("redisFailClosed"))
        guard["redis_fail_closed"] = None if redis_fail_closed is None else _bool_setting(redis_fail_closed, False)

    raw_actions = config.get("actions", {})
    if isinstance(raw_actions, dict):
        actions = dict(current["actions"])
        for action, default_budget in DEFAULT_ACCOUNT_RISK_GUARD_SETTINGS["actions"].items():
            item = raw_actions.get(action)
            if not isinstance(item, dict):
                continue
            existing = actions.get(action, default_budget)
            actions[action] = {
                "daily_limit": _int_setting(
                    item.get("daily_limit", item.get("dailyLimit", existing["daily_limit"])),
                    existing["daily_limit"],
                    min_value=1,
                    max_value=100000,
                ),
                "cooldown_seconds": _int_setting(
                    item.get("cooldown_seconds", item.get("cooldownSeconds", existing["cooldown_seconds"])),
                    existing["cooldown_seconds"],
                    min_value=0,
                    max_value=86400,
                ),
            }
        guard["actions"] = actions

    for key, camel_key in (
        ("level_thresholds", "levelThresholds"),
        ("level_budget_multipliers", "levelBudgetMultipliers"),
        ("risk_score_deltas", "riskScoreDeltas"),
        ("lifecycle", "lifecycle"),
        ("group_write_forbidden", "groupWriteForbidden"),
        ("retention", "retention"),
    ):
        value = config.get(key, config.get(camel_key))
        if isinstance(value, dict):
            guard[key] = value

    automation["accountRiskGuard"] = guard
    raw["automation"] = automation
    save_runtime_settings(raw)
    return get_account_risk_guard_settings()
