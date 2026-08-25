"""
Attribution Module

Analyzes user attribution from multiple touchpoints.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.modules.acquisition.models import AcquisitionTracking

logger = structlog.get_logger()


class AttributionModel(str, Enum):
    """Attribution model types."""
    FIRST_TOUCH = "first_touch"
    LAST_TOUCH = "last_touch"
    MULTI_TOUCH = "multi_touch"
    LINEAR = "linear"
    TIME_DECAY = "time_decay"


@dataclass
class TouchPoint:
    """Represents a user touchpoint."""
    source: str
    channel: str
    campaign: Optional[str]
    group_id: Optional[int]
    keyword: Optional[str]
    timestamp: datetime
    is_conversion: bool = False


@dataclass
class Attribution:
    """Attribution result."""
    user_id: int
    model: AttributionModel
    winning_touchpoint: Optional[TouchPoint]
    all_touchpoints: list[TouchPoint]
    channel_weights: dict[str, float]
    calculated_at: datetime


class AttributionAnalyzer:
    """
    User attribution analyzer.

    Analyzes user journey and attributes conversions
    to touchpoints using various attribution models.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize AttributionAnalyzer.

        Args:
            db: Database session
        """
        self.db = db
        self.logger = logger.bind(module="attribution_analyzer")

    async def analyze(
        self,
        user_id: int,
        model: AttributionModel = AttributionModel.LAST_TOUCH,
    ) -> Optional[Attribution]:
        """
        Analyze user attribution.

        Args:
            user_id: User ID
            model: Attribution model to use

        Returns:
            Attribution result
        """
        self.logger.info("analyzing_attribution", user_id=user_id, model=model.value)

        # 获取用户的所有追踪记录
        touchpoints = await self._get_user_touchpoints(user_id)

        if not touchpoints:
            return None

        # 根据模型计算归因
        if model == AttributionModel.LAST_TOUCH:
            winning = self._last_touch_attribution(touchpoints)
        elif model == AttributionModel.FIRST_TOUCH:
            winning = self._first_touch_attribution(touchpoints)
        elif model == AttributionModel.MULTI_TOUCH:
            winning = self._multi_touch_attribution(touchpoints)
        else:
            winning = self._last_touch_attribution(touchpoints)

        # 计算渠道权重
        weights = self._calculate_channel_weights(touchpoints)

        return Attribution(
            user_id=user_id,
            model=model,
            winning_touchpoint=winning,
            all_touchpoints=touchpoints,
            channel_weights=weights,
            calculated_at=datetime.utcnow(),
        )

    async def get_channel_stats(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> dict:
        """
        Get conversion statistics by channel.

        Args:
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            Channel statistics
        """
        query = select(
            AcquisitionTracking.source_type,
            func.count(AcquisitionTracking.id).label("total"),
            func.sum(
                func.case(
                    (AcquisitionTracking.converted == True, 1),
                    else_=0,
                )
            ).label("converted"),
        )

        if start_date:
            query = query.where(AcquisitionTracking.created_at >= start_date)
        if end_date:
            query = query.where(AcquisitionTracking.created_at <= end_date)

        query = query.group_by(AcquisitionTracking.source_type)

        result = await self.db.execute(query)
        rows = result.all()

        stats = {}
        for row in rows:
            source = row.source_type or "unknown"
            total = row.total or 0
            converted = row.converted or 0

            stats[source] = {
                "total": total,
                "converted": converted,
                "conversion_rate": (converted / total * 100) if total > 0 else 0,
            }

        return stats

    async def get_campaign_stats(
        self,
        campaign_name: str,
    ) -> dict:
        """
        Get statistics for a specific campaign.

        Args:
            campaign_name: Campaign name

        Returns:
            Campaign statistics
        """
        result = await self.db.execute(
            select(
                func.count(AcquisitionTracking.id).label("total"),
                func.sum(
                    func.case(
                        (AcquisitionTracking.converted == True, 1),
                        else_=0,
                    )
                ).label("converted"),
                func.sum(
                    func.case(
                        (AcquisitionTracking.registered_at.isnot(None), 1),
                        else_=0,
                    )
                ).label("registered"),
            ).where(
                AcquisitionTracking.campaign_name == campaign_name
            )
        )
        row = result.one()

        return {
            "campaign": campaign_name,
            "total_clicks": row.total or 0,
            "total_registrations": row.registered or 0,
            "total_conversions": row.converted or 0,
            "click_to_register_rate": (
                (row.registered or 0) / (row.total or 1) * 100
            ),
            "register_to_pay_rate": (
                (row.converted or 0) / (row.registered or 1) * 100
            ),
        }

    async def _get_user_touchpoints(self, user_id: int) -> list[TouchPoint]:
        """Get all touchpoints for a user."""
        result = await self.db.execute(
            select(AcquisitionTracking).where(
                AcquisitionTracking.user_id == user_id
            )
        )
        records = result.scalars().all()

        touchpoints = []
        for record in records:
            touchpoint = TouchPoint(
                source=record.source_type or "unknown",
                channel=record.source_type or "unknown",
                campaign=record.campaign_name,
                group_id=record.group_id,
                keyword=record.keyword,
                timestamp=record.created_at,
                is_conversion=record.converted,
            )
            touchpoints.append(touchpoint)

        # 按时间排序
        touchpoints.sort(key=lambda x: x.timestamp)
        return touchpoints

    def _last_touch_attribution(self, touchpoints: list[TouchPoint]) -> Optional[TouchPoint]:
        """Last touch attribution model."""
        if not touchpoints:
            return None
        return touchpoints[-1]

    def _first_touch_attribution(self, touchpoints: list[TouchPoint]) -> Optional[TouchPoint]:
        """First touch attribution model."""
        if not touchpoints:
            return None
        return touchpoints[0]

    def _multi_touch_attribution(self, touchpoints: list[TouchPoint]) -> Optional[TouchPoint]:
        """Multi-touch attribution (last touch with full journey)."""
        return self._last_touch_attribution(touchpoints)

    def _calculate_channel_weights(self, touchpoints: list[TouchPoint]) -> dict[str, float]:
        """Calculate channel attribution weights."""
        if not touchpoints:
            return {}

        weights = {}
        total = len(touchpoints)

        for touchpoint in touchpoints:
            channel = touchpoint.channel
            if channel in weights:
                weights[channel] += 1
            else:
                weights[channel] = 1

        # 转换为百分比
        for channel in weights:
            weights[channel] = (weights[channel] / total) * 100

        return weights
