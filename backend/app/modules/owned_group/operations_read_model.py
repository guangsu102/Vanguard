"""Pure-read aggregate for the stage-five owned-group operations centre."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountOperationConfig,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.automation_settings import get_owned_group_messaging_settings
from app.core.config import settings
from app.core.governance_gate import (
    GovernanceGateState,
    get_governance_gate_state,
    is_owned_group_governance_enabled,
)
from app.core.group.models import GroupAccountMembership
from app.core.user.models import User
from app.modules.guardian.models import ManagedGroupBinding
from app.modules.owned_group.member_classifier import (
    ClassificationResult,
    MemberCandidate,
    classify_member_candidates,
)
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.models import OwnedGroupAsset, OwnedGroupOperation
from app.modules.owned_group.models_extra import (
    OwnedBotProfile,
    OwnedGroupMemberObservation,
    OwnedGroupMembership,
)
from app.modules.owned_group.operations_schemas import (
    AssetSummary,
    CountsByKind,
    Coverage,
    GovernanceSummary,
    LatestOperation,
    MemberQuery,
    MemberSummary,
    MessagingSummary,
    OperationsCenterData,
    OperationsPermissions,
    OwnedGroupMember,
    SectionsSummary,
    SectionSummary,
)
from app.modules.owned_group.security import safe_exception_message

logger = structlog.get_logger().bind(module="owned_group_operations_read_model")


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _positive(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    return dict(mapping) if mapping is not None else dict(row)


class OwnedGroupOperationsError(Exception):
    """Typed domain failure translated by the router-local error adapter."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: int,
        details: dict[str, object] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
        self.retryable = retryable


@dataclass(slots=True)
class _ReadSnapshot:
    asset: dict[str, Any]
    binding: dict[str, Any] | None
    guardian_profile: dict[str, Any] | None
    members: list[dict[str, Any]]
    mapping_invalid: bool
    binding_valid: bool
    guardian_degraded: bool
    observation_started_at: datetime | None
    last_observed_at: datetime | None


@dataclass(slots=True)
class _MessagingState:
    summary: MessagingSummary
    available: bool = True


class OwnedGroupOperationsReadModel:
    """Build current committed, weakly-consistent operations snapshots.

    Every SQL statement is a SELECT with an explicit column projection.  In
    particular, Growth advertising fields, Telegram credentials, Persona
    bodies/hashes and permissions snapshots are never materialised.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        governance_gate_reader: Callable[[], Awaitable[GovernanceGateState]] | None = None,
        messaging_settings_reader: Callable[[AsyncSession], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        self.db = db
        self._governance_gate_reader = governance_gate_reader or get_governance_gate_state
        self._messaging_settings_reader = (
            messaging_settings_reader or get_owned_group_messaging_settings
        )

    async def get_operations_center(
        self,
        asset_id: int,
        *,
        role: str,
        correlation_id: str | None = None,
    ) -> OperationsCenterData:
        started = time.perf_counter()
        try:
            snapshot = await self._load_snapshot(asset_id)
            coverage = await self._coverage(snapshot)
            member_summary = self._member_summary(
                snapshot.members,
                managed_resource_count=int(snapshot.asset["member_count"] or 0),
                coverage=coverage,
            )
            messaging = await self._messaging_state(asset_id)
            latest_operation = await self._latest_operation(asset_id)
            permissions = self._permissions(role)
            sections = await self._sections(
                snapshot,
                coverage=coverage,
                messaging=messaging,
                permissions=permissions,
            )
            governance = GovernanceSummary(
                status=str(snapshot.asset["governance_status"]),
                bot_account_id=(
                    _positive(snapshot.binding.get("bot_account_id")) if snapshot.binding else None
                ),
                bot_role=(
                    str(_value(snapshot.binding.get("bot_role")))
                    if snapshot.binding and snapshot.binding.get("bot_role") is not None
                    else None
                ),
                health_status=(
                    str(_value(snapshot.guardian_profile.get("health_status")))
                    if snapshot.guardian_profile
                    and snapshot.guardian_profile.get("health_status") is not None
                    else None
                ),
                last_checked_at=_utc(snapshot.asset.get("governance_last_checked_at")),
            )
            result = OperationsCenterData(
                asset=AssetSummary(
                    asset_id=int(snapshot.asset["id"]),
                    internal_name=str(snapshot.asset["internal_name"]),
                    title=str(snapshot.asset["title"]),
                    visibility=str(snapshot.asset["visibility"]),
                    asset_status=str(snapshot.asset["status"]),
                    telegram_chat_id=snapshot.asset["telegram_chat_id"],
                    core_group_id=snapshot.asset["core_group_id"],
                    managed_binding_id=snapshot.asset["managed_binding_id"],
                    guardian_bot_account_id=snapshot.asset["guardian_bot_account_id"],
                    owner_account_id=int(snapshot.asset["owner_account_id"]),
                    managed_resource_count=int(snapshot.asset["member_count"] or 0),
                    updated_at=_utc(snapshot.asset["updated_at"]),
                ),
                sections=sections,
                governance=governance,
                messaging=messaging.summary,
                member_summary=member_summary,
                latest_operation=latest_operation,
                permissions=permissions,
                snapshot_at=datetime.now(UTC),
            )
            logger.info(
                "owned_group_operations_summary_duration_seconds",
                value=max(0.0, time.perf_counter() - started),
                result="success",
            )
            return result
        except OwnedGroupOperationsError:
            raise
        except Exception as exc:
            logger.error(
                "owned_group_operations_summary_failed",
                asset_id=asset_id,
                correlation_id=correlation_id,
                error_type=type(exc).__name__,
                error=safe_exception_message(exc, max_length=300),
            )
            raise OwnedGroupOperationsError(
                code="OPERATIONS_CENTER_READ_FAILED",
                message="Owned group operations summary is temporarily unavailable",
                http_status=503,
                retryable=True,
            ) from exc

    async def list_members(
        self,
        asset_id: int,
        query: MemberQuery,
        *,
        correlation_id: str | None = None,
    ) -> tuple[list[OwnedGroupMember], int, Coverage, MemberSummary]:
        started = time.perf_counter()
        try:
            snapshot = await self._load_snapshot(asset_id)
            coverage = await self._coverage(snapshot)
            summary = self._member_summary(
                snapshot.members,
                managed_resource_count=int(snapshot.asset["member_count"] or 0),
                coverage=None,
            )
            filtered = self._filter_members(snapshot.members, query)
            ordered = self._sort_members(filtered, query.sort)
            page = ordered[query.offset : query.offset + query.limit]
            members = [
                OwnedGroupMember.model_validate(
                    {key: value for key, value in item.items() if not key.startswith("_")}
                )
                for item in page
            ]
            logger.info(
                "owned_group_members_query_duration_seconds",
                value=max(0.0, time.perf_counter() - started),
                candidate_count=len(snapshot.members),
                returned_count=len(members),
                result="success",
            )
            return members, len(filtered), coverage, summary
        except OwnedGroupOperationsError:
            raise
        except Exception as exc:
            logger.error(
                "owned_group_members_query_failed",
                asset_id=asset_id,
                correlation_id=correlation_id,
                error_type=type(exc).__name__,
                error=safe_exception_message(exc, max_length=300),
            )
            raise OwnedGroupOperationsError(
                code="MEMBER_SOURCE_READ_FAILED",
                message="Owned group member sources are temporarily unavailable",
                http_status=503,
                retryable=True,
            ) from exc

    async def _load_snapshot(self, asset_id: int) -> _ReadSnapshot:
        asset = await self._load_asset(asset_id)
        membership_rows = [
            _mapping(row)
            for row in (
                await self.db.execute(
                    select(
                        OwnedGroupMembership.id,
                        OwnedGroupMembership.resource_type,
                        OwnedGroupMembership.resource_id,
                        OwnedGroupMembership.telegram_user_id,
                        OwnedGroupMembership.status,
                        OwnedGroupMembership.is_admin,
                        OwnedGroupMembership.admin_title,
                        OwnedGroupMembership.joined_at,
                        OwnedGroupMembership.last_verified_at,
                        OwnedGroupMembership.created_at,
                        OwnedGroupMembership.updated_at,
                    ).where(OwnedGroupMembership.group_asset_id == asset_id)
                )
            ).all()
        ]

        binding = await self._load_binding(asset.get("managed_binding_id"))
        mapping_invalid = False
        if asset.get("managed_binding_id") is not None:
            mapping_invalid = binding is None or any(
                (
                    binding.get("id") != asset.get("managed_binding_id"),
                    binding.get("group_id") != asset.get("core_group_id"),
                    binding.get("telegram_group_id") != asset.get("telegram_chat_id"),
                    binding.get("bot_account_id") != asset.get("guardian_bot_account_id"),
                )
            )

        group_rows: list[dict[str, Any]] = []
        core_group_id = _positive(asset.get("core_group_id"))
        telegram_chat_id = asset.get("telegram_chat_id")
        if core_group_id is not None and type(telegram_chat_id) is int:
            # Deliberately project only the eight allowed non-advertising
            # columns. account_id is also the stable source reference because
            # the table is unique per group/account.
            raw_group_rows = (
                await self.db.execute(
                    select(
                        GroupAccountMembership.group_id,
                        GroupAccountMembership.telegram_group_id,
                        GroupAccountMembership.account_id,
                        GroupAccountMembership.status,
                        GroupAccountMembership.joined_at,
                        GroupAccountMembership.left_at,
                        GroupAccountMembership.last_checked_at,
                        GroupAccountMembership.updated_at,
                    ).where(
                        or_(
                            GroupAccountMembership.group_id == core_group_id,
                            GroupAccountMembership.telegram_group_id == telegram_chat_id,
                        )
                    )
                )
            ).all()
            for row in raw_group_rows:
                item = _mapping(row)
                if (
                    item["group_id"] == core_group_id
                    and item["telegram_group_id"] == telegram_chat_id
                ):
                    group_rows.append(item)
                else:
                    mapping_invalid = True

        owned_profile_ids = {
            int(row["resource_id"])
            for row in membership_rows
            if str(_value(row["resource_type"])) == "bot"
            and _positive(row["resource_id"]) is not None
        }
        binding_account_ids = {
            account_id
            for account_id in (_positive(binding.get("bot_account_id")) if binding else None,)
            if account_id is not None
        }
        owned_profiles = await self._load_owned_bot_profiles(
            owned_profile_ids,
            binding_account_ids,
        )

        account_ids = {int(asset["owner_account_id"])}
        account_ids.update(
            int(row["resource_id"])
            for row in membership_rows
            if str(_value(row["resource_type"])) == "user"
            and _positive(row["resource_id"]) is not None
        )
        account_ids.update(int(row["account_id"]) for row in group_rows)
        account_ids.update(int(profile["account_id"]) for profile in owned_profiles.values())
        if binding and _positive(binding.get("bot_account_id")):
            account_ids.add(int(binding["bot_account_id"]))
        accounts, operation_modes = await self._load_accounts(account_ids)
        guardian_profiles = await self._load_guardian_profiles(account_ids)

        observation_rows = [
            _mapping(row)
            for row in (
                await self.db.execute(
                    select(
                        OwnedGroupMemberObservation.id,
                        OwnedGroupMemberObservation.telegram_user_id,
                        OwnedGroupMemberObservation.is_bot,
                        OwnedGroupMemberObservation.username_snapshot,
                        OwnedGroupMemberObservation.display_name_snapshot,
                        OwnedGroupMemberObservation.presence_status,
                        OwnedGroupMemberObservation.last_event_type,
                        OwnedGroupMemberObservation.source_bot_account_id,
                        OwnedGroupMemberObservation.last_update_id,
                        OwnedGroupMemberObservation.first_observed_at,
                        OwnedGroupMemberObservation.last_observed_at,
                        OwnedGroupMemberObservation.joined_at,
                        OwnedGroupMemberObservation.left_at,
                        OwnedGroupMemberObservation.created_at,
                        OwnedGroupMemberObservation.updated_at,
                    ).where(OwnedGroupMemberObservation.group_asset_id == asset_id)
                )
            ).all()
        ]
        user_profiles = await self._load_user_profiles(
            {int(row["telegram_user_id"]) for row in observation_rows}
        )

        candidates: list[MemberCandidate] = []

        def account_fields(account_id: int | None) -> dict[str, Any]:
            account = accounts.get(account_id or -1, {})
            return {
                "account_type": str(_value(account.get("account_type")))
                if account.get("account_type") is not None
                else None,
                "account_display_name": account.get("display_name"),
                "account_status": str(_value(account.get("status")))
                if account.get("status") is not None
                else None,
                "account_risk_level": str(_value(account.get("risk_level")))
                if account.get("risk_level") is not None
                else None,
                "risk_pause_until": _utc(account.get("risk_pause_until")),
                "operation_mode": operation_modes.get(account_id or -1),
                "persona_configured": account.get("persona_configured"),
                "persona_revision": account.get("persona_revision"),
            }

        owner_account_id = int(asset["owner_account_id"])
        candidates.append(
            MemberCandidate(
                relation="asset_owner",
                source="asset_owner",
                source_id=int(asset["id"]),
                source_time=_utc(asset.get("updated_at")),
                created_at=_utc(asset.get("created_at")),
                account_id=owner_account_id,
                **account_fields(owner_account_id),
            )
        )

        for row in membership_rows:
            resource_type = str(_value(row["resource_type"]))
            if resource_type == "bot":
                owned_profile = owned_profiles.get(int(row["resource_id"]))
                account_id = int(owned_profile["account_id"]) if owned_profile is not None else None
                guardian = guardian_profiles.get(account_id or -1)
                candidates.append(
                    MemberCandidate(
                        relation="owned_membership_bot",
                        source="owned_group_membership",
                        source_id=int(row["id"]),
                        source_time=_utc(row.get("last_verified_at") or row.get("updated_at")),
                        account_id=account_id,
                        telegram_user_id=_positive(row.get("telegram_user_id")),
                        owned_bot_profile_id=(
                            int(owned_profile["id"]) if owned_profile is not None else None
                        ),
                        owned_bot_user_id=(
                            _positive(owned_profile.get("bot_user_id"))
                            if owned_profile is not None
                            else None
                        ),
                        owned_bot_username=(
                            owned_profile.get("bot_username") if owned_profile is not None else None
                        ),
                        owned_bot_display_name=(
                            owned_profile.get("display_name") if owned_profile is not None else None
                        ),
                        parent_account_id=(
                            int(owned_profile["owner_account_id"])
                            if owned_profile is not None
                            else None
                        ),
                        guardian_bot_profile_id=(int(guardian["id"]) if guardian else None),
                        guardian_bot_user_id=(
                            _positive(guardian.get("bot_user_id")) if guardian else None
                        ),
                        guardian_bot_username=(guardian.get("bot_username") if guardian else None),
                        bot_health_status=(
                            str(_value(guardian.get("health_status"))) if guardian else None
                        ),
                        bot_sync_status=(
                            str(_value(guardian.get("sync_status"))) if guardian else None
                        ),
                        raw_presence_status=str(_value(row["status"])),
                        joined_at=_utc(row.get("joined_at")),
                        last_verified_at=_utc(row.get("last_verified_at")),
                        updated_at=_utc(row.get("updated_at")),
                        created_at=_utc(row.get("created_at")),
                        is_admin=bool(row.get("is_admin")),
                        admin_title=row.get("admin_title"),
                        quality_codes=("source_record_missing",)
                        if owned_profile is None or account_id not in accounts
                        else (),
                        **account_fields(account_id),
                    )
                )
            elif resource_type == "user":
                account_id = _positive(row.get("resource_id"))
                candidates.append(
                    MemberCandidate(
                        relation="owned_membership_user",
                        source="owned_group_membership",
                        source_id=int(row["id"]),
                        source_time=_utc(row.get("last_verified_at") or row.get("updated_at")),
                        account_id=account_id,
                        telegram_user_id=_positive(row.get("telegram_user_id")),
                        raw_presence_status=str(_value(row["status"])),
                        joined_at=_utc(row.get("joined_at")),
                        last_verified_at=_utc(row.get("last_verified_at")),
                        updated_at=_utc(row.get("updated_at")),
                        created_at=_utc(row.get("created_at")),
                        is_admin=bool(row.get("is_admin")),
                        admin_title=row.get("admin_title"),
                        quality_codes=("source_record_missing",)
                        if account_id not in accounts
                        else (),
                        **account_fields(account_id),
                    )
                )
            else:
                candidates.append(
                    MemberCandidate(
                        relation="unknown_system_resource",
                        source="owned_group_membership",
                        source_id=int(row["id"]),
                        source_time=_utc(row.get("updated_at")),
                        quality_codes=("source_record_missing",),
                    )
                )

        if binding is not None:
            account_id = _positive(binding.get("bot_account_id"))
            guardian = guardian_profiles.get(account_id or -1)
            owned_profile = next(
                (
                    profile
                    for profile in owned_profiles.values()
                    if profile["account_id"] == account_id
                ),
                None,
            )
            candidates.append(
                MemberCandidate(
                    relation="guardian_binding",
                    source="guardian_binding",
                    source_id=int(binding["id"]),
                    source_time=_utc(binding.get("last_synced_at")),
                    account_id=account_id,
                    owned_bot_profile_id=int(owned_profile["id"]) if owned_profile else None,
                    owned_bot_user_id=(
                        _positive(owned_profile.get("bot_user_id")) if owned_profile else None
                    ),
                    owned_bot_username=(
                        owned_profile.get("bot_username") if owned_profile else None
                    ),
                    owned_bot_display_name=(
                        owned_profile.get("display_name") if owned_profile else None
                    ),
                    parent_account_id=(
                        int(owned_profile["owner_account_id"]) if owned_profile else None
                    ),
                    guardian_bot_profile_id=int(guardian["id"]) if guardian else None,
                    guardian_bot_user_id=(
                        _positive(guardian.get("bot_user_id")) if guardian else None
                    ),
                    guardian_bot_username=(guardian.get("bot_username") if guardian else None),
                    bot_health_status=(
                        str(_value(guardian.get("health_status"))) if guardian else None
                    ),
                    bot_sync_status=(
                        str(_value(guardian.get("sync_status"))) if guardian else None
                    ),
                    bot_role=str(_value(binding.get("bot_role"))),
                    quality_codes=tuple(
                        code
                        for code, applies in (
                            ("target_mapping_invalid", mapping_invalid),
                            (
                                "source_record_missing",
                                account_id not in accounts or guardian is None,
                            ),
                        )
                        if applies
                    ),
                    **account_fields(account_id),
                )
            )

        for row in group_rows:
            account_id = int(row["account_id"])
            candidates.append(
                MemberCandidate(
                    relation="group_membership",
                    source="group_account_membership",
                    source_id=account_id,
                    source_time=_utc(row.get("last_checked_at") or row.get("updated_at")),
                    account_id=account_id,
                    raw_presence_status=str(_value(row["status"])),
                    joined_at=_utc(row.get("joined_at")),
                    left_at=_utc(row.get("left_at")),
                    updated_at=_utc(row.get("updated_at")),
                    created_at=_utc(row.get("updated_at")),
                    quality_codes=("source_record_missing",) if account_id not in accounts else (),
                    **account_fields(account_id),
                )
            )

        for row in observation_rows:
            telegram_user_id = int(row["telegram_user_id"])
            user = user_profiles.get(telegram_user_id)
            username = row.get("username_snapshot")
            if (
                user is not None
                and user.get("username")
                and _utc(user.get("updated_at"))
                and _utc(user.get("updated_at")) > _utc(row.get("last_observed_at"))
            ):
                username = user["username"]
            candidates.append(
                MemberCandidate(
                    relation="observation",
                    source="guardian_observation",
                    source_id=int(row["id"]),
                    source_time=_utc(row["last_observed_at"]),
                    telegram_user_id=telegram_user_id,
                    is_bot=row["is_bot"] if type(row["is_bot"]) is bool else None,
                    display_name_snapshot=row.get("display_name_snapshot"),
                    username_snapshot=username,
                    raw_presence_status=str(row["presence_status"]),
                    joined_at=_utc(row.get("joined_at")),
                    left_at=_utc(row.get("left_at")),
                    last_observed_at=_utc(row["last_observed_at"]),
                    updated_at=_utc(row.get("updated_at")),
                    created_at=_utc(row.get("created_at")),
                    user_id=int(user["id"]) if user is not None else None,
                    user_state=(str(_value(user.get("state"))) if user is not None else None),
                    warning_count=(int(user["warning_count"]) if user is not None else None),
                    muted_until=_utc(user.get("muted_until")) if user is not None else None,
                    user_profile_source_id=int(user["id"]) if user is not None else None,
                    user_profile_source_time=(
                        _utc(user.get("updated_at")) if user is not None else None
                    ),
                    quality_codes=("identity_incomplete",)
                    if type(row["is_bot"]) is not bool
                    else (),
                )
            )

        classified: ClassificationResult = classify_member_candidates(candidates, asset)
        members = [dict(item) for item in classified.members]
        for item in members:
            if item["classification_status"] == "conflict":
                logger.warning(
                    "owned_group_member_classification_conflict",
                    asset_id=asset_id,
                    result="conflict",
                    reason_code=item["classification_reason"],
                )
                logger.info(
                    "owned_group_member_classification_conflicts_total",
                    reason=item["classification_reason"],
                    value=1,
                )
            logger.info(
                "owned_group_member_classification_total",
                member_kind=item["member_kind"] or "unresolved",
                status=item["classification_status"],
                value=1,
            )

        binding_status = str(_value(binding.get("binding_status"))) if binding else None
        binding_valid = bool(
            binding
            and not mapping_invalid
            and binding_status == "active"
            and str(asset.get("governance_status")) == "managed"
        )
        guardian_profile = (
            guardian_profiles.get(int(binding["bot_account_id"])) if binding else None
        )
        health = (
            str(_value(guardian_profile.get("health_status")))
            if guardian_profile and guardian_profile.get("health_status") is not None
            else None
        )
        guardian_degraded = bool(
            str(asset.get("governance_status")) == "degraded"
            or binding_status == "degraded"
            or health in {"degraded", "offline"}
        )
        return _ReadSnapshot(
            asset=asset,
            binding=binding,
            guardian_profile=guardian_profile,
            members=members,
            mapping_invalid=mapping_invalid,
            binding_valid=binding_valid,
            guardian_degraded=guardian_degraded,
            observation_started_at=min(
                (_utc(row["first_observed_at"]) for row in observation_rows),
                default=None,
                key=lambda value: value.timestamp() if value else float("inf"),
            ),
            last_observed_at=max(
                (_utc(row["last_observed_at"]) for row in observation_rows),
                default=None,
                key=lambda value: value.timestamp() if value else float("-inf"),
            ),
        )

    async def _load_asset(self, asset_id: int) -> dict[str, Any]:
        row = (
            await self.db.execute(
                select(
                    OwnedGroupAsset.id,
                    OwnedGroupAsset.internal_name,
                    OwnedGroupAsset.title,
                    OwnedGroupAsset.visibility,
                    OwnedGroupAsset.status,
                    OwnedGroupAsset.telegram_chat_id,
                    OwnedGroupAsset.core_group_id,
                    OwnedGroupAsset.managed_binding_id,
                    OwnedGroupAsset.guardian_bot_account_id,
                    OwnedGroupAsset.owner_account_id,
                    OwnedGroupAsset.member_count,
                    OwnedGroupAsset.governance_status,
                    OwnedGroupAsset.governance_last_checked_at,
                    OwnedGroupAsset.created_at,
                    OwnedGroupAsset.updated_at,
                ).where(OwnedGroupAsset.id == asset_id)
            )
        ).first()
        if row is None:
            raise OwnedGroupOperationsError(
                code="OWNED_GROUP_NOT_FOUND",
                message="Owned group asset was not found",
                http_status=404,
            )
        return _mapping(row)

    async def _load_binding(self, binding_id: int | None) -> dict[str, Any] | None:
        if _positive(binding_id) is None:
            return None
        row = (
            await self.db.execute(
                select(
                    ManagedGroupBinding.id,
                    ManagedGroupBinding.group_id,
                    ManagedGroupBinding.telegram_group_id,
                    ManagedGroupBinding.bot_account_id,
                    ManagedGroupBinding.binding_status,
                    ManagedGroupBinding.bot_role,
                    ManagedGroupBinding.last_synced_at,
                ).where(ManagedGroupBinding.id == binding_id)
            )
        ).first()
        return _mapping(row) if row is not None else None

    async def _load_accounts(
        self, account_ids: set[int]
    ) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
        if not account_ids:
            return {}, {}
        account_rows = (
            await self.db.execute(
                select(
                    TelegramAccount.id,
                    TelegramAccount.account_type,
                    TelegramAccount.display_name,
                    TelegramAccount.status,
                    TelegramAccount.risk_level,
                    TelegramAccount.risk_pause_until,
                    case(
                        (TelegramAccount.ai_persona.is_not(None), True),
                        else_=False,
                    ).label("persona_configured"),
                    TelegramAccount.ai_persona_revision.label("persona_revision"),
                ).where(TelegramAccount.id.in_(account_ids))
            )
        ).all()
        accounts = {int(item["id"]): item for row in account_rows if (item := _mapping(row))}

        # Growth isolation contract: do not materialise the ORM entity.  This
        # compiled SELECT contains exactly account_id and operation_mode.
        operation_rows = (
            await self.db.execute(
                select(
                    AccountOperationConfig.account_id,
                    AccountOperationConfig.operation_mode,
                ).where(AccountOperationConfig.account_id.in_(account_ids))
            )
        ).all()
        operation_modes = {
            int(item["account_id"]): str(_value(item["operation_mode"]))
            for row in operation_rows
            if (item := _mapping(row))
        }
        return accounts, operation_modes

    async def _load_owned_bot_profiles(
        self,
        profile_ids: set[int],
        binding_account_ids: set[int],
    ) -> dict[int, dict[str, Any]]:
        filters = []
        if profile_ids:
            filters.append(OwnedBotProfile.id.in_(profile_ids))
        if binding_account_ids:
            filters.append(OwnedBotProfile.account_id.in_(binding_account_ids))
        if not filters:
            return {}
        rows = (
            await self.db.execute(
                select(
                    OwnedBotProfile.id,
                    OwnedBotProfile.owner_account_id,
                    OwnedBotProfile.account_id,
                    OwnedBotProfile.bot_user_id,
                    OwnedBotProfile.bot_username,
                    OwnedBotProfile.display_name,
                    OwnedBotProfile.status,
                    OwnedBotProfile.last_verified_at,
                    OwnedBotProfile.updated_at,
                ).where(or_(*filters))
            )
        ).all()
        return {int(item["id"]): item for row in rows if (item := _mapping(row))}

    async def _load_guardian_profiles(self, account_ids: set[int]) -> dict[int, dict[str, Any]]:
        if not account_ids:
            return {}
        rows = (
            await self.db.execute(
                select(
                    GuardianBotProfile.id,
                    GuardianBotProfile.account_id,
                    GuardianBotProfile.bot_username,
                    GuardianBotProfile.bot_user_id,
                    GuardianBotProfile.health_status,
                    GuardianBotProfile.sync_status,
                    GuardianBotProfile.last_heartbeat_at,
                    GuardianBotProfile.last_synced_at,
                ).where(GuardianBotProfile.account_id.in_(account_ids))
            )
        ).all()
        return {int(item["account_id"]): item for row in rows if (item := _mapping(row))}

    async def _load_user_profiles(self, telegram_user_ids: set[int]) -> dict[int, dict[str, Any]]:
        if not telegram_user_ids:
            return {}
        rows = (
            await self.db.execute(
                select(
                    User.id,
                    User.telegram_id,
                    User.username,
                    User.state,
                    User.warning_count,
                    User.muted_until,
                    User.updated_at,
                ).where(User.telegram_id.in_(telegram_user_ids))
            )
        ).all()
        return {int(item["telegram_id"]): item for row in rows if (item := _mapping(row))}

    async def _coverage(self, snapshot: _ReadSnapshot) -> Coverage:
        reasons = ["telegram_full_roster_not_loaded"]
        asset_status = str(snapshot.asset["status"])
        gate: GovernanceGateState | None = None
        gate_failed = False
        if snapshot.binding is not None:
            try:
                gate = await self._governance_gate_reader()
            except Exception:
                gate_failed = True

        if asset_status == "archived":
            coverage_status = "historical"
            reasons.append("asset_archived")
        elif (
            snapshot.mapping_invalid
            or snapshot.guardian_degraded
            or gate_failed
            or (gate is not None and not gate.backend_available)
        ):
            coverage_status = "degraded"
            if snapshot.mapping_invalid:
                reasons.append("target_mapping_invalid")
            elif gate_failed or (gate is not None and not gate.backend_available):
                reasons.append("governance_gate_backend_unavailable")
            else:
                reasons.append("guardian_degraded")
        elif snapshot.binding_valid and (
            not is_owned_group_governance_enabled() or (gate is not None and gate.governance_stop)
        ):
            coverage_status = "paused"
            reasons.append("member_observation_paused")
            reasons.append(
                "governance_feature_disabled"
                if not is_owned_group_governance_enabled()
                else "governance_stop_enabled"
            )
        elif snapshot.binding_valid:
            coverage_status = "collecting"
        elif snapshot.last_observed_at is not None:
            coverage_status = "historical"
            reasons.append("governance_binding_missing")
        else:
            coverage_status = "not_started"
            reasons.append("guardian_not_managed")
        return Coverage(
            coverage_status=coverage_status,
            observation_started_at=snapshot.observation_started_at,
            last_observed_at=snapshot.last_observed_at,
            blocking_reasons=list(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _member_summary(
        members: Iterable[dict[str, Any]],
        *,
        managed_resource_count: int,
        coverage: Coverage | None,
    ) -> MemberSummary:
        values = list(members)
        counts = CountsByKind(
            real_user=sum(item["member_kind"] == "real_user" for item in values),
            system_ad_account=sum(item["member_kind"] == "system_ad_account" for item in values),
            system_bot=sum(item["member_kind"] == "system_bot" for item in values),
        )
        return MemberSummary(
            managed_resource_count=max(0, managed_resource_count),
            observed_real_user_count=counts.real_user,
            counts_by_kind=counts,
            unresolved_count=sum(item["classification_status"] == "unresolved" for item in values),
            conflict_count=sum(item["classification_status"] == "conflict" for item in values),
            coverage=coverage,
        )

    async def _messaging_state(self, asset_id: int) -> _MessagingState:
        try:
            runtime = await self._messaging_settings_reader(self.db)
            aggregate = (
                await self.db.execute(
                    select(
                        func.count(GroupAccountMessagePolicy.id).label("policy_count"),
                        func.coalesce(
                            func.sum(
                                case((GroupAccountMessagePolicy.enabled.is_(True), 1), else_=0)
                            ),
                            0,
                        ).label("enabled_policy_count"),
                    ).where(GroupAccountMessagePolicy.owned_group_asset_id == asset_id)
                )
            ).one()
            execution = (
                await self.db.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        GroupAccountMessageExecution.status == "pending_review",
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("pending_review_count"),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        (GroupAccountMessageExecution.status == "sent")
                                        & (
                                            GroupAccountMessageExecution.sent_at
                                            >= datetime.now(UTC).replace(
                                                hour=0,
                                                minute=0,
                                                second=0,
                                                microsecond=0,
                                                tzinfo=None,
                                            )
                                        ),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ).label("sent_today"),
                    ).where(GroupAccountMessageExecution.owned_group_asset_id == asset_id)
                )
            ).one()
            aggregate_values = _mapping(aggregate)
            execution_values = _mapping(execution)
            static_enabled = bool(settings.OWNED_GROUP_MESSAGING_ENABLED)
            runtime_enabled = bool(runtime.get("enabled", False))
            dry_run = bool(runtime.get("dryRun", True))
            return _MessagingState(
                summary=MessagingSummary(
                    static_enabled=static_enabled,
                    runtime_enabled=runtime_enabled,
                    dry_run=dry_run,
                    can_send=static_enabled and runtime_enabled and not dry_run,
                    policy_count=int(aggregate_values["policy_count"] or 0),
                    enabled_policy_count=int(aggregate_values["enabled_policy_count"] or 0),
                    pending_review_count=int(execution_values["pending_review_count"] or 0),
                    sent_today=int(execution_values["sent_today"] or 0),
                )
            )
        except Exception:
            return _MessagingState(
                summary=MessagingSummary(
                    static_enabled=bool(settings.OWNED_GROUP_MESSAGING_ENABLED),
                    runtime_enabled=False,
                    dry_run=True,
                    can_send=False,
                    policy_count=0,
                    enabled_policy_count=0,
                    pending_review_count=0,
                    sent_today=0,
                ),
                available=False,
            )

    async def _latest_operation(self, asset_id: int) -> LatestOperation | None:
        row = (
            await self.db.execute(
                select(
                    OwnedGroupOperation.id,
                    OwnedGroupOperation.operation_type,
                    OwnedGroupOperation.status,
                    OwnedGroupOperation.updated_at,
                )
                .where(OwnedGroupOperation.group_asset_id == asset_id)
                .order_by(OwnedGroupOperation.updated_at.desc(), OwnedGroupOperation.id.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        item = _mapping(row)
        return LatestOperation(
            id=int(item["id"]),
            operation_type=str(item["operation_type"]),
            status=str(item["status"]),
            updated_at=_utc(item["updated_at"]),
        )

    @staticmethod
    def _permissions(role: str) -> OperationsPermissions:
        admin = role == "admin"
        operator = role == "operator"
        return OperationsPermissions(
            manage_orchestration=admin or operator,
            manage_governance=admin or operator,
            manage_messaging=admin,
            manage_persona=admin,
            manage_activities=admin or operator,
            view_members=True,
            view_audit=True,
        )

    async def _sections(
        self,
        snapshot: _ReadSnapshot,
        *,
        coverage: Coverage,
        messaging: _MessagingState,
        permissions: OperationsPermissions,
    ) -> SectionsSummary:
        status = str(snapshot.asset["status"])
        archived = status == "archived"
        orchestration_reasons: list[str] = []
        if archived:
            orchestration_state = "disabled"
            orchestration_reasons.append("asset_archived")
        elif status in {"draft", "prechecking", "creating"}:
            orchestration_state = "pending"
            orchestration_reasons.append("asset_not_ready")
        elif status in {"create_failed", "needs_attention"}:
            orchestration_state = "degraded"
            orchestration_reasons.append("asset_needs_attention")
        else:
            orchestration_state = "ready"
        orchestration_gate_enabled = bool(
            settings.OWNED_GROUP_MODULE_ENABLED and settings.OWNED_GROUP_EXECUTION_ENABLED
        )
        orchestration_execute = bool(
            permissions.manage_orchestration and status == "ready" and orchestration_gate_enabled
        )
        if status == "ready" and not orchestration_gate_enabled:
            orchestration_reasons.append("owned_group_execution_disabled")

        coverage_state = coverage.coverage_status
        if archived:
            governance_state = "disabled"
        elif (
            status != "ready"
            or snapshot.asset.get("telegram_chat_id") is None
            or snapshot.asset.get("core_group_id") is None
        ):
            governance_state = "unavailable"
        elif snapshot.mapping_invalid or snapshot.guardian_degraded or coverage_state == "degraded":
            governance_state = "degraded"
        elif coverage_state == "paused":
            governance_state = "stopped"
        elif str(snapshot.asset["governance_status"]) == "pending":
            governance_state = "pending"
        elif not snapshot.binding_valid:
            governance_state = "not_configured"
        else:
            governance_state = "ready"
        governance_reasons = [
            reason
            for reason in coverage.blocking_reasons
            if reason != "telegram_full_roster_not_loaded"
        ]
        if governance_state == "unavailable":
            governance_reasons.append(
                "asset_not_ready" if status != "ready" else "governance_not_managed"
            )

        members_state = {
            "collecting": "ready",
            "paused": "stopped",
            "degraded": "degraded",
            "historical": "ready" if archived else "not_configured",
            "not_started": "not_configured",
        }[coverage_state]

        messaging_reasons: list[str] = []
        if archived:
            messaging_state = "disabled"
            messaging_reasons.append("asset_archived")
        elif not messaging.available:
            messaging_state = "unavailable"
            messaging_reasons.append("messaging_source_unavailable")
        elif status != "ready" or snapshot.asset.get("core_group_id") is None:
            messaging_state = "unavailable"
            messaging_reasons.append("asset_not_ready")
        elif not messaging.summary.static_enabled:
            messaging_state = "disabled"
            messaging_reasons.append("messaging_feature_disabled")
        elif not messaging.summary.runtime_enabled:
            messaging_state = "disabled"
            messaging_reasons.append("messaging_runtime_disabled")
        else:
            messaging_state = "ready"
        if messaging.summary.dry_run:
            messaging_reasons.append("messaging_dry_run")

        if archived:
            activities_state = "disabled"
            activities_reasons = ["asset_archived"]
        elif (
            status != "ready"
            or snapshot.asset.get("telegram_chat_id") is None
            or snapshot.asset.get("core_group_id") is None
        ):
            activities_state = "unavailable"
            activities_reasons = [
                "asset_not_ready" if status != "ready" else "governance_not_managed"
            ]
        elif snapshot.mapping_invalid or snapshot.guardian_degraded or coverage_state == "degraded":
            activities_state = "degraded"
            activities_reasons = governance_reasons or ["guardian_degraded"]
        elif coverage_state == "paused":
            activities_state = "stopped"
            activities_reasons = governance_reasons
        elif snapshot.binding_valid:
            activities_state = "ready"
            activities_reasons = []
        else:
            activities_state = "not_configured"
            activities_reasons = ["guardian_not_managed"]

        return SectionsSummary(
            orchestration=SectionSummary(
                state=orchestration_state,
                can_view=True,
                can_manage=permissions.manage_orchestration and not archived,
                can_execute=orchestration_execute,
                blocking_reasons=list(dict.fromkeys(orchestration_reasons)),
            ),
            governance=SectionSummary(
                state=governance_state,
                can_view=True,
                can_manage=permissions.manage_governance and not archived,
                can_execute=permissions.manage_governance and governance_state == "ready",
                blocking_reasons=list(dict.fromkeys(governance_reasons)),
            ),
            members=SectionSummary(
                state=members_state,
                can_view=True,
                can_manage=False,
                can_execute=False,
                blocking_reasons=list(
                    dict.fromkeys(coverage.blocking_reasons + ["member_data_scope_partial"])
                ),
            ),
            messaging=SectionSummary(
                state=messaging_state,
                can_view=True,
                can_manage=permissions.manage_messaging and not archived,
                can_execute=permissions.manage_messaging
                and messaging_state == "ready"
                and messaging.summary.can_send
                and status == "ready",
                blocking_reasons=list(dict.fromkeys(messaging_reasons)),
            ),
            activities=SectionSummary(
                state=activities_state,
                can_view=True,
                can_manage=permissions.manage_activities and not archived,
                can_execute=permissions.manage_activities and activities_state == "ready",
                blocking_reasons=list(dict.fromkeys(activities_reasons)),
            ),
            audit=SectionSummary(
                state="ready",
                can_view=True,
                can_manage=False,
                can_execute=False,
                blocking_reasons=["asset_archived"] if archived else [],
            ),
        )

    @staticmethod
    def _filter_members(
        members: Iterable[dict[str, Any]], query: MemberQuery
    ) -> list[dict[str, Any]]:
        result = list(members)
        if query.member_kind:
            result = [item for item in result if item["member_kind"] == query.member_kind]
        if query.presence_status:
            result = [item for item in result if item["presence_status"] == query.presence_status]
        if query.classification_status:
            result = [
                item
                for item in result
                if item["classification_status"] == query.classification_status
            ]
        if query.telegram_role:
            result = [item for item in result if item["telegram_role"] == query.telegram_role]
        if query.q:
            query_value = query.q.strip()
            if query_value.isdigit():
                telegram_id = int(query_value)
                result = [item for item in result if item["telegram_user_id"] == telegram_id]
            elif query_value.lower().startswith("account:") and query_value[8:].isdigit():
                account_id = int(query_value[8:])
                result = [item for item in result if item["account_id"] == account_id]
            else:
                needle = query_value.lstrip("@").casefold()
                result = [
                    item
                    for item in result
                    if needle in str(item.get("display_name") or "").casefold()
                    or needle in str(item.get("username") or "").casefold()
                ]
        return result

    @staticmethod
    def _sort_members(members: Iterable[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
        values = list(members)
        if sort == "name_asc":
            return sorted(
                values,
                key=lambda item: (
                    not bool(item.get("display_name") or item.get("username")),
                    str(item.get("display_name") or item.get("username") or "").casefold(),
                    item["member_key"],
                ),
            )
        if sort == "kind_asc":
            order = {"system_bot": 0, "system_ad_account": 1, "real_user": 2, None: 3}
            return sorted(
                values,
                key=lambda item: (
                    order.get(item.get("member_kind"), 3),
                    str(item.get("display_name") or item.get("username") or "").casefold(),
                    item["member_key"],
                ),
            )

        def evidence_timestamp(item: dict[str, Any]) -> float:
            for value in (
                item.get("last_observed_at"),
                item.get("last_verified_at"),
                item.get("joined_at"),
                item.get("_sort_created_at"),
            ):
                normalized = _utc(value)
                if normalized is not None:
                    return normalized.timestamp()
            return float("-inf")

        # Stable two-pass sort gives evidence DESC and member_key ASC.
        values.sort(key=lambda item: item["member_key"])
        values.sort(key=evidence_timestamp, reverse=True)
        return values


__all__ = ["OwnedGroupOperationsError", "OwnedGroupOperationsReadModel"]
