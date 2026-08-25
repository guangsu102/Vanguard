"""
Rewards Module

Provides reward calculation and distribution utilities.

Features:
- Reward eligibility checking
- Reward calculation based on rules
- Multi-reward support
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from app.core.campaign.models import Campaign, CampaignType

logger = structlog.get_logger()


@dataclass
class RewardResult:
    """
    Result of reward calculation.

    Attributes:
        success: Whether reward was successful
        reward_type: Type of reward granted
        reward_data: Reward details
        message: Optional message to send to user
    """

    success: bool
    reward_type: str
    reward_data: dict
    message: Optional[str] = None
    error: Optional[str] = None


class RewardCalculator:
    """
    Calculator for campaign rewards.

    Calculates eligible rewards based on campaign configuration
    and user eligibility rules.
    """

    def __init__(self):
        """Initialize RewardCalculator."""
        self.logger = logger.bind(module="reward_calculator")

    def calculate_reward(
        self,
        campaign: Campaign,
        user_data: dict,
    ) -> RewardResult:
        """
        Calculate reward for user based on campaign.

        Args:
            campaign: Campaign configuration
            user_data: User data for eligibility check

        Returns:
            RewardResult with reward details
        """
        if not campaign.enabled:
            return RewardResult(
                success=False,
                reward_type="none",
                reward_data={},
                error="Campaign not enabled",
            )

        if not self._check_eligibility(campaign, user_data):
            return RewardResult(
                success=False,
                reward_type="none",
                reward_data={},
                error="User not eligible",
            )

        if campaign.campaign_type == CampaignType.TRIAL:
            return self._calculate_trial_reward(campaign)
        elif campaign.campaign_type == CampaignType.GIFT_CARD:
            return self._calculate_gift_card_reward(campaign)
        elif campaign.campaign_type == CampaignType.DISCOUNT:
            return self._calculate_discount_reward(campaign)
        elif campaign.campaign_type == CampaignType.PROMO:
            return self._calculate_promo_reward(campaign)

        return RewardResult(
            success=False,
            reward_type="none",
            reward_data={},
            error="Unknown campaign type",
        )

    def _check_eligibility(
        self,
        campaign: Campaign,
        user_data: dict,
    ) -> bool:
        """
        Check if user is eligible for reward.

        Args:
            campaign: Campaign to check
            user_data: User data

        Returns:
            True if eligible
        """
        campaign_type_map = {
            CampaignType.TRIAL: "has_received_trial",
            CampaignType.GIFT_CARD: "has_received_gift_card",
            CampaignType.DISCOUNT: "has_received_discount",
            CampaignType.PROMO: "has_received_promo",
        }

        eligibility_key = campaign_type_map.get(campaign.campaign_type)
        if eligibility_key and user_data.get(eligibility_key, False):
            return False

        return True

    def _calculate_trial_reward(
        self,
        campaign: Campaign,
    ) -> RewardResult:
        """Calculate trial reward."""
        return RewardResult(
            success=True,
            reward_type="trial",
            reward_data={
                "plan_id": campaign.trial_plan_id,
                "hours": campaign.trial_hours,
                "traffic_gb": campaign.trial_traffic_gb,
                "validity_hours": campaign.validity_hours,
            },
            message=self._generate_trial_message(campaign),
        )

    def _calculate_gift_card_reward(
        self,
        campaign: Campaign,
    ) -> RewardResult:
        """Calculate gift card reward."""
        return RewardResult(
            success=True,
            reward_type="gift_card",
            reward_data={
                "template_id": campaign.gift_card_template_id,
                "validity_hours": campaign.validity_hours,
            },
            message="You received a gift card! Check your rewards.",
        )

    def _calculate_discount_reward(
        self,
        campaign: Campaign,
    ) -> RewardResult:
        """Calculate discount reward."""
        return RewardResult(
            success=True,
            reward_type="discount",
            reward_data={
                "validity_hours": campaign.validity_hours,
            },
            message="You received a discount! Use it before it expires.",
        )

    def _calculate_promo_reward(
        self,
        campaign: Campaign,
    ) -> RewardResult:
        """Calculate promo reward."""
        return RewardResult(
            success=True,
            reward_type="promo",
            reward_data={
                "campaign_name": campaign.name,
                "validity_hours": campaign.validity_hours,
            },
            message=f"Promo activated: {campaign.name}",
        )

    def _generate_trial_message(self, campaign: Campaign) -> str:
        """Generate trial reward message."""
        return (
            f"Congratulations! You've received a {campaign.trial_hours}-hour trial "
            f"with {campaign.trial_traffic_gb}GB of traffic. "
            f"Enjoy your trial!"
        )


class RewardDistributor:
    """
    Distributes rewards to users.

    Handles the actual reward distribution through various channels.
    """

    def __init__(self, xboard_integration: Optional[object] = None):
        """
        Initialize RewardDistributor.

        Args:
            xboard_integration: XBoard integration for reward distribution
        """
        self.xboard = xboard_integration
        self.logger = logger.bind(module="reward_distributor")

    async def distribute_trial(
        self,
        telegram_id: int,
        plan_id: int,
        hours: int,
        traffic_gb: int,
    ) -> bool:
        """
        Distribute trial to user.

        Args:
            telegram_id: User's Telegram ID
            plan_id: XBoard plan ID
            hours: Trial hours
            traffic_gb: Traffic in GB

        Returns:
            True if successful
        """
        if self.xboard:
            try:
                await self.xboard.set_user_trial(
                    telegram_id=telegram_id,
                    plan_id=plan_id,
                    hours=hours,
                    traffic_gb=traffic_gb,
                )
                self.logger.info(
                    "trial_distributed",
                    telegram_id=telegram_id,
                    hours=hours,
                )
                return True
            except Exception as e:
                self.logger.error(
                    "trial_distribution_failed",
                    telegram_id=telegram_id,
                    error=str(e),
                )
                return False

        return False

    async def distribute_gift_card(
        self,
        telegram_id: int,
        template_id: int,
    ) -> Optional[str]:
        """
        Distribute gift card to user.

        Args:
            telegram_id: User's Telegram ID
            template_id: Gift card template ID

        Returns:
            Gift card code or None
        """
        if self.xboard:
            try:
                code = await self.xboard.create_gift_card(
                    telegram_id=telegram_id,
                    template_id=template_id,
                )
                self.logger.info(
                    "gift_card_distributed",
                    telegram_id=telegram_id,
                    code=code,
                )
                return code
            except Exception as e:
                self.logger.error(
                    "gift_card_distribution_failed",
                    telegram_id=telegram_id,
                    error=str(e),
                )
                return None

        return None

    async def distribute_discount(
        self,
        telegram_id: int,
        discount_code: str,
    ) -> bool:
        """
        Distribute discount code to user.

        Args:
            telegram_id: User's Telegram ID
            discount_code: Discount code

        Returns:
            True if successful
        """
        self.logger.info(
            "discount_distributed",
            telegram_id=telegram_id,
            code=discount_code,
        )
        return True
