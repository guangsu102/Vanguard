"""
Campaign Engine Module

Manages marketing campaigns and triggers rewards.

Features:
- Campaign CRUD operations
- Campaign triggering based on events
- Reward calculation and distribution
- XBoard integration
"""

from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campaign.models import Campaign, CampaignTracking, CampaignTriggerTiming, CampaignType
from app.core.user.models import User, UserState
from app.core.exceptions import CampaignNotFoundError, ValidationError

logger = structlog.get_logger()


class CampaignEngine:
    """
    Marketing campaign engine.

    Manages campaigns and triggers rewards based on user actions.
    Integrates with XBoard for reward distribution.
    """

    def __init__(self, db: AsyncSession, xboard_integration: Optional[object] = None):
        """
        Initialize CampaignEngine.

        Args:
            db: SQLAlchemy async session
            xboard_integration: Optional XBoard integration module
        """
        self.db = db
        self.xboard = xboard_integration
        self.logger = logger.bind(module="campaign_engine")

    async def create_campaign(
        self,
        name: str,
        campaign_type: CampaignType = CampaignType.DISCOUNT,
        trigger_timing: CampaignTriggerTiming | str = CampaignTriggerTiming.AFTER_REGISTER,
        validity_hours: int = 168,
        enabled: bool = False,
    ) -> Campaign:
        """
        Create a new campaign.

        Args:
            name: Campaign name
            campaign_type: Type of campaign
            trigger_timing: When to trigger.
            validity_hours: Validity period in hours
            enabled: Whether campaign is enabled

        Returns:
            Created Campaign
        """
        if campaign_type != CampaignType.DISCOUNT:
            raise ValidationError("Current campaign integration only supports coupon campaigns")

        campaign = Campaign(
            name=name,
            campaign_type=campaign_type,
            trigger_timing=CampaignTriggerTiming(trigger_timing),
            validity_hours=validity_hours,
            enabled=enabled,
        )

        self.db.add(campaign)
        await self.db.commit()
        await self.db.refresh(campaign)

        self.logger.info(
            "campaign_created",
            campaign_id=campaign.id,
            name=name,
            campaign_type=campaign_type.value,
        )

        return campaign

    async def get_campaign(self, campaign_id: int) -> Optional[Campaign]:
        """
        Get campaign by ID.

        Args:
            campaign_id: Campaign database ID

        Returns:
            Campaign if found, None otherwise
        """
        result = await self.db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )
        return result.scalar_one_or_none()

    async def get_campaign_by_name(self, name: str) -> Optional[Campaign]:
        """
        Get campaign by name.

        Args:
            name: Campaign name

        Returns:
            Campaign if found, None otherwise
        """
        result = await self.db.execute(
            select(Campaign).where(Campaign.name == name)
        )
        return result.scalar_one_or_none()

    async def list_campaigns(
        self,
        enabled_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Campaign]:
        """
        List campaigns.

        Args:
            enabled_only: Only return enabled campaigns
            limit: Max results
            offset: Pagination offset

        Returns:
            List of campaigns
        """
        query = select(Campaign).where(Campaign.campaign_type == CampaignType.DISCOUNT)

        if enabled_only:
            query = query.where(Campaign.enabled == True)

        query = query.order_by(Campaign.created_at.desc())
        query = query.limit(limit).offset(offset)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def update_campaign(
        self,
        campaign_id: int,
        **kwargs,
    ) -> Campaign:
        """
        Update campaign.

        Args:
            campaign_id: Campaign ID
            **kwargs: Fields to update

        Returns:
            Updated Campaign

        Raises:
            CampaignNotFoundError: If not found
        """
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        allowed_fields = [
            "name", "campaign_type", "trigger_timing", "validity_hours",
            "enabled"
        ]

        for field, value in kwargs.items():
            if field == "campaign_type" and CampaignType(value) != CampaignType.DISCOUNT:
                raise ValidationError("Current campaign integration only supports coupon campaigns")
            if field in allowed_fields and hasattr(campaign, field):
                setattr(campaign, field, value)

        await self.db.commit()
        await self.db.refresh(campaign)

        self.logger.info("campaign_updated", campaign_id=campaign_id)

        return campaign

    async def delete_campaign(self, campaign_id: int) -> bool:
        """
        Delete campaign.

        Args:
            campaign_id: Campaign ID

        Returns:
            True if deleted

        Raises:
            CampaignNotFoundError: If not found
        """
        campaign = await self.get_campaign(campaign_id)
        if not campaign:
            raise CampaignNotFoundError(f"Campaign {campaign_id} not found")

        await self.db.execute(delete(Campaign).where(Campaign.id == campaign_id))
        await self.db.commit()

        self.logger.info("campaign_deleted", campaign_id=campaign_id)

        return True

    async def enable_campaign(self, campaign_id: int) -> Campaign:
        """
        Enable a campaign.

        Args:
            campaign_id: Campaign ID

        Returns:
            Updated Campaign
        """
        return await self.update_campaign(campaign_id, enabled=True)

    async def disable_campaign(self, campaign_id: int) -> Campaign:
        """
        Disable a campaign.

        Args:
            campaign_id: Campaign ID

        Returns:
            Updated Campaign
        """
        return await self.update_campaign(campaign_id, enabled=False)

    async def trigger(
        self,
        user: User,
        campaign_name: Optional[str] = None,
        tracking_data: Optional[dict] = None,
    ) -> Optional[CampaignTracking]:
        """
        Trigger campaign for user.

        Args:
            user: User to trigger for
            campaign_name: Optional specific campaign name
            tracking_data: Tracking metadata

        Returns:
            CampaignTracking record or None
        """
        campaign = None

        if campaign_name:
            campaign = await self.get_campaign_by_name(campaign_name)
        else:
            campaign = await self.get_default_campaign()

        if not campaign or not campaign.enabled:
            self.logger.debug(
                "no_campaign_found",
                user_id=user.id,
                campaign_name=campaign_name,
            )
            return None

        tracking = CampaignTracking(
            user_id=user.id,
            campaign_name=campaign.name,
            source=tracking_data.get("source") if tracking_data else None,
            group_id=tracking_data.get("group_id") if tracking_data else None,
            keyword=tracking_data.get("keyword") if tracking_data else None,
            bot_id=tracking_data.get("bot_id") if tracking_data else None,
        )

        self.db.add(tracking)
        await self.db.commit()
        await self.db.refresh(tracking)

        self.logger.info(
            "campaign_triggered",
            tracking_id=tracking.id,
            user_id=user.id,
            campaign_name=campaign.name,
        )

        await self._execute_reward(user, campaign, tracking)

        return tracking

    async def _execute_reward(
        self,
        user: User,
        campaign: Campaign,
        tracking: CampaignTracking,
    ) -> None:
        """
        Execute campaign reward.

        Args:
            user: User to reward
            campaign: Campaign configuration
            tracking: Tracking record
        """
        try:
            if campaign.campaign_type == CampaignType.DISCOUNT:
                await self._grant_discount(user, campaign, tracking)

        except Exception as e:
            self.logger.error(
                "reward_execution_failed",
                user_id=user.id,
                campaign_name=campaign.name,
                error=str(e),
            )

    async def _grant_discount(
        self,
        user: User,
        campaign: Campaign,
        tracking: CampaignTracking,
    ) -> None:
        """Mark coupon campaign as granted in Vanguard."""
        validity_started_at = datetime.utcnow()

        tracking.coupon_granted = True
        tracking.validity_started_at = validity_started_at
        await self.db.commit()

    async def is_reward_valid(
        self,
        tracking: CampaignTracking,
        campaign: Campaign,
    ) -> bool:
        """
        Check if reward is still within validity period.

        Args:
            tracking: Campaign tracking record
            campaign: Campaign configuration

        Returns:
            True if reward is still valid
        """
        if not tracking.validity_started_at:
            return False

        if campaign.campaign_type != CampaignType.DISCOUNT or not tracking.coupon_granted:
            return False

        expiry_time = tracking.validity_started_at + timedelta(hours=campaign.validity_hours)
        return datetime.utcnow() < expiry_time

    async def get_user_reward_status(
        self,
        user_id: int,
    ) -> list[dict]:
        """
        Get user's reward status for all their campaign participations.

        Args:
            user_id: User ID

        Returns:
            List of reward status dictionaries
        """
        result = await self.db.execute(
            select(CampaignTracking, Campaign)
            .join(Campaign, CampaignTracking.campaign_name == Campaign.name)
            .where(CampaignTracking.user_id == user_id)
        )
        rows = result.all()

        statuses = []
        for tracking, campaign in rows:
            is_valid = await self.is_reward_valid(tracking, campaign)

            if tracking.validity_started_at:
                expiry_time = tracking.validity_started_at + timedelta(hours=campaign.validity_hours)
                expires_at = expiry_time if is_valid else None
                remaining_hours = max(0, (expiry_time - datetime.utcnow()).total_seconds() / 3600)
            else:
                expires_at = None
                remaining_hours = 0

            statuses.append({
                "campaign_name": campaign.name,
                "campaign_type": campaign.campaign_type.value,
                "granted": tracking.trial_granted or tracking.coupon_granted,
                "is_valid": is_valid,
                "granted_at": tracking.validity_started_at.isoformat() if tracking.validity_started_at else None,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "remaining_hours": round(remaining_hours, 1),
            })

        return statuses

    async def get_default_campaign(self) -> Optional[Campaign]:
        """
        Get default campaign for registration.

        Returns:
            Default campaign or None
        """
        result = await self.db.execute(
            select(Campaign)
            .where(Campaign.enabled == True)
            .where(Campaign.trigger_timing == CampaignTriggerTiming.AFTER_REGISTER)
            .order_by(Campaign.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def record_registration(
        self,
        tracking_id: int,
        registered_at: Optional[datetime] = None,
    ) -> CampaignTracking:
        """
        Record user registration for tracking.

        Args:
            tracking_id: CampaignTracking ID
            registered_at: Registration time

        Returns:
            Updated tracking record
        """
        result = await self.db.execute(
            select(CampaignTracking).where(CampaignTracking.id == tracking_id)
        )
        tracking = result.scalar_one_or_none()

        if tracking:
            tracking.registered_at = registered_at or datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(tracking)

        return tracking

    async def record_conversion(
        self,
        tracking_id: int,
        converted_at: Optional[datetime] = None,
    ) -> CampaignTracking:
        """
        Record user conversion.

        Args:
            tracking_id: CampaignTracking ID
            converted_at: Conversion time

        Returns:
            Updated tracking record
        """
        result = await self.db.execute(
            select(CampaignTracking).where(CampaignTracking.id == tracking_id)
        )
        tracking = result.scalar_one_or_none()

        if tracking:
            tracking.converted_at = converted_at or datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(tracking)

            self.logger.info(
                "conversion_recorded",
                tracking_id=tracking_id,
            )

        return tracking

    async def get_campaign_stats(self, campaign_name: str) -> dict:
        """
        Get statistics for a campaign.

        Args:
            campaign_name: Campaign name

        Returns:
            Dictionary with statistics
        """
        result = await self.db.execute(
            select(CampaignTracking).where(
                CampaignTracking.campaign_name == campaign_name
            )
        )
        trackings = list(result.scalars().all())

        registered = sum(1 for t in trackings if t.registered_at is not None)
        converted = sum(1 for t in trackings if t.converted_at is not None)

        return {
            "campaign_name": campaign_name,
            "total_triggers": len(trackings),
            "total_registered": registered,
            "total_converted": converted,
            "conversion_rate": converted / registered if registered > 0 else 0,
            "trial_granted": sum(1 for t in trackings if t.trial_granted),
            "coupon_granted": sum(1 for t in trackings if t.coupon_granted),
        }
