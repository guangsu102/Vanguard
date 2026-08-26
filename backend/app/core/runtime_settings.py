"""Runtime configuration defaults and template helpers shared by the application."""

from __future__ import annotations

import re
from typing import Any, Mapping

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
    "account_outbound_message_hard_cap_default": 30,
    "global_daily_limit": 30,
    "group_write_daily_limit": 8,
    "redis_fail_closed": True,
    "actions": {
        "search": {"daily_limit": 100, "cooldown_seconds": 30},
        "join": {"daily_limit": 6, "cooldown_seconds": 7200},
        "private_message": {"daily_limit": 20, "cooldown_seconds": 300},
        "group_message": {"daily_limit": 4, "cooldown_seconds": 7200},
        "ad_probe": {"daily_limit": 10, "cooldown_seconds": 3600},
        "ai_warmup": {"daily_limit": 1, "cooldown_seconds": 21600},
        "moderation": {"daily_limit": 60, "cooldown_seconds": 15},
        "ad_delivery": {"daily_limit": 100000, "cooldown_seconds": 0},
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
            "age_floor_days": 0,
        },
        "month_1": {
            "join_multiplier": 0.4,
            "ad_multiplier": 0.25,
            "run_multiplier": 0.25,
            "probe_multiplier": 0.45,
            "age_floor_days": 30,
        },
        "month_3_6": {
            "join_multiplier": 0.7,
            "ad_multiplier": 0.6,
            "run_multiplier": 0.6,
            "probe_multiplier": 0.75,
            "age_floor_days": 120,
        },
        "year_1": {
            "join_multiplier": 1.0,
            "ad_multiplier": 1.0,
            "run_multiplier": 1.0,
            "probe_multiplier": 1.0,
            "age_floor_days": 365,
        },
        "year_2": {
            "join_multiplier": 1.15,
            "ad_multiplier": 1.2,
            "run_multiplier": 1.15,
            "probe_multiplier": 1.1,
            "age_floor_days": 730,
        },
        "year_3_plus": {
            "join_multiplier": 1.3,
            "ad_multiplier": 1.35,
            "run_multiplier": 1.25,
            "probe_multiplier": 1.15,
            "age_floor_days": 1095,
        },
    },
}
DEFAULT_ACCOUNT_WARMUP_POLICY_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "default_warmup_days": 15,
    "minimum_warmup_days": 7,
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
    "growth_min_interval_seconds": 1800,
    "growth_max_interval_seconds": 10800,
}
DEFAULT_AD_DELIVERY_EXECUTION_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "dispatcher_interval_seconds": 60,
    "dispatcher_batch_size": 100,
    "max_parallel_accounts": 3,
    "job_lease_seconds": 300,
    "growth_group_global_cooldown_seconds": 86400,
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



    "max_groups_per_account": 400,
    "max_new_ad_groups_per_day": 2,
    "leave_on_deleted_ad": True,
    "block_group_on_probe_failure": True,
    "ad_policy_ai_enabled": True,
    "ad_policy_ai_model": "gpt-5.6-terra",
    "ad_policy_ai_timeout_seconds": 45,
    "ad_policy_ai_min_confidence": 95,
    "ad_policy_ai_require_second_pass": True,
    "ad_policy_auto_probe_enabled": False,
    "ad_policy_auto_probe_daily_limit": 1,
    "ad_policy_auto_probe_daily_limit_per_account": 10,
    "ad_policy_auto_probe_interval_hours": 24,
    "ad_policy_auto_ttl_days": 7,
    "ad_policy_manual_ttl_days": 30,
    "premium_min_samples": 20,
    "premium_min_conversions": 1,
    "premium_growth_samples": 100,
    "premium_full_capacity_samples": 1000,



    "premium_survival_rate_percent": 95,
    "premium_clean_days_auto": 5,
    "premium_clean_days_verified": 3,
    "deleted_ad_pause_hours": 72,
    "membership_delete_block_count": 2,
    "warmup_daily_interactions_min": 0,
    "warmup_daily_interactions_max": 1,
    "mature_daily_interactions_min": 0,
    "mature_daily_interactions_max": 1,
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
