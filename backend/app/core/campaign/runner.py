"""Unified campaign execution runner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

import structlog
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignExecution,
    CampaignExecutionStatus,
    CampaignScope,
    CampaignTracking,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.config import settings
from app.core.user.models import User, UserState
from app.integrations.telegram.client import TelegramClient, TelegramConfig

if TYPE_CHECKING:
    from app.modules.guardian.coupon.coupon_distributor import DistributeResult

logger = structlog.get_logger()


@dataclass(slots=True)
class CampaignExecutionResult:
    """Execution summary shared by conversion and group campaigns."""

    campaign_id: int
    campaign_name: str
    campaign_scope: str
    triggered: bool
    delivered: bool
    reward_granted: bool
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    status: str = CampaignExecutionStatus.SKIPPED.value
    reason: Optional[str] = None
    execution_id: Optional[int] = None


class CampaignRunner:
    """Execute conversion campaigns according to trigger timing."""

    def __init__(self, db: AsyncSession, xboard_client=None):
        from app.modules.guardian.coupon.coupon_distributor import CouponDistributor

        self.db = db
        self.coupon_distributor = CouponDistributor(db, xboard_client)
        self.logger = logger.bind(module="campaign_runner")

    async def trigger_for_registration(
        self,
        user: User,
        *,
        occurred_at: Optional[datetime] = None,
        metadata: Optional[dict] = None,
    ) -> list[CampaignExecutionResult]:
        """Trigger conversion campaigns after a real registration event."""
        timings = [
            CampaignTriggerTiming.AFTER_REGISTER,
            CampaignTriggerTiming.IMMEDIATE,
            CampaignTriggerTiming.DELAYED,
        ]
        rows = await self.db.execute(
            select(Campaign)
            .where(
                and_(
                    Campaign.enabled == True,  # noqa: E712
                    Campaign.campaign_scope == CampaignScope.GLOBAL,
                    Campaign.trigger_timing.in_(timings),
                )
            )
            .order_by(Campaign.created_at.asc())
        )
        results: list[CampaignExecutionResult] = []
        for campaign in rows.scalars().all():
            results.append(
                await self.trigger_campaign(
                    campaign=campaign,
                    user=user,
                    now=occurred_at,
                    metadata=metadata,
                )
            )
        return results

    async def trigger_campaign(
        self,
        campaign: Campaign,
        *,
        user: Optional[User] = None,
        user_id: Optional[int] = None,
        now: Optional[datetime] = None,
        metadata: Optional[dict] = None,
        manual: bool = False,
    ) -> CampaignExecutionResult | list[CampaignExecutionResult]:
        """Trigger a campaign using trigger_timing as the execution source."""
        current = now or datetime.utcnow()
        mode = self.resolve_distribution_mode(campaign)
        metadata = metadata or {}

        if not campaign.enabled:
            return self._result(campaign, False, False, False, "campaign_disabled")

        if mode == CampaignDistributionMode.DELAYED:
            target_users = await self._load_target_users(campaign, user=user, user_id=user_id)
            return await self._schedule_for_users(campaign, target_users, current, metadata)

        if mode in {CampaignDistributionMode.SCHEDULED, CampaignDistributionMode.PERIODIC} and not manual:
            return self._result(campaign, False, False, False, f"{mode.value}:worker_managed")

        if user is not None or user_id is not None:
            target_user = user or await self.db.get(User, user_id)
            if target_user is None:
                return self._result(campaign, False, False, False, "user_not_found", user_id=user_id)
            return await self._execute_now(campaign, target_user, current, metadata=metadata)

        target_users = await self._load_target_users(campaign)
        return await self._execute_for_users(campaign, target_users, current, metadata=metadata)

    async def process_due_campaigns(self, now: Optional[datetime] = None) -> dict[str, int]:
        """Process delayed, scheduled, and periodic conversion campaigns."""
        current = now or datetime.utcnow()
        result = {"processed": 0, "delivered": 0, "rewarded": 0, "scheduled": 0, "skipped": 0, "failed": 0}

        delayed = await self._process_due_delayed(current)
        for key in result:
            result[key] += delayed.get(key, 0)

        timed = await self._process_timed_campaigns(current)
        for key in result:
            result[key] += timed.get(key, 0)

        return result

    async def _process_due_delayed(self, now: datetime) -> dict[str, int]:
        rows = await self.db.execute(
            select(CampaignExecution, Campaign)
            .join(Campaign, CampaignExecution.campaign_id == Campaign.id)
            .where(
                and_(
                    CampaignExecution.status == CampaignExecutionStatus.PENDING,
                    CampaignExecution.scheduled_at.is_not(None),
                    CampaignExecution.scheduled_at <= now,
                    Campaign.campaign_scope == CampaignScope.GLOBAL,
                    Campaign.enabled == True,  # noqa: E712
                )
            )
            .order_by(CampaignExecution.scheduled_at.asc())
        )
        counts = {"processed": 0, "delivered": 0, "rewarded": 0, "scheduled": 0, "skipped": 0, "failed": 0}

        for execution, campaign in rows.all():
            if execution.user_id is None:
                await self._finish_execution(execution, CampaignExecutionStatus.SKIPPED, now, error="user_required")
                counts["skipped"] += 1
                continue

            user = await self.db.get(User, execution.user_id)
            if user is None:
                await self._finish_execution(execution, CampaignExecutionStatus.SKIPPED, now, error="user_not_found")
                counts["skipped"] += 1
                continue

            item = await self._execute_now(campaign, user, now, execution=execution)
            self._accumulate(counts, item)

        return counts

    async def _process_timed_campaigns(self, now: datetime) -> dict[str, int]:
        rows = await self.db.execute(
            select(Campaign).where(
                and_(
                    Campaign.enabled == True,  # noqa: E712
                    Campaign.campaign_scope == CampaignScope.GLOBAL,
                    Campaign.trigger_timing.in_(
                        [CampaignTriggerTiming.SCHEDULED, CampaignTriggerTiming.PERIODIC]
                    ),
                )
            )
        )
        counts = {"processed": 0, "delivered": 0, "rewarded": 0, "scheduled": 0, "skipped": 0, "failed": 0}

        for campaign in rows.scalars().all():
            should_run, reason = await self._should_run_timed_campaign(campaign, now)
            if not should_run:
                counts["skipped"] += 1
                self.logger.debug(
                    "campaign_timed_skipped",
                    campaign_id=campaign.id,
                    reason=reason,
                )
                continue

            target_users = await self._load_target_users(campaign)
            executions = await self._execute_for_users(campaign, target_users, now)
            for item in executions:
                self._accumulate(counts, item)

            await self._record_campaign_run(campaign, now)

        return counts

    async def _schedule_for_users(
        self,
        campaign: Campaign,
        users: list[User],
        now: datetime,
        metadata: dict,
    ) -> CampaignExecutionResult | list[CampaignExecutionResult]:
        delay_minutes = self._get_delay_minutes(campaign)
        scheduled_at = now + timedelta(minutes=delay_minutes)
        results: list[CampaignExecutionResult] = []

        for user in users:
            eligible, reason = await self._is_user_eligible(campaign, user, include_pending=True)
            if not eligible:
                results.append(self._result(campaign, False, False, False, reason, user_id=user.id))
                continue

            existing = await self._find_existing_execution(
                campaign_id=campaign.id,
                user_id=user.id,
                statuses=[
                    CampaignExecutionStatus.PENDING,
                    CampaignExecutionStatus.RUNNING,
                    CampaignExecutionStatus.COMPLETED,
                ],
            )
            if existing is not None:
                results.append(
                    self._result(campaign, False, False, False, "already_scheduled", user_id=user.id, execution_id=existing.id)
                )
                continue

            await self._get_or_create_tracking(campaign, user.id, None, now, metadata)
            execution = CampaignExecution(
                campaign_id=campaign.id,
                user_id=user.id,
                status=CampaignExecutionStatus.PENDING,
                trigger_timing=self._timing_value(campaign.trigger_timing),
                trigger_event=campaign.trigger_event,
                distribution_mode=CampaignDistributionMode.DELAYED,
                scheduled_at=scheduled_at,
            )
            self.db.add(execution)
            await self.db.commit()
            await self.db.refresh(execution)
            results.append(
                self._result(
                    campaign,
                    True,
                    False,
                    False,
                    "scheduled",
                    user_id=user.id,
                    status=CampaignExecutionStatus.PENDING,
                    execution_id=execution.id,
                )
            )

        return results[0] if len(results) == 1 else results

    async def _execute_for_users(
        self,
        campaign: Campaign,
        users: list[User],
        now: datetime,
        *,
        metadata: Optional[dict] = None,
    ) -> list[CampaignExecutionResult]:
        results: list[CampaignExecutionResult] = []
        for user in users:
            results.append(await self._execute_now(campaign, user, now, metadata=metadata or {}))
        return results

    async def _execute_now(
        self,
        campaign: Campaign,
        user: User,
        now: datetime,
        *,
        metadata: Optional[dict] = None,
        execution: Optional[CampaignExecution] = None,
    ) -> CampaignExecutionResult:
        metadata = metadata or {}
        mode = self.resolve_distribution_mode(campaign)
        execution = execution or CampaignExecution(
            campaign_id=campaign.id,
            user_id=user.id,
            status=CampaignExecutionStatus.RUNNING,
            trigger_timing=self._timing_value(campaign.trigger_timing),
            trigger_event=campaign.trigger_event,
            distribution_mode=mode,
            scheduled_at=now,
        )
        execution.status = CampaignExecutionStatus.RUNNING
        self.db.add(execution)
        await self.db.flush()

        eligible, reason = await self._is_user_eligible(campaign, user)
        if not eligible:
            await self._finish_execution(execution, CampaignExecutionStatus.SKIPPED, now, error=reason)
            return self._result(
                campaign,
                False,
                False,
                False,
                reason,
                user_id=user.id,
                execution_id=execution.id,
                status=CampaignExecutionStatus.SKIPPED,
            )

        try:
            tracking = await self._get_or_create_tracking(campaign, user.id, None, now, metadata)
            reward_result = await self._grant_reward(campaign, user, tracking)
            reward_granted = bool(reward_result and reward_result.success)
            delivered = await self._send_user_message(campaign, user, reward_result)

            if reward_granted:
                tracking.coupon_granted = True
                tracking.validity_started_at = tracking.validity_started_at or now
            tracking.registered_at = tracking.registered_at or now

            execution.delivered = delivered
            execution.reward_granted = reward_granted
            execution.status = CampaignExecutionStatus.COMPLETED
            execution.executed_at = now
            execution.last_run_at = now
            execution.error = None
            await self.db.commit()
            await self.db.refresh(execution)

            self.logger.info(
                "campaign_executed",
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                user_id=user.id,
                delivered=delivered,
                reward_granted=reward_granted,
            )

            return self._result(
                campaign,
                True,
                delivered,
                reward_granted,
                None,
                user_id=user.id,
                execution_id=execution.id,
                status=CampaignExecutionStatus.COMPLETED,
            )
        except Exception as exc:
            await self._finish_execution(execution, CampaignExecutionStatus.FAILED, now, error=str(exc))
            self.logger.error(
                "campaign_execution_failed",
                campaign_id=campaign.id,
                user_id=user.id,
                error=str(exc),
            )
            return self._result(
                campaign,
                True,
                False,
                False,
                str(exc),
                user_id=user.id,
                execution_id=execution.id,
                status=CampaignExecutionStatus.FAILED,
            )

    async def _load_target_users(
        self,
        campaign: Campaign,
        *,
        user: Optional[User] = None,
        user_id: Optional[int] = None,
    ) -> list[User]:
        if user is not None:
            return [user]
        if user_id is not None:
            target = await self.db.get(User, user_id)
            return [target] if target is not None else []

        policy = self._parse_json_dict(campaign.eligibility_policy_json)
        query = select(User).where(User.state != UserState.BLOCKED)

        states = policy.get("target_user_states")
        if isinstance(states, list) and states:
            normalized_states: list[UserState] = []
            for state in states:
                try:
                    normalized_states.append(UserState(str(state)))
                except ValueError:
                    continue
            if normalized_states:
                query = query.where(User.state.in_(normalized_states))

        target_limit = policy.get("target_limit")
        limit = target_limit if isinstance(target_limit, int) and target_limit > 0 else 10_000
        rows = await self.db.execute(query.order_by(User.id.asc()).limit(limit))
        return list(rows.scalars().all())

    async def _is_user_eligible(
        self,
        campaign: Campaign,
        user: User,
        *,
        include_pending: bool = False,
    ) -> tuple[bool, Optional[str]]:
        policy = self._parse_json_dict(campaign.eligibility_policy_json)
        if user.state == UserState.BLOCKED:
            return False, "user_blocked"

        states = policy.get("target_user_states")
        if isinstance(states, list) and states:
            allowed = {str(item) for item in states}
            if user.state.value not in allowed:
                return False, "user_state_not_eligible"

        min_account_age_minutes = policy.get("min_account_age_minutes")
        if isinstance(min_account_age_minutes, int) and min_account_age_minutes > 0:
            if datetime.utcnow() - user.created_at < timedelta(minutes=min_account_age_minutes):
                return False, "account_age_not_met"

        if policy.get("once_per_user"):
            statuses = [CampaignExecutionStatus.COMPLETED]
            if include_pending:
                statuses.extend([CampaignExecutionStatus.PENDING, CampaignExecutionStatus.RUNNING])
            existing = await self._find_existing_execution(campaign.id, user.id, statuses)
            if existing is not None:
                return False, "already_executed"

            tracking = await self._find_tracking(campaign.name, user.id, None)
            if tracking and (tracking.trial_granted or tracking.coupon_granted or tracking.validity_started_at):
                return False, "already_rewarded"

        return True, None

    async def _get_or_create_tracking(
        self,
        campaign: Campaign,
        user_id: int,
        group_id: Optional[int],
        now: datetime,
        metadata: dict,
    ) -> CampaignTracking:
        existing = await self._find_tracking(campaign.name, user_id, group_id)
        if existing is not None:
            return existing

        tracking = CampaignTracking(
            user_id=user_id,
            campaign_name=campaign.name,
            source=metadata.get("source") or f"global:{self._timing_value(campaign.trigger_timing)}",
            group_id=group_id,
            keyword=metadata.get("tracking_code") or metadata.get("keyword"),
            bot_id=metadata.get("bot_id"),
            registered_at=now,
        )
        self.db.add(tracking)
        await self.db.flush()
        return tracking

    async def _find_tracking(
        self,
        campaign_name: str,
        user_id: int,
        group_id: Optional[int],
    ) -> Optional[CampaignTracking]:
        conditions = [
            CampaignTracking.campaign_name == campaign_name,
            CampaignTracking.user_id == user_id,
        ]
        if group_id is None:
            conditions.append(CampaignTracking.group_id.is_(None))
        else:
            conditions.append(CampaignTracking.group_id == group_id)
        row = await self.db.execute(select(CampaignTracking).where(and_(*conditions)))
        return row.scalar_one_or_none()

    async def _find_existing_execution(
        self,
        campaign_id: int,
        user_id: int,
        statuses: list[CampaignExecutionStatus],
    ) -> Optional[CampaignExecution]:
        row = await self.db.execute(
            select(CampaignExecution)
            .where(
                and_(
                    CampaignExecution.campaign_id == campaign_id,
                    CampaignExecution.user_id == user_id,
                    CampaignExecution.status.in_(statuses),
                )
            )
            .order_by(desc(CampaignExecution.id))
            .limit(1)
        )
        return row.scalar_one_or_none()

    async def _grant_reward(
        self,
        campaign: Campaign,
        user: User,
        tracking: CampaignTracking,
    ) -> Optional["DistributeResult"]:
        if tracking.trial_granted or tracking.coupon_granted:
            return None

        if campaign.campaign_type != CampaignType.DISCOUNT:
            return None

        return await self.coupon_distributor.distribute_discount(
            user_id=user.id,
            campaign_id=campaign.id,
            telegram_id=user.telegram_id,
        )

    async def _send_user_message(
        self,
        campaign: Campaign,
        user: User,
        reward_result: Optional["DistributeResult"] = None,
    ) -> bool:
        message = self._resolve_message(campaign, user=user, reward_result=reward_result)
        if not message:
            return False
        if not settings.BOT_TOKEN:
            return False

        client = TelegramClient(TelegramConfig(bot_token=settings.BOT_TOKEN), risk_guard=AccountRiskGuard(self.db))
        execution = TelegramExecutionService(AccountRiskGuard(self.db))
        try:
            await execution.send_bot_message(client, user.telegram_id, message, source="campaign_runner")
            return True
        finally:
            await client.close()

    async def _should_run_timed_campaign(self, campaign: Campaign, now: datetime) -> tuple[bool, str]:
        mode = self.resolve_distribution_mode(campaign)
        policy = self._parse_json_dict(campaign.broadcast_policy_json)
        last_run = await self._get_last_campaign_run(campaign)

        if mode == CampaignDistributionMode.SCHEDULED:
            schedule_times = policy.get("schedule_times") or []
            current_hhmm = now.strftime("%H:%M")
            if current_hhmm not in schedule_times:
                return False, f"scheduled:not_due:{current_hhmm}"
            if last_run and last_run.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M"):
                return False, "scheduled:already_ran"
            return True, f"scheduled:{current_hhmm}"

        if mode == CampaignDistributionMode.PERIODIC:
            interval_minutes = policy.get("interval_minutes")
            if not isinstance(interval_minutes, int) or interval_minutes <= 0:
                return False, "periodic:invalid_interval"
            if last_run is None:
                return True, "periodic:first_run"
            return now - last_run >= timedelta(minutes=interval_minutes), f"periodic:{interval_minutes}"

        return False, f"{mode.value}:not_timed"

    async def _get_last_campaign_run(self, campaign: Campaign) -> Optional[datetime]:
        row = await self.db.execute(
            select(CampaignExecution)
            .where(
                and_(
                    CampaignExecution.campaign_id == campaign.id,
                    CampaignExecution.user_id.is_(None),
                    CampaignExecution.group_id.is_(None),
                    CampaignExecution.status == CampaignExecutionStatus.COMPLETED,
                    CampaignExecution.last_run_at.is_not(None),
                )
            )
            .order_by(desc(CampaignExecution.last_run_at))
            .limit(1)
        )
        execution = row.scalar_one_or_none()
        return execution.last_run_at if execution else None

    async def _record_campaign_run(self, campaign: Campaign, run_at: datetime) -> None:
        execution = CampaignExecution(
            campaign_id=campaign.id,
            status=CampaignExecutionStatus.COMPLETED,
            trigger_timing=self._timing_value(campaign.trigger_timing),
            trigger_event=campaign.trigger_event,
            distribution_mode=self.resolve_distribution_mode(campaign),
            scheduled_at=run_at,
            executed_at=run_at,
            last_run_at=run_at,
        )
        self.db.add(execution)
        await self.db.commit()

    async def _finish_execution(
        self,
        execution: CampaignExecution,
        status: CampaignExecutionStatus,
        now: datetime,
        *,
        error: Optional[str] = None,
    ) -> None:
        execution.status = status
        execution.executed_at = now
        execution.last_run_at = now if status == CampaignExecutionStatus.COMPLETED else execution.last_run_at
        execution.error = error
        await self.db.commit()

    def resolve_distribution_mode(self, campaign: Campaign) -> CampaignDistributionMode:
        timing = self._timing_value(campaign.trigger_timing)
        mapping = {
            CampaignTriggerTiming.AFTER_REGISTER.value: CampaignDistributionMode.WELCOME,
            CampaignTriggerTiming.IMMEDIATE.value: CampaignDistributionMode.WELCOME,
            CampaignTriggerTiming.DELAYED.value: CampaignDistributionMode.DELAYED,
            CampaignTriggerTiming.SCHEDULED.value: CampaignDistributionMode.SCHEDULED,
            CampaignTriggerTiming.MANUAL.value: CampaignDistributionMode.MANUAL,
            CampaignTriggerTiming.PERIODIC.value: CampaignDistributionMode.PERIODIC,
        }
        return mapping.get(timing, CampaignDistributionMode.WELCOME)

    def _get_delay_minutes(self, campaign: Campaign) -> int:
        broadcast_policy = self._parse_json_dict(campaign.broadcast_policy_json)
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        delay = broadcast_policy.get("delay_minutes", reward_policy.get("delay_minutes", 10))
        return delay if isinstance(delay, int) and delay > 0 else 10

    def _resolve_message(
        self,
        campaign: Campaign,
        *,
        user: Optional[User] = None,
        reward_result: Optional["DistributeResult"] = None,
    ) -> Optional[str]:
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        broadcast_policy = self._parse_json_dict(campaign.broadcast_policy_json)
        base_message = (
            broadcast_policy.get("message")
            or reward_policy.get("message")
            or reward_policy.get("welcome_message")
        )
        message = base_message if isinstance(base_message, str) and base_message.strip() else ""
        coupon_code = reward_result.coupon_code if reward_result and reward_result.coupon_code else ""
        if message:
            context = {
                "campaign_name": campaign.name,
                "coupon_code": coupon_code,
                "code": coupon_code,
                "batch_key": reward_result.batch_key if reward_result and reward_result.batch_key else "",
                "validity_hours": campaign.validity_hours,
                "username": user.username if user and user.username else "",
                "user_id": user.id if user else "",
                "telegram_id": user.telegram_id if user else "",
            }
            for key, value in context.items():
                message = message.replace("{" + key + "}", str(value))
            if coupon_code and "{coupon_code}" not in str(base_message) and "{code}" not in str(base_message):
                message = f"{message}\n\n兑换码：`{coupon_code}`"
            return message

        if coupon_code:
            return (
                f"{campaign.name}\n"
                f"兑换码：`{coupon_code}`\n"
                f"有效期：{campaign.validity_hours}小时"
            )
        return None

    def _parse_json_dict(self, raw: Optional[str]) -> dict:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _timing_value(self, value: CampaignTriggerTiming | str | None) -> Optional[str]:
        if value is None:
            return None
        return value.value if isinstance(value, CampaignTriggerTiming) else str(value)

    def _result(
        self,
        campaign: Campaign,
        triggered: bool,
        delivered: bool,
        reward_granted: bool,
        reason: Optional[str],
        *,
        user_id: Optional[int] = None,
        group_id: Optional[int] = None,
        status: CampaignExecutionStatus = CampaignExecutionStatus.SKIPPED,
        execution_id: Optional[int] = None,
    ) -> CampaignExecutionResult:
        return CampaignExecutionResult(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            campaign_scope=campaign.campaign_scope.value if hasattr(campaign.campaign_scope, "value") else str(campaign.campaign_scope),
            triggered=triggered,
            delivered=delivered,
            reward_granted=reward_granted,
            user_id=user_id,
            group_id=group_id,
            status=status.value,
            reason=reason,
            execution_id=execution_id,
        )

    def _accumulate(self, counts: dict[str, int], item: CampaignExecutionResult) -> None:
        if item.status == CampaignExecutionStatus.FAILED.value:
            counts["failed"] += 1
            return
        if item.status == CampaignExecutionStatus.PENDING.value:
            counts["scheduled"] += 1
            return
        if item.status == CampaignExecutionStatus.SKIPPED.value or not item.triggered:
            counts["skipped"] += 1
            return
        counts["processed"] += 1
        if item.delivered:
            counts["delivered"] += 1
        if item.reward_granted:
            counts["rewarded"] += 1


async def run_global_campaigns_with_db(now: Optional[datetime] = None) -> dict[str, int]:
    """Run due conversion campaigns from a standalone worker process."""
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        runner = CampaignRunner(db)
        return await runner.process_due_campaigns(now=now)
