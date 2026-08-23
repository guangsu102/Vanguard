"""
Acquisition automation and advertisement management API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.warmup import account_warmup_context
from app.core.automation_constants import AD_MAX_DELIVERIES_PER_ACCOUNT_PER_RUN
from app.core.automation_settings import (
    get_account_asset_policy_settings,
    get_account_risk_guard_settings,
    get_account_warmup_policy_settings,
    get_ad_capacity_settings,
    get_ad_delivery_execution_settings,
    get_ad_delivery_throttle_settings,
    get_ad_failure_policy_settings,
    get_auto_join_scheduler_settings,
    save_account_asset_policy_settings,
    save_account_risk_guard_settings,
    save_account_warmup_policy_settings,
    save_ad_capacity_settings,
    save_ad_delivery_execution_settings,
    save_ad_delivery_throttle_settings,
    save_ad_failure_policy_settings,
    save_auto_join_scheduler_settings,
)
from app.core.database import get_db
from app.core.effective_limits import build_effective_limit_summary
from app.core.group.models import Group, GroupAccountMembership
from app.core.scheduler.tasks import (
    auto_join_groups_task,
    auto_probe_unknown_group_ad_policies_task,
    check_ad_survival_task,
    deliver_ads_task,
    recover_orphaned_groups_task,
    replenish_keywords_task,
)
from app.core.security import require_admin
from app.modules.acquisition.automation import (
    GROUP_STATUS_AD_BLOCKED,
    HTML_RESPONSE_RE,
    WEB_ERROR_RESPONSE_RE,
    AcquisitionAutomationService,
)
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdCreativeType,
    AdDeliveryLog,
    AdSendMode,
    AutoJoinAttempt,
    GroupAdPolicyEvent,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupFailoverStatus,
    GroupFailoverTask,
)

router = APIRouter()


def _auto_join_verification_log_from_membership(
    membership: GroupAccountMembership,
    group: Group,
) -> Optional[dict[str, Any]]:
    if not membership.note:
        return None
    try:
        note = json.loads(membership.note)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(note, dict):
        return None

    details = note.get("verification_details")
    if not isinstance(details, dict):
        details = {}

    action = note.get("verification_action") or details.get("action")
    attempted = bool(details.get("attempted"))
    if not action or action == "none":
        if not attempted:
            return None
        action = details.get("action") or "none"

    success = details.get("success")
    return {
        "id": membership.id,
        "account_id": membership.account_id,
        "group_id": group.id,
        "telegram_group_id": group.group_id,
        "group_username": group.username,
        "group_title": group.title,
        "membership_status": membership.status,
        "audit_passed": bool(note.get("passed")),
        "audit_reason": note.get("reason"),
        "action": action,
        "source": details.get("decision_source") or details.get("source") or "unknown",
        "challenge_type": details.get("challenge_type") or "unknown",
        "success": bool(success) if success is not None else None,
        "reason": details.get("reason") or note.get("reason"),
        "error": details.get("error"),
        "confidence": details.get("confidence"),
        "decision_reason": details.get("decision_reason"),
        "button_text": details.get("button_text"),
        "answer": details.get("answer"),
        "target_message_id": details.get("target_message_id"),
        "post_action_status": details.get("post_action_status"),
        "post_action_rechecks": details.get("post_action_rechecks") or [],
        "post_action_final_can_send": details.get("post_action_final_can_send"),
        "post_action_final_permission_reason": details.get("post_action_final_permission_reason"),
        "should_retry_audit": bool(details.get("should_retry_audit")),
        "should_leave": bool(details.get("should_leave")),
        "joined_at": membership.joined_at.isoformat() if membership.joined_at else None,
        "updated_at": membership.updated_at.isoformat() if membership.updated_at else "",
    }


# =============================================================================
# Auto-join Scheduler Config
# =============================================================================


class AutoJoinSchedulerConfigUpdate(BaseModel):
    enabled: bool = True
    scan_interval_minutes: int = Field(default=5, ge=1, le=1440)
    search_filter: Optional[dict[str, Any]] = None
    join_verification: Optional[dict[str, Any]] = None
    group_capacity_cleanup: Optional[dict[str, Any]] = None


class AdFailurePolicyUpdate(BaseModel):
    enabled: bool = True
    leave_on_group_control_failure: bool = True
    group_control_failure_limit: int = Field(default=1, ge=1, le=20)
    group_control_failure_window_hours: int = Field(default=720, ge=1, le=720)
    levels: list[str] = Field(default_factory=lambda: ["A", "B", "C", "UNRATED"])


class AccountRiskActionBudgetUpdate(BaseModel):
    daily_limit: int = Field(default=1, ge=1, le=100000)
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)


class AccountRiskGuardUpdate(BaseModel):
    enabled: bool = True
    global_daily_limit: int = Field(default=30, ge=1, le=30)
    group_write_daily_limit: int = Field(default=8, ge=1, le=8)
    redis_fail_closed: Optional[bool] = None
    actions: dict[str, AccountRiskActionBudgetUpdate] = Field(default_factory=dict)
    level_thresholds: dict[str, float] = Field(default_factory=dict)
    level_budget_multipliers: dict[str, float] = Field(default_factory=dict)
    risk_score_deltas: dict[str, float] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    group_write_forbidden: dict[str, Any] = Field(default_factory=dict)
    retention: dict[str, Any] = Field(default_factory=dict)


class AccountAssetTierPolicyUpdate(BaseModel):
    join_multiplier: float = Field(default=1.0, ge=0.0, le=3.0)
    ad_multiplier: float = Field(default=1.0, ge=0.0, le=3.0)
    run_multiplier: float = Field(default=1.0, ge=0.0, le=3.0)
    probe_multiplier: float = Field(default=1.0, ge=0.0, le=3.0)
    age_floor_days: int = Field(default=0, ge=0, le=3650)


class AccountAssetPolicyUpdate(BaseModel):
    enabled: bool = True
    tiers: dict[str, AccountAssetTierPolicyUpdate] = Field(default_factory=dict)


class AccountWarmupTierPolicyUpdate(BaseModel):
    warmup_days: int = Field(default=15, ge=7, le=120)


class AccountWarmupStagePolicyUpdate(BaseModel):
    limit_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    join_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    ad_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    run_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    probe_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    private_message_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    group_message_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    profile_update_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    allow_proactive_private_message: bool = False


class AccountWarmupPolicyUpdate(BaseModel):
    enabled: bool = True
    default_warmup_days: int = Field(default=15, ge=7, le=120)
    minimum_warmup_days: int = Field(default=7, ge=7, le=120)
    user_initiated_private_message_multiplier: float = Field(default=1.0, ge=0.0, le=2.0)
    tiers: dict[str, AccountWarmupTierPolicyUpdate] = Field(default_factory=dict)
    stages: dict[str, AccountWarmupStagePolicyUpdate] = Field(default_factory=dict)


class AdDeliveryThrottleUpdate(BaseModel):
    enabled: bool = True
    delivery_interval_seconds: int = Field(default=9000, ge=9000, le=86400)
    batch_window_seconds: int = Field(default=3600, ge=1, le=3600)
    cooldown_min_seconds: int = Field(default=9000, ge=9000, le=86400)
    cooldown_max_seconds: int = Field(default=10800, ge=9000, le=86400)


class AdDeliveryExecutionUpdate(BaseModel):
    enabled: bool = True
    dispatcher_interval_seconds: int = Field(default=600, ge=1, le=86400)
    group_campaign_cooldown_minutes: int = Field(default=4320, ge=4320, le=10080)
    stop_account_after_success: bool = True
    stop_account_after_failure: bool = True


class AdCapacityUpdate(BaseModel):
    enabled: bool = True
    timezone_offset_hours: int = Field(default=8, ge=-12, le=14)
    window_start_hour: int = Field(default=9, ge=0, le=23)
    window_end_hour: int = Field(default=2, ge=0, le=23)
    survival_check_delay_seconds: int = Field(default=120, ge=30, le=3600)
    survival_one_hour_seconds: int = Field(default=3600, ge=300, le=7200)
    survival_twenty_four_hour_seconds: int = Field(default=86400, ge=3600, le=172800)
    survival_check_batch_size: int = Field(default=50, ge=1, le=500)
    account_ad_daily_hard_cap: int = Field(default=5, ge=1, le=5)
    group_global_daily_hard_cap: int = Field(default=400, ge=1, le=400)
    group_min_interval_seconds: int = Field(default=259200, ge=259200, le=604800)
    max_groups_per_account: int = Field(default=400, ge=1, le=1000)
    max_new_ad_groups_per_day: int = Field(default=2, ge=0, le=2)
    leave_on_deleted_ad: bool = True
    block_group_on_probe_failure: bool = True
    ad_policy_ai_enabled: bool = True
    ad_policy_ai_model: str = Field(default="gpt-5.6-terra", min_length=1, max_length=100)
    ad_policy_ai_timeout_seconds: int = Field(default=45, ge=5, le=120)
    ad_policy_ai_min_confidence: int = Field(default=95, ge=90, le=100)
    ad_policy_ai_require_second_pass: bool = True
    ad_policy_auto_probe_enabled: bool = False
    ad_policy_auto_probe_daily_limit: int = Field(default=1, ge=0, le=20)
    ad_policy_auto_probe_daily_limit_per_account: int = Field(default=10, ge=0, le=20)
    ad_policy_auto_probe_interval_hours: int = Field(default=24, ge=1, le=168)
    ad_policy_auto_ttl_days: int = Field(default=7, ge=1, le=90)
    ad_policy_manual_ttl_days: int = Field(default=30, ge=1, le=365)
    premium_min_samples: int = Field(default=20, ge=1, le=1000)
    premium_min_conversions: int = Field(default=1, ge=1, le=1000)
    premium_survival_rate_percent: int = Field(default=95, ge=50, le=100)
    premium_growth_samples: int = Field(default=100, ge=20, le=1000)
    premium_full_capacity_samples: int = Field(default=1000, ge=20, le=5000)
    premium_entry_capacity: int = Field(default=20, ge=1, le=20)
    premium_growth_capacity: int = Field(default=50, ge=1, le=50)
    premium_conversion_capacity_step: int = Field(default=20, ge=1, le=20)
    premium_clean_days_auto: int = Field(default=5, ge=3, le=30)
    premium_clean_days_verified: int = Field(default=3, ge=3, le=30)
    deleted_ad_pause_hours: int = Field(default=72, ge=1, le=720)
    membership_delete_block_count: int = Field(default=2, ge=1, le=20)
    warmup_daily_interactions_min: int = Field(default=0, ge=0, le=20)
    warmup_daily_interactions_max: int = Field(default=1, ge=0, le=20)
    mature_daily_interactions_min: int = Field(default=0, ge=0, le=20)
    mature_daily_interactions_max: int = Field(default=1, ge=0, le=20)
    tier_daily_capacities: dict[str, int] = Field(
        default_factory=lambda: {
            "blocked": 0,
            "observing": 0,
            "trial": 1,
            "validated": 3,
            "stable": 10,
            "low": 3,
            "medium": 10,
            "high": 20,
            "premium": 400,
        }
    )
    hourly_weights: dict[str, int] = Field(
        default_factory=lambda: {
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
        }
    )


class GroupAdPolicyUpdate(BaseModel):
    mode: str = Field(
        pattern="^(forbidden|unknown|unknown_probe|approval_required|soft_ad_trial|soft_ad_allowed|high_volume_ad_allowed)$"
    )
    confidence: int = Field(default=100, ge=0, le=100)
    expires_days: Optional[int] = Field(default=None, ge=1, le=365)
    note: Optional[str] = Field(default=None, max_length=500)


class GroupAdPolicyProbeRequest(BaseModel):
    account_id: Optional[int] = Field(default=None, ge=1)


def _iso_datetime(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def _campaign_is_active_for_status(campaign: Optional[AdCampaign], now: datetime) -> bool:
    if campaign is None:
        return False
    if not campaign.enabled or campaign.status != "active":
        return False
    if campaign.start_at and now < campaign.start_at:
        return False
    if campaign.end_at and now > campaign.end_at:
        return False
    return True


def _diagnostic_reason(
    reason: str,
    label: str,
    *,
    severity: str = "warning",
    detail: Optional[str] = None,
) -> dict[str, Any]:
    return {"reason": reason, "label": label, "severity": severity, "detail": detail}


_HEALTH_ADJUSTMENT_LABELS = {
    "risk_score": "账号风险分扣减",
    "account_missing": "账号记录缺失",
    "account_inactive": "账号未启用",
    "account_risk_paused": "账号风控暂停",
    "risk_recovery": "账号恢复期",
    "risk_level_watch": "风控观察",
    "risk_level_limited": "风控限流",
    "risk_level_frozen": "风控冻结",
    "risk_level_quarantined": "风控隔离",
    "join_peer_flood": "加群触发 PeerFlood",
    "join_account_restricted": "加群账号受限",
    "join_account_banned": "加群账号封禁",
    "join_flood_wait": "加群 FloodWait",
    "join_success_rate_very_low": "加群成功率很低",
    "join_success_rate_low": "加群成功率偏低",
    "writable_rate_low": "入群后可发言率低",
    "probe_success_rate_low": "探针成功率低",
    "ad_success_rate_low": "广告成功率低",
    "group_control_failures": "群管控失败",
    "account_failures": "账号级发送失败",
    "transient_failures": "临时发送失败",
    "ad_peer_flood": "广告触发 PeerFlood",
    "ad_account_restricted": "广告账号受限",
    "ad_delivery_success_rate_low": "广告投放成功率低",
    "group_quality_low": "群质量偏低",
}


def _health_adjustment_label(reason: str) -> str:
    if reason.startswith("account_status_"):
        return f"账号状态 {reason.removeprefix('account_status_')}"
    return _HEALTH_ADJUSTMENT_LABELS.get(reason, reason.replace("_", " "))


def _health_adjustment_severity(delta: float) -> str:
    if delta <= -60:
        return "danger"
    if delta < 0:
        return "warning"
    if delta > 0:
        return "success"
    return "info"


def _build_dynamic_health_diagnostic(
    *,
    account: TelegramAccount,
    health: dict[str, Any],
    join_metrics: dict[str, Any],
    probe_budget: dict[str, Any],
    warmup_action_multiplier: float,
    daily_limit: int,
    run_limit: int,
    now: datetime,
) -> dict[str, Any]:
    health_score = float(health.get("health_score", 0.0) or 0.0)
    adjustments = [
        {
            "reason": str(item.get("reason") or "unknown"),
            "label": _health_adjustment_label(str(item.get("reason") or "unknown")),
            "delta": round(float(item.get("delta", 0.0) or 0.0), 2),
            "severity": _health_adjustment_severity(float(item.get("delta", 0.0) or 0.0)),
        }
        for item in health.get("adjustments", [])
        if isinstance(item, dict)
    ]
    negative_adjustments = sorted(
        [item for item in adjustments if item["delta"] < 0],
        key=lambda item: item["delta"],
    )[:6]

    reasons: list[dict[str, Any]] = []
    if account.risk_pause_until and account.risk_pause_until > now:
        reasons.append(
            _diagnostic_reason(
                "risk_pause_active",
                "风控暂停压低额度",
                severity="danger",
                detail=f"恢复时间 {_iso_datetime(account.risk_pause_until)}",
            )
        )
    if health_score < 25:
        reasons.append(
            _diagnostic_reason(
                "health_score_below_floor",
                "健康分低于广告额度下限",
                severity="danger",
                detail=f"{round(health_score, 2)} < 25",
            )
        )
    if warmup_action_multiplier <= 0:
        reasons.append(
            _diagnostic_reason("warmup_multiplier_zero", "暖号阶段禁止广告动作", severity="danger")
        )
    if int(probe_budget.get("probe_based_limit", 0) or 0) <= 0:
        reasons.append(
            _diagnostic_reason("probe_budget_zero", "已验证群广告容量为 0", severity="warning")
        )
    if daily_limit <= 0:
        reasons.append(
            _diagnostic_reason("ad_daily_limit_zero", "广告日额度为 0", severity="danger")
        )
    if run_limit <= 0:
        reasons.append(
            _diagnostic_reason("ad_run_limit_zero", "广告单轮额度为 0", severity="danger")
        )
    if not reasons and negative_adjustments:
        first = negative_adjustments[0]
        reasons.append(
            _diagnostic_reason(
                first["reason"],
                first["label"],
                severity=first["severity"],
                detail=f"{first['delta']}",
            )
        )

    primary = next((item for item in reasons if item["severity"] == "danger"), None) or (
        reasons[0] if reasons else _diagnostic_reason("healthy", "动态健康正常", severity="success")
    )
    return {
        "primary_reason": primary["reason"],
        "primary_label": primary["label"],
        "primary_severity": primary["severity"],
        "reasons": reasons,
        "adjustments": adjustments,
        "negative_adjustments": negative_adjustments,
        "health_score": round(health_score, 2),
        "risk_score": round(float(health.get("risk_score", 0.0) or 0.0), 2),
        "warmup_action_multiplier": round(float(warmup_action_multiplier or 0.0), 3),
        "probe_based_daily_limit": int(probe_budget.get("probe_based_limit", 0) or 0),
        "probe_factor": round(float(probe_budget.get("probe_factor", 0.0) or 0.0), 3),
        "writable_rate": round(float(join_metrics.get("writable_rate", 0.0) or 0.0), 3),
        "probe_success_rate_24h": round(
            float(join_metrics.get("probe_success_rate_24h", 0.0) or 0.0), 3
        ),
        "ad_success_rate_24h": round(float(join_metrics.get("ad_success_rate_24h", 0.0) or 0.0), 3),
    }


def _ad_group_wait_until(membership: GroupAccountMembership) -> Optional[datetime]:
    waits = [
        membership.first_ad_allowed_at,
        membership.ad_eligible_after,
        membership.probe_due_at if membership.probe_status == "scheduled" else None,
    ]
    values = [item for item in waits if item is not None]
    return max(values) if values else None


async def _build_ad_delivery_diagnostic(
    db: AsyncSession,
    *,
    account: TelegramAccount,
    op_config: Optional[AccountOperationConfig],
    campaign: Optional[AdCampaign],
    now: datetime,
    daily_limit: int,
    run_limit: int,
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    next_action = "ready"
    next_action_label = "等待下一轮投放"
    next_action_at: Optional[datetime] = None

    if op_config is None:
        reasons.append(
            _diagnostic_reason("operation_config_missing", "缺少运营配置", severity="danger")
        )
    elif not op_config.enabled:
        reasons.append(_diagnostic_reason("operation_disabled", "账号运营关闭", severity="danger"))
    elif not op_config.auto_ads_enabled:
        reasons.append(_diagnostic_reason("auto_ads_disabled", "广告开关关闭"))

    account_status = getattr(account.status, "value", account.status)
    if not account.is_active:
        reasons.append(_diagnostic_reason("account_inactive", "账号未启用", severity="danger"))
    if str(account_status) in {"error", "banned"}:
        reasons.append(
            _diagnostic_reason(
                "account_status_blocked", f"账号状态 {account_status}", severity="danger"
            )
        )
    if account.risk_pause_until and account.risk_pause_until > now:
        reasons.append(
            _diagnostic_reason(
                "risk_pause_active",
                "风控暂停中",
                severity="danger",
                detail=f"恢复时间 {_iso_datetime(account.risk_pause_until)}",
            )
        )
        next_action_at = account.risk_pause_until
    if account.risk_level in {"frozen", "quarantined"}:
        reasons.append(
            _diagnostic_reason(
                "risk_level_blocked", f"风控等级 {account.risk_level}", severity="danger"
            )
        )
    elif account.risk_level in {"limited", "watch"}:
        reasons.append(_diagnostic_reason("risk_level_limited", f"风控等级 {account.risk_level}"))

    if campaign is None:
        reasons.append(
            _diagnostic_reason("campaign_missing", "没有启用的广告计划", severity="danger")
        )
    elif not _campaign_is_active_for_status(campaign, now):
        reasons.append(
            _diagnostic_reason("campaign_inactive", "广告计划未处于活动状态", severity="danger")
        )

    binding_query = select(func.count(AccountAdBinding.id)).where(
        AccountAdBinding.account_id == account.id,
        AccountAdBinding.enabled == True,
    )
    if campaign is not None:
        binding_query = binding_query.where(AccountAdBinding.ad_campaign_id == campaign.id)
    binding_count = (await db.execute(binding_query)).scalar() or 0
    if campaign is not None and binding_count <= 0:
        reasons.append(
            _diagnostic_reason("binding_missing", "账号未绑定当前广告计划素材", severity="danger")
        )

    if daily_limit <= 0:
        reasons.append(
            _diagnostic_reason("dynamic_daily_limit_zero", "动态日额度为 0", severity="danger")
        )
    if run_limit <= 0:
        reasons.append(
            _diagnostic_reason("dynamic_run_limit_zero", "动态单轮额度为 0", severity="danger")
        )

    memberships = (
        (
            await db.execute(
                select(GroupAccountMembership)
                .options(selectinload(GroupAccountMembership.group))
                .where(
                    GroupAccountMembership.account_id == account.id,
                    GroupAccountMembership.status == "joined",
                )
                .order_by(
                    GroupAccountMembership.updated_at.desc(), GroupAccountMembership.id.desc()
                )
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    group_ids = [int(item.group_id) for item in memberships if item.group_id is not None]
    profile_map: dict[int, GroupAdProfile] = {}
    if group_ids:
        profile_rows = await db.execute(
            select(GroupAdProfile).where(GroupAdProfile.group_id.in_(group_ids))
        )
        profile_map = {int(item.group_id): item for item in profile_rows.scalars().all()}

    target_levels = set(campaign.get_target_levels()) if campaign is not None else set()
    target_group_ids = set(campaign.get_target_group_ids()) if campaign is not None else set()
    group_counts = {
        "joined": len(memberships),
        "ready": 0,
        "pending_probe": 0,
        "probe_failed": 0,
        "waiting_first_ad": 0,
        "waiting_ad_eligible": 0,
        "blocked": 0,
        "group_not_active": 0,
        "level_not_targeted": 0,
        "ai_warmed": 0,
        "ad_permission_unknown": 0,
        "ad_policy_probe_pending": 0,
        "ad_approval_required": 0,
        "ad_permission_low_confidence": 0,
        "ad_permission_forbidden": 0,
        "ad_policy_expired": 0,
        "premium": 0,
    }
    group_samples: list[dict[str, Any]] = []
    soonest_wait: Optional[datetime] = None

    for membership in memberships:
        group = membership.group
        profile = profile_map.get(int(membership.group_id))
        group_reason = "ready"
        group_label = "可投放"
        severity = "success"
        wait_until = _ad_group_wait_until(membership)
        ai_warmed = "group_ai_warmup_interaction" in (membership.note or "")
        if ai_warmed:
            group_counts["ai_warmed"] += 1

        if group is None:
            group_reason = "group_missing"
            group_label = "群记录缺失"
            severity = "danger"
            group_counts["blocked"] += 1
        elif group.status != "active":
            group_reason = f"group_status_{group.status}"
            group_label = f"群状态 {group.status}"
            severity = "warning"
            group_counts["group_not_active"] += 1
        elif target_group_ids and group.id not in target_group_ids:
            group_reason = "group_not_targeted"
            group_label = "不在计划指定群中"
            severity = "info"
            group_counts["level_not_targeted"] += 1
        elif not target_group_ids and target_levels and group.level.value not in target_levels:
            group_reason = "level_not_targeted"
            group_label = f"群等级 {group.level.value} 未命中"
            severity = "info"
            group_counts["level_not_targeted"] += 1
        elif (
            membership.warmup_status == "blocked"
            or membership.probe_status == "failed"
            or membership.ad_status == "blocked"
        ):
            group_reason = "probe_or_ad_blocked"
            group_label = "探针或广告状态阻断"
            severity = "danger"
            group_counts["probe_failed"] += 1
        elif membership.probe_status in {"not_started", "scheduled"}:
            group_reason = "probe_pending"
            group_label = "已 AI 暖群，等待探针" if ai_warmed else "等待探针"
            severity = "warning"
            group_counts["pending_probe"] += 1
        elif profile is None or profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN.value:
            group_reason = "ad_permission_unknown"
            group_label = "广告许可未知"
            severity = "warning"
            group_counts["ad_permission_unknown"] += 1
        elif profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN_PROBE.value:
            group_reason = "ad_permission_probe_pending"
            group_label = "广告检测进行中"
            severity = "warning"
            group_counts["ad_policy_probe_pending"] += 1
        elif profile.ad_policy_mode == GroupAdPolicyMode.APPROVAL_REQUIRED.value:
            group_reason = "ad_approval_required"
            group_label = "需要管理员审批"
            severity = "warning"
            group_counts["ad_approval_required"] += 1
        elif profile.ad_policy_mode == GroupAdPolicyMode.FORBIDDEN.value:
            group_reason = "ad_permission_forbidden"
            group_label = "群不允许广告"
            severity = "danger"
            group_counts["ad_permission_forbidden"] += 1
        elif int(profile.ad_policy_confidence or 0) < 90:
            group_reason = "ad_permission_low_confidence"
            group_label = "广告许可置信度不足"
            severity = "warning"
            group_counts["ad_permission_low_confidence"] += 1
        elif profile.ad_policy_expires_at and profile.ad_policy_expires_at <= now:
            group_reason = "ad_policy_expired"
            group_label = "广告许可已过期"
            severity = "warning"
            group_counts["ad_policy_expired"] += 1
        elif profile.paused_until and profile.paused_until > now:
            group_reason = "group_ad_paused"
            group_label = "群广告暂停中"
            severity = "warning"
            group_counts["blocked"] += 1
        elif membership.first_ad_allowed_at and now < membership.first_ad_allowed_at:
            group_reason = "first_ad_warmup_wait"
            group_label = "首次广告暖群等待"
            severity = "warning"
            group_counts["waiting_first_ad"] += 1
        elif membership.ad_eligible_after and now < membership.ad_eligible_after:
            group_reason = "ad_eligible_wait"
            group_label = "探针后广告等待"
            severity = "warning"
            group_counts["waiting_ad_eligible"] += 1
        elif membership.probe_status == "success":
            group_counts["ready"] += 1
            if profile and profile.ad_tier == "premium":
                group_counts["premium"] += 1
        else:
            group_reason = "warmup_unknown"
            group_label = "暖群状态未明确"
            severity = "warning"
            group_counts["blocked"] += 1

        if wait_until and wait_until > now and (soonest_wait is None or wait_until < soonest_wait):
            soonest_wait = wait_until

        if group_reason != "ready" and len(group_samples) < 8:
            group_samples.append(
                {
                    "group_id": group.id if group else membership.group_id,
                    "telegram_group_id": membership.telegram_group_id,
                    "title": group.title if group else None,
                    "level": group.level.value if group else None,
                    "group_status": group.status if group else None,
                    "reason": group_reason,
                    "label": group_label,
                    "severity": severity,
                    "warmup_status": membership.warmup_status,
                    "probe_status": membership.probe_status,
                    "ad_status": membership.ad_status,
                    "probe_due_at": _iso_datetime(membership.probe_due_at),
                    "interaction_started_at": _iso_datetime(membership.interaction_started_at),
                    "interaction_sent_today": int(membership.interaction_sent_today or 0),
                    "first_ad_allowed_at": _iso_datetime(membership.first_ad_allowed_at),
                    "ad_eligible_after": _iso_datetime(membership.ad_eligible_after),
                    "last_probe_error": (membership.last_probe_error or "")[:300],
                    "ad_policy_mode": profile.ad_policy_mode
                    if profile
                    else GroupAdPolicyMode.UNKNOWN.value,
                    "ad_tier": profile.ad_tier if profile else "observing",
                    "ad_daily_capacity": int(profile.daily_capacity or 0) if profile else 0,
                }
            )

    if memberships and group_counts["ready"] <= 0:
        if group_counts["pending_probe"]:
            reasons.append(
                _diagnostic_reason(
                    "groups_pending_probe",
                    "已入群但等待探针",
                    detail=f"{group_counts['pending_probe']} 个群",
                )
            )
            next_action = "send_probe"
            next_action_label = "等待探针发送"
            next_action_at = soonest_wait or next_action_at
        elif group_counts["ad_policy_probe_pending"]:
            reasons.append(
                _diagnostic_reason(
                    "groups_ad_policy_probe_pending",
                    "未知群广告检测进行中",
                    detail=f"{group_counts['ad_policy_probe_pending']} 个群",
                )
            )
            next_action = "wait_ad_policy_probe"
            next_action_label = "等待广告检测结果"
        elif group_counts["ad_permission_unknown"]:
            reasons.append(
                _diagnostic_reason(
                    "groups_ad_permission_unknown",
                    "群广告许可未知，需要先发送检测广告",
                    detail=f"{group_counts['ad_permission_unknown']} 个群",
                )
            )
            next_action = "send_ad_policy_probe"
            next_action_label = "发送广告检测"
        elif (
            group_counts["ad_approval_required"]
            or group_counts["ad_permission_low_confidence"]
            or group_counts["ad_policy_expired"]
        ):
            reasons.append(
                _diagnostic_reason(
                    "groups_ad_permission_review",
                    "群广告许可需要确认",
                    detail=(
                        f"{group_counts['ad_permission_unknown'] + group_counts['ad_approval_required'] + group_counts['ad_permission_low_confidence'] + group_counts['ad_policy_expired']} 个群"
                    ),
                )
            )
            next_action = "review_group_ad_policy"
            next_action_label = "审核群广告许可"
        elif group_counts["ad_permission_forbidden"]:
            reasons.append(
                _diagnostic_reason(
                    "groups_ad_forbidden",
                    "候选群禁止广告",
                    severity="danger",
                    detail=f"{group_counts['ad_permission_forbidden']} 个群",
                )
            )
            next_action = "find_more_groups"
            next_action_label = "继续寻找允许软广告的群"
        elif group_counts["waiting_first_ad"] or group_counts["waiting_ad_eligible"]:
            reasons.append(
                _diagnostic_reason(
                    "groups_waiting_warmup",
                    "群还在暖群等待",
                    detail=f"{group_counts['waiting_first_ad'] + group_counts['waiting_ad_eligible']} 个群",
                )
            )
            next_action = "wait_warmup"
            next_action_label = "等待暖群结束"
            next_action_at = soonest_wait or next_action_at
        elif group_counts["level_not_targeted"]:
            reasons.append(
                _diagnostic_reason(
                    "groups_level_not_targeted",
                    "已有群未命中广告目标等级",
                    detail=f"{group_counts['level_not_targeted']} 个群",
                )
            )
            next_action = "adjust_campaign_levels"
            next_action_label = "调整广告目标等级或提升群评级"
        elif group_counts["probe_failed"] or group_counts["blocked"]:
            reasons.append(
                _diagnostic_reason("groups_blocked", "候选群被探针/权限阻断", severity="danger")
            )
            next_action = "find_more_groups"
            next_action_label = "继续加群或清理阻断群"
    elif not memberships:
        reasons.append(_diagnostic_reason("no_joined_groups", "账号没有已加入群"))
        next_action = "join_groups"
        next_action_label = "先积累可用群"

    probe_blocking_reasons = {
        "operation_config_missing",
        "operation_disabled",
        "auto_ads_disabled",
        "account_inactive",
        "account_status_blocked",
        "risk_pause_active",
        "risk_level_blocked",
        "campaign_missing",
        "campaign_inactive",
        "binding_missing",
    }
    probe_execution_allowed = not any(item["reason"] in probe_blocking_reasons for item in reasons)
    ad_delivery_allowed = (
        probe_execution_allowed and daily_limit > 0 and run_limit > 0 and group_counts["ready"] > 0
    )

    hard_reason = next((item for item in reasons if item["severity"] == "danger"), None)
    primary = hard_reason or (
        reasons[0] if reasons else _diagnostic_reason("ready", "可以尝试投放", severity="success")
    )
    if primary["reason"] in {"dynamic_daily_limit_zero", "dynamic_run_limit_zero"}:
        if probe_execution_allowed and group_counts["pending_probe"] > 0:
            next_action = "send_probe_while_ads_paused"
            next_action_label = "继续发探针，广告等待健康恢复"
        else:
            next_action = "recover_account_health"
            next_action_label = "等待账号健康恢复或调低暖号限制"
    elif primary["reason"] in {"operation_disabled", "auto_ads_disabled"}:
        next_action = "enable_ads"
        next_action_label = "开启账号广告投放"
    elif primary["reason"] == "campaign_missing":
        next_action = "create_campaign"
        next_action_label = "创建并启用广告计划"
    elif primary["reason"] == "binding_missing":
        next_action = "bind_creatives"
        next_action_label = "绑定广告素材"

    return {
        "primary_block_reason": primary["reason"],
        "primary_block_label": primary["label"],
        "primary_block_severity": primary["severity"],
        "block_reasons": reasons,
        "next_action": next_action,
        "next_action_label": next_action_label,
        "next_action_at": _iso_datetime(next_action_at),
        "probe_execution_allowed": probe_execution_allowed,
        "ad_delivery_allowed": ad_delivery_allowed,
        "active_campaign_id": campaign.id if campaign else None,
        "active_campaign_name": campaign.name if campaign else None,
        "enabled_binding_count": int(binding_count),
        "group_diagnostics": group_counts,
        "blocked_group_samples": group_samples,
    }


def _scheduler_config_to_dict(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(config.get("enabled", True)),
        "scan_interval_minutes": int(config.get("scan_interval_minutes", 5)),
        "search_filter": config.get("search_filter", {}),
        "join_verification": config.get("join_verification", {}),
        "group_capacity_cleanup": config.get("group_capacity_cleanup", {}),
    }


@router.get("/auto-join/scheduler-config")
async def get_auto_join_scheduler_config(db: AsyncSession = Depends(get_db)) -> dict:
    config = await get_auto_join_scheduler_settings(db)
    return {"code": 0, "message": "success", "data": _scheduler_config_to_dict(config)}


@router.put("/auto-join/scheduler-config")
async def update_auto_join_scheduler_config(
    request: AutoJoinSchedulerConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_auto_join_scheduler_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": _scheduler_config_to_dict(config)}


# =============================================================================
# Account Risk Guard
# =============================================================================


@router.get("/account-risk-guard")
async def get_account_risk_guard_config(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_account_risk_guard_settings(db)}


@router.put("/account-risk-guard")
async def update_account_risk_guard_config(
    request: AccountRiskGuardUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_account_risk_guard_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": config}


@router.get("/account-asset-policy")
async def get_account_asset_policy_config(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_account_asset_policy_settings(db)}


@router.put("/account-asset-policy")
async def update_account_asset_policy_config(
    request: AccountAssetPolicyUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_account_asset_policy_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": config}


@router.get("/account-warmup-policy")
async def get_account_warmup_policy_config(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_account_warmup_policy_settings(db)}


@router.put("/account-warmup-policy")
async def update_account_warmup_policy_config(
    request: AccountWarmupPolicyUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_account_warmup_policy_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": config}


@router.get("/ads/delivery-throttle")
async def get_ad_delivery_throttle_config(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_ad_delivery_throttle_settings(db)}


@router.put("/ads/delivery-throttle")
async def update_ad_delivery_throttle_config(
    request: AdDeliveryThrottleUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_ad_delivery_throttle_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": config}


@router.get("/ads/delivery-execution")
async def get_ad_delivery_execution_config(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_ad_delivery_execution_settings(db)}


@router.put("/ads/delivery-execution")
async def update_ad_delivery_execution_config(
    request: AdDeliveryExecutionUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_ad_delivery_execution_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": config}


@router.get("/ads/capacity")
async def get_ad_capacity_config(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_ad_capacity_settings(db)}


@router.put("/ads/capacity")
async def update_ad_capacity_config(
    request: AdCapacityUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await save_ad_capacity_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": config}


@router.get("/effective-limits")
async def get_effective_limits(db: AsyncSession = Depends(get_db)) -> dict:
    """Return the normalized hard caps and their runtime reduction factors."""
    summary = build_effective_limit_summary(
        risk_guard=await get_account_risk_guard_settings(db),
        ad_execution=await get_ad_delivery_execution_settings(db),
        ad_throttle=await get_ad_delivery_throttle_settings(db),
        ad_capacity=await get_ad_capacity_settings(db),
    )
    return {"code": 0, "message": "success", "data": summary}


def _group_ad_profile_payload(
    profile: GroupAdProfile, metrics: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    group = profile.group
    return {
        "id": profile.id,
        "group_id": profile.group_id,
        "telegram_group_id": profile.telegram_group_id,
        "group_title": group.title if group else None,
        "group_status": group.status if group else None,
        "group_level": getattr(group.level, "value", group.level) if group else None,
        "ad_policy_mode": profile.ad_policy_mode,
        "ad_policy_confidence": int(profile.ad_policy_confidence or 0),
        "ad_policy_source": profile.ad_policy_source,
        "ad_policy_verified_at": _iso_datetime(profile.ad_policy_verified_at),
        "ad_policy_probe_status": profile.ad_policy_probe_status,
        "ad_policy_probe_at": _iso_datetime(profile.ad_policy_probe_at),
        "ad_policy_probe_account_id": profile.ad_policy_probe_account_id,
        "ad_policy_probe_error": profile.ad_policy_probe_error,
        "ad_policy_expires_at": _iso_datetime(profile.ad_policy_expires_at),
        "ad_tier": profile.ad_tier,
        "daily_capacity": int(profile.daily_capacity or 0),
        "paused_until": _iso_datetime(profile.paused_until),
        "survival_count": int(profile.survival_count or 0),
        "deleted_count": int(profile.deleted_count or 0),
        "last_survived_at": _iso_datetime(profile.last_survived_at),
        "last_deleted_at": _iso_datetime(profile.last_deleted_at),
        "blocked_reason": profile.blocked_reason,
        "metrics": metrics or {},
    }


@router.get("/ads/group-profiles")
async def list_group_ad_profiles(
    policy_mode: Optional[str] = Query(default=None),
    tier: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = (
        select(GroupAdProfile)
        .options(selectinload(GroupAdProfile.group))
        .order_by(GroupAdProfile.updated_at.desc())
    )
    if policy_mode:
        query = query.where(GroupAdProfile.ad_policy_mode == policy_mode)
    if tier:
        query = query.where(GroupAdProfile.ad_tier == tier)
    profiles = list((await db.execute(query.limit(limit))).scalars().all())
    service = AcquisitionAutomationService(db)
    capacity = await get_ad_capacity_settings(db)
    now = datetime.utcnow()
    data = []
    for profile in profiles:
        metrics = (
            await service._refresh_group_ad_profile_tier(profile, profile.group, now, capacity)
            if profile.group
            else {}
        )
        data.append(_group_ad_profile_payload(profile, metrics))
    return {"code": 0, "message": "success", "data": data}


@router.get("/ads/group-profiles/{group_id}/policy-events")
async def list_group_ad_policy_events(
    group_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await db.execute(
        select(GroupAdPolicyEvent)
        .where(GroupAdPolicyEvent.group_id == group_id)
        .order_by(GroupAdPolicyEvent.created_at.desc())
        .limit(limit)
    )
    data = [
        {
            "id": event.id,
            "group_id": event.group_id,
            "account_id": event.account_id,
            "telegram_group_id": event.telegram_group_id,
            "previous_mode": event.previous_mode,
            "new_mode": event.new_mode,
            "confidence": event.confidence,
            "source": event.source,
            "reason": event.reason,
            "evidence": event.evidence,
            "changed_by_user_id": event.changed_by_user_id,
            "created_at": _iso_datetime(event.created_at),
        }
        for event in rows.scalars().all()
    ]
    return {"code": 0, "message": "success", "data": data}


@router.post("/ads/group-profiles/{group_id}/probe")
async def trigger_group_ad_policy_probe(
    group_id: int,
    request: GroupAdPolicyProbeRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = AcquisitionAutomationService(db)
    try:
        result = await service.send_group_ad_policy_probe(
            group_id,
            account_id=request.account_id,
            changed_by_user_id=current_user.get("id"),
        )
    except ValueError as exc:
        if str(exc) == "group_not_found":
            raise HTTPException(status_code=404, detail="Group not found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        detail = str(exc)
        conflict_codes = {
            "group_ad_policy_probe_already_pending",
            "group_ad_policy_probe_cooldown",
            "group_ad_policy_already_resolved",
        }
        known_precondition_codes = {
            "group_ad_forbidden",
            "group_ad_approval_required",
            "no_probe_ready_membership",
            "no_active_ad_binding_for_probe",
            "account_ads_disabled",
            "group_level_disallows_ads",
        }
        if detail in conflict_codes:
            code = 409
        elif detail in known_precondition_codes or detail.startswith("group_status_"):
            code = 400
        else:
            code = 503
        raise HTTPException(status_code=code, detail=detail) from exc
    return {"code": 0, "message": "success", "data": result}


@router.put("/ads/group-profiles/{group_id}/policy")
async def update_group_ad_policy(
    group_id: int,
    request: GroupAdPolicyUpdate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    service = AcquisitionAutomationService(db)
    capacity = await get_ad_capacity_settings(db)
    profile = await service._get_or_create_group_ad_profile(group, capacity)
    now = datetime.utcnow()
    mode = GroupAdPolicyMode(request.mode).value
    required_confidence = 80 if mode == GroupAdPolicyMode.SOFT_AD_TRIAL.value else 90
    if (
        mode
        in {
            GroupAdPolicyMode.SOFT_AD_TRIAL.value,
            GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
            GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
        }
        and request.confidence < required_confidence
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Selected ad policy requires confidence >= {required_confidence}",
        )
    previous_mode = str(profile.ad_policy_mode or GroupAdPolicyMode.UNKNOWN.value)
    profile.ad_policy_mode = mode
    profile.ad_policy_confidence = request.confidence
    profile.ad_policy_source = "manual"
    profile.ad_policy_verified_at = now
    profile.ad_policy_probe_status = "not_started"
    profile.ad_policy_probe_at = None
    profile.ad_policy_probe_account_id = None
    profile.ad_policy_probe_error = None
    if mode == GroupAdPolicyMode.FORBIDDEN.value:
        group.status = GROUP_STATUS_AD_BLOCKED
        profile.ad_policy_expires_at = None
        profile.ad_tier = "blocked"
        profile.daily_capacity = 0
        profile.blocked_at = now
        profile.blocked_reason = "manual_ad_policy_forbidden"
    elif mode in {
        GroupAdPolicyMode.UNKNOWN.value,
        GroupAdPolicyMode.UNKNOWN_PROBE.value,
        GroupAdPolicyMode.APPROVAL_REQUIRED.value,
    }:
        if group.status == GROUP_STATUS_AD_BLOCKED:
            group.status = "active"
        profile.ad_policy_expires_at = None
        profile.ad_tier = "observing"
        profile.daily_capacity = 0
        profile.blocked_at = None
        profile.blocked_reason = None
    else:
        if group.status == GROUP_STATUS_AD_BLOCKED:
            group.status = "active"
        expires_days = request.expires_days or int(capacity.get("ad_policy_manual_ttl_days") or 30)
        profile.ad_policy_expires_at = now + timedelta(days=expires_days)
        if profile.ad_tier in {"blocked", "observing", "low"}:
            profile.ad_tier = "trial"
        profile.blocked_at = None
        profile.blocked_reason = None
    profile.tier_changed_at = now
    profile.updated_at = now
    db.add(
        GroupAdPolicyEvent(
            group_id=group.id,
            telegram_group_id=group.group_id,
            previous_mode=previous_mode,
            new_mode=mode,
            confidence=request.confidence,
            source="manual",
            reason=request.note or "manual_policy_update",
            changed_by_user_id=current_user.get("id"),
        )
    )
    await db.commit()
    metrics = await service._refresh_group_ad_profile_tier(profile, group, now, capacity)
    return {"code": 0, "message": "success", "data": _group_ad_profile_payload(profile, metrics)}


@router.get("/ads/dynamic-status")
async def get_ad_dynamic_status(db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.utcnow()
    service = AcquisitionAutomationService(db)
    execution = await get_ad_delivery_execution_settings(db)
    warmup_policy = await get_account_warmup_policy_settings(db)
    campaign = (
        await db.execute(
            select(AdCampaign)
            .where(AdCampaign.enabled == True)
            .order_by(AdCampaign.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    accounts = (
        (
            await db.execute(
                select(TelegramAccount)
                .where(TelegramAccount.account_type == AccountType.PROMOTER)
                .order_by(TelegramAccount.id.asc())
            )
        )
        .scalars()
        .all()
    )

    data: list[dict[str, Any]] = []
    for account in accounts:
        op_config = (
            await db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == account.id
                )
            )
        ).scalar_one_or_none()
        health = await service._ad_dynamic_account_health(account.id, now)
        tier = service._ad_health_tier(float(health["health_score"]))
        daily_limit = (
            await service._ad_dynamic_daily_limit(account.id, op_config, campaign, now)
            if campaign is not None
            else 0
        )
        probe_budget = await service._ad_probe_budget_metrics(account.id, now, op_config=op_config)
        join_metrics = await service._account_join_quality_metrics(account.id, now)
        join_daily_limit = (
            await service._auto_join_dynamic_daily_limit(op_config, now)
            if op_config is not None
            else 0
        )
        business_stage = getattr(op_config, "business_stage", "new") if op_config else "new"
        warmup_context = account_warmup_context(warmup_policy, account, now, action="ad_delivery")
        run_limit = await service._ad_dynamic_run_limit(
            account.id,
            AD_MAX_DELIVERIES_PER_ACCOUNT_PER_RUN,
            now,
        )

        warmup_rows = await db.execute(
            select(
                GroupAccountMembership.warmup_status,
                GroupAccountMembership.probe_status,
                func.count(GroupAccountMembership.id),
            )
            .where(GroupAccountMembership.account_id == account.id)
            .group_by(GroupAccountMembership.warmup_status, GroupAccountMembership.probe_status)
        )
        warmup_summary = [
            {"warmup_status": row[0], "probe_status": row[1], "count": int(row[2] or 0)}
            for row in warmup_rows.all()
        ]

        recent_probe_success = (
            await db.execute(
                select(func.count(GroupAccountMembership.id)).where(
                    GroupAccountMembership.account_id == account.id,
                    GroupAccountMembership.probe_status == "success",
                    GroupAccountMembership.last_probe_at >= now - timedelta(hours=6),
                )
            )
        ).scalar() or 0
        ad_eligible_groups = (
            await db.execute(
                select(func.count(GroupAccountMembership.id)).where(
                    GroupAccountMembership.account_id == account.id,
                    GroupAccountMembership.status == "joined",
                    GroupAccountMembership.probe_status == "success",
                    GroupAccountMembership.ad_eligible_after.isnot(None),
                    GroupAccountMembership.ad_eligible_after <= now,
                )
            )
        ).scalar() or 0
        pending_probe_groups = (
            await db.execute(
                select(func.count(GroupAccountMembership.id)).where(
                    GroupAccountMembership.account_id == account.id,
                    GroupAccountMembership.status == "joined",
                    GroupAccountMembership.probe_status.in_(["not_started", "scheduled"]),
                )
            )
        ).scalar() or 0

        recent_errors_rows = await db.execute(
            select(AdDeliveryLog.error, func.count(AdDeliveryLog.id))
            .where(
                AdDeliveryLog.account_id == account.id,
                AdDeliveryLog.status == "failed",
                AdDeliveryLog.created_at >= now - timedelta(hours=24),
            )
            .group_by(AdDeliveryLog.error)
            .order_by(desc(func.count(AdDeliveryLog.id)))
            .limit(5)
        )
        recent_errors = [
            {"error": (row[0] or "")[:300], "count": int(row[1] or 0)}
            for row in recent_errors_rows.all()
        ]
        delivery_diagnostic = await _build_ad_delivery_diagnostic(
            db,
            account=account,
            op_config=op_config,
            campaign=campaign,
            now=now,
            daily_limit=int(daily_limit or 0),
            run_limit=int(run_limit or 0),
        )
        dynamic_health_diagnostic = _build_dynamic_health_diagnostic(
            account=account,
            health=health,
            join_metrics=join_metrics,
            probe_budget=probe_budget,
            warmup_action_multiplier=float(warmup_context.action_multiplier),
            daily_limit=int(daily_limit or 0),
            run_limit=int(run_limit or 0),
            now=now,
        )

        data.append(
            {
                "account_id": account.id,
                "account_label": account.display_name
                or account.identifier
                or account.phone
                or account.session_name,
                "account_status": getattr(account.status, "value", account.status),
                "risk_level": account.risk_level,
                "risk_score": account.risk_score,
                "risk_reason": account.risk_reason,
                "risk_pause_until": account.risk_pause_until.isoformat()
                if account.risk_pause_until
                else None,
                "auto_join_enabled": bool(op_config.auto_join_enabled) if op_config else False,
                "auto_ads_enabled": bool(op_config.auto_ads_enabled) if op_config else False,
                "business_stage": business_stage,
                "warmup_stage": warmup_context.stage,
                "managed_started_at": warmup_context.managed_started_at.isoformat()
                if warmup_context.managed_started_at
                else None,
                "managed_age_days": warmup_context.managed_age_days,
                "warmup_remaining_days": warmup_context.remaining_days,
                "warmup_action_multiplier": round(float(warmup_context.action_multiplier), 3),
                "health_score": round(float(health["health_score"]), 2),
                "tier": tier,
                "success_24h": health["success"],
                "failed_24h": health["failed"],
                "success_rate_24h": round(float(health["success_rate"]), 3),
                "group_control_failed_24h": health["group_control_failed"],
                "account_failed_24h": health["account_failed"],
                "transient_failed_24h": health["transient_failed"],
                "dynamic_daily_limit": daily_limit,
                "dynamic_run_limit": run_limit,
                "time_window_multiplier": service._ad_time_window_multiplier(now),
                "probe_based_daily_limit": int(probe_budget["probe_based_limit"]),
                "probe_factor": round(float(probe_budget["probe_factor"]), 3),
                "probe_quality_multiplier": round(float(probe_budget["quality_multiplier"]), 3),
                "recent_probe_success_6h": int(recent_probe_success),
                "recent_probe_failed_6h": int(probe_budget["recent_probe_failed"]),
                "recent_probe_success_rate_6h": round(
                    float(probe_budget["recent_probe_success_rate"]), 3
                ),
                "ad_eligible_groups": int(ad_eligible_groups),
                "pending_probe_groups": int(pending_probe_groups),
                "join_dynamic_daily_limit": int(join_daily_limit),
                "join_time_window_multiplier": service._join_time_window_multiplier(now),
                "writable_rate": round(float(join_metrics["writable_rate"]), 3),
                "probe_success_rate_24h": round(float(join_metrics["probe_success_rate_24h"]), 3),
                "ad_success_rate_24h": round(float(join_metrics["ad_success_rate_24h"]), 3),
                "average_group_quality_score": round(
                    float(join_metrics["average_group_quality_score"]), 2
                ),
                "warmup_summary": warmup_summary,
                "recent_errors": recent_errors,
                "delivery_diagnostic": delivery_diagnostic,
                "dynamic_health_diagnostic": dynamic_health_diagnostic,
            }
        )

    return {"code": 0, "message": "success", "data": data}


# =============================================================================
# Advertisement Failure Policy
# =============================================================================


@router.get("/ads/failure-policy")
async def get_ad_failure_policy(db: AsyncSession = Depends(get_db)) -> dict:
    return {"code": 0, "message": "success", "data": await get_ad_failure_policy_settings(db)}


@router.put("/ads/failure-policy")
async def update_ad_failure_policy(
    request: AdFailurePolicyUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    policy = await save_ad_failure_policy_settings(db, request.model_dump())
    return {"code": 0, "message": "success", "data": policy}


# =============================================================================
# Account Operation Config
# =============================================================================


class AccountOperationConfigUpdate(BaseModel):
    operation_mode: Optional[str] = Field(None, pattern="^(growth|ad_only)$")
    auto_join_enabled: Optional[bool] = None
    auto_ads_enabled: Optional[bool] = None
    max_groups_per_day: Optional[int] = Field(None, ge=0, le=1000)
    max_groups_total: Optional[int] = Field(None, ge=0, le=10000)
    join_interval_min_seconds: Optional[int] = Field(None, ge=60)
    join_interval_max_seconds: Optional[int] = Field(None, ge=60)
    next_join_after: Optional[datetime] = None
    max_messages_per_day: Optional[int] = Field(None, ge=0, le=20000)
    message_interval_seconds: Optional[int] = Field(None, ge=1)
    quiet_hours_start: Optional[str] = Field(None, max_length=5)
    quiet_hours_end: Optional[str] = Field(None, max_length=5)
    keyword_types: Optional[list[str]] = None
    keyword_auto_replenish_enabled: Optional[bool] = None
    keyword_replenish_requires_review: Optional[bool] = None
    enabled: Optional[bool] = None


class AccountOperationConfigBatchUpdate(BaseModel):
    account_ids: list[int] = Field(default_factory=list, min_length=1, max_length=500)
    config: AccountOperationConfigUpdate


def _operation_config_to_dict(config: AccountOperationConfig) -> dict:
    keyword_types = []
    if config.keyword_types:
        try:
            keyword_types = json.loads(config.keyword_types)
        except (json.JSONDecodeError, TypeError):
            keyword_types = []
    return {
        "id": config.id,
        "account_id": config.account_id,
        "operation_mode": getattr(config, "operation_mode", None) or AccountOperationMode.GROWTH.value,
        "auto_join_enabled": config.auto_join_enabled,
        "auto_ads_enabled": config.auto_ads_enabled,
        "max_groups_per_day": config.max_groups_per_day,
        "max_groups_total": config.max_groups_total,
        "join_interval_min_seconds": config.join_interval_min_seconds,
        "join_interval_max_seconds": config.join_interval_max_seconds,
        "next_join_after": config.next_join_after.isoformat() if config.next_join_after else None,
        "max_messages_per_day": config.max_messages_per_day,
        "message_interval_seconds": config.message_interval_seconds,
        "quiet_hours_start": config.quiet_hours_start,
        "quiet_hours_end": config.quiet_hours_end,
        "keyword_types": keyword_types,
        "keyword_auto_replenish_enabled": config.keyword_auto_replenish_enabled,
        "keyword_replenish_requires_review": config.keyword_replenish_requires_review,
        "risk_level": config.risk_level,
        "business_stage": config.business_stage,
        "enabled": config.enabled,
        "created_at": config.created_at.isoformat() if config.created_at else "",
        "updated_at": config.updated_at.isoformat() if config.updated_at else "",
    }


async def _get_or_create_operation_config(
    db: AsyncSession, account_id: int
) -> AccountOperationConfig:
    account_result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == account_id)
    )
    account = account_result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.account_type != AccountType.PROMOTER:
        raise HTTPException(
            status_code=400, detail="Only promoter accounts support growth automation config"
        )

    result = await db.execute(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id)
    )
    config = result.scalar_one_or_none()
    if config:
        return config

    config = AccountOperationConfig(account_id=account_id)
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


def _prepare_operation_config_update(
    config: AccountOperationConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    data = dict(payload)
    if "keyword_types" in data:
        data["keyword_types"] = json.dumps(data["keyword_types"], ensure_ascii=False)

    min_interval = data.get("join_interval_min_seconds", config.join_interval_min_seconds)
    max_interval = data.get("join_interval_max_seconds", config.join_interval_max_seconds)
    if max_interval < min_interval:
        raise HTTPException(status_code=400, detail="join_interval_max_seconds must be >= min")

    operation_mode = data.get(
        "operation_mode",
        getattr(config, "operation_mode", None) or AccountOperationMode.GROWTH.value,
    )
    if operation_mode == AccountOperationMode.AD_ONLY.value:
        data["auto_join_enabled"] = False
        data["keyword_auto_replenish_enabled"] = False
    return data


def _apply_operation_config_update(config: AccountOperationConfig, data: dict[str, Any]) -> None:
    for field, value in data.items():
        setattr(config, field, value)


@router.get("/accounts/{account_id:int}/operation-config")
async def get_account_operation_config(account_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    config = await _get_or_create_operation_config(db, account_id)
    return {"code": 0, "message": "success", "data": _operation_config_to_dict(config)}


@router.put("/accounts/{account_id:int}/operation-config")
async def update_account_operation_config(
    account_id: int,
    request: AccountOperationConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await _get_or_create_operation_config(db, account_id)
    data = _prepare_operation_config_update(config, request.model_dump(exclude_none=True))
    _apply_operation_config_update(config, data)

    await db.commit()
    await db.refresh(config)
    return {"code": 0, "message": "success", "data": _operation_config_to_dict(config)}


@router.put("/accounts/operation-config/batch")
async def update_account_operation_configs_batch(
    request: AccountOperationConfigBatchUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    account_ids = list(dict.fromkeys(request.account_ids))
    data = request.config.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No config fields provided")

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for account_id in account_ids:
        try:
            config = await _get_or_create_operation_config(db, account_id)
            prepared = _prepare_operation_config_update(config, data)
            _apply_operation_config_update(config, prepared)
            updated.append(_operation_config_to_dict(config))
        except HTTPException as exc:
            skipped.append({"account_id": account_id, "reason": exc.detail})

    await db.commit()
    return {
        "code": 0,
        "message": "success",
        "data": {
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "updated": updated,
            "skipped": skipped,
        },
    }


# =============================================================================
# Manual Automation Runs
# =============================================================================


class KeywordReplenishRequest(BaseModel):
    min_per_type: Optional[dict[str, int]] = None
    generate_counts: Optional[dict[str, int]] = None
    auto_approve: bool = False


def _queued_automation_result(
    task_name: str, async_result: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "queued": True,
        "status": "queued",
        "task_name": task_name,
        "task_id": async_result.id,
        "payload": payload,
        "processed": 0,
        "created": 0,
        "updated": 0,
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "details": [
            {
                "action": "task_queued",
                "task_name": task_name,
                "task_id": async_result.id,
            }
        ],
    }


def _enqueue_automation_task(task: Any, task_name: str, **kwargs: Any) -> dict[str, Any]:
    try:
        async_result = task.apply_async(kwargs=kwargs, queue="automation")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Automation queue unavailable: {exc}") from exc
    return _queued_automation_result(task_name, async_result, kwargs)


@router.post("/keywords/replenish", status_code=status.HTTP_202_ACCEPTED)
async def run_keyword_replenishment(
    request: KeywordReplenishRequest,
) -> dict:
    result = _enqueue_automation_task(
        replenish_keywords_task,
        "replenish_keywords_task",
        min_per_type=request.min_per_type,
        generate_counts=request.generate_counts,
        auto_approve=request.auto_approve,
    )
    return {"code": 0, "message": "success", "data": result}


class AutoJoinRunRequest(BaseModel):
    max_accounts: int = Field(default=10, ge=1, le=100)
    keywords_per_account: int = Field(default=10, ge=1, le=50)
    max_groups_per_keyword: int = Field(default=20, ge=1, le=50)
    dry_run: bool = False


@router.post("/auto-join/run", status_code=status.HTTP_202_ACCEPTED)
async def run_auto_join(request: AutoJoinRunRequest) -> dict:
    result = _enqueue_automation_task(
        auto_join_groups_task,
        "auto_join_groups_task",
        max_accounts=request.max_accounts,
        keywords_per_account=request.keywords_per_account,
        max_groups_per_keyword=request.max_groups_per_keyword,
        dry_run=request.dry_run,
    )
    return {"code": 0, "message": "success", "data": result}


class GroupFailoverRunRequest(BaseModel):
    max_tasks: int = Field(default=20, ge=1, le=100)
    dry_run: bool = False
    target_account_ids: list[int] = Field(default_factory=list, max_length=100)


@router.post("/auto-join/failover/run", status_code=status.HTTP_202_ACCEPTED)
async def run_group_failover(request: GroupFailoverRunRequest) -> dict:
    result = _enqueue_automation_task(
        recover_orphaned_groups_task,
        "recover_orphaned_groups_task",
        max_tasks=request.max_tasks,
        target_account_ids=list(dict.fromkeys(request.target_account_ids)),
        dry_run=request.dry_run,
    )
    return {"code": 0, "message": "success", "data": result}


class AdDeliveryRunRequest(BaseModel):
    max_deliveries: int = Field(default=300, ge=1, le=10000)
    dry_run: bool = False


class AdPolicyAutoProbeRunRequest(BaseModel):
    limit: Optional[int] = Field(default=None, ge=1, le=20)
    dry_run: bool = False


@router.post("/ads/run", status_code=status.HTTP_202_ACCEPTED)
async def run_ad_delivery(request: AdDeliveryRunRequest) -> dict:
    result = _enqueue_automation_task(
        deliver_ads_task,
        "deliver_ads_task",
        max_deliveries=request.max_deliveries,
        dry_run=request.dry_run,
    )
    return {"code": 0, "message": "success", "data": result}


@router.post("/ads/auto-policy-probe/run", status_code=status.HTTP_202_ACCEPTED)
async def run_auto_group_ad_policy_probe(request: AdPolicyAutoProbeRunRequest) -> dict:
    result = _enqueue_automation_task(
        auto_probe_unknown_group_ad_policies_task,
        "auto_probe_unknown_group_ad_policies_task",
        limit=request.limit,
        dry_run=request.dry_run,
    )
    return {"code": 0, "message": "success", "data": result}


class AdSurvivalCheckRunRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


@router.post("/ads/survival-check/run", status_code=status.HTTP_202_ACCEPTED)
async def run_ad_survival_check(request: AdSurvivalCheckRunRequest) -> dict:
    result = _enqueue_automation_task(
        check_ad_survival_task,
        "check_ad_survival_task",
        limit=request.limit,
    )
    return {"code": 0, "message": "success", "data": result}


# =============================================================================
# Auto-join Logs
# =============================================================================


class GroupFailoverRetryRequest(BaseModel):
    target_account_id: Optional[int] = None


def _group_failover_task_to_dict(item: GroupFailoverTask) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_membership_id": item.source_membership_id,
        "source_account_id": item.source_account_id,
        "source_account_label": (
            item.source_account.display_name or item.source_account.identifier
            if item.source_account
            else None
        ),
        "target_account_id": item.target_account_id,
        "target_account_label": (
            item.target_account.display_name or item.target_account.identifier
            if item.target_account
            else None
        ),
        "group_id": item.group_id,
        "telegram_group_id": item.telegram_group_id,
        "group_title": item.group.title if item.group else None,
        "group_username": item.group.username if item.group else None,
        "status": item.status,
        "reason": item.reason,
        "error": item.error,
        "attempt_count": item.attempt_count,
        "next_retry_at": item.next_retry_at.isoformat() if item.next_retry_at else None,
        "last_attempt_at": item.last_attempt_at.isoformat() if item.last_attempt_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


@router.get("/auto-join/failover/tasks")
async def list_group_failover_tasks(
    status_filter: Optional[str] = Query(None, alias="status"),
    source_account_id: Optional[int] = None,
    target_account_id: Optional[int] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(GroupFailoverTask).options(
        selectinload(GroupFailoverTask.source_account),
        selectinload(GroupFailoverTask.target_account),
        selectinload(GroupFailoverTask.group),
    )
    count_query = select(func.count(GroupFailoverTask.id))
    filters = []
    if status_filter:
        filters.append(GroupFailoverTask.status == status_filter)
    if source_account_id:
        filters.append(GroupFailoverTask.source_account_id == source_account_id)
    if target_account_id:
        filters.append(GroupFailoverTask.target_account_id == target_account_id)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)
    total = int((await db.execute(count_query)).scalar() or 0)
    rows = await db.execute(
        query.order_by(GroupFailoverTask.updated_at.desc(), GroupFailoverTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    summary_rows = await db.execute(
        select(GroupFailoverTask.status, func.count(GroupFailoverTask.id)).group_by(
            GroupFailoverTask.status
        )
    )
    summary = {status_value: int(count) for status_value, count in summary_rows.all()}
    return {
        "code": 0,
        "message": "success",
        "data": [_group_failover_task_to_dict(item) for item in rows.scalars().all()],
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": summary,
    }


@router.post("/auto-join/failover/tasks/{task_id:int}/retry")
async def retry_group_failover_task(
    task_id: int,
    request: GroupFailoverRetryRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    task = await db.get(GroupFailoverTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Group failover task not found")
    if task.status == GroupFailoverStatus.SUCCEEDED.value:
        raise HTTPException(status_code=409, detail="Succeeded task cannot be retried")
    if request.target_account_id is not None:
        account = await db.get(TelegramAccount, request.target_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Target account not found")
        task.target_account_id = account.id
    task.status = GroupFailoverStatus.RETRY.value
    task.reason = "manual_retry"
    task.error = None
    task.next_retry_at = datetime.utcnow()
    task.completed_at = None
    task.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "message": "success", "data": _group_failover_task_to_dict(task)}


@router.post("/auto-join/failover/tasks/{task_id:int}/cancel")
async def cancel_group_failover_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    task = await db.get(GroupFailoverTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Group failover task not found")
    if task.status == GroupFailoverStatus.SUCCEEDED.value:
        raise HTTPException(status_code=409, detail="Succeeded task cannot be cancelled")
    task.status = GroupFailoverStatus.CANCELLED.value
    task.reason = "manual_cancelled"
    task.next_retry_at = None
    task.completed_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return {"code": 0, "message": "success", "data": _group_failover_task_to_dict(task)}


@router.get("/auto-join/attempts")
async def list_auto_join_attempts(
    account_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AutoJoinAttempt)
    if account_id:
        query = query.where(AutoJoinAttempt.account_id == account_id)
    if status_filter:
        query = query.where(AutoJoinAttempt.status == status_filter)
    query = query.order_by(AutoJoinAttempt.attempted_at.desc()).limit(limit)
    rows = await db.execute(query)
    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": item.id,
                "account_id": item.account_id,
                "group_id": item.group_id,
                "telegram_group_id": item.telegram_group_id,
                "group_username": item.group_username,
                "group_title": item.group_title,
                "source_keyword": item.source_keyword,
                "status": item.status,
                "reason": item.reason,
                "error": item.error,
                "attempted_at": item.attempted_at.isoformat() if item.attempted_at else "",
                "joined_at": item.joined_at.isoformat() if item.joined_at else None,
            }
            for item in rows.scalars().all()
        ],
    }


@router.get("/auto-join/verification-logs")
async def list_auto_join_verification_logs(
    account_id: Optional[int] = None,
    source: Optional[str] = Query(None, pattern="^(ai|local|fallback|unknown)$"),
    success: Optional[bool] = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = (
        select(GroupAccountMembership, Group)
        .join(Group, Group.id == GroupAccountMembership.group_id)
        .where(GroupAccountMembership.note.like("%verification%"))
        .order_by(desc(GroupAccountMembership.updated_at))
        .limit(min(limit * 4, 500))
    )
    if account_id:
        query = query.where(GroupAccountMembership.account_id == account_id)

    rows = await db.execute(query)
    items: list[dict[str, Any]] = []
    for membership, group in rows.all():
        item = _auto_join_verification_log_from_membership(membership, group)
        if item is None:
            continue
        if source and item.get("source") != source:
            continue
        if success is not None and item.get("success") is not success:
            continue
        items.append(item)
        if len(items) >= limit:
            break

    return {"code": 0, "message": "success", "data": items}


# =============================================================================
# Advertisement CRUD
# =============================================================================


class AdCreativeCreate(BaseModel):
    name: str = Field(..., max_length=120)
    content: str = Field(..., min_length=1)
    creative_type: str = Field(default=AdCreativeType.TEXT.value)
    media_url: Optional[str] = None
    link_url: Optional[str] = None
    weight: int = Field(default=100, ge=0)
    enabled: bool = True


class AdCreativeUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    content: Optional[str] = Field(None, min_length=1)
    creative_type: Optional[str] = None
    media_url: Optional[str] = None
    link_url: Optional[str] = None
    weight: Optional[int] = Field(None, ge=0)
    enabled: Optional[bool] = None


def _validate_ad_creative_content(content: Optional[str]) -> None:
    if content is None:
        return
    text = content.strip()
    if HTML_RESPONSE_RE.search(text) or WEB_ERROR_RESPONSE_RE.search(text):
        raise HTTPException(
            status_code=400, detail="Creative content looks like an HTML or web error response"
        )


def _creative_to_dict(item: AdCreative) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "content": item.content,
        "creative_type": item.creative_type,
        "media_url": item.media_url,
        "link_url": item.link_url,
        "weight": item.weight,
        "enabled": item.enabled,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


@router.get("/ads/creatives")
async def list_ad_creatives(
    enabled: Optional[bool] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AdCreative)
    count_query = select(func.count(AdCreative.id))
    if enabled is not None:
        query = query.where(AdCreative.enabled == enabled)
        count_query = count_query.where(AdCreative.enabled == enabled)
    total = (await db.execute(count_query)).scalar() or 0
    rows = await db.execute(
        query.order_by(AdCreative.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "code": 0,
        "message": "success",
        "data": [_creative_to_dict(i) for i in rows.scalars().all()],
        "total": total,
    }


@router.post("/ads/creatives", status_code=status.HTTP_201_CREATED)
async def create_ad_creative(request: AdCreativeCreate, db: AsyncSession = Depends(get_db)) -> dict:
    if request.creative_type not in {item.value for item in AdCreativeType}:
        raise HTTPException(status_code=400, detail="Invalid creative_type")
    _validate_ad_creative_content(request.content)
    creative = AdCreative(**request.model_dump())
    db.add(creative)
    await db.commit()
    await db.refresh(creative)
    return {"code": 0, "message": "success", "data": _creative_to_dict(creative)}


@router.post("/ads/creatives/cleanup-invalid")
async def cleanup_invalid_ad_creatives(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.execute(select(AdCreative).where(AdCreative.enabled == True))
    disabled: list[int] = []
    for creative in rows.scalars().all():
        content = creative.content or ""
        if HTML_RESPONSE_RE.search(content) or WEB_ERROR_RESPONSE_RE.search(content):
            creative.enabled = False
            disabled.append(creative.id)
    if disabled:
        await db.commit()
    return {
        "code": 0,
        "message": "success",
        "data": {
            "disabled_count": len(disabled),
            "creative_ids": disabled,
        },
    }


@router.put("/ads/creatives/{creative_id:int}")
async def update_ad_creative(
    creative_id: int,
    request: AdCreativeUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    creative = (
        await db.execute(select(AdCreative).where(AdCreative.id == creative_id))
    ).scalar_one_or_none()
    if not creative:
        raise HTTPException(status_code=404, detail="Creative not found")
    data = request.model_dump(exclude_none=True)
    if "creative_type" in data and data["creative_type"] not in {
        item.value for item in AdCreativeType
    }:
        raise HTTPException(status_code=400, detail="Invalid creative_type")
    _validate_ad_creative_content(data.get("content"))
    for field, value in data.items():
        setattr(creative, field, value)
    await db.commit()
    await db.refresh(creative)
    return {"code": 0, "message": "success", "data": _creative_to_dict(creative)}


@router.delete("/ads/creatives/{creative_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ad_creative(creative_id: int, db: AsyncSession = Depends(get_db)) -> None:
    creative = (
        await db.execute(select(AdCreative).where(AdCreative.id == creative_id))
    ).scalar_one_or_none()
    if not creative:
        raise HTTPException(status_code=404, detail="Creative not found")
    await db.delete(creative)
    await db.commit()


class AdCampaignCreate(BaseModel):
    name: str = Field(..., max_length=120)
    enabled: bool = False
    status: str = Field(default="draft", max_length=30)
    send_mode: str = Field(default=AdSendMode.AFTER_JOIN.value)
    target_group_levels: list[str] = Field(default_factory=lambda: ["A"])
    target_group_ids: list[int] = Field(default_factory=list)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    min_wait_after_join_minutes: int = Field(default=60, ge=0)
    interval_minutes: int = Field(default=1440, ge=1)
    scheduled_times: Optional[list[str]] = None
    max_sends_per_group_per_day: int = Field(default=1, ge=0)
    max_sends_per_account_per_day: int = Field(default=3, ge=0)


class AdCampaignUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    enabled: Optional[bool] = None
    status: Optional[str] = Field(None, max_length=30)
    send_mode: Optional[str] = None
    target_group_levels: Optional[list[str]] = None
    target_group_ids: Optional[list[int]] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    min_wait_after_join_minutes: Optional[int] = Field(None, ge=0)
    interval_minutes: Optional[int] = Field(None, ge=1)
    scheduled_times: Optional[list[str]] = None
    max_sends_per_group_per_day: Optional[int] = Field(None, ge=0)
    max_sends_per_account_per_day: Optional[int] = Field(None, ge=0)


def _campaign_to_dict(item: AdCampaign) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "enabled": item.enabled,
        "status": item.status,
        "send_mode": item.send_mode,
        "target_group_levels": item.get_target_levels(),
        "target_group_ids": item.get_target_group_ids(),
        "start_at": item.start_at.isoformat() if item.start_at else None,
        "end_at": item.end_at.isoformat() if item.end_at else None,
        "min_wait_after_join_minutes": item.min_wait_after_join_minutes,
        "interval_minutes": item.interval_minutes,
        "scheduled_times": item.get_scheduled_times(),
        "max_sends_per_group_per_day": item.max_sends_per_group_per_day,
        "max_sends_per_account_per_day": item.max_sends_per_account_per_day,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


def _normalize_scheduled_times(values: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    for raw_value in values or []:
        value = str(raw_value).strip()
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise HTTPException(status_code=400, detail=f"Invalid scheduled time: {value}")
        hour, minute = (int(part) for part in parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise HTTPException(status_code=400, detail=f"Invalid scheduled time: {value}")
        canonical = f"{hour:02d}:{minute:02d}"
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _normalize_target_group_ids(values: Optional[list[int]]) -> list[int]:
    normalized: list[int] = []
    for raw_value in values or []:
        group_id = int(raw_value)
        if group_id <= 0:
            raise HTTPException(status_code=400, detail=f"Invalid target group ID: {group_id}")
        if group_id not in normalized:
            normalized.append(group_id)
    return normalized


async def _validate_target_group_ids(group_ids: list[int], db: AsyncSession) -> None:
    if not group_ids:
        return
    rows = await db.execute(select(Group.id).where(Group.id.in_(group_ids)))
    existing_ids = set(rows.scalars().all())
    missing_ids = [group_id for group_id in group_ids if group_id not in existing_ids]
    if missing_ids:
        raise HTTPException(status_code=400, detail=f"Target groups not found: {missing_ids}")

    joined_membership_exists = (
        select(GroupAccountMembership.id)
        .where(
            GroupAccountMembership.group_id == Group.id,
            GroupAccountMembership.status == "joined",
        )
        .exists()
    )
    available_rows = await db.execute(
        select(Group.id).where(
            Group.id.in_(group_ids),
            Group.status == "active",
            joined_membership_exists,
        )
    )
    available_ids = set(available_rows.scalars().all())
    unavailable_ids = [group_id for group_id in group_ids if group_id not in available_ids]
    if unavailable_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "Target groups are not active or not currently joined: "
                f"{unavailable_ids}"
            ),
        )


def _validate_campaign_schedule(send_mode: str, scheduled_times: list[str]) -> None:
    if send_mode == AdSendMode.SCHEDULED.value and not scheduled_times:
        raise HTTPException(
            status_code=400, detail="scheduled_times is required for scheduled campaigns"
        )


def _campaign_payload(data: dict) -> dict:
    if "send_mode" in data and data["send_mode"] not in {item.value for item in AdSendMode}:
        raise HTTPException(status_code=400, detail="Invalid send_mode")
    if "target_group_levels" in data:
        data["target_group_levels"] = json.dumps(data["target_group_levels"], ensure_ascii=False)
    if "target_group_ids" in data:
        data["target_group_ids"] = json.dumps(data["target_group_ids"], ensure_ascii=False)
    if "scheduled_times" in data:
        data["scheduled_times"] = json.dumps(data["scheduled_times"] or [], ensure_ascii=False)
    return data


@router.get("/ads/campaigns")
async def list_ad_campaigns(
    enabled: Optional[bool] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AdCampaign)
    count_query = select(func.count(AdCampaign.id))
    if enabled is not None:
        query = query.where(AdCampaign.enabled == enabled)
        count_query = count_query.where(AdCampaign.enabled == enabled)
    total = (await db.execute(count_query)).scalar() or 0
    rows = await db.execute(
        query.order_by(AdCampaign.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "code": 0,
        "message": "success",
        "data": [_campaign_to_dict(i) for i in rows.scalars().all()],
        "total": total,
    }


@router.post("/ads/campaigns", status_code=status.HTTP_201_CREATED)
async def create_ad_campaign(request: AdCampaignCreate, db: AsyncSession = Depends(get_db)) -> dict:
    data = request.model_dump()
    data["scheduled_times"] = _normalize_scheduled_times(data.get("scheduled_times"))
    data["target_group_ids"] = _normalize_target_group_ids(data.get("target_group_ids"))
    _validate_campaign_schedule(data["send_mode"], data["scheduled_times"])
    await _validate_target_group_ids(data["target_group_ids"], db)
    data = _campaign_payload(data)
    campaign = AdCampaign(**data)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return {"code": 0, "message": "success", "data": _campaign_to_dict(campaign)}


@router.put("/ads/campaigns/{campaign_id:int}")
async def update_ad_campaign(
    campaign_id: int,
    request: AdCampaignUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    campaign = (
        await db.execute(select(AdCampaign).where(AdCampaign.id == campaign_id))
    ).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    data = request.model_dump(exclude_none=True)
    if "scheduled_times" in data:
        data["scheduled_times"] = _normalize_scheduled_times(data["scheduled_times"])
    if "target_group_ids" in data:
        data["target_group_ids"] = _normalize_target_group_ids(data["target_group_ids"])
        await _validate_target_group_ids(data["target_group_ids"], db)
    send_mode = data.get("send_mode", campaign.send_mode)
    scheduled_times = data.get("scheduled_times", campaign.get_scheduled_times())
    _validate_campaign_schedule(send_mode, scheduled_times)
    data = _campaign_payload(data)
    for field, value in data.items():
        setattr(campaign, field, value)
    await db.commit()
    await db.refresh(campaign)
    return {"code": 0, "message": "success", "data": _campaign_to_dict(campaign)}


@router.delete("/ads/campaigns/{campaign_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ad_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)) -> None:
    campaign = (
        await db.execute(select(AdCampaign).where(AdCampaign.id == campaign_id))
    ).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    await db.delete(campaign)
    await db.commit()


class AccountAdBindingCreate(BaseModel):
    account_id: int
    ad_campaign_id: int
    creative_id: Optional[int] = None
    enabled: bool = True
    priority: int = 0


class AccountAdBindingBatchCreate(BaseModel):
    account_id: Optional[int] = None
    account_ids: list[int] = Field(default_factory=list)
    ad_campaign_id: int
    creative_ids: list[int] = Field(default_factory=list, min_length=1)
    enabled: bool = True
    priority: int = 0


class AccountAdBindingUpdate(BaseModel):
    creative_id: Optional[int] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


def _binding_to_dict(item: AccountAdBinding) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "ad_campaign_id": item.ad_campaign_id,
        "creative_id": item.creative_id,
        "enabled": item.enabled,
        "priority": item.priority,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


@router.get("/ads/bindings")
async def list_account_ad_bindings(
    account_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AccountAdBinding)
    if account_id:
        query = query.where(AccountAdBinding.account_id == account_id)
    if campaign_id:
        query = query.where(AccountAdBinding.ad_campaign_id == campaign_id)
    rows = await db.execute(
        query.order_by(AccountAdBinding.priority.desc(), AccountAdBinding.id.desc())
    )
    return {
        "code": 0,
        "message": "success",
        "data": [_binding_to_dict(i) for i in rows.scalars().all()],
    }


@router.post("/ads/bindings", status_code=status.HTTP_201_CREATED)
async def create_account_ad_binding(
    request: AccountAdBindingCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    binding = AccountAdBinding(**request.model_dump())
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return {"code": 0, "message": "success", "data": _binding_to_dict(binding)}


@router.post("/ads/bindings/batch", status_code=status.HTTP_201_CREATED)
async def create_account_ad_bindings_batch(
    request: AccountAdBindingBatchCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not request.creative_ids:
        raise HTTPException(status_code=400, detail="creative_ids is required")

    account_ids = list(
        dict.fromkeys(
            [
                *request.account_ids,
                *([request.account_id] if request.account_id is not None else []),
            ]
        )
    )
    if not account_ids:
        raise HTTPException(status_code=400, detail='account_id or account_ids is required')
    if any(account_id <= 0 for account_id in account_ids):
        raise HTTPException(status_code=400, detail='account_ids must contain positive integers')

    existing_account_rows = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id.in_(account_ids))
    )
    accounts_by_id = {
        account.id: account for account in existing_account_rows.scalars().all()
    }
    missing_account_ids = [
        account_id for account_id in account_ids if account_id not in accounts_by_id
    ]
    if missing_account_ids:
        raise HTTPException(status_code=404, detail=f'Account not found: {missing_account_ids[0]}')
    banned_account_ids = [
        account_id
        for account_id in account_ids
        if accounts_by_id[account_id].status == AccountStatus.BANNED
    ]
    if banned_account_ids:
        raise HTTPException(
            status_code=409,
            detail=f'Banned account cannot be bound: {banned_account_ids[0]}',
        )

    requested_creative_ids = list(dict.fromkeys(request.creative_ids))
    creatives = (
        (
            await db.execute(
                select(AdCreative).where(
                    AdCreative.id.in_(requested_creative_ids),
                    AdCreative.enabled == True,
                )
            )
        )
        .scalars()
        .all()
    )
    available_creative_ids = {item.id for item in creatives}
    missing = [
        creative_id
        for creative_id in requested_creative_ids
        if creative_id not in available_creative_ids
    ]
    if missing:
        raise HTTPException(status_code=404, detail=f"Creative not found: {missing[0]}")

    existing_rows = await db.execute(
        select(AccountAdBinding.account_id, AccountAdBinding.creative_id).where(
            AccountAdBinding.account_id.in_(account_ids),
            AccountAdBinding.ad_campaign_id == request.ad_campaign_id,
            AccountAdBinding.creative_id.in_(requested_creative_ids),
        )
    )
    existing_pairs = {
        (account_id, creative_id)
        for account_id, creative_id in existing_rows.all()
        if creative_id is not None
    }

    rows = []
    for account_id in account_ids:
        for creative_id in requested_creative_ids:
            if (account_id, creative_id) in existing_pairs:
                continue
            binding = AccountAdBinding(
                account_id=account_id,
                ad_campaign_id=request.ad_campaign_id,
                creative_id=creative_id,
                enabled=request.enabled,
                priority=request.priority,
            )
            db.add(binding)
            rows.append(binding)

    if not rows:
        return {"code": 0, "message": "success", "data": []}

    await db.commit()
    for binding in rows:
        await db.refresh(binding)
    return {"code": 0, "message": "success", "data": [_binding_to_dict(item) for item in rows]}


class CreativePoolEnsureRequest(BaseModel):
    account_id: int
    ad_campaign_id: int
    min_pool_size: int = Field(default=3, ge=1, le=20)
    generate_count: int = Field(default=3, ge=1, le=10)


@router.post("/ads/creatives/ensure-pool", status_code=status.HTTP_200_OK)
async def ensure_ad_creative_pool(
    request: CreativePoolEnsureRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = AcquisitionAutomationService(db)
    try:
        result = await service.ensure_ad_creative_pool(
            request.account_id,
            request.ad_campaign_id,
            min_pool_size=request.min_pool_size,
            generate_count=request.generate_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"code": 0, "message": "success", "data": result}


@router.put("/ads/bindings/{binding_id:int}")
async def update_account_ad_binding(
    binding_id: int,
    request: AccountAdBindingUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    binding = (
        await db.execute(select(AccountAdBinding).where(AccountAdBinding.id == binding_id))
    ).scalar_one_or_none()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    for field, value in request.model_dump(exclude_none=True).items():
        setattr(binding, field, value)
    await db.commit()
    await db.refresh(binding)
    return {"code": 0, "message": "success", "data": _binding_to_dict(binding)}


@router.delete("/ads/bindings/{binding_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_ad_binding(binding_id: int, db: AsyncSession = Depends(get_db)) -> None:
    binding = (
        await db.execute(select(AccountAdBinding).where(AccountAdBinding.id == binding_id))
    ).scalar_one_or_none()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    await db.delete(binding)
    await db.commit()


@router.get("/ads/delivery-logs")
async def list_ad_delivery_logs(
    account_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AdDeliveryLog)
    if account_id:
        query = query.where(AdDeliveryLog.account_id == account_id)
    if campaign_id:
        query = query.where(AdDeliveryLog.ad_campaign_id == campaign_id)
    if status_filter:
        query = query.where(AdDeliveryLog.status == status_filter)
    if start_at:
        query = query.where(AdDeliveryLog.created_at >= start_at)
    if end_at:
        query = query.where(AdDeliveryLog.created_at <= end_at)

    effective_page_size = limit or page_size
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    rows = await db.execute(
        query.order_by(AdDeliveryLog.created_at.desc())
        .offset((page - 1) * effective_page_size)
        .limit(effective_page_size)
    )
    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": item.id,
                "account_id": item.account_id,
                "group_id": item.group_id,
                "telegram_group_id": item.telegram_group_id,
                "group_title": item.group.title if item.group else None,
                "group_username": item.group.username if item.group else None,
                "ad_campaign_id": item.ad_campaign_id,
                "creative_id": item.creative_id,
                "status": item.status,
                "telegram_message_id": item.telegram_message_id,
                "survival_status": item.survival_status,
                "survival_check_due_at": item.survival_check_due_at.isoformat()
                if item.survival_check_due_at
                else None,
                "survival_checked_at": item.survival_checked_at.isoformat()
                if item.survival_checked_at
                else None,
                "survival_error": item.survival_error,
                "error": item.error,
                "sent_at": item.sent_at.isoformat() if item.sent_at else None,
                "created_at": item.created_at.isoformat() if item.created_at else "",
            }
            for item in rows.scalars().all()
        ],
        "total": total,
        "page": page,
        "page_size": effective_page_size,
    }
