"""
Core Module Exceptions

Re-exports exceptions from app.exceptions for backward compatibility
with imports from app.core.exceptions.
"""

from app.exceptions import (
    VanguardError,
    AccountNotFoundError,
    ProxyNotFoundError,
    GroupNotFoundError,
    KeywordNotFoundError,
    UserNotFoundError,
    CampaignNotFoundError,
    ValidationError,
    TelegramAPIError,
    RateLimitError,
    AccountBannedError,
)

__all__ = [
    "VanguardError",
    "AccountNotFoundError",
    "ProxyNotFoundError",
    "GroupNotFoundError",
    "KeywordNotFoundError",
    "UserNotFoundError",
    "CampaignNotFoundError",
    "ValidationError",
    "TelegramAPIError",
    "RateLimitError",
    "AccountBannedError",
]
