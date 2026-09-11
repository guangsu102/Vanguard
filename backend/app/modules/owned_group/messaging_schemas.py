"""Validated request contracts for the owned-group messaging API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.owned_group.messaging_contracts import (
    ALLOWED_OWNED_GROUP_TEMPLATE_VARIABLES,
    MessageMode,
    MessageTriggerType,
)

ContentCategory = Literal["community", "promotion"]
Mode = Literal["ai", "template", "off"]
TemplateMessageType = Literal["interaction", "share", "guide", "qa"]
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScheduledTriggerConfig(StrictSchema):
    enabled: bool = False
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    weekdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5, 6, 7])
    times: list[str] = Field(default_factory=list, max_length=12)
    jitter_seconds: int = Field(default=0, ge=0, le=900)
    content_category: ContentCategory = "community"

    @field_validator("weekdays")
    @classmethod
    def normalize_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("weekdays must contain values from 1 through 7")
        return sorted(set(value))

    @field_validator("times")
    @classmethod
    def normalize_times(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        if any(not _TIME_RE.fullmatch(item) for item in normalized):
            raise ValueError("times must use HH:MM")
        return normalized

    @model_validator(mode="after")
    def require_time_when_enabled(self) -> ScheduledTriggerConfig:
        if self.enabled and not self.times:
            raise ValueError("scheduled times are required when scheduled trigger is enabled")
        return self


class KeywordTriggerConfig(StrictSchema):
    enabled: bool = False
    trigger_ids: list[int] = Field(default_factory=list, max_length=50)
    reply_to_source: bool = True
    content_category: ContentCategory = "community"

    @field_validator("trigger_ids")
    @classmethod
    def normalize_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("trigger_ids must be positive")
        return sorted(set(value))


class ReplyTriggerConfig(StrictSchema):
    enabled: bool = False
    strategy: Literal["directed", "semantic"] = "directed"
    semantic_min_confidence: float = Field(default=0.75, ge=0.5, le=1.0)
    context_messages: int = Field(default=6, ge=1, le=20)
    content_category: ContentCategory = "community"


class ManualTriggerConfig(StrictSchema):
    enabled: bool = True
    allowed_content_categories: list[ContentCategory] = Field(
        default_factory=lambda: ["community", "promotion"], min_length=1, max_length=2
    )

    @field_validator("allowed_content_categories")
    @classmethod
    def normalize_categories(cls, value: list[str]) -> list[str]:
        order = {"community": 0, "promotion": 1}
        return sorted(set(value), key=order.__getitem__)


class TriggerConfigV1(StrictSchema):
    version: Literal[1] = 1
    scheduled: ScheduledTriggerConfig = Field(default_factory=ScheduledTriggerConfig)
    keyword: KeywordTriggerConfig = Field(default_factory=KeywordTriggerConfig)
    reply: ReplyTriggerConfig = Field(default_factory=ReplyTriggerConfig)
    manual: ManualTriggerConfig = Field(default_factory=ManualTriggerConfig)
    dedupe_window_seconds: int = Field(default=21600, ge=600, le=86400)


class PromotionConfig(StrictSchema):
    mode: Mode = "off"
    default_template_id: int | None = Field(default=None, gt=0)
    destination_url: str | None = Field(default=None, max_length=512)
    cta_text: str | None = Field(default=None, max_length=100)

    @field_validator("destination_url")
    @classmethod
    def validate_https_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError("destination_url must be an absolute HTTPS URL without credentials")
        return value

    @field_validator("cta_text")
    @classmethod
    def reject_urls_in_cta(cls, value: str | None) -> str | None:
        if value and re.search(r"https?://", value, flags=re.IGNORECASE):
            raise ValueError("cta_text must not contain a URL")
        return value

    @model_validator(mode="after")
    def require_template(self) -> PromotionConfig:
        if self.mode == MessageMode.TEMPLATE.value and self.default_template_id is None:
            raise ValueError("promotion template mode requires default_template_id")
        if self.mode != MessageMode.TEMPLATE.value and self.default_template_id is not None:
            raise ValueError("promotion default_template_id is only valid in template mode")
        return self


class PolicySettings(StrictSchema):
    mode: Mode = "off"
    default_template_id: int | None = Field(default=None, gt=0)
    trigger_config: TriggerConfigV1 = Field(default_factory=TriggerConfigV1)
    promotion_config: PromotionConfig = Field(default_factory=PromotionConfig)
    daily_limit: int = Field(default=5, ge=0, le=100)
    cooldown_seconds: int = Field(default=3600, ge=60, le=86400)
    allowed_topics: list[str] = Field(default_factory=list, max_length=20)
    require_review: bool = True
    enabled: bool = False

    @field_validator("allowed_topics")
    @classmethod
    def normalize_topics(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            topic = raw.strip()
            if not 1 <= len(topic) <= 50:
                raise ValueError("each allowed topic must contain 1 to 50 characters")
            key = topic.casefold()
            if key not in seen:
                seen.add(key)
                result.append(topic)
        return result

    @model_validator(mode="after")
    def validate_mode_combinations(self) -> PolicySettings:
        if self.mode == MessageMode.TEMPLATE.value and self.default_template_id is None:
            raise ValueError("community template mode requires default_template_id")
        if self.mode != MessageMode.TEMPLATE.value and self.default_template_id is not None:
            raise ValueError("default_template_id is only valid in community template mode")

        automatic = (
            self.trigger_config.scheduled,
            self.trigger_config.keyword,
            self.trigger_config.reply,
        )
        for item in automatic:
            if (
                item.enabled
                and item.content_category == "promotion"
                and self.promotion_config.mode == "off"
            ):
                raise ValueError("promotion triggers require promotion_config.mode")
            if item.enabled and item.content_category == "community" and self.mode == "off":
                raise ValueError("community triggers require policy mode")

        reply = self.trigger_config.reply
        if reply.enabled:
            category_mode = (
                self.mode if reply.content_category == "community" else self.promotion_config.mode
            )
            if category_mode == "template" and reply.strategy != "directed":
                raise ValueError("template reply mode only supports directed strategy")
            if reply.strategy == "semantic" and category_mode != "ai":
                raise ValueError("semantic reply strategy requires AI mode")

        needs_ai_topic = False
        scheduled = self.trigger_config.scheduled
        if scheduled.enabled:
            category_mode = (
                self.mode
                if scheduled.content_category == "community"
                else self.promotion_config.mode
            )
            needs_ai_topic = category_mode == "ai"
        if reply.enabled and reply.strategy == "semantic":
            needs_ai_topic = True
        if needs_ai_topic and not self.allowed_topics:
            raise ValueError("AI scheduled or semantic reply triggers require allowed_topics")
        return self


class PolicyCreate(PolicySettings):
    account_id: int = Field(gt=0)


class PolicyUpdate(PolicySettings):
    revision: int = Field(ge=1)
    mode: Mode
    default_template_id: int | None = Field(..., gt=0)
    trigger_config: TriggerConfigV1
    promotion_config: PromotionConfig
    daily_limit: int = Field(ge=0, le=100)
    cooldown_seconds: int = Field(ge=60, le=86400)
    allowed_topics: list[str] = Field(max_length=20)
    require_review: bool
    enabled: bool


class MessageGenerationRequest(StrictSchema):
    trigger_type: Literal["manual"] = MessageTriggerType.MANUAL.value
    content_category: ContentCategory
    topic: str | None = Field(default=None, min_length=1, max_length=100)
    instruction: str | None = Field(default=None, max_length=1000)
    template_id: int | None = Field(default=None, gt=0)
    variables: dict[str, str] = Field(default_factory=dict)

    @field_validator("variables")
    @classmethod
    def validate_variables(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - ALLOWED_OWNED_GROUP_TEMPLATE_VARIABLES
        if unknown:
            raise ValueError(f"unsupported template variables: {', '.join(sorted(unknown))}")
        return value


class MessagePreviewRequest(MessageGenerationRequest):
    pass


class ManualExecutionCreate(MessageGenerationRequest):
    reply_to_message_id: int | None = None
    scheduled_at: datetime | None = None


class ExecutionApprove(StrictSchema):
    revision: int = Field(ge=1)
    content_override: str | None = Field(default=None, min_length=1, max_length=4096)


class ExecutionReject(StrictSchema):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


class OwnedGroupTemplateWrite(StrictSchema):
    name: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=5000)
    message_type: TemplateMessageType
    template_variables: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("template name must not be blank")
        return normalized

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("template content must not be blank")
        return value

    @field_validator("template_variables")
    @classmethod
    def normalize_variables(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        unknown = set(normalized) - ALLOWED_OWNED_GROUP_TEMPLATE_VARIABLES
        if unknown:
            raise ValueError(f"unsupported template variables: {', '.join(sorted(unknown))}")
        return normalized


class MessagingSuccessEnvelope(StrictSchema):
    data: Any
    correlation_id: str


class MessagingErrorBody(StrictSchema):
    code: str
    message: str
    details: dict[str, Any]
    retryable: bool


class MessagingErrorEnvelope(StrictSchema):
    error: MessagingErrorBody
    correlation_id: str


class AccountEligibilityResponse(StrictSchema):
    account_id: int
    display_name: str
    status: str
    risk_level: str
    operation_mode: str | None
    membership_status: str | None
    last_verified_at: str | None
    eligible: bool
    blocking_reasons: list[str]
    policy_id: int | None = None
    probe_required: bool = False


class MessagingStatusResponse(StrictSchema):
    static_enabled: bool
    runtime_enabled: bool
    dry_run: bool
    can_send: bool


class MessagingRuntimeLimitsResponse(StrictSchema):
    global_group_daily_limit: int
    global_account_daily_limit: int
    min_group_cooldown_seconds: int
    dedupe_window_seconds: int
    review_ttl_hours: int
    max_attempts: int


class MessagingStatsResponse(StrictSchema):
    policy_count: int
    enabled_policy_count: int
    sent_today: int
    pending_review_count: int


class MessagingAssetResponse(StrictSchema):
    asset_id: int
    core_group_id: int | None
    telegram_chat_id: int | None
    governance_status: str
    messaging_status: MessagingStatusResponse
    runtime_limits: MessagingRuntimeLimitsResponse
    stats: MessagingStatsResponse


class EligibleAccountsEnvelope(StrictSchema):
    data: list[AccountEligibilityResponse]
    asset: MessagingAssetResponse
    correlation_id: str


class PolicyPersonaSummary(StrictSchema):
    account_id: int
    configured: bool
    name: str | None = Field(max_length=50)
    revision: int = Field(ge=0)
    applicable: bool
    effective_enabled: bool


class PolicyResponse(StrictSchema):
    policy_id: int
    owned_group_asset_id: int
    core_group_id: int
    account_id: int
    mode: str
    default_template_id: int | None
    trigger_config: dict[str, Any]
    promotion_config: dict[str, Any]
    daily_limit: int
    cooldown_seconds: int
    allowed_topics: list[str]
    require_review: bool
    enabled: bool
    revision: int
    account_display_name: str
    account_eligible: bool
    account_blocking_reasons: list[str]
    sent_today: int
    community_sent_today: int
    promotion_sent_today: int
    group_sent_today: int
    remaining_today: int
    last_sent_at: str | None
    cooldown_until: str | None
    pending_review_count: int
    created_by: int | None
    updated_by: int | None
    created_at: str | None
    updated_at: str | None
    persona: PolicyPersonaSummary


class PolicyEnvelope(StrictSchema):
    data: PolicyResponse
    correlation_id: str


class PolicyListEnvelope(StrictSchema):
    data: list[PolicyResponse]
    correlation_id: str


class MessagePreviewResponse(StrictSchema):
    content: str
    normalized_content: str
    content_hash: str
    message_purpose: str
    content_category: str
    mode_snapshot: str
    template_id: int | None
    warnings: list[str]
    would_require_review: bool
    promotion_config_snapshot: dict[str, Any] | None = None
    topic: str | None = None


class MessagePreviewEnvelope(StrictSchema):
    data: MessagePreviewResponse
    correlation_id: str


class ManualExecutionResponse(StrictSchema):
    execution_id: int
    status: str
    correlation_id: str
    scheduled_at: str | None
    created: bool


class ManualExecutionEnvelope(StrictSchema):
    data: ManualExecutionResponse
    correlation_id: str


class ExecutionTimelineResponse(StrictSchema):
    status: str
    at: str | None
    timestamp: str | None
    actor_id: int | None = None


class ExecutionGenerationPolicySnapshot(StrictSchema):
    mode: str
    require_review: bool
    allowed_topics: list[str] = Field(default_factory=list)
    default_template_id: int | None = None


class ExecutionPromptContextSummary(StrictSchema):
    keyword_requires_review: bool | None = None
    request_fingerprint: str | None = None
    scheduled_slot: str | None = None
    strategy: str | None = None
    topic: str | None = None
    generation_policy_snapshot: ExecutionGenerationPolicySnapshot | None = None
    instruction_present: bool = False
    matched_keyword_present: bool = False
    recent_context_message_count: int = 0
    source_message_present: bool = False
    user_name_present: bool = False
    variable_keys: list[str] = Field(default_factory=list)


class ExecutionPersonaSummary(StrictSchema):
    source: Literal[
        "configured",
        "neutral_default",
        "feature_disabled_default",
        "legacy_default",
        "legacy_untracked",
    ]
    name: str | None = Field(max_length=50)
    revision: int | None = Field(default=None, ge=0)
    hash_prefix: str | None = Field(pattern=r"^[0-9a-f]{12}$")


class ExecutionResponse(StrictSchema):
    id: int
    execution_id: int
    policy_id: int
    owned_group_asset_id: int
    core_group_id: int
    telegram_chat_id: int
    account_id: int
    trigger_type: str
    message_purpose: str
    content_category: str
    mode_snapshot: str
    policy_revision: int
    status: str
    source_message_id: int | None
    reply_to_message_id: int | None
    keyword_trigger_id: int | None
    template_id: int | None
    topic: str | None
    prompt_context: ExecutionPromptContextSummary | None
    promotion_config_snapshot: dict[str, Any] | None
    content: str | None
    content_summary: str | None
    content_hash: str | None
    persona: ExecutionPersonaSummary | None
    prompt_template_version: str | None = Field(max_length=32)
    prompt_hash_prefix: str | None = Field(pattern=r"^[0-9a-f]{12}$")
    governance_rules_hash_prefix: str | None = Field(pattern=r"^[0-9a-f]{12}$")
    correlation_id: str
    scheduled_at: str | None
    next_retry_at: str | None
    attempt_count: int
    revision: int
    requested_by: int | None
    reviewer_id: int | None
    reviewed_at: str | None
    review_expires_at: str | None
    telegram_message_id: int | None
    error_code: str | None
    error_message: str | None
    created_at: str | None
    updated_at: str | None
    sent_at: str | None
    timeline: list[ExecutionTimelineResponse]
    audit_event_ids: list[int]


class ExecutionEnvelope(StrictSchema):
    data: ExecutionResponse
    correlation_id: str


class ExecutionPageResponse(StrictSchema):
    total: int
    page: int
    page_size: int
    items: list[ExecutionResponse]


class ExecutionListEnvelope(StrictSchema):
    data: ExecutionPageResponse
    correlation_id: str


class OwnedGroupTemplateResponse(StrictSchema):
    id: int
    name: str
    content: str
    message_type: str
    content_category: str
    template_variables: list[str]
    enabled: bool
    created_at: str | None
    updated_at: str | None


class OwnedGroupTemplateEnvelope(StrictSchema):
    data: OwnedGroupTemplateResponse
    correlation_id: str


class OwnedGroupTemplateListEnvelope(StrictSchema):
    data: list[OwnedGroupTemplateResponse]
    correlation_id: str


MESSAGING_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": MessagingErrorEnvelope, "description": "Domain validation failed"},
    401: {"model": MessagingErrorEnvelope, "description": "Authentication required"},
    403: {"model": MessagingErrorEnvelope, "description": "Administrator role required"},
    404: {"model": MessagingErrorEnvelope, "description": "Resource not found"},
    409: {"model": MessagingErrorEnvelope, "description": "Revision or idempotency conflict"},
    422: {"model": MessagingErrorEnvelope, "description": "Request validation failed"},
    429: {"model": MessagingErrorEnvelope, "description": "Rate or safety limit reached"},
    500: {"model": MessagingErrorEnvelope, "description": "Internal service error"},
    503: {"model": MessagingErrorEnvelope, "description": "Required service unavailable"},
}
