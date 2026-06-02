"""
Custom Exceptions for Vanguard Application

Defines application-specific exceptions for better error handling.
"""


class VanguardError(Exception):
    """Base exception for all Vanguard errors."""

    def __init__(self, message: str, code: int = 1000):
        self.message = message
        self.code = code
        super().__init__(self.message)


class AccountNotFoundError(VanguardError):
    """Raised when an account is not found."""

    def __init__(self, message: str = "Account not found"):
        super().__init__(message, code=2001)


class ProxyNotFoundError(VanguardError):
    """Raised when a proxy is not found."""

    def __init__(self, message: str = "Proxy not found"):
        super().__init__(message, code=2002)


class GroupNotFoundError(VanguardError):
    """Raised when a group is not found."""

    def __init__(self, message: str = "Group not found"):
        super().__init__(message, code=3001)


class KeywordNotFoundError(VanguardError):
    """Raised when a keyword is not found."""

    def __init__(self, message: str = "Keyword not found"):
        super().__init__(message, code=4001)


class UserNotFoundError(VanguardError):
    """Raised when a user is not found."""

    def __init__(self, message: str = "User not found"):
        super().__init__(message, code=4002)


class CampaignNotFoundError(VanguardError):
    """Raised when a campaign is not found."""

    def __init__(self, message: str = "Campaign not found"):
        super().__init__(message, code=4003)


class ValidationError(VanguardError):
    """Raised when validation fails."""

    def __init__(self, message: str = "Validation failed"):
        super().__init__(message, code=1001)


class TelegramAPIError(VanguardError):
    """Raised when Telegram API call fails."""

    def __init__(self, message: str = "Telegram API error"):
        super().__init__(message, code=5001)


class RateLimitError(VanguardError):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, code=5002)


class AccountBannedError(VanguardError):
    """Raised when an account is banned."""

    def __init__(self, message: str = "Account is banned"):
        super().__init__(message, code=2003)
