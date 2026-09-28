"""Durable quota and eligibility guard for external promotional joins."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountOperationConfig,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    parse_telegram_group_link,
)
from app.core.automation_settings import get_auto_join_scheduler_settings
from app.core.group.identity import telegram_group_id_aliases
from app.core.group.models import Group, GroupAccountMembership
from app.core.operating_time import operating_day_start
from app.modules.acquisition.dynamic_frequency import AccountDynamicFrequencyService
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus
from app.modules.owned_group.models import OwnedGroupAsset

JOIN_REQUEST_HARD_CAP = 30
JOIN_REQUEST_MIN_INTERVAL_SECONDS = 48 * 60
JOIN_RESERVATION_TTL_SECONDS = 10 * 60
JOIN_ACCOUNT_MIN_AGE_DAYS = 180
JOIN_REVIEW_BACKLOG_HIGH_WATERMARK = 30
JOIN_REVIEW_BACKLOG_LOW_WATERMARK = 20
JOIN_REVIEW_OVERDUE_GRACE_SECONDS = 60 * 60
JOIN_REVIEW_INITIAL_DUE_SECONDS = 2 * 60 * 60

_COUNTED_REQUEST_STATES = ("sent", "outcome_unknown")
_WAITING_REVIEW_STATES = (
    "initial_pending",
    "review_2h",
    "final_pending",
    "exit_pending",
    "leave_failed",
    "manual_required",
)
_LIVE_MEMBERSHIP_STATES = ("joined", "pending", "leave_failed")


class JoinBudgetBlocked(RuntimeError):
    """Stable business error raised before any Telegram join request is sent."""

    def __init__(
        self,
        reason: str,
        *,
        retry_after_seconds: int | None = None,
        status: JoinBudgetStatus | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.status = status


@dataclass(frozen=True)
class JoinBudgetStatus:
    configured_limit: int
    effective_limit: int
    requested_today: int
    requested_rolling_24h: int
    active_reservations: int
    pending_review_count: int
    overdue_review_count: int
    next_allowed_at: datetime | None
    blocked_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured_limit": self.configured_limit,
            "effective_limit": self.effective_limit,
            "requested_today": self.requested_today,
            "requested_rolling_24h": self.requested_rolling_24h,
            "active_reservations": self.active_reservations,
            "pending_review_count": self.pending_review_count,
            "overdue_review_count": self.overdue_review_count,
            "next_allowed_at": self.next_allowed_at.isoformat() if self.next_allowed_at else None,
            "blocked_reasons": list(self.blocked_reasons),
        }


class JoinRequestBudgetService:
    """Persist reservations and count only requests marked at the Telegram boundary."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    async def _global_join_enabled(self) -> bool:
        settings = await get_auto_join_scheduler_settings(self.db)
        return bool(settings.get("enabled"))

    async def _is_owned_group_owner(self, account_id: int) -> bool:
        row = await self.db.execute(
            select(OwnedGroupAsset.id)
            .where(OwnedGroupAsset.owner_account_id == account_id)
            .limit(1)
        )
        return row.scalar_one_or_none() is not None

    async def _lock_account_and_config(
        self,
        account_id: int,
    ) -> tuple[TelegramAccount | None, AccountOperationConfig | None]:
        """Serialize every reservation/send transition for one account."""

        account = (
            await self.db.execute(
                select(TelegramAccount)
                .where(TelegramAccount.id == account_id)
                .with_for_update(of=TelegramAccount)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        config = (
            await self.db.execute(
                select(AccountOperationConfig)
                .where(AccountOperationConfig.account_id == account_id)
                .with_for_update(of=AccountOperationConfig)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return account, config

    async def eligibility_reason(
        self,
        account: TelegramAccount | None,
        config: AccountOperationConfig | None,
        now: datetime,
        *,
        require_auto_join_enabled: bool,
        global_enabled: bool | None = None,
    ) -> str | None:
        if global_enabled is None:
            global_enabled = await self._global_join_enabled()
        if not global_enabled:
            return "global_auto_join_paused"
        if account is None or config is None:
            return "account_operation_config_missing"
        if (
            not account.is_active
            or self._enum_value(account.account_type) != AccountType.PROMOTER.value
        ):
            return "account_not_eligible"
        if self._enum_value(account.status) in {
            AccountStatus.ERROR.value,
            AccountStatus.BANNED.value,
            AccountStatus.RESTRICTED.value,
        }:
            return "account_status_unavailable"
        if self._enum_value(account.risk_level) not in {
            AccountRiskLevel.NORMAL.value,
            AccountRiskLevel.WATCH.value,
        }:
            return "account_risk_level_not_allowed"
        if account.risk_pause_until and account.risk_pause_until > now:
            return "account_risk_paused"
        if not config.enabled:
            return "account_operation_disabled"
        if require_auto_join_enabled and not config.auto_join_enabled:
            return "account_auto_join_disabled"
        if int(config.max_groups_per_day or 0) <= 0:
            return "account_join_limit_zero"
        from app.core.account.age import account_age_eligibility_reason
        reason = account_age_eligibility_reason(account, now)
        if reason:
            return reason
        if getattr(config, "dynamic_capacity_enabled", False):
            from app.core.account.outbound_budget import effective_capacity_limits
            limits = await effective_capacity_limits(self.db, account, config, now)
            if limits["action_pauses"].get("join"):
                return "join_flood_wait"
        if await self._is_owned_group_owner(account.id):
            return "owned_group_owner_protected"
        if getattr(config, "dynamic_capacity_enabled", False):
            from app.modules.acquisition.ad_output_plan import ad_output_plan
            plan = await ad_output_plan(self.db, account, config, now)
            if plan["join_blocker"]:
                return plan["join_blocker"]
        return None

    @staticmethod
    def _watermarks(config: Any) -> tuple[int, int]:
        return (12, 6) if getattr(config, "dynamic_capacity_enabled", False) else (30, 20)

    async def _minimum_interval(self, account: Any, config: Any, now: datetime) -> int:
        if getattr(config, "dynamic_capacity_enabled", False):
            from app.core.account.outbound_budget import effective_capacity_limits
            return (await effective_capacity_limits(self.db, account, config, now))["join_interval_seconds"]
        return JOIN_REQUEST_MIN_INTERVAL_SECONDS

    @staticmethod
    def _total_limit(config: Any) -> int:
        value = max(0, int(config.max_groups_total or 0))
        return min(300, value or 300) if getattr(config, "dynamic_capacity_enabled", False) else value

    async def _effective_limit(
        self,
        config: AccountOperationConfig,
        now: datetime,
        *,
        persist_state: bool = False,
    ) -> int:
        configured = min(JOIN_REQUEST_HARD_CAP, max(0, int(config.max_groups_per_day or 0)))
        if configured <= 0:
            return 0
        dynamic = await AccountDynamicFrequencyService(self.db).auto_join_dynamic_daily_limit(
            config,
            now,
            persist_state=persist_state,
        )
        return min(configured, max(0, int(dynamic)))

    @staticmethod
    def normalize_target_key(
        *,
        group_id: int | None = None,
        telegram_group_id: int | None = None,
        group_username: str | None = None,
        target_key: str | None = None,
    ) -> str | None:
        username = (group_username or "").strip().lstrip("@").lower()
        if username:
            return f"username:{username}"
        raw = (target_key or "").strip().rstrip("/")
        if raw:
            lowered = raw.lower()
            if lowered.startswith("username:"):
                value = raw.split(":", 1)[1].strip().lstrip("@").lower()
                return f"username:{value}" if value else None
            if lowered.startswith("telegram:"):
                try:
                    return f"telegram:{int(raw.split(':', 1)[1])}"
                except (TypeError, ValueError):
                    return None
            if lowered.startswith("group:"):
                try:
                    return f"group:{int(raw.split(':', 1)[1])}"
                except (TypeError, ValueError):
                    return None
            if lowered.startswith("invite:"):
                invite_hash = raw.split(":", 1)[1].strip()
                return f"invite:{invite_hash}"[:500] if invite_hash else None
            if lowered.startswith("link:"):
                raw = raw.split(":", 1)[1].strip()
            try:
                parsed = parse_telegram_group_link(raw)
            except TelegramExecutionError:
                return f"link:{raw.lower()}"[:500]
            if parsed.kind == "public":
                return f"username:{parsed.target.lower()}"
            return f"invite:{parsed.target}"[:500]
        if telegram_group_id is not None:
            return f"telegram:{int(telegram_group_id)}"
        if group_id is not None:
            return f"group:{int(group_id)}"
        return None

    async def _unresolved_target_exists(
        self,
        account_id: int,
        target_key: str | None,
        *,
        exclude_attempt_id: int | None = None,
        group_id: int | None = None,
        telegram_group_id: int | None = None,
        group_username: str | None = None,
    ) -> bool:
        target_key = target_key or ""
        target_predicates = [AutoJoinAttempt.target_key == target_key] if target_key else []
        group_predicates = []
        if group_id is not None:
            group_predicates.append(Group.id == group_id)
        aliases = telegram_group_id_aliases(telegram_group_id)
        username = (group_username or "").strip().lstrip("@").lower()
        if target_key.startswith("username:"):
            username = target_key.split(":", 1)[1]
        elif target_key.startswith("telegram:"):
            try:
                aliases.update(telegram_group_id_aliases(int(target_key.split(":", 1)[1])))
            except ValueError:
                pass
        elif target_key.startswith("group:"):
            try:
                group_predicates.append(Group.id == int(target_key.split(":", 1)[1]))
            except ValueError:
                pass
        if username:
            group_predicates.append(func.lower(Group.username) == username)
            target_predicates.extend([
                func.lower(AutoJoinAttempt.group_username) == username,
                AutoJoinAttempt.target_key == f"username:{username}",
            ])
        if aliases:
            group_predicates.append(Group.group_id.in_(aliases))
        if group_predicates:
            groups = (await self.db.scalars(select(Group).where(or_(*group_predicates)))).all()
            group_ids = {item.id for item in groups}
            if group_id is not None:
                group_ids.add(group_id)
            for item in groups:
                aliases.update(telegram_group_id_aliases(item.group_id))
            if group_ids:
                target_predicates.extend([
                    AutoJoinAttempt.group_id.in_(group_ids),
                    AutoJoinAttempt.target_key.in_([f"group:{value}" for value in group_ids]),
                ])
        if aliases:
            target_predicates.extend([
                AutoJoinAttempt.telegram_group_id.in_(aliases),
                AutoJoinAttempt.target_key.in_([f"telegram:{value}" for value in aliases]),
            ])
        if not target_predicates:
            return False
        query = select(AutoJoinAttempt.id).where(
            AutoJoinAttempt.account_id == account_id,
            or_(*target_predicates),
            or_(
                AutoJoinAttempt.request_state == "outcome_unknown",
                (
                    (AutoJoinAttempt.request_state == "sent")
                    & (AutoJoinAttempt.status == DeliveryStatus.PENDING.value)
                ),
            ),
        )
        if exclude_attempt_id is not None:
            query = query.where(AutoJoinAttempt.id != exclude_attempt_id)
        return (await self.db.execute(query.limit(1))).scalar_one_or_none() is not None

    async def _live_membership_count(self, account_id: int) -> int:
        config = await self.db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
        membership_scope = GroupAccountMembership.status.in_(_LIVE_MEMBERSHIP_STATES)
        if not getattr(config, "dynamic_capacity_enabled", False):
            membership_scope = or_(membership_scope, GroupAccountMembership.review_status.in_(
                ("exit_pending", "leave_failed", "manual_required")))
        value = await self.db.scalar(
            select(func.count(GroupAccountMembership.id)).where(
                GroupAccountMembership.account_id == account_id,
                membership_scope,
            )
        )
        return int(value or 0)

    async def _quota_next_allowed_at(
        self,
        account_id: int,
        now: datetime,
        *,
        today: int,
        rolling: int,
        reserved: int,
        effective_limit: int,
    ) -> datetime | None:
        if effective_limit <= 0:
            return None
        day_start = operating_day_start(now)
        rolling_start = now - timedelta(hours=24)
        oldest_rolling, first_reservation_expiry = (
            await self.db.execute(
                select(
                    func.min(AutoJoinAttempt.request_sent_at).filter(
                        AutoJoinAttempt.request_state.in_(_COUNTED_REQUEST_STATES),
                        AutoJoinAttempt.request_sent_at >= rolling_start,
                    ),
                    func.min(AutoJoinAttempt.reservation_expires_at).filter(
                        AutoJoinAttempt.request_state == "reserved",
                        AutoJoinAttempt.reservation_expires_at > now,
                    ),
                ).where(AutoJoinAttempt.account_id == account_id)
            )
        ).one()
        candidates: list[datetime] = []
        if today >= effective_limit:
            candidates.append(day_start + timedelta(days=1))
        elif today + reserved >= effective_limit and first_reservation_expiry is not None:
            candidates.append(first_reservation_expiry)
        if rolling >= effective_limit and oldest_rolling is not None:
            candidates.append(oldest_rolling + timedelta(hours=24))
        elif rolling + reserved >= effective_limit and first_reservation_expiry is not None:
            candidates.append(first_reservation_expiry)
        return max(candidates) if candidates else None

    async def _request_counts(
        self,
        account_id: int,
        now: datetime,
    ) -> tuple[int, int, int, datetime | None]:
        day_start = operating_day_start(now)
        rolling_start = now - timedelta(hours=24)
        row = await self.db.execute(
            select(
                func.count(AutoJoinAttempt.id)
                .filter(
                    AutoJoinAttempt.request_state.in_(_COUNTED_REQUEST_STATES),
                    AutoJoinAttempt.request_sent_at >= day_start,
                )
                .label("today"),
                func.count(AutoJoinAttempt.id)
                .filter(
                    AutoJoinAttempt.request_state.in_(_COUNTED_REQUEST_STATES),
                    AutoJoinAttempt.request_sent_at >= rolling_start,
                )
                .label("rolling"),
                func.count(AutoJoinAttempt.id)
                .filter(
                    AutoJoinAttempt.request_state == "reserved",
                    AutoJoinAttempt.reservation_expires_at > now,
                )
                .label("reserved"),
                func.max(AutoJoinAttempt.request_sent_at).label("last_sent_at"),
            ).where(AutoJoinAttempt.account_id == account_id)
        )
        today, rolling, reserved, last_sent_at = row.one()
        return int(today or 0), int(rolling or 0), int(reserved or 0), last_sent_at

    async def _review_backlog(
        self,
        account_id: int,
        now: datetime,
    ) -> tuple[int, int]:
        config = await self.db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
        if config is not None and getattr(config, "dynamic_capacity_enabled", False):
            from app.modules.acquisition.capacity import inventory_snapshot
            inventory = await inventory_snapshot(self.db, account_id, now)
            return inventory["active_backlog"], inventory["overdue"]
        rows = await self.db.execute(
            select(
                GroupAccountMembership.review_status,
                GroupAccountMembership.review_next_at,
            ).where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.review_status.in_(_WAITING_REVIEW_STATES),
            )
        )
        pending = 0
        overdue = 0
        overdue_cutoff = now - timedelta(seconds=JOIN_REVIEW_OVERDUE_GRACE_SECONDS)
        for _review_status, due_at in rows:
            pending += 1
            if due_at is not None and due_at < overdue_cutoff:
                overdue += 1
        unresolved_rows = await self.db.execute(
            select(
                AutoJoinAttempt.request_state,
                AutoJoinAttempt.request_sent_at,
            ).where(
                AutoJoinAttempt.account_id == account_id,
                or_(
                    AutoJoinAttempt.request_state == "outcome_unknown",
                    (
                        (AutoJoinAttempt.request_state == "sent")
                        & (AutoJoinAttempt.status == DeliveryStatus.PENDING.value)
                        & (AutoJoinAttempt.group_id.is_(None))
                    ),
                ),
            )
        )
        unresolved_overdue_cutoff = now - timedelta(
            seconds=JOIN_REVIEW_INITIAL_DUE_SECONDS
            + JOIN_REVIEW_OVERDUE_GRACE_SECONDS
        )
        for _request_state, sent_at in unresolved_rows:
            pending += 1
            if sent_at is not None and sent_at < unresolved_overdue_cutoff:
                overdue += 1
        return pending, overdue

    async def refresh_review_backlog_pause(
        self,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> tuple[int, int, bool]:
        """Persist the 30/20 hysteresis before the scheduler performs a read check."""

        now = now or datetime.utcnow()
        config = (
            await self.db.execute(
                select(AccountOperationConfig)
                .where(AccountOperationConfig.account_id == account_id)
                .with_for_update(of=AccountOperationConfig)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if config is None:
            return 0, 0, False
        pending, overdue = await self._review_backlog(account_id, now)
        paused = pending >= self._watermarks(config)[0] or (
            bool(config.join_review_backlog_paused)
            and pending > self._watermarks(config)[1]
        )
        config.join_review_backlog_paused = paused
        config.join_review_backlog_reason = (
            "join_review_overdue"
            if overdue > 0 and not getattr(config, "dynamic_capacity_enabled", False)
            else ("join_review_backlog" if paused else None)
        )
        await self.db.commit()
        return pending, overdue, paused

    async def status(
        self,
        account_id: int,
        *,
        now: datetime | None = None,
        require_auto_join_enabled: bool = True,
    ) -> JoinBudgetStatus:
        now = now or datetime.utcnow()
        account = await self.db.get(TelegramAccount, account_id)
        config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == account_id
                )
            )
        ).scalar_one_or_none()
        global_enabled = await self._global_join_enabled()
        configured = min(
            JOIN_REQUEST_HARD_CAP,
            max(0, int(getattr(config, "max_groups_per_day", 0) or 0)),
        )
        effective = (
            await self._effective_limit(config, now, persist_state=False)
            if config is not None
            else 0
        )
        today, rolling, reserved, last_sent_at = await self._request_counts(account_id, now)
        pending_review, overdue_review = await self._review_backlog(account_id, now)
        interval_seconds = max(
            await self._minimum_interval(account, config, now),
            int(getattr(config, "join_interval_min_seconds", 0) or 0),
        )
        next_allowed_at = getattr(config, "next_join_after", None)
        if last_sent_at is not None:
            request_interval_at = last_sent_at + timedelta(seconds=interval_seconds)
            next_allowed_at = max(next_allowed_at or request_interval_at, request_interval_at)
        interval_blocked = next_allowed_at is not None and next_allowed_at > now
        if account is not None and account.risk_pause_until and account.risk_pause_until > now:
            next_allowed_at = max(
                next_allowed_at or account.risk_pause_until,
                account.risk_pause_until,
            )
        quota_next_at = await self._quota_next_allowed_at(
            account_id,
            now,
            today=today,
            rolling=rolling,
            reserved=reserved,
            effective_limit=effective,
        )
        if quota_next_at is not None:
            next_allowed_at = max(next_allowed_at or quota_next_at, quota_next_at)

        reasons: list[str] = []
        eligibility = await self.eligibility_reason(
            account,
            config,
            now,
            require_auto_join_enabled=require_auto_join_enabled,
            global_enabled=global_enabled,
        )
        if eligibility:
            reasons.append(eligibility)
        if effective <= 0 and "account_join_limit_zero" not in reasons:
            reasons.append("account_dynamic_health_paused")
        if today + reserved >= effective > 0:
            reasons.append("daily_join_quota")
        if rolling + reserved >= effective > 0:
            reasons.append("rolling_24h_join_quota")
        backlog_paused = bool(getattr(config, "join_review_backlog_paused", False))
        if pending_review >= self._watermarks(config)[0] or (
            backlog_paused and pending_review > self._watermarks(config)[1]
        ):
            reasons.append("join_review_backlog")
        if overdue_review > 0 and not getattr(config, "dynamic_capacity_enabled", False):
            reasons.append("join_review_overdue")
        if interval_blocked:
            reasons.append("join_interval")
        if config is not None:
            total_memberships = await self._live_membership_count(account_id)
            total_limit = self._total_limit(config)
            if total_limit > 0 and total_memberships + reserved >= total_limit:
                reasons.append("total_group_quota")
        return JoinBudgetStatus(
            configured_limit=configured,
            effective_limit=effective,
            requested_today=today,
            requested_rolling_24h=rolling,
            active_reservations=reserved,
            pending_review_count=pending_review,
            overdue_review_count=overdue_review,
            next_allowed_at=next_allowed_at,
            blocked_reasons=list(dict.fromkeys(reasons)),
        )

    async def reserve(
        self,
        account_id: int,
        *,
        group_id: int | None = None,
        telegram_group_id: int | None = None,
        group_username: str | None = None,
        group_title: str | None = None,
        source_keyword: str | None = None,
        target_key: str | None = None,
        source: str,
        require_auto_join_enabled: bool = True,
        now: datetime | None = None,
    ) -> AutoJoinAttempt:
        now = now or datetime.utcnow()
        status = await self.status(
            account_id,
            now=now,
            require_auto_join_enabled=require_auto_join_enabled,
        )

        account, config = await self._lock_account_and_config(account_id)
        if config is None:
            raise JoinBudgetBlocked("account_operation_config_missing", status=status)

        stale = await self.db.execute(
            select(AutoJoinAttempt)
            .where(
                AutoJoinAttempt.account_id == account_id,
                AutoJoinAttempt.request_state == "reserved",
                AutoJoinAttempt.reservation_expires_at <= now,
            )
            .with_for_update(of=AutoJoinAttempt)
        )
        for item in stale.scalars().all():
            item.request_state = "released"
            item.reservation_released_at = now
            item.reason = item.reason or "reservation_expired_before_send"

        pending_review, overdue_review = await self._review_backlog(account_id, now)
        backlog_paused = pending_review >= self._watermarks(config)[0] or (
            bool(config.join_review_backlog_paused)
            and pending_review > self._watermarks(config)[1]
        )
        config.join_review_backlog_paused = backlog_paused
        config.join_review_backlog_reason = (
            "join_review_overdue"
            if overdue_review > 0 and not getattr(config, "dynamic_capacity_enabled", False)
            else ("join_review_backlog" if backlog_paused else None)
        )

        global_enabled = await self._global_join_enabled()
        today, rolling, reserved, last_sent_at = await self._request_counts(account_id, now)
        effective_limit = min(
            await self._effective_limit(config, now, persist_state=False),
            JOIN_REQUEST_HARD_CAP,
            max(0, int(config.max_groups_per_day or 0)),
        )
        interval_seconds = max(
            await self._minimum_interval(account, config, now),
            int(config.join_interval_min_seconds or 0),
        )
        next_allowed_at = config.next_join_after
        if last_sent_at is not None:
            request_interval_at = last_sent_at + timedelta(seconds=interval_seconds)
            next_allowed_at = max(next_allowed_at or request_interval_at, request_interval_at)
        reasons: list[str] = []
        eligibility = await self.eligibility_reason(
            account,
            config,
            now,
            require_auto_join_enabled=require_auto_join_enabled,
            global_enabled=global_enabled,
        )
        if eligibility:
            reasons.append(eligibility)
        if effective_limit <= 0 and "account_join_limit_zero" not in reasons:
            reasons.append("account_dynamic_health_paused")
        if today + reserved >= effective_limit > 0:
            reasons.append("daily_join_quota")
        if rolling + reserved >= effective_limit > 0:
            reasons.append("rolling_24h_join_quota")
        if backlog_paused:
            reasons.append("join_review_backlog")
        if overdue_review > 0 and not getattr(config, "dynamic_capacity_enabled", False):
            reasons.append("join_review_overdue")
        if next_allowed_at is not None and next_allowed_at > now:
            reasons.append("join_interval")
        normalized_target_key = self.normalize_target_key(
            group_id=group_id,
            telegram_group_id=telegram_group_id,
            group_username=group_username,
            target_key=target_key,
        )
        if await self._unresolved_target_exists(
            account_id, normalized_target_key, group_id=group_id,
            telegram_group_id=telegram_group_id, group_username=group_username,
        ):
            reasons.append("join_outcome_reconciliation_required")
        total_memberships = await self._live_membership_count(account_id)
        total_limit = self._total_limit(config)
        if total_limit > 0 and total_memberships + reserved >= total_limit:
            reasons.append("total_group_quota")
        refreshed = JoinBudgetStatus(
            configured_limit=status.configured_limit,
            effective_limit=effective_limit,
            requested_today=today,
            requested_rolling_24h=rolling,
            active_reservations=reserved,
            pending_review_count=pending_review,
            overdue_review_count=overdue_review,
            next_allowed_at=next_allowed_at,
            blocked_reasons=list(dict.fromkeys(reasons)),
        )
        if refreshed.blocked_reasons:
            await self.db.commit()
            retry_after = None
            if refreshed.next_allowed_at and refreshed.next_allowed_at > now:
                retry_after = max(
                    1, int((refreshed.next_allowed_at - now).total_seconds())
                )
            raise JoinBudgetBlocked(
                refreshed.blocked_reasons[0],
                retry_after_seconds=retry_after,
                status=refreshed,
            )

        attempt = AutoJoinAttempt(
            account_id=account_id,
            group_id=group_id,
            telegram_group_id=telegram_group_id,
            group_username=(group_username or "").lstrip("@") or None,
            group_title=group_title,
            source_keyword=source_keyword,
            status=DeliveryStatus.PENDING.value,
            reason=f"join_request_reserved:{source}"[:255],
            telegram_action_attempted=False,
            request_state="reserved",
            reservation_key=uuid.uuid4().hex,
            target_key=normalized_target_key,
            require_auto_join_enabled=require_auto_join_enabled,
            reserved_at=now,
            reservation_expires_at=now + timedelta(seconds=JOIN_RESERVATION_TTL_SECONDS),
            attempted_at=now,
        )
        self.db.add(attempt)
        await self.db.commit()
        await self.db.refresh(attempt)
        return attempt

    async def mark_sent(
        self,
        attempt: AutoJoinAttempt,
        *,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.utcnow()
        # Keep the same lock order as reserve(): account, config, then attempt.
        account, config = await self._lock_account_and_config(attempt.account_id)
        row = (
            await self.db.execute(
                select(AutoJoinAttempt)
                .where(AutoJoinAttempt.id == attempt.id)
                .with_for_update(of=AutoJoinAttempt)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if row.request_state != "reserved":
            raise JoinBudgetBlocked("join_reservation_not_active")
        if row.reservation_expires_at is None or row.reservation_expires_at <= now:
            row.request_state = "released"
            row.reservation_released_at = now
            row.reason = "reservation_expired_before_send"
            await self.db.commit()
            raise JoinBudgetBlocked("join_reservation_expired")
        global_enabled = await self._global_join_enabled()
        eligibility = await self.eligibility_reason(
            account,
            config,
            now,
            require_auto_join_enabled=bool(row.require_auto_join_enabled),
            global_enabled=global_enabled,
        )
        if eligibility:
            raise JoinBudgetBlocked(eligibility)
        if await self._unresolved_target_exists(
            row.account_id,
            row.target_key,
            exclude_attempt_id=row.id,
            group_id=row.group_id,
            telegram_group_id=row.telegram_group_id,
            group_username=row.group_username,
        ):
            raise JoinBudgetBlocked("join_outcome_reconciliation_required")
        pending_review, overdue_review = await self._review_backlog(row.account_id, now)
        backlog_paused = pending_review >= self._watermarks(config)[0] or (
            bool(config.join_review_backlog_paused)
            and pending_review > self._watermarks(config)[1]
        )
        config.join_review_backlog_paused = backlog_paused
        config.join_review_backlog_reason = (
            "join_review_overdue"
            if overdue_review > 0 and not getattr(config, "dynamic_capacity_enabled", False)
            else ("join_review_backlog" if backlog_paused else None)
        )
        if backlog_paused:
            raise JoinBudgetBlocked("join_review_backlog")
        if overdue_review > 0 and not getattr(config, "dynamic_capacity_enabled", False):
            raise JoinBudgetBlocked("join_review_overdue")
        total_limit = self._total_limit(config)
        if total_limit > 0 and await self._live_membership_count(row.account_id) >= total_limit:
            raise JoinBudgetBlocked("total_group_quota")
        today, rolling, _reserved, last_sent_at = await self._request_counts(
            row.account_id,
            now,
        )
        effective_limit = await self._effective_limit(config, now, persist_state=False)
        if effective_limit <= 0:
            raise JoinBudgetBlocked("account_dynamic_health_paused")
        if today >= effective_limit:
            raise JoinBudgetBlocked("daily_join_quota")
        if rolling >= effective_limit:
            raise JoinBudgetBlocked("rolling_24h_join_quota")
        interval_seconds = max(
            await self._minimum_interval(account, config, now),
            int(config.join_interval_min_seconds or 0),
        )
        next_allowed_at = config.next_join_after
        if last_sent_at is not None:
            interval_at = last_sent_at + timedelta(seconds=interval_seconds)
            next_allowed_at = max(next_allowed_at or interval_at, interval_at)
        if next_allowed_at is not None and next_allowed_at > now:
            raise JoinBudgetBlocked(
                "join_interval",
                retry_after_seconds=max(1, int((next_allowed_at - now).total_seconds())),
            )
        row.request_state = "sent"
        row.telegram_action_attempted = True
        row.request_sent_at = now
        row.reservation_expires_at = None
        row.reason = "join_request_sent"
        if config is not None:
            interval_at = now + timedelta(seconds=interval_seconds)
            config.next_join_after = max(config.next_join_after or interval_at, interval_at)
        await self.db.commit()
        attempt.request_state = row.request_state
        attempt.telegram_action_attempted = True
        attempt.request_sent_at = now

    async def release(
        self,
        attempt: AutoJoinAttempt,
        *,
        reason: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> None:
        now = now or datetime.utcnow()
        await self.db.flush()
        row = (
            await self.db.execute(
                select(AutoJoinAttempt)
                .where(AutoJoinAttempt.id == attempt.id)
                .with_for_update(of=AutoJoinAttempt)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None or row.request_state != "reserved":
            return
        row.request_state = "released"
        row.reservation_released_at = now
        row.reservation_expires_at = None
        row.reason = reason[:255]
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        attempt.request_state = row.request_state
        attempt.reservation_released_at = now

    async def finalize(
        self,
        attempt: AutoJoinAttempt,
        *,
        status: DeliveryStatus,
        reason: str | None = None,
        error: str | None = None,
        joined_at: datetime | None = None,
        group_id: int | None = None,
        telegram_group_id: int | None = None,
        group_username: str | None = None,
        group_title: str | None = None,
        outcome_unknown: bool = False,
        commit: bool = True,
    ) -> AutoJoinAttempt:
        await self.db.flush()
        row = (
            await self.db.execute(
                select(AutoJoinAttempt)
                .where(AutoJoinAttempt.id == attempt.id)
                .with_for_update(of=AutoJoinAttempt)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError("join reservation disappeared")
        row.status = status.value
        row.reason = reason
        row.error = error
        row.joined_at = joined_at
        row.group_id = group_id if group_id is not None else row.group_id
        row.telegram_group_id = (
            telegram_group_id if telegram_group_id is not None else row.telegram_group_id
        )
        row.group_username = (
            (group_username or "").lstrip("@") or row.group_username
        )
        row.group_title = group_title or row.group_title
        if outcome_unknown and row.telegram_action_attempted:
            row.request_state = "outcome_unknown"
        elif status == DeliveryStatus.SUCCESS and row.telegram_action_attempted:
            row.request_state = "sent"
            row.reconciliation_next_at = None
        elif row.request_state == "reserved" and not row.telegram_action_attempted:
            row.request_state = "released"
            row.reservation_released_at = datetime.utcnow()
            row.reservation_expires_at = None
        if commit:
            await self.db.commit()
            await self.db.refresh(row)
        else:
            await self.db.flush()
        return row
