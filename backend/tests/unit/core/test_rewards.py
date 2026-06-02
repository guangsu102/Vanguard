"""
Unit Tests for Rewards Module

Tests cover:
- Reward eligibility checking
- Reward calculation for different campaign types
- Reward distribution
- Error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from app.core.campaign.rewards import RewardCalculator, RewardDistributor, RewardResult
from app.core.campaign.models import Campaign, CampaignType


class TestRewardCalculator:
    """Test RewardCalculator class."""

    @pytest.fixture
    def calculator(self):
        """Create RewardCalculator instance."""
        return RewardCalculator()

    @pytest.fixture
    def mock_campaign(self):
        """Create mock campaign."""
        campaign = MagicMock(spec=Campaign)
        campaign.enabled = True
        campaign.campaign_type = CampaignType.TRIAL
        campaign.trial_plan_id = 1
        campaign.trial_hours = 24
        campaign.trial_traffic_gb = 100
        campaign.validity_hours = 168
        campaign.gift_card_template_id = 10
        return campaign

    def test_init(self, calculator):
        """Test calculator initialization."""
        assert calculator.logger is not None

    def test_calculate_reward_disabled_campaign(self, calculator, mock_campaign):
        """Test reward calculation for disabled campaign."""
        mock_campaign.enabled = False

        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is False
        assert result.reward_type == "none"
        assert result.error == "Campaign not enabled"

    def test_calculate_reward_user_not_eligible(self, calculator, mock_campaign):
        """Test reward calculation when user not eligible."""
        user_data = {"has_received_trial": True}

        result = calculator.calculate_reward(mock_campaign, user_data)

        assert result.success is False
        assert result.reward_type == "none"
        assert result.error == "User not eligible"

    def test_calculate_trial_reward(self, calculator, mock_campaign):
        """Test trial reward calculation."""
        mock_campaign.campaign_type = CampaignType.TRIAL

        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is True
        assert result.reward_type == "trial"
        assert result.reward_data["plan_id"] == 1
        assert result.reward_data["hours"] == 24
        assert result.reward_data["traffic_gb"] == 100
        assert result.message is not None

    def test_calculate_gift_card_reward(self, calculator, mock_campaign):
        """Test gift card reward calculation."""
        mock_campaign.campaign_type = CampaignType.GIFT_CARD

        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is True
        assert result.reward_type == "gift_card"
        assert result.reward_data["template_id"] == 10
        assert result.reward_data["validity_hours"] == 168

    def test_calculate_discount_reward(self, calculator, mock_campaign):
        """Test discount reward calculation."""
        mock_campaign.campaign_type = CampaignType.DISCOUNT

        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is True
        assert result.reward_type == "discount"
        assert result.reward_data["validity_hours"] == 168

    def test_calculate_promo_reward(self, calculator, mock_campaign):
        """Test promo reward calculation."""
        mock_campaign.campaign_type = CampaignType.PROMO
        mock_campaign.name = "Summer Sale"

        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is True
        assert result.reward_type == "promo"
        assert result.reward_data["campaign_name"] == "Summer Sale"

    def test_calculate_reward_unknown_type(self, calculator):
        """Test reward calculation for unknown campaign type."""
        mock_campaign = MagicMock(spec=Campaign)
        mock_campaign.enabled = True
        mock_campaign.campaign_type = "unknown_type"
        mock_campaign.name = "Test"

        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is False
        assert result.reward_type == "none"
        assert result.error == "Unknown campaign type"


class TestRewardEligibility:
    """Test reward eligibility checking."""

    @pytest.fixture
    def calculator(self):
        """Create RewardCalculator instance."""
        return RewardCalculator()

    @pytest.fixture
    def mock_campaign(self):
        """Create mock campaign."""
        campaign = MagicMock(spec=Campaign)
        campaign.enabled = True
        campaign.campaign_type = CampaignType.TRIAL
        return campaign

    def test_eligible_user(self, calculator, mock_campaign):
        """Test eligible user gets reward."""
        user_data = {"has_received_trial": False}

        result = calculator.calculate_reward(mock_campaign, user_data)

        assert result.success is True

    def test_ineligible_already_received_trial(self, calculator, mock_campaign):
        """Test user who already received trial cannot get another."""
        user_data = {"has_received_trial": True}

        result = calculator.calculate_reward(mock_campaign, user_data)

        assert result.success is False
        assert result.error == "User not eligible"

    def test_eligible_empty_user_data(self, calculator, mock_campaign):
        """Test user with empty data is eligible."""
        result = calculator.calculate_reward(mock_campaign, {})

        assert result.success is True


class TestTrialMessage:
    """Test trial reward message generation."""

    @pytest.fixture
    def calculator(self):
        """Create RewardCalculator instance."""
        return RewardCalculator()

    def test_generate_trial_message(self, calculator):
        """Test trial message content."""
        campaign = MagicMock()
        campaign.trial_hours = 24
        campaign.trial_traffic_gb = 100

        message = calculator._generate_trial_message(campaign)

        assert "24" in message
        assert "100" in message
        assert "Congratulations" in message
        assert "trial" in message.lower()


class TestRewardDistributor:
    """Test RewardDistributor class."""

    @pytest.fixture
    def distributor(self):
        """Create RewardDistributor instance."""
        return RewardDistributor()

    @pytest.fixture
    def distributor_with_xboard(self):
        """Create RewardDistributor with XBoard integration."""
        xboard = MagicMock()
        xboard.set_user_trial = AsyncMock(return_value=True)
        xboard.create_gift_card = AsyncMock(return_value="TEST123")
        return RewardDistributor(xboard_integration=xboard)

    def test_init(self, distributor):
        """Test distributor initialization."""
        assert distributor.xboard is None

    def test_init_with_xboard(self, distributor_with_xboard):
        """Test distributor initialization with XBoard."""
        assert distributor_with_xboard.xboard is not None

    @pytest.mark.asyncio
    async def test_distribute_trial_no_xboard(self, distributor):
        """Test distributing trial without XBoard."""
        result = await distributor.distribute_trial(
            telegram_id=123,
            plan_id=1,
            hours=24,
            traffic_gb=100,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_distribute_trial_with_xboard(self, distributor_with_xboard):
        """Test distributing trial with XBoard."""
        result = await distributor_with_xboard.distribute_trial(
            telegram_id=123,
            plan_id=1,
            hours=24,
            traffic_gb=100,
        )

        assert result is True
        distributor_with_xboard.xboard.set_user_trial.assert_called_once_with(
            telegram_id=123,
            plan_id=1,
            hours=24,
            traffic_gb=100,
        )

    @pytest.mark.asyncio
    async def test_distribute_trial_xboard_error(self, distributor_with_xboard):
        """Test distributing trial when XBoard fails."""
        distributor_with_xboard.xboard.set_user_trial = AsyncMock(
            side_effect=Exception("Connection failed")
        )

        result = await distributor_with_xboard.distribute_trial(
            telegram_id=123,
            plan_id=1,
            hours=24,
            traffic_gb=100,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_distribute_gift_card_no_xboard(self, distributor):
        """Test distributing gift card without XBoard."""
        result = await distributor.distribute_gift_card(
            telegram_id=123,
            template_id=10,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_distribute_gift_card_with_xboard(self, distributor_with_xboard):
        """Test distributing gift card with XBoard."""
        result = await distributor_with_xboard.distribute_gift_card(
            telegram_id=123,
            template_id=10,
        )

        assert result == "TEST123"
        distributor_with_xboard.xboard.create_gift_card.assert_called_once_with(
            telegram_id=123,
            template_id=10,
        )

    @pytest.mark.asyncio
    async def test_distribute_gift_card_xboard_error(self, distributor_with_xboard):
        """Test distributing gift card when XBoard fails."""
        distributor_with_xboard.xboard.create_gift_card = AsyncMock(
            side_effect=Exception("Creation failed")
        )

        result = await distributor_with_xboard.distribute_gift_card(
            telegram_id=123,
            template_id=10,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_distribute_discount(self, distributor):
        """Test distributing discount."""
        result = await distributor.distribute_discount(
            telegram_id=123,
            discount_code="SAVE20",
        )

        assert result is True


class TestRewardResult:
    """Test RewardResult dataclass."""

    def test_successful_result(self):
        """Test creating successful result."""
        result = RewardResult(
            success=True,
            reward_type="trial",
            reward_data={"hours": 24},
            message="Congratulations!",
        )

        assert result.success is True
        assert result.reward_type == "trial"
        assert result.reward_data["hours"] == 24
        assert result.message == "Congratulations!"
        assert result.error is None

    def test_failed_result(self):
        """Test creating failed result."""
        result = RewardResult(
            success=False,
            reward_type="none",
            reward_data={},
            error="Campaign not enabled",
        )

        assert result.success is False
        assert result.reward_type == "none"
        assert result.error == "Campaign not enabled"

    def test_result_default_values(self):
        """Test RewardResult default values."""
        result = RewardResult(
            success=True,
            reward_type="trial",
            reward_data={},
        )

        assert result.message is None
        assert result.error is None
