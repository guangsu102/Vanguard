"""
Campaign Module Initialization

Exports campaign-related components.
"""

from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignScope,
    CampaignTracking,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.campaign.engine import CampaignEngine
from app.core.campaign.rewards import RewardCalculator, RewardDistributor, RewardResult

__all__ = [
    "Campaign",
    "CampaignDistributionMode",
    "CampaignScope",
    "CampaignTracking",
    "CampaignTriggerTiming",
    "CampaignType",
    "CampaignEngine",
    "RewardCalculator",
    "RewardDistributor",
    "RewardResult",
]
