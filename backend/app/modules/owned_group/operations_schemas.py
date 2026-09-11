"""Public contracts for the stage-five owned-group operations centre.

The models in this module deliberately contain no free-form Persona, Telegram
session, token, proxy, invite-link or permissions-snapshot fields.  Keeping the
wire contract explicit is part of the read boundary, not just documentation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemberKind = Literal["real_user", "system_ad_account", "system_bot"]
ClassificationStatus = Literal["resolved", "unresolved", "conflict"]
PresenceStatus = Literal["present", "pending", "left", "failed", "unknown"]
PresenceConfidence = Literal["verified", "observed", "stored", "unknown"]
DataQuality = Literal["reliable", "partial", "conflict"]
TelegramRole = Literal["owner", "administrator", "member", "unknown"]
CoverageStatus = Literal["historical", "degraded", "paused", "collecting", "not_started"]
SectionState = Literal[
    "ready",
    "not_configured",
    "pending",
    "degraded",
    "disabled",
    "stopped",
    "unavailable",
]
MemberSort = Literal["last_observed_desc", "name_asc", "kind_asc"]


class OperationsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class MemberQuery(OperationsModel):
    member_kind: MemberKind | None = None
    presence_status: PresenceStatus | None = None
    classification_status: ClassificationStatus | None = None
    telegram_role: TelegramRole | None = None
    q: str | None = Field(default=None, max_length=100)
    sort: MemberSort = "last_observed_desc"
    offset: int = Field(default=0, ge=0, le=10_000)
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator("q", mode="before")
    @classmethod
    def normalize_query(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("q must contain at least one non-whitespace character")
        return normalized


class MemberSource(OperationsModel):
    source: Literal[
        "asset_owner",
        "owned_group_membership",
        "guardian_binding",
        "group_account_membership",
        "guardian_observation",
        "user_profile",
    ]
    record_id: int = Field(ge=1)
    observed_at: datetime | None = None


class Coverage(OperationsModel):
    data_scope: Literal["managed_and_observed"] = "managed_and_observed"
    is_complete: Literal[False] = False
    full_roster_supported: Literal[False] = False
    coverage_status: CoverageStatus
    observation_started_at: datetime | None = None
    last_observed_at: datetime | None = None
    blocking_reasons: list[str] = Field(default_factory=list)


class CountsByKind(OperationsModel):
    real_user: int = Field(default=0, ge=0)
    system_ad_account: int = Field(default=0, ge=0)
    system_bot: int = Field(default=0, ge=0)


class MemberSummary(OperationsModel):
    managed_resource_count: int = Field(default=0, ge=0)
    observed_real_user_count: int = Field(default=0, ge=0)
    counts_by_kind: CountsByKind = Field(default_factory=CountsByKind)
    unresolved_count: int = Field(default=0, ge=0)
    conflict_count: int = Field(default=0, ge=0)
    coverage: Coverage | None = None


class OwnedGroupMember(OperationsModel):
    member_key: str = Field(min_length=1, max_length=160)
    member_kind: MemberKind | None = None
    classification_status: ClassificationStatus
    classification_reason: str = Field(min_length=1, max_length=64)
    telegram_user_id: int | None = None
    display_name: str = Field(min_length=1, max_length=200)
    username: str | None = Field(default=None, max_length=120)
    primary_source: str = Field(min_length=1, max_length=64)
    sources: list[MemberSource]
    account_id: int | None = None
    owned_bot_profile_id: int | None = None
    guardian_bot_profile_id: int | None = None
    parent_account_id: int | None = None
    user_id: int | None = None
    operation_mode: Literal["growth", "ad_only"] | None = None
    account_status: str | None = Field(default=None, max_length=32)
    account_risk_level: str | None = Field(default=None, max_length=32)
    risk_pause_until: datetime | None = None
    risk_scope: Literal["account_global", "user_global"] | None = None
    persona_configured: bool | None = None
    persona_revision: int | None = Field(default=None, ge=0)
    bot_health_status: str | None = Field(default=None, max_length=32)
    bot_sync_status: str | None = Field(default=None, max_length=32)
    telegram_role: TelegramRole
    is_admin: bool
    admin_title: str | None = Field(default=None, max_length=64)
    presence_status: PresenceStatus
    presence_confidence: PresenceConfidence
    raw_presence_status: str | None = Field(default=None, max_length=64)
    user_state: str | None = Field(default=None, max_length=32)
    warning_count: int | None = Field(default=None, ge=0)
    muted_until: datetime | None = None
    joined_at: datetime | None = None
    left_at: datetime | None = None
    last_verified_at: datetime | None = None
    last_observed_at: datetime | None = None
    data_quality: DataQuality
    quality_codes: list[str] = Field(default_factory=list)


class AssetSummary(OperationsModel):
    asset_id: int
    internal_name: str
    title: str
    visibility: str
    asset_status: str
    telegram_chat_id: int | None = None
    core_group_id: int | None = None
    managed_binding_id: int | None = None
    guardian_bot_account_id: int | None = None
    owner_account_id: int
    managed_resource_count: int = Field(ge=0)
    updated_at: datetime


class SectionSummary(OperationsModel):
    state: SectionState
    can_view: bool
    can_manage: bool
    can_execute: bool
    blocking_reasons: list[str] = Field(default_factory=list)


class SectionsSummary(OperationsModel):
    orchestration: SectionSummary
    governance: SectionSummary
    members: SectionSummary
    messaging: SectionSummary
    activities: SectionSummary
    audit: SectionSummary


class GovernanceSummary(OperationsModel):
    status: str
    bot_account_id: int | None = None
    bot_role: str | None = None
    health_status: str | None = None
    last_checked_at: datetime | None = None


class MessagingSummary(OperationsModel):
    static_enabled: bool
    runtime_enabled: bool
    dry_run: bool
    can_send: bool
    policy_count: int = Field(ge=0)
    enabled_policy_count: int = Field(ge=0)
    pending_review_count: int = Field(ge=0)
    sent_today: int = Field(ge=0)


class LatestOperation(OperationsModel):
    id: int
    operation_type: str
    status: str
    updated_at: datetime


class OperationsPermissions(OperationsModel):
    manage_orchestration: bool
    manage_governance: bool
    manage_messaging: bool
    manage_persona: bool
    manage_activities: bool
    view_members: bool = True
    view_audit: bool = True


class OperationsCenterData(OperationsModel):
    asset: AssetSummary
    sections: SectionsSummary
    governance: GovernanceSummary
    messaging: MessagingSummary
    member_summary: MemberSummary
    latest_operation: LatestOperation | None = None
    permissions: OperationsPermissions
    snapshot_at: datetime


class OperationsCenterEnvelope(OperationsModel):
    data: OperationsCenterData
    correlation_id: str


class MemberPageEnvelope(OperationsModel):
    data: list[OwnedGroupMember]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    coverage: Coverage
    summary: MemberSummary
    correlation_id: str


class OperationsErrorBody(OperationsModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)
    retryable: bool = False


class OperationsErrorEnvelope(OperationsModel):
    error: OperationsErrorBody
    correlation_id: str


OPERATIONS_ERROR_RESPONSES = {
    401: {"model": OperationsErrorEnvelope},
    403: {"model": OperationsErrorEnvelope},
    404: {"model": OperationsErrorEnvelope},
    422: {"model": OperationsErrorEnvelope},
    503: {"model": OperationsErrorEnvelope},
}


__all__ = [
    "AssetSummary",
    "ClassificationStatus",
    "CountsByKind",
    "Coverage",
    "DataQuality",
    "GovernanceSummary",
    "LatestOperation",
    "MemberKind",
    "MemberPageEnvelope",
    "MemberQuery",
    "MemberSort",
    "MemberSource",
    "MemberSummary",
    "MessagingSummary",
    "OPERATIONS_ERROR_RESPONSES",
    "OperationsCenterData",
    "OperationsCenterEnvelope",
    "OperationsErrorEnvelope",
    "OperationsPermissions",
    "OwnedGroupMember",
    "PresenceConfidence",
    "PresenceStatus",
    "SectionState",
    "SectionSummary",
    "SectionsSummary",
    "TelegramRole",
]
