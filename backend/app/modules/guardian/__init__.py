"""
Guardian Module Initialization

Telegram guardian bot for group moderation, anti-spam, and user verification.
"""

from app.modules.guardian.models import (
    Violation,
    ViolationAction,
    ViolationLevel,
    ModerationRule,
    RuleType,
    Whitelist,
    VerificationSession,
    VerificationType,
    VerificationState,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
    ModerationSensitiveKeyword,
    SensitiveKeywordSource,
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    CouponDistribution,
)
from app.modules.guardian.campaign_runner import ManagedGroupCampaignRunner

__version__ = "1.0.0"

__all__ = [
    # Models
    "Violation",
    "ViolationAction",
    "ViolationLevel",
    "ModerationRule",
    "RuleType",
    "Whitelist",
    "VerificationSession",
    "VerificationType",
    "VerificationState",
    "GroupVerificationConfig",
    "ManagedGroupBinding",
    "ManagedGroupBindingStatus",
    "ManagedGroupBotRole",
    "ModerationSensitiveKeyword",
    "SensitiveKeywordSource",
    "GroupModerationPolicy",
    "GroupPunishmentPolicy",
    "CouponDistribution",
    "ManagedGroupCampaignRunner",
    # Config
    "config",
]
