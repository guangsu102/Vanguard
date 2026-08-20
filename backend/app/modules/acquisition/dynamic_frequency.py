"""Unified account health and dynamic frequency policy for acquisition."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountAssetTier,
    AccountBusinessStage,
    AccountOperationConfig,
    AccountRiskLevel,
    AccountStatus,
    TelegramAccount,
)
from app.core.account.warmup import account_warmup_context
from app.core.automation_constants import AD_ACCOUNT_GROUP_DAILY_CAP
from app.core.automation_settings import (
    get_account_asset_policy_settings,
    get_account_warmup_policy_settings,
    get_ad_capacity_settings,
)
from app.core.group.models import Group, GroupAccountMembership
from app.core.runtime_settings import DEFAULT_AD_CAPACITY_SETTINGS
from app.modules.acquisition.models import (
    AdCampaign,
    AdDeliveryLog,
    AutoJoinAttempt,
    DeliveryStatus,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
)

ACCOUNT_BUSINESS_NEW_DAYS = 3
ACCOUNT_STABLE_MIN_DAYS = 7
AD_DYNAMIC_PROBE_WINDOW_HOURS = 6
AD_GROUP_CONTROL_ERROR_PREFIX = "group_control:"
AD_GROUP_LEFT_ERROR_PREFIX = "group_control_left:"
AD_DELIVERY_DEFAULT_ACCOUNT_DAILY_LIMIT = int(DEFAULT_AD_CAPACITY_SETTINGS["account_ad_daily_hard_cap"])
AD_GROUP_ABSOLUTE_DAILY_CAP = int(DEFAULT_AD_CAPACITY_SETTINGS["group_global_daily_hard_cap"])


@dataclass(frozen=True)
class DailyRange:
    minimum: int
    maximum: int


@dataclass(frozen=True)
class FrequencyPolicy:
    account_id: int
    health_score: float
    business_stage: str
    lifecycle_segment: str
    join_daily_limit: int
    ad_daily_limit: int
    ad_run_limit: int
    join_interval_min_seconds: int
    join_interval_max_seconds: int
    pause_reason: str | None = None


class AccountDynamicFrequencyService:
    """Single source of truth for acquisition account health and rates."""

    MIN_JOIN_TIME_WINDOW_MULTIPLIER = 0.25

    JOIN_DAILY_RANGES: dict[str, DailyRange] = {
        "new": DailyRange(1, 3),
        "recovery": DailyRange(1, 3),
        "normal": DailyRange(5, 12),
        "stable": DailyRange(8, 20),
        "cooldown": DailyRange(0, 0),
    }
    AD_DAILY_RANGES: dict[str, DailyRange] = {
        "new": DailyRange(0, 3),
        "recovery": DailyRange(0, 2),
        "normal": DailyRange(8, 18),
        "stable": DailyRange(20, AD_DELIVERY_DEFAULT_ACCOUNT_DAILY_LIMIT),
        "cooldown": DailyRange(0, 0),
    }
    AD_RUN_RANGES: dict[str, DailyRange] = {
        "new": DailyRange(1, 1),
        "recovery": DailyRange(1, 1),
        "normal": DailyRange(1, 3),
        "stable": DailyRange(2, 5),
        "cooldown": DailyRange(0, 0),
    }
    JOIN_INTERVAL_RANGES: dict[str, tuple[int, int]] = {
        "new": (30 * 60, 120 * 60),
        "recovery": (60 * 60, 120 * 60),
        "normal": (20 * 60, 75 * 60),
        "stable": (15 * 60, 45 * 60),
        "cooldown": (8 * 3600, 24 * 3600),
    }

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_account(self, account_id: int) -> TelegramAccount | None:
        row = await self.db.execute(select(TelegramAccount).where(TelegramAccount.id == account_id))
        return row.scalar_one_or_none()

    @staticmethod
    def clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, float(value)))

    @staticmethod
    def business_stage_or_default(config: AccountOperationConfig | None) -> str:
        raw = str(getattr(config, "business_stage", "") or AccountBusinessStage.NEW.value)
        valid = {stage.value for stage in AccountBusinessStage}
        return raw if raw in valid else AccountBusinessStage.NEW.value

    @staticmethod
    def business_stage_limit_multiplier(stage: str) -> float:
        return {
            AccountBusinessStage.NEW.value: 0.55,
            AccountBusinessStage.NORMAL.value: 1.0,
            AccountBusinessStage.HOT.value: 1.25,
            AccountBusinessStage.COOLDOWN.value: 0.35,
        }.get(stage, 0.55)

    async def apply_business_stage_state(
        self,
        config: AccountOperationConfig,
        stage: str,
        now: datetime,
    ) -> None:
        changed = False
        if getattr(config, "business_stage", None) != stage:
            config.business_stage = stage
            changed = True

        if stage != AccountBusinessStage.COOLDOWN.value and config.next_join_after:
            segment = {
                AccountBusinessStage.NEW.value: "new",
                AccountBusinessStage.NORMAL.value: "normal",
                AccountBusinessStage.HOT.value: "stable",
            }.get(stage, "new")
            policy_max = self.JOIN_INTERVAL_RANGES[segment][1]
            configured_max = max(60, int(config.join_interval_max_seconds or 0))
            max_effective_seconds = int(
                max(policy_max, configured_max) / self.MIN_JOIN_TIME_WINDOW_MULTIPLIER
            )
            if config.next_join_after > now + timedelta(seconds=max_effective_seconds):
                config.next_join_after = now
                changed = True

        if changed:
            config.updated_at = now
            await self.db.commit()

    @staticmethod
    def account_asset_tier(account: TelegramAccount | None) -> str:
        raw = str(getattr(account, "asset_tier", "") or AccountAssetTier.UNKNOWN.value)
        valid = {tier.value for tier in AccountAssetTier}
        return raw if raw in valid else AccountAssetTier.UNKNOWN.value

    @staticmethod
    def account_asset_tier_policy(policy: dict[str, Any], account: TelegramAccount | None) -> dict[str, Any]:
        tiers = policy.get("tiers") if isinstance(policy.get("tiers"), dict) else {}
        return tiers.get(AccountDynamicFrequencyService.account_asset_tier(account)) or tiers.get(
            AccountAssetTier.UNKNOWN.value,
            {},
        )

    @staticmethod
    def account_asset_multiplier(
        policy: dict[str, Any],
        account: TelegramAccount | None,
        key: str,
        *,
        default: float = 1.0,
    ) -> float:
        if not policy.get("enabled", True):
            return 1.0
        tier_policy = AccountDynamicFrequencyService.account_asset_tier_policy(policy, account)
        try:
            return max(0.0, float(tier_policy.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def account_asset_age_days(policy: dict[str, Any], account: TelegramAccount | None, now: datetime) -> int:
        if account is None:
            return 0
        if getattr(account, "registered_at", None):
            return max(0, (now - account.registered_at).days)
        tier_policy = AccountDynamicFrequencyService.account_asset_tier_policy(policy, account)
        age_floor_days = int(tier_policy.get("age_floor_days", 0) or 0) if policy.get("enabled", True) else 0
        if age_floor_days > 0:
            return age_floor_days
        if account.created_at:
            return max(0, (now - account.created_at).days)
        return 0

    @staticmethod
    def account_risk_limit_multiplier(account: TelegramAccount | None, now: datetime) -> float:
        if account is None:
            return 1.0
        if account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
            return 0.0
        if account.risk_pause_until and account.risk_pause_until > now:
            return 0.0
        if account.risk_level == AccountRiskLevel.QUARANTINED.value:
            return 0.0
        if account.risk_level == AccountRiskLevel.FROZEN.value:
            return 0.0
        if account.risk_level == AccountRiskLevel.LIMITED.value:
            return 0.35
        if account.risk_level == AccountRiskLevel.WATCH.value:
            return 0.7
        return 1.0

    async def managed_warmup_context(
        self,
        account: TelegramAccount | None,
        now: datetime,
        *,
        action: str | None = None,
    ):
        policy = await get_account_warmup_policy_settings(self.db)
        context = account_warmup_context(policy, account, now, action=action)
        await self.sync_account_warmup_stage(account, context.stage, now)
        return context

    async def sync_account_warmup_stage(
        self,
        account: TelegramAccount | None,
        stage: str,
        now: datetime,
    ) -> None:
        if account is None:
            return
        changed = False
        if account.managed_started_at is None:
            account.managed_started_at = account.created_at or now
            changed = True
        if account.warmup_stage != stage:
            account.warmup_stage = stage
            account.warmup_stage_updated_at = now
            changed = True
        if changed:
            self.db.add(account)
            await self.db.commit()

    @staticmethod
    def join_time_window_multiplier(now: datetime) -> float:
        local_hour = (now + timedelta(hours=8)).hour
        if 2 <= local_hour < 9:
            return 0.25
        if 10 <= local_hour < 12:
            return 0.85
        if 14 <= local_hour < 18:
            return 1.1
        if 19 <= local_hour < 24:
            return 1.2
        if 0 <= local_hour < 2:
            return 0.45
        return 0.65

    @staticmethod
    def ad_time_window_multiplier(now: datetime) -> float:
        local_hour = (now + timedelta(hours=8)).hour
        if 2 <= local_hour < 9:
            return 0.15
        if 10 <= local_hour < 12:
            return 0.8
        if 14 <= local_hour < 18:
            return 1.1
        if 19 <= local_hour < 24:
            return 1.25
        if 0 <= local_hour < 2:
            return 0.35
        return 0.55

    @staticmethod
    def ad_health_tier(health_score: float) -> str:
        if health_score >= 90:
            return "hot"
        if health_score >= 70:
            return "normal"
        if health_score >= 50:
            return "conservative"
        if health_score >= 30:
            return "cooldown"
        return "paused"

    @staticmethod
    def group_quality_score(group: Group) -> float:
        level_value = str(getattr(group.level, "value", group.level) or "")
        dimensions = {
            "level": float(group.level_score or 0),
            "rule": float(group.rule_score or 0),
            "admin": float(group.admin_score or 0),
            "history": float(group.history_score or 0),
            "conversion": float(group.convert_score or 0),
            "activity": float(group.activity_score or 0),
        }
        if not any(dimensions.values()):
            return 85.0 if level_value == "A" else 65.0 if level_value == "B" else 35.0 if level_value == "C" else 50.0
        score = (
            dimensions["level"] * 0.10
            + dimensions["rule"] * 0.15
            + dimensions["admin"] * 0.10
            + dimensions["history"] * 0.10
            + dimensions["conversion"] * 0.30
            + dimensions["activity"] * 0.25
        )
        return AccountDynamicFrequencyService.clamp(score, 0.0, 100.0)

    @classmethod
    def initial_group_ad_tier(cls, group: Group | None) -> str:
        if group is None:
            return GroupAdTier.LOW.value
        if str(getattr(group, "status", "") or "") == "ad_blocked":
            return GroupAdTier.BLOCKED.value
        return GroupAdTier.OBSERVING.value

    @classmethod
    def group_ad_daily_capacity(
        cls,
        profile: GroupAdProfile | None,
        group: Group | None,
        capacity: dict[str, Any],
    ) -> int:
        tier = str(getattr(profile, "ad_tier", None) or cls.initial_group_ad_tier(group))
        policy_mode = str(getattr(profile, "ad_policy_mode", GroupAdPolicyMode.UNKNOWN.value) or GroupAdPolicyMode.UNKNOWN.value)
        if policy_mode not in {
            GroupAdPolicyMode.UNKNOWN_PROBE.value,
            GroupAdPolicyMode.SOFT_AD_TRIAL.value,
            GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
            GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
        }:
            return 0
        confidence_floor = (
            0
            if policy_mode == GroupAdPolicyMode.UNKNOWN_PROBE.value
            else 80
            if policy_mode == GroupAdPolicyMode.SOFT_AD_TRIAL.value
            else 90
        )
        if int(getattr(profile, "ad_policy_confidence", 0) or 0) < confidence_floor:
            return 0
        tier_caps = capacity.get("tier_daily_capacities") or {}
        tier_cap = int(tier_caps.get(tier, 0) or 0)
        if tier == GroupAdTier.BLOCKED.value:
            return 0
        if profile is not None:
            configured = int(profile.daily_capacity or tier_cap or 0)
        else:
            configured = int(tier_cap or 0)
        hard_cap = max(
            0,
            min(
                AD_GROUP_ABSOLUTE_DAILY_CAP,
                int(capacity.get("group_global_daily_hard_cap") or AD_GROUP_ABSOLUTE_DAILY_CAP),
            ),
        )
        result = max(0, min(configured, tier_cap or configured, hard_cap))
        if policy_mode in {
            GroupAdPolicyMode.UNKNOWN_PROBE.value,
            GroupAdPolicyMode.SOFT_AD_TRIAL.value,
        }:
            return min(result, 1)
        return result

    @staticmethod
    def membership_ad_ramp_multiplier(membership: GroupAccountMembership, now: datetime) -> float:
        full_capacity_at = getattr(membership, "first_ad_allowed_at", None)
        if full_capacity_at is None or now >= full_capacity_at:
            return 1.0
        started_at = (
            getattr(membership, "interaction_started_at", None)
            or getattr(membership, "last_probe_at", None)
            or getattr(membership, "joined_at", None)
        )
        if started_at is None or full_capacity_at <= started_at:
            return 1.0
        total_seconds = max(1.0, (full_capacity_at - started_at).total_seconds())
        elapsed_seconds = max(0.0, (now - started_at).total_seconds())
        progress = AccountDynamicFrequencyService.clamp(elapsed_seconds / total_seconds, 0.0, 1.0)
        return AccountDynamicFrequencyService.clamp(0.1 + progress * 0.9, 0.1, 1.0)

    @classmethod
    def ramped_daily_capacity(cls, daily_cap: int, membership: GroupAccountMembership, now: datetime) -> int:
        if daily_cap <= 0:
            return 0
        return max(1, int(math.ceil(daily_cap * cls.membership_ad_ramp_multiplier(membership, now))))

    @staticmethod
    def _membership_audit_payload(note: str | None) -> dict[str, Any]:
        if not note:
            return {}
        import json

        for line in reversed(str(note).splitlines()):
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and ("can_send_messages" in payload or "passed" in payload):
                return payload
        return {}

    @staticmethod
    def _contains_peer_flood(value: str | None) -> bool:
        text = str(value or "").lower()
        return "peer_flood" in text or "peer flood" in text or "peerflood" in text

    @staticmethod
    def _contains_account_restricted(value: str | None) -> bool:
        text = str(value or "").lower()
        return (
            "user_restricted" in text
            or "userrestricted" in text
            or "account_restricted" in text
            or "account restricted" in text
        )

    async def account_join_quality_metrics(self, account_id: int, now: datetime) -> dict[str, Any]:
        rows = await self.db.execute(
            select(GroupAccountMembership)
            .options(selectinload(GroupAccountMembership.group))
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
            )
            .order_by(desc(GroupAccountMembership.updated_at))
            .limit(120)
        )
        memberships = list(rows.scalars().all())

        writable_checked = 0
        writable_success = 0
        quality_scores: list[float] = []
        high_quality_groups = 0
        joined_groups = 0
        for membership in memberships:
            audit = self._membership_audit_payload(membership.note)
            if "can_send_messages" in audit:
                writable_checked += 1
                if audit.get("can_send_messages") is True:
                    writable_success += 1
            if membership.group is None:
                continue
            joined_groups += 1
            group = membership.group
            level_value = str(getattr(group.level, "value", group.level) or "")
            if level_value == "A":
                high_quality_groups += 1
            quality_scores.append(self.group_quality_score(group))

        writable_rate = writable_success / writable_checked if writable_checked else 1.0
        average_quality_score = sum(quality_scores) / len(quality_scores) if quality_scores else 50.0
        high_quality_rate = high_quality_groups / joined_groups if joined_groups else 0.0

        probe_rows = await self.db.execute(
            select(GroupAccountMembership.probe_status, func.count(GroupAccountMembership.id))
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
                GroupAccountMembership.last_probe_at >= now - timedelta(hours=24),
            )
            .group_by(GroupAccountMembership.probe_status)
        )
        probe_counts = {str(status or "unknown"): int(count or 0) for status, count in probe_rows.all()}
        probe_success = probe_counts.get("success", 0)
        probe_failed = probe_counts.get("failed", 0)
        probe_total = probe_success + probe_failed
        probe_success_rate = probe_success / probe_total if probe_total else 1.0

        ad_rows = await self.db.execute(
            select(AdDeliveryLog.status, func.count(AdDeliveryLog.id))
            .where(
                AdDeliveryLog.account_id == account_id,
                AdDeliveryLog.created_at >= now - timedelta(hours=24),
                AdDeliveryLog.status.in_([DeliveryStatus.SUCCESS.value, DeliveryStatus.FAILED.value]),
            )
            .group_by(AdDeliveryLog.status)
        )
        ad_counts = {str(status or "unknown"): int(count or 0) for status, count in ad_rows.all()}
        ad_success = ad_counts.get(DeliveryStatus.SUCCESS.value, 0)
        ad_failed = ad_counts.get(DeliveryStatus.FAILED.value, 0)
        ad_total = ad_success + ad_failed
        ad_success_rate = ad_success / ad_total if ad_total else 1.0

        writable_multiplier = self.clamp(0.35 + writable_rate * 0.8, minimum=0.35, maximum=1.15)
        probe_multiplier = self.clamp(0.55 + probe_success_rate * 0.55, minimum=0.55, maximum=1.1)
        ad_multiplier = self.clamp(0.55 + ad_success_rate * 0.55, minimum=0.55, maximum=1.1)
        quality_multiplier = self.clamp(0.55 + average_quality_score / 100.0 * 0.65, minimum=0.55, maximum=1.2)
        if high_quality_rate >= 0.5:
            quality_multiplier = min(1.25, quality_multiplier + 0.05)

        return {
            "writable_checked": writable_checked,
            "writable_success": writable_success,
            "writable_rate": writable_rate,
            "probe_success_24h": probe_success,
            "probe_failed_24h": probe_failed,
            "probe_success_rate_24h": probe_success_rate,
            "ad_success_24h": ad_success,
            "ad_failed_24h": ad_failed,
            "ad_success_rate_24h": ad_success_rate,
            "joined_groups": joined_groups,
            "average_group_quality_score": average_quality_score,
            "high_quality_group_rate": high_quality_rate,
            "writable_multiplier": writable_multiplier,
            "probe_multiplier": probe_multiplier,
            "ad_multiplier": ad_multiplier,
            "quality_multiplier": quality_multiplier,
        }

    async def join_attempt_metrics(self, account_id: int, now: datetime) -> dict[str, Any]:
        rows = await self.db.execute(
            select(
                AutoJoinAttempt.status,
                AutoJoinAttempt.reason,
                AutoJoinAttempt.error,
                AutoJoinAttempt.joined_at,
            ).where(
                AutoJoinAttempt.account_id == account_id,
                AutoJoinAttempt.attempted_at >= now - timedelta(hours=24),
            )
        )
        success = 0
        pending = 0
        failed = 0
        post_join_filtered = 0
        flood_wait = 0
        peer_flood = 0
        account_restricted = 0
        account_banned = 0
        for status, reason, error, joined_at in rows.all():
            joined = joined_at is not None
            if joined:
                success += 1
                if status != DeliveryStatus.SUCCESS.value:
                    post_join_filtered += 1
            elif status == DeliveryStatus.PENDING.value:
                pending += 1
            else:
                failed += 1
            text = f"{reason or ''} {error or ''}".lower()
            if not joined and self._contains_peer_flood(text):
                peer_flood += 1
            elif not joined and ("flood_wait" in text or "flood wait" in text):
                flood_wait += 1
            if not joined and self._contains_account_restricted(text):
                account_restricted += 1
            if not joined and ("account_banned" in text or "phone_number_banned" in text):
                account_banned += 1

        total = success + pending + failed
        effective_success = success + min(pending, 3)
        success_rate = effective_success / total if total else 1.0
        return {
            "success": success,
            "pending": pending,
            "failed": failed,
            "post_join_filtered": post_join_filtered,
            "total": total,
            "effective_success": effective_success,
            "success_rate": success_rate,
            "flood_wait": flood_wait,
            "peer_flood": peer_flood,
            "account_restricted": account_restricted,
            "account_banned": account_banned,
        }

    async def ad_delivery_metrics(self, account_id: int, now: datetime) -> dict[str, Any]:
        rows = await self.db.execute(
            select(AdDeliveryLog.status, AdDeliveryLog.error)
            .where(
                AdDeliveryLog.account_id == account_id,
                AdDeliveryLog.created_at >= now - timedelta(hours=24),
            )
            .order_by(AdDeliveryLog.created_at.asc())
        )
        success = 0
        failed = 0
        group_control_failed = 0
        account_failed = 0
        transient_failed = 0
        peer_flood_failed = 0
        account_restricted_failed = 0
        for status, error in rows.all():
            if status == DeliveryStatus.SUCCESS.value:
                success += 1
                continue
            if status != DeliveryStatus.FAILED.value:
                continue
            failed += 1
            text = str(error or "")
            if self._contains_peer_flood(text):
                peer_flood_failed += 1
                account_failed += 1
            elif self._contains_account_restricted(text):
                account_restricted_failed += 1
                account_failed += 1
            elif text.startswith(AD_GROUP_CONTROL_ERROR_PREFIX):
                group_control_failed += 1
            elif text.startswith("account_issue:") or "risk_guard_blocked:" in text:
                account_failed += 1
            elif text.startswith("transient:"):
                transient_failed += 1

        total = success + failed
        success_rate = success / total if total else 1.0
        return {
            "success": success,
            "failed": failed,
            "success_rate": success_rate,
            "group_control_failed": group_control_failed,
            "account_failed": account_failed,
            "transient_failed": transient_failed,
            "peer_flood_failed": peer_flood_failed,
            "account_restricted_failed": account_restricted_failed,
        }

    async def account_health(
        self,
        account_id: int,
        now: datetime,
        *,
        account: TelegramAccount | None = None,
        join_metrics: dict[str, Any] | None = None,
        join_attempts: dict[str, Any] | None = None,
        ad_metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        account = account or await self.get_account(account_id)
        join_metrics = join_metrics or await self.account_join_quality_metrics(account_id, now)
        join_attempts = join_attempts or await self.join_attempt_metrics(account_id, now)
        ad_metrics = ad_metrics or await self.ad_delivery_metrics(account_id, now)

        adjustments: list[dict[str, Any]] = []
        score = 100.0

        def adjust(reason: str, delta: float) -> None:
            nonlocal score
            if delta == 0:
                return
            score += delta
            adjustments.append({"reason": reason, "delta": round(delta, 2)})

        risk_score = float(getattr(account, "risk_score", 0.0) or 0.0) if account is not None else 50.0
        if account is None:
            adjust("account_missing", -50.0)
        else:
            if not account.is_active:
                adjust("account_inactive", -70.0)
            if account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
                adjust(f"account_status_{account.status.value}", -80.0)
            if account.risk_pause_until and account.risk_pause_until > now:
                adjust(account.risk_reason or "account_risk_paused", -100.0)
            if account.risk_recovery_until and account.risk_recovery_until > now:
                adjust("risk_recovery", -18.0)
            if account.risk_level == AccountRiskLevel.FROZEN.value:
                adjust("risk_level_frozen", -100.0)
            elif account.risk_level == AccountRiskLevel.QUARANTINED.value:
                adjust("risk_level_quarantined", -100.0)

        if int(join_attempts.get("peer_flood", 0) or 0) > 0:
            adjust("join_peer_flood", -80.0)
        if int(join_attempts.get("account_restricted", 0) or 0) > 0:
            adjust("join_account_restricted", -45.0)
        if int(join_attempts.get("account_banned", 0) or 0) > 0:
            adjust("join_account_banned", -80.0)
        if int(join_attempts.get("flood_wait", 0) or 0) > 0:
            adjust("join_flood_wait", -30.0)

        join_total = int(join_attempts.get("total", 0) or 0)
        join_success_rate = float(join_attempts.get("success_rate", 1.0) or 0.0)
        if join_total >= 10 and join_success_rate >= 0.7:
            adjust("join_success_rate_high", 6.0)
        elif join_total >= 10 and join_success_rate < 0.25:
            adjust("join_success_rate_very_low", -25.0)
        elif join_total >= 10 and join_success_rate < 0.5:
            adjust("join_success_rate_low", -12.0)

        writable_checked = int(join_metrics.get("writable_checked", 0) or 0)
        writable_rate = float(join_metrics.get("writable_rate", 1.0) or 0.0)
        if writable_checked >= 3 and writable_rate >= 0.75:
            adjust("writable_rate_high", 6.0)
        elif writable_checked >= 3 and writable_rate < 0.45:
            adjust("writable_rate_low", -25.0)

        probe_total = int(join_metrics.get("probe_success_24h", 0) or 0) + int(join_metrics.get("probe_failed_24h", 0) or 0)
        probe_rate = float(join_metrics.get("probe_success_rate_24h", 1.0) or 0.0)
        if probe_total >= 3 and probe_rate >= 0.75:
            adjust("probe_success_rate_high", 5.0)
        elif probe_total >= 3 and probe_rate < 0.45:
            adjust("probe_success_rate_low", -18.0)

        ad_total = int(join_metrics.get("ad_success_24h", 0) or 0) + int(join_metrics.get("ad_failed_24h", 0) or 0)
        ad_rate = float(join_metrics.get("ad_success_rate_24h", 1.0) or 0.0)
        if ad_total >= 5 and ad_rate >= 0.75:
            adjust("ad_success_rate_high", 5.0)
        elif ad_total >= 5 and ad_rate < 0.35:
            adjust("ad_success_rate_low", -20.0)

        delivery_total = int(ad_metrics.get("success", 0) or 0) + int(ad_metrics.get("failed", 0) or 0)
        delivery_success_rate = float(ad_metrics.get("success_rate", 1.0) or 0.0)
        adjust("group_control_failures", -min(25.0, int(ad_metrics.get("group_control_failed", 0) or 0) * 1.5))
        adjust("account_failures", -min(60.0, int(ad_metrics.get("account_failed", 0) or 0) * 30.0))
        adjust("transient_failures", -min(20.0, int(ad_metrics.get("transient_failed", 0) or 0) * 4.0))
        if int(ad_metrics.get("peer_flood_failed", 0) or 0) > 0:
            adjust("ad_peer_flood", -80.0)
        if int(ad_metrics.get("account_restricted_failed", 0) or 0) > 0:
            adjust("ad_account_restricted", -45.0)
        if delivery_total >= 10 and delivery_success_rate >= 0.7:
            adjust("ad_delivery_success_rate_high", 10.0)
        elif delivery_total >= 10 and delivery_success_rate < 0.3:
            adjust("ad_delivery_success_rate_low", -25.0)

        quality_score = float(join_metrics.get("average_group_quality_score", 50.0) or 0.0)
        if quality_score >= 70:
            adjust("group_quality_high", 5.0)
        elif quality_score < 40:
            adjust("group_quality_low", -12.0)

        health_score = self.clamp(score, 0.0, 100.0)
        return {
            "health_score": health_score,
            "success": ad_metrics["success"],
            "failed": ad_metrics["failed"],
            "success_rate": ad_metrics["success_rate"],
            "group_control_failed": ad_metrics["group_control_failed"],
            "account_failed": ad_metrics["account_failed"],
            "transient_failed": ad_metrics["transient_failed"],
            "peer_flood_failed": ad_metrics.get("peer_flood_failed", 0),
            "account_restricted_failed": ad_metrics.get("account_restricted_failed", 0),
            "risk_score": risk_score,
            "adjustments": adjustments,
        }

    def lifecycle_segment(
        self,
        account: TelegramAccount | None,
        now: datetime,
        health_score: float,
        join_metrics: dict[str, Any],
        join_attempts: dict[str, Any],
        *,
        config_enabled: bool = True,
        include_ad_health: bool = True,
        asset_policy: dict[str, Any] | None = None,
        warmup_policy: dict[str, Any] | None = None,
    ) -> str:
        if account is None or not config_enabled:
            return "cooldown"
        if not account.is_active or account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
            return "cooldown"
        if account.risk_pause_until and account.risk_pause_until > now:
            return "cooldown"
        if account.risk_level in {AccountRiskLevel.FROZEN.value, AccountRiskLevel.QUARANTINED.value}:
            return "cooldown"
        if int(join_attempts.get("peer_flood", 0) or 0) > 0 or health_score < 25:
            return "cooldown"
        if account.risk_recovery_until and account.risk_recovery_until > now:
            return "recovery"
        if warmup_policy is not None:
            warmup = account_warmup_context(warmup_policy, account, now)
            if warmup.stage == "cooldown":
                return "cooldown"
            if warmup.stage != "normal":
                return "new"
        asset_policy = asset_policy or {"enabled": False, "tiers": {}}
        account_age_days = self.account_asset_age_days(asset_policy, account, now)
        if account_age_days < ACCOUNT_BUSINESS_NEW_DAYS:
            return "new"

        writable_rate = float(join_metrics.get("writable_rate", 1.0) or 0.0)
        probe_rate = float(join_metrics.get("probe_success_rate_24h", 1.0) or 0.0)
        ad_rate = float(join_metrics.get("ad_success_rate_24h", 1.0) or 0.0)
        quality_score = float(join_metrics.get("average_group_quality_score", 50.0) or 0.0)
        recent_probe_success = int(join_metrics.get("probe_success_24h", 0) or 0)
        recent_probe_failed = int(join_metrics.get("probe_failed_24h", 0) or 0)
        recent_ad_success = int(join_metrics.get("ad_success_24h", 0) or 0)
        recent_ad_failed = int(join_metrics.get("ad_failed_24h", 0) or 0)
        probe_rate_unhealthy = recent_probe_success + recent_probe_failed >= 3 and probe_rate < 0.45
        ad_rate_unhealthy = recent_ad_success + recent_ad_failed >= 3 and ad_rate < 0.35

        if health_score < 45 or writable_rate < 0.45 or probe_rate_unhealthy:
            return "cooldown"
        if include_ad_health and ad_rate_unhealthy:
            return "cooldown"
        if (
            health_score >= 88
            and writable_rate >= 0.75
            and quality_score >= 60
            and recent_probe_success >= 3
            and account_age_days >= ACCOUNT_BUSINESS_NEW_DAYS
        ):
            return "stable"
        return "normal"

    @staticmethod
    def business_stage_for_segment(segment: str) -> str:
        return {
            "new": AccountBusinessStage.NEW.value,
            "recovery": AccountBusinessStage.COOLDOWN.value,
            "normal": AccountBusinessStage.NORMAL.value,
            "stable": AccountBusinessStage.HOT.value,
            "cooldown": AccountBusinessStage.COOLDOWN.value,
        }.get(segment, AccountBusinessStage.NEW.value)

    async def sync_account_business_stage(
        self,
        config: AccountOperationConfig | None,
        now: datetime,
        *,
        health: dict[str, Any] | None = None,
        join_metrics: dict[str, Any] | None = None,
    ) -> str:
        if config is None:
            return AccountBusinessStage.NEW.value
        account = getattr(config, "account", None) or await self.get_account(config.account_id)
        join_metrics = join_metrics or await self.account_join_quality_metrics(config.account_id, now)
        join_attempts = await self.join_attempt_metrics(config.account_id, now)
        asset_policy = await get_account_asset_policy_settings(self.db)
        warmup_policy = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(warmup_policy, account, now)
        await self.sync_account_warmup_stage(account, warmup.stage, now)
        health = health or await self.account_health(
            config.account_id,
            now,
            account=account,
            join_metrics=join_metrics,
            join_attempts=join_attempts,
        )
        segment = self.lifecycle_segment(
            account,
            now,
            float(health.get("health_score", 0.0) or 0.0),
            join_metrics,
            join_attempts,
            config_enabled=bool(config.enabled),
            asset_policy=asset_policy,
            warmup_policy=warmup_policy,
        )
        stage = self.business_stage_for_segment(segment)
        await self.apply_business_stage_state(config, stage, now)
        return stage

    def _limit_from_range(self, range_: DailyRange, health_score: float, configured_limit: int) -> int:
        if range_.maximum <= 0 or health_score < 25:
            return 0
        high = min(range_.maximum, max(1, int(configured_limit)))
        low = min(range_.minimum, high)
        health_ratio = self.clamp((health_score - 45.0) / 45.0, 0.0, 1.0)
        return int(round(low + (high - low) * health_ratio))

    async def auto_join_dynamic_daily_limit(self, config: AccountOperationConfig, now: datetime) -> int:
        account = getattr(config, "account", None) or await self.get_account(config.account_id)
        configured_limit = max(1, int(config.max_groups_per_day or 1))
        join_metrics = await self.account_join_quality_metrics(config.account_id, now)
        join_attempts = await self.join_attempt_metrics(config.account_id, now)
        asset_policy = await get_account_asset_policy_settings(self.db)
        warmup_policy = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(warmup_policy, account, now, action="join")
        await self.sync_account_warmup_stage(account, warmup.stage, now)
        health = await self.account_health(
            config.account_id,
            now,
            account=account,
            join_metrics=join_metrics,
            join_attempts=join_attempts,
        )
        segment = self.lifecycle_segment(
            account,
            now,
            float(health["health_score"]),
            join_metrics,
            join_attempts,
            config_enabled=bool(config.enabled),
            include_ad_health=False,
            asset_policy=asset_policy,
            warmup_policy=warmup_policy,
        )
        stage = self.business_stage_for_segment(segment)
        await self.apply_business_stage_state(config, stage, now)
        risk_multiplier = self.account_risk_limit_multiplier(account, now)
        if risk_multiplier <= 0 or segment == "cooldown" or warmup.action_multiplier <= 0:
            return 0

        base_limit = self._limit_from_range(self.JOIN_DAILY_RANGES[segment], float(health["health_score"]), configured_limit)
        if base_limit <= 0:
            return 0
        multiplier = (
            risk_multiplier
            * self.join_time_window_multiplier(now)
            * float(join_metrics["writable_multiplier"])
            * float(join_metrics["probe_multiplier"])
            * float(join_metrics["ad_multiplier"])
            * float(join_metrics["quality_multiplier"])
            * self.account_asset_multiplier(asset_policy, account, "join_multiplier")
            * warmup.action_multiplier
        )
        dynamic_limit = int(round(base_limit * multiplier))
        if dynamic_limit <= 0:
            return 0
        asset_cap_multiplier = max(1.0, self.account_asset_multiplier(asset_policy, account, "join_multiplier"))
        hard_cap = min(int(round(self.JOIN_DAILY_RANGES[segment].maximum * asset_cap_multiplier)), configured_limit)
        return max(1, min(dynamic_limit, hard_cap))

    async def join_candidate_decision(
        self,
        config: AccountOperationConfig,
        group: Group,
        now: datetime,
    ) -> dict[str, Any]:
        account = getattr(config, "account", None) or await self.get_account(config.account_id)
        join_metrics = await self.account_join_quality_metrics(config.account_id, now)
        join_attempts = await self.join_attempt_metrics(config.account_id, now)
        asset_policy = await get_account_asset_policy_settings(self.db)
        warmup_policy = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(warmup_policy, account, now, action="join")
        await self.sync_account_warmup_stage(account, warmup.stage, now)
        health = await self.account_health(
            config.account_id,
            now,
            account=account,
            join_metrics=join_metrics,
            join_attempts=join_attempts,
        )
        health_score = float(health["health_score"])
        segment = self.lifecycle_segment(
            account,
            now,
            health_score,
            join_metrics,
            join_attempts,
            config_enabled=bool(config.enabled),
            include_ad_health=False,
            asset_policy=asset_policy,
            warmup_policy=warmup_policy,
        )
        group_score = self.group_quality_score(group)
        success_rate = float(join_attempts.get("success_rate", 1.0) or 0.0)
        composite_score = round(health_score * (0.55 + group_score / 100.0 * 0.45) * (0.65 + success_rate * 0.35), 2)

        if segment == "cooldown":
            return {
                "allowed": False,
                "reason": "account_dynamic_health_paused",
                "health_score": health_score,
                "group_quality_score": group_score,
                "lifecycle_segment": segment,
                "composite_score": composite_score,
            }
        if segment in {"new", "recovery"} and group_score < 50:
            return {
                "allowed": False,
                "reason": "group_quality_too_low_for_account_stage",
                "health_score": health_score,
                "group_quality_score": group_score,
                "lifecycle_segment": segment,
                "composite_score": composite_score,
            }
        if health_score < 70 and group_score < 45:
            return {
                "allowed": False,
                "reason": "group_quality_too_low_for_account_health",
                "health_score": health_score,
                "group_quality_score": group_score,
                "lifecycle_segment": segment,
                "composite_score": composite_score,
            }
        if int(join_attempts.get("peer_flood", 0) or 0) > 0 or int(join_attempts.get("account_restricted", 0) or 0) > 0:
            return {
                "allowed": False,
                "reason": "recent_high_risk_join_error",
                "health_score": health_score,
                "group_quality_score": group_score,
                "lifecycle_segment": segment,
                "composite_score": composite_score,
            }
        if warmup.action_multiplier <= 0:
            return {
                "allowed": False,
                "reason": f"account_warmup_{warmup.stage}_join_blocked",
                "health_score": health_score,
                "group_quality_score": group_score,
                "lifecycle_segment": segment,
                "composite_score": composite_score,
            }

        return {
            "allowed": True,
            "reason": "allowed",
            "health_score": health_score,
            "group_quality_score": group_score,
            "lifecycle_segment": segment,
            "composite_score": composite_score,
        }

    async def ad_probe_budget_metrics(
        self,
        account_id: int,
        now: datetime,
        *,
        health_score: float | None = None,
        op_config: AccountOperationConfig | None = None,
    ) -> dict[str, Any]:
        account = getattr(op_config, "account", None) if op_config else None
        account = account or await self.get_account(account_id)
        asset_policy = await get_account_asset_policy_settings(self.db)
        capacity = await get_ad_capacity_settings(self.db)
        warmup = await self.managed_warmup_context(account, now, action="ad_probe")
        since = now - timedelta(hours=AD_DYNAMIC_PROBE_WINDOW_HOURS)
        rows = await self.db.execute(
            select(GroupAccountMembership)
            .options(selectinload(GroupAccountMembership.group))
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
                GroupAccountMembership.last_probe_at >= since,
            )
        )
        memberships = list(rows.scalars().all())
        success_memberships = [item for item in memberships if item.probe_status == "success"]
        failed_memberships = [item for item in memberships if item.probe_status == "failed"]

        eligible_rows = await self.db.execute(
            select(GroupAccountMembership)
            .options(selectinload(GroupAccountMembership.group))
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.status == "joined",
                GroupAccountMembership.probe_status == "success",
                GroupAccountMembership.ad_eligible_after.isnot(None),
                GroupAccountMembership.ad_eligible_after <= now,
            )
        )
        eligible_memberships = list(eligible_rows.scalars().all())

        eligible_group_ids = [int(membership.group_id) for membership in eligible_memberships if membership.group_id is not None]
        profile_map: dict[int, GroupAdProfile] = {}
        if eligible_group_ids:
            profile_rows = await self.db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id.in_(eligible_group_ids))
            )
            profile_map = {int(profile.group_id): profile for profile in profile_rows.scalars().all()}

        quality_scores: list[float] = []
        eligible_capacity_limit = 0
        account_group_default_cap = AD_ACCOUNT_GROUP_DAILY_CAP
        account_ad_multiplier = self.account_asset_multiplier(asset_policy, account, "ad_multiplier")
        for membership in eligible_memberships:
            group = membership.group
            if group is None or str(getattr(group, "status", "") or "") != "active":
                continue
            quality_scores.append(self.group_quality_score(group))
            profile = profile_map.get(int(membership.group_id))
            group_capacity = self.group_ad_daily_capacity(profile, group, capacity)
            account_group_base = min(
                account_group_default_cap,
                int(membership.account_group_daily_cap or account_group_default_cap),
            )
            account_group_capacity = max(0, int(round(account_group_base * account_ad_multiplier)))
            base_capacity = min(
                value
                for value in (group_capacity, account_group_capacity)
                if value > 0
            ) if group_capacity > 0 and account_group_capacity > 0 else 0
            eligible_capacity_limit += self.ramped_daily_capacity(base_capacity, membership, now)

        score = float(health_score if health_score is not None else (await self.account_health(account_id, now))["health_score"])
        if score >= 90:
            probe_factor = 0.6
        elif score >= 70:
            probe_factor = 0.5
        elif score >= 50:
            probe_factor = 0.4
        elif score >= 30:
            probe_factor = 0.3
        else:
            probe_factor = 0.0

        stage = self.business_stage_or_default(op_config)
        if stage == AccountBusinessStage.NEW.value:
            probe_factor = min(probe_factor, 0.35)
        elif stage == AccountBusinessStage.COOLDOWN.value:
            probe_factor = min(probe_factor, 0.3)
        elif stage == AccountBusinessStage.HOT.value:
            probe_factor = max(probe_factor, 0.6)
        probe_factor *= self.account_asset_multiplier(asset_policy, account, "probe_multiplier")
        probe_factor *= warmup.action_multiplier
        probe_factor = self.clamp(probe_factor, minimum=0.0, maximum=0.9)

        average_quality_score = sum(quality_scores) / len(quality_scores) if quality_scores else 50.0
        quality_multiplier = self.clamp(0.75 + average_quality_score / 100.0 * 0.4, minimum=0.75, maximum=1.15)
        recent_probe_success = len(success_memberships)
        probe_based_limit = max(0, int(eligible_capacity_limit))

        probe_total = len(success_memberships) + len(failed_memberships)
        return {
            "probe_window_hours": AD_DYNAMIC_PROBE_WINDOW_HOURS,
            "recent_probe_success": recent_probe_success,
            "recent_probe_failed": len(failed_memberships),
            "recent_probe_success_rate": recent_probe_success / probe_total if probe_total else 0.0,
            "ad_eligible_groups": len(eligible_memberships),
            "ad_eligible_capacity_limit": eligible_capacity_limit,
            "average_group_quality_score": average_quality_score,
            "quality_multiplier": quality_multiplier,
            "probe_factor": probe_factor,
            "probe_based_limit": probe_based_limit,
            "warmup_stage": warmup.stage,
            "warmup_action_multiplier": warmup.action_multiplier,
            "managed_age_days": warmup.managed_age_days,
            "warmup_remaining_days": warmup.remaining_days,
        }

    async def ad_dynamic_daily_limit(
        self,
        account_id: int,
        op_config: AccountOperationConfig | None,
        campaign: AdCampaign,
        now: datetime,
    ) -> int:
        capacity = await get_ad_capacity_settings(self.db)
        hard_cap = min(500, max(1, int(capacity.get("account_ad_daily_hard_cap") or AD_DELIVERY_DEFAULT_ACCOUNT_DAILY_LIMIT)))
        configured_values = [
            int(value)
            for value in (
                op_config.max_messages_per_day if op_config else None,
                campaign.max_sends_per_account_per_day,
            )
            if value is not None and int(value) > 0
        ]
        configured_limit = min([hard_cap, *configured_values]) if configured_values else hard_cap
        account = getattr(op_config, "account", None) if op_config else None
        account = account or await self.get_account(account_id)
        join_metrics = await self.account_join_quality_metrics(account_id, now)
        join_attempts = await self.join_attempt_metrics(account_id, now)
        asset_policy = await get_account_asset_policy_settings(self.db)
        warmup_policy = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(warmup_policy, account, now, action="ad_delivery")
        await self.sync_account_warmup_stage(account, warmup.stage, now)
        health = await self.account_health(
            account_id,
            now,
            account=account,
            join_metrics=join_metrics,
            join_attempts=join_attempts,
        )
        segment = self.lifecycle_segment(
            account,
            now,
            float(health["health_score"]),
            join_metrics,
            join_attempts,
            config_enabled=bool(getattr(op_config, "enabled", True)),
            asset_policy=asset_policy,
            warmup_policy=warmup_policy,
        )
        stage = self.business_stage_for_segment(segment)
        if op_config is not None:
            await self.apply_business_stage_state(op_config, stage, now)

        risk_multiplier = self.account_risk_limit_multiplier(account, now)
        if risk_multiplier <= 0 or segment == "cooldown" or warmup.action_multiplier <= 0:
            return 0
        health_score = float(health["health_score"])
        base_limit = self._limit_from_range(self.AD_DAILY_RANGES[segment], health_score, configured_limit)
        if base_limit <= 0:
            return 0

        probe_budget = await self.ad_probe_budget_metrics(account_id, now, health_score=health_score, op_config=op_config)
        probe_limited = int(probe_budget["probe_based_limit"])
        if probe_limited <= 0:
            return 0
        asset_multiplier = self.account_asset_multiplier(asset_policy, account, "ad_multiplier")
        return max(1, min(int(base_limit * risk_multiplier * asset_multiplier * warmup.action_multiplier), probe_limited))

    async def ad_dynamic_run_limit(self, account_id: int, configured_run_limit: int, now: datetime) -> int:
        op_config = await self._get_account_operation_config(account_id)
        account = getattr(op_config, "account", None) if op_config else None
        account = account or await self.get_account(account_id)
        join_metrics = await self.account_join_quality_metrics(account_id, now)
        join_attempts = await self.join_attempt_metrics(account_id, now)
        asset_policy = await get_account_asset_policy_settings(self.db)
        warmup_policy = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(warmup_policy, account, now, action="ad_run")
        await self.sync_account_warmup_stage(account, warmup.stage, now)
        health = await self.account_health(
            account_id,
            now,
            account=account,
            join_metrics=join_metrics,
            join_attempts=join_attempts,
        )
        segment = self.lifecycle_segment(
            account,
            now,
            float(health["health_score"]),
            join_metrics,
            join_attempts,
            config_enabled=bool(getattr(op_config, "enabled", True)),
            asset_policy=asset_policy,
            warmup_policy=warmup_policy,
        )
        if segment == "cooldown" or warmup.action_multiplier <= 0:
            return 0
        probe_budget = await self.ad_probe_budget_metrics(
            account_id,
            now,
            health_score=float(health["health_score"]),
            op_config=op_config,
        )
        probe_limit = int(probe_budget["probe_based_limit"])
        if probe_limit <= 0:
            return 0

        run_range = self.AD_RUN_RANGES[segment]
        base_limit = min(max(configured_run_limit, run_range.minimum), run_range.maximum)
        run_multiplier = self.account_asset_multiplier(asset_policy, account, "run_multiplier")
        windowed = max(1, int(base_limit * self.ad_time_window_multiplier(now) * run_multiplier * warmup.action_multiplier))
        return min(probe_limit, windowed)

    async def build_policy(
        self,
        account_id: int,
        now: datetime,
        *,
        op_config: AccountOperationConfig | None = None,
        campaign: AdCampaign | None = None,
        configured_run_limit: int = 10,
    ) -> FrequencyPolicy:
        account = getattr(op_config, "account", None) if op_config else None
        account = account or await self.get_account(account_id)
        join_metrics = await self.account_join_quality_metrics(account_id, now)
        join_attempts = await self.join_attempt_metrics(account_id, now)
        asset_policy = await get_account_asset_policy_settings(self.db)
        warmup_policy = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(warmup_policy, account, now)
        await self.sync_account_warmup_stage(account, warmup.stage, now)
        health = await self.account_health(
            account_id,
            now,
            account=account,
            join_metrics=join_metrics,
            join_attempts=join_attempts,
        )
        segment = self.lifecycle_segment(
            account,
            now,
            float(health["health_score"]),
            join_metrics,
            join_attempts,
            config_enabled=bool(getattr(op_config, "enabled", True)),
            asset_policy=asset_policy,
            warmup_policy=warmup_policy,
        )
        stage = self.business_stage_for_segment(segment)
        join_limit = await self.auto_join_dynamic_daily_limit(op_config, now) if op_config else 0
        ad_limit = await self.ad_dynamic_daily_limit(account_id, op_config, campaign, now) if campaign else 0
        run_limit = await self.ad_dynamic_run_limit(account_id, configured_run_limit, now)
        interval_min, interval_max = self.JOIN_INTERVAL_RANGES[segment]
        return FrequencyPolicy(
            account_id=account_id,
            health_score=float(health["health_score"]),
            business_stage=stage,
            lifecycle_segment=segment,
            join_daily_limit=join_limit,
            ad_daily_limit=ad_limit,
            ad_run_limit=run_limit,
            join_interval_min_seconds=interval_min,
            join_interval_max_seconds=interval_max,
            pause_reason=getattr(account, "risk_reason", None) if segment == "cooldown" else None,
        )

    async def _get_account_operation_config(self, account_id: int) -> AccountOperationConfig | None:
        row = await self.db.execute(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
        return row.scalar_one_or_none()
