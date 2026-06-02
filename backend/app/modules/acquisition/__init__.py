"""
Acquisition Module Initialization

Telegram acquisition bot functionality.

This module handles user acquisition through Telegram groups, including:
- Group search and discovery
- Automatic messaging
- Keyword trigger handling
- Private message guidance
- Registration tracking
"""

# Core imports
from app.modules.acquisition.models import (
    # Enums
    TriggerType,
    TriggerAction,
    GuideState,
    MessageType,
    AdCreativeType,
    AdSendMode,
    DeliveryStatus,
    # Search & Tracking
    GroupSearchRecord,
    AutoJoinAttempt,
    AcquisitionTracking,
    # Message & Trigger
    AcquisitionMessage,
    KeywordTrigger,
    MessageTemplate,
    TriggerRecord,
    # Conversation & Guide
    GuideFlow,
    ConversationContext,
    # Campaign
    AcquisitionCampaign,
    # Ads
    AdCreative,
    AdCampaign,
    AccountAdBinding,
    AdDeliveryLog,
)

# Config
from app.modules.acquisition.config import (
    AcquisitionConfig,
    SearchConfig,
    SpeakerConfig,
    TriggerConfig,
    GuideConfig,
    TrackingConfig,
    get_acquisition_config,
)

# Constants
from app.modules.acquisition.constants import (
    ResponseMode,
    SourceType,
    GuideStep,
    GUIDE_STEP_TIMEOUTS,
    MESSAGE_TYPE_WEIGHTS,
    INTENT_RESPONSE_MAP,
    DEFAULT_WELCOME_TEMPLATE,
    DEFAULT_GUIDE_MESSAGES,
    INTENT_KEYWORDS,
    RATE_LIMIT_KEYS,
)

# Exceptions
from app.modules.acquisition.exceptions import (
    AcquisitionError,
    SearchError,
    GroupNotFoundError,
    SearchLimitExceededError,
    RateLimitError,
    TriggerError,
    TemplateNotFoundError,
    TemplateRenderError,
    DialogError,
    ConversationNotFoundError,
    GuideFlowError,
    GuideStepTimeoutError,
    TrackingError,
    InvalidTrackingCodeError,
    TrackingCodeExpiredError,
    AccountError,
    NoAvailableAccountError,
    MessageSendError,
    PermissionDeniedError,
    ConfigurationError,
)

# Search module
from app.modules.acquisition.search import (
    GroupFinder,
    GroupFilter,
    GroupFilterCriteria,
    Searcher,
    SearchResult,
    SearchCampaign,
)

# Auto reply module
from app.modules.acquisition.auto_reply import (
    Speaker,
    ReplyEngine,
    TemplateEngine,
    MessageTemplateStore,
    SpeakScheduler,
    SpeakSchedule,
)

# Keyword trigger module
from app.modules.acquisition.keyword_trigger import (
    KeywordMatcher,
    TriggerHandler,
    TriggerResult,
    ActionExecutor,
    TriggerActionType,
)

# Private message module
from app.modules.acquisition.private_msg import (
    PrivateHandler,
    DialogManager,
    ConversationState,
    WelcomeGenerator,
    GuideFlowManager,
    GuideStep,
)

# Tracking module
from app.modules.acquisition.tracking import (
    Tracker,
    TrackingData,
    URLBuilder,
    URLBuilderConfig,
    AttributionAnalyzer,
    Attribution,
)

# Context
from app.modules.acquisition.context import ContextManager, MessageContext


__all__ = [
    # Models - Enums
    "TriggerType",
    "TriggerAction",
    "GuideState",
    "MessageType",
    "AdCreativeType",
    "AdSendMode",
    "DeliveryStatus",

    # Models - Search & Tracking
    "GroupSearchRecord",
    "AutoJoinAttempt",
    "AcquisitionTracking",

    # Models - Message & Trigger
    "AcquisitionMessage",
    "KeywordTrigger",
    "MessageTemplate",
    "TriggerRecord",

    # Models - Conversation
    "GuideFlow",
    "ConversationContext",

    # Models - Campaign
    "AcquisitionCampaign",

    # Models - Ads
    "AdCreative",
    "AdCampaign",
    "AccountAdBinding",
    "AdDeliveryLog",

    # Config
    "AcquisitionConfig",
    "SearchConfig",
    "SpeakerConfig",
    "TriggerConfig",
    "GuideConfig",
    "TrackingConfig",
    "get_acquisition_config",

    # Constants
    "ResponseMode",
    "SourceType",
    "GuideStep",
    "GUIDE_STEP_TIMEOUTS",
    "MESSAGE_TYPE_WEIGHTS",
    "INTENT_RESPONSE_MAP",
    "DEFAULT_WELCOME_TEMPLATE",
    "DEFAULT_GUIDE_MESSAGES",
    "INTENT_KEYWORDS",
    "RATE_LIMIT_KEYS",

    # Exceptions
    "AcquisitionError",
    "SearchError",
    "GroupNotFoundError",
    "SearchLimitExceededError",
    "RateLimitError",
    "TriggerError",
    "TemplateNotFoundError",
    "TemplateRenderError",
    "DialogError",
    "ConversationNotFoundError",
    "GuideFlowError",
    "GuideStepTimeoutError",
    "TrackingError",
    "InvalidTrackingCodeError",
    "TrackingCodeExpiredError",
    "AccountError",
    "NoAvailableAccountError",
    "MessageSendError",
    "PermissionDeniedError",
    "ConfigurationError",

    # Search module
    "GroupFinder",
    "GroupFilter",
    "GroupFilterCriteria",
    "Searcher",
    "SearchResult",
    "SearchCampaign",

    # Auto reply module
    "Speaker",
    "ReplyEngine",
    "TemplateEngine",
    "MessageTemplateStore",
    "SpeakScheduler",
    "SpeakSchedule",

    # Keyword trigger module
    "KeywordMatcher",
    "TriggerHandler",
    "TriggerResult",
    "ActionExecutor",
    "TriggerActionType",

    # Private message module
    "PrivateHandler",
    "DialogManager",
    "ConversationState",
    "WelcomeGenerator",
    "GuideFlowManager",

    # Tracking module
    "Tracker",
    "TrackingData",
    "URLBuilder",
    "URLBuilderConfig",
    "AttributionAnalyzer",
    "Attribution",

    # Context
    "ContextManager",
    "MessageContext",
]
