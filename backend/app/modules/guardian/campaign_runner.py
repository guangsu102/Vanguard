"""
Managed-group campaign runner.

Executes guardian managed-group campaigns for join, verification,
scheduled broadcast, periodic broadcast, delayed campaigns, and
manual broadcasts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broadcasts import BroadcastRecord
from app.core.campaign.models import (
    Campaign,
    CampaignDistributionMode,
    CampaignExecution,
    CampaignExecutionStatus,
    CampaignScope,
    CampaignTracking,
    CampaignType,
)
from app.core.campaign.runner import CampaignExecutionResult
from app.core.user.tracker import UserTracker
from app.integrations.telegram.client import init_telegram_client
from app.modules.guardian.broadcast.broadcaster import GuardianBroadcaster
from app.modules.guardian.coupon.coupon_distributor import (
    CouponBatchResult,
    CouponDistributor,
    DistributeResult,
)
from app.modules.guardian.models import GroupCampaignTriggerEvent, ManagedGroupBinding

logger = structlog.get_logger()

CAMPAIGN_SCHEDULE_TIMEZONE = ZoneInfo("Asia/Shanghai")
CLAIM_PAYLOAD_PREFIX = "vgc"
CLAIM_CONTEXT_ALLOWED_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
CLAIM_PAYLOAD_MAX_LENGTH = 64


class ManagedGroupCampaignRunner:
    """Execute managed-group campaigns for guardian events."""

    def __init__(self, db: AsyncSession, xboard_client=None):
        self.db = db
        self.user_tracker = UserTracker(db)
        self.coupon_distributor = CouponDistributor(db, xboard_client)
        self.logger = logger.bind(module="managed_group_campaign_runner")

    async def trigger_for_event(
        self,
        event: GroupCampaignTriggerEvent,
        telegram_group_id: int,
        user_telegram_id: Optional[int] = None,
        username: Optional[str] = None,
        bot_account_id: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> list[CampaignExecutionResult]:
        """Trigger all eligible managed-group campaigns for the given event."""
        metadata = metadata or {}
        user = None
        if user_telegram_id is not None:
            user = await self.user_tracker.get_or_create_user(
                telegram_id=user_telegram_id,
                username=username,
            )

        if event == GroupCampaignTriggerEvent.USER_JOINED and user is not None:
            await self._queue_delayed_campaigns(
                telegram_group_id=telegram_group_id,
                user_id=user.id,
            )

        campaigns = await self._load_campaigns_for_event(
            event=event,
            telegram_group_id=telegram_group_id,
            bot_account_id=bot_account_id,
        )
        results: list[CampaignExecutionResult] = []
        for campaign in campaigns:
            results.append(
                await self._execute_campaign(
                    campaign=campaign,
                    event=event,
                    telegram_group_id=telegram_group_id,
                    user=user,
                    username=username,
                    metadata=metadata,
                )
            )
        return results

    async def process_scheduled_campaigns(self, now: Optional[datetime] = None) -> dict[str, int]:
        """Process scheduled, periodic, and delayed managed-group campaigns."""
        current = now or datetime.utcnow()
        result = {"processed": 0, "broadcasted": 0, "rewarded": 0, "skipped": 0}

        campaigns = await self._load_scheduled_campaigns()
        for campaign in campaigns:
            if self._resolve_distribution_mode(campaign) == CampaignDistributionMode.DELAYED:
                delayed = await self._process_delayed_campaign(campaign, current)
                for key in result:
                    result[key] += delayed.get(key, 0)
                continue

            should_run, reason = await self._should_run_scheduled_campaign(campaign, current)
            if not should_run:
                result["skipped"] += 1
                self.logger.debug(
                    "managed_group_scheduled_campaign_skipped",
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    reason=reason,
                )
                continue

            delivered = await self._broadcast_campaign_to_groups(
                campaign=campaign,
                telegram_group_ids=self._parse_json_list(campaign.target_group_ids),
                run_at=current,
                batch_context=self._batch_context_for_timed_campaign(campaign, current),
            )
            result["processed"] += 1
            if delivered:
                result["broadcasted"] += 1
                await self._mark_campaign_run(campaign, current)
            else:
                result["skipped"] += 1

            self.logger.info(
                "managed_group_scheduled_campaign_processed",
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                delivered=delivered,
                reason=reason,
            )

        return result

    async def trigger_manual_broadcast(self, campaign: Campaign) -> CampaignExecutionResult:
        """Execute a manual managed-group broadcast campaign immediately."""
        now = datetime.utcnow()
        execution = CampaignExecution(
            campaign_id=campaign.id,
            status=CampaignExecutionStatus.RUNNING,
            trigger_timing=self._timing_value(campaign.trigger_timing),
            trigger_event=campaign.trigger_event,
            distribution_mode=self._resolve_distribution_mode(campaign),
            scheduled_at=now,
        )
        self.db.add(execution)
        await self.db.flush()
        delivered = await self._broadcast_campaign_to_groups(
            campaign=campaign,
            telegram_group_ids=self._parse_json_list(campaign.target_group_ids),
            run_at=now,
            batch_context=f"manual-{execution.id}",
        )
        execution.delivered = delivered
        execution.status = CampaignExecutionStatus.COMPLETED if delivered else CampaignExecutionStatus.FAILED
        execution.executed_at = now
        execution.last_run_at = now if delivered else None
        execution.error = None if delivered else "broadcast_failed"
        await self.db.commit()
        return CampaignExecutionResult(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            campaign_scope=CampaignScope.MANAGED_GROUP.value,
            triggered=True,
            delivered=delivered,
            reward_granted=False,
            status=CampaignExecutionStatus.COMPLETED.value if delivered else CampaignExecutionStatus.FAILED.value,
            reason=None if delivered else "broadcast_failed",
            execution_id=execution.id,
        )

    async def claim_group_coupon(
        self,
        payload: str,
        *,
        user_telegram_id: int,
        username: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Claim one managed-group coupon from a Telegram deep-link payload."""
        parsed = self._parse_claim_payload(payload)
        if parsed is None:
            return None

        campaign_id, batch_context = parsed
        campaign = await self._get_claimable_campaign(campaign_id)
        if campaign is None:
            return "活动不存在或已结束。"

        current = now or datetime.utcnow()
        active, reason = await self._is_claim_batch_active(campaign, batch_context, current)
        if not active:
            if reason == "expired":
                return "本批次兑换码领取已过期。"
            return "本批次领取入口尚未生效，请从群内最新活动消息进入。"

        user = await self.user_tracker.get_or_create_user(
            telegram_id=user_telegram_id,
            username=username,
        )
        batch_key = self._claim_distribution_batch_key(campaign, batch_context)
        existing = await self.coupon_distributor.get_distribution_for_batch(
            user_id=user.id,
            campaign_id=campaign.id,
            batch_key=batch_key,
        )
        if existing is not None:
            return self._resolve_claim_already_received_message(
                campaign,
                DistributeResult(
                    success=False,
                    coupon_code=existing.coupon_code,
                    trial_hours=None,
                    traffic_gb=None,
                    message="User has already received this batch reward",
                    batch_key=batch_key,
                ),
            )

        result = await self.coupon_distributor.distribute_discount(
            user_id=user.id,
            campaign_id=campaign.id,
            telegram_id=user.telegram_id,
            batch_key_override=batch_key,
        )

        if result.success:
            return self._resolve_claim_success_message(campaign, result)

        if result.coupon_code:
            return self._resolve_claim_already_received_message(campaign, result)

        existing = await self.coupon_distributor.get_distribution_for_batch(
            user_id=user.id,
            campaign_id=campaign.id,
            batch_key=batch_key,
        )
        if existing is not None:
            return self._resolve_claim_already_received_message(
                campaign,
                DistributeResult(
                    success=False,
                    coupon_code=existing.coupon_code,
                    trial_hours=None,
                    traffic_gb=None,
                    message="User has already received this batch reward",
                    batch_key=batch_key,
                ),
            )

        return result.message or "领取失败，请稍后再试。"

    async def _load_campaigns_for_event(
        self,
        event: GroupCampaignTriggerEvent,
        telegram_group_id: int,
        bot_account_id: Optional[int],
    ) -> list[Campaign]:
        query = select(Campaign).where(
            and_(
                Campaign.enabled == True,  # noqa: E712
                Campaign.campaign_scope == CampaignScope.MANAGED_GROUP,
                Campaign.trigger_event == event.value,
            )
        )
        if bot_account_id is not None:
            query = query.where(Campaign.bot_account_id == bot_account_id)

        rows = await self.db.execute(query.order_by(Campaign.created_at.asc()))
        campaigns = list(rows.scalars().all())
        return [
            campaign
            for campaign in campaigns
            if telegram_group_id in self._parse_json_list(campaign.target_group_ids)
        ]

    async def _load_scheduled_campaigns(self) -> list[Campaign]:
        rows = await self.db.execute(
            select(Campaign).where(
                and_(
                    Campaign.enabled == True,  # noqa: E712
                    Campaign.campaign_scope == CampaignScope.MANAGED_GROUP,
                    or_(
                        Campaign.trigger_event.in_(
                            [
                                GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
                                GroupCampaignTriggerEvent.SCHEDULED.value,
                                GroupCampaignTriggerEvent.PERIODIC.value,
                            ]
                        ),
                        and_(
                            Campaign.trigger_event.is_(None),
                            Campaign.distribution_mode.in_(
                                [
                                    CampaignDistributionMode.DELAYED,
                                    CampaignDistributionMode.SCHEDULED,
                                    CampaignDistributionMode.PERIODIC,
                                ]
                            ),
                        ),
                    ),
                )
            )
        )
        return list(rows.scalars().all())

    async def _execute_campaign(
        self,
        campaign: Campaign,
        event: GroupCampaignTriggerEvent,
        telegram_group_id: int,
        user,
        username: Optional[str],
        metadata: dict,
        execution: Optional[CampaignExecution] = None,
    ) -> CampaignExecutionResult:
        mode = self._resolve_distribution_mode(campaign)
        if mode == CampaignDistributionMode.DELAYED and event != GroupCampaignTriggerEvent.NEW_MEMBER_DELAY:
            if user is None:
                return CampaignExecutionResult(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    campaign_scope=CampaignScope.MANAGED_GROUP.value,
                    triggered=False,
                    delivered=False,
                    reward_granted=False,
                    group_id=telegram_group_id,
                    status=CampaignExecutionStatus.SKIPPED.value,
                    reason="user_required",
                )
            scheduled = await self._schedule_delayed_execution(campaign, telegram_group_id, user.id, metadata)
            return CampaignExecutionResult(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                campaign_scope=CampaignScope.MANAGED_GROUP.value,
                triggered=True,
                delivered=False,
                reward_granted=False,
                user_id=user.id,
                group_id=telegram_group_id,
                status=CampaignExecutionStatus.PENDING.value,
                reason="scheduled",
                execution_id=scheduled.id,
            )

        if user is None and event in {
            GroupCampaignTriggerEvent.USER_JOINED,
            GroupCampaignTriggerEvent.VERIFICATION_PASSED,
            GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
        }:
            return CampaignExecutionResult(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                campaign_scope=CampaignScope.MANAGED_GROUP.value,
                triggered=False,
                delivered=False,
                reward_granted=False,
                group_id=telegram_group_id,
                status=CampaignExecutionStatus.SKIPPED.value,
                reason="user_required",
            )

        now = datetime.utcnow()
        execution = execution or CampaignExecution(
            campaign_id=campaign.id,
            user_id=getattr(user, "id", None),
            group_id=telegram_group_id,
            status=CampaignExecutionStatus.RUNNING,
            trigger_timing=self._timing_value(campaign.trigger_timing),
            trigger_event=event.value,
            distribution_mode=mode,
            scheduled_at=now,
        )
        execution.status = CampaignExecutionStatus.RUNNING
        self.db.add(execution)
        await self.db.flush()

        if user is not None:
            eligible = await self._is_user_eligible(
                campaign=campaign,
                user=user,
                telegram_group_id=telegram_group_id,
                event=event,
            )
            if not eligible:
                await self._finish_execution(
                    execution,
                    CampaignExecutionStatus.SKIPPED,
                    now,
                    error="eligibility_not_met",
                )
                return CampaignExecutionResult(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    campaign_scope=CampaignScope.MANAGED_GROUP.value,
                    triggered=False,
                    delivered=False,
                    reward_granted=False,
                    user_id=getattr(user, "id", None),
                    group_id=telegram_group_id,
                    status=CampaignExecutionStatus.SKIPPED.value,
                    reason="eligibility_not_met",
                    execution_id=execution.id,
                )

        tracking = None
        if user is not None:
            tracking = await self._get_or_create_tracking(
                campaign=campaign,
                user_id=user.id,
                telegram_group_id=telegram_group_id,
                event=event,
                metadata=metadata,
            )

        reward_result: Optional[DistributeResult] = None
        reward_granted = False
        if user is not None:
            reward_result = await self._grant_reward(campaign=campaign, user=user, tracking=tracking)
            reward_granted = reward_result.success

        if user is not None:
            delivered = False
            if reward_result and reward_result.success:
                delivered = await self._broadcast_to_groups(
                    campaign=campaign,
                    telegram_group_ids=[telegram_group_id],
                    message_override=self._resolve_reward_broadcast_message(
                        campaign=campaign,
                        user=user,
                        username=username,
                        reward_result=reward_result,
                    ),
                    parse_mode="",
                )
        else:
            delivered = await self._broadcast_to_groups(campaign=campaign, telegram_group_ids=[telegram_group_id])

        if tracking is not None:
            if delivered or reward_granted:
                tracking.registered_at = tracking.registered_at or now
            if reward_granted:
                tracking.validity_started_at = tracking.validity_started_at or now

        execution.delivered = delivered
        execution.reward_granted = reward_granted
        execution.status = CampaignExecutionStatus.COMPLETED if delivered or reward_granted else CampaignExecutionStatus.FAILED
        execution.executed_at = now
        execution.last_run_at = now
        execution.error = None if delivered or reward_granted else "delivery_and_reward_failed"
        await self.db.commit()

        self.logger.info(
            "managed_group_campaign_executed",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            trigger_event=event.value,
            telegram_group_id=telegram_group_id,
            user_id=getattr(user, "id", None),
            delivered=delivered,
            reward_granted=reward_granted,
        )

        return CampaignExecutionResult(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            campaign_scope=CampaignScope.MANAGED_GROUP.value,
            triggered=True,
            delivered=delivered,
            reward_granted=reward_granted,
            user_id=getattr(user, "id", None),
            group_id=telegram_group_id,
            status=execution.status.value,
            reason=execution.error,
            execution_id=execution.id,
        )

    async def _is_user_eligible(
        self,
        campaign: Campaign,
        user,
        telegram_group_id: int,
        event: GroupCampaignTriggerEvent,
    ) -> bool:
        policy = self._parse_json_dict(campaign.eligibility_policy_json)

        if policy.get("verified_only") and event != GroupCampaignTriggerEvent.VERIFICATION_PASSED:
            return False

        existing = await self._find_tracking(campaign.name, user.id, telegram_group_id)
        if policy.get("once_per_user") and existing and (
            existing.trial_granted or existing.coupon_granted or existing.registered_at
        ):
            return False

        min_join_minutes = policy.get("min_join_minutes")
        if isinstance(min_join_minutes, int):
            if existing is None or existing.created_at is None:
                return False
            if datetime.utcnow() - existing.created_at < timedelta(minutes=min_join_minutes):
                return False

        return True

    async def _get_or_create_tracking(
        self,
        campaign: Campaign,
        user_id: int,
        telegram_group_id: int,
        event: GroupCampaignTriggerEvent,
        metadata: dict,
    ) -> CampaignTracking:
        existing = await self._find_tracking(campaign.name, user_id, telegram_group_id)
        if existing is not None:
            return existing

        registered_at = None if metadata.get("queue_only") else datetime.utcnow()
        tracking = CampaignTracking(
            user_id=user_id,
            campaign_name=campaign.name,
            source=f"managed_group:{event.value}",
            group_id=telegram_group_id,
            keyword=metadata.get("keyword"),
            bot_id=str(campaign.bot_account_id) if campaign.bot_account_id is not None else None,
            registered_at=registered_at,
        )
        self.db.add(tracking)
        await self.db.commit()
        await self.db.refresh(tracking)
        return tracking

    async def _find_tracking(
        self,
        campaign_name: str,
        user_id: int,
        telegram_group_id: int,
    ) -> Optional[CampaignTracking]:
        row = await self.db.execute(
            select(CampaignTracking).where(
                and_(
                    CampaignTracking.campaign_name == campaign_name,
                    CampaignTracking.user_id == user_id,
                    CampaignTracking.group_id == telegram_group_id,
                )
            )
        )
        return row.scalar_one_or_none()

    async def _grant_reward(self, campaign: Campaign, user, tracking: Optional[CampaignTracking]) -> DistributeResult:
        if tracking and (tracking.trial_granted or tracking.coupon_granted):
            return DistributeResult(
                success=False,
                coupon_code=None,
                trial_hours=None,
                traffic_gb=None,
                message="User has already received this reward",
            )

        if campaign.campaign_type == CampaignType.DISCOUNT:
            result = await self.coupon_distributor.distribute_discount(
                user_id=user.id,
                campaign_id=campaign.id,
                telegram_id=user.telegram_id,
            )
            if result.success and tracking:
                tracking.coupon_granted = True
            return result

        return DistributeResult(
            success=False,
            coupon_code=None,
            trial_hours=None,
            traffic_gb=None,
            message="Unsupported campaign type",
        )

    async def _schedule_delayed_execution(
        self,
        campaign: Campaign,
        telegram_group_id: int,
        user_id: int,
        metadata: dict,
    ) -> CampaignExecution:
        existing = await self._find_execution(
            campaign_id=campaign.id,
            user_id=user_id,
            group_id=telegram_group_id,
            statuses=[
                CampaignExecutionStatus.PENDING,
                CampaignExecutionStatus.RUNNING,
                CampaignExecutionStatus.COMPLETED,
            ],
        )
        if existing is not None:
            return existing

        delay_minutes = self._get_delay_minutes(campaign)
        await self._get_or_create_tracking(
            campaign=campaign,
            user_id=user_id,
            telegram_group_id=telegram_group_id,
            event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
            metadata={**metadata, "queue_only": True},
        )
        execution = CampaignExecution(
            campaign_id=campaign.id,
            user_id=user_id,
            group_id=telegram_group_id,
            status=CampaignExecutionStatus.PENDING,
            trigger_timing=self._timing_value(campaign.trigger_timing),
            trigger_event=campaign.trigger_event,
            distribution_mode=CampaignDistributionMode.DELAYED,
            scheduled_at=datetime.utcnow() + timedelta(minutes=delay_minutes),
        )
        self.db.add(execution)
        await self.db.commit()
        await self.db.refresh(execution)
        return execution

    async def _find_execution(
        self,
        campaign_id: int,
        user_id: int,
        group_id: int,
        statuses: list[CampaignExecutionStatus],
    ) -> Optional[CampaignExecution]:
        row = await self.db.execute(
            select(CampaignExecution)
            .where(
                and_(
                    CampaignExecution.campaign_id == campaign_id,
                    CampaignExecution.user_id == user_id,
                    CampaignExecution.group_id == group_id,
                    CampaignExecution.status.in_(statuses),
                )
            )
            .order_by(desc(CampaignExecution.id))
            .limit(1)
        )
        return row.scalar_one_or_none()

    async def _finish_execution(
        self,
        execution: CampaignExecution,
        status: CampaignExecutionStatus,
        run_at: datetime,
        *,
        error: Optional[str] = None,
    ) -> None:
        execution.status = status
        execution.executed_at = run_at
        execution.error = error
        if status == CampaignExecutionStatus.COMPLETED:
            execution.last_run_at = run_at
        await self.db.commit()

    async def _queue_delayed_campaigns(self, telegram_group_id: int, user_id: int) -> None:
        rows = await self.db.execute(
            select(Campaign).where(
                and_(
                    Campaign.enabled == True,  # noqa: E712
                    Campaign.campaign_scope == CampaignScope.MANAGED_GROUP,
                    Campaign.trigger_event == GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
                )
            )
        )
        campaigns = [
            campaign
            for campaign in rows.scalars().all()
            if telegram_group_id in self._parse_json_list(campaign.target_group_ids)
        ]
        for campaign in campaigns:
            await self._schedule_delayed_execution(campaign, telegram_group_id, user_id, {"queue_only": True})

    async def _process_delayed_campaign(self, campaign: Campaign, now: datetime) -> dict[str, int]:
        target_group_ids = self._parse_json_list(campaign.target_group_ids)
        if not target_group_ids:
            return {"processed": 0, "broadcasted": 0, "rewarded": 0, "skipped": 1}

        rows = await self.db.execute(
            select(CampaignExecution).where(
                and_(
                    CampaignExecution.campaign_id == campaign.id,
                    CampaignExecution.status == CampaignExecutionStatus.PENDING,
                    CampaignExecution.scheduled_at.is_not(None),
                    CampaignExecution.scheduled_at <= now,
                    CampaignExecution.group_id.in_(target_group_ids),
                )
            )
        )
        pending = list(rows.scalars().all())
        result = {"processed": 0, "broadcasted": 0, "rewarded": 0, "skipped": 0}

        for delayed_execution in pending:
            if delayed_execution.user_id is None:
                await self._finish_execution(delayed_execution, CampaignExecutionStatus.SKIPPED, now, error="user_required")
                result["skipped"] += 1
                continue

            user = await self.user_tracker.get_user(delayed_execution.user_id)
            if user is None:
                await self._finish_execution(delayed_execution, CampaignExecutionStatus.SKIPPED, now, error="user_not_found")
                result["skipped"] += 1
                continue

            execution = await self._execute_campaign(
                campaign=campaign,
                event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
                telegram_group_id=delayed_execution.group_id or 0,
                user=user,
                username=user.username,
                metadata={},
                execution=delayed_execution,
            )
            result["processed"] += 1
            if execution.delivered:
                result["broadcasted"] += 1
            if execution.reward_granted:
                result["rewarded"] += 1

        return result

    async def _broadcast_campaign_to_groups(
        self,
        *,
        campaign: Campaign,
        telegram_group_ids: list[int],
        run_at: datetime,
        batch_context: str,
    ) -> bool:
        """Render and send a timed/manual managed-group campaign broadcast."""
        message = self._resolve_broadcast_message(campaign)
        parse_mode = "Markdown"
        reply_markup: Optional[dict] = None

        if self._should_use_group_coupon_claim_link(campaign):
            claim_url = await self._build_group_coupon_claim_url(
                campaign=campaign,
                batch_context=batch_context,
            )
            if not claim_url:
                self.logger.error(
                    "managed_group_coupon_claim_link_failed",
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    batch_context=batch_context,
                    error="bot_username_missing",
                )
                return False
            message = self._resolve_group_coupon_claim_message(
                campaign=campaign,
                base_message=message or "",
                claim_url=claim_url,
                batch_context=batch_context,
                run_at=run_at,
            )
            parse_mode = ""
            reply_markup = self._build_claim_reply_markup(claim_url)
        elif self._should_generate_group_coupon_batch(campaign):
            batch_result = await self.coupon_distributor.generate_discount_batch(
                campaign,
                batch_context=batch_context,
            )
            if not batch_result.success:
                self.logger.error(
                    "managed_group_coupon_batch_generation_failed",
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    batch_context=batch_context,
                    error=batch_result.message,
                )
                return False
            message = self._resolve_group_coupon_broadcast_message(
                campaign=campaign,
                base_message=message or "",
                batch_result=batch_result,
                run_at=run_at,
            )
            parse_mode = ""

        return await self._broadcast_to_groups(
            campaign=campaign,
            telegram_group_ids=telegram_group_ids,
            message_override=message,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    async def _broadcast_to_groups(
        self,
        campaign: Campaign,
        telegram_group_ids: list[int],
        message_override: Optional[str] = None,
        parse_mode: str = "Markdown",
        reply_markup: Optional[dict] = None,
    ) -> bool:
        if not telegram_group_ids:
            return False

        message = message_override or self._resolve_broadcast_message(campaign)
        if not message:
            return False

        rows = await self.db.execute(
            select(ManagedGroupBinding).where(ManagedGroupBinding.telegram_group_id.in_(telegram_group_ids))
        )
        bindings = list(rows.scalars().all())
        bot_group_map: dict[int, list[int]] = {}
        for binding in bindings:
            bot_group_map.setdefault(binding.bot_account_id, []).append(binding.telegram_group_id)

        if not bot_group_map and campaign.bot_account_id is not None:
            bot_group_map[campaign.bot_account_id] = telegram_group_ids

        overall_success = False
        total_success = 0
        total_failed = 0
        for account_id, group_ids in bot_group_map.items():
            client = await self._create_guardian_client(account_id)
            try:
                broadcaster = GuardianBroadcaster(self.db, client)
                broadcast = await broadcaster.broadcast_custom(
                    group_ids,
                    message,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                total_success += broadcast.success
                total_failed += broadcast.failed
                overall_success = overall_success or broadcast.success > 0
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    await close()

        record = BroadcastRecord(
            content=message,
            broadcast_type=f"managed_group_campaign:{campaign.trigger_event or 'manual'}",
            target_groups=json.dumps(telegram_group_ids, ensure_ascii=False),
            target_group_count=len(telegram_group_ids),
            success_count=total_success,
            failed_count=total_failed,
            status="completed" if overall_success else "failed",
            completed_at=datetime.utcnow(),
        )
        self.db.add(record)
        await self.db.commit()
        return overall_success

    async def _create_guardian_client(self, account_id: int):
        from app.core.account.models import GuardianBotProfile

        row = await self.db.execute(
            select(GuardianBotProfile).where(GuardianBotProfile.account_id == account_id)
        )
        profile = row.scalar_one_or_none()
        if profile is None:
            raise RuntimeError(f"Guardian bot profile not found for account_id={account_id}")
        return await init_telegram_client(profile.bot_token)

    def _resolve_broadcast_message(self, campaign: Campaign) -> Optional[str]:
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        broadcast_policy = self._parse_json_dict(campaign.broadcast_policy_json)
        return (
            broadcast_policy.get("message")
            or reward_policy.get("message")
            or reward_policy.get("welcome_message")
            or broadcast_policy.get("template")
            or campaign.name
        )

    def _should_use_group_coupon_claim_link(self, campaign: Campaign) -> bool:
        if not self._is_sub2api_group_coupon_campaign(campaign):
            return False
        return self._group_coupon_delivery_mode(campaign) != "public_codes"

    def _should_generate_group_coupon_batch(self, campaign: Campaign) -> bool:
        return (
            self._is_sub2api_group_coupon_campaign(campaign)
            and self._group_coupon_delivery_mode(campaign) == "public_codes"
        )

    def _is_sub2api_group_coupon_campaign(self, campaign: Campaign) -> bool:
        if campaign.campaign_type != CampaignType.DISCOUNT:
            return False
        mode = self._resolve_distribution_mode(campaign)
        if mode not in {
            CampaignDistributionMode.SCHEDULED,
            CampaignDistributionMode.PERIODIC,
            CampaignDistributionMode.MANUAL,
        }:
            return False
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        provider = str(reward_policy.get("coupon_provider") or reward_policy.get("provider") or "").lower()
        return provider == "sub2api"

    def _group_coupon_delivery_mode(self, campaign: Campaign) -> str:
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        raw = (
            reward_policy.get("coupon_delivery_mode")
            or reward_policy.get("group_coupon_delivery_mode")
            or reward_policy.get("group_coupon_mode")
            or "claim_link"
        )
        return str(raw).strip().lower()

    async def _build_group_coupon_claim_url(self, *, campaign: Campaign, batch_context: str) -> Optional[str]:
        bot_username = await self._get_guardian_bot_username(campaign.bot_account_id)
        if not bot_username:
            return None
        username = bot_username.lstrip("@")
        payload = self._build_claim_payload(campaign.id, batch_context)
        return f"https://t.me/{username}?start={payload}"

    async def _get_guardian_bot_username(self, bot_account_id: Optional[int]) -> Optional[str]:
        if bot_account_id is None:
            return None
        from app.core.account.models import GuardianBotProfile

        row = await self.db.execute(
            select(GuardianBotProfile.bot_username).where(GuardianBotProfile.account_id == bot_account_id)
        )
        username = row.scalar_one_or_none()
        if not username:
            return None
        return str(username).strip() or None

    def _build_claim_reply_markup(self, claim_url: str) -> dict:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "领取优惠券",
                        "url": claim_url,
                    }
                ]
            ]
        }

    def _resolve_group_coupon_claim_message(
        self,
        *,
        campaign: Campaign,
        base_message: str,
        claim_url: str,
        batch_context: str,
        run_at: datetime,
    ) -> str:
        local_run_at = self._scheduled_local_datetime(run_at)
        command = f"/start {self._build_claim_payload(campaign.id, batch_context)}"
        context = _SafeFormatDict(
            campaign_name=campaign.name,
            claim_url=claim_url,
            claim_command=command,
            batch_key=self._claim_distribution_batch_key(campaign, batch_context),
            batch_context=batch_context,
            validity_hours=campaign.validity_hours,
            schedule_time=local_run_at.strftime("%H:%M"),
            schedule_date=local_run_at.strftime("%Y-%m-%d"),
        )

        if base_message:
            message = self._replace_coupon_placeholder_lines_with_claim(base_message)
            try:
                message = message.format_map(context)
            except (KeyError, ValueError):
                message = base_message
            message = self._replace_known_placeholders(message, context)
        else:
            message = (
                f"{campaign.name}\n\n"
                f"本批次每位用户仅可领取一次。\n"
                f"领取入口：{claim_url}\n"
                f"有效期：{campaign.validity_hours}小时"
            )

        if "{coupon_code}" in message or "{code}" in message:
            message = self._replace_coupon_placeholder_lines_with_claim(message)
            message = self._replace_known_placeholders(message, context)

        if "{claim_url}" not in base_message and claim_url not in message:
            message = f"{message}\n\n领取入口：{claim_url}"
        if "每位用户" not in message:
            message = f"{message}\n每位用户每个批次仅可领取一次。"
        return message

    def _replace_coupon_placeholder_lines_with_claim(self, message: str) -> str:
        lines = []
        for line in message.splitlines():
            if self._has_coupon_placeholder(line):
                lines.append("领取入口：{claim_url}")
            else:
                lines.append(line)
        return "\n".join(lines)

    def _resolve_group_coupon_broadcast_message(
        self,
        *,
        campaign: Campaign,
        base_message: str,
        batch_result: CouponBatchResult,
        run_at: datetime,
    ) -> str:
        codes_text = self._format_coupon_codes(batch_result.coupon_codes)
        code_lines = self._format_coupon_code_lines(batch_result.coupon_codes)
        first_code = batch_result.coupon_codes[0] if batch_result.coupon_codes else ""
        local_run_at = self._scheduled_local_datetime(run_at)
        context = _SafeFormatDict(
            campaign_name=campaign.name,
            coupon_code=codes_text,
            code=codes_text,
            coupon_codes=codes_text,
            codes=codes_text,
            coupon_code_first=first_code,
            first_code=first_code,
            coupon_code_lines=code_lines,
            batch_key=batch_result.batch_key or "",
            validity_hours=campaign.validity_hours,
            schedule_time=local_run_at.strftime("%H:%M"),
            schedule_date=local_run_at.strftime("%Y-%m-%d"),
        )

        if base_message:
            try:
                message = base_message.format_map(context)
            except (KeyError, ValueError):
                message = base_message
            message = self._replace_known_placeholders(message, context)
            if not self._has_coupon_placeholder(base_message) and codes_text:
                message = f"{message}\n\n兑换码：\n{codes_text}"
            return message

        lines = [
            f"{campaign.name}",
            "本批兑换码：",
            codes_text,
            f"有效期：{campaign.validity_hours}小时",
        ]
        if batch_result.batch_key:
            lines.append(f"批次：{batch_result.batch_key}")
        return "\n".join(line for line in lines if line)

    def _replace_known_placeholders(self, message: str, context: dict) -> str:
        rendered = message
        for key, value in context.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered

    def _has_coupon_placeholder(self, message: str) -> bool:
        placeholders = {
            "{coupon_code}",
            "{code}",
            "{coupon_codes}",
            "{codes}",
            "{coupon_code_first}",
            "{first_code}",
            "{coupon_code_lines}",
        }
        return any(placeholder in message for placeholder in placeholders)

    def _format_coupon_codes(self, codes: list[str]) -> str:
        return "\n".join(str(code) for code in codes if code)

    def _format_coupon_code_lines(self, codes: list[str]) -> str:
        return "\n".join(f"{index}. {code}" for index, code in enumerate(codes, start=1) if code)

    def _resolve_reward_broadcast_message(
        self,
        *,
        campaign: Campaign,
        user,
        username: Optional[str],
        reward_result: DistributeResult,
    ) -> str:
        base_message = self._resolve_broadcast_message(campaign) or ""
        coupon_code = reward_result.coupon_code or ""
        display_name = self._format_user_display(user, username)
        context = _SafeFormatDict(
            campaign_name=campaign.name,
            coupon_code=coupon_code,
            code=coupon_code,
            batch_key=reward_result.batch_key or "",
            validity_hours=campaign.validity_hours,
            username=username or getattr(user, "username", None) or "",
            user_id=getattr(user, "id", ""),
            telegram_id=getattr(user, "telegram_id", ""),
            user_display=display_name,
        )

        if base_message:
            try:
                message = base_message.format_map(context)
            except (KeyError, ValueError):
                message = base_message
            has_coupon_placeholder = "{coupon_code}" in base_message or "{code}" in base_message
            if coupon_code and not has_coupon_placeholder:
                message = f"{message}\n\n兑换码：`{coupon_code}`"
            return message

        lines = [
            f"{display_name} 领取成功",
            f"活动：{campaign.name}",
        ]
        if coupon_code:
            lines.append(f"兑换码：`{coupon_code}`")
        if reward_result.batch_key:
            lines.append(f"批次：{reward_result.batch_key}")
        lines.append(f"有效期：{campaign.validity_hours}小时")
        return "\n".join(lines)

    def _format_user_display(self, user, username: Optional[str]) -> str:
        handle = username or getattr(user, "username", None)
        if handle:
            handle = str(handle).lstrip("@")
            return f"@{handle}"
        telegram_id = getattr(user, "telegram_id", None)
        return str(telegram_id or getattr(user, "id", "用户"))

    def _build_claim_payload(self, campaign_id: int, batch_context: str) -> str:
        context = self._normalize_claim_context(batch_context)
        prefix = f"{CLAIM_PAYLOAD_PREFIX}_{campaign_id}_"
        max_context_length = CLAIM_PAYLOAD_MAX_LENGTH - len(prefix)
        return f"{prefix}{context[:max_context_length]}"

    def _normalize_claim_context(self, batch_context: str) -> str:
        normalized = "".join(
            char if char in CLAIM_CONTEXT_ALLOWED_CHARS else "-"
            for char in str(batch_context or "default").strip()
        )
        return normalized or "default"

    def _parse_claim_payload(self, payload: str) -> Optional[tuple[int, str]]:
        raw = str(payload or "").strip()
        if raw.startswith("/start"):
            parts = raw.split(maxsplit=1)
            raw = parts[1].strip() if len(parts) == 2 else ""
        if not raw.startswith(f"{CLAIM_PAYLOAD_PREFIX}_"):
            return None

        parts = raw.split("_", 2)
        if len(parts) != 3 or parts[0] != CLAIM_PAYLOAD_PREFIX:
            return None
        try:
            campaign_id = int(parts[1])
        except ValueError:
            return None
        batch_context = self._normalize_claim_context(parts[2])
        return campaign_id, batch_context

    def _claim_distribution_batch_key(self, campaign: Campaign, batch_context: str) -> str:
        reward_policy = self._parse_json_dict(campaign.reward_policy_json)
        raw = reward_policy.get("coupon_batch_key") or reward_policy.get("batch_key") or campaign.id
        base = str(raw).strip() or str(campaign.id)
        context = self._normalize_claim_context(batch_context)
        return f"{base}:{context}"[:100]

    async def _get_claimable_campaign(self, campaign_id: int) -> Optional[Campaign]:
        row = await self.db.execute(
            select(Campaign).where(
                and_(
                    Campaign.id == campaign_id,
                    Campaign.enabled == True,  # noqa: E712
                    Campaign.campaign_scope == CampaignScope.MANAGED_GROUP,
                    Campaign.campaign_type == CampaignType.DISCOUNT,
                )
            )
        )
        campaign = row.scalar_one_or_none()
        if campaign is None or not self._should_use_group_coupon_claim_link(campaign):
            return None
        return campaign

    async def _is_claim_batch_active(
        self,
        campaign: Campaign,
        batch_context: str,
        now: datetime,
    ) -> tuple[bool, str]:
        context = self._normalize_claim_context(batch_context)
        current = self._utc_naive(now)

        if context.startswith("manual-"):
            execution_id = self._parse_context_suffix_int(context, "manual-")
            if execution_id is None:
                return False, "invalid"
            row = await self.db.execute(
                select(CampaignExecution).where(
                    and_(
                        CampaignExecution.id == execution_id,
                        CampaignExecution.campaign_id == campaign.id,
                        CampaignExecution.status == CampaignExecutionStatus.COMPLETED,
                        CampaignExecution.delivered == True,  # noqa: E712
                    )
                )
            )
            execution = row.scalar_one_or_none()
            if execution is None:
                return False, "not_announced"
            run_at = execution.last_run_at or execution.executed_at or execution.scheduled_at
            if run_at and current > self._utc_naive(run_at) + timedelta(hours=campaign.validity_hours):
                return False, "expired"
            return True, "active"

        run_at = self._claim_context_run_at_utc(context)
        if run_at is None:
            return False, "invalid"
        if current > run_at + timedelta(hours=campaign.validity_hours):
            return False, "expired"

        row = await self.db.execute(
            select(CampaignExecution.id)
            .where(
                and_(
                    CampaignExecution.campaign_id == campaign.id,
                    CampaignExecution.user_id.is_(None),
                    CampaignExecution.group_id.is_(None),
                    CampaignExecution.status == CampaignExecutionStatus.COMPLETED,
                    CampaignExecution.last_run_at >= run_at,
                    CampaignExecution.last_run_at < run_at + timedelta(minutes=1),
                )
            )
            .limit(1)
        )
        found = row.scalar_one_or_none() is not None
        return found, "active" if found else "not_announced"

    def _parse_context_suffix_int(self, context: str, prefix: str) -> Optional[int]:
        if not context.startswith(prefix):
            return None
        try:
            return int(context[len(prefix) :])
        except ValueError:
            return None

    def _claim_context_run_at_utc(self, context: str) -> Optional[datetime]:
        for prefix in ("scheduled-", "periodic-"):
            if not context.startswith(prefix):
                continue
            timestamp = context[len(prefix) :]
            try:
                local_run_at = datetime.strptime(timestamp, "%Y%m%d%H%M")
            except ValueError:
                return None
            aware = local_run_at.replace(tzinfo=CAMPAIGN_SCHEDULE_TIMEZONE)
            return aware.astimezone(timezone.utc).replace(tzinfo=None)
        return None

    def _utc_naive(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def _resolve_claim_success_message(self, campaign: Campaign, result: DistributeResult) -> str:
        lines = [
            "领取成功",
            f"活动：{campaign.name}",
        ]
        if result.coupon_code:
            lines.append(f"兑换码：{result.coupon_code}")
        if result.batch_key:
            lines.append(f"批次：{result.batch_key}")
        lines.append(f"有效期：{campaign.validity_hours}小时")
        return "\n".join(lines)

    def _resolve_claim_already_received_message(self, campaign: Campaign, result: DistributeResult) -> str:
        lines = [
            "你已领取过本批次优惠券。",
            f"活动：{campaign.name}",
        ]
        if result.coupon_code:
            lines.append(f"已领取兑换码：{result.coupon_code}")
        if result.batch_key:
            lines.append(f"批次：{result.batch_key}")
        return "\n".join(lines)

    async def _should_run_scheduled_campaign(self, campaign: Campaign, now: datetime) -> tuple[bool, str]:
        policy = self._parse_json_dict(campaign.broadcast_policy_json)
        mode = self._resolve_distribution_mode(campaign)
        last_run = await self._get_last_campaign_run(campaign)
        schedule_now = self._scheduled_local_datetime(now)

        if mode == CampaignDistributionMode.SCHEDULED:
            schedule_times = policy.get("schedule_times") or []
            current_hhmm = schedule_now.strftime("%H:%M")
            if current_hhmm not in schedule_times:
                return False, f"scheduled:not_due:{current_hhmm}"
            if last_run and self._scheduled_local_datetime(last_run).strftime("%Y-%m-%d %H:%M") == schedule_now.strftime(
                "%Y-%m-%d %H:%M"
            ):
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

    def _scheduled_local_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            aware = value.replace(tzinfo=timezone.utc)
        else:
            aware = value.astimezone(timezone.utc)
        return aware.astimezone(CAMPAIGN_SCHEDULE_TIMEZONE).replace(tzinfo=None)

    def _batch_context_for_timed_campaign(self, campaign: Campaign, run_at: datetime) -> str:
        local_run_at = self._scheduled_local_datetime(run_at)
        mode = self._resolve_distribution_mode(campaign)
        return f"{mode.value}-{local_run_at.strftime('%Y%m%d%H%M')}"

    async def _mark_campaign_run(self, campaign: Campaign, run_at: datetime) -> None:
        execution = CampaignExecution(
            campaign_id=campaign.id,
            status=CampaignExecutionStatus.COMPLETED,
            trigger_timing=self._timing_value(campaign.trigger_timing),
            trigger_event=campaign.trigger_event,
            distribution_mode=self._resolve_distribution_mode(campaign),
            scheduled_at=run_at,
            executed_at=run_at,
            last_run_at=run_at,
        )
        self.db.add(execution)
        await self.db.commit()

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

    def _get_delay_minutes(self, campaign: Campaign) -> int:
        policy = self._parse_json_dict(campaign.broadcast_policy_json)
        delay = policy.get("delay_minutes")
        return delay if isinstance(delay, int) and delay > 0 else 10

    def _resolve_distribution_mode(self, campaign: Campaign) -> CampaignDistributionMode:
        if campaign.trigger_event == GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value:
            return CampaignDistributionMode.DELAYED
        if campaign.trigger_event == GroupCampaignTriggerEvent.SCHEDULED.value:
            return CampaignDistributionMode.SCHEDULED
        if campaign.trigger_event == GroupCampaignTriggerEvent.MANUAL_BROADCAST.value:
            return CampaignDistributionMode.MANUAL
        if campaign.trigger_event == GroupCampaignTriggerEvent.PERIODIC.value:
            return CampaignDistributionMode.PERIODIC
        if campaign.distribution_mode:
            return (
                campaign.distribution_mode
                if isinstance(campaign.distribution_mode, CampaignDistributionMode)
                else CampaignDistributionMode(str(campaign.distribution_mode))
            )
        return CampaignDistributionMode.WELCOME

    def _timing_value(self, value) -> Optional[str]:
        if value is None:
            return None
        return value.value if hasattr(value, "value") else str(value)

    def _parse_json_dict(self, raw: Optional[str]) -> dict:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _parse_json_list(self, raw: Optional[str]) -> list[int]:
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except Exception:
            return []
        if not isinstance(value, list):
            return []
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


async def run_managed_group_campaigns_with_db(now: Optional[datetime] = None) -> dict[str, int]:
    """Run scheduled guardian campaigns from a standalone worker process."""
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        runner = ManagedGroupCampaignRunner(db)
        return await runner.process_scheduled_campaigns(now=now)
