"""
Stats API Router

RESTful API for statistics and reporting with multiple time granularity support.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.account.models import TelegramAccount, AccountStatus
from app.core.group.models import Group, GroupLevel
from app.core.user.models import User, UserState
from app.core.keyword.models import Keyword, KeywordType, KeywordStatus
from app.core.campaign.models import Campaign, CampaignTracking
from app.modules.acquisition.models import AcquisitionTracking
from app.modules.guardian.models import Violation


router = APIRouter()


# =============================================================================
# Enums
# =============================================================================

class TimeGranularity(str, Enum):
    """Time granularity for statistics."""
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# =============================================================================
# Helper Functions
# =============================================================================

def get_date_trunc_expression(granularity: TimeGranularity, column: str) -> str:
    """Get SQL date truncation expression based on granularity."""
    if granularity == TimeGranularity.HOURLY:
        return f"DATE_FORMAT({column}, '%Y-%m-%d %H:00:00')"
    elif granularity == TimeGranularity.DAILY:
        return f"DATE({column})"
    elif granularity == TimeGranularity.WEEKLY:
        return f"DATE(DATE_SUB({column}, INTERVAL WEEKDAY({column}) DAY))"
    elif granularity == TimeGranularity.MONTHLY:
        return f"DATE_FORMAT({column}, '%Y-%m-01')"
    return f"DATE({column})"


def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse date string to datetime."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except ValueError:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return None


# =============================================================================
# Dashboard Endpoint
# =============================================================================

@router.get("/dashboard")
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get dashboard statistics overview.
    """
    # Account stats
    total_accounts_result = await db.execute(select(func.count(TelegramAccount.id)))
    total_accounts = total_accounts_result.scalar() or 0

    online_accounts_result = await db.execute(
        select(func.count(TelegramAccount.id))
        .where(TelegramAccount.status.in_([AccountStatus.ONLINE, AccountStatus.WORKING]))
    )
    online_accounts = online_accounts_result.scalar() or 0

    # Group stats
    total_groups_result = await db.execute(select(func.count(Group.id)))
    total_groups = total_groups_result.scalar() or 0

    # User stats
    total_users_result = await db.execute(select(func.count(User.id)))
    total_users = total_users_result.scalar() or 0

    active_users_result = await db.execute(
        select(func.count(User.id))
        .where(User.state == UserState.ACTIVE)
    )
    active_users = active_users_result.scalar() or 0

    # Daily registrations
    today = datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)
    daily_reg_result = await db.execute(
        select(func.count(User.id))
        .where(User.created_at >= today)
        .where(User.created_at < tomorrow)
    )
    daily_registered = daily_reg_result.scalar() or 0

    # Daily conversions (users moved to active)
    daily_converted_result = await db.execute(
        select(func.count(User.id))
        .where(User.state == UserState.ACTIVE)
        .where(User.updated_at >= today)
        .where(User.updated_at < tomorrow)
    )
    daily_converted = daily_converted_result.scalar() or 0

    # Daily messages (placeholder - would need message tracking)
    daily_messages = 0

    # Daily violations
    daily_violations_result = await db.execute(
        select(func.count(Violation.id))
        .where(Violation.created_at >= today)
        .where(Violation.created_at < tomorrow)
    )
    daily_violations = daily_violations_result.scalar() or 0

    # Conversion rate
    conversion_rate = round(daily_converted / daily_registered * 100, 2) if daily_registered > 0 else 0.0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total_accounts": total_accounts,
            "online_accounts": online_accounts,
            "total_groups": total_groups,
            "total_users": total_users,
            "active_users": active_users,
            "daily_registered": daily_registered,
            "daily_converted": daily_converted,
            "conversion_rate": conversion_rate,
            "daily_messages": daily_messages,
            "daily_violations": daily_violations,
        }
    }


# =============================================================================
# Overview Endpoint
# =============================================================================

@router.get("/overview")
async def get_stats_overview(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get statistics overview.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=7))
    end = parse_date(end_date) or datetime.utcnow()

    # Total users
    total_users_result = await db.execute(select(func.count(User.id)))
    total_users = total_users_result.scalar() or 0

    # Total groups
    total_groups_result = await db.execute(select(func.count(Group.id)))
    total_groups = total_groups_result.scalar() or 0

    # Total campaigns
    total_campaigns_result = await db.execute(select(func.count(Campaign.id)))
    total_campaigns = total_campaigns_result.scalar() or 0

    # Total keywords
    total_keywords_result = await db.execute(select(func.count(Keyword.id)))
    total_keywords = total_keywords_result.scalar() or 0

    # Today's registrations
    today = datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)
    today_registered_result = await db.execute(
        select(func.count(User.id))
        .where(User.created_at >= today)
        .where(User.created_at < tomorrow)
    )
    today_registered = today_registered_result.scalar() or 0

    # Today's active users
    today_active_result = await db.execute(
        select(func.count(User.id))
        .where(User.state == UserState.ACTIVE)
        .where(User.updated_at >= today)
        .where(User.updated_at < tomorrow)
    )
    today_active = today_active_result.scalar() or 0

    # Weekly growth
    week_ago = datetime.utcnow() - timedelta(days=7)
    weekly_new_result = await db.execute(
        select(func.count(User.id))
        .where(User.created_at >= week_ago)
    )
    weekly_growth = weekly_new_result.scalar() or 0

    # Monthly growth
    month_ago = datetime.utcnow() - timedelta(days=30)
    monthly_new_result = await db.execute(
        select(func.count(User.id))
        .where(User.created_at >= month_ago)
    )
    monthly_growth = monthly_new_result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "totalUsers": total_users,
            "totalGroups": total_groups,
            "totalCampaigns": total_campaigns,
            "totalKeywords": total_keywords,
            "todayRegistered": today_registered,
            "todayActive": today_active,
            "weeklyGrowth": weekly_growth,
            "monthlyGrowth": monthly_growth,
        }
    }


# =============================================================================
# Trend Endpoint
# =============================================================================

@router.get("/trend")
async def get_stats_trend(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get trend data for registrations, conversions, and active users.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=7))
    end = parse_date(end_date) or datetime.utcnow()

    # Generate date range
    date_list = []
    current = start.date()
    while current <= end.date():
        date_list.append(current)
        current += timedelta(days=1)

    # Get daily registrations from the Telegram acquisition attribution table.
    reg_query = (
        select(
            func.date(AcquisitionTracking.registered_at).label("date"),
            func.count(AcquisitionTracking.id).label("count")
        )
        .where(AcquisitionTracking.registered_at.isnot(None))
        .where(AcquisitionTracking.registered_at >= start)
        .where(AcquisitionTracking.registered_at <= end)
        .group_by(func.date(AcquisitionTracking.registered_at))
    )
    reg_result = await db.execute(reg_query)
    reg_dict = {row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]): row[1] for row in reg_result.all()}

    # Get daily conversions from XBoard activation/conversion callbacks.
    conv_query = (
        select(
            func.date(AcquisitionTracking.converted_at).label("date"),
            func.count(AcquisitionTracking.id).label("count")
        )
        .where(AcquisitionTracking.converted_at.isnot(None))
        .where(AcquisitionTracking.converted_at >= start)
        .where(AcquisitionTracking.converted_at <= end)
        .group_by(func.date(AcquisitionTracking.converted_at))
    )
    conv_result = await db.execute(conv_query)
    conv_dict = {row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]): row[1] for row in conv_result.all()}

    # Use converted acquisition records as the acquisition-side active cohort.
    active_query = (
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.converted == True)
    )
    active_result = await db.execute(active_query)
    total_active = active_result.scalar() or 0

    # Build trend data
    trend_data = []
    for date in date_list:
        date_key = date.isoformat()
        trend_data.append({
            "date": date_key,
            "registered": reg_dict.get(date_key, 0),
            "converted": conv_dict.get(date_key, 0),
            "active": total_active,
        })

    return {
        "code": 0,
        "message": "success",
        "data": trend_data
    }


# =============================================================================
# Funnel Endpoint
# =============================================================================

@router.get("/funnel")
async def get_stats_funnel(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get funnel data showing conversion stages.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=7))
    end = parse_date(end_date) or datetime.utcnow()

    # Cohort is based on acquisition tracking records created in the period.
    total_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
    )
    total = total_result.scalar() or 0

    clicked_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.click_at.isnot(None))
    )
    clicked_count = clicked_result.scalar() or 0

    registered_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.registered_at.isnot(None))
    )
    registered_count = registered_result.scalar() or 0

    activated_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.converted_at.isnot(None))
    )
    activated_count = activated_result.scalar() or 0

    # Build funnel stages
    funnel_data = [
        {
            "stage": "Tracking",
            "count": total,
            "rate": 100.0
        },
        {
            "stage": "Clicked",
            "count": clicked_count,
            "rate": round(clicked_count / total * 100, 2) if total > 0 else 0.0
        },
        {
            "stage": "Registered",
            "count": registered_count,
            "rate": round(registered_count / total * 100, 2) if total > 0 else 0.0
        },
        {
            "stage": "Activated",
            "count": activated_count,
            "rate": round(activated_count / total * 100, 2) if total > 0 else 0.0
        }
    ]

    return {
        "code": 0,
        "message": "success",
        "data": funnel_data
    }


# =============================================================================
# Sources Endpoint
# =============================================================================

@router.get("/sources")
async def get_stats_sources(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get source distribution data.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=7))
    end = parse_date(end_date) or datetime.utcnow()

    # Source distribution is based on acquisition attribution records, not reward campaign records.
    total_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
    )
    total = total_result.scalar() or 0

    source_query = (
        select(
            AcquisitionTracking.source_type.label("source"),
            func.count(AcquisitionTracking.id).label("count")
        )
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .group_by(AcquisitionTracking.source_type)
        .order_by(desc("count"))
        .limit(10)
    )
    source_result = await db.execute(source_query)

    sources_data = []
    tracked_total = 0
    for row in source_result.all():
        count = row[1]
        tracked_total += count
        sources_data.append({
            "source": row[0] or "unknown",
            "count": count,
            "percentage": round(count / total * 100, 2) if total > 0 else 0.0
        })

    if total > tracked_total:
        sources_data.append({
            "source": "other",
            "count": total - tracked_total,
            "percentage": round((total - tracked_total) / total * 100, 2) if total > 0 else 0.0
        })

    return {
        "code": 0,
        "message": "success",
        "data": sources_data
    }


# =============================================================================
# Time-series Statistics
# =============================================================================

@router.get("/timeseries/{metric}")
async def get_timeseries_stats(
    metric: str,
    granularity: TimeGranularity = Query(default=TimeGranularity.DAILY),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    group_id: Optional[int] = Query(None, description="Group ID for filtering"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get time-series statistics for various metrics.

    Supported metrics:
    - registrations: User registrations over time
    - conversions: User conversions over time
    - violations: Violations over time
    - keywords: Keyword triggers over time
    - messages: Message counts over time
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=7))
    end = parse_date(end_date) or datetime.utcnow()

    # Build date truncation based on granularity
    trunc_expr = get_date_trunc_expression(granularity, "created_at")

    if metric == "registrations":
        trunc_expr = get_date_trunc_expression(granularity, "registered_at")
        query = (
            select(
                text(trunc_expr).label("period"),
                func.count(AcquisitionTracking.id).label("count")
            )
            .where(AcquisitionTracking.registered_at.isnot(None))
            .where(AcquisitionTracking.registered_at >= start)
            .where(AcquisitionTracking.registered_at <= end)
            .group_by(text(trunc_expr))
            .order_by(text("period"))
        )

        result = await db.execute(query)
        data = [{"period": row[0], "count": row[1]} for row in result.all()]

    elif metric == "conversions":
        trunc_expr = get_date_trunc_expression(granularity, "converted_at")
        query = (
            select(
                text(trunc_expr).label("period"),
                func.count(AcquisitionTracking.id).label("count")
            )
            .where(AcquisitionTracking.converted_at.isnot(None))
            .where(AcquisitionTracking.converted_at >= start)
            .where(AcquisitionTracking.converted_at <= end)
            .group_by(text(trunc_expr))
            .order_by(text("period"))
        )

        result = await db.execute(query)
        data = [{"period": row[0], "count": row[1]} for row in result.all()]

    elif metric == "violations":
        query = (
            select(
                text(trunc_expr).label("period"),
                func.count(Violation.id).label("count")
            )
            .where(Violation.created_at >= start)
            .where(Violation.created_at <= end)
        )

        if group_id:
            query = query.where(Violation.group_id == group_id)

        query = query.group_by(text(trunc_expr)).order_by(text("period"))

        result = await db.execute(query)
        data = [{"period": row[0], "count": row[1]} for row in result.all()]

    elif metric == "keywords":
        query = (
            select(
                text(trunc_expr).label("period"),
                func.sum(Keyword.trigger_count).label("count")
            )
            .where(Keyword.updated_at >= start)
            .where(Keyword.updated_at <= end)
            .group_by(text(trunc_expr))
            .order_by(text("period"))
        )

        result = await db.execute(query)
        data = [{"period": row[0], "count": row[1] or 0} for row in result.all()]

    elif metric == "campaigns":
        query = (
            select(
                text(trunc_expr).label("period"),
                func.count(CampaignTracking.id).label("count")
            )
            .where(CampaignTracking.created_at >= start)
            .where(CampaignTracking.created_at <= end)
            .group_by(text(trunc_expr))
            .order_by(text("period"))
        )

        result = await db.execute(query)
        data = [{"period": row[0], "count": row[1]} for row in result.all()]

    else:
        raise HTTPException(status_code=400, detail=f"Unknown metric: {metric}")

    return {
        "code": 0,
        "message": "success",
        "data": {
            "metric": metric,
            "granularity": granularity.value,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "data": data,
        }
    }


# =============================================================================
# Conversion Funnel
# =============================================================================

@router.get("/conversion")
async def get_conversion_funnel(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get conversion funnel data.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=30))
    end = parse_date(end_date) or datetime.utcnow()

    exposed_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
    )
    exposed = exposed_result.scalar() or 0

    clicked_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.click_at.isnot(None))
    )
    clicked = clicked_result.scalar() or 0

    registered_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.registered_at.isnot(None))
    )
    registered = registered_result.scalar() or 0

    trial_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.trial_granted == True)
    )
    trial = trial_result.scalar() or 0

    converted_result = await db.execute(
        select(func.count(AcquisitionTracking.id))
        .where(AcquisitionTracking.created_at >= start)
        .where(AcquisitionTracking.created_at <= end)
        .where(AcquisitionTracking.converted_at.isnot(None))
    )
    converted = converted_result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "exposed": exposed,
            "registered": registered,
            "trial": trial,
            "converted": converted,
            "funnel_rates": {
                "click_rate": round(clicked / exposed * 100, 2) if exposed > 0 else 0.0,
                "register_rate": round(registered / exposed * 100, 2) if exposed > 0 else 0.0,
                "trial_rate": round(trial / registered * 100, 2) if registered > 0 else 0.0,
                "convert_rate": round(converted / registered * 100, 2) if registered > 0 else 0.0,
            },
            "by_stage": {
                "tracking": exposed,
                "clicked": clicked,
                "registered": registered,
                "trial_granted": trial,
                "converted": converted,
            },
        }
    }


# =============================================================================
# Keyword Statistics
# =============================================================================

@router.get("/keywords")
async def get_keyword_stats(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(default=10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get keyword statistics.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=30))
    end = parse_date(end_date) or datetime.utcnow()

    # Top keywords by trigger count
    top_query = (
        select(Keyword)
        .where(Keyword.updated_at >= start)
        .where(Keyword.updated_at <= end)
        .order_by(desc(Keyword.trigger_count))
        .limit(limit)
    )
    top_result = await db.execute(top_query)
    top_keywords = [
        {
            "id": kw.id,
            "text": kw.text,
            "type": kw.type.value,
            "trigger_count": kw.trigger_count,
        }
        for kw in top_result.scalars().all()
    ]

    # Triggers by type
    triggers_by_type = {}
    for ktype in KeywordType:
        count_result = await db.execute(
            select(func.sum(Keyword.trigger_count))
            .where(Keyword.type == ktype)
            .where(Keyword.updated_at >= start)
            .where(Keyword.updated_at <= end)
        )
        triggers_by_type[ktype.value] = count_result.scalar() or 0

    # Total keywords
    total_result = await db.execute(select(func.count(Keyword.id)))
    total_keywords = total_result.scalar() or 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "total_keywords": total_keywords,
            "top_keywords": top_keywords,
            "triggers_by_type": triggers_by_type,
        }
    }


# =============================================================================
# Violation Statistics
# =============================================================================

@router.get("/violations")
async def get_violation_stats(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    group_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get violation statistics.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=30))
    end = parse_date(end_date) or datetime.utcnow()

    # Base query
    base_query = select(Violation).where(
        Violation.created_at >= start,
        Violation.created_at <= end
    )
    if group_id:
        base_query = base_query.where(Violation.group_id == group_id)

    # Total violations
    total_result = await db.execute(select(func.count(Violation.id)))
    total_violations = total_result.scalar() or 0

    # Violations by action (via rule_type as proxy)
    violations_by_action = {
        "warn": 0,
        "mute": 0,
        "ban": 0,
        "kick": 0,
    }

    # Violations by level (via content length as proxy for now)
    violations_by_level = {
        "low": 0,
        "medium": 0,
        "high": 0,
    }

    # Top violators
    top_query = (
        select(Violation.user_id, func.count(Violation.id).label("count"))
        .where(Violation.created_at >= start)
        .where(Violation.created_at <= end)
        .group_by(Violation.user_id)
        .order_by(desc("count"))
        .limit(10)
    )
    if group_id:
        top_query = top_query.where(Violation.group_id == group_id)

    top_result = await db.execute(top_query)
    top_violators = [
        {"user_id": row[0], "violation_count": row[1]}
        for row in top_result.all()
    ]

    # Top violated groups
    group_query = (
        select(Violation.group_id, func.count(Violation.id).label("count"))
        .where(Violation.created_at >= start)
        .where(Violation.created_at <= end)
        .group_by(Violation.group_id)
        .order_by(desc("count"))
        .limit(10)
    )

    group_result = await db.execute(group_query)
    top_groups = [
        {"group_id": row[0], "violation_count": row[1]}
        for row in group_result.all()
    ]

    return {
        "code": 0,
        "message": "success",
        "data": {
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "total_violations": total_violations,
            "violations_by_action": violations_by_action,
            "violations_by_level": violations_by_level,
            "top_violators": top_violators,
            "top_groups": top_groups,
        }
    }


# =============================================================================
# Group Statistics
# =============================================================================

@router.get("/groups")
async def get_group_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get group statistics.
    """
    # Total groups
    total_result = await db.execute(select(func.count(Group.id)))
    total = total_result.scalar() or 0

    # By level
    by_level = {}
    for level in GroupLevel:
        count_result = await db.execute(
            select(func.count(Group.id)).where(Group.level == level)
        )
        by_level[level.value] = count_result.scalar() or 0

    # Average scores
    avg_score_result = await db.execute(
        select(func.avg(Group.level_score))
    )
    avg_score = round(avg_score_result.scalar() or 0, 2)

    # Top groups by score
    top_query = (
        select(Group)
        .order_by(desc(Group.level_score))
        .limit(10)
    )
    top_result = await db.execute(top_query)
    top_groups = [
        {
            "id": g.id,
            "group_id": g.group_id,
            "title": g.title,
            "level": g.level.value,
            "score": float(g.level_score),
            "member_count": g.member_count,
        }
        for g in top_result.scalars().all()
    ]

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total_groups": total,
            "by_level": by_level,
            "avg_score": avg_score,
            "top_groups": top_groups,
        }
    }


# =============================================================================
# Account Statistics
# =============================================================================

@router.get("/accounts")
async def get_account_stats(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get account statistics.
    """
    # Total accounts
    total_result = await db.execute(select(func.count(TelegramAccount.id)))
    total = total_result.scalar() or 0

    # By status
    by_status = {}
    for status in AccountStatus:
        count_result = await db.execute(
            select(func.count(TelegramAccount.id)).where(TelegramAccount.status == status)
        )
        by_status[status.value] = count_result.scalar() or 0

    # By country
    country_result = await db.execute(
        select(TelegramAccount.country_code, func.count(TelegramAccount.id))
        .group_by(TelegramAccount.country_code)
        .order_by(desc(func.count(TelegramAccount.id)))
        .limit(10)
    )
    by_country = {row[0]: row[1] for row in country_result.all()}

    # Health metrics
    error_rate = round(by_status.get("error", 0) / total * 100, 2) if total > 0 else 0
    online_rate = round(
        (by_status.get("online", 0) + by_status.get("working", 0)) / total * 100, 2
    ) if total > 0 else 0

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total_accounts": total,
            "by_status": by_status,
            "by_country": by_country,
            "error_rate": error_rate,
            "online_rate": online_rate,
        }
    }


# =============================================================================
# Campaign Statistics
# =============================================================================

@router.get("/campaigns")
async def get_campaign_stats(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get campaign statistics.
    """
    start = parse_date(start_date) or (datetime.utcnow() - timedelta(days=30))
    end = parse_date(end_date) or datetime.utcnow()

    # Total campaigns
    total_result = await db.execute(select(func.count(Campaign.id)))
    total_campaigns = total_result.scalar() or 0

    # Enabled campaigns
    enabled_result = await db.execute(
        select(func.count(Campaign.id)).where(Campaign.enabled == True)
    )
    enabled_campaigns = enabled_result.scalar() or 0

    # Total tracking records
    tracking_result = await db.execute(
        select(func.count(CampaignTracking.id))
        .where(CampaignTracking.created_at >= start)
        .where(CampaignTracking.created_at <= end)
    )
    total_tracking = tracking_result.scalar() or 0

    # By type
    by_type = {}
    for ctype in ["trial", "promo", "discount", "gift_card"]:
        count_result = await db.execute(
            select(func.count(Campaign.id)).where(Campaign.campaign_type == ctype)
        )
        by_type[ctype] = count_result.scalar() or 0

    # Campaign performance
    campaign_perf = []
    campaigns_result = await db.execute(
        select(Campaign).where(Campaign.enabled == True).limit(10)
    )
    for campaign in campaigns_result.scalars().all():
        tracking_count = await db.execute(
            select(func.count(CampaignTracking.id))
            .where(CampaignTracking.campaign_name == campaign.name)
        )
        count = tracking_count.scalar() or 0

        campaign_perf.append({
            "id": campaign.id,
            "name": campaign.name,
            "type": campaign.campaign_type.value,
            "tracking_count": count,
        })

    return {
        "code": 0,
        "message": "success",
        "data": {
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "total_campaigns": total_campaigns,
            "enabled_campaigns": enabled_campaigns,
            "total_tracking_records": total_tracking,
            "by_type": by_type,
            "campaigns": campaign_perf,
        }
    }


# Import HTTPException for error handling
from fastapi import HTTPException
