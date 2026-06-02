"""
Campaigns API Router

RESTful API for campaign management with cursor pagination.
"""

from datetime import datetime
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
from app.core.user.models import User
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
            return json.loads(raw)
        except Exception:
            return None

    target_group_ids = None
    if campaign.target_group_ids:
        try:
            target_group_ids = json.loads(campaign.target_group_ids)
        except Exception:
            target_group_ids = None

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
        reward_policy_json=parse_json(campaign.reward_policy_json),
        broadcast_policy_json=parse_json(campaign.broadcast_policy_json),
        eligibility_policy_json=parse_json(campaign.eligibility_policy_json),
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
    distribution_mode: CampaignDistributionMode | str | None,
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
        return (
            None,
            None,
            None,
            _coerce_trigger_timing(trigger_timing),
            _coerce_distribution_mode(distribution_mode),
            reward_policy_json,
            broadcast_policy_json,
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

    expected_distribution_mode = MANAGED_GROUP_DISTRIBUTION_MODE_BY_EVENT[trigger_event_enum]
    normalized_distribution_mode = _coerce_distribution_mode(distribution_mode, expected_distribution_mode)
    if normalized_distribution_mode and normalized_distribution_mode != expected_distribution_mode:
        raise HTTPException(
            status_code=400,
            detail=(
                f"distribution_mode must be '{expected_distribution_mode.value}' for trigger_event "
                f"'{trigger_event_enum.value}'"
            ),
        )

    normalized_reward_policy = dict(reward_policy_json or {})
    normalized_broadcast_policy = dict(broadcast_policy_json or {})
    normalized_eligibility_policy = dict(eligibility_policy_json or {})

    if trigger_event_enum in {GroupCampaignTriggerEvent.USER_JOINED, GroupCampaignTriggerEvent.VERIFICATION_PASSED}:
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("schedule_times", None)
        normalized_broadcast_policy.pop("interval_minutes", None)
    elif trigger_event_enum == GroupCampaignTriggerEvent.NEW_MEMBER_DELAY:
        delay_minutes = normalized_broadcast_policy.get("delay_minutes")
        if not isinstance(delay_minutes, int) or delay_minutes < 1 or delay_minutes > 10080:
            raise HTTPException(status_code=400, detail="broadcast_policy_json.delay_minutes must be between 1 and 10080")
        normalized_broadcast_policy.pop("schedule_times", None)
        normalized_broadcast_policy.pop("interval_minutes", None)
    elif trigger_event_enum == GroupCampaignTriggerEvent.SCHEDULED:
        schedule_times = normalized_broadcast_policy.get("schedule_times")
        if not isinstance(schedule_times, list) or not schedule_times:
            raise HTTPException(status_code=400, detail="broadcast_policy_json.schedule_times is required for scheduled campaigns")
        cleaned_times: list[str] = []
        for item in schedule_times:
            if not isinstance(item, str):
                raise HTTPException(status_code=400, detail="broadcast_policy_json.schedule_times must be an array of HH:MM strings")
            value = item.strip()
            if len(value) != 5 or value[2] != ":":
                raise HTTPException(status_code=400, detail="broadcast_policy_json.schedule_times must use HH:MM format")
            hour, minute = value.split(":", 1)
            if not hour.isdigit() or not minute.isdigit():
                raise HTTPException(status_code=400, detail="broadcast_policy_json.schedule_times must use HH:MM format")
            hour_num = int(hour)
            minute_num = int(minute)
            if hour_num < 0 or hour_num > 23 or minute_num < 0 or minute_num > 59:
                raise HTTPException(status_code=400, detail="broadcast_policy_json.schedule_times must use valid HH:MM values")
            cleaned_times.append(f"{hour_num:02d}:{minute_num:02d}")
        normalized_broadcast_policy["schedule_times"] = list(dict.fromkeys(cleaned_times))
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("interval_minutes", None)
    elif trigger_event_enum == GroupCampaignTriggerEvent.MANUAL_BROADCAST:
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("schedule_times", None)
        normalized_broadcast_policy.pop("interval_minutes", None)
    elif trigger_event_enum == GroupCampaignTriggerEvent.PERIODIC:
        interval_minutes = normalized_broadcast_policy.get("interval_minutes")
        if not isinstance(interval_minutes, int) or interval_minutes < 5 or interval_minutes > 10080:
            raise HTTPException(status_code=400, detail="broadcast_policy_json.interval_minutes must be between 5 and 10080")
        normalized_broadcast_policy.pop("delay_minutes", None)
        normalized_broadcast_policy.pop("schedule_times", None)

    return (
        [binding.telegram_group_id for binding in bindings],
        bot_account_id,
        trigger_event_enum.value,
        expected_trigger_timing,
        normalized_distribution_mode,
        normalized_reward_policy or None,
        normalized_broadcast_policy or None,
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
        campaign_data.reward_policy_json,
        campaign_data.broadcast_policy_json,
        campaign_data.eligibility_policy_json,
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

    scope_for_validation = update_data.get("campaign_scope", campaign.campaign_scope)
    incoming_target_group_ids = update_data.get("target_group_ids")
    incoming_bot_account_id = update_data.get("bot_account_id", campaign.bot_account_id)
    incoming_trigger_event = update_data.get("trigger_event", campaign.trigger_event)
    incoming_trigger_timing = update_data.get("trigger_timing", campaign.trigger_timing)
    incoming_distribution_mode = update_data.get("distribution_mode", campaign.distribution_mode)

    existing_target_group_ids = _parse_json_field(campaign.target_group_ids)
    existing_reward_policy_json = _parse_json_field(campaign.reward_policy_json)
    existing_broadcast_policy_json = _parse_json_field(campaign.broadcast_policy_json)
    existing_eligibility_policy_json = _parse_json_field(campaign.eligibility_policy_json)

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
        update_data.get("distribution_mode", incoming_distribution_mode),
        update_data.get("reward_policy_json", existing_reward_policy_json),
        update_data.get("broadcast_policy_json", existing_broadcast_policy_json),
        update_data.get("eligibility_policy_json", existing_eligibility_policy_json),
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
        update_data["reward_policy_json"] = json.dumps(update_data["reward_policy_json"], ensure_ascii=False)
    if "broadcast_policy_json" in update_data:
        update_data["broadcast_policy_json"] = json.dumps(update_data["broadcast_policy_json"], ensure_ascii=False)
    if "eligibility_policy_json" in update_data:
        update_data["eligibility_policy_json"] = json.dumps(update_data["eligibility_policy_json"], ensure_ascii=False)

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
        if campaign.trigger_event != GroupCampaignTriggerEvent.MANUAL_BROADCAST.value:
            raise HTTPException(
                status_code=400,
                detail="Managed-group manual trigger currently only supports manual_broadcast campaigns",
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

    if user_id is None:
        raise HTTPException(status_code=400, detail="user_id is required for global campaign manual trigger")

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tracking = CampaignTracking(
        user_id=user_id,
        campaign_name=campaign.name,
        registered_at=datetime.utcnow(),
    )
    db.add(tracking)
    await db.commit()
    await db.refresh(tracking)

    return {
        "code": 0,
        "message": "Campaign triggered",
        "data": {
            "campaign_id": campaign_id,
            "user_id": user_id,
            "tracking_id": tracking.id,
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
