"""
Unified account risk guard for Telegram actions.

The guard centralizes account-level budgets, pause checks, and audit records so
Telegram actions do not rely only on scattered workflow-specific limits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Optional

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountRiskDailyStat,
    AccountRiskEvent,
    AccountRiskLevel,
    AccountStatus,
    TelegramAccount,
)
from app.core.account.warmup import account_warmup_block_reason, account_warmup_context
from app.core.automation_settings import (
    get_account_risk_guard_settings,
    get_account_warmup_policy_settings,
)
from app.core.config import get_settings
from app.core.redis import RedisCache

logger = structlog.get_logger()


class AccountRiskAction(str, Enum):
    SEARCH = "search"
    JOIN = "join"
    PRIVATE_MESSAGE = "private_message"
    GROUP_MESSAGE = "group_message"
    AD_PROBE = "ad_probe"
    AI_WARMUP = "ai_warmup"
    MODERATION = "moderation"
    AD_DELIVERY = "ad_delivery"
    PROFILE_UPDATE = "profile_update"
    REACTION = "reaction"
    FORWARD = "forward"
    PIN = "pin"
    BOT_MESSAGE = "bot_message"
    BOT_PIN = "bot_pin"
    CHANNEL_CREATE = "channel_create"


@dataclass(frozen=True)
class RiskBudget:
    daily_limit: int
    cooldown_seconds: int = 0


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = "allowed"
    retry_after_seconds: Optional[int] = None


DEFAULT_ACTION_BUDGETS: dict[AccountRiskAction, RiskBudget] = {
    AccountRiskAction.SEARCH: RiskBudget(daily_limit=100, cooldown_seconds=30),
    AccountRiskAction.JOIN: RiskBudget(daily_limit=15, cooldown_seconds=1200),
    AccountRiskAction.PRIVATE_MESSAGE: RiskBudget(daily_limit=40, cooldown_seconds=45),
    AccountRiskAction.GROUP_MESSAGE: RiskBudget(daily_limit=20, cooldown_seconds=300),
    AccountRiskAction.AD_PROBE: RiskBudget(daily_limit=10, cooldown_seconds=1800),
    AccountRiskAction.AI_WARMUP: RiskBudget(daily_limit=10, cooldown_seconds=1800),
    AccountRiskAction.MODERATION: RiskBudget(daily_limit=60, cooldown_seconds=15),
    AccountRiskAction.AD_DELIVERY: RiskBudget(daily_limit=50, cooldown_seconds=300),
    AccountRiskAction.PROFILE_UPDATE: RiskBudget(daily_limit=5, cooldown_seconds=3600),
    AccountRiskAction.REACTION: RiskBudget(daily_limit=120, cooldown_seconds=10),
    AccountRiskAction.FORWARD: RiskBudget(daily_limit=25, cooldown_seconds=120),
    AccountRiskAction.PIN: RiskBudget(daily_limit=20, cooldown_seconds=120),
    AccountRiskAction.BOT_MESSAGE: RiskBudget(daily_limit=500, cooldown_seconds=1),
    AccountRiskAction.BOT_PIN: RiskBudget(daily_limit=100, cooldown_seconds=5),
    AccountRiskAction.CHANNEL_CREATE: RiskBudget(daily_limit=1, cooldown_seconds=86400),
}

GLOBAL_DAILY_LIMIT = 200
DEFAULT_FREEZE_SECONDS = 3600
FLOOD_WAIT_BUFFER_SECONDS = 60
PEER_FLOOD_FREEZE_SECONDS = 24 * 3600
ACCOUNT_RESTRICTED_FREEZE_SECONDS = 24 * 3600
GROUP_WRITE_FORBIDDEN_FREEZE_SECONDS = 12 * 3600
RECOVERY_SECONDS = 24 * 3600
DECAY_INTERVAL_HOURS = 24
DECAY_POINTS_PER_INTERVAL = 8.0
NEW_ACCOUNT_DAYS = 3
HEALTHY_ACCOUNT_DAYS = 14
LOW_VALUE_EVENT_STATUSES = {"allow", "success"}
LOW_VALUE_DETAIL_RETENTION_DAYS = 14
HIGH_VALUE_DETAIL_RETENTION_DAYS = 90
GROUP_WRITE_FORBIDDEN_BASE_SCORE = 4.0
GROUP_WRITE_FORBIDDEN_PLATFORM_SCORE = 12.0
GROUP_WRITE_FORBIDDEN_FREEZE_WINDOW = timedelta(hours=2)
GROUP_WRITE_FORBIDDEN_FREEZE_DISTINCT_GROUPS = 5
GROUP_WRITE_FORBIDDEN_QUARANTINE_WINDOW = timedelta(hours=24)
GROUP_WRITE_FORBIDDEN_QUARANTINE_DISTINCT_GROUPS = 10
GROUP_WRITE_CAPABILITY_RECOVERY_REASON = "group_write_capability_confirmed"
PLATFORM_GROUP_WRITE_BAN_MARKERS = (
    "banned from sending messages in supergroups",
    "banned from sending messages in channels",
    "you're banned from sending messages",
    "userbannedinchannel",
    "user banned in channel",
)

RISK_LEVEL_THRESHOLDS: tuple[tuple[float, AccountRiskLevel], ...] = (
    (90.0, AccountRiskLevel.QUARANTINED),
    (70.0, AccountRiskLevel.FROZEN),
    (45.0, AccountRiskLevel.LIMITED),
    (20.0, AccountRiskLevel.WATCH),
    (0.0, AccountRiskLevel.NORMAL),
)
RISK_LEVEL_BUDGET_MULTIPLIER: dict[str, float] = {
    AccountRiskLevel.NORMAL.value: 1.0,
    AccountRiskLevel.WATCH.value: 0.7,
    AccountRiskLevel.LIMITED.value: 0.45,
    AccountRiskLevel.FROZEN.value: 0.0,
    AccountRiskLevel.QUARANTINED.value: 0.0,
}
MESSAGE_ACTIONS = {
    AccountRiskAction.PRIVATE_MESSAGE,
    AccountRiskAction.GROUP_MESSAGE,
    AccountRiskAction.AD_PROBE,
    AccountRiskAction.AI_WARMUP,
    AccountRiskAction.AD_DELIVERY,
    AccountRiskAction.BOT_MESSAGE,
}
CONTENT_DEDUP_EXEMPT_SOURCES = {
    "managed_group_channel_announcement",
}
AI_ERROR_CLASSIFIER_ALLOWED_REASONS = {
    "group_write_forbidden",
    "flood_wait",
    "peer_flood",
    "account_banned",
    "account_restricted",
    "telegram_error",
}


class AccountRiskGuard:
    """Central risk gate for account-level Telegram operations."""

    def __init__(self, db: AsyncSession, cache: Optional[RedisCache] = None):
        self.db = db
        self.cache = cache or RedisCache()
        self.logger = logger.bind(module="account_risk_guard")
        self.settings = get_settings()

    async def check_and_reserve(
        self,
        account: Any,
        action: AccountRiskAction | str,
        *,
        target_type: Optional[str] = None,
        target_id: Optional[Any] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> RiskDecision:
        action = AccountRiskAction(action)
        account_id = self._account_id(account)
        db_account = await self._get_db_account(account_id)
        now = datetime.utcnow()
        risk_settings = await get_account_risk_guard_settings(self.db)

        if db_account is not None:
            await self._apply_risk_lifecycle(db_account, now, risk_settings=risk_settings)
            if not db_account.is_active:
                return await self._block(
                    account, action, "account_inactive", target_type, target_id, details
                )
            if db_account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
                return await self._block(
                    account,
                    action,
                    f"account_status_{db_account.status.value}",
                    target_type,
                    target_id,
                    details,
                )
            if db_account.risk_level == AccountRiskLevel.QUARANTINED.value:
                return await self._block(
                    account, action, "account_risk_quarantined", target_type, target_id, details
                )
            if db_account.risk_pause_until and db_account.risk_pause_until > now:
                retry_after = max(1, int((db_account.risk_pause_until - now).total_seconds()))
                return await self._block(
                    account,
                    action,
                    db_account.risk_reason or "account_risk_paused",
                    target_type,
                    target_id,
                    details,
                    retry_after_seconds=retry_after,
                )

        if account_id is None:
            return await self._block(
                account, action, "account_missing", target_type, target_id, details
            )

        if not risk_settings["enabled"]:
            await self.record_event(
                account,
                action,
                "allow",
                reason="risk_guard_disabled",
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
            return RiskDecision(True)
        warmup_settings = await get_account_warmup_policy_settings(self.db)
        warmup = account_warmup_context(
            warmup_settings, db_account, now, action=action, details=details
        )
        if db_account is not None:
            self._sync_warmup_stage(db_account, warmup.stage, now)
        warmup_reason = account_warmup_block_reason(warmup, action, details)
        if warmup_reason:
            return await self._block(
                account, action, warmup_reason, target_type, target_id, details
            )
        budget = self._budget_for_action(action, risk_settings)
        budget = self._apply_budget_policy(
            budget,
            db_account,
            now,
            risk_settings=risk_settings,
            warmup_multiplier=warmup.action_multiplier,
        )
        content_decision = await self._check_content_policy(
            account_id, action, target_type, target_id, details
        )
        if not content_decision.allowed:
            return await self._block(
                account, action, content_decision.reason, target_type, target_id, details
            )
        allowed, reason, retry_after = await self._reserve_budget(
            account_id, action, budget, risk_settings
        )
        if not allowed:
            return await self._block(
                account,
                action,
                reason,
                target_type,
                target_id,
                details,
                retry_after_seconds=retry_after,
            )

        await self.record_event(
            account,
            action,
            "allow",
            reason="allowed",
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        return RiskDecision(True)

    async def record_success(
        self,
        account: Any,
        action: AccountRiskAction | str,
        *,
        target_type: Optional[str] = None,
        target_id: Optional[Any] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        action = AccountRiskAction(action)
        await self.record_event(
            account,
            action,
            "success",
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        if (
            action
            in {
                AccountRiskAction.AD_DELIVERY,
                AccountRiskAction.GROUP_MESSAGE,
                AccountRiskAction.AD_PROBE,
                AccountRiskAction.AI_WARMUP,
            }
            and target_type == "group"
        ):
            await self._reconcile_group_write_success(account)

    async def record_failure(
        self,
        account: Any,
        action: AccountRiskAction | str,
        exc: Exception | str,
        *,
        target_type: Optional[str] = None,
        target_id: Optional[Any] = None,
        details: Optional[dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> None:
        action = AccountRiskAction(action)
        reason = reason or await self._classify_error(
            exc,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        risk_settings = await get_account_risk_guard_settings(self.db)
        lifecycle = risk_settings.get("lifecycle", {})
        merged_details = dict(details or {})
        merged_details.setdefault("error", str(exc))
        await self.record_event(
            account,
            action,
            "failure",
            reason=reason,
            target_type=target_type,
            target_id=target_id,
            details=merged_details,
        )
        if reason == "peer_flood":
            await self.freeze_account(
                account,
                reason="peer_flood",
                seconds=int(lifecycle.get("peer_flood_freeze_seconds", PEER_FLOOD_FREEZE_SECONDS)),
                action=action,
                details=merged_details,
            )
        elif reason == "flood_wait":
            seconds = self.extract_wait_seconds(exc) or int(
                lifecycle.get("default_freeze_seconds", DEFAULT_FREEZE_SECONDS)
            )
            await self.freeze_account(
                account,
                reason="flood_wait",
                seconds=seconds
                + int(lifecycle.get("flood_wait_buffer_seconds", FLOOD_WAIT_BUFFER_SECONDS)),
                action=action,
                details=merged_details,
            )
        elif reason == "account_banned":
            await self.quarantine_account(
                account, reason=reason, action=action, details=merged_details
            )
        elif reason == "account_restricted":
            await self.freeze_account(
                account,
                reason=reason,
                seconds=int(
                    lifecycle.get(
                        "account_restricted_freeze_seconds", ACCOUNT_RESTRICTED_FREEZE_SECONDS
                    )
                ),
                action=action,
                details=merged_details,
            )
        elif reason == "group_write_forbidden" and self._confirmed_account_wide_group_write_ban(
            merged_details
        ):
            await self._escalate_repeated_group_write_forbidden(
                account,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=merged_details,
                risk_settings=risk_settings,
            )

    async def _classify_error(
        self,
        exc: Exception | str,
        *,
        action: AccountRiskAction,
        target_type: Optional[str] = None,
        target_id: Optional[Any] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        reason = self.classify_error(exc, action=action, target_type=target_type)
        if reason != "telegram_error" or not self._ai_error_classifier_enabled(action):
            return reason
        return await self._classify_error_with_ai(
            exc,
            fallback=reason,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )

    @staticmethod
    def _ai_error_classifier_enabled(action: AccountRiskAction) -> bool:
        if action not in {
            AccountRiskAction.AD_DELIVERY,
            AccountRiskAction.GROUP_MESSAGE,
            AccountRiskAction.AD_PROBE,
            AccountRiskAction.AI_WARMUP,
        }:
            return False
        return str(os.getenv("AD_ERROR_AI_CLASSIFIER_ENABLED", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    async def _classify_error_with_ai(
        self,
        exc: Exception | str,
        *,
        fallback: str,
        action: AccountRiskAction,
        target_type: Optional[str],
        target_id: Optional[Any],
        details: Optional[dict[str, Any]],
    ) -> str:
        try:
            from app.core.ai.llm_client import LLMClient

            prompt = (
                "Classify this Telegram action error into exactly one label.\n"
                "Allowed labels: group_write_forbidden, flood_wait, peer_flood, account_banned, "
                "account_restricted, telegram_error.\n"
                "Rules:\n"
                "- Group/channel permission, cannot write, topic closed, private channel, "
                "not participant, or banned from sending in a supergroup/channel => group_write_forbidden.\n"
                "- Flood wait/retry-after/too many requests => flood_wait.\n"
                "- PEER_FLOOD or peer flood protection => peer_flood.\n"
                "- Only explicit account/session/phone deactivation, revoked auth, or phone banned "
                "=> account_banned.\n"
                "- Privacy restriction or account-wide restricted behavior => account_restricted.\n"
                "- If unsure => telegram_error.\n\n"
                f"action={action.value}\n"
                f"target_type={target_type}\n"
                f"target_id={target_id}\n"
                f"details={json.dumps(details or {}, ensure_ascii=False)[:800]}\n"
                f"error={exc.__class__.__name__ if isinstance(exc, Exception) else 'str'}: {str(exc)[:1000]}\n\n"
                "Return only the label."
            )
            response = (
                (await LLMClient().generate(prompt, temperature=0, max_tokens=20)).strip().lower()
            )
            reason = re.sub(r"[^a-z_]+", "", response.splitlines()[0] if response else "")
            if reason in AI_ERROR_CLASSIFIER_ALLOWED_REASONS:
                self.logger.info(
                    "ad_error_ai_classified", reason=reason, fallback=fallback, action=action.value
                )
                return reason
        except Exception as exc_ai:
            self.logger.warning(
                "ad_error_ai_classifier_failed", error=str(exc_ai), fallback=fallback
            )
        return fallback

    async def freeze_account(
        self,
        account: Any,
        *,
        reason: str,
        seconds: Optional[int] = None,
        action: AccountRiskAction | str = AccountRiskAction.JOIN,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        account_id = self._account_id(account)
        db_account = await self._get_db_account(account_id)
        if seconds is None:
            risk_settings = await get_account_risk_guard_settings(self.db)
            seconds = int(
                risk_settings.get("lifecycle", {}).get(
                    "default_freeze_seconds", DEFAULT_FREEZE_SECONDS
                )
            )
        pause_until = datetime.utcnow() + timedelta(seconds=max(60, int(seconds)))
        if db_account is not None:
            if db_account.risk_pause_until is None or db_account.risk_pause_until < pause_until:
                db_account.risk_pause_until = pause_until
            db_account.risk_reason = reason
            db_account.last_risk_event_at = datetime.utcnow()
            db_account.risk_level = AccountRiskLevel.FROZEN.value
            self.db.add(db_account)
            await self.db.commit()
        await self.record_event(
            account,
            AccountRiskAction(action),
            "freeze",
            reason=reason,
            details={**(details or {}), "risk_pause_until": pause_until.isoformat()},
        )

    async def quarantine_account(
        self,
        account: Any,
        *,
        reason: str,
        action: AccountRiskAction | str = AccountRiskAction.JOIN,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        account_id = self._account_id(account)
        db_account = await self._get_db_account(account_id)
        if db_account is not None:
            db_account.risk_score = 100.0
            db_account.risk_level = AccountRiskLevel.QUARANTINED.value
            db_account.risk_reason = reason
            db_account.last_risk_event_at = datetime.utcnow()
            self.db.add(db_account)
            await self.db.commit()
        await self.record_event(
            account,
            AccountRiskAction(action),
            "quarantine",
            reason=reason,
            details=details,
        )

    async def record_event(
        self,
        account: Any,
        action: AccountRiskAction,
        status: str,
        *,
        reason: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[Any] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        account_id = self._account_id(account)
        event_account_id = account_id if account_id and account_id > 0 else None
        event_details = dict(details or {})
        risk_settings = await get_account_risk_guard_settings(self.db)
        if account_id is not None and account_id <= 0:
            event_details.setdefault("system_account_id", account_id)
            event_details.setdefault("system_identity", getattr(account, "session_name", None))

        await self._increment_daily_stat(event_account_id, action, status, reason, target_type)
        if self._should_keep_detail_event(status, reason):
            event = AccountRiskEvent(
                account_id=event_account_id,
                action=action.value,
                status=status,
                reason=reason,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                fingerprint_id=getattr(account, "fingerprint_id", None),
                proxy_mode=self._enum_value(getattr(account, "proxy_mode", None)),
                proxy_id=getattr(account, "static_proxy_id", None),
                proxy_country=self._country_code(
                    getattr(account, "current_proxy_country", None)
                    or getattr(account, "country_code", None)
                ),
                details=json.dumps(event_details, ensure_ascii=False, default=str)
                if event_details
                else None,
            )
            self.db.add(event)
        db_account = await self._get_db_account(account_id)
        if db_account is not None:
            await self._apply_risk_lifecycle(
                db_account, datetime.utcnow(), risk_settings=risk_settings, commit=False
            )
        group_scoped_write_failure = (
            status == "failure"
            and reason == "group_write_forbidden"
            and target_type == "group"
            and not self._confirmed_account_wide_group_write_ban(event_details)
        )
        if db_account is not None and status in {"failure", "freeze"} and not group_scoped_write_failure:
            now = datetime.utcnow()
            db_account.last_risk_event_at = now
            preserve_group_freeze_reason = (
                status == "failure"
                and reason == "group_write_forbidden"
                and db_account.risk_reason == "platform_group_write_repeated"
                and db_account.risk_pause_until is not None
                and db_account.risk_pause_until > now
            )
            if not preserve_group_freeze_reason:
                db_account.risk_reason = reason or status
            if status == "failure":
                db_account.risk_score = min(
                    100.0,
                    float(db_account.risk_score or 0.0)
                    + self._risk_score_delta(reason, event_details, risk_settings),
                )
            self._sync_risk_level(db_account, risk_settings=risk_settings)
            self.db.add(db_account)
        await self.db.commit()

    async def should_leave_group_after_write_forbidden(
        self, account: Any, target_id: Any
    ) -> bool:
        """Return whether repeated write failures require leaving this group."""
        account_id = self._account_id(account)
        canonical_target_id = self._canonical_group_target_id(target_id)
        if account_id is None or canonical_target_id is None:
            return False

        risk_settings = await get_account_risk_guard_settings(self.db)
        policy = risk_settings.get("group_write_forbidden", {})
        threshold = int(policy.get("leave_after_failures", 2))
        window_hours = int(policy.get("leave_window_hours", 24))
        since = datetime.utcnow() - timedelta(hours=window_hours)
        result = await self.db.execute(
            select(AccountRiskEvent.target_id).where(
                AccountRiskEvent.account_id == account_id,
                AccountRiskEvent.status == "failure",
                AccountRiskEvent.reason == "group_write_forbidden",
                AccountRiskEvent.target_type == "group",
                AccountRiskEvent.created_at >= since,
            )
        )
        failures = sum(
            self._canonical_group_target_id(event_target_id) == canonical_target_id
            for event_target_id in result.scalars().all()
        )
        return failures >= threshold

    async def mark_group_write_forbidden_group_left(self, account: Any, target_id: Any) -> bool:
        """Mark only this account's membership as left after a confirmed group leave."""
        account_id = self._account_id(account)
        target_ids = self._group_membership_target_ids(target_id)
        if account_id is None or not target_ids:
            return False

        from app.core.group.models import GroupAccountMembership

        membership = None
        for telegram_group_id in target_ids:
            result = await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.account_id == account_id,
                    GroupAccountMembership.telegram_group_id == telegram_group_id,
                )
            )
            membership = result.scalar_one_or_none()
            if membership is not None:
                break
        if membership is None:
            return False

        now = datetime.utcnow()
        membership.status = "left"
        membership.left_at = now
        membership.last_checked_at = now
        membership.warmup_status = "blocked"
        membership.probe_status = "failed"
        membership.ad_status = "blocked"
        membership.last_probe_error = "group_write_forbidden"
        self.db.add(membership)
        await self.db.commit()
        return True

    @staticmethod
    def _canonical_group_target_id(target_id: Any) -> Optional[str]:
        text = str(target_id or "").strip()
        if not text:
            return None
        if text.startswith("-100") and text[4:].isdigit():
            return text[4:].lstrip("0") or "0"
        try:
            return str(abs(int(text)))
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _group_membership_target_ids(target_id: Any) -> list[int]:
        text = str(target_id or "").strip()
        try:
            exact = int(text)
        except (TypeError, ValueError):
            return []

        candidates = [exact]
        if text.startswith("-100") and text[4:].isdigit():
            candidates.append(int(text[4:]))
        elif exact < 0:
            candidates.append(abs(exact))
        elif exact > 0:
            candidates.append(int(f"-100{exact}"))
        return list(dict.fromkeys(candidates))

    async def _escalate_repeated_group_write_forbidden(
        self,
        account: Any,
        *,
        action: AccountRiskAction,
        target_type: Optional[str],
        target_id: Optional[Any],
        details: Optional[dict[str, Any]],
        risk_settings: dict[str, Any],
    ) -> None:
        account_id = self._account_id(account)
        if account_id is None:
            return
        if not self._confirmed_account_wide_group_write_ban(details):
            return

        db_account = await self._get_db_account(account_id)
        if db_account is None:
            return
        if (
            db_account.risk_level == AccountRiskLevel.QUARANTINED.value
            and db_account.risk_reason == "platform_group_write_banned"
        ):
            return

        now = datetime.utcnow()
        group_policy = risk_settings.get("group_write_forbidden", {})
        thresholds = risk_settings.get("level_thresholds", {})
        lifecycle = risk_settings.get("lifecycle", {})
        freeze_groups = await self._recent_distinct_group_write_forbidden_targets(
            account_id,
            since=now - timedelta(hours=float(group_policy.get("freeze_window_hours", 2))),
        )
        quarantine_groups = await self._recent_distinct_group_write_forbidden_targets(
            account_id,
            since=now - timedelta(hours=float(group_policy.get("quarantine_window_hours", 24))),
        )
        escalation_details = {
            **(details or {}),
            "source": "repeated_group_write_forbidden",
            "freeze_window_distinct_groups": len(freeze_groups),
            "quarantine_window_distinct_groups": len(quarantine_groups),
            "current_target_id": str(target_id) if target_id is not None else None,
        }

        if self._confirmed_account_wide_group_write_ban(details):
            await self.quarantine_account(
                account,
                reason="platform_group_write_banned",
                action=action,
                details=escalation_details,
            )
            return

        if len(freeze_groups) >= int(
            group_policy.get("freeze_distinct_groups", GROUP_WRITE_FORBIDDEN_FREEZE_DISTINCT_GROUPS)
        ) or float(db_account.risk_score or 0.0) >= float(thresholds.get("frozen", 70.0)):
            if db_account.risk_pause_until and db_account.risk_pause_until > now:
                return
            await self.freeze_account(
                account,
                reason="platform_group_write_repeated",
                seconds=int(
                    lifecycle.get(
                        "group_write_forbidden_freeze_seconds", GROUP_WRITE_FORBIDDEN_FREEZE_SECONDS
                    )
                ),
                action=action,
                details=escalation_details,
            )

    async def _reconcile_group_write_success(self, account: Any) -> None:
        """Clear only risk state that a successful group send directly disproves."""
        account_id = self._account_id(account)
        db_account = await self._get_db_account(account_id)
        if db_account is None or db_account.risk_reason not in {
            "group_write_forbidden",
            "platform_group_write_repeated",
            "platform_group_write_banned",
        }:
            return

        risk_settings = await get_account_risk_guard_settings(self.db)
        thresholds = risk_settings.get("level_thresholds", {})
        lifecycle = risk_settings.get("lifecycle", {})
        now = datetime.utcnow()
        db_account.risk_pause_until = None
        db_account.risk_recovery_until = now + timedelta(
            seconds=int(lifecycle.get("recovery_seconds", RECOVERY_SECONDS))
        )
        db_account.risk_score = min(
            float(db_account.risk_score or 0.0),
            max(0.0, float(thresholds.get("limited", 45.0)) - 1.0),
        )
        db_account.risk_reason = GROUP_WRITE_CAPABILITY_RECOVERY_REASON
        db_account.last_risk_event_at = now
        self._sync_risk_level(db_account, risk_settings=risk_settings)
        if db_account.risk_level == AccountRiskLevel.NORMAL.value:
            db_account.risk_level = AccountRiskLevel.WATCH.value
        self.db.add(db_account)
        await self.db.commit()

    async def _recent_distinct_group_write_forbidden_targets(
        self, account_id: int, *, since: datetime
    ) -> set[str]:
        result = await self.db.execute(
            select(AccountRiskEvent.target_id).where(
                AccountRiskEvent.account_id == account_id,
                AccountRiskEvent.status == "failure",
                AccountRiskEvent.reason == "group_write_forbidden",
                AccountRiskEvent.target_type == "group",
                AccountRiskEvent.created_at >= since,
            )
        )
        return {str(target_id) for target_id in result.scalars().all() if target_id is not None}

    async def _reserve_budget(
        self,
        account_id: int,
        action: AccountRiskAction,
        budget: RiskBudget,
        risk_settings: dict[str, Any],
    ) -> tuple[bool, str, Optional[int]]:
        if self.cache.client is None:
            fail_closed = risk_settings.get("redis_fail_closed")
            if fail_closed is None:
                fail_closed = bool(getattr(self.settings, "RISK_GUARD_FAIL_CLOSED", False))
            if fail_closed:
                return False, "risk_budget_unavailable", None
            return True, "redis_unavailable_budget_not_enforced", None

        day_key = datetime.utcnow().strftime("%Y%m%d")
        total_key = f"risk:account:{account_id}:daily:total:{day_key}"
        action_key = f"risk:account:{account_id}:daily:{action.value}:{day_key}"
        cooldown_key = f"risk:account:{account_id}:cooldown:{action.value}"

        cooldown = await self.cache.get(cooldown_key)
        if cooldown:
            try:
                retry_after = max(1, int(float(cooldown) - datetime.utcnow().timestamp()))
                if retry_after > 0:
                    return False, f"{action.value}_cooldown", retry_after
            except ValueError:
                pass

        total_count = await self.cache.incr(total_key)
        await self.cache.expire(total_key, 48 * 3600)
        global_daily_limit = int(risk_settings.get("global_daily_limit", GLOBAL_DAILY_LIMIT))
        if total_count > global_daily_limit:
            return False, "account_global_daily_budget", None

        action_count = await self.cache.incr(action_key)
        await self.cache.expire(action_key, 48 * 3600)
        if action_count > budget.daily_limit:
            return False, f"{action.value}_daily_budget", None

        if budget.cooldown_seconds > 0:
            until = datetime.utcnow().timestamp() + budget.cooldown_seconds
            await self.cache.set(cooldown_key, str(until), ttl=budget.cooldown_seconds)
        return True, "reserved", None

    async def decay_risk_scores(self, *, now: Optional[datetime] = None) -> dict[str, int]:
        now = now or datetime.utcnow()
        risk_settings = await get_account_risk_guard_settings(self.db)
        rows = await self.db.execute(select(TelegramAccount))
        accounts = list(rows.scalars().all())
        checked = 0
        decayed = 0
        recovered = 0
        for account in accounts:
            checked += 1
            before_score = float(account.risk_score or 0.0)
            before_level = account.risk_level
            changed = await self._apply_risk_lifecycle(
                account, now, risk_settings=risk_settings, commit=False
            )
            if float(account.risk_score or 0.0) < before_score:
                decayed += 1
            if before_level != account.risk_level:
                recovered += 1
            if changed:
                self.db.add(account)
        await self.db.commit()
        return {"checked": checked, "decayed": decayed, "level_changed": recovered}

    async def cleanup_risk_events(
        self,
        *,
        low_value_retention_days: Optional[int] = None,
        high_value_retention_days: Optional[int] = None,
        stat_retention_days: Optional[int] = None,
    ) -> dict[str, int]:
        now = datetime.utcnow()
        risk_settings = await get_account_risk_guard_settings(self.db)
        retention = risk_settings.get("retention", {})
        low_value_retention_days = (
            int(retention.get("low_value_detail_retention_days", LOW_VALUE_DETAIL_RETENTION_DAYS))
            if low_value_retention_days is None
            else low_value_retention_days
        )
        high_value_retention_days = (
            int(retention.get("high_value_detail_retention_days", HIGH_VALUE_DETAIL_RETENTION_DAYS))
            if high_value_retention_days is None
            else high_value_retention_days
        )
        stat_retention_days = (
            int(retention.get("daily_stat_retention_days", 370))
            if stat_retention_days is None
            else stat_retention_days
        )
        low_cutoff = now - timedelta(days=low_value_retention_days)
        high_cutoff = now - timedelta(days=high_value_retention_days)
        stat_cutoff = date.today() - timedelta(days=stat_retention_days)

        low_result = await self.db.execute(
            delete(AccountRiskEvent).where(
                AccountRiskEvent.status.in_(LOW_VALUE_EVENT_STATUSES),
                AccountRiskEvent.created_at < low_cutoff,
            )
        )
        high_result = await self.db.execute(
            delete(AccountRiskEvent).where(
                ~AccountRiskEvent.status.in_(LOW_VALUE_EVENT_STATUSES),
                AccountRiskEvent.created_at < high_cutoff,
            )
        )
        stat_result = await self.db.execute(
            delete(AccountRiskDailyStat).where(AccountRiskDailyStat.stat_date < stat_cutoff)
        )
        await self.db.commit()
        return {
            "low_value_deleted": int(low_result.rowcount or 0),
            "high_value_deleted": int(high_result.rowcount or 0),
            "daily_stat_deleted": int(stat_result.rowcount or 0),
        }

    async def manual_adjust_risk(
        self,
        account_id: int,
        *,
        score_delta: Optional[float] = None,
        set_score: Optional[float] = None,
        target_level: Optional[str] = None,
        clear_pause: bool = False,
        reason: str = "manual_adjust",
        operator: Optional[str] = None,
    ) -> TelegramAccount:
        account = await self._get_db_account(account_id)
        if account is None:
            raise ValueError("account_not_found")
        if set_score is not None:
            account.risk_score = max(0.0, min(100.0, float(set_score)))
        elif score_delta is not None:
            account.risk_score = max(
                0.0, min(100.0, float(account.risk_score or 0.0) + float(score_delta))
            )
        risk_settings = await get_account_risk_guard_settings(self.db)
        lifecycle = risk_settings.get("lifecycle", {})
        if clear_pause:
            account.risk_pause_until = None
            account.risk_recovery_until = datetime.utcnow() + timedelta(
                seconds=int(lifecycle.get("recovery_seconds", RECOVERY_SECONDS))
            )
            if set_score is None:
                account.risk_score = min(
                    float(account.risk_score or 0.0),
                    float(lifecycle.get("manual_clear_score_cap", 44.0)),
                )
        if target_level:
            account.risk_level = AccountRiskLevel(target_level).value
        else:
            self._sync_risk_level(account, risk_settings=risk_settings)
        account.risk_reason = reason
        account.last_risk_event_at = datetime.utcnow()
        self.db.add(account)
        await self.record_event(
            account,
            AccountRiskAction.JOIN,
            "manual",
            reason=reason,
            details={
                "operator": operator,
                "score_delta": score_delta,
                "set_score": set_score,
                "target_level": target_level,
                "clear_pause": clear_pause,
            },
        )
        await self.db.refresh(account)
        return account

    @staticmethod
    def _budget_for_action(action: AccountRiskAction, risk_settings: dict[str, Any]) -> RiskBudget:
        raw = risk_settings.get("actions", {}).get(action.value, {})
        default = DEFAULT_ACTION_BUDGETS[action]
        return RiskBudget(
            daily_limit=int(raw.get("daily_limit", default.daily_limit)),
            cooldown_seconds=int(raw.get("cooldown_seconds", default.cooldown_seconds)),
        )

    async def _apply_risk_lifecycle(
        self,
        account: TelegramAccount,
        now: datetime,
        *,
        risk_settings: Optional[dict[str, Any]] = None,
        commit: bool = True,
    ) -> bool:
        risk_settings = risk_settings or await get_account_risk_guard_settings(self.db)
        lifecycle = risk_settings.get("lifecycle", {})
        changed = False
        if account.risk_pause_until and account.risk_pause_until <= now:
            account.risk_pause_until = None
            account.risk_recovery_until = now + timedelta(
                seconds=int(lifecycle.get("recovery_seconds", RECOVERY_SECONDS))
            )
            account.risk_score = min(
                float(account.risk_score or 0.0),
                float(lifecycle.get("post_freeze_score_cap", 69.0)),
            )
            if account.risk_level == AccountRiskLevel.FROZEN.value:
                account.risk_level = AccountRiskLevel.LIMITED.value
            account.risk_reason = "risk_recovery"
            changed = True

        last_decay = account.last_risk_decay_at or account.last_risk_event_at or account.created_at
        if last_decay is None:
            last_decay = now
        decay_interval_hours = int(lifecycle.get("decay_interval_hours", DECAY_INTERVAL_HOURS))
        quiet_enough = (
            account.last_risk_event_at is None
            or account.last_risk_event_at <= now - timedelta(hours=decay_interval_hours)
        )
        if quiet_enough and float(account.risk_score or 0.0) > 0:
            intervals = max(
                0, int((now - last_decay).total_seconds() // (decay_interval_hours * 3600))
            )
            if intervals > 0:
                account.risk_score = max(
                    0.0,
                    float(account.risk_score or 0.0)
                    - intervals
                    * float(lifecycle.get("decay_points_per_interval", DECAY_POINTS_PER_INTERVAL)),
                )
                account.last_risk_decay_at = now
                changed = True

        before_level = account.risk_level
        self._sync_risk_level(account, risk_settings=risk_settings)
        if account.risk_recovery_until and account.risk_recovery_until > now:
            if account.risk_level == AccountRiskLevel.NORMAL.value:
                account.risk_level = AccountRiskLevel.WATCH.value
        elif account.risk_recovery_until and account.risk_recovery_until <= now:
            account.risk_recovery_until = None
            self._sync_risk_level(account, risk_settings=risk_settings)
            changed = True
        if before_level != account.risk_level:
            changed = True

        if changed and commit:
            self.db.add(account)
            await self.db.commit()
        return changed

    def _sync_risk_level(
        self, account: TelegramAccount, *, risk_settings: Optional[dict[str, Any]] = None
    ) -> None:
        if account.risk_level == AccountRiskLevel.QUARANTINED.value:
            return
        if account.risk_pause_until and account.risk_pause_until > datetime.utcnow():
            account.risk_level = AccountRiskLevel.FROZEN.value
            return
        risk_settings = risk_settings or {}
        thresholds = risk_settings.get("level_thresholds", {})
        score = float(account.risk_score or 0.0)
        configured_thresholds = (
            (
                float(thresholds.get(AccountRiskLevel.QUARANTINED.value, 90.0)),
                AccountRiskLevel.QUARANTINED,
            ),
            (float(thresholds.get(AccountRiskLevel.FROZEN.value, 70.0)), AccountRiskLevel.FROZEN),
            (float(thresholds.get(AccountRiskLevel.LIMITED.value, 45.0)), AccountRiskLevel.LIMITED),
            (float(thresholds.get(AccountRiskLevel.WATCH.value, 20.0)), AccountRiskLevel.WATCH),
            (0.0, AccountRiskLevel.NORMAL),
        )
        for threshold, level in configured_thresholds:
            if score >= threshold:
                account.risk_level = level.value
                break
        if account.risk_level == AccountRiskLevel.FROZEN.value and not account.risk_pause_until:
            lifecycle = risk_settings.get("lifecycle", {})
            account.risk_pause_until = datetime.utcnow() + timedelta(
                seconds=int(lifecycle.get("default_freeze_seconds", DEFAULT_FREEZE_SECONDS))
            )

    @staticmethod
    def _sync_warmup_stage(account: TelegramAccount, stage: str, now: datetime) -> None:
        if not account.managed_started_at:
            account.managed_started_at = account.created_at or now
        if account.warmup_stage != stage:
            account.warmup_stage = stage
            account.warmup_stage_updated_at = now

    def _apply_budget_policy(
        self,
        budget: RiskBudget,
        account: Optional[TelegramAccount],
        now: datetime,
        *,
        risk_settings: dict[str, Any],
        warmup_multiplier: float = 1.0,
    ) -> RiskBudget:
        if account is None:
            return budget
        level_multipliers = risk_settings.get(
            "level_budget_multipliers", RISK_LEVEL_BUDGET_MULTIPLIER
        )
        lifecycle = risk_settings.get("lifecycle", {})
        level_multiplier = float(
            level_multipliers.get(account.risk_level or AccountRiskLevel.NORMAL.value, 1.0)
        )
        lifecycle_multiplier = 1.0
        if account.created_at and account.created_at > now - timedelta(
            days=int(lifecycle.get("new_account_days", NEW_ACCOUNT_DAYS))
        ):
            lifecycle_multiplier = min(
                lifecycle_multiplier, float(lifecycle.get("new_account_multiplier", 0.3))
            )
        if account.risk_recovery_until and account.risk_recovery_until > now:
            lifecycle_multiplier = min(
                lifecycle_multiplier, float(lifecycle.get("recovery_multiplier", 0.5))
            )
        healthy_since = account.last_risk_event_at or account.created_at
        if (
            account.risk_level == AccountRiskLevel.NORMAL.value
            and healthy_since
            and healthy_since
            <= now
            - timedelta(days=int(lifecycle.get("healthy_account_days", HEALTHY_ACCOUNT_DAYS)))
        ):
            lifecycle_multiplier = max(
                lifecycle_multiplier, float(lifecycle.get("healthy_account_multiplier", 1.0))
            )
        multiplier = max(
            0.0,
            min(
                level_multiplier * lifecycle_multiplier * max(0.0, warmup_multiplier),
                float(lifecycle.get("max_budget_multiplier", 1.0)),
            ),
        )
        daily_limit = max(1, int(budget.daily_limit * multiplier)) if multiplier > 0 else 0
        cooldown_seconds = budget.cooldown_seconds
        if multiplier < 1.0 and cooldown_seconds > 0:
            cooldown_seconds = int(cooldown_seconds / max(multiplier, 0.25))
        return RiskBudget(daily_limit=daily_limit, cooldown_seconds=cooldown_seconds)

    async def _check_content_policy(
        self,
        account_id: int,
        action: AccountRiskAction,
        target_type: Optional[str],
        target_id: Optional[Any],
        details: Optional[dict[str, Any]],
    ) -> RiskDecision:
        if action not in MESSAGE_ACTIONS:
            return RiskDecision(True)
        details = details or {}
        if details.get("source") in CONTENT_DEDUP_EXEMPT_SOURCES:
            return RiskDecision(True)
        content = details.get("content") or details.get("text") or details.get("caption")
        if not content:
            return RiskDecision(True)
        normalized = self._normalize_content(str(content))
        if len(normalized) < 12:
            return RiskDecision(True)
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        if self.cache.client is None:
            return RiskDecision(True)

        target_scope = (
            f"{target_type or 'target'}:{target_id}" if target_id is not None else "target:unknown"
        )
        account_key = f"risk:content:account:{account_id}:{content_hash}"
        target_key = f"risk:content:target:{target_scope}:{content_hash}"
        account_seen = await self.cache.exists(account_key)
        target_seen = await self.cache.exists(target_key)
        if account_seen:
            return RiskDecision(False, reason="content_repeat_account")
        if target_seen:
            return RiskDecision(False, reason="content_repeat_target")

        index_key = f"risk:content:index:{target_scope}"
        try:
            recent = await self.cache.client.lrange(index_key, 0, 24)
            for item in recent or []:
                try:
                    payload = json.loads(item)
                    previous = str(payload.get("normalized") or "")
                except Exception:
                    previous = str(item)
                if previous and SequenceMatcher(None, normalized, previous).ratio() >= 0.92:
                    return RiskDecision(False, reason="content_similar_target")
            await self.cache.client.lpush(
                index_key,
                json.dumps({"hash": content_hash, "normalized": normalized}, ensure_ascii=False),
            )
            await self.cache.client.ltrim(index_key, 0, 49)
            await self.cache.client.expire(index_key, 3 * 24 * 3600)
        except Exception as exc:
            self.logger.warning("content_similarity_check_failed", error=str(exc))

        await self.cache.set(account_key, "1", ttl=6 * 3600)
        await self.cache.set(target_key, "1", ttl=3 * 24 * 3600)
        return RiskDecision(True)

    async def _increment_daily_stat(
        self,
        account_id: Optional[int],
        action: AccountRiskAction,
        status: str,
        reason: Optional[str],
        target_type: Optional[str],
    ) -> None:
        today = date.today()
        result = await self.db.execute(
            select(AccountRiskDailyStat).where(
                AccountRiskDailyStat.account_id == account_id,
                AccountRiskDailyStat.stat_date == today,
                AccountRiskDailyStat.action == action.value,
                AccountRiskDailyStat.status == status,
                AccountRiskDailyStat.target_type == target_type,
            )
        )
        stat = result.scalar_one_or_none()
        now = datetime.utcnow()
        if stat is None:
            stat = AccountRiskDailyStat(
                account_id=account_id,
                stat_date=today,
                action=action.value,
                status=status,
                target_type=target_type,
                count=1,
                last_reason=reason,
                first_seen_at=now,
                last_seen_at=now,
            )
        else:
            stat.count += 1
            stat.last_reason = reason
            stat.last_seen_at = now
        self.db.add(stat)

    @staticmethod
    def _should_keep_detail_event(status: str, reason: Optional[str]) -> bool:
        if status not in LOW_VALUE_EVENT_STATUSES:
            return True
        return reason not in {None, "allowed"}

    @staticmethod
    def _normalize_content(value: str) -> str:
        text = value.lower()
        text = re.sub(r"https?://\S+", "<url>", text)
        text = re.sub(r"t\.me/\S+", "<telegram>", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    async def _block(
        self,
        account: Any,
        action: AccountRiskAction,
        reason: str,
        target_type: Optional[str],
        target_id: Optional[Any],
        details: Optional[dict[str, Any]],
        *,
        retry_after_seconds: Optional[int] = None,
    ) -> RiskDecision:
        await self.record_event(
            account,
            action,
            "block",
            reason=reason,
            target_type=target_type,
            target_id=target_id,
            details={**(details or {}), "retry_after_seconds": retry_after_seconds}
            if retry_after_seconds
            else details,
        )
        return RiskDecision(False, reason=reason, retry_after_seconds=retry_after_seconds)

    async def _get_db_account(self, account_id: Optional[int]) -> Optional[TelegramAccount]:
        if account_id is None:
            return None
        result = await self.db.execute(
            select(TelegramAccount).where(TelegramAccount.id == account_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _account_id(account: Any) -> Optional[int]:
        value = getattr(account, "account_id", None) or getattr(account, "id", None)
        return int(value) if value is not None else None

    @staticmethod
    def _enum_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        return getattr(value, "value", str(value))

    @staticmethod
    def _country_code(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).upper()
        return text[:2] if text else None

    @staticmethod
    def classify_error(
        exc: Exception | str,
        *,
        action: AccountRiskAction | str | None = None,
        target_type: Optional[str] = None,
    ) -> str:
        text = (
            f"{exc.__class__.__name__}: {exc}".lower()
            if isinstance(exc, Exception)
            else str(exc).lower()
        )
        if "peer_flood" in text or "peer flood" in text or "peerflood" in text:
            return "peer_flood"
        if "flood" in text or "wait of" in text or "retry after" in text:
            return "flood_wait"
        action_value = AccountRiskAction(action).value if action is not None else None
        if action_value == AccountRiskAction.JOIN.value and (
            "successfully requested to join" in text
            or "join request pending" in text
            or "request to join" in text
        ):
            return "join_request_pending"
        is_group_context = (
            action_value
            in {
                AccountRiskAction.AD_DELIVERY.value,
                AccountRiskAction.GROUP_MESSAGE.value,
                AccountRiskAction.AD_PROBE.value,
                AccountRiskAction.AI_WARMUP.value,
            }
            or target_type == "group"
        )
        group_write_markers = (
            "banned from sending messages in supergroups",
            "banned from sending messages in channels",
            "you're banned from sending messages",
            "chatwriteforbidden",
            "chat_write_forbidden",
            "write forbidden",
            "send messages",
            "can't write",
            "cannot write",
            "not enough rights",
            "slowmode",
            "slow mode",
            "userbannedinchannel",
            "user banned in channel",
            "topic_closed",
            "topic closed",
            "channel specified is private",
            "lack permission",
            "were banned from it",
            "private channel",
        )
        if is_group_context and any(marker in text for marker in group_write_markers):
            return "group_write_forbidden"
        account_markers = (
            "auth key",
            "auth_key",
            "session revoked",
            "session_revoke",
            "user deactivated",
            "user_deactivated",
            "phone number banned",
            "phone_number_banned",
            "user_deactivated_ban",
        )
        if any(marker in text for marker in account_markers):
            return "account_banned"
        if "banned" in text or "deactivated" in text:
            return "account_banned"
        if "user_restricted" in text or "userrestricted" in text:
            return "account_restricted"
        if "restricted" in text or "privacy" in text or "not allowed" in text:
            return "account_restricted"
        return "telegram_error"

    @staticmethod
    def extract_wait_seconds(exc: Exception | str) -> Optional[int]:
        text = f"{exc.__class__.__name__}: {exc}" if isinstance(exc, Exception) else str(exc)
        patterns = (
            r"flood\D+(\d+)",
            r"wait of\D*(\d+)",
            r"retry after\D*(\d+)",
            r"(\d+)\s*seconds?",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _risk_score_delta(
        reason: Optional[str],
        details: Optional[dict[str, Any]] = None,
        risk_settings: Optional[dict[str, Any]] = None,
    ) -> float:
        deltas = (risk_settings or {}).get("risk_score_deltas", {})
        if reason == "group_write_forbidden":
            return (
                float(
                    deltas.get(
                        "platform_group_write_forbidden", GROUP_WRITE_FORBIDDEN_PLATFORM_SCORE
                    )
                )
                if AccountRiskGuard._looks_like_platform_group_write_ban(details)
                else float(deltas.get("group_write_forbidden", GROUP_WRITE_FORBIDDEN_BASE_SCORE))
            )
        if reason == "join_request_pending":
            return 0.0
        if reason == "flood_wait":
            return float(deltas.get("flood_wait", 15.0))
        if reason == "peer_flood":
            return float(deltas.get("peer_flood", 35.0))
        if reason == "account_banned":
            return float(deltas.get("account_banned", 50.0))
        if reason == "account_restricted":
            return float(deltas.get("account_restricted", 50.0))
        return float(deltas.get("generic_failure", 5.0))

    @staticmethod
    def _looks_like_platform_group_write_ban(details: Optional[dict[str, Any]]) -> bool:
        if not details:
            return False
        text_parts = []
        for key in ("error", "permission_reason", "reason"):
            value = details.get(key)
            if value:
                text_parts.append(str(value))
        text = " ".join(text_parts).lower()
        return any(marker in text for marker in PLATFORM_GROUP_WRITE_BAN_MARKERS)

    @staticmethod
    def _confirmed_account_wide_group_write_ban(details: Optional[dict[str, Any]]) -> bool:
        """Require a failed probe in a previously writable control group."""
        if not details:
            return False
        return (
            details.get("control_probe_confirmed") is True
            and details.get("control_group_previously_writable") is True
            and AccountRiskGuard._looks_like_platform_group_write_ban(details)
        )
