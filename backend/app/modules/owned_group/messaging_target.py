"""Exact owned-group target resolution and promoter-account admission gates."""

from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountOperationMode,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group
from app.core.p0_safety_gate import _has_usable_user_session
from app.modules.guardian.models import ManagedGroupBinding, ManagedGroupBindingStatus
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_models import GroupAccountMessagePolicy
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedGroupMembership


def _value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


TELEGRAM_VERIFIED_MEMBER_STATUSES = frozenset(
    {"member", "administrator", "admin", "owner", "creator"}
)
TELEGRAM_NON_MEMBER_STATUSES = frozenset({"left", "kicked", "banned", "restricted"})


def normalize_telegram_member_status(value: object) -> str:
    """Normalize Pyrogram enums and plain Telegram membership statuses."""

    return str(_value(value) or "").strip().casefold().rsplit(".", 1)[-1]


@dataclass(frozen=True, slots=True)
class OwnedGroupMessageTarget:
    owned_group_asset_id: int
    core_group_id: int
    telegram_chat_id: int
    managed_binding_id: int
    group_title: str = ""


@dataclass(frozen=True, slots=True)
class AccountEligibility:
    account_id: int
    display_name: str
    status: str
    risk_level: str
    operation_mode: str | None
    membership_status: str | None
    last_verified_at: datetime | None
    eligible: bool
    blocking_reasons: tuple[str, ...]
    policy_id: int | None = None
    probe_required: bool = False

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["blocking_reasons"] = list(self.blocking_reasons)
        return value


@dataclass(frozen=True, slots=True)
class MembershipProbeResult:
    """Sanitized result of one exact-account, read-only Telegram probe."""

    verified: bool
    reason_code: str | None = None
    telegram_user_id: int | None = None


class MembershipProbe(Protocol):
    async def __call__(
        self,
        *,
        asset: OwnedGroupAsset,
        account: TelegramAccount,
    ) -> MembershipProbeResult | bool: ...


class OwnedGroupAccountMembershipProbe:
    """Verify that the configured sender itself is still a group member.

    The probe performs only identity/entity/participant reads.  It deliberately
    acquires the exact configured account and never asks the pool to substitute
    another account.
    """

    def __init__(self, pool: Any | None = None):
        self.pool = pool

    @staticmethod
    async def _call(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def __call__(
        self,
        *,
        asset: OwnedGroupAsset,
        account: TelegramAccount,
    ) -> MembershipProbeResult:
        from app.core.account.pool import get_account_pool

        pool = self.pool or get_account_pool()
        wrapper = None
        try:
            if asset.telegram_chat_id is None:
                return MembershipProbeResult(False, "target_mapping_invalid")
            add_account = getattr(pool, "add_account_from_db", None)
            if callable(add_account):
                await self._call(add_account(account))
            wrapper = await self._call(
                pool.acquire_by_id(
                    int(account.id),
                    purpose="owned_group_message_membership_probe",
                    require_session=True,
                    raise_on_lease_failure=True,
                )
            )
            if wrapper is None:
                return MembershipProbeResult(False, "account_unavailable")
            client = getattr(wrapper, "client", None)
            if client is None and hasattr(wrapper, "get_client"):
                client = wrapper.get_client()
            if client is None:
                return MembershipProbeResult(False, "telegram_client_unavailable")

            me = await self._call(client.get_me())
            telegram_user_id = int(getattr(me, "id", None) or getattr(me, "user_id", None) or 0)
            if not telegram_user_id:
                return MembershipProbeResult(False, "telegram_user_id_missing")

            get_chat_member = getattr(client, "get_chat_member", None)
            if callable(get_chat_member):
                participant = await self._call(
                    get_chat_member(int(asset.telegram_chat_id), telegram_user_id)
                )
                raw_status = (
                    participant.get("status", "")
                    if isinstance(participant, dict)
                    else getattr(participant, "status", "")
                )
                status = normalize_telegram_member_status(raw_status)
                if participant is None or status not in TELEGRAM_VERIFIED_MEMBER_STATUSES:
                    return MembershipProbeResult(
                        False,
                        "membership_not_verified",
                        telegram_user_id,
                    )
            else:
                from telethon import functions

                from app.core.account.telegram_membership import (
                    classify_telethon_membership_response,
                )

                entity = await self._call(client.get_entity(int(asset.telegram_chat_id)))
                response = await self._call(
                    client(
                        functions.channels.GetParticipantRequest(
                            channel=entity,
                            participant=me,
                        )
                    )
                )
                membership_state = classify_telethon_membership_response(response)
                if membership_state != "verified":
                    return MembershipProbeResult(
                        False,
                        (
                            "membership_not_verified"
                            if membership_state == "not_member"
                            else "membership_probe_failed"
                        ),
                        telegram_user_id,
                    )
            return MembershipProbeResult(True, None, telegram_user_id)
        except Exception as exc:
            exception_name = exc.__class__.__name__.casefold()
            if "notparticipant" in exception_name or "usernotparticipant" in exception_name:
                return MembershipProbeResult(False, "membership_not_verified")
            return MembershipProbeResult(False, "membership_probe_failed")
        finally:
            if wrapper is not None:
                try:
                    await self._call(pool.release(wrapper))
                except Exception:
                    pass


class OwnedGroupMessageTargetResolver:
    """Resolve IDs without sign/size guesses and reject every mismatch."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        membership_probe: MembershipProbe | None = None,
    ):
        self.db = db
        self.membership_probe = membership_probe or OwnedGroupAccountMembershipProbe()

    async def _probe_stale_membership(
        self,
        *,
        asset_id: int,
        account: TelegramAccount,
        membership: OwnedGroupMembership,
    ) -> MembershipProbeResult:
        if membership.telegram_user_id is None:
            membership.status = "unknown_needs_reconcile"
            membership.updated_at = datetime.utcnow()
            await self.db.flush()
            return MembershipProbeResult(False, "telegram_identity_unbound")

        asset = await self.get_asset(asset_id)
        try:
            value = await self.membership_probe(asset=asset, account=account)
        except Exception:
            value = MembershipProbeResult(False, "membership_probe_failed")
        result = (
            value
            if isinstance(value, MembershipProbeResult)
            else MembershipProbeResult(bool(value), None if value else "membership_probe_failed")
        )
        if not result.verified:
            return result

        now = datetime.utcnow()
        if result.telegram_user_id is None:
            membership.status = "unknown_needs_reconcile"
            membership.updated_at = now
            await self.db.flush()
            return MembershipProbeResult(False, "telegram_user_id_missing")
        if int(membership.telegram_user_id) != int(result.telegram_user_id):
            membership.status = "unknown_needs_reconcile"
            membership.updated_at = now
            await self.db.flush()
            return MembershipProbeResult(
                False,
                "telegram_identity_mismatch",
                int(result.telegram_user_id),
            )
        membership.last_verified_at = now
        membership.updated_at = now
        await self.db.flush()
        return result

    async def get_asset(self, asset_id: int) -> OwnedGroupAsset:
        asset = await self.db.get(OwnedGroupAsset, int(asset_id), populate_existing=True)
        if asset is None:
            raise OwnedGroupMessagingError(
                "OWNED_GROUP_NOT_FOUND",
                "自建群不存在",
                http_status=404,
                details={"asset_id": int(asset_id)},
            )
        return asset

    async def resolve(
        self,
        asset_id: int,
        *,
        require_managed: bool = True,
    ) -> OwnedGroupMessageTarget:
        asset = await self.get_asset(asset_id)
        mapping_details = {"asset_id": int(asset_id)}
        if (
            _value(asset.status) != "ready"
            or asset.archived_at is not None
            or asset.core_group_id is None
            or asset.telegram_chat_id is None
            or asset.managed_binding_id is None
        ):
            raise OwnedGroupMessagingError(
                "TARGET_MAPPING_INVALID",
                "自建群目标映射不完整或资产未就绪",
                details=mapping_details,
            )

        group = await self.db.get(Group, int(asset.core_group_id), populate_existing=True)
        binding = await self.db.get(
            ManagedGroupBinding,
            int(asset.managed_binding_id),
            populate_existing=True,
        )
        if (
            group is None
            or int(group.id) != int(asset.core_group_id)
            or int(group.group_id) != int(asset.telegram_chat_id)
            or binding is None
            or int(binding.id) != int(asset.managed_binding_id)
            or int(binding.group_id) != int(asset.core_group_id)
            or int(binding.telegram_group_id) != int(asset.telegram_chat_id)
            or _value(binding.binding_status) != ManagedGroupBindingStatus.ACTIVE.value
        ):
            raise OwnedGroupMessagingError(
                "TARGET_MAPPING_INVALID",
                "自建群、内部群与治理绑定的目标标识不一致",
                details=mapping_details,
            )
        if require_managed and _value(asset.governance_status) != "managed":
            raise OwnedGroupMessagingError(
                "GOVERNANCE_NOT_MANAGED",
                "群未处于 managed 治理状态",
                details={
                    "asset_id": int(asset_id),
                    "governance_status": str(_value(asset.governance_status)),
                },
            )
        return OwnedGroupMessageTarget(
            owned_group_asset_id=int(asset.id),
            core_group_id=int(group.id),
            telegram_chat_id=int(group.group_id),
            managed_binding_id=int(binding.id),
            group_title=str(group.title or ""),
        )

    async def resolve_by_telegram_chat_id(
        self,
        telegram_chat_id: int,
        *,
        require_managed: bool = True,
    ) -> OwnedGroupMessageTarget:
        rows = await self.db.scalars(
            select(OwnedGroupAsset).where(OwnedGroupAsset.telegram_chat_id == int(telegram_chat_id))
        )
        assets = list(rows.all())
        if len(assets) != 1:
            raise OwnedGroupMessagingError(
                "TARGET_MAPPING_INVALID",
                "Telegram 群目标未唯一映射到自建群资产",
                details={"telegram_chat_id": int(telegram_chat_id)},
            )
        return await self.resolve(assets[0].id, require_managed=require_managed)

    async def validate_account_eligibility(
        self,
        asset_id: int,
        account_id: int,
        *,
        probe_stale: bool = True,
    ) -> AccountEligibility:
        """Return every admission failure; never select or substitute another account."""
        account = await self.db.scalar(
            select(TelegramAccount)
            .options(selectinload(TelegramAccount.operation_config))
            .where(TelegramAccount.id == int(account_id))
            .execution_options(populate_existing=True)
        )
        if account is None:
            return AccountEligibility(
                account_id=int(account_id),
                display_name="",
                status="missing",
                risk_level="unknown",
                operation_mode=None,
                membership_status=None,
                last_verified_at=None,
                eligible=False,
                blocking_reasons=("account_not_found",),
            )

        membership = await self.db.scalar(
            select(OwnedGroupMembership).where(
                OwnedGroupMembership.group_asset_id == int(asset_id),
                OwnedGroupMembership.resource_type == "user",
                OwnedGroupMembership.resource_id == int(account_id),
            )
            .execution_options(populate_existing=True)
        )
        policy_id = await self.db.scalar(
            select(GroupAccountMessagePolicy.id).where(
                GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id),
                GroupAccountMessagePolicy.account_id == int(account_id),
            )
        )
        config = account.operation_config
        reasons: list[str] = []
        if _value(account.account_type) != AccountType.PROMOTER.value:
            reasons.append("account_type_not_promoter")
        if not bool(account.is_active):
            reasons.append("account_inactive")
        if not _has_usable_user_session(account):
            reasons.append("account_session_missing")
        if _value(account.status) in {AccountStatus.ERROR.value, AccountStatus.BANNED.value}:
            reasons.append("account_status_blocked")
        if str(_value(account.risk_level)) not in {
            AccountRiskLevel.NORMAL.value,
            AccountRiskLevel.WATCH.value,
        }:
            reasons.append("account_risk_blocked")

        now = datetime.utcnow()
        pause_until = account.risk_pause_until
        if pause_until is not None and pause_until > now:
            reasons.append("account_risk_pause_active")
        if config is None:
            reasons.append("operation_config_missing")
            operation_mode = None
        else:
            operation_mode = str(_value(config.operation_mode))
            if not bool(config.enabled):
                reasons.append("operation_config_disabled")
            if operation_mode != AccountOperationMode.GROWTH.value:
                reasons.append("account_mode_not_allowed")

        membership_status = str(_value(membership.status)) if membership is not None else None
        last_verified_at = membership.last_verified_at if membership is not None else None
        allowed_membership_statuses = {
            "member_verified",
            "admin_verified",
            "skipped_already_member",
        }
        if membership is None:
            reasons.append("membership_missing")
        elif (
            membership.resource_type != "user"
            or membership.resource_id != int(account_id)
            or membership_status not in allowed_membership_statuses
        ):
            reasons.append("membership_not_verified")

        identity_unbound = (
            membership is not None
            and membership.resource_type == "user"
            and membership.resource_id == int(account_id)
            and membership_status in allowed_membership_statuses
            and membership.telegram_user_id is None
        )
        if identity_unbound:
            membership.status = "unknown_needs_reconcile"
            membership.updated_at = now
            membership_status = str(_value(membership.status))
            reasons.append("telegram_identity_unbound")
            await self.db.flush()

        stale = (
            membership is not None
            and membership_status in allowed_membership_statuses
            and (last_verified_at is None or last_verified_at < now - timedelta(hours=24))
        )
        if stale and probe_stale and membership is not None and not reasons:
            probe_result = await self._probe_stale_membership(
                asset_id=asset_id,
                account=account,
                membership=membership,
            )
            if probe_result.verified:
                last_verified_at = membership.last_verified_at
                stale = False
            elif probe_result.reason_code == "membership_not_verified":
                reasons.append("membership_not_verified")
            elif probe_result.reason_code == "telegram_identity_mismatch":
                membership_status = str(_value(membership.status))
                stale = False
                reasons.append("telegram_identity_mismatch")
            elif probe_result.reason_code in {
                "telegram_identity_unbound",
                "telegram_user_id_missing",
            }:
                membership_status = str(_value(membership.status))
                stale = False
                reasons.append("telegram_identity_unbound")
            else:
                reasons.append("membership_probe_failed")
        if stale:
            reasons.append("membership_verification_stale")

        return AccountEligibility(
            account_id=int(account.id),
            display_name=account.display_name or account.identifier,
            status=str(_value(account.status)),
            risk_level=str(_value(account.risk_level)),
            operation_mode=operation_mode,
            membership_status=membership_status,
            last_verified_at=last_verified_at,
            eligible=not reasons,
            blocking_reasons=tuple(reasons),
            policy_id=int(policy_id) if policy_id is not None else None,
            probe_required=bool(stale),
        )

    async def list_account_eligibility(
        self,
        asset_id: int,
        *,
        probe_stale: bool = False,
    ) -> list[AccountEligibility]:
        # Include every promoter account, including disabled/risky/ad-only
        # rows, so the UI can explain why each candidate is blocked.
        ids = list(
            (
                await self.db.scalars(
                    select(TelegramAccount.id)
                    .where(TelegramAccount.account_type == AccountType.PROMOTER)
                    .order_by(TelegramAccount.id)
                )
            ).all()
        )
        return [
            await self.validate_account_eligibility(
                asset_id,
                int(account_id),
                probe_stale=probe_stale,
            )
            for account_id in ids
        ]
