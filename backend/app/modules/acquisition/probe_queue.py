"""Read-only operational view of write verification and ad admission."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.automation_settings import get_ad_capacity_settings
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import AccountAdBinding, AdCampaign, GroupAdProfile
from app.modules.acquisition.probe_policy import in_ad_window_at, probe_day_start, write_probe_limit
from app.modules.owned_group.models import OwnedGroupAsset


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


async def build_probe_queue(db: AsyncSession, now: datetime) -> dict[str, Any]:
    capacity = await get_ad_capacity_settings(db)
    day_start = probe_day_start(now, capacity)
    success_counts = dict(
        (
            await db.execute(
                select(GroupAccountMembership.account_id, func.count(GroupAccountMembership.id))
                .where(
                    GroupAccountMembership.probe_status == "success",
                    GroupAccountMembership.last_probe_at >= day_start,
                )
                .group_by(GroupAccountMembership.account_id)
            )
        ).all()
    )
    bindings = set(
        (
            await db.execute(
                select(AccountAdBinding.account_id)
                .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
                .where(
                    AccountAdBinding.enabled.is_(True),
                    AdCampaign.enabled.is_(True),
                    or_(AdCampaign.start_at.is_(None), AdCampaign.start_at <= now),
                    or_(AdCampaign.end_at.is_(None), AdCampaign.end_at >= now),
                )
            )
        ).scalars()
    )
    owned_ids = set((await db.execute(select(OwnedGroupAsset.telegram_chat_id))).scalars())
    rows = (
        await db.execute(
            select(
                GroupAccountMembership,
                Group,
                TelegramAccount,
                GroupAdProfile,
                AccountOperationConfig,
            )
            .join(Group, Group.id == GroupAccountMembership.group_id)
            .join(TelegramAccount, TelegramAccount.id == GroupAccountMembership.account_id)
            .outerjoin(GroupAdProfile, GroupAdProfile.group_id == Group.id)
            .outerjoin(
                AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id
            )
            .where(
                GroupAccountMembership.status == "joined",
                or_(
                    AccountOperationConfig.id.is_(None),
                    AccountOperationConfig.operation_mode != "ad_only",
                ),
            )
            .order_by(
                GroupAccountMembership.joined_at.asc().nullsfirst(), GroupAccountMembership.id
            )
        )
    ).all()
    accounts: dict[int, dict[str, Any]] = {}
    cooldowns: dict[int, int | None] = {}
    risk_guard = AccountRiskGuard(db)
    positions: dict[int, int] = defaultdict(int)
    counts: Counter[str] = Counter()
    items = []
    for membership, group, account, profile, config in rows:
        account_id = int(account.id)
        if account_id not in cooldowns:
            try:
                cooldowns[account_id] = await risk_guard.peek_action_cooldown(
                    account_id, AccountRiskAction.AD_PROBE,
                )
            except Exception:
                cooldowns[account_id] = None
        mode = str(profile.ad_policy_mode if profile else "unknown")
        limit = write_probe_limit(capacity, account, now)
        completed = int(success_counts.get(account_id, 0))
        summary = accounts.setdefault(
            account_id,
            {
                "account_id": account_id,
                "daily_limit": limit,
                "completed_today": completed,
                "remaining_today": max(0, limit - completed) if limit else None,
                "pending": 0,
                "cooldown_seconds": cooldowns[account_id],
                "approval_required": 0,
                "reset_at": _iso(day_start + timedelta(days=1)),
            },
        )
        due = None
        position = None
        stage, label = "ready_for_ads", "广告准入时间已满足，等待投放复核"
        if membership.telegram_group_id in owned_ids:
            stage, label = "owned_group_excluded", "自有群由专用模块运营"
        elif (
            group.status != "active"
            or membership.ad_status == "blocked"
            or membership.probe_status == "failed"
        ):
            stage, label = "blocked", "群状态或发言能力阻断"
        elif mode == "forbidden":
            stage, label = "forbidden", "明确禁止广告"
        elif mode == "approval_required":
            stage, label = "approval_required", "等待人工取得广告许可"
            summary["approval_required"] += 1
        elif config is not None and (not config.enabled or not config.auto_ads_enabled):
            stage, label = "disabled", "账号自动投放未启用"
        elif account_id not in bindings:
            stage, label = "campaign_required", "等待有效广告计划"
        elif str(getattr(group.level, "value", group.level)) not in {"A", "B"}:
            stage, label = "level_review", "等待群等级准入"
        elif not account.is_active or str(
            getattr(account.status, "value", account.status)
        ).lower() in {"banned", "error", "restricted", "disabled", "offline"}:
            stage, label = "account_unavailable", "等待账号可用"
        elif any(
            value and value > now for value in (account.risk_pause_until, membership.ad_pause_until)
        ):
            stage, label = "risk_wait", "等待账号或关系风险恢复"
            due = max(
                value for value in (account.risk_pause_until, membership.ad_pause_until) if value
            )
        elif membership.probe_status in {"not_started", "scheduled"}:
            summary["pending"] += 1
            positions[account_id] += 1
            position = positions[account_id]
            stage, label = "write_probe_queued", "等待中性写入探测"
            due = max(now, membership.probe_due_at or now)
            if cooldowns[account_id]:
                due = max(due, now + timedelta(seconds=cooldowns[account_id] + 30))
            if limit and completed >= limit:
                stage, label = "daily_quota", "等待账号中性探测日额度"
                due = max(due, day_start + timedelta(days=1, seconds=30))
            elif due > now:
                stage, label = "probe_wait", "等待探测排期或冷却"
            if cooldowns[account_id] is None:
                stage, label, due = "probe_wait", "冷却状态暂不可用，等待风控复核", None
        elif membership.probe_status == "success":
            if mode == "unknown_probe":
                stage, label = "trial_observing", "广告权限试探已发出，观察消息存活"
            elif membership.first_ad_allowed_at is None or membership.ad_eligible_after is None:
                stage, label = "deadline_pending", "等待补齐观察期限"
            else:
                due = max(membership.first_ad_allowed_at, membership.ad_eligible_after)
                if due > now:
                    stage, label = "observing", "中性发言已成功，等待广告观察期"
                elif mode == "unknown":
                    stage, label = "policy_probe_ready", "时间门槛已满足，等待广告权限试探复核"
        if (
            profile
            and profile.ad_policy_expires_at
            and profile.ad_policy_expires_at <= now
            and mode not in {"unknown", "forbidden", "approval_required"}
        ):
            stage, label, due = "permission_expired", "广告许可已过期，等待复核", None
        if due:
            due = in_ad_window_at(max(now, due), capacity)
        counts[stage] += 1
        evidence = profile.ad_policy_source if profile else None
        items.append(
            {
                "membership_id": membership.id,
                "account_id": account_id,
                "group_id": group.id,
                "title": group.title,
                "probe_status": membership.probe_status,
                "policy_mode": mode,
                "stage": stage,
                "label": label,
                "queue_position": position,
                "earliest_action_at": _iso(due),
                "last_probe_error": membership.last_probe_error,
                "permission_evidence": "运行观察"
                if evidence in {"survival_validated", "ad_policy_probe_survived"}
                else evidence,
                "first_ad_allowed_at": _iso(membership.first_ad_allowed_at),
                "ad_eligible_after": _iso(membership.ad_eligible_after),
            }
        )
    for summary in accounts.values():
        limit = summary["daily_limit"]
        summary["minimum_capacity_cycles"] = ceil(summary["pending"] / limit) if limit else None
    return {
        "as_of": _iso(now),
        "total": len(items),
        "counts": dict(counts),
        "accounts": list(accounts.values()),
        "items": items,
        "note": "最早时间仅表示时间门槛；执行时仍须通过群规则、风控、素材和投放额度检查。",
    }
