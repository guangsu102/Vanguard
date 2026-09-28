"""One fenced daily review of the previous cycle's last confirmed advertisement."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import or_, select

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.modules.acquisition.adaptive_frequency import (
    DAY,
    FrequencyService,
    frequency_context,
    negative_observation,
    next_quota,
    payload,
    rule_quota,
)
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency, GroupAdFrequencyEvent
from app.modules.acquisition.qualification_identity import identity_relation, peer_identity

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DailyReviewPlan:
    log: AdDeliveryLog | None = None
    due_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class DailyReviewClaim:
    group_key: int
    epoch: int
    log_id: int
    account_id: int
    token: str


class DailyFrequencyService:
    def __init__(self, db: Any):
        self.db = db
        self.frequency = FrequencyService(db)

    async def plan(
        self,
        state: GroupAdFrequency,
        *,
        logs: list[AdDeliveryLog] | None = None,
    ) -> DailyReviewPlan:
        logs = logs if logs is not None else await self.frequency.logs(state.telegram_group_id)
        identity = peer_identity(state.telegram_group_id)
        current = []
        for log in logs:
            context = payload(log.qualification_context_json)
            if log.status in {"unknown", "sending", "reconciliation_required", "pending"}:
                return DailyReviewPlan(reason="qualification_delivery_reconciliation_required")
            if log.status != "success" and log.telegram_message_id is None:
                continue
            if identity_relation(identity, peer_identity(log.telegram_group_id, context)) != "same":
                return DailyReviewPlan(reason="qualification_group_identity_unknown")
            if log.status != "success" or log.sent_at is None or log.telegram_message_id is None:
                return DailyReviewPlan(reason="qualification_delivery_reconciliation_required")
            if (
                frequency_context(log).get("epoch") == state.epoch
                and log.sent_at >= state.epoch_started_at
            ):
                current.append(log)
        if not current:
            return DailyReviewPlan()
        first = min(log.sent_at for log in current)
        due = state.daily_review_due_at or first + DAY
        if state.daily_review_due_at is None and any(
            frequency_context(log).get("version") == 1 for log in current
        ):
            # Adopt the last real legacy message once, without replaying historical days.
            due = max(log.sent_at for log in current) + DAY
        # An idle cycle earns no increase. Restart its observation period at the next real send.
        if all(log.sent_at >= due for log in current):
            due = first + DAY
        if not state.mature:
            due = max(due, first + DAY)
        previous = [log for log in current if log.sent_at < due]
        latest = max(previous, key=lambda log: (log.sent_at, log.id), default=None)
        return DailyReviewPlan(latest, due if latest else None)

    async def note_sent(self, log: AdDeliveryLog) -> None:
        if log.status != "success" or not log.sent_at or not frequency_context(log):
            return
        state = await self.frequency.state(
            log.telegram_group_id,
            payload(log.qualification_context_json),
            lock=True,
        )
        if state is None or frequency_context(log).get("epoch") != state.epoch:
            return
        await self.db.flush()
        plan = await self.plan(state)
        if plan.due_at:
            state.daily_review_due_at = plan.due_at

    async def claim(self, group_key: int, now: datetime) -> DailyReviewClaim | None:
        state = await self.frequency.state(group_key, lock=True)
        if state is None or state.status != "active":
            return None
        if (
            (state.pause_until and state.pause_until > now)
            or (state.daily_review_retry_at and state.daily_review_retry_at > now)
            or (
                state.daily_review_token
                and state.daily_review_expires_at
                and state.daily_review_expires_at > now
            )
        ):
            return None
        plan = await self.plan(state)
        if plan.reason:
            state.daily_review_error = plan.reason
            state.daily_review_retry_at = now + timedelta(minutes=2)
            return None
        if plan.log is None or plan.due_at is None or plan.due_at > now:
            return None
        config = await self.db.scalar(
            select(AccountOperationConfig)
            .where(
                AccountOperationConfig.account_id == plan.log.account_id,
            )
            .execution_options(populate_existing=True)
        )
        if not (
            config
            and config.enabled
            and config.auto_ads_enabled
            and config.adaptive_ads_enabled
            and config.dynamic_capacity_enabled
        ):
            return None
        token = uuid4().hex
        state.daily_review_due_at = plan.due_at
        state.daily_review_log_id = plan.log.id
        state.daily_review_token = token
        state.daily_review_expires_at = now + timedelta(minutes=3)
        state.daily_review_error = None
        await self.db.flush()
        return DailyReviewClaim(group_key, state.epoch, plan.log.id, plan.log.account_id, token)

    async def finish(
        self,
        claim: DailyReviewClaim,
        facts: dict[str, Any],
        reason: str | None,
        now: datetime,
        *,
        retry_seconds: int = 120,
    ) -> str:
        # Match the ordinary survival worker's log-then-group lock order.
        # Lock only the log table because its eager relationships use nullable joins.
        await self.db.scalar(
            select(AdDeliveryLog)
            .where(AdDeliveryLog.id == claim.log_id)
            .with_for_update(of=AdDeliveryLog)
            .execution_options(populate_existing=True)
        )
        state = await self.frequency.state(claim.group_key, lock=True)
        if (
            state is None
            or state.status != "active"
            or state.epoch != claim.epoch
            or state.daily_review_token != claim.token
            or state.daily_review_expires_at is None
            or state.daily_review_expires_at <= now
        ):
            return "daily_stale_claim"
        plan = await self.plan(state)
        state.daily_review_token = None
        state.daily_review_expires_at = None
        config = await self.db.scalar(
            select(AccountOperationConfig)
            .where(
                AccountOperationConfig.account_id == claim.account_id,
            )
            .execution_options(populate_existing=True)
        )
        if not (
            config
            and config.enabled
            and config.auto_ads_enabled
            and config.adaptive_ads_enabled
            and config.dynamic_capacity_enabled
        ):
            state.daily_review_error = "account_auto_ads_disabled"
            state.daily_review_retry_at = now + timedelta(minutes=2)
            return "daily_review_retry"
        if (
            plan.reason
            or plan.log is None
            or plan.log.id != claim.log_id
            or plan.due_at is None
            or now < plan.due_at
        ):
            state.daily_review_error = plan.reason or "frequency_daily_target_changed"
            state.daily_review_retry_at = now + timedelta(seconds=max(120, retry_seconds))
            return "daily_review_retry"
        log = plan.log
        context = payload(log.qualification_context_json)
        previous = list(context.get("daily_frequency_observations") or [])[-9:]
        negative = negative_observation(
            facts,
            previous + list(context.get("survival_observations") or [])[-9:],
            now,
        )
        # A verified absence also counts when the group has automatic expiry;
        # it does not identify who removed the message or assert an administrator deletion.
        if (
            negative is None
            and not facts.get("errors")
            and facts.get("exists") is False
            and all(
                facts.get(key) is True
                for key in ("group_accessible", "account_readable", "member", "can_send")
            )
        ):
            for observation in reversed(previous):
                old = observation.get("facts") or {}
                try:
                    age = now - datetime.fromisoformat(observation["checked_at"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    timedelta(seconds=120) <= age <= DAY
                    and not old.get("errors")
                    and old.get("exists") is False
                    and all(
                        old.get(key) is True
                        for key in (
                            "group_accessible",
                            "account_readable",
                            "member",
                            "can_send",
                        )
                    )
                ):
                    negative = "deleted"
                    break
        previous.append({"checked_at": now.isoformat(), "facts": facts, "reason": reason})
        context["daily_frequency_observations"] = previous[-10:]
        log.qualification_context_json = json.dumps(context, ensure_ascii=False)
        positive = (
            reason is None
            and not facts.get("errors")
            and all(
                facts.get(key) is True
                for key in (
                    "exists",
                    "group_accessible",
                    "account_readable",
                    "member",
                    "can_send",
                    "is_own_message",
                )
            )
        )
        if positive:
            try:
                created = datetime.fromisoformat(facts["message_created_at"])
                positive = abs((created - log.sent_at).total_seconds()) <= 60
            except (KeyError, TypeError, ValueError):
                positive = False
        if negative is None and not positive:
            state.daily_review_error = (reason or "frequency_daily_message_unconfirmed")[:160]
            state.daily_review_retry_at = now + timedelta(seconds=max(120, retry_seconds))
            await self.db.flush()
            return "daily_review_retry"
        old_quota = state.quota
        if negative == "muted":
            await self.db.flush()
            await self.frequency.observe(log, "muted", now)
        elif negative == "deleted":
            state.quota = max(1, state.quota // 2)
            state.reason = "frequency_daily_deleted"
            if old_quota == 1:
                state.status = "exit_pending"
                state.reason = "frequency_deleted_at_minimum"
            log.survival_status = "deleted"
            log.survival_stage = "complete"
            log.survival_error = "frequency_daily_deleted"
            log.survival_check_due_at = None
        else:
            state.quota = max(1, min(next_quota(state.quota), rule_quota(context)))
            state.mature = True
            state.reason = None
        # Advancing the epoch also invalidates old reservations and delayed callbacks.
        self.db.add(
            GroupAdFrequencyEvent(
                telegram_group_id=state.telegram_group_id,
                log_id=log.id,
                kind="daily_" + (negative or "survived"),
                epoch=state.epoch,
                old_quota=old_quota,
                new_quota=state.quota,
                created_at=now,
            )
        )
        state.epoch += 1
        state.epoch_started_at = now
        state.daily_review_due_at = now + DAY
        state.promote_after = now + DAY
        state.daily_review_checked_at = now
        state.daily_review_retry_at = None
        state.daily_review_error = None
        state.updated_at = now
        await self.db.flush()
        return "daily_" + (negative or "survived")


async def run_daily_frequency_reviews(service: Any, *, limit: int = 50) -> dict[str, int]:
    """Use the existing account pool and governed read channel, outside DB transactions."""
    from app.core.account.rpc_governor import RpcDeferred
    from app.modules.acquisition.survival_reads import SurvivalReadBatch

    db = service.db
    now = datetime.utcnow()
    daily = DailyFrequencyService(db)
    keys = list(
        (
            await db.scalars(
                select(GroupAdFrequency.telegram_group_id)
                .where(
                    GroupAdFrequency.status == "active",
                    or_(
                        GroupAdFrequency.daily_review_due_at <= now,
                        (
                            GroupAdFrequency.daily_review_due_at.is_(None)
                            & (GroupAdFrequency.epoch_started_at <= now - DAY)
                        ),
                    ),
                    or_(
                        GroupAdFrequency.daily_review_retry_at.is_(None),
                        GroupAdFrequency.daily_review_retry_at <= now,
                    ),
                    or_(
                        GroupAdFrequency.daily_review_token.is_(None),
                        GroupAdFrequency.daily_review_expires_at <= now,
                    ),
                )
                .order_by(GroupAdFrequency.daily_review_due_at, GroupAdFrequency.epoch_started_at)
                .limit(max(1, limit))
            )
        ).all()
    )
    result: dict[str, int] = {"daily_processed": 0}
    await db.commit()
    reads = SurvivalReadBatch(service.account_pool)
    try:
        for key in keys:
            claim = await daily.claim(key, datetime.utcnow())
            await db.commit()
            if claim is None:
                continue
            result["daily_processed"] += 1
            try:
                account = await db.scalar(
                    select(TelegramAccount).where(
                        TelegramAccount.id == claim.account_id,
                    )
                )
                log = await db.scalar(select(AdDeliveryLog).where(AdDeliveryLog.id == claim.log_id))
                await db.commit()
                await service._sync_account_pool([account] if account else [])
                facts: dict[str, Any] = {}
                reason = None
                retry_seconds = 120
                try:
                    facts = await service._read_survival_facts(log, "ad_survival_check", reads)
                    reason = service._survival_unknown_reason(facts, log, datetime.utcnow())
                except RpcDeferred as exc:
                    reason = exc.reason
                    retry_seconds = exc.retry_after_seconds
                except Exception as exc:
                    reason = "daily_read_unknown:" + type(exc).__name__
                outcome = await daily.finish(
                    claim,
                    facts,
                    reason,
                    datetime.utcnow(),
                    retry_seconds=retry_seconds,
                )
                await db.commit()
                result[outcome] = result.get(outcome, 0) + 1
            except Exception as exc:
                await reads.close()
                await db.rollback()
                result["daily_check_failed"] = result.get("daily_check_failed", 0) + 1
                logger.error(
                    "daily_frequency_review_failed",
                    log_id=claim.log_id,
                    error_type=type(exc).__name__,
                )
    finally:
        await reads.close()
    return result
