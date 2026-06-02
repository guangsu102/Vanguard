"""
Managed-group campaign runner.

Executes guardian managed-group campaigns for join, verification,
scheduled broadcast, periodic broadcast, delayed campaigns, and
manual broadcasts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.broadcasts import BroadcastRecord
from app.core.campaign.models import Campaign, CampaignScope, CampaignTracking, CampaignType
from app.core.user.tracker import UserTracker
from app.integrations.telegram.client import init_telegram_client
from app.modules.guardian.broadcast.broadcaster import GuardianBroadcaster
from app.modules.guardian.coupon.coupon_distributor import CouponDistributor
from app.modules.guardian.models import GroupCampaignTriggerEvent, ManagedGroupBinding

logger = structlog.get_logger()


@dataclass(slots=True)
class CampaignExecutionResult:
    """Execution summary for a managed-group campaign trigger."""

    campaign_id: int
    campaign_name: str
    triggered: bool
    delivered: bool
    reward_granted: bool
    reason: Optional[str] = None


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
            if campaign.trigger_event == GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value:
                delayed = await self._process_delayed_campaign(campaign, current)
                for key in result:
                    result[key] += delayed.get(key, 0)
                continue

            should_run, reason = self._should_run_scheduled_campaign(campaign, current)
            if not should_run:
                result["skipped"] += 1
                self.logger.debug(
                    "managed_group_scheduled_campaign_skipped",
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    reason=reason,
                )
                continue

            delivered = await self._broadcast_to_groups(
                campaign=campaign,
                telegram_group_ids=self._parse_json_list(campaign.target_group_ids),
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
        delivered = await self._broadcast_to_groups(
            campaign=campaign,
            telegram_group_ids=self._parse_json_list(campaign.target_group_ids),
        )
        return CampaignExecutionResult(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            triggered=True,
            delivered=delivered,
            reward_granted=False,
            reason=None if delivered else "broadcast_failed",
        )

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
                    Campaign.trigger_event.in_(
                        [
                            GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
                            GroupCampaignTriggerEvent.SCHEDULED.value,
                            GroupCampaignTriggerEvent.PERIODIC.value,
                        ]
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
    ) -> CampaignExecutionResult:
        if user is None and event in {
            GroupCampaignTriggerEvent.USER_JOINED,
            GroupCampaignTriggerEvent.VERIFICATION_PASSED,
            GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
        }:
            return CampaignExecutionResult(
                campaign_id=campaign.id,
                campaign_name=campaign.name,
                triggered=False,
                delivered=False,
                reward_granted=False,
                reason="user_required",
            )

        if user is not None:
            eligible = await self._is_user_eligible(
                campaign=campaign,
                user=user,
                telegram_group_id=telegram_group_id,
                event=event,
            )
            if not eligible:
                return CampaignExecutionResult(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    triggered=False,
                    delivered=False,
                    reward_granted=False,
                    reason="eligibility_not_met",
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

        delivered = await self._broadcast_to_groups(campaign=campaign, telegram_group_ids=[telegram_group_id])
        reward_granted = False
        if user is not None:
            reward_granted = await self._grant_reward(campaign=campaign, user=user, tracking=tracking)

        if tracking is not None:
            if delivered or reward_granted:
                tracking.registered_at = tracking.registered_at or datetime.utcnow()
            if reward_granted:
                tracking.validity_started_at = tracking.validity_started_at or datetime.utcnow()
            await self.db.commit()

        self.logger.info(
            "managed_group_campaign_executed",
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            event=event.value,
            telegram_group_id=telegram_group_id,
            user_id=getattr(user, "id", None),
            delivered=delivered,
            reward_granted=reward_granted,
        )

        return CampaignExecutionResult(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            triggered=True,
            delivered=delivered,
            reward_granted=reward_granted,
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

    async def _grant_reward(self, campaign: Campaign, user, tracking: Optional[CampaignTracking]) -> bool:
        if tracking and (tracking.trial_granted or tracking.coupon_granted):
            return False

        distributed = False
        if campaign.campaign_type == CampaignType.DISCOUNT:
            result = await self.coupon_distributor.distribute_discount(
                user_id=user.id,
                campaign_id=campaign.id,
                telegram_id=user.telegram_id,
            )
            distributed = result.success
            if distributed and tracking:
                tracking.coupon_granted = True

        return distributed

    async def _queue_delayed_campaigns(self, telegram_group_id: int, user_id: int) -> None:
        campaigns = await self._load_campaigns_for_event(
            event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
            telegram_group_id=telegram_group_id,
            bot_account_id=None,
        )
        for campaign in campaigns:
            await self._get_or_create_tracking(
                campaign=campaign,
                user_id=user_id,
                telegram_group_id=telegram_group_id,
                event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
                metadata={"queue_only": True},
            )

    async def _process_delayed_campaign(self, campaign: Campaign, now: datetime) -> dict[str, int]:
        policy = self._parse_json_dict(campaign.broadcast_policy_json)
        delay_minutes = policy.get("delay_minutes")
        if not isinstance(delay_minutes, int) or delay_minutes < 1:
            return {"processed": 0, "broadcasted": 0, "rewarded": 0, "skipped": 1}

        target_group_ids = self._parse_json_list(campaign.target_group_ids)
        if not target_group_ids:
            return {"processed": 0, "broadcasted": 0, "rewarded": 0, "skipped": 1}

        rows = await self.db.execute(
            select(CampaignTracking).where(
                and_(
                    CampaignTracking.campaign_name == campaign.name,
                    CampaignTracking.source == f"managed_group:{GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value}",
                    CampaignTracking.group_id.in_(target_group_ids),
                    CampaignTracking.registered_at.is_(None),
                )
            )
        )
        pending = list(rows.scalars().all())
        result = {"processed": 0, "broadcasted": 0, "rewarded": 0, "skipped": 0}

        for tracking in pending:
            created_at = tracking.created_at or now
            if now < created_at + timedelta(minutes=delay_minutes):
                result["skipped"] += 1
                continue

            user = await self.user_tracker.get_user(tracking.user_id)
            if user is None:
                result["skipped"] += 1
                continue

            execution = await self._execute_campaign(
                campaign=campaign,
                event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY,
                telegram_group_id=tracking.group_id or 0,
                user=user,
                username=user.username,
                metadata={},
            )
            result["processed"] += 1
            if execution.delivered:
                result["broadcasted"] += 1
            if execution.reward_granted:
                result["rewarded"] += 1

        return result

    async def _broadcast_to_groups(self, campaign: Campaign, telegram_group_ids: list[int]) -> bool:
        if not telegram_group_ids:
            return False

        message = self._resolve_broadcast_message(campaign)
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
                broadcast = await broadcaster.broadcast_custom(group_ids, message)
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

    def _should_run_scheduled_campaign(self, campaign: Campaign, now: datetime) -> tuple[bool, str]:
        policy = self._parse_json_dict(campaign.broadcast_policy_json)
        last_run = self._extract_last_run_time(policy)

        if campaign.trigger_event == GroupCampaignTriggerEvent.SCHEDULED.value:
            schedule_times = policy.get("schedule_times") or []
            current_hhmm = now.strftime("%H:%M")
            if current_hhmm not in schedule_times:
                return False, f"scheduled:not_due:{current_hhmm}"
            if last_run and last_run.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M"):
                return False, "scheduled:already_ran"
            return True, f"scheduled:{current_hhmm}"

        if campaign.trigger_event == GroupCampaignTriggerEvent.PERIODIC.value:
            interval_minutes = policy.get("interval_minutes")
            if not isinstance(interval_minutes, int) or interval_minutes <= 0:
                return False, "periodic:invalid_interval"
            if last_run is None:
                return True, "periodic:first_run"
            return now - last_run >= timedelta(minutes=interval_minutes), f"periodic:{interval_minutes}"

        return False, "unsupported_trigger"

    async def _mark_campaign_run(self, campaign: Campaign, run_at: datetime) -> None:
        policy = self._parse_json_dict(campaign.broadcast_policy_json)
        policy["last_run_at"] = run_at.isoformat()
        campaign.broadcast_policy_json = json.dumps(policy, ensure_ascii=False)
        await self.db.commit()

    def _extract_last_run_time(self, policy: dict) -> Optional[datetime]:
        raw = policy.get("last_run_at")
        if not raw or not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

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


async def run_managed_group_campaigns_with_db(now: Optional[datetime] = None) -> dict[str, int]:
    """Run scheduled guardian campaigns from a standalone worker process."""
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    async with db_module.get_db_session() as db:
        runner = ManagedGroupCampaignRunner(db)
        return await runner.process_scheduled_campaigns(now=now)
