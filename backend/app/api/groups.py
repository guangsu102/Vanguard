"""
Groups API Router

RESTful API for group pool management, account membership, and group analytics.
"""


import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.pool import get_account_pool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
    TelegramJoinRequestPendingError,
)
from app.core.campaign.models import CampaignTracking
from app.core.database import get_db
from app.core.group import Group, GroupAccountMembership, GroupLevel, GroupManager
from app.core.security import require_admin
from app.exceptions import GroupNotFoundError, ValidationError
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    JoinedGroupAuditResult,
    JOIN_AUDIT_MESSAGE_LIMIT,
)
from app.modules.acquisition.models import (
    AcquisitionMessage,
    AcquisitionTracking,
    TriggerAction,
    TriggerRecord,
)
from app.modules.acquisition.search.group_finder import extract_flood_wait_seconds

router = APIRouter()

logger = logging.getLogger(__name__)


class GroupCreate(BaseModel):
    """Group creation request."""

    group_id: int = Field(..., description="Telegram group ID")
    title: str | None = Field(None, description="Group title")
    username: str | None = Field(None, description="Group username")
    member_count: int = Field(0, ge=0, description="Member count")
    status: str = Field("active", description="Group status")
    discovery_source: str = Field("manual", description="How this group was discovered")
    source_keyword: str | None = Field(None, description="Keyword that discovered this group")
    account_id: int | None = Field(None, description="Account that joined this group")
    join_method: str = Field("manual", description="How the account joined this group")
    level: str | None = Field(None, description="Group level: A, B, C, unrated")


class GroupJoinByLinkRequest(BaseModel):
    """Join a Telegram group with a promoter account and persist the result."""

    account_id: int = Field(..., gt=0, description="Promoter account database ID")
    group_link: str = Field(..., min_length=4, max_length=512, description="Telegram group link")


class GroupUpdate(BaseModel):
    """Group update request."""

    title: str | None = None
    username: str | None = None
    member_count: int | None = Field(None, ge=0)
    status: str | None = None
    discovery_source: str | None = None
    source_keyword: str | None = None
    level: str | None = Field(None, description="Group level: A, B, C, unrated")
    rule_score: int | None = Field(None, ge=0, le=100)
    admin_score: int | None = Field(None, ge=0, le=100)
    history_score: int | None = Field(None, ge=0, le=100)
    convert_score: int | None = Field(None, ge=0, le=100)
    activity_score: int | None = Field(None, ge=0, le=100)


class GroupMetricsUpdate(BaseModel):
    """Group metrics update request for bot integrations."""

    member_count: int | None = Field(None, ge=0)
    last_message_at: str | None = None
    status: str | None = None
    rule_score: int | None = Field(None, ge=0, le=100)
    admin_score: int | None = Field(None, ge=0, le=100)
    history_score: int | None = Field(None, ge=0, le=100)
    convert_score: int | None = Field(None, ge=0, le=100)
    activity_score: int | None = Field(None, ge=0, le=100)


class GroupMembershipCreate(BaseModel):
    """Account membership creation request."""

    account_id: int = Field(..., description="Telegram account database ID")
    status: str = Field("joined", description="Membership status")
    join_method: str = Field("manual", description="Join method")
    source_keyword: str | None = Field(None, description="Source keyword")
    note: str | None = Field(None, description="Operator note")


class GroupMetricsResponse(BaseModel):
    """Aggregated business metrics for a group."""

    ads_sent: int = 0
    group_replies: int = 0
    private_messages: int = 0
    replied_users: int = 0
    registered_users: int = 0
    paid_users: int = 0
    conversion_rate: float = 0.0


class GroupMembershipResponse(BaseModel):
    """Account membership response."""

    id: int
    account_id: int
    account_phone: str | None = None
    status: str
    join_method: str
    source_keyword: str | None = None
    joined_at: str | None = None
    left_at: str | None = None
    note: str | None = None


class GroupResponse(BaseModel):
    """Group response."""

    id: int
    group_id: int
    title: str | None = None
    username: str | None = None
    status: str
    discovery_source: str
    source_keyword: str | None = None
    level: str
    level_score: float
    member_count: int
    rule_score: int
    admin_score: int
    history_score: int
    convert_score: int
    activity_score: int
    account_count: int = 0
    primary_account_phone: str | None = None
    metrics: GroupMetricsResponse = Field(default_factory=GroupMetricsResponse)
    last_message_at: str | None = None
    created_at: str
    updated_at: str


class GroupListResponse(BaseModel):
    """Group list response."""

    code: int = 0
    message: str = "success"
    data: list[GroupResponse]
    total: int


class GroupMembershipListResponse(BaseModel):
    """Group membership list response."""

    code: int = 0
    message: str = "success"
    data: list[GroupMembershipResponse]
    total: int


class GroupLevelConfigUpdate(BaseModel):
    """Level configuration update request."""

    min_score: float | None = Field(None, ge=0, le=100, description="Minimum score threshold")
    can_send_ads: bool | None = None
    can_mention_users: bool | None = None
    can_share_links: bool | None = None
    can_initiate_private: bool | None = None
    daily_message_limit: int | None = Field(None, ge=0)
    message_interval: int | None = Field(None, ge=1)
    private_message_interval: int | None = Field(None, ge=1)
    rule_weight: float | None = Field(None, ge=0, le=1)
    admin_weight: float | None = Field(None, ge=0, le=1)
    history_weight: float | None = Field(None, ge=0, le=1)
    convert_weight: float | None = Field(None, ge=0, le=1)
    activity_weight: float | None = Field(None, ge=0, le=1)
    auto_downgrade_kick_threshold: int | None = Field(None, ge=1)
    auto_downgrade_warning_threshold: int | None = Field(None, ge=1)
    auto_downgrade_success_rate_threshold: float | None = Field(None, ge=0, le=1)
    auto_upgrade_no_warning_days: int | None = Field(None, ge=1)
    auto_upgrade_high_success_days: int | None = Field(None, ge=1)
    auto_upgrade_high_convert_days: int | None = Field(None, ge=1)
    description: str | None = None


class LevelConfigResponse(BaseModel):
    """Level configuration response."""

    id: int
    level: str
    min_score: float
    can_send_ads: bool
    can_mention_users: bool
    can_share_links: bool
    can_initiate_private: bool
    daily_message_limit: int
    message_interval: int
    private_message_interval: int
    rule_weight: float
    admin_weight: float
    history_weight: float
    convert_weight: float
    activity_weight: float
    auto_downgrade_kick_threshold: int
    auto_downgrade_warning_threshold: int
    auto_downgrade_success_rate_threshold: float
    auto_upgrade_no_warning_days: int
    auto_upgrade_high_success_days: int
    auto_upgrade_high_convert_days: int
    description: str | None = None
    created_at: str
    updated_at: str


def _membership_to_response(membership: GroupAccountMembership) -> GroupMembershipResponse:
    """Convert GroupAccountMembership model to response."""
    return GroupMembershipResponse(
        id=membership.id,
        account_id=membership.account_id,
        account_phone=membership.account.phone if membership.account else None,
        status=membership.status,
        join_method=membership.join_method,
        source_keyword=membership.source_keyword,
        joined_at=membership.joined_at.isoformat() if membership.joined_at else None,
        left_at=membership.left_at.isoformat() if membership.left_at else None,
        note=membership.note,
    )


def _group_to_response(
    group,
    metrics: GroupMetricsResponse | None = None,
    account_count: int = 0,
    primary_account_phone: str | None = None,
) -> GroupResponse:
    """Convert Group model to response."""
    return GroupResponse(
        id=group.id,
        group_id=group.group_id,
        title=group.title,
        username=group.username,
        status=group.status,
        discovery_source=group.discovery_source,
        source_keyword=group.source_keyword,
        level=group.level.value,
        level_score=float(group.level_score),
        member_count=group.member_count,
        rule_score=group.rule_score,
        admin_score=group.admin_score,
        history_score=group.history_score,
        convert_score=group.convert_score,
        activity_score=group.activity_score,
        account_count=account_count,
        primary_account_phone=primary_account_phone,
        metrics=metrics or GroupMetricsResponse(),
        last_message_at=group.last_message_at.isoformat() if group.last_message_at else None,
        created_at=group.created_at.isoformat() if group.created_at else "",
        updated_at=group.updated_at.isoformat() if group.updated_at else "",
    )


def _config_to_response(config) -> LevelConfigResponse:
    """Convert GroupLevelConfig model to response."""
    return LevelConfigResponse(
        id=config.id,
        level=config.level.value,
        min_score=float(config.min_score),
        can_send_ads=config.can_send_ads,
        can_mention_users=config.can_mention_users,
        can_share_links=config.can_share_links,
        can_initiate_private=config.can_initiate_private,
        daily_message_limit=config.daily_message_limit,
        message_interval=config.message_interval,
        private_message_interval=config.private_message_interval,
        rule_weight=float(config.rule_weight),
        admin_weight=float(config.admin_weight),
        history_weight=float(config.history_weight),
        convert_weight=float(config.convert_weight),
        activity_weight=float(config.activity_weight),
        auto_downgrade_kick_threshold=config.auto_downgrade_kick_threshold,
        auto_downgrade_warning_threshold=config.auto_downgrade_warning_threshold,
        auto_downgrade_success_rate_threshold=float(config.auto_downgrade_success_rate_threshold),
        auto_upgrade_no_warning_days=config.auto_upgrade_no_warning_days,
        auto_upgrade_high_success_days=config.auto_upgrade_high_success_days,
        auto_upgrade_high_convert_days=config.auto_upgrade_high_convert_days,
        description=config.description,
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


def _parse_group_level(level: str | None) -> GroupLevel | None:
    """Parse current A/B/C levels and old numeric frontend levels."""
    if not level:
        return None

    value = str(level)
    legacy_map = {
        "1": GroupLevel.C,
        "2": GroupLevel.C,
        "3": GroupLevel.B,
        "4": GroupLevel.A,
        "5": GroupLevel.A,
    }
    if value in legacy_map:
        return legacy_map[value]

    try:
        return GroupLevel(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid level: {level}") from exc


async def _get_group_metrics(db: AsyncSession, telegram_group_ids: list[int]) -> dict[int, GroupMetricsResponse]:
    """Get group metrics for Telegram group IDs."""
    if not telegram_group_ids:
        return {}

    metrics: dict[int, GroupMetricsResponse] = {
        group_id: GroupMetricsResponse() for group_id in telegram_group_ids
    }

    message_result = await db.execute(
        select(
            AcquisitionMessage.group_id,
            func.count(AcquisitionMessage.id).label("ads_sent"),
        )
        .where(AcquisitionMessage.group_id.in_(telegram_group_ids))
        .group_by(AcquisitionMessage.group_id)
    )
    for group_id, ads_sent in message_result.all():
        metrics[group_id].ads_sent = ads_sent or 0

    trigger_result = await db.execute(
        select(
            TriggerRecord.group_id,
            func.count(
                case(
                    (
                        TriggerRecord.action_taken.in_([
                            TriggerAction.REPLY_TEMPLATE,
                            TriggerAction.REPLY_AI,
                        ]),
                        1,
                    )
                )
            ).label("group_replies"),
            func.count(
                case(
                    (
                        TriggerRecord.action_taken == TriggerAction.SEND_PRIVATE,
                        1,
                    )
                )
            ).label("private_messages"),
            func.count(distinct(TriggerRecord.user_id)).label("replied_users"),
        )
        .where(TriggerRecord.group_id.in_(telegram_group_ids))
        .group_by(TriggerRecord.group_id)
    )
    for row in trigger_result.all():
        metrics[row.group_id].group_replies = row.group_replies or 0
        metrics[row.group_id].private_messages = row.private_messages or 0
        metrics[row.group_id].replied_users = row.replied_users or 0

    acquisition_result = await db.execute(
        select(
            AcquisitionTracking.group_id,
            func.count(
                distinct(
                    case(
                        (
                            AcquisitionTracking.registered_at.isnot(None),
                            AcquisitionTracking.user_id,
                        )
                    )
                )
            ).label("registered_users"),
            func.count(
                distinct(
                    case(
                        (
                            AcquisitionTracking.converted == True,
                            AcquisitionTracking.user_id,
                        )
                    )
                )
            ).label("paid_users"),
        )
        .where(AcquisitionTracking.group_id.in_(telegram_group_ids))
        .group_by(AcquisitionTracking.group_id)
    )
    for row in acquisition_result.all():
        metrics[row.group_id].registered_users += row.registered_users or 0
        metrics[row.group_id].paid_users += row.paid_users or 0

    campaign_result = await db.execute(
        select(
            CampaignTracking.group_id,
            func.count(
                distinct(
                    case(
                        (
                            CampaignTracking.registered_at.isnot(None),
                            CampaignTracking.user_id,
                        )
                    )
                )
            ).label("registered_users"),
            func.count(
                distinct(
                    case(
                        (
                            CampaignTracking.converted_at.isnot(None),
                            CampaignTracking.user_id,
                        )
                    )
                )
            ).label("paid_users"),
        )
        .where(CampaignTracking.group_id.in_(telegram_group_ids))
        .group_by(CampaignTracking.group_id)
    )
    for row in campaign_result.all():
        metrics[row.group_id].registered_users += row.registered_users or 0
        metrics[row.group_id].paid_users += row.paid_users or 0

    for group_metrics in metrics.values():
        if group_metrics.replied_users > 0:
            group_metrics.conversion_rate = round(
                group_metrics.paid_users / group_metrics.replied_users * 100,
                2,
            )

    return metrics


async def _get_group_account_summary(
    db: AsyncSession,
    group_ids: list[int],
) -> dict[int, dict[str, object]]:
    """Get account counts and primary account phone for group database IDs."""
    if not group_ids:
        return {}

    result = await db.execute(
        select(
            GroupAccountMembership.group_id,
            func.count(GroupAccountMembership.id).label("account_count"),
            func.min(TelegramAccount.phone).label("primary_account_phone"),
        )
        .join(TelegramAccount, TelegramAccount.id == GroupAccountMembership.account_id)
        .where(
            GroupAccountMembership.group_id.in_(group_ids),
            GroupAccountMembership.status == "joined",
        )
        .group_by(GroupAccountMembership.group_id)
    )

    return {
        row.group_id: {
            "account_count": row.account_count or 0,
            "primary_account_phone": row.primary_account_phone,
        }
        for row in result.all()
    }


# ============================================================================
# Static Group Endpoints
# ============================================================================


@router.get("/stats/summary")
async def get_group_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get group statistics summary."""
    manager = GroupManager(db)
    stats = await manager.get_group_stats()

    membership_result = await db.execute(select(func.count(GroupAccountMembership.id)))
    metrics = await _get_group_metrics(db, [])

    return {
        "code": 0,
        "message": "success",
        "data": {
            **stats,
            "total_memberships": membership_result.scalar() or 0,
            "metrics": metrics,
        },
    }


@router.get("/config/levels")
async def list_level_configs(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all level configurations."""
    manager = GroupManager(db)
    await manager.ensure_default_configs()
    configs = await manager.list_level_configs()

    return {
        "code": 0,
        "message": "success",
        "data": [_config_to_response(c) for c in configs],
    }


@router.get("/config/levels/{level}")
async def get_level_config(
    level: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get configuration for a specific level."""
    try:
        level_enum = GroupLevel(level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid level: {level}") from exc

    manager = GroupManager(db)
    config = await manager.get_level_config(level_enum)

    return {
        "code": 0,
        "message": "success",
        "data": config,
    }


@router.put("/config/levels/{level}")
async def update_level_config(
    level: str,
    config_data: GroupLevelConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update configuration for a specific level."""
    try:
        level_enum = GroupLevel(level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid level: {level}") from exc

    manager = GroupManager(db)
    update_kwargs = config_data.model_dump(exclude_none=True)

    try:
        config = await manager.update_level_config(level_enum, **update_kwargs)
        return {
            "code": 0,
            "message": "success",
            "data": _config_to_response(config),
        }
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/top", response_model=GroupListResponse)
async def get_top_groups(
    limit: int = Query(10, ge=1, le=100),
    sortBy: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> GroupListResponse:
    """Get top groups for dashboards and selectors."""
    manager = GroupManager(db)
    groups = await manager.list_groups(limit=limit, offset=0)
    metrics = await _get_group_metrics(db, [g.group_id for g in groups])
    account_summary = await _get_group_account_summary(db, [g.id for g in groups])

    if sortBy == "memberCount":
        groups.sort(key=lambda g: g.member_count, reverse=True)
    elif sortBy == "activity":
        groups.sort(key=lambda g: metrics.get(g.group_id, GroupMetricsResponse()).replied_users, reverse=True)

    return GroupListResponse(
        data=[
            _group_to_response(
                group,
                metrics=metrics.get(group.group_id),
                account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
                primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
            )
            for group in groups
        ],
        total=len(groups),
    )


# ============================================================================
# Group CRUD Endpoints
# ============================================================================


@router.get("", response_model=GroupListResponse)
async def list_groups(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=200),
    pageSize: int | None = Query(None, ge=1, le=200),
    level: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    keyword: str | None = None,
    source_keyword: str | None = None,
    min_level: str | None = None,
    min_members: int | None = None,
    limit: int | None = Query(None, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> GroupListResponse:
    """Get list of groups with pagination and group-pool metrics."""
    manager = GroupManager(db)

    size = limit or pageSize or page_size or 20
    offset = 0 if limit else (page - 1) * size

    level_value = level or min_level
    level_filter = None
    if level_value:
        level_filter = _parse_group_level(level_value)

    groups = await manager.list_groups(
        level=level_filter,
        status=status_filter,
        keyword=keyword,
        source_keyword=source_keyword,
        min_members=min_members,
        limit=size,
        offset=offset,
    )

    total = await manager.count_groups(
        level=level_filter,
        status=status_filter,
        keyword=keyword,
        source_keyword=source_keyword,
        min_members=min_members,
    )

    metrics = await _get_group_metrics(db, [g.group_id for g in groups])
    account_summary = await _get_group_account_summary(db, [g.id for g in groups])

    return GroupListResponse(
        data=[
            _group_to_response(
                group,
                metrics=metrics.get(group.group_id),
                account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
                primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
            )
            for group in groups
        ],
        total=total,
    )


@router.post("/join-by-link", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def join_group_by_link(
    request: GroupJoinByLinkRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: dict = Depends(require_admin),
) -> GroupResponse:
    """Join by Telegram link, then persist the resolved group and membership."""
    account_result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == request.account_id)
    )
    account = account_result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Promoter account not found")
    if account.account_type != AccountType.PROMOTER:
        raise HTTPException(status_code=400, detail="Only promoter accounts can join ad groups")
    if not account.is_active or account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
        raise HTTPException(status_code=400, detail="Promoter account is inactive or unavailable")
    if account.risk_level in {
        AccountRiskLevel.FROZEN.value,
        AccountRiskLevel.QUARANTINED.value,
    }:
        raise HTTPException(status_code=400, detail="Promoter account is frozen or quarantined")

    account_pool = get_account_pool()
    await account_pool.add_account_from_db(account)
    wrapper = None
    try:
        wrapper = await account_pool.acquire_by_id(
            account.id,
            purpose="manual_group_link_join",
        )
        if wrapper is None:
            raise HTTPException(status_code=400, detail="Promoter account session is unavailable")
        resolved = await TelegramExecutionService(AccountRiskGuard(db)).join_group_by_link(
            wrapper,
            request.group_link,
        )
    except HTTPException:
        raise
    except TelegramJoinRequestPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TelegramExecutionError as exc:
        detail = str(exc)
        status_code = 429 if detail.startswith("risk_guard_blocked:") else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        flood_wait_seconds = extract_flood_wait_seconds(exc)
        if flood_wait_seconds is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Telegram rate limit: retry after {flood_wait_seconds} seconds",
            ) from exc
        error_name = exc.__class__.__name__
        if error_name in {"InviteHashExpiredError", "InviteHashInvalidError"}:
            detail = "Telegram invite link is invalid or expired"
        elif error_name in {"UsernameInvalidError", "UsernameNotOccupiedError"}:
            detail = "Telegram public group link does not exist"
        elif error_name == "ChannelsTooMuchError":
            detail = "This account has joined the maximum number of Telegram groups"
        else:
            detail = f"Telegram group join failed: {error_name}: {exc}"
        raise HTTPException(status_code=400, detail=detail) from exc
    finally:
        if wrapper is not None:
            await account_pool.release(wrapper)

    telegram_group_id = int(resolved["id"])
    title = str(resolved.get("title") or "").strip() or None
    username = str(resolved.get("username") or "").strip().lstrip("@") or None
    member_count = max(0, int(resolved.get("participants_count") or 0))
    now = datetime.utcnow()

    group_result = await db.execute(select(Group).where(Group.group_id == telegram_group_id))
    group = group_result.scalar_one_or_none()
    if group is None:
        group = Group(
            group_id=telegram_group_id,
            title=title,
            username=username,
            member_count=member_count,
            status="active",
            discovery_source="manual_link_join",
            level=GroupLevel.A,
        )
        db.add(group)
        await db.flush()
    else:
        if title:
            group.title = title
        if username:
            group.username = username
        if member_count:
            group.member_count = member_count
        group.status = "active"
        group.updated_at = now

    membership_result = await db.execute(
        select(GroupAccountMembership).where(
            GroupAccountMembership.group_id == group.id,
            GroupAccountMembership.account_id == account.id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        membership = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=telegram_group_id,
            account_id=account.id,
            status="joined",
            join_method="manual_link_join",
            joined_at=now,
            last_checked_at=now,
        )
        db.add(membership)
    else:
        membership.telegram_group_id = telegram_group_id
        membership.status = "joined"
        membership.join_method = "manual_link_join"
        membership.joined_at = membership.joined_at or now
        membership.left_at = None
        membership.last_checked_at = now
        membership.updated_at = now

    await db.commit()

    policy_service = AcquisitionAutomationService(db)
    policy_audit = None
    policy_wrapper = None
    try:
        policy_wrapper = await account_pool.acquire_by_id(
            account.id,
            purpose="manual_group_link_policy_audit",
        )
        if policy_wrapper is not None and policy_wrapper.client is not None:
            entity = await policy_wrapper.client.get_entity(group.username or group.group_id)
            messages = await policy_service._fetch_recent_messages(
                policy_wrapper.client,
                entity,
                limit=JOIN_AUDIT_MESSAGE_LIMIT,
            )
            policy_audit = await policy_service._audit_group_ad_rules(
                policy_wrapper.client,
                entity,
                messages,
            )
    except Exception as exc:
        logger.warning(
            "manual_group_link_policy_audit_failed",
            extra={"group_id": group.id, "error": str(exc)},
        )
    finally:
        if policy_wrapper is not None:
            await account_pool.release(policy_wrapper)

    if policy_audit is not None:
        audit = JoinedGroupAuditResult(
            passed=True,
            ad_allowed=policy_audit.ad_allowed,
            ad_rule_reason=policy_audit.reason,
            ad_rule_details=policy_audit.details(),
        )
        await policy_service._sync_group_ad_policy_from_audit(group, audit)
        if policy_audit.ad_allowed is False:
            await policy_service._apply_join_audit_ad_rule_decision(group, membership, audit)
        else:
            group.status = "active"
            await db.commit()
    await db.refresh(group)
    metrics = await _get_group_metrics(db, [group.group_id])
    account_summary = await _get_group_account_summary(db, [group.id])
    return _group_to_response(
        group,
        metrics=metrics.get(group.group_id),
        account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
        primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
    )


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    group_data: GroupCreate,
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """Create a new group or attach an account to an existing group."""
    manager = GroupManager(db)

    group = await manager.get_group_by_telegram_id(group_data.group_id)
    if group is None:
        try:
            group = await manager.create_group(
                group_id=group_data.group_id,
                title=group_data.title,
                username=group_data.username,
                member_count=group_data.member_count,
                status=group_data.status,
                discovery_source=group_data.discovery_source,
                source_keyword=group_data.source_keyword,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif group_data.account_id is None:
        raise HTTPException(status_code=400, detail=f"Group {group_data.group_id} already exists")

    create_level = _parse_group_level(group_data.level)
    if create_level is not None:
        group = await manager.adjust_level(
            group_id=group.id,
            reason="create_group",
            new_level=create_level,
        )

    if group_data.account_id is not None:
        account_result = await db.execute(
            select(TelegramAccount).where(TelegramAccount.id == group_data.account_id)
        )
        if account_result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")
        try:
            await manager.record_account_membership(
                group_id=group.id,
                account_id=group_data.account_id,
                status="joined",
                join_method=group_data.join_method,
                source_keyword=group_data.source_keyword,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    metrics = await _get_group_metrics(db, [group.group_id])
    account_summary = await _get_group_account_summary(db, [group.id])
    return _group_to_response(
        group,
        metrics=metrics.get(group.group_id),
        account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
        primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
    )


@router.get("/{group_id:int}", response_model=GroupResponse)
async def get_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """Get group by database ID."""
    manager = GroupManager(db)
    group = await manager.get_group(group_id)

    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    metrics = await _get_group_metrics(db, [group.group_id])
    account_summary = await _get_group_account_summary(db, [group.id])
    return _group_to_response(
        group,
        metrics=metrics.get(group.group_id),
        account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
        primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
    )


@router.put("/{group_id:int}", response_model=GroupResponse)
async def update_group(
    group_id: int,
    group_data: GroupUpdate,
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """Update group."""
    manager = GroupManager(db)
    group = await manager.get_group(group_id)

    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    try:
        if group_data.level:
            level = _parse_group_level(group_data.level)
            group = await manager.adjust_level(
                group_id=group_id,
                reason="manual_update",
                new_level=level,
            )

        if any([
            group_data.rule_score is not None,
            group_data.admin_score is not None,
            group_data.history_score is not None,
            group_data.convert_score is not None,
            group_data.activity_score is not None,
        ]):
            group = await manager.update_scores(
                group_id=group_id,
                rule_score=group_data.rule_score,
                admin_score=group_data.admin_score,
                history_score=group_data.history_score,
                convert_score=group_data.convert_score,
                activity_score=group_data.activity_score,
            )

        if any([
            group_data.title is not None,
            group_data.username is not None,
            group_data.member_count is not None,
            group_data.status is not None,
            group_data.discovery_source is not None,
            group_data.source_keyword is not None,
        ]):
            group = await manager.update_group(
                group_id=group_id,
                title=group_data.title,
                username=group_data.username,
                member_count=group_data.member_count,
                status=group_data.status,
                discovery_source=group_data.discovery_source,
                source_keyword=group_data.source_keyword,
            )

        metrics = await _get_group_metrics(db, [group.group_id])
        account_summary = await _get_group_account_summary(db, [group.id])
        return _group_to_response(
            group,
            metrics=metrics.get(group.group_id),
            account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
            primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
        )
    except GroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Group not found") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{group_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a group."""
    manager = GroupManager(db)

    try:
        await manager.delete_group(group_id)
    except GroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Group not found") from exc


@router.get("/{group_id:int}/operation-config")
async def get_operation_config(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get operation configuration for a group."""
    manager = GroupManager(db)
    group = await manager.get_group(group_id)

    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    config = await manager.get_operation_config(group)
    return {
        "code": 0,
        "message": "success",
        "data": {
            "group_id": group_id,
            "level": group.level.value,
            **config,
        },
    }


@router.get("/{group_id:int}/memberships", response_model=GroupMembershipListResponse)
async def list_group_memberships(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> GroupMembershipListResponse:
    """List accounts that have joined this group."""
    manager = GroupManager(db)
    group = await manager.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    memberships = await manager.list_account_memberships(group_id)
    return GroupMembershipListResponse(
        data=[_membership_to_response(membership) for membership in memberships],
        total=len(memberships),
    )


@router.get("/{group_id:int}/members", response_model=GroupMembershipListResponse)
async def list_group_members_compat(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> GroupMembershipListResponse:
    """Backward-compatible alias for account memberships."""
    return await list_group_memberships(group_id, db)


@router.post("/{group_id:int}/memberships", response_model=GroupMembershipResponse, status_code=status.HTTP_201_CREATED)
async def create_group_membership(
    group_id: int,
    request: GroupMembershipCreate,
    db: AsyncSession = Depends(get_db),
) -> GroupMembershipResponse:
    """Record an account joining this group with duplicate protection."""
    manager = GroupManager(db)

    account_result = await db.execute(
        select(TelegramAccount).where(TelegramAccount.id == request.account_id)
    )
    if account_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        membership = await manager.record_account_membership(
            group_id=group_id,
            account_id=request.account_id,
            status=request.status,
            join_method=request.join_method,
            source_keyword=request.source_keyword,
            note=request.note,
        )
        return _membership_to_response(membership)
    except GroupNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Group not found") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{group_id:int}/metrics", response_model=GroupResponse)
async def update_group_metrics(
    group_id: int,
    metrics_data: GroupMetricsUpdate,
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """Update group operational metrics and scores."""
    manager = GroupManager(db)
    group = await manager.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    if metrics_data.member_count is not None or metrics_data.status is not None:
        group = await manager.update_group(
            group_id=group_id,
            member_count=metrics_data.member_count,
            status=metrics_data.status,
        )

    if metrics_data.last_message_at:
        from datetime import datetime

        try:
            group.last_message_at = datetime.fromisoformat(metrics_data.last_message_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid last_message_at") from exc
        await db.commit()
        await db.refresh(group)

    if any([
        metrics_data.rule_score is not None,
        metrics_data.admin_score is not None,
        metrics_data.history_score is not None,
        metrics_data.convert_score is not None,
        metrics_data.activity_score is not None,
    ]):
        group = await manager.update_scores(
            group_id=group_id,
            rule_score=metrics_data.rule_score,
            admin_score=metrics_data.admin_score,
            history_score=metrics_data.history_score,
            convert_score=metrics_data.convert_score,
            activity_score=metrics_data.activity_score,
        )

    metrics = await _get_group_metrics(db, [group.group_id])
    account_summary = await _get_group_account_summary(db, [group.id])
    return _group_to_response(
        group,
        metrics=metrics.get(group.group_id),
        account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
        primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
    )


@router.post("/{group_id:int}/sync-metrics", response_model=GroupResponse)
async def sync_group_metrics(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """Recalculate group score from current aggregate metrics."""
    manager = GroupManager(db)
    group = await manager.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    metrics = (await _get_group_metrics(db, [group.group_id])).get(group.group_id, GroupMetricsResponse())
    activity_score = min(100, metrics.group_replies + metrics.private_messages + metrics.replied_users)
    convert_score = min(100, int(metrics.conversion_rate))

    group = await manager.update_scores(
        group_id=group_id,
        convert_score=convert_score,
        activity_score=activity_score,
    )

    account_summary = await _get_group_account_summary(db, [group.id])
    return _group_to_response(
        group,
        metrics=metrics,
        account_count=int(account_summary.get(group.id, {}).get("account_count", 0)),
        primary_account_phone=account_summary.get(group.id, {}).get("primary_account_phone"),
    )
