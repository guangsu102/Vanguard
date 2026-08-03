"""
Campaigns API Router

RESTful API for campaign management with cursor pagination.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guardian_validation import ensure_guardian_bot_account, ensure_managed_group_bindings
from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignScope,
    CampaignTracking,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.database import get_db
from app.core.scheduler.tasks import execute_campaign_rewards
from app.core.user.models import User, UserState
from app.modules.guardian.models import GroupCampaignTriggerEvent

router = APIRouter()


LEGACY_MANAGED_GROUP_TRIGGER_EVENT_ALIASES = {
    "member_join": GroupCampaignTriggerEvent.USER_JOINED.value,
    "member_joined": GroupCampaignTriggerEvent.USER_JOINED.value,
    "scheduled_broadcast": GroupCampaignTriggerEvent.SCHEDULED.value,
}

MANAGED_GROUP_TRIGGER_TIMING_BY_EVENT = {
    GroupCampaignTriggerEvent.USER_JOINED: CampaignTriggerTiming.IMMEDIATE,
    GroupCampaignTriggerEvent.VERIFICATION_PASSED: CampaignTriggerTiming.IMMEDIATE,
    GroupCampaignTriggerEvent.NEW_MEMBER_DELAY: CampaignTriggerTiming.DELAYED,
    GroupCampaignTriggerEvent.SCHEDULED: CampaignTriggerTiming.SCHEDULED,
    GroupCampaignTriggerEvent.MANUAL_BROADCAST: CampaignTriggerTiming.MANUAL,
    GroupCampaignTriggerEvent.PERIODIC: CampaignTriggerTiming.PERIODIC,
}

MANAGED_GROUP_DISTRIBUTION_MODE_BY_EVENT = {
    GroupCampaignTriggerEvent.USER_JOINED: CampaignDistributionMode.WELCOME,
    GroupCampaignTriggerEvent.VERIFICATION_PASSED: CampaignDistributionMode.WELCOME,
    GroupCampaignTriggerEvent.NEW_MEMBER_DELAY: CampaignDistributionMode.DELAYED,
    GroupCampaignTriggerEvent.SCHEDULED: CampaignDistributionMode.SCHEDULED,
    GroupCampaignTriggerEvent.MANUAL_BROADCAST: CampaignDistributionMode.MANUAL,
    GroupCampaignTriggerEvent.PERIODIC: CampaignDistributionMode.PERIODIC,
}

SUPPORTED_CAMPAIGN_TYPES = {CampaignType.DISCOUNT}


# =============================================================================
# Request/Response Models
# =============================================================================


class CampaignCreate(BaseModel):
    """Campaign creation request."""

    name: str = Field(..., max_length=100, description="Campaign name")
    campaign_type: str = Field(default=CampaignType.DISCOUNT.value, description="Campaign type: discount coupon")
    campaign_scope: str = Field(default="global", description="Campaign scope: global/managed_group")
    trigger_timing: CampaignTriggerTiming = Field(
        default=CampaignTriggerTiming.AFTER_REGISTER,
        description="Trigger timing",
    )
    trigger_event: Optional[str] = Field(None, description="Managed-group trigger event")
    validity_hours: int = Field(default=168, ge=1, description="Validity period in hours")
    target_group_ids: Optional[list[int]] = None
    bot_account_id: Optional[int] = None
    distribution_mode: Optional[CampaignDistributionMode] = None
    reward_policy_json: Optional[dict] = None
    broadcast_policy_json: Optional[dict] = None
    eligibility_policy_json: Optional[dict] = None
    broadcast_message: Optional[str] = Field(None, description="Message content to broadcast")
    delay_minutes: Optional[int] = Field(None, ge=1, le=10080, description="Delay in minutes")
    schedule_times: Optional[list[str]] = Field(None, description="Scheduled HH:MM times")
    interval_minutes: Optional[int] = Field(None, ge=5, le=10080, description="Periodic interval in minutes")
    verified_only: Optional[bool] = Field(None, description="Only users who passed verification are eligible")
    once_per_user: Optional[bool] = Field(None, description="Only grant or trigger once per user")
    min_join_minutes: Optional[int] = Field(None, ge=0, le=10080, description="Minimum join age in minutes")
    target_user_states: Optional[list[str]] = Field(None, description="Eligible global user lifecycle states")
    target_limit: Optional[int] = Field(None, ge=0, le=100000, description="Maximum global target users")
    min_account_age_minutes: Optional[int] = Field(None, ge=0, le=10080, description="Minimum account age")
    coupon_provider: Optional[str] = Field(None, description="Coupon provider: xboard/sub2api")
    coupon_amount: Optional[float] = Field(None, gt=0, description="Sub2API balance or concurrency value")
    coupon_quantity: Optional[int] = Field(None, ge=1, le=100, description="Maximum claims per coupon batch")
    coupon_type: Optional[str] = Field(None, description="Sub2API redeem-code type")
    coupon_batch_key: Optional[str] = Field(None, max_length=100, description="Coupon batch key")
    sub2api_group_id: Optional[int] = Field(None, gt=0, description="Sub2API subscription group id")
    sub2api_validity_days: Optional[int] = Field(None, description="Sub2API subscription validity days")
    enabled: bool = Field(default=False)


class CampaignUpdate(BaseModel):
    """Campaign update request."""

    name: Optional[str] = Field(None, max_length=100)
    campaign_type: Optional[str] = None
    campaign_scope: Optional[str] = None
    trigger_timing: Optional[CampaignTriggerTiming] = None
    trigger_event: Optional[str] = None
    validity_hours: Optional[int] = Field(None, ge=1)
    target_group_ids: Optional[list[int]] = None
    bot_account_id: Optional[int] = None
    distribution_mode: Optional[CampaignDistributionMode] = None
    reward_policy_json: Optional[dict] = None
    broadcast_policy_json: Optional[dict] = None
    eligibility_policy_json: Optional[dict] = None
    broadcast_message: Optional[str] = None
    delay_minutes: Optional[int] = Field(None, ge=1, le=10080)
    schedule_times: Optional[list[str]] = None
    interval_minutes: Optional[int] = Field(None, ge=5, le=10080)
    verified_only: Optional[bool] = None
    once_per_user: Optional[bool] = None
    min_join_minutes: Optional[int] = Field(None, ge=0, le=10080)
    target_user_states: Optional[list[str]] = None
    target_limit: Optional[int] = Field(None, ge=0, le=100000)
    min_account_age_minutes: Optional[int] = Field(None, ge=0, le=10080)
    coupon_provider: Optional[str] = None
    coupon_amount: Optional[float] = Field(None, gt=0)
    coupon_quantity: Optional[int] = Field(None, ge=1, le=100)
    coupon_type: Optional[str] = None
    coupon_batch_key: Optional[str] = Field(None, max_length=100)
    sub2api_group_id: Optional[int] = Field(None, gt=0)
    sub2api_validity_days: Optional[int] = None
    enabled: Optional[bool] = None


class CampaignResponse(BaseModel):
    """Campaign response."""

    id: int
    name: str
    campaign_type: str
    campaign_scope: str
    trigger_timing: CampaignTriggerTiming
    trigger_event: Optional[str] = None
    validity_hours: int
    target_group_ids: Optional[list[int]] = None
    bot_account_id: Optional[int] = None
    distribution_mode: Optional[CampaignDistributionMode] = None
    reward_policy_json: Optional[dict] = None
    broadcast_policy_json: Optional[dict] = None
    eligibility_policy_json: Optional[dict] = None
    broadcast_message: Optional[str] = None
    delay_minutes: Optional[int] = None
    schedule_times: Optional[list[str]] = None
    interval_minutes: Optional[int] = None
    verified_only: bool = False
    once_per_user: bool = False
    min_join_minutes: Optional[int] = None
    target_user_states: Optional[list[str]] = None
    target_limit: Optional[int] = None
    min_account_age_minutes: Optional[int] = None
    coupon_provider: Optional[str] = None
    coupon_amount: Optional[float] = None
    coupon_quantity: Optional[int] = None
    coupon_type: Optional[str] = None
    coupon_batch_key: Optional[str] = None
    sub2api_group_id: Optional[int] = None
    sub2api_validity_days: Optional[int] = None
    enabled: bool
    created_at: str
    updated_at: str


class CampaignListResponse(BaseModel):
    """Campaign list response with cursor pagination."""

    code: int = 0
    message: str = "success"
    data: list[CampaignResponse]
    total: int
    next_cursor: Optional[str] = None
    has_more: bool = False


class CampaignStatsResponse(BaseModel):
    """Campaign statistics response."""

    code: int = 0
    message: str = "success"
    data: dict


class CampaignTrackingResponse(BaseModel):
    """Campaign tracking record response."""

    id: int
    user_id: int
    campaign_name: Optional[str]
    source: Optional[str]
    group_id: Optional[int]
    keyword: Optional[str]
    bot_id: Optional[str]
    registered_at: Optional[str]
    converted_at: Optional[str]
    validity_started_at: Optional[str]
    trial_granted: bool
    coupon_granted: bool
    created_at: str


class CampaignTrackingListResponse(BaseModel):
    """Campaign tracking list response."""

    code: int = 0
    message: str = "success"
    data: list[CampaignTrackingResponse]
    total: int
    next_cursor: Optional[str] = None
    has_more: bool = False


# =============================================================================
# Helper Functions
# =============================================================================


def _campaign_to_response(campaign: Campaign) -> CampaignResponse:
    """Convert Campaign model to response."""
    import json

    def parse_json(raw: Optional[str]) -> Optional[dict]:
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    target_group_ids = None
    if campaign.target_group_ids:
        try:
            target_group_ids = json.loads(campaign.target_group_ids)
        except Exception:
            target_group_ids = None

    reward_policy = parse_json(campaign.reward_policy_json) or {}
    broadcast_policy = parse_json(campaign.broadcast_policy_json) or {}
    eligibility_policy = parse_json(campaign.eligibility_policy_json) or {}
    broadcast_message = (
        broadcast_policy.get("message")
        or reward_policy.get("message")
        or reward_policy.get("welcome_message")
        or broadcast_policy.get("template")
    )

    return CampaignResponse(
        id=campaign.id,
        name=campaign.name,
        campaign_type=campaign.campaign_type.value,
        campaign_scope=campaign.campaign_scope.value,
        trigger_timing=_coerce_trigger_timing(campaign.trigger_timing),
        trigger_event=campaign.trigger_event,
        validity_hours=campaign.validity_hours,
        target_group_ids=target_group_ids,
        bot_account_id=campaign.bot_account_id,
        distribution_mode=_coerce_distribution_mode(campaign.distribution_mode, None),
        reward_policy_json=reward_policy or None,
        broadcast_policy_json=broadcast_policy or None,
        eligibility_policy_json=eligibility_policy or None,
        broadcast_message=broadcast_message if isinstance(broadcast_message, str) else None,
        delay_minutes=broadcast_policy.get("delay_minutes") if isinstance(broadcast_policy.get("delay_minutes"), int) else None,
        schedule_times=broadcast_policy.get("schedule_times") if isinstance(broadcast_policy.get("schedule_times"), list) else None,
        interval_minutes=(
            broadcast_policy.get("interval_minutes") if isinstance(broadcast_policy.get("interval_minutes"), int) else None
        ),
        verified_only=bool(eligibility_policy.get("verified_only")),
        once_per_user=bool(eligibility_policy.get("once_per_user")),
        min_join_minutes=(
            eligibility_policy.get("min_join_minutes")
            if isinstance(eligibility_policy.get("min_join_minutes"), int)
            else None
        ),
        target_user_states=(
            eligibility_policy.get("target_user_states")
            if isinstance(eligibility_policy.get("target_user_states"), list)
            else None
        ),
        target_limit=(
            eligibility_policy.get("target_limit")
            if isinstance(eligibility_policy.get("target_limit"), int)
            else None
        ),
        min_account_age_minutes=(
            eligibility_policy.get("min_account_age_minutes")
            if isinstance(eligibility_policy.get("min_account_age_minutes"), int)
            else None
        ),
        coupon_provider=(
            reward_policy.get("coupon_provider")
            if isinstance(reward_policy.get("coupon_provider"), str)
            else None
        ),
        coupon_amount=(
            float(reward_policy.get("coupon_amount"))
            if isinstance(reward_policy.get("coupon_amount"), (int, float))
            else None
        ),
        coupon_quantity=(
            reward_policy.get("coupon_quantity")
            if isinstance(reward_policy.get("coupon_quantity"), int)
            else None
        ),
        coupon_type=(
            reward_policy.get("coupon_type")
            if isinstance(reward_policy.get("coupon_type"), str)
            else None
        ),
        coupon_batch_key=(
            reward_policy.get("coupon_batch_key")
            if isinstance(reward_policy.get("coupon_batch_key"), str)
            else None
        ),
        sub2api_group_id=(
            reward_policy.get("sub2api_group_id")
            if isinstance(reward_policy.get("sub2api_group_id"), int)
            else None
        ),
        sub2api_validity_days=(
            reward_policy.get("sub2api_validity_days")
            if isinstance(reward_policy.get("sub2api_validity_days"), int)
            else None
        ),
        enabled=campaign.enabled,
        created_at=campaign.created_at.isoformat() if campaign.created_at else "",
        updated_at=campaign.updated_at.isoformat() if campaign.updated_at else "",
    )


def _coerce_trigger_timing(
    value: CampaignTriggerTiming | str | None,
    default: CampaignTriggerTiming = CampaignTriggerTiming.AFTER_REGISTER,
) -> CampaignTriggerTiming:
    """Normalize trigger timing to the supported enum."""
    if value is None or value == "":
        return default
    if isinstance(value, CampaignTriggerTiming):
        return value
    try:
        return CampaignTriggerTiming(str(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid trigger_timing. Must be one of: {[item.value for item in CampaignTriggerTiming]}",
        ) from exc


def _coerce_distribution_mode(
    value: CampaignDistributionMode | str | None,
    default: Optional[CampaignDistributionMode] = None,
) -> Optional[CampaignDistributionMode]:
    """Normalize distribution mode to the supported enum."""
    if value is None or value == "":
        return default
    if isinstance(value, CampaignDistributionMode):
        return value
    try:
        return CampaignDistributionMode(str(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid distribution_mode. Must be one of: {[item.value for item in CampaignDistributionMode]}",
        ) from exc


def _coerce_campaign_type(value: CampaignType | str | None) -> CampaignType:
    """Normalize and constrain campaign type to the current XBoard capability."""
    try:
        campaign_type = value if isinstance(value, CampaignType) else CampaignType(str(value))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid campaign type. Must be one of: {[item.value for item in SUPPORTED_CAMPAIGN_TYPES]}",
        ) from exc

    if campaign_type not in SUPPORTED_CAMPAIGN_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Current XBoard campaign integration only supports coupon campaigns",
        )
    return campaign_type


def _apply_structured_policy_fields(
    *,
    reward_policy_json: Optional[dict],
    broadcast_policy_json: Optional[dict],
    eligibility_policy_json: Optional[dict],
    broadcast_message: Optional[str] = None,
    delay_minutes: Optional[int] = None,
    schedule_times: Optional[list[str]] = None,
    interval_minutes: Optional[int] = None,
    verified_only: Optional[bool] = None,
    once_per_user: Optional[bool] = None,
    min_join_minutes: Optional[int] = None,
    target_user_states: Optional[list[str]] = None,
    target_limit: Optional[int] = None,
    min_account_age_minutes: Optional[int] = None,
    coupon_provider: Optional[str] = None,
    coupon_amount: Optional[float] = None,
    coupon_quantity: Optional[int] = None,
    coupon_type: Optional[str] = None,
    coupon_batch_key: Optional[str] = None,
    sub2api_group_id: Optional[int] = None,
    sub2api_validity_days: Optional[int] = None,
) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Merge user-facing structured fields into the legacy policy JSON storage."""
    reward_policy = dict(reward_policy_json or {})
    broadcast_policy = dict(broadcast_policy_json or {})
    eligibility_policy = dict(eligibility_policy_json or {})

    if broadcast_message is not None:
        message = broadcast_message.strip()
        if message:
            broadcast_policy["message"] = message
        else:
            broadcast_policy.pop("message", None)
            reward_policy.pop("message", None)
            reward_policy.pop("welcome_message", None)

    if delay_minutes is not None:
        broadcast_policy["delay_minutes"] = delay_minutes
    if schedule_times is not None:
        broadcast_policy["schedule_times"] = schedule_times
    if interval_minutes is not None:
        broadcast_policy["interval_minutes"] = interval_minutes

    if coupon_provider is not None:
        provider = coupon_provider.strip().lower()
        if provider:
            if provider not in {"xboard", "sub2api"}:
                raise HTTPException(status_code=400, detail="coupon_provider must be xboard or sub2api")
            reward_policy["coupon_provider"] = provider
        else:
            reward_policy.pop("coupon_provider", None)
    if coupon_amount is not None:
        reward_policy["coupon_amount"] = coupon_amount
    if coupon_quantity is not None:
        reward_policy["coupon_quantity"] = coupon_quantity
    if coupon_type is not None:
        normalized_coupon_type = coupon_type.strip()
        if normalized_coupon_type:
            if normalized_coupon_type not in {"balance", "concurrency", "subscription", "invitation"}:
                raise HTTPException(
                    status_code=400,
                    detail="coupon_type must be one of: balance, concurrency, subscription, invitation",
                )
            reward_policy["coupon_type"] = normalized_coupon_type
        else:
            reward_policy.pop("coupon_type", None)
    if coupon_batch_key is not None:
        normalized_batch_key = coupon_batch_key.strip()
        if normalized_batch_key:
            reward_policy["coupon_batch_key"] = normalized_batch_key
        else:
            reward_policy.pop("coupon_batch_key", None)
    if sub2api_group_id is not None:
        reward_policy["sub2api_group_id"] = sub2api_group_id
    if sub2api_validity_days is not None:
        reward_policy["sub2api_validity_days"] = sub2api_validity_days

    if verified_only is not None:
        if verified_only:
            eligibility_policy["verified_only"] = True
        else:
            eligibility_policy.pop("verified_only", None)
    if once_per_user is not None:
        if once_per_user:
            eligibility_policy["once_per_user"] = True
        else:
            eligibility_policy.pop("once_per_user", None)
    if min_join_minutes is not None:
        if min_join_minutes > 0:
            eligibility_policy["min_join_minutes"] = min_join_minutes
        else:
            eligibility_policy.pop("min_join_minutes", None)
    if target_user_states is not None:
        cleaned_states: list[str] = []
        for state in target_user_states:
            try:
                state_enum = UserState(str(state))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid target_user_states. Must be one of: {[item.value for item in UserState]}",
                ) from exc
            if state_enum.value not in cleaned_states:
                cleaned_states.append(state_enum.value)
        if cleaned_states:
            eligibility_policy["target_user_states"] = cleaned_states
        else:
            eligibility_policy.pop("target_user_states", None)
    if target_limit is not None:
        if target_limit > 0:
            eligibility_policy["target_limit"] = target_limit
        else:
            eligibility_policy.pop("target_limit", None)
    if min_account_age_minutes is not None:
        if min_account_age_minutes > 0:
            eligibility_policy["min_account_age_minutes"] = min_account_age_minutes
        else:
            eligibility_policy.pop("min_account_age_minutes", None)

    return reward_policy or None, broadcast_policy or None, eligibility_policy or None


def _default_distribution_mode_for_timing(
    trigger_timing: CampaignTriggerTiming,
) -> CampaignDistributionMode:
    mapping = {
        CampaignTriggerTiming.AFTER_REGISTER: CampaignDistributionMode.WELCOME,
        CampaignTriggerTiming.IMMEDIATE: CampaignDistributionMode.WELCOME,
        CampaignTriggerTiming.DELAYED: CampaignDistributionMode.DELAYED,
        CampaignTriggerTiming.SCHEDULED: CampaignDistributionMode.SCHEDULED,
        CampaignTriggerTiming.MANUAL: CampaignDistributionMode.MANUAL,
        CampaignTriggerTiming.PERIODIC: CampaignDistributionMode.PERIODIC,
    }
    return mapping[trigger_timing]


def _normalize_broadcast_policy_for_distribution(
    distribution_mode: Optional[CampaignDistributionMode],
    broadcast_policy_json: Optional[dict],
) -> Optional[dict]:
    normalized_broadcast_policy = dict(broadcast_policy_json or {})

    if distribution_mode == CampaignDistributionMode.DELAYED:
        delay_minutes = normalized_broadcast_policy.get("delay_minutes")
        if not isinstance(delay_minutes, int) or delay_minutes < 1 or delay_minutes > 10080:
            raise HTTPException(status_code=400, detail="delay_minutes must be between 1 and 10080")
        normalized_broadcast_policy.pop("schedule_times", None)
        normalized_broadcast_policy.pop("interval_minutes", None)
    elif distribution_mode == CampaignDistributionMode.SCHEDULED:
        schedule_times = normalized_broadcast_policy.get("schedule_times")
        if not isinstance(schedule_times, list) or not schedule_times:
            raise HTTPException(status_code=400, detail="schedule_times is required for scheduled campaigns")
        cleaned_times: list[str] = []
        for item in schedule_times:
            if not isinstance(item, str):
                raise HTTPException(status_code=400, detail="schedule_times must be an array of HH:MM strings")
            value = item.strip()
            if len(value) != 5 or value[2] != ":":
                raise HTTPException(status_code=400, detail="schedule_times must use HH:MM format")
            hour, minute = value.split(":", 1)
            if not hour.isdigit() or not minute.isdigit():
                raise HTTPException(status_code=400, detail="schedule_times must use HH:MM format")
            hour_num = int(hour)
            minute_num = int(minute)
            if hour_num < 0 or hour_num > 23 or minute_num < 0 or minute_num > 59:
                raise HTTPException(status_code=400, detail="schedule_times must use valid HH:MM values")
            cleaned_times.append(f"{hour_num:02d}:{minute_num:02d}")
        normalized_broadcast_policy["schedule_times"] = list(dict.fromkeys(cleaned_times))
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("interval_minutes", None)
    elif distribution_mode == CampaignDistributionMode.PERIODIC:
        interval_minutes = normalized_broadcast_policy.get("interval_minutes")
        if not isinstance(interval_minutes, int) or interval_minutes < 5 or interval_minutes > 10080:
            raise HTTPException(status_code=400, detail="interval_minutes must be between 5 and 10080")
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("schedule_times", None)
    else:
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("schedule_times", None)
        normalized_broadcast_policy.pop("interval_minutes", None)

    return normalized_broadcast_policy or None


def _validate_coupon_reward_policy(reward_policy_json: Optional[dict]) -> Optional[dict]:
    reward_policy = dict(reward_policy_json or {})
    provider = reward_policy.get("coupon_provider")
    if provider is None:
        return reward_policy or None

    if provider not in {"xboard", "sub2api"}:
        raise HTTPException(status_code=400, detail="coupon_provider must be xboard or sub2api")

    if provider != "sub2api":
        return reward_policy or None

    quantity = reward_policy.get("coupon_quantity", 1)
    if not isinstance(quantity, int) or quantity < 1 or quantity > 100:
        raise HTTPException(status_code=400, detail="coupon_quantity must be between 1 and 100")

    coupon_type = reward_policy.get("coupon_type", "balance")
    if coupon_type not in {"balance", "concurrency", "subscription", "invitation"}:
        raise HTTPException(
            status_code=400,
            detail="coupon_type must be one of: balance, concurrency, subscription, invitation",
        )
    if coupon_type in {"balance", "concurrency"}:
        amount = reward_policy.get("coupon_amount")
        if not isinstance(amount, (int, float)) or amount <= 0:
            raise HTTPException(
                status_code=400,
                detail="coupon_amount is required for balance and concurrency coupons",
            )
    if coupon_type == "subscription":
        if not isinstance(reward_policy.get("sub2api_group_id"), int):
            raise HTTPException(status_code=400, detail="sub2api_group_id is required for subscription coupons")
        if not isinstance(reward_policy.get("sub2api_validity_days"), int) or reward_policy["sub2api_validity_days"] == 0:
            raise HTTPException(status_code=400, detail="sub2api_validity_days is required for subscription coupons")

    return reward_policy


def _tracking_to_response(tracking: CampaignTracking) -> CampaignTrackingResponse:
    """Convert CampaignTracking model to response."""
    return CampaignTrackingResponse(
        id=tracking.id,
        user_id=tracking.user_id,
        campaign_name=tracking.campaign_name,
        source=tracking.source,
        group_id=tracking.group_id,
        keyword=tracking.keyword,
        bot_id=tracking.bot_id,
        registered_at=tracking.registered_at.isoformat() if tracking.registered_at else None,
        converted_at=tracking.converted_at.isoformat() if tracking.converted_at else None,
        validity_started_at=tracking.validity_started_at.isoformat() if tracking.validity_started_at else None,
        trial_granted=tracking.trial_granted,
        coupon_granted=tracking.coupon_granted,
        created_at=tracking.created_at.isoformat() if tracking.created_at else "",
    )


async def _validate_managed_group_campaign_payload(
    db: AsyncSession,
    scope: CampaignScope,
    target_group_ids: Optional[list[int]],
    bot_account_id: Optional[int],
    trigger_event: Optional[str],
    trigger_timing: CampaignTriggerTiming | str | None,
    _distribution_mode: CampaignDistributionMode | str | None,
    reward_policy_json: Optional[dict],
    broadcast_policy_json: Optional[dict],
    eligibility_policy_json: Optional[dict],
) -> tuple[
    Optional[list[int]],
    Optional[int],
    Optional[str],
    CampaignTriggerTiming,
    Optional[CampaignDistributionMode],
    Optional[dict],
    Optional[dict],
    Optional[dict],
]:
    """Validate campaign payload according to campaign scope."""
    if scope != CampaignScope.MANAGED_GROUP:
        normalized_trigger_timing = _coerce_trigger_timing(trigger_timing)
        normalized_distribution_mode = _default_distribution_mode_for_timing(normalized_trigger_timing)
        normalized_broadcast_policy = _normalize_broadcast_policy_for_distribution(
            normalized_distribution_mode,
            broadcast_policy_json,
        )
        normalized_reward_policy = _validate_coupon_reward_policy(reward_policy_json)
        return (
            None,
            None,
            None,
            normalized_trigger_timing,
            normalized_distribution_mode,
            normalized_reward_policy,
            normalized_broadcast_policy,
            eligibility_policy_json,
        )

    bindings = await ensure_managed_group_bindings(db, target_group_ids or [])

    if bot_account_id is None:
        raise HTTPException(status_code=400, detail="bot_account_id is required for managed_group campaigns")

    await ensure_guardian_bot_account(db, bot_account_id)

    mismatched_groups = [
        binding.telegram_group_id for binding in bindings if binding.bot_account_id != bot_account_id
    ]
    if mismatched_groups:
        raise HTTPException(
            status_code=400,
            detail=(
                "bot_account_id must match the primary guardian bot for all target groups: "
                f"{', '.join(map(str, mismatched_groups))}"
            ),
        )

    normalized_trigger_event = (trigger_event or "").strip()
    if not normalized_trigger_event:
        raise HTTPException(status_code=400, detail="trigger_event is required for managed_group campaigns")

    normalized_trigger_event = LEGACY_MANAGED_GROUP_TRIGGER_EVENT_ALIASES.get(
        normalized_trigger_event,
        normalized_trigger_event,
    )
    try:
        trigger_event_enum = GroupCampaignTriggerEvent(normalized_trigger_event)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid trigger_event for managed_group campaign. Must be one of: "
                f"{[item.value for item in GroupCampaignTriggerEvent]}"
            ),
        )

    expected_trigger_timing = MANAGED_GROUP_TRIGGER_TIMING_BY_EVENT[trigger_event_enum]
    normalized_trigger_timing = _coerce_trigger_timing(trigger_timing, expected_trigger_timing)
    if normalized_trigger_timing and normalized_trigger_timing != expected_trigger_timing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"trigger_timing must be '{expected_trigger_timing.value}' for trigger_event "
                f"'{trigger_event_enum.value}'"
            ),
        )

    normalized_distribution_mode = MANAGED_GROUP_DISTRIBUTION_MODE_BY_EVENT[trigger_event_enum]

    normalized_reward_policy = _validate_coupon_reward_policy(reward_policy_json)
    normalized_broadcast_policy = _normalize_broadcast_policy_for_distribution(
        normalized_distribution_mode,
        broadcast_policy_json,
    )
    normalized_eligibility_policy = dict(eligibility_policy_json or {})

    return (
        [binding.telegram_group_id for binding in bindings],
        bot_account_id,
        trigger_event_enum.value,
        expected_trigger_timing,
        normalized_distribution_mode,
        normalized_reward_policy,
        normalized_broadcast_policy,
        normalized_eligibility_policy or None,
    )


def _parse_json_field(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        import json

        return json.loads(raw)
    except Exception:
        return None


# =============================================================================
# Campaign CRUD Endpoints
# =============================================================================


@router.get("", response_model=CampaignListResponse)
async def list_campaigns(
    cursor: Optional[str] = None,
    limit: int = 20,
    campaign_type: Optional[str] = None,
    campaign_scope: Optional[str] = None,
    enabled: Optional[bool] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> CampaignListResponse:
    """
    Get list of campaigns with cursor pagination.

    - cursor: Pagination cursor (campaign ID from previous response)
    - limit: Number of items per page (max 100)
    - campaign_type: Filter by type (currently discount coupon only)
    - enabled: Filter by enabled status
    """
    query = select(Campaign)
    count_query = select(func.count(Campaign.id))
    query = query.where(Campaign.campaign_type == CampaignType.DISCOUNT)
    count_query = count_query.where(Campaign.campaign_type == CampaignType.DISCOUNT)

    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.where(Campaign.id < cursor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    if campaign_type:
        _coerce_campaign_type(campaign_type)

    if campaign_scope:
        try:
            scope_enum = CampaignScope(campaign_scope)
            query = query.where(Campaign.campaign_scope == scope_enum)
            count_query = count_query.where(Campaign.campaign_scope == scope_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid campaign scope: {campaign_scope}")

    if enabled is not None:
        query = query.where(Campaign.enabled == enabled)
        count_query = count_query.where(Campaign.enabled == enabled)

    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(
                Campaign.name.ilike(pattern),
                cast(Campaign.trigger_timing, String).ilike(pattern),
                Campaign.trigger_event.ilike(pattern),
            )
        )
        count_query = count_query.where(
            or_(
                Campaign.name.ilike(pattern),
                cast(Campaign.trigger_timing, String).ilike(pattern),
                Campaign.trigger_event.ilike(pattern),
            )
        )

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(desc(Campaign.id)).limit(limit + 1)
    result = await db.execute(query)
    campaigns = list(result.scalars().all())

    has_more = len(campaigns) > limit
    if has_more:
        campaigns = campaigns[:limit]

    next_cursor = str(campaigns[-1].id) if campaigns and has_more else None

    return CampaignListResponse(
        data=[_campaign_to_response(campaign) for campaign in campaigns],
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    campaign_data: CampaignCreate,
    db: AsyncSession = Depends(get_db),
) -> CampaignResponse:
    """Create a new campaign."""
    type_enum = _coerce_campaign_type(campaign_data.campaign_type)

    try:
        scope_enum = CampaignScope(campaign_data.campaign_scope)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid campaign scope. Must be one of: {[item.value for item in CampaignScope]}",
        )

    import json

    reward_policy_input, broadcast_policy_input, eligibility_policy_input = _apply_structured_policy_fields(
        reward_policy_json=campaign_data.reward_policy_json,
        broadcast_policy_json=campaign_data.broadcast_policy_json,
        eligibility_policy_json=campaign_data.eligibility_policy_json,
        broadcast_message=campaign_data.broadcast_message,
        delay_minutes=campaign_data.delay_minutes,
        schedule_times=campaign_data.schedule_times,
        interval_minutes=campaign_data.interval_minutes,
        verified_only=campaign_data.verified_only,
        once_per_user=campaign_data.once_per_user,
        min_join_minutes=campaign_data.min_join_minutes,
        target_user_states=campaign_data.target_user_states,
        target_limit=campaign_data.target_limit,
        min_account_age_minutes=campaign_data.min_account_age_minutes,
        coupon_provider=campaign_data.coupon_provider,
        coupon_amount=campaign_data.coupon_amount,
        coupon_quantity=campaign_data.coupon_quantity,
        coupon_type=campaign_data.coupon_type,
        coupon_batch_key=campaign_data.coupon_batch_key,
        sub2api_group_id=campaign_data.sub2api_group_id,
        sub2api_validity_days=campaign_data.sub2api_validity_days,
    )

    (
        target_group_ids,
        bot_account_id,
        trigger_event,
        trigger_timing,
        distribution_mode,
        reward_policy_json,
        broadcast_policy_json,
        eligibility_policy_json,
    ) = await _validate_managed_group_campaign_payload(
        db,
        scope_enum,
        campaign_data.target_group_ids,
        campaign_data.bot_account_id,
        campaign_data.trigger_event,
        campaign_data.trigger_timing,
        campaign_data.distribution_mode,
        reward_policy_input,
        broadcast_policy_input,
        eligibility_policy_input,
    )

    campaign = Campaign(
        name=campaign_data.name,
        campaign_type=type_enum,
        campaign_scope=scope_enum,
        trigger_timing=trigger_timing,
        trigger_event=trigger_event,
        validity_hours=campaign_data.validity_hours,
        target_group_ids=json.dumps(target_group_ids, ensure_ascii=False) if target_group_ids is not None else None,
        bot_account_id=bot_account_id,
        distribution_mode=distribution_mode,
        reward_policy_json=json.dumps(reward_policy_json, ensure_ascii=False)
        if reward_policy_json is not None
        else None,
        broadcast_policy_json=json.dumps(broadcast_policy_json, ensure_ascii=False)
        if broadcast_policy_json is not None
        else None,
        eligibility_policy_json=json.dumps(eligibility_policy_json, ensure_ascii=False)
        if eligibility_policy_json is not None
        else None,
        enabled=campaign_data.enabled,
    )

    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)

    return _campaign_to_response(campaign)


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> CampaignResponse:
    """Get campaign by ID."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return _campaign_to_response(campaign)


@router.put("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: int,
    campaign_data: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
) -> CampaignResponse:
    """Update campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    update_data = campaign_data.model_dump(exclude_none=True)
    import json

    if "campaign_scope" in update_data:
        update_data["campaign_scope"] = CampaignScope(update_data["campaign_scope"])
    if "campaign_type" in update_data:
        update_data["campaign_type"] = _coerce_campaign_type(update_data["campaign_type"])

    structured_policy_keys = {
        "broadcast_message",
        "delay_minutes",
        "schedule_times",
        "interval_minutes",
        "verified_only",
        "once_per_user",
        "min_join_minutes",
        "target_user_states",
        "target_limit",
        "min_account_age_minutes",
        "coupon_provider",
        "coupon_amount",
        "coupon_quantity",
        "coupon_type",
        "coupon_batch_key",
        "sub2api_group_id",
        "sub2api_validity_days",
    }
    structured_policy_values = {
        key: update_data.pop(key)
        for key in list(update_data)
        if key in structured_policy_keys
    }

    scope_for_validation = update_data.get("campaign_scope", campaign.campaign_scope)
    incoming_target_group_ids = update_data.get("target_group_ids")
    incoming_bot_account_id = update_data.get("bot_account_id", campaign.bot_account_id)
    incoming_trigger_event = update_data.get("trigger_event", campaign.trigger_event)
    incoming_trigger_timing = update_data.get("trigger_timing", campaign.trigger_timing)

    existing_target_group_ids = _parse_json_field(campaign.target_group_ids)
    existing_reward_policy_json = _parse_json_field(campaign.reward_policy_json)
    existing_broadcast_policy_json = _parse_json_field(campaign.broadcast_policy_json)
    existing_eligibility_policy_json = _parse_json_field(campaign.eligibility_policy_json)

    reward_policy_input, broadcast_policy_input, eligibility_policy_input = _apply_structured_policy_fields(
        reward_policy_json=update_data.get("reward_policy_json", existing_reward_policy_json),
        broadcast_policy_json=update_data.get("broadcast_policy_json", existing_broadcast_policy_json),
        eligibility_policy_json=update_data.get("eligibility_policy_json", existing_eligibility_policy_json),
        broadcast_message=structured_policy_values.get("broadcast_message"),
        delay_minutes=structured_policy_values.get("delay_minutes"),
        schedule_times=structured_policy_values.get("schedule_times"),
        interval_minutes=structured_policy_values.get("interval_minutes"),
        verified_only=structured_policy_values.get("verified_only"),
        once_per_user=structured_policy_values.get("once_per_user"),
        min_join_minutes=structured_policy_values.get("min_join_minutes"),
        target_user_states=structured_policy_values.get("target_user_states"),
        target_limit=structured_policy_values.get("target_limit"),
        min_account_age_minutes=structured_policy_values.get("min_account_age_minutes"),
        coupon_provider=structured_policy_values.get("coupon_provider"),
        coupon_amount=structured_policy_values.get("coupon_amount"),
        coupon_quantity=structured_policy_values.get("coupon_quantity"),
        coupon_type=structured_policy_values.get("coupon_type"),
        coupon_batch_key=structured_policy_values.get("coupon_batch_key"),
        sub2api_group_id=structured_policy_values.get("sub2api_group_id"),
        sub2api_validity_days=structured_policy_values.get("sub2api_validity_days"),
    )

    target_groups_for_validation = (
        incoming_target_group_ids if "target_group_ids" in update_data else existing_target_group_ids
    )
    (
        target_group_ids,
        bot_account_id,
        trigger_event,
        trigger_timing,
        distribution_mode,
        reward_policy_json,
        broadcast_policy_json,
        eligibility_policy_json,
    ) = await _validate_managed_group_campaign_payload(
        db,
        scope_for_validation,
        target_groups_for_validation,
        incoming_bot_account_id,
        incoming_trigger_event,
        incoming_trigger_timing,
        update_data.get("distribution_mode"),
        reward_policy_input,
        broadcast_policy_input,
        eligibility_policy_input,
    )
    update_data["target_group_ids"] = (
        json.dumps(target_group_ids, ensure_ascii=False) if target_group_ids is not None else None
    )
    update_data["bot_account_id"] = bot_account_id
    update_data["trigger_event"] = trigger_event
    update_data["trigger_timing"] = trigger_timing
    update_data["distribution_mode"] = distribution_mode
    update_data["reward_policy_json"] = reward_policy_json
    update_data["broadcast_policy_json"] = broadcast_policy_json
    update_data["eligibility_policy_json"] = eligibility_policy_json

    if "reward_policy_json" in update_data:
        update_data["reward_policy_json"] = (
            json.dumps(update_data["reward_policy_json"], ensure_ascii=False)
            if update_data["reward_policy_json"] is not None
            else None
        )
    if "broadcast_policy_json" in update_data:
        update_data["broadcast_policy_json"] = (
            json.dumps(update_data["broadcast_policy_json"], ensure_ascii=False)
            if update_data["broadcast_policy_json"] is not None
            else None
        )
    if "eligibility_policy_json" in update_data:
        update_data["eligibility_policy_json"] = (
            json.dumps(update_data["eligibility_policy_json"], ensure_ascii=False)
            if update_data["eligibility_policy_json"] is not None
            else None
        )

    for field, value in update_data.items():
        setattr(campaign, field, value)

    await db.commit()
    await db.refresh(campaign)

    return _campaign_to_response(campaign)


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete campaign."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    await db.delete(campaign)
    await db.commit()


@router.post("/{campaign_id}/toggle")
async def toggle_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Toggle campaign enabled status."""
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    campaign.enabled = not campaign.enabled
    await db.commit()
    await db.refresh(campaign)

    return {
        "code": 0,
        "message": "Campaign toggled",
        "data": {
            "campaign_id": campaign_id,
            "enabled": campaign.enabled,
        },
    }


# =============================================================================
# Campaign Tracking Endpoints
# =============================================================================


@router.get("/{campaign_id}/tracking", response_model=CampaignTrackingListResponse)
async def get_campaign_tracking(
    campaign_id: int,
    cursor: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> CampaignTrackingListResponse:
    """Get campaign tracking records."""
    campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    if not campaign_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Campaign not found")

    query = select(CampaignTracking).where(CampaignTracking.campaign_name != None)
    count_query = select(func.count(CampaignTracking.id))

    campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = campaign_result.scalar_one_or_none()
    if campaign:
        query = query.where(CampaignTracking.campaign_name == campaign.name)
        count_query = count_query.where(CampaignTracking.campaign_name == campaign.name)

    if cursor:
        try:
            cursor_id = int(cursor)
            query = query.where(CampaignTracking.id < cursor_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    query = query.order_by(desc(CampaignTracking.id)).limit(limit + 1)
    result = await db.execute(query)
    records = list(result.scalars().all())

    has_more = len(records) > limit
    if has_more:
        records = records[:limit]

    next_cursor = str(records[-1].id) if records and has_more else None

    return CampaignTrackingListResponse(
        data=[_tracking_to_response(record) for record in records],
        total=total,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{campaign_id}/stats", response_model=CampaignStatsResponse)
async def get_campaign_stats(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
) -> CampaignStatsResponse:
    """Get campaign statistics."""
    campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = campaign_result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    tracking_query = select(CampaignTracking).where(CampaignTracking.campaign_name == campaign.name)
    tracking_result = await db.execute(tracking_query)
    trackings = list(tracking_result.scalars().all())

    total_tracked = len(trackings)
    registered = sum(1 for tracking in trackings if tracking.registered_at is not None)
    converted = sum(1 for tracking in trackings if tracking.converted_at is not None)
    trial_granted = sum(1 for tracking in trackings if tracking.trial_granted)
    coupon_granted = sum(1 for tracking in trackings if tracking.coupon_granted)

    by_source = {}
    for tracking in trackings:
        source = tracking.source or "unknown"
        if source not in by_source:
            by_source[source] = {"total": 0, "registered": 0, "converted": 0}
        by_source[source]["total"] += 1
        if tracking.registered_at:
            by_source[source]["registered"] += 1
        if tracking.converted_at:
            by_source[source]["converted"] += 1

    return CampaignStatsResponse(
        code=0,
        message="success",
        data={
            "campaign_id": campaign_id,
            "campaign_name": campaign.name,
            "campaign_type": campaign.campaign_type.value,
            "enabled": campaign.enabled,
            "total_tracked": total_tracked,
            "registered": registered,
            "converted": converted,
            "trial_granted": trial_granted,
            "coupon_granted": coupon_granted,
            "conversion_rate": round(converted / registered * 100, 2) if registered > 0 else 0,
            "by_source": by_source,
        },
    )


# =============================================================================
# Manual Trigger Endpoints
# =============================================================================


@router.post("/{campaign_id}/trigger")
async def trigger_campaign(
    campaign_id: int,
    user_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manually trigger a campaign."""
    campaign_result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = campaign_result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if not campaign.enabled:
        raise HTTPException(status_code=400, detail="Campaign is not enabled")

    if campaign.campaign_scope == CampaignScope.MANAGED_GROUP:
        is_manual_event = campaign.trigger_event == GroupCampaignTriggerEvent.MANUAL_BROADCAST.value
        is_legacy_manual = not campaign.trigger_event and campaign.distribution_mode == CampaignDistributionMode.MANUAL
        if not is_manual_event and not is_legacy_manual:
            raise HTTPException(
                status_code=400,
                detail="Managed-group manual trigger only supports manual broadcast events",
            )

        try:
            async_result = execute_campaign_rewards.apply_async(args=[campaign_id], queue="default")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Campaign execution queue unavailable: {exc}") from exc
        return {
            "code": 0,
            "message": "Campaign trigger queued",
            "data": {
                "campaign_id": campaign_id,
                "queued": True,
                "status": "queued",
                "task_name": "execute_campaign_rewards",
                "task_id": async_result.id,
            },
        }

    if user_id is not None:
        user_result = await db.execute(select(User).where(User.id == user_id))
        if not user_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="User not found")

    try:
        if user_id is None:
            async_result = execute_campaign_rewards.apply_async(args=[campaign_id], queue="default")
        else:
            async_result = execute_campaign_rewards.apply_async(args=[campaign_id, user_id], queue="default")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Campaign execution queue unavailable: {exc}") from exc
    return {
        "code": 0,
        "message": "Campaign trigger queued",
        "data": {
            "campaign_id": campaign_id,
            "user_id": user_id,
            "queued": True,
            "status": "queued",
            "task_name": "execute_campaign_rewards",
            "task_id": async_result.id,
        },
    }


@router.post("/{campaign_id}/grant-trial")
async def grant_trial(
    campaign_id: int,
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Legacy endpoint kept for compatibility; campaigns only support coupons."""
    raise HTTPException(
        status_code=400,
        detail="Current XBoard campaign integration only supports coupon campaigns",
    )


# =============================================================================
# Statistics Endpoints
# =============================================================================


@router.get("/stats/all")
async def get_all_campaign_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get statistics for all campaigns."""
    total_result = await db.execute(select(func.count(Campaign.id)))
    total_campaigns = total_result.scalar() or 0

    enabled_result = await db.execute(select(func.count(Campaign.id)).where(Campaign.enabled == True))
    enabled_campaigns = enabled_result.scalar() or 0

    tracking_result = await db.execute(select(func.count(CampaignTracking.id)))
    total_tracking = tracking_result.scalar() or 0

    by_type = {}
    for campaign_type in CampaignType:
        count_result = await db.execute(
            select(func.count(Campaign.id)).where(Campaign.campaign_type == campaign_type)
        )
        by_type[campaign_type.value] = count_result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total_campaigns": total_campaigns,
            "enabled_campaigns": enabled_campaigns,
            "total_tracking_records": total_tracking,
            "by_type": by_type,
        },
    }
