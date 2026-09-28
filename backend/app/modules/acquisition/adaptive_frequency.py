"""Shared adaptive ad policy. Mutations are serialized on a canonical group row.

No Telegram calls or commits here: callers commit feedback with the original log.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig
from app.modules.acquisition.ad_readiness import AdReadiness
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency, GroupAdFrequencyEvent
from app.modules.acquisition.qualification_identity import (
    identity_aliases,
    identity_relation,
    peer_identity,
)

STEPS = (1, 2, 4, 8, 16, 30)
DAY = timedelta(hours=24)


def payload(raw: Any) -> dict:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, (str, type(None))) else raw
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def frequency_context(log: Any) -> dict:
    return payload(getattr(log, "qualification_context_json", None)).get("frequency") or {}


def canonical(target: Any, context: dict | None = None) -> int | None:
    identity = peer_identity(target, context)
    if identity is None or identity[1] is None:
        return None
    return -identity[0] - (1_000_000_000_000 if identity[1] == "channel" else 0)


async def enabled(db: Any, account_id: int) -> bool:
    config = await db.scalar(
        select(AccountOperationConfig)
        .where(AccountOperationConfig.account_id == account_id)
        .execution_options(populate_existing=True)
    )
    return bool(
        config and config.adaptive_ads_enabled is True and config.dynamic_capacity_enabled is True
    )


def interval_seconds(quota: int) -> int:
    return ceil(86400 / max(1, min(30, quota)))


def next_quota(quota: int) -> int:
    return min(30, max(1, quota) * 2)


def rule_quota(context: dict | None) -> int:
    """Conservative numeric frequency constraints from current authoritative rules."""
    context = context or {}
    cap = min(30, max(0, int(context.get("frequency_rule_quota", 30))))
    number = r"(\d{1,3}|[一二三四五六七八九十两]{1,3})"
    digits = {ch: value for value, ch in enumerate("零一二三四五六七八九")}

    def integer(value: str) -> int:
        if value.isdigit():
            return int(value)
        value = value.replace("两", "二")
        if "十" in value:
            left, right = value.split("十", 1)
            return (digits.get(left, 1) if left else 1) * 10 + digits.get(right, 0)
        return digits.get(value, 0)

    for item in context.get("evidence", []):
        if not isinstance(item, dict):
            continue
        authoritative = (
            item.get("sender_role") == "group_metadata" or item.get("verified_admin") is True
        )
        text = str(item.get("text") or "")
        if not authoritative or not re.search(r"广告|advertis|\bads?\b", text, re.I):
            continue
        daily = re.search(
            r"(?:每天|每日|24\s*小时)(?:[^。！？\n]{0,12}?)" + number + r"\s*(?:条|次)", text
        )
        english = re.search(number + r"\s*(?:ads?|advertisements?)\s*(?:per|/)\s*day", text, re.I)
        if daily or english:
            cap = min(cap, integer((daily or english).group(1)))
        spacing = re.search(
            r"(?:间隔|每隔?|interval)[^。！？\n\d一二三四五六七八九十两]{0,8}"
            + number
            + r"\s*(小时|分钟|hours?|minutes?)",
            text,
            re.I,
        )
        if not (daily or english or spacing) and re.search(
            r"每天|每日|每周|每月|小时|分钟|间隔|频率|per\s+(?:day|hour)|daily|interval", text, re.I
        ):
            cap = min(cap, 1)  # Unparsed frequency conditions never authorize escalation.
        if spacing:
            seconds = integer(spacing.group(1)) * (
                3600 if spacing.group(2).lower().startswith(("小时", "hour")) else 60
            )
            if seconds:
                cap = min(cap, 86400 // seconds)
    return cap


def negative_observation(facts: dict, previous: list[dict], now: datetime) -> str | None:
    """A failed read is never a deletion or a mute."""
    if facts.get("errors") or not all(
        facts.get(k) is True for k in ("group_accessible", "account_readable", "member")
    ):
        return None
    if facts.get("can_send") is False:
        from app.modules.acquisition.send_restriction import verified_long_restriction
        if verified_long_restriction({"permissions": facts.get("send_restriction") or {}}, now):
            return "muted"
    if (
        facts.get("can_send") is not True
        or facts.get("exists") is not False
        or facts.get("ttl_period") != 0
    ):
        return None
    for item in reversed(previous):
        old = item.get("facts") or {}
        try:
            at = datetime.fromisoformat(item["checked_at"])
        except (ValueError, KeyError, TypeError):
            continue
        if not timedelta(seconds=120) <= now - at <= DAY:
            continue
        if (
            not old.get("errors")
            and old.get("exists") is False
            and old.get("ttl_period") == 0
            and all(
                old.get(k) is True
                for k in ("group_accessible", "account_readable", "member", "can_send")
            )
        ):
            return "deleted"
    return None


class FrequencyService:
    def __init__(self, db: Any):
        self.db = db

    async def state(
        self,
        target: int,
        context: dict | None = None,
        *,
        lock: bool = False,
        create: bool = False,
        now: datetime | None = None,
    ) -> GroupAdFrequency | None:
        key = canonical(target, context)
        if key is None:
            return None
        if create:
            dialect = self.db.get_bind().dialect.name
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            elif dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
            else:
                raise RuntimeError("adaptive_frequency_lock_unsupported")
            await self.db.execute(
                insert(GroupAdFrequency)
                .values(
                    telegram_group_id=key,
                    quota=1,
                    epoch=1,
                    epoch_started_at=now or datetime.utcnow(),
                    mature=False,
                    status="active",
                    updated_at=now or datetime.utcnow(),
                )
                .on_conflict_do_nothing(index_elements=["telegram_group_id"])
            )
        query = select(GroupAdFrequency).where(GroupAdFrequency.telegram_group_id == key)
        if lock:
            query = query.with_for_update()
        return await self.db.scalar(query.execution_options(populate_existing=True))

    async def logs(self, target: int, context: dict | None = None) -> list[AdDeliveryLog]:
        identity = peer_identity(target, context)
        return list(
            (
                await self.db.scalars(
                    select(AdDeliveryLog)
                    .where(AdDeliveryLog.telegram_group_id.in_(identity_aliases(identity)))
                    .order_by(AdDeliveryLog.created_at, AdDeliveryLog.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def readiness(
        self,
        target: int,
        now: datetime,
        *,
        context: dict | None = None,
        reservation_token: str | None = None,
        lock: bool = False,
    ) -> tuple[AdReadiness, GroupAdFrequency | None]:
        key = canonical(target, context)
        if key is None:
            return AdReadiness("qualification_group_identity_unknown"), None
        state = await self.state(target, context, lock=lock, create=lock, now=now)
        if state and state.status != "active":
            return AdReadiness(state.reason or "frequency_blocked"), state
        if state and state.pause_until and state.pause_until > now:
            return AdReadiness("frequency_deleted_cooldown", state.pause_until), state
        logs = await self.logs(target, context)
        events = list(
            (
                await self.db.scalars(
                    select(GroupAdFrequencyEvent).where(
                        GroupAdFrequencyEvent.telegram_group_id == key,
                        GroupAdFrequencyEvent.kind.in_(["deleted", "muted", "daily_deleted", "daily_muted", "daily_survived"]),
                    )
                )
            ).all()
        )
        resolved = {e.log_id for e in events}
        exposures = []
        identity = peer_identity(key)
        for log in logs:
            old = payload(log.qualification_context_json)
            if log.status == "success" or log.telegram_message_id is not None:
                if identity_relation(identity, peer_identity(log.telegram_group_id, old)) != "same":
                    return AdReadiness("qualification_group_identity_unknown"), state
                if log.status != "success" or log.sent_at is None:
                    return AdReadiness("qualification_delivery_reconciliation_required"), state
                exposures.append(log)
                if log.id in resolved or frequency_context(log).get("version") == 2:
                    continue
                if log.survival_status in {"deleted", "check_failed"} or log.survival_error:
                    return AdReadiness("frequency_survival_unresolved"), state
                if log.survival_status == "pending" and (
                    log.survival_check_due_at is None or log.survival_check_due_at <= now
                ):
                    return AdReadiness("frequency_survival_due", log.survival_check_due_at), state
                if log.survival_status not in {"survived", "pending"}:
                    return AdReadiness("frequency_survival_unresolved"), state
                continue
            if log.status in {
                "unknown",
                "sending",
                "reconciliation_required",
            } or "send_outcome_unknown" in (log.error or ""):
                return AdReadiness("qualification_delivery_reconciliation_required"), state
            if log.status == "pending":
                if reservation_token and log.reservation_token == reservation_token:
                    fc = frequency_context(log)
                    if (
                        state is None
                        or fc.get("epoch") != state.epoch
                        or fc.get("quota") != state.quota
                    ):
                        return AdReadiness("frequency_reservation_stale"), state
                    continue
                return AdReadiness("frequency_group_inflight"), state
        if state:
            from app.modules.acquisition.daily_frequency import DailyFrequencyService

            review_logs = [log for log in logs if not (
                log.status == "pending" and reservation_token
                and log.reservation_token == reservation_token
            )]
            review = await DailyFrequencyService(self.db).plan(state, logs=review_logs)
            if review.reason:
                return AdReadiness(review.reason), state
            if review.due_at and review.due_at <= now:
                reason = "frequency_daily_review_unknown" if state.daily_review_error else "frequency_daily_review_due"
                return AdReadiness(reason, state.daily_review_retry_at or review.due_at), state
        quota = min(state.quota if state else 1, rule_quota(context))
        if quota <= 0:
            return AdReadiness("frequency_group_rule_limit"), state
        recent = [log for log in exposures if log.sent_at > now - DAY]
        if len(recent) >= quota:
            release = sorted(log.sent_at for log in recent)[len(recent) - quota] + DAY
            return AdReadiness("frequency_group_daily_cap", release), state
        latest = max(exposures, key=lambda log: log.sent_at, default=None)
        if latest:
            due = latest.sent_at + timedelta(seconds=interval_seconds(quota))
            if due > now:
                return AdReadiness("frequency_group_interval", due), state
            if latest.id not in resolved and frequency_context(latest).get("version") != 2:
                if latest.survived_two_minute_at is None:
                    return AdReadiness("frequency_first_checkpoint_required"), state
                if not (state and state.mature) and latest.survival_status != "survived":
                    return AdReadiness("qualification_previous_survival_unresolved"), state
        return AdReadiness(), state

    @staticmethod
    def context(state: GroupAdFrequency) -> dict:
        return {
            "epoch": state.epoch,
            "quota": state.quota,
            "lane": "mature" if state.mature else "probe",
            "telegram_group_id": state.telegram_group_id,
            "version": 2,
        }

    async def observe(
        self, log: AdDeliveryLog, kind: str, now: datetime
    ) -> GroupAdFrequency | None:
        context = payload(log.qualification_context_json)
        fc = frequency_context(log)
        if not fc:
            return None  # Historical deliveries do not bootstrap a high quota.
        state = await self.state(log.telegram_group_id, context, lock=True)
        if state is None:
            return None
        if await self.db.scalar(
            select(GroupAdFrequencyEvent.id).where(
                GroupAdFrequencyEvent.log_id == log.id, GroupAdFrequencyEvent.kind == kind
            )
        ):
            return state
        if kind not in {"survived", "deleted", "muted"}:
            raise ValueError("invalid frequency observation")
        epoch = int(fc["epoch"])
        old_quota = state.quota
        if kind == "survived":
            if not (
                log.sent_at
                and log.telegram_message_id
                and log.status == "success"
                and log.survived_two_minute_at
                and log.survived_one_hour_at
                and now >= log.sent_at + DAY
                and log.survival_status == "survived"
            ):
                return state
        elif kind == "muted":
            state.status = "exit_pending"
            state.reason = "frequency_muted"
        self.db.add(
            GroupAdFrequencyEvent(
                telegram_group_id=state.telegram_group_id,
                log_id=log.id,
                kind=kind,
                epoch=epoch,
                old_quota=old_quota,
                new_quota=state.quota,
                created_at=now,
            )
        )
        await self.db.flush()
        # Per-message checkpoints retain evidence; only the daily last-message review
        # changes the quota. Late callbacks cannot adjust a newer cycle.
        state.updated_at = now
        return state

    async def exit_reason(self, account_id: int, group: Any, member: Any) -> str | None:
        if not await enabled(self.db, account_id):
            return None
        if not member.joined_at:
            return None
        pairs = list(
            (
                await self.db.execute(
                    select(GroupAdFrequency, AdDeliveryLog, GroupAdFrequencyEvent)
                    .join(
                        GroupAdFrequencyEvent,
                        GroupAdFrequencyEvent.telegram_group_id
                        == GroupAdFrequency.telegram_group_id,
                    )
                    .join(AdDeliveryLog, AdDeliveryLog.id == GroupAdFrequencyEvent.log_id)
                    .where(
                        GroupAdFrequency.status == "exit_pending",
                        AdDeliveryLog.group_id == group.id,
                        AdDeliveryLog.account_id == account_id,
                        (
                            (AdDeliveryLog.sent_at >= member.joined_at)
                            | (
                                (AdDeliveryLog.sent_at.is_(None))
                                & (AdDeliveryLog.created_at >= member.joined_at)
                            )
                        ),
                        GroupAdFrequencyEvent.kind.in_(["deleted", "muted", "daily_deleted"]),
                        (GroupAdFrequencyEvent.kind != "muted") | ~GroupAdFrequencyEvent.log_id.in_(
                            select(GroupAdFrequencyEvent.log_id).where(GroupAdFrequencyEvent.kind == "mute_revoked")
                        ),
                    )
                )
            ).all()
        )
        for state, log, event in pairs:
            if (
                canonical(log.telegram_group_id, payload(log.qualification_context_json))
                != state.telegram_group_id
            ):
                continue
            if (
                event.kind == "muted"
                or frequency_context(log).get("sent_quota", frequency_context(log).get("quota"))
                == 1
            ):
                return state.reason
        return None

    async def summary(self, target: int, now: datetime, context: dict | None = None) -> dict:
        ready, state = await self.readiness(target, now, context=context)
        from app.modules.acquisition.daily_frequency import DailyFrequencyService, DailyReviewPlan

        review = await DailyFrequencyService(self.db).plan(state) if state else DailyReviewPlan()
        review_status = "idle"
        if state:
            if state.status != "active":
                review_status = "blocked"
            elif state.daily_review_error or review.reason:
                review_status = "retry"
            elif state.daily_review_token and state.daily_review_expires_at and state.daily_review_expires_at > now:
                review_status = "checking"
            elif review.due_at:
                review_status = "due" if review.due_at <= now else "waiting"
        return {
            "telegram_group_id": canonical(target, context),
            "quota": min(state.quota if state else 1, rule_quota(context)),
            "mature": bool(state and state.mature),
            "successes": 0,
            "frequency_policy": "daily_last_message",
            "daily_review_status": review_status,
            "daily_review_due_at": review.due_at.isoformat() if review.due_at else None,
            "daily_review_log_id": review.log.id if review.log else None,
            "daily_review_checked_at": state.daily_review_checked_at.isoformat() if state and state.daily_review_checked_at else None,
            "daily_review_error": (state.daily_review_error if state else None) or review.reason,
            "status": state.status if state else "active",
            "reason": ready.reason,
            "next_allowed_at": ready.next_allowed_at.isoformat() if ready.next_allowed_at else None,
        }


async def run_frequency_exits(service: Any, *, limit: int = 1) -> dict:
    """Use the existing serialized exit worker, scope and live protection checks."""
    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition.qualification_actions import reconcile_exit
    from app.modules.acquisition.qualification_service import authorize_leave

    db = service.db
    now = datetime.utcnow()
    rows = list(
        (
            await db.execute(
                select(GroupAccountMembership, Group)
                .join(Group, Group.id == GroupAccountMembership.group_id)
                .join(
                    AdDeliveryLog,
                    (AdDeliveryLog.group_id == Group.id)
                    & (AdDeliveryLog.account_id == GroupAccountMembership.account_id),
                )
                .join(GroupAdFrequencyEvent, GroupAdFrequencyEvent.log_id == AdDeliveryLog.id)
                .join(
                    GroupAdFrequency,
                    GroupAdFrequency.telegram_group_id == GroupAdFrequencyEvent.telegram_group_id,
                )
                .where(
                    GroupAdFrequency.status == "exit_pending",
                    GroupAccountMembership.status.in_(["joined", "leave_failed"]),
                )
                .order_by(GroupAccountMembership.id)
            )
        )
        .unique()
        .all()
    )
    results = []
    for member, group in rows:
        if len(results) >= limit:
            break
        reason = await FrequencyService(db).exit_reason(member.account_id, group, member)
        if not reason or (member.leave_retry_at and member.leave_retry_at > now):
            continue
        member.ad_status, member.review_status = "blocked", "exit_pending"
        await db.commit()
        allowed, blocked = await authorize_leave(db, member.account_id, group, member)
        if not allowed:
            member.leave_error = blocked
            member.leave_retry_at = now + timedelta(minutes=5)
            await db.commit()
            results.append(
                {"membership_id": member.id, "status": "exit_pending", "reason": blocked}
            )
            continue
        if member.leave_attempts:
            observed = await reconcile_exit(service, member, group)
            if observed == "left":
                member.status, member.review_status = "left", "left"
                member.left_at = member.leave_confirmed_at = now
                member.leave_retry_at = member.review_next_at = None
                await db.commit()
                results.append({"membership_id": member.id, "status": "left"})
                continue
            if observed != "member":
                member.leave_error = "frequency_exit_reconciliation_" + observed
                member.leave_retry_at = now + timedelta(hours=2)
                await db.commit()
                results.append(
                    {
                        "membership_id": member.id,
                        "status": "exit_pending",
                        "reason": member.leave_error,
                    }
                )
                continue
        # Persist attempted intent before RPC: a crash must reconcile membership first.
        member.leave_attempts = (member.leave_attempts or 0) + 1
        member.leave_error = "frequency_exit_attempted"
        member.leave_retry_at = now + timedelta(minutes=5)
        await db.commit()
        error = await service._leave_group(
            member.account_id, service._discovered_group_from_model(group)
        )
        if error == "qualification_send_restriction_cleared":
            # The final live check already queued fresh qualification and revoked the intent.
            results.append({"membership_id": member.id, "status": member.review_status, "reason": error})
            continue
        if error is None:
            member.status, member.review_status = "left", "left"
            member.left_at = member.leave_confirmed_at = datetime.utcnow()
            member.leave_retry_at = member.review_next_at = None
            member.leave_error = None
        else:
            member.review_status, member.ad_status = "exit_pending", "blocked"
            member.leave_error = str(error)[:500]
            member.leave_retry_at = member.review_next_at = now + timedelta(minutes=5)
        await db.commit()
        results.append({"membership_id": member.id, "status": member.review_status})
    return {"processed": len(results), "results": results}


async def queue_deleted_observation(db: Any, account_id: int, event: Any) -> None:
    """Deletion notifications prompt a verified read, never an unscoped penalty."""
    if not await enabled(db, account_id):
        return
    target = canonical(getattr(event, "chat_id", None))
    ids = [value for value in (getattr(event, "deleted_ids", None) or []) if type(value) is int]
    if target is None or not ids:
        return
    logs = list(
        (
            await db.scalars(
                select(AdDeliveryLog)
                .where(
                    AdDeliveryLog.account_id == account_id,
                    AdDeliveryLog.telegram_group_id.in_(identity_aliases(peer_identity(target))),
                    AdDeliveryLog.telegram_message_id.in_(ids),
                    AdDeliveryLog.status == "success",
                )
                .with_for_update(of=AdDeliveryLog)
            )
        ).all()
    )
    for log in logs:
        if not frequency_context(log) or log.survival_status == "deleted":
            continue
        if canonical(log.telegram_group_id, payload(log.qualification_context_json)) != target:
            continue
        if log.survival_stage == "complete":
            log.survival_stage = "twenty_four_hour"
        log.survival_status = "pending"
        log.survival_error = "deletion_event_requires_confirmation"
        log.survival_check_due_at = datetime.utcnow()
        log.survival_claim_token = None
        log.survival_claim_expires_at = None
        log.survival_version = (log.survival_version or 0) + 1
    await db.commit()


async def record_live_mute(db: Any, account_id: int, target: int, context: dict) -> None:
    from app.modules.acquisition.send_restriction import verified_long_restriction
    if not verified_long_restriction(context, datetime.utcnow()):
        return
    if not await enabled(db, account_id):
        return
    logs = await FrequencyService(db).logs(target, context)
    for log in reversed(logs):
        if (
            log.account_id == account_id
            and frequency_context(log)
            and log.status in {"pending", "success", "sending"}
        ):
            await FrequencyService(db).observe(log, "muted", datetime.utcnow())
            await db.commit()
            return
