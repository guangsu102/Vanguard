"""
Acquisition Module Exceptions

Custom exceptions for the acquisition module.
"""


class AcquisitionError(Exception):
    """Base exception for acquisition module."""
    pass


class SearchError(AcquisitionError):
    """Search related errors."""
    pass


class GroupNotFoundError(SearchError):
    """Group not found."""
    pass


class SearchLimitExceededError(SearchError):
    """Search rate limit exceeded."""
    pass


class RateLimitError(AcquisitionError):
    """Rate limit exceeded."""
    pass


class TriggerError(AcquisitionError):
    """Trigger related errors."""
    pass


class TemplateNotFoundError(TriggerError):
    """Message template not found."""
    pass


class TemplateRenderError(TriggerError):
    """Template rendering error."""
    pass


class DialogError(AcquisitionError):
    """Dialog/conversation related errors."""
    pass


class ConversationNotFoundError(DialogError):
    """Conversation not found."""
    pass


class GuideFlowError(AcquisitionError):
    """Guide flow related errors."""
    pass


class GuideStepTimeoutError(GuideFlowError):
    """Guide step timeout."""
    pass


class TrackingError(AcquisitionError):
    """Tracking related errors."""
    pass


class InvalidTrackingCodeError(TrackingError):
    """Invalid or expired tracking code."""
    pass


class TrackingCodeExpiredError(TrackingError):
    """Tracking code has expired."""
    pass


class AccountError(AcquisitionError):
    """Account related errors in acquisition."""
    pass


class NoAvailableAccountError(AccountError):
    """No available account for operation."""
    pass


class MessageSendError(AcquisitionError):
    """Failed to send message."""
    pass


class PermissionDeniedError(AcquisitionError):
    """Permission denied for operation."""
    pass


class ConfigurationError(AcquisitionError):
    """Configuration related errors."""
    pass
