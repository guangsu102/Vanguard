"""
Coupon Distributor

Distributes coupons and rewards to users.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.campaign.models import Campaign
from app.modules.guardian.models import CouponDistribution

logger = structlog.get_logger()


@dataclass
class DistributeResult:
    """Result of coupon distribution."""
    success: bool
    coupon_code: Optional[str]
    trial_hours: Optional[int]
    traffic_gb: Optional[int]
    message: str


@dataclass
class EligibilityResult:
    """Result of eligibility check."""
    eligible: bool
    reason: Optional[str]


class CouponDistributor:
    """
    Distributes coupons and rewards to users.
    
    Handles:
    - Trial distribution
    - Discount coupons
    - Gift cards
    - Reward tracking
    """
    
    def __init__(
        self,
        db: AsyncSession,
        xboard_client=None
    ):
        """
        Initialize CouponDistributor.
        
        Args:
            db: Database session
            xboard_client: Optional XBoard API client
        """
        self.db = db
        self._xboard = xboard_client
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="coupon_distributor")
    
    def set_xboard_client(self, client) -> None:
        """Set XBoard client."""
        self._xboard = client
    
    async def check_eligibility(
        self,
        user_id: int,
        campaign_id: int
    ) -> EligibilityResult:
        """
        Check if user is eligible for a campaign.
        
        Args:
            user_id: User ID
            campaign_id: Campaign ID
            
        Returns:
            EligibilityResult
        """
        campaign = await self._get_campaign(campaign_id)
        
        if not campaign:
            return EligibilityResult(eligible=False, reason="Campaign not found")
        
        if not campaign.enabled:
            return EligibilityResult(eligible=False, reason="Campaign is not active")
        
        existing = await self._get_distribution(user_id, campaign_id)
        if existing:
            return EligibilityResult(
                eligible=False,
                reason="User has already received this reward"
            )
        
        return EligibilityResult(eligible=True, reason=None)
    
    async def distribute_trial(
        self,
        user_id: int,
        campaign_id: int,
        telegram_id: Optional[int] = None
    ) -> DistributeResult:
        """
        Distribute trial to a user.
        
        Args:
            user_id: Internal user ID
            campaign_id: Campaign ID
            telegram_id: Telegram user ID
            
        Returns:
            DistributeResult
        """
        eligibility = await self.check_eligibility(user_id, campaign_id)
        
        if not eligibility.eligible:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=eligibility.reason or "Not eligible"
            )
        
        campaign = await self._get_campaign(campaign_id)
        
        coupon_code = None
        
        if self._xboard:
            try:
                coupon_code = await self._create_xboard_trial(
                    user_id=telegram_id or user_id,
                    trial_hours=campaign.trial_hours,
                    traffic_gb=campaign.trial_traffic_gb
                )
            except Exception as e:
                self.logger.error(
                    "xboard_trial_failed",
                    user_id=user_id,
                    error=str(e)
                )
                return DistributeResult(
                    success=False,
                    coupon_code=None,
                    trial_hours=None,
                    traffic_gb=None,
                    message="Failed to create trial"
                )
        
        distribution = CouponDistribution(
            user_id=user_id,
            campaign_id=campaign_id,
            distribution_type="trial",
            coupon_code=coupon_code,
            trial_hours=campaign.trial_hours,
            traffic_gb=campaign.trial_traffic_gb
        )
        
        self.db.add(distribution)
        await self.db.commit()
        await self.db.refresh(distribution)
        
        self.logger.info(
            "trial_distributed",
            user_id=user_id,
            campaign_id=campaign_id,
            coupon_code=coupon_code
        )
        
        return DistributeResult(
            success=True,
            coupon_code=coupon_code,
            trial_hours=campaign.trial_hours,
            traffic_gb=campaign.trial_traffic_gb,
            message="Trial distributed successfully"
        )
    
    async def distribute_discount(
        self,
        user_id: int,
        campaign_id: int,
        telegram_id: Optional[int] = None
    ) -> DistributeResult:
        """
        Distribute discount coupon to a user.
        
        Args:
            user_id: Internal user ID
            campaign_id: Campaign ID
            telegram_id: Telegram user ID
            
        Returns:
            DistributeResult
        """
        eligibility = await self.check_eligibility(user_id, campaign_id)
        
        if not eligibility.eligible:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=eligibility.reason or "Not eligible"
            )
        
        coupon_code = None
        
        if self._xboard:
            try:
                coupon_code = await self._create_xboard_discount(
                    user_id=telegram_id or user_id,
                    campaign_name=f"campaign_{campaign_id}"
                )
            except Exception as e:
                self.logger.error(
                    "xboard_discount_failed",
                    user_id=user_id,
                    error=str(e)
                )
        
        distribution = CouponDistribution(
            user_id=user_id,
            campaign_id=campaign_id,
            distribution_type="discount",
            coupon_code=coupon_code
        )
        
        self.db.add(distribution)
        await self.db.commit()
        await self.db.refresh(distribution)
        
        self.logger.info(
            "discount_distributed",
            user_id=user_id,
            campaign_id=campaign_id,
            coupon_code=coupon_code
        )
        
        return DistributeResult(
            success=True,
            coupon_code=coupon_code,
            trial_hours=None,
            traffic_gb=None,
            message="Discount coupon distributed"
        )
    
    async def distribute_gift(
        self,
        user_id: int,
        campaign_id: int,
        gift_card_template_id: int,
        telegram_id: Optional[int] = None
    ) -> DistributeResult:
        """
        Distribute gift card to a user.
        
        Args:
            user_id: Internal user ID
            campaign_id: Campaign ID
            gift_card_template_id: Gift card template ID
            telegram_id: Telegram user ID
            
        Returns:
            DistributeResult
        """
        eligibility = await self.check_eligibility(user_id, campaign_id)
        
        if not eligibility.eligible:
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message=eligibility.reason or "Not eligible"
            )
        
        coupon_code = None
        
        if self._xboard:
            try:
                coupon_code = await self._create_xboard_gift(
                    user_id=telegram_id or user_id,
                    template_id=gift_card_template_id
                )
            except Exception as e:
                self.logger.error(
                    "xboard_gift_failed",
                    user_id=user_id,
                    error=str(e)
                )
        
        distribution = CouponDistribution(
            user_id=user_id,
            campaign_id=campaign_id,
            distribution_type="gift",
            coupon_code=coupon_code
        )
        
        self.db.add(distribution)
        await self.db.commit()
        await self.db.refresh(distribution)
        
        self.logger.info(
            "gift_distributed",
            user_id=user_id,
            campaign_id=campaign_id,
            coupon_code=coupon_code
        )
        
        return DistributeResult(
            success=True,
            coupon_code=coupon_code,
            trial_hours=None,
            traffic_gb=None,
            message="Gift card distributed"
        )
    
    async def get_user_distributions(
        self,
        user_id: int,
        distribution_type: Optional[str] = None
    ) -> list[CouponDistribution]:
        """
        Get user's distribution history.
        
        Args:
            user_id: User ID
            distribution_type: Optional type filter
            
        Returns:
            List of distributions
        """
        query = select(CouponDistribution).where(
            CouponDistribution.user_id == user_id
        )
        
        if distribution_type:
            query = query.where(
                CouponDistribution.distribution_type == distribution_type
            )
        
        query = query.order_by(CouponDistribution.distributed_at.desc())
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def _get_campaign(self, campaign_id: int) -> Optional[Campaign]:
        """Get campaign by ID."""
        result = await self.db.execute(
            select(Campaign).where(Campaign.id == campaign_id)
        )
        return result.scalar_one_or_none()
    
    async def _get_distribution(
        self,
        user_id: int,
        campaign_id: int
    ) -> Optional[CouponDistribution]:
        """Get existing distribution."""
        result = await self.db.execute(
            select(CouponDistribution).where(
                and_(
                    CouponDistribution.user_id == user_id,
                    CouponDistribution.campaign_id == campaign_id
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def _create_xboard_trial(
        self,
        user_id: int,
        trial_hours: int,
        traffic_gb: int
    ) -> str:
        """Create trial in XBoard."""
        if not self._xboard:
            return f"TRIAL_{user_id}_{datetime.now().strftime('%Y%m%d')}"
        
        return await self._xboard.create_trial(
            user_id=user_id,
            hours=trial_hours,
            traffic_gb=traffic_gb
        )
    
    async def _create_xboard_discount(
        self,
        user_id: int,
        campaign_name: str
    ) -> str:
        """Create discount coupon in XBoard."""
        if not self._xboard:
            return f"DISCOUNT_{user_id}_{datetime.now().strftime('%Y%m%d')}"
        
        return await self._xboard.create_discount(
            user_id=user_id,
            campaign_name=campaign_name
        )
    
    async def _create_xboard_gift(
        self,
        user_id: int,
        template_id: int
    ) -> str:
        """Create gift card in XBoard."""
        if not self._xboard:
            return f"GIFT_{user_id}_{datetime.now().strftime('%Y%m%d')}"
        
        return await self._xboard.create_gift(
            user_id=user_id,
            template_id=template_id
        )
