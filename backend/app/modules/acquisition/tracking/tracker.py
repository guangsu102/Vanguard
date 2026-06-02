"""
Tracker Module

User acquisition tracking and conversion attribution.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import case, desc, func, select

from app.core.user.models import User, UserState
from app.modules.acquisition.tracking.url_builder import URLBuilder
from app.modules.acquisition.tracking.attribution import AttributionAnalyzer
from app.modules.acquisition.models import AcquisitionTracking
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.exceptions import InvalidTrackingCodeError, TrackingCodeExpiredError

logger = structlog.get_logger()


@dataclass
class TrackingData:
    """Tracking data for a user."""
    tracking_code: str
    source_type: str
    campaign_name: Optional[str]
    group_id: Optional[int]
    keyword: Optional[str]
    bot_id: Optional[str]
    created_at: datetime
    click_at: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    converted: bool = False
    external_user_id: Optional[str] = None


class Tracker:
    """
    User acquisition tracker.

    Handles tracking link generation, click recording,
    and conversion attribution.
    """

    def __init__(
        self,
        db: AsyncSession,
        url_builder: Optional[URLBuilder] = None,
        config: Optional[AcquisitionConfig] = None,
    ):
        """
        Initialize Tracker.

        Args:
            db: Database session
            url_builder: Optional URL builder
            config: Optional configuration
        """
        self.db = db
        self.url_builder = url_builder or URLBuilder()
        self.config = config or AcquisitionConfig()
        self.attribution = AttributionAnalyzer(db)
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="tracker")

    async def generate_tracking_link(
        self,
        user_id: int,
        source_type: str = "tg_private",
        campaign_name: Optional[str] = None,
        group_id: Optional[int] = None,
        keyword: Optional[str] = None,
        bot_id: Optional[str] = None,
        tracking_code: Optional[str] = None,
    ) -> str:
        """
        Generate a tracking link for a user.

        Args:
            user_id: User ID
            source_type: Source type (tg_group, tg_private, search, etc.)
            campaign_name: Campaign name
            group_id: Source group ID
            keyword: Trigger keyword
            bot_id: Bot account ID
            tracking_code: Optional specific tracking code

        Returns:
            Tracking URL
        """
        # 生成追踪码
        if not tracking_code:
            tracking_code = await self._generate_tracking_code(user_id)

        local_user_id = await self._get_or_create_local_user_id(user_id)
        await self._upsert_tracking_record(
            tracking_code=tracking_code,
            user_id=local_user_id,
            source_type=source_type,
            campaign_name=campaign_name,
            group_id=group_id,
            keyword=keyword,
            bot_id=bot_id,
        )

        return await self.url_builder.build_tracking_url(
            tracking_code=tracking_code,
            source_type=source_type,
            campaign=campaign_name,
            group_id=group_id,
            keyword=keyword,
            bot_id=bot_id,
        )

    async def record_tracking_code(
        self,
        user_id: int,
        tracking_code: str,
        source_type: str = "tg_private",
        campaign_name: Optional[str] = None,
        group_id: Optional[int] = None,
        keyword: Optional[str] = None,
        bot_id: Optional[str] = None,
    ) -> str:
        """Record a tracking code for a user and return it."""
        local_user_id = await self._get_or_create_local_user_id(user_id)
        await self._upsert_tracking_record(
            tracking_code=tracking_code,
            user_id=local_user_id,
            source_type=source_type,
            campaign_name=campaign_name,
            group_id=group_id,
            keyword=keyword,
            bot_id=bot_id,
        )
        return tracking_code

    async def record_click(
        self,
        tracking_code: str,
        user_info: Optional[dict] = None,
    ) -> bool:
        """
        Record a tracking link click.

        Args:
            tracking_code: Tracking code from URL
            user_info: Optional user info from click

        Returns:
            True if recorded successfully
        """
        async with self._lock:
            # 查找追踪记录
            tracking = await self._get_tracking_by_code(tracking_code)
            if not tracking:
                self.logger.warning("tracking_code_not_found", code=tracking_code)
                return False

            # 检查过期
            if await self._is_code_expired(tracking):
                self.logger.warning("tracking_code_expired", code=tracking_code)
                raise TrackingCodeExpiredError(f"Tracking code {tracking_code} has expired")

            # 更新点击时间
            tracking.click_at = datetime.utcnow()
            await self.db.commit()

            self.logger.info(
                "click_recorded",
                code=tracking_code,
                user_id=tracking.user_id,
            )
            return True

    async def record_registration(
        self,
        tracking_code: str,
        xboard_user_id: int,
    ) -> Optional[TrackingData]:
        """
        Record user registration.

        Args:
            tracking_code: Tracking code
            xboard_user_id: XBoard user ID

        Returns:
            TrackingData with updated info or None
        """
        async with self._lock:
            tracking = await self._get_tracking_by_code(tracking_code)
            if not tracking:
                self.logger.warning("tracking_code_not_found_for_registration", code=tracking_code)
                return None

            # XBoard user id is external identity; tracking.user_id remains
            # Vanguard's local User FK.
            tracking.external_user_id = str(xboard_user_id)
            tracking.registered_at = datetime.utcnow()
            if tracking.user_id is not None:
                user = await self.db.get(User, tracking.user_id)
                if user is not None:
                    user.xboard_user_id = xboard_user_id
                    if user.state == UserState.NEW:
                        user.state = UserState.PENDING
            await self.db.commit()

            self.logger.info(
                "registration_recorded",
                code=tracking_code,
                xboard_user_id=xboard_user_id,
            )

            return self._to_tracking_data(tracking)

    async def record_conversion(
        self,
        user_id: int,
        conversion_type: str = "paid",
    ) -> bool:
        """
        Record a conversion event.

        Args:
            user_id: User ID
            conversion_type: Type of conversion

        Returns:
            True if recorded
        """
        tracking = await self._get_tracking_by_user(user_id)
        if not tracking:
            return False

        tracking.converted = True
        tracking.converted_at = datetime.utcnow()
        await self.db.commit()

        self.logger.info(
            "conversion_recorded",
            user_id=user_id,
            type=conversion_type,
        )
        return True

    async def get_tracking_data(
        self,
        tracking_code: str,
    ) -> Optional[TrackingData]:
        """
        Get tracking data by code.

        Args:
            tracking_code: Tracking code

        Returns:
            TrackingData or None
        """
        tracking = await self._get_tracking_by_code(tracking_code)
        if not tracking:
            return None
        return self._to_tracking_data(tracking)

    async def get_user_tracking(
        self,
        user_id: int,
    ) -> Optional[TrackingData]:
        """
        Get tracking data for a user.

        Args:
            user_id: User ID

        Returns:
            TrackingData or None
        """
        tracking = await self._get_tracking_by_user(user_id)
        if not tracking:
            return None
        return self._to_tracking_data(tracking)

    async def get_conversion_stats(
        self,
        campaign_name: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> dict:
        """
        Get conversion statistics.

        Args:
            campaign_name: Optional campaign filter
            start_date: Optional start date
            end_date: Optional end date

        Returns:
            Statistics dict
        """
        query = select(
            func.count(AcquisitionTracking.id).label("total"),
            func.sum(case((AcquisitionTracking.converted == True, 1), else_=0)).label("converted"),
        ).where(AcquisitionTracking.click_at.isnot(None))

        if campaign_name:
            query = query.where(AcquisitionTracking.campaign_name == campaign_name)
        if start_date:
            query = query.where(AcquisitionTracking.click_at >= start_date)
        if end_date:
            query = query.where(AcquisitionTracking.click_at <= end_date)

        result = await self.db.execute(query)
        row = result.one()

        total = row.total or 0
        converted = row.converted or 0

        return {
            "total_clicks": total,
            "total_registrations": converted,
            "conversion_rate": (converted / total * 100) if total > 0 else 0,
        }

    async def _generate_tracking_code(self, user_id: int) -> str:
        """Generate unique tracking code."""
        unique_id = str(uuid.uuid4())[:8]
        return f"acq_{user_id}_{unique_id}"

    async def _upsert_tracking_record(
        self,
        tracking_code: str,
        user_id: Optional[int],
        source_type: str,
        campaign_name: Optional[str],
        group_id: Optional[int],
        keyword: Optional[str],
        bot_id: Optional[str],
    ) -> None:
        """Create or update tracking record."""
        tracking = await self._get_tracking_by_code(tracking_code)
        if tracking:
            if user_id is not None:
                tracking.user_id = user_id
            tracking.source_type = source_type
            tracking.campaign_name = campaign_name
            tracking.group_id = group_id
            tracking.keyword = keyword
            tracking.bot_id = bot_id
            if not tracking.created_at:
                tracking.created_at = datetime.utcnow()
        else:
            tracking = AcquisitionTracking(
                tracking_code=tracking_code,
                user_id=user_id,
                source_type=source_type,
                campaign_name=campaign_name,
                group_id=group_id,
                keyword=keyword,
                bot_id=bot_id,
            )
            self.db.add(tracking)
        await self.db.commit()

    async def _get_tracking_by_code(
        self,
        tracking_code: str,
    ) -> Optional[AcquisitionTracking]:
        """Get tracking by code."""
        result = await self.db.execute(
            select(AcquisitionTracking).where(
                AcquisitionTracking.tracking_code == tracking_code
            )
        )
        return result.scalar_one_or_none()

    async def _get_tracking_by_user(
        self,
        user_id: int,
    ) -> Optional[AcquisitionTracking]:
        """Get latest tracking for user."""
        result = await self.db.execute(
            select(AcquisitionTracking)
            .where(AcquisitionTracking.user_id == user_id)
            .order_by(desc(AcquisitionTracking.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_or_create_local_user_id(self, telegram_user_id: int) -> int:
        """Resolve a Telegram user id to Vanguard's local User primary key."""
        result = await self.db.execute(select(User).where(User.telegram_id == telegram_user_id))
        user = result.scalar_one_or_none()
        if user is not None:
            return user.id

        existing = await self.db.get(User, telegram_user_id)
        if existing is not None:
            return existing.id

        user = User(telegram_id=telegram_user_id, state=UserState.NEW)
        self.db.add(user)
        await self.db.flush()
        return user.id

    async def _is_code_expired(self, tracking: AcquisitionTracking) -> bool:
        """Check if tracking code is expired."""
        expiry_days = self.config.tracking.code_expiry_days
        expiry_time = tracking.created_at + timedelta(days=expiry_days)
        return datetime.utcnow() > expiry_time

    def _to_tracking_data(self, tracking: AcquisitionTracking) -> TrackingData:
        """Convert database model to TrackingData."""
        return TrackingData(
            tracking_code=tracking.tracking_code,
            source_type=tracking.source_type or "unknown",
            campaign_name=tracking.campaign_name,
            group_id=tracking.group_id,
            keyword=tracking.keyword,
            bot_id=tracking.bot_id,
            created_at=tracking.created_at,
            click_at=tracking.click_at,
            registered_at=tracking.registered_at,
            converted=tracking.converted,
            external_user_id=tracking.external_user_id,
        )
