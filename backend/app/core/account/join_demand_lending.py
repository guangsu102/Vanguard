"""Lend an idle join floor only after checking final admission and maintenance."""

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, cast, exists, func, or_, select


async def maintenance_due(db: Any, account_id: int, now: datetime) -> list[str]:
    """Check local due work for one account without scanning all account sets."""
    from app.core.automation_settings import get_auto_join_scheduler_settings
    from app.core.group.identity import is_owned_group_target
    from app.core.group.models import Group
    from app.core.group.models import GroupAccountMembership as Member
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.join_reconciliation import unresolved_join_request_due
    from app.modules.acquisition.models import (
        AdDeliveryLog,
        AutoJoinAttempt,
        GroupAdFrequency,
        GroupAdFrequencyEvent,
    )
    from app.modules.acquisition.models import (
        GroupQualificationAudit as Audit,
    )
    from app.modules.acquisition.qualification_service import (
        _date,
        exit_scope_filters,
        manually_protected,
        policy,
        verification_account_allowed,
    )
    from app.modules.acquisition.qualification_verification import action_count

    requests = select(AutoJoinAttempt.id).where(
        AutoJoinAttempt.account_id == account_id,
        unresolved_join_request_due(now),
    )
    if await db.scalar(select(exists(requests))):
        return ["join_request_reconciliation"]
    config = await policy(db, fresh=True)
    if (
        config.get("enabled")
        and config.get("execute_exits")
        and (
            config.get("exit_all_accounts") is True
            or account_id in (config.get("account_ids") or [])
        )
    ):
        membership_ids, reasons = exit_scope_filters(config)
        if membership_ids != set() and reasons != set():
            ordinary = and_(
                Member.review_status.in_(
                    ["exit_pending", "leave_failed", "membership_reconciliation"]
                ),
                Member.review_next_at <= now,
            )
            frequency = (
                select(AdDeliveryLog.id)
                .join(
                    GroupAdFrequencyEvent,
                    GroupAdFrequencyEvent.log_id == AdDeliveryLog.id,
                )
                .join(
                    GroupAdFrequency,
                    GroupAdFrequency.telegram_group_id == GroupAdFrequencyEvent.telegram_group_id,
                )
                .where(
                    AdDeliveryLog.account_id == Member.account_id,
                    AdDeliveryLog.group_id == Member.group_id,
                    GroupAdFrequency.status == "exit_pending",
                )
                .correlate(Member)
            )
            frequency_due = and_(
                Member.status.in_(["joined", "leave_failed"]),
                exists(frequency),
                or_(Member.leave_retry_at.is_(None), Member.leave_retry_at <= now),
            )
            query = (
                select(Member, Group, ordinary)
                .join(Group, Group.id == Member.group_id)
                .where(
                    Member.account_id == account_id,
                    or_(ordinary, frequency_due),
                )
            )
            if membership_ids is not None:
                query = query.where(Member.id.in_(membership_ids))
            exits = (await db.execute(query.limit(301))).all()
            if len(exits) > 300:
                raise ValueError("exit_inventory_incomplete")
            for member, group, ordinary_due in exits:
                if manually_protected(config, group, member) or await is_owned_group_target(
                    db,
                    core_group_id=group.id,
                    telegram_group_id=group.group_id,
                ):
                    continue
                # Ordinary exits first collect fresh evidence or reconcile an
                # old membership; these reads are due before the leave verdict.
                if ordinary_due:
                    return ["auto_join_leave"]
                from app.modules.acquisition.adaptive_frequency import FrequencyService

                reason = await FrequencyService(db).exit_reason(account_id, group, member)
                if reason and (reasons is None or reason in reasons):
                    return ["auto_join_leave"]
    if not (
        config.get("enabled")
        and config.get("execute_verification")
        and verification_account_allowed(config, account_id)
    ):
        return []
    if not (await get_auto_join_scheduler_settings(db))["join_verification"]["enabled"]:
        return []
    latest = (
        select(func.max(Audit.id))
        .where(
            Audit.membership_id == Member.id,
            Audit.membership_joined_at == Member.joined_at,
            Audit.state != "cancelled",
        )
        .correlate(Member)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Member, Group, SystemSetting.value)
            .join(
                Group,
                Group.id == Member.group_id,
            )
            .join(
                Audit,
                Audit.id == latest,
            )
            .outerjoin(
                SystemSetting,
                SystemSetting.key == "qualification.verification." + cast(Member.id, String),
            )
            .where(
                Member.account_id == account_id,
                Member.status.in_(["joined", "pending"]),
                Member.joined_at >= now - timedelta(hours=48),
                Audit.state == "completed",
                Audit.policy_version == POLICY_VERSION,
                Audit.content_scope == "text_profile",
                Audit.decision.in_(["observe", "wait"]),
            )
            .limit(301)
        )
    ).all()
    if len(rows) > 300:
        raise ValueError("verification_inventory_incomplete")
    for member, group, value in rows:
        ledger = json.loads(value) if value else {}
        if ledger.get("membership_version") != member.joined_at.isoformat():
            ledger = {}
        if (
            action_count(ledger) < 3
            and (_date(ledger.get("claim_until")) or datetime.min) <= now
            and (_date(ledger.get("next_check_at")) or datetime.min) <= now
        ):
            if manually_protected(config, group, member) or await is_owned_group_target(
                db,
                core_group_id=group.id,
                telegram_group_id=group.group_id,
            ):
                continue
            return ["qualification_verification"]
    return []


async def add_join_demand_headroom(
    db: Any,
    account_id: int,
    now: datetime,
    limits: dict,
    budget: dict,
) -> None:
    from app.core.account.models import AccountOperationConfig, TelegramAccount
    from app.core.account.read_work import initial_pending
    from app.core.account.work_demand import _join_demand_snapshot
    from app.core.automation_settings import get_auto_join_scheduler_settings
    from app.core.scheduler.growth_dispatch import eligible_accounts
    from app.modules.acquisition.growth_admission import admission_plan
    from app.modules.acquisition.models import AutoJoinAttempt

    def apply(hold: int, reason: str, **details: Any) -> None:
        limits.update(join_idle_hold_hour=hold, join_idle_hold_day=hold)
        budget["join_idle_lending"] = {
            "reason": reason,
            "holds": {"hour": hold, "day": hold},
            **details,
        }

    reservation = budget.get("work_reservation") or {}
    active_reads = reservation.get("remaining", 0) > 0 and (
        reservation.get("kind") == "join"
        or (reservation.get("kind") == "review" and reservation.get("pending") is True)
    )
    active_request = await db.scalar(
        select(
            exists(
                select(AutoJoinAttempt.id).where(
                    AutoJoinAttempt.account_id == account_id,
                    AutoJoinAttempt.request_state == "reserved",
                    AutoJoinAttempt.reservation_expires_at > now,
                )
            )
        )
    )
    if active_reads or active_request:
        # Admitted reads remain protected even if a toggle or forecast changes.
        apply(12, "active_join_or_review_reservation")
        return
    # The existing dispatcher retains the account's risk and spam gates.
    if await db.scalar(eligible_accounts(now).where(TelegramAccount.id == account_id)) is None:
        apply(0, "account_not_eligible")
        return
    due = await maintenance_due(db, account_id, now)
    if due:
        apply(12, "due_join_maintenance", due=due)
        return
    config = await db.scalar(
        select(AccountOperationConfig).where(
            AccountOperationConfig.account_id == account_id,
        )
    )
    if (
        config is None
        or not config.enabled
        or not config.auto_join_enabled
        or config.operation_mode == "ad_only"
        or config.max_groups_per_day <= 0
    ):
        apply(0, "auto_join_disabled")
        return
    if config.next_join_after and config.next_join_after > now:
        apply(0, "join_interval", next_join_after=config.next_join_after.isoformat())
        return
    if config.join_review_backlog_paused or await initial_pending(db, account_id, 0):
        apply(0, "join_wait_pending_review")
        return
    if not (await get_auto_join_scheduler_settings(db))["enabled"]:
        apply(0, "join_scheduler_disabled")
        return
    # This stage runs after ad/survival/sync forecasts. Passing the budget avoids
    # a recursive snapshot and tests the same final daily debt as real joining.
    admission = await admission_plan(db, account_id, now, rpc={**budget, "limits": limits})
    if admission["remaining"] <= 0:
        apply(0, admission["reason"], admission=admission)
        return
    demand = await _join_demand_snapshot(db, account_id, now)
    if demand["active_request_reservation"]:
        apply(12, "active_join_or_review_reservation", candidate_inventory=demand)
    elif demand["executable"]:
        apply(12, "executable_join_candidate", candidate_inventory=demand, admission=admission)
    elif not demand["sample_complete"]:
        # A bounded incomplete scan cannot prove that the join floor is idle.
        apply(-1, "candidate_inventory_incomplete", candidate_inventory=demand)
    elif demand["retry_wait"]:
        apply(0, "candidate_retry_wait", candidate_inventory=demand)
    elif demand["preview_pending"]:
        apply(12, "candidate_preview_due", candidate_inventory=demand, admission=admission)
    else:
        apply(0, "no_executable_join_candidate", candidate_inventory=demand)
