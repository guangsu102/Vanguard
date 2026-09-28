"""Read-only dynamic inventory and capacity projection for promoted accounts."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.account.outbound_budget import AccountOutboundBudgetService, effective_capacity_limits
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.ad_readiness import AdReadiness, schedule_readiness
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdDeliveryLog,
    AdDeliveryScheduleState,
    GroupQualificationAudit,
    GroupAdFrequency,
    GroupAdProfile,
)


async def inventory_snapshot(
    db: Any, account_id: int, now: datetime | None = None
) -> dict[str, int]:
    now = now or datetime.utcnow()
    members = list(
        (
            await db.execute(
                select(*(getattr(GroupAccountMembership, key) for key in (
                    "id", "joined_at", "review_status", "status", "telegram_group_id",
                    "ad_status", "ad_pause_until", "left_at", "group_id",
                ))).where(
                    GroupAccountMembership.account_id == account_id,
                    GroupAccountMembership.status.in_(["joined", "pending", "leave_failed"]),
                )
            )
        ).all()
    )
    ids = {member.id: member for member in members}
    columns = ("id", "membership_id", "membership_joined_at", "state", "decision", "reason", "next_retry_at", "expires_at", "evidence_json", "policy_version", "content_scope")
    latest = select(*(getattr(GroupQualificationAudit, key) for key in columns)).where(
        GroupQualificationAudit.account_id == account_id,
        GroupQualificationAudit.membership_id.in_(list(ids)),
        GroupQualificationAudit.state != "cancelled",
    ).order_by(GroupQualificationAudit.id.desc())
    rows = (await db.execute(latest)).all() if ids else []
    from app.modules.acquisition.qualification_continuity import choose_authorization
    grouped = {}
    for row in rows:
        grouped.setdefault(row.membership_id, []).append(row)
    rows = [choose_authorization(items) for items in grouped.values()]
    from app.modules.acquisition.adaptive_frequency import FrequencyService, canonical, payload, rule_quota
    keys = {canonical(ids[row.membership_id].telegram_group_id, payload(row.evidence_json)) for row in rows}
    frequencies = {row.telegram_group_id: row for row in (await db.execute(
        select(GroupAdFrequency.telegram_group_id, GroupAdFrequency.quota,
               GroupAdFrequency.status, GroupAdFrequency.pause_until)
        .where(GroupAdFrequency.telegram_group_id.in_([key for key in keys if key is not None]))
    )).all()} if keys else {}
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    profiles = {row.group_id: row for row in (await db.execute(select(
        GroupAdProfile.group_id, GroupAdProfile.paused_until, GroupAdProfile.ad_policy_source,
        GroupAdProfile.ad_policy_mode, GroupAdProfile.ad_policy_expires_at,
    ).where(GroupAdProfile.group_id.in_([m.group_id for m in members])))).all()} if members else {}
    usable_keys: dict[int, int] = {}
    identity_blocked = 0
    frequency_service = FrequencyService(db)
    seen, ready, active, overdue = set(), 0, 0, 0
    review_due = 0
    manual = sum(member.review_status == "manual_required" for member in members)
    for row in rows:
        if row.membership_id in seen:
            continue
        seen.add(row.membership_id)
        member = ids[row.membership_id]
        if row.membership_joined_at != member.joined_at:
            continue
        if member.review_status == "manual_required" or row.state == "manual_required":
            continue
        if member.status == "joined" and row.next_retry_at and row.next_retry_at <= now:
            review_due += 1
        if (
            row.state in {"completed", "running"}
            and row.decision in {"allowed", "trial"}
            and row.expires_at
            and row.expires_at > now
            and member.status == "joined"
        ):
            ready += 1
            context = payload(row.evidence_json)
            key = canonical(member.telegram_group_id, context)
            frequency = frequencies.get(key)
            profile = profiles.get(member.group_id)
            profile_blocked = profile is not None and (
                (profile.paused_until is not None and profile.paused_until > now)
                or (profile.ad_policy_source == "manual"
                    and profile.ad_policy_mode in {"forbidden", "approval_required"}
                    and (profile.ad_policy_expires_at is None or profile.ad_policy_expires_at > now))
            )
            if (key is not None and not profile_blocked
                    and row.policy_version == POLICY_VERSION and row.content_scope == "text_profile"
                    and member.review_status == "approved"
                    and member.ad_status == "active" and member.left_at is None
                    and (member.ad_pause_until is None or member.ad_pause_until <= now)
                    and (frequency is None or (frequency.status == "active"
                         and (frequency.pause_until is None or frequency.pause_until <= now)))):
                # Use the execution gate's history checks, including legacy
                # receipts without a proven namespace. Ordinary cooldowns keep
                # their future daily slots; unresolved identity/results do not.
                readiness, _ = await frequency_service.readiness(
                    member.telegram_group_id, now, context=context
                )
                if readiness.reason == "qualification_group_identity_unknown":
                    identity_blocked += 1
                if readiness.reason in {None, "frequency_group_daily_cap", "frequency_group_interval"}:
                    usable_keys[key] = min(frequency.quota if frequency else 1, rule_quota(context))
        elif row.state in {"queued", "running", "waiting_ai"} or (
            row.state == "completed"
            and row.decision in {"wait", "technical_wait"}
            and row.next_retry_at
            and row.next_retry_at <= now + timedelta(hours=48)
        ):
            # Only current executable work is pressure; observe/manual/history is reported separately.
            active += 1
            if row.next_retry_at and row.next_retry_at < now - timedelta(hours=1):
                overdue += 1
    return {
        "total": len(members),
        "qualified": ready,
        "usable": len(usable_keys),
        "identity_blocked": identity_blocked,
        "slots_24h": sum(usable_keys.values()),
        "active_backlog": active,
        "overdue": overdue,
        "manual": manual,
        "target_hours": 48,
        "review_due": review_due,
    }


async def executable_inventory(
    db: Any, account_id: int, config: Any, now: datetime, *, include_candidates: bool = True
) -> dict[str, Any]:
    """Project only cached, presently runnable work; this never reserves or sends.

    Live permissions and rule freshness are still rechecked at the RPC boundary.
    Unknown candidate identities are intentionally absent from executable counts.
    """
    from telethon.tl.types import PeerChannel, PeerChat

    from app.core.automation_settings import (
        get_ad_capacity_settings,
        get_ad_delivery_execution_settings,
    )
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.group_qualification import URL
    from app.modules.acquisition.qualification_identity import peer_identity
    from app.modules.acquisition.qualification_join_gate import qualification_join_gate
    from app.modules.acquisition.qualification_service import policy, send_gate

    service = AcquisitionAutomationService(db, account_pool=SimpleNamespace())
    reasons: Counter[str] = Counter()
    qualification = await policy(db, fresh=True)
    execution = await get_ad_delivery_execution_settings(db)
    rollout = (qualification.get("rollout_accounts") or {}).get(str(account_id), {})
    phase = rollout.get("phase", "paused")
    ad_global = True
    if not qualification.get("enabled"):
        reasons["qualification_disabled"] += 1
        ad_global = False
    if not execution.get("enabled"):
        reasons["ad_delivery_paused"] += 1
        ad_global = False
    if phase not in {"pilot", "dynamic"}:
        reasons["qualification_rollout_paused"] += 1
        ad_global = False
    if not config.auto_ads_enabled:
        reasons["account_auto_ads_disabled"] += 1
        ad_global = False
    capacity = await get_ad_capacity_settings(db)
    window = service._ad_window_skip_reason(now, capacity)
    if window:
        reasons[window] += 1
        ad_global = False
    if service._in_quiet_hours(config, now):
        reasons["account_quiet_hours"] += 1
        ad_global = False

    bindings = list(
        (
            await db.scalars(
                select(AccountAdBinding)
                .options(
                    selectinload(AccountAdBinding.campaign), selectinload(AccountAdBinding.creative)
                )
                .where(
                    AccountAdBinding.account_id == account_id, AccountAdBinding.enabled.is_(True)
                )
            )
        ).all()
    )
    bindings = [
        binding
        for binding in bindings
        if binding.campaign
        and binding.campaign.enabled
        and (binding.campaign.start_at is None or binding.campaign.start_at <= now)
        and (binding.campaign.end_at is None or binding.campaign.end_at >= now)
        and binding.campaign.delivery_policy == config.operation_mode
    ]
    materials = []
    for binding in bindings:
        creative = binding.creative
        if (
            creative is not None
            and creative.enabled
            and creative.creative_type == "text"
            and not creative.media_url
            and not creative.link_url
            and service._creative_is_sendable(creative)
            and not URL.search(service._render_ad_content(creative))
        ):
            materials.append((binding, service._render_ad_content(creative)))
    if not materials:
        reasons["qualification_text_profile_material_missing"] += 1
        ad_global = False
    schedules = list(
        (
            await db.scalars(
                select(AdDeliveryScheduleState).where(
                    AdDeliveryScheduleState.account_id == account_id
                )
            )
        ).all()
    )
    scheduled = {(row.campaign_id, row.group_id): row for row in schedules}
    members = list(
        (
            await db.scalars(
                select(GroupAccountMembership)
                .options(selectinload(GroupAccountMembership.group))
                .where(
                    GroupAccountMembership.account_id == account_id,
                    GroupAccountMembership.status == "joined",
                )
                .order_by(GroupAccountMembership.id)
                .limit(301)
            )
        ).all()
    )
    # Qualification lookups refresh membership rows in this session. Keep the
    # eagerly loaded group before those awaits can expire the relationship.
    member_groups = {member.id: member.group for member in members}
    throttle = (
        await service._ad_account_throttle_readiness(config, now) if ad_global else AdReadiness()
    )
    if throttle.reason:
        reasons[throttle.reason] += 1
    eligible_targets = set()
    target_deadlines = []
    if ad_global:
        for member in members[:300]:
            for binding, content in materials:
                campaign = binding.campaign
                reason = await service._ad_skip_reason(
                    binding,
                    campaign,
                    binding.creative,
                    member,
                    dry_run=True,
                    read_only=True,
                    now=now,
                    check_account_throttle=False,
                )
                if reason:
                    reasons[reason] += 1
                    if reason.startswith("frequency_"):
                        from app.modules.acquisition.adaptive_frequency import FrequencyService
                        context, context_reason = await service._qualified_ad_context(
                            account_id, member.telegram_group_id, now
                        )
                        if context is not None and context_reason is None:
                            frequency_ready, _ = await FrequencyService(db).readiness(
                                member.telegram_group_id, now, context=context
                            )
                            if frequency_ready.next_allowed_at:
                                target_deadlines.append(frequency_ready.next_allowed_at)
                    if reason in {"qualification_group_daily_cap", "growth_group_global_cooldown"}:
                        from app.core.group.identity import telegram_group_id_aliases

                        sent = await db.scalar(
                            select(func.max(AdDeliveryLog.sent_at)).where(
                                AdDeliveryLog.telegram_group_id.in_(
                                    telegram_group_id_aliases(member.telegram_group_id)
                                ),
                                AdDeliveryLog.status == "success",
                            )
                        )
                        if sent:
                            state = scheduled.get((campaign.id, member.group_id))
                            target_deadlines.append(
                                max(sent + timedelta(hours=24), state.next_due_at if state else now)
                            )
                    continue
                state = scheduled.get((campaign.id, member.group_id))
                readiness = schedule_readiness(state, now)
                if readiness.reason:
                    reasons[readiness.reason] += 1
                    if readiness.next_allowed_at:
                        target_deadlines.append(readiness.next_allowed_at)
                    continue
                from app.core.operating_time import operating_day_start

                reason = await service._growth_campaign_daily_quota_reason(
                    campaign=campaign,
                    account_id=account_id,
                    group=member_groups[member.id],
                    day_start=operating_day_start(now),
                )
                if reason:
                    reasons[reason] += 1
                    continue
                reason = await send_gate(db, account_id, member.telegram_group_id, content, None)
                # At this point send_gate checked all qualification, ban and replay
                # conditions. A preview has no durable send receipt yet; execution
                # still requires the dispatcher to atomically allocate that receipt.
                if reason and not (
                    phase == "pilot" and reason == "qualification_pilot_reservation_required"
                ):
                    reasons[reason] += 1
                    continue
                eligible_targets.add(member.telegram_group_id)
                break
    if not include_candidates:
        return {
            "ad_targets": len(eligible_targets) if not throttle.reason else 0,
            "material_count": len(materials), "blocker_counts": dict(reasons),
            "rollout_phase": phase,
        }
    candidate_rows = list(
        (
            await db.scalars(
                select(Group)
                .where(
                    Group.status.in_(["pending_join", "join_failed", "cooling_down"]),
                    Group.username.is_not(None),
                )
                .order_by(Group.id)
                .limit(301)
            )
        ).all()
    )
    from app.modules.acquisition import candidate_inventory as preview_inventory
    facts = await preview_inventory.load(db, account_id, [group.id for group in candidate_rows[:300]])
    # Exclude a group joined by any account, as the execution helper does, but
    # fetch the bounded candidate set once instead of one query per candidate.
    candidate_ids = [group.id for group in candidate_rows[:300]]
    joined_candidate_ids = set((await db.scalars(
        select(GroupAccountMembership.group_id).where(
            GroupAccountMembership.group_id.in_(candidate_ids),
            GroupAccountMembership.status == "joined",
        ).distinct()
    )).all()) if candidate_ids else set()
    join_ready = identity_pending = preview_pending = excluded = candidate_total = 0
    for group in candidate_rows[:300]:
        if group.id in joined_candidate_ids:
            continue
        candidate_total += 1
        fact = facts.get(group.id, {})
        if not preview_inventory.fresh(fact, group, account_id, now):
            preview_pending += 1
            reasons["join_candidate_preview_due"] += 1
            if (peer_identity(group.group_id) or (None, None))[1] is None:
                identity_pending += 1
            continue
        if fact.get("preview_exclusion"):
            excluded += 1
            reasons[fact["preview_exclusion"]] += 1
            continue
        if not preview_inventory.ready(fact):
            preview_pending += 1
            reasons["join_candidate_quality_pending"] += 1
            if fact.get("namespace") not in {"chat", "channel"}:
                identity_pending += 1
            continue
        identity = peer_identity(group.group_id, namespace=fact["namespace"])
        if identity is None or identity[1] is None:
            identity_pending += 1
            preview_pending += 1
            reasons["join_candidate_identity_pending"] += 1
            continue
        entity = PeerChannel(identity[0]) if identity[1] == "channel" else PeerChat(identity[0])
        reason = await qualification_join_gate(db, entity)
        if reason:
            excluded += 1
            reasons[reason] += 1
            continue
        join_ready += 1
    if not candidate_total:
        reasons["join_candidates_unavailable"] += 1
    return {
        "ad_targets": len(eligible_targets) if not throttle.reason else 0,
        "ad_next_allowed_at": max(
            filter(
                None,
                [
                    throttle.next_allowed_at,
                    min(target_deadlines) if target_deadlines and not eligible_targets else None,
                ],
            ),
            default=None,
        ),
        "join_candidates": join_ready,
        "join_candidates_pending_identity": identity_pending,
        "join_candidates_total": candidate_total,
        "join_candidates_preview_pending": preview_pending,
        "join_candidates_excluded": excluded,
        "material_count": len(materials),
        "rollout_phase": phase,
        "blocker_counts": dict(reasons),
        "sample_complete": len(members) <= 300 and len(candidate_rows) <= 300,
        "live_permission_recheck_required": True,
    }


async def capacity_snapshot(
    db: Any, account_id: int, now: datetime | None = None
) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.acquisition.capacity_reads import CapacityReads
    if isinstance(db, AsyncSession):
        db = CapacityReads(db)
    now = now or datetime.utcnow()
    account = await db.get(TelegramAccount, account_id)
    config = await db.scalar(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id)
    )
    if config is None or not getattr(config, "dynamic_capacity_enabled", False):
        return {
            "account_id": account_id,
            "enabled": False,
            "configured": {},
            "effective": {},
            "used_today": {},
            "used_rolling_24h": {},
            "remaining": {},
            "next_allowed_at": None,
            "blockers": ["dynamic_capacity_disabled"],
            "inventory": {},
        }
    from app.core.account.age import account_age_eligibility_reason
    from app.core.account.rpc_governor import parse_date
    from app.core.account.rpc_governor import snapshot as rpc_snapshot
    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    rpc = await rpc_snapshot(db, account_id, now)
    pause_until = max(filter(None, [parse_date(rpc.get("resume_at")), account.risk_pause_until]), default=None)
    cooling = bool(pause_until and pause_until > now)
    limits = await effective_capacity_limits(db, account, config, now)
    inventory = await inventory_snapshot(db, account_id, now)
    inventory["suspended_due"] = inventory["review_due"] if cooling else 0
    from app.modules.acquisition.ad_output_plan import ad_output_plan, output_metrics
    workload = await executable_inventory(db, account_id, config, now)
    plan = await ad_output_plan(db, account, config, now, inventory=inventory,
                               rpc=rpc, limits=limits, workload=workload)
    inventory["target"] = plan["target_groups"]
    inventory["deficit"] = plan["group_deficit"]
    outbound = await AccountOutboundBudgetService(db).snapshot(account_id, now)
    today, rolling, reservations, last_sent = await JoinRequestBudgetService(db)._request_counts(
        account_id, now
    )
    join_remaining = max(
        0,
        min(
            limits["join"] - today - reservations,
            limits["join"] - rolling - reservations,
            inventory["deficit"],
            max(0, min(300, int(config.max_groups_total or 300)) - inventory["total"]),
        ),
    )
    blockers = list(outbound["blockers"])
    if plan["join_blocker"]:
        blockers.append(plan["join_blocker"])
        join_remaining = 0
    if limits["ad"] > 0 and inventory["deficit"] <= 0 and not plan["join_blocker"]:
        blockers.append("join_inventory_target_met")
    if inventory["total"] >= min(300, int(config.max_groups_total or 300)):
        blockers.append("total_group_quota")
    eligibility = await JoinRequestBudgetService(db).eligibility_reason(
        account, config, now, require_auto_join_enabled=True
    )
    if eligibility:
        blockers.append(eligibility)
        join_remaining = 0
    age_reason = account_age_eligibility_reason(account, now)
    if age_reason:
        blockers.append(age_reason)
        join_remaining = 0
    paused = inventory["active_backlog"] >= 12 or (
        config.join_review_backlog_paused and inventory["active_backlog"] > 6
    )
    if paused:
        blockers.append("join_review_backlog")
        join_remaining = 0
    interval = max(limits["join_interval_seconds"], int(config.join_interval_min_seconds or 0))
    due = max(
        filter(
            None,
            [
                config.next_join_after,
                last_sent + timedelta(seconds=interval) if last_sent else None,
            ],
        ),
        default=None,
    )
    join_pause = limits["action_pauses"].get("join")
    if join_pause:
        due = max(filter(None, (due, datetime.fromisoformat(join_pause))))
    join_read_wait = rpc.get("lanes", {}).get("routine", {})
    ad_read_wait = rpc.get("lanes", {}).get("ad", {})
    ad_read_due = parse_date(ad_read_wait.get("resume_at"))
    if join_read_wait.get("resume_at"):
        due = max(filter(None, (due, parse_date(join_read_wait["resume_at"]))))
        blockers.append("telegram_join_read_budget")
    if due and due > now:
        blockers.append("join_interval")
    ad_remaining = min(
        outbound["remaining"], outbound["categories"]["ad"]["remaining"], inventory["qualified"]
    )
    if age_reason or not config.auto_ads_enabled:
        ad_remaining = 0
    if not config.auto_ads_enabled:
        blockers.append("account_auto_ads_disabled")
    quota_remaining = {
        "join": max(
            0, min(limits["join"] - today - reservations, limits["join"] - rolling - reservations)
        ),
        "ad": min(outbound["remaining"], outbound["categories"]["ad"]["remaining"]),
        "total": outbound["remaining"],
    }
    verification_remaining = min(
        outbound["remaining"], outbound["categories"]["verification"]["remaining"]
    )
    executable_join = min(join_remaining, workload["join_candidates"], verification_remaining)
    executable_ad = min(ad_remaining, workload["ad_targets"])
    if verification_remaining <= 0:
        blockers.append("verification_budget_unavailable")
    if due and due > now:
        executable_join = 0
    if ad_read_due and ad_read_due > now:
        executable_ad = 0
        blockers.append("telegram_ad_read_budget")
    if workload["join_candidates"] == 0:
        preview_due = parse_date(rpc.get("lanes", {}).get("background", {}).get("resume_at"))
        if preview_due and preview_due > now:
            due = max(filter(None, (due, preview_due)))
            blockers.append("telegram_preview_read_budget")
    if outbound["next_allowed_at"]:
        executable_ad = 0
        blockers.append("outbound_ad_interval")
    blockers.extend(workload["blocker_counts"])
    if cooling:
        executable_join = executable_ad = 0
        blockers = [item for item in blockers if item not in {"outbound_total_budget", "verification_budget_unavailable"}]
        blockers.append(rpc.get("reason") or "account_risk_pause")
    ad_due = max(
        filter(
            None,
            [
                pause_until if cooling else None,
                workload.pop("ad_next_allowed_at", None),
                ad_read_due if ad_read_due and ad_read_due > now else None,
                datetime.fromisoformat(outbound["next_allowed_at"])
                if outbound["next_allowed_at"]
                else None,
            ],
        ),
        default=None,
    )
    quarantined = str(getattr(account.risk_level, "value", account.risk_level)) == "quarantined"
    ads_paused = not config.enabled or not config.auto_ads_enabled or limits["ad"] <= 0
    if quarantined or ads_paused:
        executable_ad = 0
    return {
        "account_id": account_id,
        "enabled": True,
        "execution": {"state": ("quarantined" if str(getattr(account.risk_level, "value", account.risk_level)) == "quarantined"
                      else "paused" if not config.enabled or not config.auto_ads_enabled or limits["ad"] <= 0
                      else (rpc["state"] if rpc["state"] in {"budget_wait", "unavailable"} else "cooldown") if cooling else "scheduled"),
                      "resume_at": pause_until.isoformat() if cooling else None,
                      "reason": account.risk_reason if quarantined else "join_ad_account_unavailable" if ads_paused else (rpc.get("reason") or account.risk_reason) if cooling else None},
        "read_rpc": rpc,
        "configured": {
            "join": config.max_groups_per_day,
            "ad": config.max_ads_per_day,
            "total": config.max_messages_per_day or 62,
        },
        "effective": {"join": limits["join"], "ad": limits["ad"], "total": limits["total"]},
        "used_today": {
            "join": today,
            "ad": outbound["categories"]["ad"]["used_today"],
            "total": outbound["used_today"],
        },
        "used_rolling_24h": {
            "join": rolling,
            "ad": outbound["categories"]["ad"]["used_rolling_24h"],
            "total": outbound["used_rolling_24h"],
        },
        "remaining": quota_remaining,
        "quota_remaining": quota_remaining,
        "executable": {"join": executable_join, "ad": executable_ad},
        "executable_now": {"join": min(1, executable_join), "ad": min(1, executable_ad)},
        "next_allowed_at": max(filter(None, [due if due and due > now else None, pause_until if cooling else None]), default=None).isoformat() if (cooling or (due and due > now)) else None,
        "ad_next_allowed_at": ad_due.isoformat() if ad_due else None,
        "blockers": sorted(set(blockers)),
        "inventory": inventory,
        "workload": workload,
        "ad_plan": plan,
        "ad_output": await output_metrics(db, account_id, now),
        "outbound": outbound,
        "group_frequencies": await frequency_inventory(db, account_id, now) if config.adaptive_ads_enabled else [],
        "limits": limits,
    }


async def frequency_inventory(db: Any, account_id: int, now: datetime) -> list[dict]:
    from app.modules.acquisition.adaptive_frequency import FrequencyService
    from app.modules.acquisition.qualification_service import _payload, current_authorization
    members = list((await db.scalars(select(GroupAccountMembership).where(
        GroupAccountMembership.account_id == account_id,
        GroupAccountMembership.status.in_(["joined", "leave_failed"]),
    ).order_by(GroupAccountMembership.id).limit(300))).all())
    items = []
    for member in members:
        row, group, _ = await current_authorization(db, account_id, member.telegram_group_id)
        if group is None:
            continue
        item = await FrequencyService(db).summary(member.telegram_group_id, now, _payload(row) if row else None)
        items.append({**item, "group_id": group.id, "title": group.title})
    return items
