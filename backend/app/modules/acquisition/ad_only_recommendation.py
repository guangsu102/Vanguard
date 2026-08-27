"""Evidence-backed ad-only recommendations and recoverable handovers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.pool import get_account_pool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
    parse_telegram_group_link,
)
from app.core.automation_settings import (
    get_account_risk_guard_settings,
    get_ad_capacity_settings,
    get_ad_only_recommendation_settings,
)
from app.core.ephemeral_secret import (
    EphemeralSecretError,
    decrypt_ephemeral_secret,
    encrypt_ephemeral_secret,
)
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdDeliveryPolicy,
    AdDeliveryScheduleState,
    AdScheduleStatus,
    AdSendMode,
    AdSurvivalStatus,
    DeliveryStatus,
    GroupAdHandover,
    GroupAdOnlyAssessment,
    GroupAdOnlyEvent,
    GroupAdPolicyEvent,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
)

logger = structlog.get_logger()

RULE_VERSION = "ad-only-v1"
GROUP_FAILURE_PREFIXES = ("group_control:", "group_control_left:")
AUTHORITATIVE_EVIDENCE_SOURCES = {
    "about",
    "description",
    "full_about",
    "pinned_message",
    "pinned",
    "manual",
}
PEER_EVIDENCE_SOURCE = "recent_promotional_message"
ALLOWED_POLICY_MODES = {
    GroupAdPolicyMode.SOFT_AD_TRIAL.value,
    GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
    GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
}
ACTIVE_HANDOVER_STATUSES = {
    "queued",
    "running",
    "failed",
    "cleanup_pending",
    "rollback_pending",
}
TERMINAL_HANDOVER_STATUSES = {"completed", "rolled_back", "cancelled"}
DIRECT_WORKFLOW_TYPE = "direct"
ASSESSMENT_WORKFLOW_TYPE = "assessment"


class AdOnlyWorkflowError(ValueError):
    """Raised when an assessment or handover precondition is not satisfied."""


def _now() -> datetime:
    return datetime.utcnow()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _account_label(account: TelegramAccount | None) -> str | None:
    if account is None:
        return None
    return account.display_name or account.identifier or account.phone or f"account-{account.id}"


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _is_group_failure(log: AdDeliveryLog) -> bool:
    error = str(log.error or "")
    return any(error.startswith(prefix) for prefix in GROUP_FAILURE_PREFIXES)


def _safe_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(value in lowered for value in ("t.me/", "telegram.me/", "tg://")):
        return f"{exc.__class__.__name__}: telegram operation failed"
    return text[:2000]


def _event_payload(event: GroupAdOnlyEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "group_id": event.group_id,
        "assessment_id": event.assessment_id,
        "handover_id": event.handover_id,
        "event_type": event.event_type,
        "step": event.step,
        "status": event.status,
        "actor_user_id": event.actor_user_id,
        "message": event.message,
        "payload": _json_load(event.payload_json, {}),
        "created_at": _iso(event.created_at),
    }


class AdOnlyRecommendationService:
    """Build immutable recommendations and manage durable handovers."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.logger = logger.bind(module="ad_only_recommendation")

    async def _add_event(
        self,
        *,
        group_id: int | None,
        event_type: str,
        assessment_id: int | None = None,
        handover_id: int | None = None,
        step: str | None = None,
        status: str | None = None,
        actor_user_id: int | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> GroupAdOnlyEvent:
        event = GroupAdOnlyEvent(
            group_id=group_id,
            assessment_id=assessment_id,
            handover_id=handover_id,
            event_type=event_type,
            step=step,
            status=status,
            actor_user_id=actor_user_id,
            message=(message or "")[:500] or None,
            payload_json=_json_dump(payload or {}),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def evaluate_group(
        self,
        group_id: int,
        *,
        settings: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> GroupAdOnlyAssessment:
        now = now or _now()
        config = settings or await get_ad_only_recommendation_settings(self.db)
        group = await self.db.get(Group, group_id)
        if group is None:
            raise AdOnlyWorkflowError("group_not_found")

        membership_rows = await self.db.execute(
            select(GroupAccountMembership, TelegramAccount, AccountOperationConfig)
            .join(TelegramAccount, TelegramAccount.id == GroupAccountMembership.account_id)
            .outerjoin(
                AccountOperationConfig,
                AccountOperationConfig.account_id == TelegramAccount.id,
            )
            .where(
                GroupAccountMembership.group_id == group.id,
                GroupAccountMembership.status == "joined",
            )
        )
        growth_memberships: list[tuple[GroupAccountMembership, TelegramAccount]] = []
        ad_only_memberships: list[tuple[GroupAccountMembership, TelegramAccount]] = []
        for membership, account, operation_config in membership_rows.all():
            mode = _enum_value(
                operation_config.operation_mode
                if operation_config is not None
                else AccountOperationMode.GROWTH.value
            )
            target = (
                ad_only_memberships
                if mode == AccountOperationMode.AD_ONLY.value
                else growth_memberships
            )
            target.append((membership, account))

        source_account = growth_memberships[0][1] if len(growth_memberships) == 1 else None
        blockers: list[str] = []
        if len(growth_memberships) != 1:
            blockers.append("requires_exactly_one_joined_growth_account")
        if ad_only_memberships:
            blockers.append("ad_only_account_already_joined")
        if group.ad_delivery_account_id is not None:
            blockers.append("ad_only_owner_already_assigned")

        lookback_start = now - timedelta(days=int(config["risk_lookback_days"]))
        existing_ad_only_activity = int(
            (
                await self.db.execute(
                    select(func.count(AdDeliveryLog.id))
                    .join(AdCampaign, AdCampaign.id == AdDeliveryLog.ad_campaign_id)
                    .where(
                        AdDeliveryLog.group_id == group.id,
                        AdDeliveryLog.created_at >= lookback_start,
                        AdCampaign.delivery_policy == AdDeliveryPolicy.AD_ONLY.value,
                    )
                )
            ).scalar()
            or 0
        )
        if existing_ad_only_activity:
            blockers.append("recent_ad_only_activity_exists")

        logs: list[AdDeliveryLog] = []
        if source_account is not None:
            rows = await self.db.execute(
                select(AdDeliveryLog)
                .join(AdCampaign, AdCampaign.id == AdDeliveryLog.ad_campaign_id)
                .where(
                    AdDeliveryLog.group_id == group.id,
                    AdDeliveryLog.account_id == source_account.id,
                    AdDeliveryLog.creative_id.is_not(None),
                    AdDeliveryLog.created_at >= lookback_start,
                    AdCampaign.delivery_policy == AdDeliveryPolicy.GROWTH.value,
                )
                .order_by(desc(AdDeliveryLog.created_at), desc(AdDeliveryLog.id))
            )
            logs = list(rows.scalars().all())

        current_window: list[AdDeliveryLog] = []
        completed: list[AdDeliveryLog] = []
        pending: list[AdDeliveryLog] = []
        for log in logs:
            if log.survival_status == AdSurvivalStatus.DELETED.value or _is_group_failure(log):
                break
            if log.status == DeliveryStatus.SUCCESS.value:
                current_window.append(log)
                if log.survived_twenty_four_hour_at is not None:
                    completed.append(log)
                elif log.survival_status in {
                    AdSurvivalStatus.PENDING.value,
                    AdSurvivalStatus.NOT_REQUIRED.value,
                    AdSurvivalStatus.CHECK_FAILED.value,
                }:
                    pending.append(log)

        group_failure_count = sum(1 for log in logs if _is_group_failure(log))
        deleted_sample_count = sum(
            1 for log in logs if log.survival_status == AdSurvivalStatus.DELETED.value
        )
        completed_count = len(completed)
        send_success_percent = 100 if current_window else 0
        survival_percent = (
            int(round((completed_count / len(current_window)) * 100))
            if current_window
            else 0
        )

        evidence = await self._policy_evidence(group, config, now)
        if evidence["negative"]:
            blockers.append("negative_permission_evidence")
        elif not evidence["positive"]:
            blockers.append("positive_permission_evidence_required")

        profile = await self._group_profile(group.id)
        if profile is None or profile.ad_policy_mode not in ALLOWED_POLICY_MODES:
            blockers.append("current_ad_policy_not_allowed")
        elif profile.ad_policy_expires_at and profile.ad_policy_expires_at <= now:
            blockers.append("current_ad_policy_expired")

        required_samples = int(config["min_consecutive_samples"])
        if completed_count < required_samples:
            blockers.append("insufficient_consecutive_formal_ads")
        if pending:
            blockers.append("survival_checks_pending")
        if send_success_percent < int(config["required_send_success_percent"]):
            blockers.append("send_success_threshold_not_met")
        if survival_percent < int(config["required_survival_24h_percent"]):
            blockers.append("survival_24h_threshold_not_met")

        sample_times = [log.sent_at or log.created_at for log in current_window]
        sample_start = min(sample_times) if sample_times else now
        sample_end = max(sample_times) if sample_times else now
        evidence_payload = {
            **evidence,
            "profile": {
                "mode": profile.ad_policy_mode if profile else None,
                "confidence": int(profile.ad_policy_confidence or 0) if profile else 0,
                "source": profile.ad_policy_source if profile else None,
                "verified_at": _iso(profile.ad_policy_verified_at) if profile else None,
                "expires_at": _iso(profile.ad_policy_expires_at) if profile else None,
            },
        }
        evidence_hash = hashlib.sha256(
            _json_dump(evidence_payload).encode("utf-8")
        ).hexdigest()
        is_recommended = not blockers
        assessment = GroupAdOnlyAssessment(
            group_id=group.id,
            telegram_group_id=group.group_id,
            source_growth_account_id=source_account.id if source_account else None,
            status="recommended" if is_recommended else "observing",
            rule_version=RULE_VERSION,
            completed_sample_count=completed_count,
            consecutive_success_count=completed_count,
            send_success_percent=send_success_percent,
            survival_24h_percent=survival_percent,
            pending_sample_count=len(pending),
            group_failure_count=group_failure_count,
            deleted_sample_count=deleted_sample_count,
            metrics_json=_json_dump(
                {
                    "formal_log_count": len(logs),
                    "current_window_count": len(current_window),
                    "existing_ad_only_activity_count": existing_ad_only_activity,
                    "growth_membership_count": len(growth_memberships),
                    "ad_only_membership_count": len(ad_only_memberships),
                }
            ),
            blocking_reasons_json=_json_dump(blockers),
            evidence_json=_json_dump(evidence_payload),
            evidence_hash=evidence_hash,
            sample_window_started_at=sample_start,
            sample_window_ended_at=sample_end,
            valid_until=(
                now + timedelta(days=int(config["recommendation_ttl_days"]))
                if is_recommended
                else None
            ),
            created_at=now,
        )
        self.db.add(assessment)
        await self.db.flush()
        await self._add_event(
            group_id=group.id,
            assessment_id=assessment.id,
            event_type="assessment_created",
            status=assessment.status,
            message="Immutable ad-only assessment created",
            payload={"blocking_reasons": blockers, "evidence_hash": evidence_hash},
        )
        await self.db.commit()
        await self.db.refresh(assessment)
        return assessment

    async def _group_profile(self, group_id: int) -> GroupAdProfile | None:
        return (
            await self.db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id == group_id)
            )
        ).scalar_one_or_none()

    async def _joined_account_modes(
        self, group_id: int
    ) -> tuple[list[int], list[int]]:
        rows = await self.db.execute(
            select(GroupAccountMembership.account_id, AccountOperationConfig)
            .outerjoin(
                AccountOperationConfig,
                AccountOperationConfig.account_id
                == GroupAccountMembership.account_id,
            )
            .where(
                GroupAccountMembership.group_id == group_id,
                GroupAccountMembership.status == "joined",
            )
        )
        growth_ids: list[int] = []
        ad_only_ids: list[int] = []
        for account_id, operation_config in rows.all():
            mode = _enum_value(
                operation_config.operation_mode
                if operation_config is not None
                else AccountOperationMode.GROWTH.value
            )
            target = (
                ad_only_ids
                if mode == AccountOperationMode.AD_ONLY.value
                else growth_ids
            )
            target.append(int(account_id))
        return growth_ids, ad_only_ids

    async def _policy_evidence(
        self,
        group: Group,
        config: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        cutoff = now - timedelta(days=int(config["risk_lookback_days"]))
        rows = await self.db.execute(
            select(GroupAdPolicyEvent)
            .where(
                GroupAdPolicyEvent.group_id == group.id,
                GroupAdPolicyEvent.created_at >= cutoff,
            )
            .order_by(desc(GroupAdPolicyEvent.created_at), desc(GroupAdPolicyEvent.id))
        )
        events = list(rows.scalars().all())
        negative = [
            event
            for event in events
            if event.new_mode == GroupAdPolicyMode.FORBIDDEN.value
        ]
        explicit = [
            event
            for event in events
            if event.new_mode in ALLOWED_POLICY_MODES
            and str(event.source or "") in AUTHORITATIVE_EVIDENCE_SOURCES
        ]
        peer_cutoff = now - timedelta(hours=int(config["peer_ad_min_survival_hours"]))
        peer_events = [
            event
            for event in events
            if event.source == PEER_EVIDENCE_SOURCE
            and event.new_mode in ALLOWED_POLICY_MODES
            and event.created_at <= peer_cutoff
            and event.created_at
            >= now - timedelta(days=int(config["peer_ad_lookback_days"]))
        ]
        peer_senders: set[str] = set()
        for event in peer_events:
            parsed = _json_load(event.evidence, {})
            values: Iterable[Any] = []
            if isinstance(parsed, dict):
                values = parsed.get("sender_ids") or parsed.get("senders") or []
            for value in values if isinstance(values, list) else []:
                peer_senders.add(str(value))
            if event.account_id is not None:
                peer_senders.add(f"account:{event.account_id}")
        peer_positive = (
            len(peer_events) >= int(config["peer_ad_min_messages"])
            and len(peer_senders) >= int(config["peer_ad_min_senders"])
        )
        return {
            "positive": bool(explicit or peer_positive),
            "negative": bool(negative),
            "explicit_event_ids": [event.id for event in explicit],
            "negative_event_ids": [event.id for event in negative],
            "peer_event_ids": [event.id for event in peer_events],
            "peer_sender_count": len(peer_senders),
            "ai_advisory_only": True,
        }

    async def evaluate_candidates(
        self,
        *,
        limit: int = 200,
        force: bool = False,
    ) -> dict[str, Any]:
        config = await get_ad_only_recommendation_settings(self.db)
        if not config["recommendation_enabled"] and not force:
            return {
                "status": "skipped",
                "reason": "recommendation_disabled",
                "processed": 0,
                "recommended": 0,
            }
        rows = await self.db.execute(
            select(Group.id)
            .join(GroupAccountMembership, GroupAccountMembership.group_id == Group.id)
            .where(GroupAccountMembership.status == "joined")
            .distinct()
            .order_by(Group.id.asc())
            .limit(max(1, min(int(limit), 1000)))
        )
        group_ids = [int(value) for value in rows.scalars().all()]
        recommended = 0
        failures: list[dict[str, Any]] = []
        for group_id in group_ids:
            try:
                assessment = await self.evaluate_group(group_id, settings=config)
                if assessment.status == "recommended":
                    recommended += 1
            except Exception as exc:
                await self.db.rollback()
                failures.append({"group_id": group_id, "error": str(exc)[:500]})
                self.logger.warning(
                    "ad_only_assessment_failed",
                    group_id=group_id,
                    error=str(exc),
                )
        return {
            "status": "completed",
            "processed": len(group_ids),
            "recommended": recommended,
            "failed": len(failures),
            "failures": failures,
        }

    async def _latest_decision(
        self, assessment_id: int
    ) -> GroupAdOnlyEvent | None:
        return (
            await self.db.execute(
                select(GroupAdOnlyEvent)
                .where(
                    GroupAdOnlyEvent.assessment_id == assessment_id,
                    GroupAdOnlyEvent.event_type.in_(
                        [
                            "assessment_approved",
                            "assessment_rejected",
                            "assessment_deferred",
                        ]
                    ),
                )
                .order_by(desc(GroupAdOnlyEvent.created_at), desc(GroupAdOnlyEvent.id))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def assessment_payload(
        self, assessment: GroupAdOnlyAssessment
    ) -> dict[str, Any]:
        decision = await self._latest_decision(assessment.id)
        return {
            "id": assessment.id,
            "group_id": assessment.group_id,
            "telegram_group_id": assessment.telegram_group_id,
            "group_title": assessment.group.title if assessment.group else None,
            "source_growth_account_id": assessment.source_growth_account_id,
            "source_growth_account_label": _account_label(
                assessment.source_growth_account
            ),
            "status": assessment.status,
            "rule_version": assessment.rule_version,
            "completed_sample_count": assessment.completed_sample_count,
            "consecutive_success_count": assessment.consecutive_success_count,
            "send_success_percent": assessment.send_success_percent,
            "survival_24h_percent": assessment.survival_24h_percent,
            "pending_sample_count": assessment.pending_sample_count,
            "group_failure_count": assessment.group_failure_count,
            "deleted_sample_count": assessment.deleted_sample_count,
            "metrics": _json_load(assessment.metrics_json, {}),
            "blocking_reasons": _json_load(
                assessment.blocking_reasons_json, []
            ),
            "evidence": _json_load(assessment.evidence_json, {}),
            "evidence_hash": assessment.evidence_hash,
            "sample_window_started_at": _iso(
                assessment.sample_window_started_at
            ),
            "sample_window_ended_at": _iso(assessment.sample_window_ended_at),
            "valid_until": _iso(assessment.valid_until),
            "created_at": _iso(assessment.created_at),
            "decision": _event_payload(decision) if decision else None,
        }

    async def list_latest_assessments(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        latest_ids = (
            select(func.max(GroupAdOnlyAssessment.id).label("id"))
            .group_by(GroupAdOnlyAssessment.group_id)
            .subquery()
        )
        query = (
            select(GroupAdOnlyAssessment)
            .join(latest_ids, latest_ids.c.id == GroupAdOnlyAssessment.id)
            .order_by(desc(GroupAdOnlyAssessment.created_at))
            .limit(max(1, min(int(limit), 500)))
        )
        if status:
            query = query.where(GroupAdOnlyAssessment.status == status)
        rows = await self.db.execute(query)
        return [
            await self.assessment_payload(assessment)
            for assessment in rows.scalars().unique().all()
        ]

    async def assessment_history(
        self,
        group_id: int,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        assessments = list(
            (
                await self.db.execute(
                    select(GroupAdOnlyAssessment)
                    .where(GroupAdOnlyAssessment.group_id == group_id)
                    .order_by(
                        desc(GroupAdOnlyAssessment.created_at),
                        desc(GroupAdOnlyAssessment.id),
                    )
                    .limit(max(1, min(int(limit), 500)))
                )
            )
            .scalars()
            .unique()
            .all()
        )
        events = list(
            (
                await self.db.execute(
                    select(GroupAdOnlyEvent)
                    .where(GroupAdOnlyEvent.group_id == group_id)
                    .order_by(
                        desc(GroupAdOnlyEvent.created_at),
                        desc(GroupAdOnlyEvent.id),
                    )
                    .limit(max(1, min(int(limit) * 3, 1000)))
                )
            )
            .scalars()
            .all()
        )
        return {
            "assessments": [
                await self.assessment_payload(assessment)
                for assessment in assessments
            ],
            "events": [_event_payload(event) for event in events],
        }

    async def decide_assessment(
        self,
        assessment_id: int,
        *,
        decision: str,
        actor_user_id: int,
        note: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(decision or "").strip().lower()
        if normalized not in {"approve", "reject", "defer"}:
            raise AdOnlyWorkflowError("invalid_assessment_decision")
        assessment = await self.db.get(GroupAdOnlyAssessment, assessment_id)
        if assessment is None:
            raise AdOnlyWorkflowError("assessment_not_found")
        latest_id = (
            await self.db.execute(
                select(func.max(GroupAdOnlyAssessment.id)).where(
                    GroupAdOnlyAssessment.group_id == assessment.group_id
                )
            )
        ).scalar()
        if int(latest_id or 0) != assessment.id:
            raise AdOnlyWorkflowError("assessment_is_not_latest")
        now = _now()
        if normalized == "approve":
            if assessment.status != "recommended":
                raise AdOnlyWorkflowError("assessment_not_recommended")
            if assessment.valid_until is None or assessment.valid_until <= now:
                raise AdOnlyWorkflowError("assessment_expired")
        event_types = {
            "approve": "assessment_approved",
            "reject": "assessment_rejected",
            "defer": "assessment_deferred",
        }
        event = await self._add_event(
            group_id=assessment.group_id,
            assessment_id=assessment.id,
            event_type=event_types[normalized],
            status=normalized,
            actor_user_id=actor_user_id,
            message=note or f"Assessment {normalized}d by admin",
        )
        await self.db.commit()
        return _event_payload(event)

    @staticmethod
    def _validate_schedule(
        send_mode: str,
        interval_minutes: int,
        scheduled_times: list[str] | None,
    ) -> tuple[str, int, list[str], int]:
        mode = str(send_mode or "").strip()
        if mode not in {AdSendMode.INTERVAL.value, AdSendMode.SCHEDULED.value}:
            raise AdOnlyWorkflowError("ad_only_requires_interval_or_schedule")
        interval = int(interval_minutes or 0)
        values: list[str] = []
        for value in scheduled_times or []:
            text = str(value).strip()
            try:
                hour_text, minute_text = text.split(":", 1)
                hour = int(hour_text)
                minute = int(minute_text)
            except (TypeError, ValueError):
                raise AdOnlyWorkflowError("invalid_scheduled_time") from None
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise AdOnlyWorkflowError("invalid_scheduled_time")
            normalized = f"{hour:02d}:{minute:02d}"
            if normalized not in values:
                values.append(normalized)
        values.sort()
        if mode == AdSendMode.INTERVAL.value:
            if not 30 <= interval <= 10080:
                raise AdOnlyWorkflowError("interval_minutes_out_of_range")
            estimated = max(1, math.ceil(1440 / interval))
        else:
            if not values:
                raise AdOnlyWorkflowError("scheduled_times_required")
            if len(values) > 24:
                raise AdOnlyWorkflowError("too_many_scheduled_times")
            estimated = len(values)
            interval = max(30, interval or 1440)
        return mode, interval, values, estimated

    @staticmethod
    def _campaign_daily_sends(campaign: AdCampaign) -> int:
        if campaign.send_mode == AdSendMode.SCHEDULED.value:
            return len(campaign.get_scheduled_times())
        if campaign.send_mode == AdSendMode.INTERVAL.value:
            return max(1, math.ceil(1440 / max(1, int(campaign.interval_minutes or 1))))
        return max(1, int(campaign.max_sends_per_account_per_day or 1))

    async def _existing_daily_sends(
        self,
        account_id: int,
        *,
        exclude_campaign_id: int | None = None,
    ) -> int:
        query = (
            select(AdCampaign)
            .join(
                AccountAdBinding,
                AccountAdBinding.ad_campaign_id == AdCampaign.id,
            )
            .where(
                AccountAdBinding.account_id == account_id,
                AccountAdBinding.enabled,
                AdCampaign.enabled,
            )
        )
        if exclude_campaign_id is not None:
            query = query.where(AdCampaign.id != exclude_campaign_id)
        rows = await self.db.execute(query)
        return sum(
            self._campaign_daily_sends(campaign)
            for campaign in rows.scalars().unique().all()
        )

    async def preflight_direct_assignment(
        self,
        *,
        target_account_id: int,
        creative_id: int,
        invite_link: str,
        send_mode: str,
        interval_minutes: int,
        scheduled_times: list[str] | None,
        permission_mode: str,
        permission_note: str,
        permission_expires_at: datetime,
    ) -> dict[str, Any]:
        now = _now()
        settings = await get_ad_only_recommendation_settings(self.db)
        if not settings["handover_execution_enabled"]:
            raise AdOnlyWorkflowError("handover_execution_disabled")

        target = await self.db.get(TelegramAccount, target_account_id)
        if target is None:
            raise AdOnlyWorkflowError("target_account_not_found")
        if (
            target.account_type != AccountType.PROMOTER
            or not target.is_active
            or target.status in {AccountStatus.ERROR, AccountStatus.BANNED}
            or target.risk_level
            not in {AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value}
        ):
            raise AdOnlyWorkflowError("target_account_unavailable")
        target_config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == target.id
                )
            )
        ).scalar_one_or_none()
        if (
            target_config is None
            or not target_config.enabled
            or not target_config.auto_ads_enabled
            or target_config.operation_mode != AccountOperationMode.AD_ONLY.value
        ):
            raise AdOnlyWorkflowError("target_account_not_enabled_ad_only")

        creative = await self.db.get(AdCreative, creative_id)
        if creative is None or not creative.enabled:
            raise AdOnlyWorkflowError("creative_not_enabled")
        try:
            parsed_link = parse_telegram_group_link(invite_link)
        except TelegramExecutionError as exc:
            raise AdOnlyWorkflowError(f"invalid_invite_link:{exc}") from exc

        mode, interval, times, estimated = self._validate_schedule(
            send_mode, interval_minutes, scheduled_times
        )
        allowed_permission_modes = {
            GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
            GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
        }
        normalized_permission_mode = str(permission_mode or "").strip()
        if normalized_permission_mode not in allowed_permission_modes:
            raise AdOnlyWorkflowError("invalid_direct_permission_mode")
        normalized_note = str(permission_note or "").strip()
        if len(normalized_note) < 3:
            raise AdOnlyWorkflowError("direct_permission_note_required")
        if permission_expires_at <= now:
            raise AdOnlyWorkflowError("direct_permission_expiry_required")
        if permission_expires_at > now + timedelta(days=365):
            raise AdOnlyWorkflowError("direct_permission_expiry_too_far")

        risk_settings = await get_account_risk_guard_settings(self.db)
        hard_cap = await AccountRiskGuard(self.db)._outbound_message_hard_cap(
            target.id, risk_settings
        )
        existing_daily_sends = await self._existing_daily_sends(target.id)
        if existing_daily_sends + estimated > hard_cap:
            raise AdOnlyWorkflowError("target_account_daily_capacity_exceeded")
        return {
            "target": target,
            "creative": creative,
            "invite_link": invite_link,
            "invite_kind": parsed_link.kind,
            "send_mode": mode,
            "interval_minutes": interval,
            "scheduled_times": times,
            "estimated_daily_sends": estimated,
            "existing_daily_sends": existing_daily_sends,
            "hard_cap": hard_cap,
            "permission_mode": normalized_permission_mode,
            "permission_note": normalized_note,
            "permission_expires_at": permission_expires_at,
        }

    async def create_direct_assignment(
        self,
        *,
        target_account_id: int,
        creative_id: int,
        invite_link: str,
        send_mode: str,
        interval_minutes: int,
        scheduled_times: list[str] | None,
        permission_mode: str,
        permission_note: str,
        permission_expires_at: datetime,
        idempotency_key: str,
        requested_by_user_id: int,
    ) -> tuple[GroupAdHandover, bool]:
        key = str(idempotency_key or "").strip()
        if not 8 <= len(key) <= 64:
            raise AdOnlyWorkflowError("invalid_idempotency_key")
        existing = (
            await self.db.execute(
                select(GroupAdHandover).where(
                    GroupAdHandover.idempotency_key == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        values = await self.preflight_direct_assignment(
            target_account_id=target_account_id,
            creative_id=creative_id,
            invite_link=invite_link,
            send_mode=send_mode,
            interval_minutes=interval_minutes,
            scheduled_times=scheduled_times,
            permission_mode=permission_mode,
            permission_note=permission_note,
            permission_expires_at=permission_expires_at,
        )
        now = _now()
        handover = GroupAdHandover(
            workflow_type=DIRECT_WORKFLOW_TYPE,
            assessment_id=None,
            group_id=None,
            active_group_key=None,
            source_growth_account_id=None,
            target_ad_only_account_id=values["target"].id,
            creative_id=values["creative"].id,
            invite_link_encrypted=encrypt_ephemeral_secret(invite_link),
            invite_secret_expires_at=now + timedelta(hours=24),
            send_mode=values["send_mode"],
            interval_minutes=values["interval_minutes"],
            scheduled_times=_json_dump(values["scheduled_times"]),
            estimated_daily_sends=values["estimated_daily_sends"],
            permission_mode=values["permission_mode"],
            permission_note=values["permission_note"],
            permission_expires_at=values["permission_expires_at"],
            status="queued",
            current_step="queued",
            idempotency_key=key,
            requested_by_user_id=requested_by_user_id,
            approved_by_user_id=requested_by_user_id,
            approved_at=now,
        )
        self.db.add(handover)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            replay = (
                await self.db.execute(
                    select(GroupAdHandover).where(
                        GroupAdHandover.idempotency_key == key
                    )
                )
            ).scalar_one_or_none()
            if replay is not None:
                return replay, False
            raise AdOnlyWorkflowError("direct_assignment_conflict") from exc
        await self._add_event(
            group_id=None,
            handover_id=handover.id,
            event_type="direct_assignment_queued",
            step="queued",
            status="queued",
            actor_user_id=requested_by_user_id,
            message="Direct Ad-only assignment queued after successful preflight",
            payload={
                "target_account_id": target_account_id,
                "creative_id": creative_id,
                "send_mode": values["send_mode"],
                "interval_minutes": values["interval_minutes"],
                "scheduled_times": values["scheduled_times"],
                "estimated_daily_sends": values["estimated_daily_sends"],
                "existing_daily_sends": values["existing_daily_sends"],
                "hard_cap": values["hard_cap"],
                "invite_kind": values["invite_kind"],
                "permission_mode": values["permission_mode"],
                "permission_expires_at": _iso(values["permission_expires_at"]),
            },
        )
        await self.db.commit()
        await self.db.refresh(handover)
        return handover, True

    async def preflight_handover(
        self,
        *,
        assessment_id: int,
        target_account_id: int,
        creative_id: int,
        invite_link: str,
        send_mode: str,
        interval_minutes: int,
        scheduled_times: list[str] | None,
    ) -> dict[str, Any]:
        now = _now()
        config = await get_ad_only_recommendation_settings(self.db)
        if not config["handover_execution_enabled"]:
            raise AdOnlyWorkflowError("handover_execution_disabled")
        assessment = await self.db.get(GroupAdOnlyAssessment, assessment_id)
        if assessment is None:
            raise AdOnlyWorkflowError("assessment_not_found")
        latest_id = (
            await self.db.execute(
                select(func.max(GroupAdOnlyAssessment.id)).where(
                    GroupAdOnlyAssessment.group_id == assessment.group_id
                )
            )
        ).scalar()
        if int(latest_id or 0) != assessment.id:
            raise AdOnlyWorkflowError("assessment_is_not_latest")
        if assessment.status != "recommended":
            raise AdOnlyWorkflowError("assessment_not_recommended")
        if assessment.valid_until is None or assessment.valid_until <= now:
            raise AdOnlyWorkflowError("assessment_expired")
        decision = await self._latest_decision(assessment.id)
        if decision is None or decision.event_type != "assessment_approved":
            raise AdOnlyWorkflowError("assessment_approval_required")

        group = await self.db.get(Group, assessment.group_id)
        if group is None:
            raise AdOnlyWorkflowError("group_not_found")
        if group.ad_delivery_account_id is not None:
            raise AdOnlyWorkflowError("group_already_handed_over")
        source_membership = (
            await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.group_id == group.id,
                    GroupAccountMembership.account_id
                    == assessment.source_growth_account_id,
                    GroupAccountMembership.status == "joined",
                )
            )
        ).scalar_one_or_none()
        if source_membership is None:
            raise AdOnlyWorkflowError("source_growth_membership_missing")
        growth_ids, ad_only_ids = await self._joined_account_modes(group.id)
        if growth_ids != [assessment.source_growth_account_id]:
            raise AdOnlyWorkflowError(
                "current_growth_membership_changed_since_assessment"
            )
        if ad_only_ids:
            raise AdOnlyWorkflowError(
                "current_ad_only_membership_changed_since_assessment"
            )

        target = await self.db.get(TelegramAccount, target_account_id)
        if target is None:
            raise AdOnlyWorkflowError("target_account_not_found")
        if target.id == assessment.source_growth_account_id:
            raise AdOnlyWorkflowError("target_account_must_differ_from_source")
        if target.account_type != AccountType.PROMOTER:
            raise AdOnlyWorkflowError("target_account_not_promoter")
        if not target.is_active or target.status in {
            AccountStatus.ERROR,
            AccountStatus.BANNED,
        }:
            raise AdOnlyWorkflowError("target_account_unavailable")
        if target.risk_level not in {
            AccountRiskLevel.NORMAL.value,
            AccountRiskLevel.WATCH.value,
        }:
            raise AdOnlyWorkflowError("target_account_risk_blocked")
        operation_config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == target.id
                )
            )
        ).scalar_one_or_none()
        if (
            operation_config is None
            or not operation_config.enabled
            or not operation_config.auto_ads_enabled
            or operation_config.operation_mode != AccountOperationMode.AD_ONLY.value
        ):
            raise AdOnlyWorkflowError("target_account_not_enabled_ad_only")

        creative = await self.db.get(AdCreative, creative_id)
        if creative is None or not creative.enabled:
            raise AdOnlyWorkflowError("creative_not_enabled")
        try:
            parsed_link = parse_telegram_group_link(invite_link)
        except TelegramExecutionError as exc:
            raise AdOnlyWorkflowError(f"invalid_invite_link:{exc}") from exc
        mode, interval, times, estimated = self._validate_schedule(
            send_mode,
            interval_minutes,
            scheduled_times,
        )

        profile = await self._group_profile(group.id)
        if (
            profile is None
            or profile.ad_policy_mode
            not in {
                GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
            }
            or int(profile.ad_policy_confidence or 0) < 90
            or (
                profile.ad_policy_expires_at is not None
                and profile.ad_policy_expires_at <= now
            )
        ):
            raise AdOnlyWorkflowError("ad_only_delivery_permission_required")

        risk_settings = await get_account_risk_guard_settings(self.db)
        hard_cap = await AccountRiskGuard(self.db)._outbound_message_hard_cap(
            target.id, risk_settings
        )
        existing_daily_sends = await self._existing_daily_sends(target.id)
        if existing_daily_sends + estimated > hard_cap:
            raise AdOnlyWorkflowError("target_account_daily_capacity_exceeded")
        return {
            "assessment": assessment,
            "decision": decision,
            "group": group,
            "source_membership": source_membership,
            "target": target,
            "creative": creative,
            "invite_link": invite_link,
            "invite_kind": parsed_link.kind,
            "send_mode": mode,
            "interval_minutes": interval,
            "scheduled_times": times,
            "estimated_daily_sends": estimated,
            "existing_daily_sends": existing_daily_sends,
            "hard_cap": hard_cap,
        }

    async def create_handover(
        self,
        *,
        assessment_id: int,
        target_account_id: int,
        creative_id: int,
        invite_link: str,
        send_mode: str,
        interval_minutes: int,
        scheduled_times: list[str] | None,
        idempotency_key: str,
        requested_by_user_id: int,
    ) -> tuple[GroupAdHandover, bool]:
        key = str(idempotency_key or "").strip()
        if not 8 <= len(key) <= 64:
            raise AdOnlyWorkflowError("invalid_idempotency_key")
        existing = (
            await self.db.execute(
                select(GroupAdHandover).where(
                    GroupAdHandover.idempotency_key == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        values = await self.preflight_handover(
            assessment_id=assessment_id,
            target_account_id=target_account_id,
            creative_id=creative_id,
            invite_link=invite_link,
            send_mode=send_mode,
            interval_minutes=interval_minutes,
            scheduled_times=scheduled_times,
        )
        active = (
            await self.db.execute(
                select(GroupAdHandover).where(
                    GroupAdHandover.active_group_key == values["group"].id
                )
            )
        ).scalar_one_or_none()
        if active is not None:
            raise AdOnlyWorkflowError("active_handover_already_exists")

        now = _now()
        expires_at = min(
            values["assessment"].valid_until,
            now + timedelta(hours=24),
        )
        handover = GroupAdHandover(
            assessment_id=values["assessment"].id,
            group_id=values["group"].id,
            active_group_key=values["group"].id,
            source_growth_account_id=values["assessment"].source_growth_account_id,
            target_ad_only_account_id=values["target"].id,
            creative_id=values["creative"].id,
            invite_link_encrypted=encrypt_ephemeral_secret(invite_link),
            invite_secret_expires_at=expires_at,
            send_mode=values["send_mode"],
            interval_minutes=values["interval_minutes"],
            scheduled_times=_json_dump(values["scheduled_times"]),
            estimated_daily_sends=values["estimated_daily_sends"],
            status="queued",
            current_step="queued",
            idempotency_key=key,
            requested_by_user_id=requested_by_user_id,
            approved_by_user_id=int(values["decision"].actor_user_id or requested_by_user_id),
            approved_at=values["decision"].created_at,
        )
        self.db.add(handover)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            replay = (
                await self.db.execute(
                    select(GroupAdHandover).where(
                        GroupAdHandover.idempotency_key == key
                    )
                )
            ).scalar_one_or_none()
            if replay is not None:
                return replay, False
            raise AdOnlyWorkflowError("active_handover_already_exists") from exc
        await self._add_event(
            group_id=handover.group_id,
            assessment_id=handover.assessment_id,
            handover_id=handover.id,
            event_type="handover_queued",
            step="queued",
            status="queued",
            actor_user_id=requested_by_user_id,
            message="Ad-only handover queued after successful preflight",
            payload={
                "target_account_id": target_account_id,
                "creative_id": creative_id,
                "send_mode": values["send_mode"],
                "interval_minutes": values["interval_minutes"],
                "scheduled_times": values["scheduled_times"],
                "estimated_daily_sends": values["estimated_daily_sends"],
                "existing_daily_sends": values["existing_daily_sends"],
                "hard_cap": values["hard_cap"],
                "invite_kind": values["invite_kind"],
            },
        )
        await self.db.commit()
        await self.db.refresh(handover)
        return handover, True

    @staticmethod
    def handover_payload(handover: GroupAdHandover) -> dict[str, Any]:
        return {
            "id": handover.id,
            "workflow_type": handover.workflow_type or ASSESSMENT_WORKFLOW_TYPE,
            "assessment_id": handover.assessment_id,
            "group_id": handover.group_id,
            "telegram_group_id": handover.group.group_id if handover.group else None,
            "group_title": handover.group.title if handover.group else None,
            "source_growth_account_id": handover.source_growth_account_id,
            "source_growth_account_label": _account_label(
                handover.source_growth_account
            ),
            "target_ad_only_account_id": handover.target_ad_only_account_id,
            "target_ad_only_account_label": _account_label(
                handover.target_ad_only_account
            ),
            "creative_id": handover.creative_id,
            "creative_name": handover.creative.name if handover.creative else None,
            "campaign_id": handover.campaign_id,
            "send_mode": handover.send_mode,
            "interval_minutes": handover.interval_minutes,
            "scheduled_times": _json_load(handover.scheduled_times, []),
            "estimated_daily_sends": handover.estimated_daily_sends,
            "permission_mode": handover.permission_mode,
            "permission_note": handover.permission_note,
            "permission_expires_at": _iso(handover.permission_expires_at),
            "status": handover.status,
            "current_step": handover.current_step,
            "retry_count": handover.retry_count,
            "last_error": handover.last_error,
            "invite_secret_expires_at": _iso(handover.invite_secret_expires_at),
            "started_at": _iso(handover.started_at),
            "completed_at": _iso(handover.completed_at),
            "failed_at": _iso(handover.failed_at),
            "created_at": _iso(handover.created_at),
            "updated_at": _iso(handover.updated_at),
        }

    async def _record_step(
        self,
        handover: GroupAdHandover,
        step: str,
        *,
        status: str = "running",
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        handover.current_step = step
        handover.status = status
        handover.updated_at = _now()
        await self._add_event(
            group_id=handover.group_id,
            assessment_id=handover.assessment_id,
            handover_id=handover.id,
            event_type="handover_step",
            step=step,
            status=status,
            message=message or f"Handover step {step}",
            payload=payload,
        )
        await self.db.commit()

    async def _runtime_handover_values(
        self, handover: GroupAdHandover
    ) -> tuple[Group, TelegramAccount, TelegramAccount, AdCreative]:
        config = await get_ad_only_recommendation_settings(self.db)
        if not config["handover_execution_enabled"]:
            raise AdOnlyWorkflowError("handover_execution_disabled")
        assessment = await self.db.get(
            GroupAdOnlyAssessment, handover.assessment_id
        )
        if assessment is None or assessment.status != "recommended":
            raise AdOnlyWorkflowError("assessment_not_recommended")
        now = _now()
        if assessment.valid_until is None or assessment.valid_until <= now:
            raise AdOnlyWorkflowError("assessment_expired")
        decision = await self._latest_decision(assessment.id)
        if decision is None or decision.event_type != "assessment_approved":
            raise AdOnlyWorkflowError("assessment_approval_required")
        group = await self.db.get(Group, handover.group_id)
        source = await self.db.get(
            TelegramAccount, handover.source_growth_account_id
        )
        target = await self.db.get(
            TelegramAccount, handover.target_ad_only_account_id
        )
        creative = await self.db.get(AdCreative, handover.creative_id)
        if group is None or source is None or target is None or creative is None:
            raise AdOnlyWorkflowError("handover_reference_missing")
        growth_ids, ad_only_ids = await self._joined_account_modes(group.id)
        if growth_ids != [source.id]:
            raise AdOnlyWorkflowError(
                "current_growth_membership_changed_since_assessment"
            )
        unexpected_ad_only_ids = [
            account_id
            for account_id in ad_only_ids
            if account_id != target.id
        ]
        if unexpected_ad_only_ids:
            raise AdOnlyWorkflowError(
                "unexpected_ad_only_membership_present"
            )
        if not creative.enabled:
            raise AdOnlyWorkflowError("creative_not_enabled")
        if (
            not target.is_active
            or target.status in {AccountStatus.ERROR, AccountStatus.BANNED}
            or target.risk_level
            not in {AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value}
        ):
            raise AdOnlyWorkflowError("target_account_unavailable")
        target_config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == target.id
                )
            )
        ).scalar_one_or_none()
        if (
            target_config is None
            or not target_config.enabled
            or not target_config.auto_ads_enabled
            or target_config.operation_mode != AccountOperationMode.AD_ONLY.value
        ):
            raise AdOnlyWorkflowError("target_account_not_enabled_ad_only")
        profile = await self._group_profile(group.id)
        evidence = await self._policy_evidence(group, config, now)
        if (
            evidence["negative"]
            or profile is None
            or profile.ad_policy_mode
            not in {
                GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
            }
            or int(profile.ad_policy_confidence or 0) < 90
            or (
                profile.ad_policy_expires_at is not None
                and profile.ad_policy_expires_at <= now
            )
        ):
            raise AdOnlyWorkflowError(
                "ad_only_delivery_permission_changed"
            )
        risk_settings = await get_account_risk_guard_settings(self.db)
        hard_cap = await AccountRiskGuard(self.db)._outbound_message_hard_cap(
            target.id, risk_settings
        )
        current_daily_sends = await self._existing_daily_sends(
            target.id,
            exclude_campaign_id=handover.campaign_id,
        )
        if current_daily_sends + handover.estimated_daily_sends > hard_cap:
            raise AdOnlyWorkflowError(
                "target_account_daily_capacity_changed"
            )
        return group, source, target, creative

    async def _runtime_direct_values(
        self, handover: GroupAdHandover
    ) -> tuple[TelegramAccount, AdCreative]:
        settings = await get_ad_only_recommendation_settings(self.db)
        if not settings["handover_execution_enabled"]:
            raise AdOnlyWorkflowError("handover_execution_disabled")
        target = await self.db.get(
            TelegramAccount, handover.target_ad_only_account_id
        )
        creative = await self.db.get(AdCreative, handover.creative_id)
        if target is None or creative is None:
            raise AdOnlyWorkflowError("handover_reference_missing")
        if not creative.enabled:
            raise AdOnlyWorkflowError("creative_not_enabled")
        if (
            not target.is_active
            or target.status in {AccountStatus.ERROR, AccountStatus.BANNED}
            or target.risk_level
            not in {AccountRiskLevel.NORMAL.value, AccountRiskLevel.WATCH.value}
        ):
            raise AdOnlyWorkflowError("target_account_unavailable")
        target_config = (
            await self.db.execute(
                select(AccountOperationConfig).where(
                    AccountOperationConfig.account_id == target.id
                )
            )
        ).scalar_one_or_none()
        if (
            target_config is None
            or not target_config.enabled
            or not target_config.auto_ads_enabled
            or target_config.operation_mode != AccountOperationMode.AD_ONLY.value
        ):
            raise AdOnlyWorkflowError("target_account_not_enabled_ad_only")
        now = _now()
        if (
            handover.permission_mode
            not in {
                GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value,
            }
            or not str(handover.permission_note or "").strip()
            or handover.permission_expires_at is None
            or handover.permission_expires_at <= now
        ):
            raise AdOnlyWorkflowError("direct_permission_expired_or_missing")
        risk_settings = await get_account_risk_guard_settings(self.db)
        hard_cap = await AccountRiskGuard(self.db)._outbound_message_hard_cap(
            target.id, risk_settings
        )
        current_daily_sends = await self._existing_daily_sends(
            target.id, exclude_campaign_id=handover.campaign_id
        )
        if current_daily_sends + handover.estimated_daily_sends > hard_cap:
            raise AdOnlyWorkflowError("target_account_daily_capacity_changed")
        return target, creative

    @staticmethod
    def _telegram_group_matches(expected: int, actual: int) -> bool:
        expected_abs = abs(int(expected))
        actual_abs = abs(int(actual))
        values = {expected_abs, actual_abs}
        normalized = {
            value - 1_000_000_000_000 if value > 1_000_000_000_000 else value
            for value in values
        }
        return len(normalized) == 1

    async def _joined_membership(
        self, group_id: int, account_id: int
    ) -> GroupAccountMembership | None:
        return (
            await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.group_id == group_id,
                    GroupAccountMembership.account_id == account_id,
                    GroupAccountMembership.status == "joined",
                )
            )
        ).scalar_one_or_none()

    async def _confirm_direct_permission(
        self,
        handover: GroupAdHandover,
        group: Group,
    ) -> GroupAdProfile:
        profile = await self._group_profile(group.id)
        profile_existed = profile is not None
        if profile is None:
            profile = GroupAdProfile(
                group_id=group.id,
                telegram_group_id=group.group_id,
            )
            self.db.add(profile)
            await self.db.flush()
        if handover.permission_previous_json is None:
            handover.permission_previous_json = _json_dump(
                {
                    "existed": profile_existed,
                    "mode": profile.ad_policy_mode,
                    "confidence": profile.ad_policy_confidence,
                    "source": profile.ad_policy_source,
                    "verified_at": _iso(profile.ad_policy_verified_at),
                    "expires_at": _iso(profile.ad_policy_expires_at),
                    "evidence_hash": profile.ad_policy_evidence_hash,
                    "tier": profile.ad_tier,
                    "daily_capacity": profile.daily_capacity,
                    "blocked_at": _iso(profile.blocked_at),
                    "blocked_reason": profile.blocked_reason,
                }
            )
        previous_mode = profile.ad_policy_mode
        evidence = {
            "handover_id": handover.id,
            "confirmed_by_user_id": handover.approved_by_user_id,
            "note": handover.permission_note,
            "expires_at": _iso(handover.permission_expires_at),
        }
        evidence_hash = hashlib.sha256(
            _json_dump(evidence).encode("utf-8")
        ).hexdigest()
        already_confirmed = (
            profile.ad_policy_source == "manual_ad_only_direct"
            and profile.ad_policy_evidence_hash == evidence_hash
        )
        now = _now()
        profile.telegram_group_id = group.group_id
        profile.ad_policy_mode = str(handover.permission_mode)
        profile.ad_policy_confidence = 100
        profile.ad_policy_source = "manual_ad_only_direct"
        profile.ad_policy_verified_at = now
        profile.ad_policy_expires_at = handover.permission_expires_at
        profile.ad_policy_evidence_hash = evidence_hash
        profile.ad_tier = (
            GroupAdTier.HIGH.value
            if handover.permission_mode
            == GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value
            else GroupAdTier.STABLE.value
        )
        profile.daily_capacity = max(
            int(profile.daily_capacity or 0),
            int(handover.estimated_daily_sends or 1),
        )
        profile.blocked_at = None
        profile.blocked_reason = None
        profile.updated_at = now
        if not already_confirmed:
            self.db.add(
                GroupAdPolicyEvent(
                    group_id=group.id,
                    account_id=handover.target_ad_only_account_id,
                    telegram_group_id=group.group_id,
                    previous_mode=previous_mode,
                    new_mode=str(handover.permission_mode),
                    confidence=100,
                    source="manual_ad_only_direct",
                    reason="admin_confirmed_direct_ad_only_permission",
                    evidence=_json_dump(evidence),
                    changed_by_user_id=handover.approved_by_user_id,
                )
            )
        await self.db.commit()
        return profile

    async def _join_direct_target(
        self,
        handover: GroupAdHandover,
        target: TelegramAccount,
    ) -> Group:
        if handover.group_id is not None:
            group = await self.db.get(Group, handover.group_id)
            if group is None:
                raise AdOnlyWorkflowError("group_not_found")
            await self._join_target(handover, group, target)
            await self._claim_direct_group(handover, group, target)
            await self._confirm_direct_permission(handover, group)
            return group

        if (
            handover.invite_secret_expires_at is None
            or handover.invite_secret_expires_at <= _now()
        ):
            handover.invite_link_encrypted = None
            await self.db.commit()
            raise AdOnlyWorkflowError("invite_secret_expired")
        try:
            invite_link = decrypt_ephemeral_secret(
                handover.invite_link_encrypted
            )
        except EphemeralSecretError as exc:
            raise AdOnlyWorkflowError("invite_secret_unavailable") from exc
        if not invite_link:
            raise AdOnlyWorkflowError("invite_secret_unavailable")

        pool = get_account_pool()
        await pool.add_account_from_db(target)
        wrapper = await pool.acquire_by_id(
            target.id, purpose="ad_only_direct_join"
        )
        if wrapper is None:
            raise AdOnlyWorkflowError("target_account_session_unavailable")
        try:
            resolved = await TelegramExecutionService(
                AccountRiskGuard(self.db)
            ).join_group_by_link(
                wrapper,
                invite_link,
                source="ad_only_direct_assignment",
            )
        finally:
            await pool.release(wrapper)

        telegram_group_id = int(resolved.get("id") or 0)
        if not telegram_group_id:
            raise AdOnlyWorkflowError("direct_join_group_identity_missing")
        title = str(resolved.get("title") or "").strip() or None
        username = str(resolved.get("username") or "").strip().lstrip("@") or None
        member_count = max(0, int(resolved.get("participants_count") or 0))
        now = _now()
        group = (
            await self.db.execute(
                select(Group)
                .where(Group.group_id == telegram_group_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if group is None:
            group = Group(
                group_id=telegram_group_id,
                title=title,
                username=username,
                member_count=member_count,
                status="active",
                discovery_source="manual_link_join",
                level=GroupLevel.A,
            )
            self.db.add(group)
            await self.db.flush()
        else:
            if title:
                group.title = title
            if username:
                group.username = username
            if member_count:
                group.member_count = member_count
            group.status = "active"
            group.updated_at = now
        handover.group_id = group.id

        membership = (
            await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.group_id == group.id,
                    GroupAccountMembership.account_id == target.id,
                )
            )
        ).scalar_one_or_none()
        if handover.membership_previous_json is None:
            handover.membership_previous_json = _json_dump(
                {
                    "existed": membership is not None,
                    "status": membership.status if membership else None,
                    "join_method": membership.join_method if membership else None,
                    "joined_at": _iso(membership.joined_at) if membership else None,
                    "left_at": _iso(membership.left_at) if membership else None,
                    "warmup_status": membership.warmup_status if membership else None,
                    "probe_status": membership.probe_status if membership else None,
                    "ad_status": membership.ad_status if membership else None,
                }
            )
        if membership is None:
            membership = GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=telegram_group_id,
                account_id=target.id,
            )
            self.db.add(membership)
        membership.telegram_group_id = telegram_group_id
        membership.status = "joined"
        membership.join_method = "manual_link_join"
        membership.joined_at = membership.joined_at or now
        membership.left_at = None
        membership.last_checked_at = now
        membership.warmup_status = "writable_verified"
        membership.probe_status = "not_started"
        membership.ad_status = "warming"
        membership.updated_at = now
        await self.db.commit()

        await self._claim_direct_group(handover, group, target)
        await self._confirm_direct_permission(handover, group)
        return group

    async def _claim_direct_group(
        self,
        handover: GroupAdHandover,
        group: Group,
        target: TelegramAccount,
    ) -> None:
        if group.ad_delivery_account_id not in {None, target.id}:
            raise AdOnlyWorkflowError("group_already_handed_over")
        competing = (
            await self.db.execute(
                select(GroupAdHandover).where(
                    GroupAdHandover.id != handover.id,
                    GroupAdHandover.active_group_key == group.id,
                )
            )
        ).scalar_one_or_none()
        if competing is not None:
            raise AdOnlyWorkflowError("active_handover_already_exists")
        handover.group_id = group.id
        handover.active_group_key = group.id
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise AdOnlyWorkflowError("active_handover_already_exists") from exc

    async def _join_target(
        self,
        handover: GroupAdHandover,
        group: Group,
        target: TelegramAccount,
    ) -> GroupAccountMembership:
        existing = await self._joined_membership(group.id, target.id)
        if existing is not None:
            return existing
        if (
            handover.invite_secret_expires_at is None
            or handover.invite_secret_expires_at <= _now()
        ):
            handover.invite_link_encrypted = None
            await self.db.commit()
            raise AdOnlyWorkflowError("invite_secret_expired")
        try:
            invite_link = decrypt_ephemeral_secret(
                handover.invite_link_encrypted
            )
        except EphemeralSecretError as exc:
            raise AdOnlyWorkflowError("invite_secret_unavailable") from exc
        if not invite_link:
            raise AdOnlyWorkflowError("invite_secret_unavailable")

        pool = get_account_pool()
        await pool.add_account_from_db(target)
        wrapper = await pool.acquire_by_id(
            target.id, purpose="ad_only_handover_join"
        )
        if wrapper is None:
            raise AdOnlyWorkflowError("target_account_session_unavailable")
        try:
            resolved = await TelegramExecutionService(
                AccountRiskGuard(self.db)
            ).join_group_by_link(
                wrapper,
                invite_link,
                source="ad_only_handover",
            )
            resolved_id = int(resolved.get("id") or 0)
            if not resolved_id or not self._telegram_group_matches(
                group.group_id, resolved_id
            ):
                try:
                    await TelegramExecutionService(
                        AccountRiskGuard(self.db)
                    ).leave_group_by_id(
                        wrapper,
                        resolved_id,
                        source="ad_only_handover_mismatch",
                    )
                except Exception:
                    pass
                raise AdOnlyWorkflowError("invite_link_resolved_to_wrong_group")
        finally:
            await pool.release(wrapper)

        membership = (
            await self.db.execute(
                select(GroupAccountMembership).where(
                    GroupAccountMembership.group_id == group.id,
                    GroupAccountMembership.account_id == target.id,
                )
            )
        ).scalar_one_or_none()
        now = _now()
        if membership is None:
            membership = GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=target.id,
            )
            self.db.add(membership)
        membership.status = "joined"
        membership.join_method = "manual_link_join"
        membership.joined_at = membership.joined_at or now
        membership.left_at = None
        membership.last_checked_at = now
        membership.warmup_status = "writable_verified"
        membership.probe_status = "not_started"
        membership.ad_status = "warming"
        membership.updated_at = now
        await self.db.commit()
        return membership

    async def _ensure_campaign(
        self,
        handover: GroupAdHandover,
        group: Group,
    ) -> AdCampaign:
        campaign = (
            await self.db.get(AdCampaign, handover.campaign_id)
            if handover.campaign_id is not None
            else None
        )
        if campaign is None:
            campaign = AdCampaign(
                name=f"Ad-only handover {handover.id} group {group.id}",
                enabled=False,
                status="draft",
                delivery_policy=AdDeliveryPolicy.AD_ONLY.value,
                send_mode=handover.send_mode,
                target_group_ids=_json_dump([group.id]),
                target_group_levels=_json_dump([]),
                interval_minutes=handover.interval_minutes,
                scheduled_times=handover.scheduled_times,
                max_sends_per_group_per_day=max(
                    1, handover.estimated_daily_sends
                ),
                max_sends_per_account_per_day=max(
                    1, handover.estimated_daily_sends
                ),
            )
            self.db.add(campaign)
            await self.db.flush()
            handover.campaign_id = campaign.id
            await self.db.commit()
        elif campaign.delivery_policy != AdDeliveryPolicy.AD_ONLY.value:
            raise AdOnlyWorkflowError("handover_campaign_policy_mismatch")
        return campaign

    async def _ensure_binding(
        self,
        handover: GroupAdHandover,
        campaign: AdCampaign,
    ) -> AccountAdBinding:
        binding = (
            await self.db.execute(
                select(AccountAdBinding).where(
                    AccountAdBinding.account_id
                    == handover.target_ad_only_account_id,
                    AccountAdBinding.ad_campaign_id == campaign.id,
                    AccountAdBinding.creative_id == handover.creative_id,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            binding = AccountAdBinding(
                account_id=handover.target_ad_only_account_id,
                ad_campaign_id=campaign.id,
                creative_id=handover.creative_id,
                enabled=True,
                priority=100,
            )
            self.db.add(binding)
        else:
            binding.enabled = True
        await self.db.commit()
        return binding

    async def _next_due_at(self, handover: GroupAdHandover) -> datetime:
        now = _now()
        if handover.send_mode != AdSendMode.SCHEDULED.value:
            return now + timedelta(minutes=max(1, handover.interval_minutes))
        capacity = await get_ad_capacity_settings(self.db)
        offset = timedelta(
            hours=int(capacity.get("timezone_offset_hours", 8))
        )
        local_now = now + offset
        candidates: list[datetime] = []
        for value in _json_load(handover.scheduled_times, []):
            hour, minute = (int(item) for item in str(value).split(":", 1))
            for day_delta in (0, 1, 2):
                candidate = (local_now + timedelta(days=day_delta)).replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0,
                )
                if candidate > local_now:
                    candidates.append(candidate - offset)
        if not candidates:
            raise AdOnlyWorkflowError("schedule_has_no_future_slot")
        return min(candidates)

    async def _ensure_schedule(
        self,
        handover: GroupAdHandover,
        group: Group,
        campaign: AdCampaign,
    ) -> AdDeliveryScheduleState:
        state = (
            await self.db.execute(
                select(AdDeliveryScheduleState).where(
                    AdDeliveryScheduleState.campaign_id == campaign.id,
                    AdDeliveryScheduleState.account_id
                    == handover.target_ad_only_account_id,
                    AdDeliveryScheduleState.group_id == group.id,
                )
            )
        ).scalar_one_or_none()
        next_due_at = await self._next_due_at(handover)
        if state is None:
            state = AdDeliveryScheduleState(
                campaign_id=campaign.id,
                account_id=handover.target_ad_only_account_id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                next_due_at=next_due_at,
                status=AdScheduleStatus.IDLE.value,
            )
            self.db.add(state)
        else:
            state.next_due_at = next_due_at
            state.status = AdScheduleStatus.IDLE.value
            state.lock_token = None
            state.lease_expires_at = None
            state.last_reason = "ad_only_handover_verified"
        await self.db.commit()
        return state

    async def _verify_and_assign_takeover(
        self,
        handover: GroupAdHandover,
        group: Group,
        target: TelegramAccount,
        campaign: AdCampaign,
        binding: AccountAdBinding,
        schedule: AdDeliveryScheduleState,
    ) -> None:
        if (
            not campaign.enabled
            or not binding.enabled
            or schedule.status
            not in {AdScheduleStatus.IDLE.value, AdScheduleStatus.RETRY.value}
            or campaign.get_target_group_ids() != [group.id]
        ):
            raise AdOnlyWorkflowError("takeover_database_verification_failed")
        membership = await self._joined_membership(group.id, target.id)
        if membership is None or membership.join_method != "manual_link_join":
            raise AdOnlyWorkflowError("target_membership_verification_failed")

        pool = get_account_pool()
        await pool.add_account_from_db(target)
        wrapper = await pool.acquire_by_id(
            target.id, purpose="ad_only_handover_verify"
        )
        if wrapper is None or wrapper.client is None:
            raise AdOnlyWorkflowError("target_account_session_unavailable")
        try:
            entity = await wrapper.client.get_entity(
                group.username or group.group_id
            )
            entity_id = int(getattr(entity, "id", 0) or 0)
            if not entity_id or not self._telegram_group_matches(
                group.group_id, entity_id
            ):
                raise AdOnlyWorkflowError(
                    "target_membership_resolved_to_wrong_group"
                )
        finally:
            await pool.release(wrapper)

        now = _now()
        if (
            group.ad_delivery_account_id is not None
            and group.ad_delivery_account_id != target.id
        ):
            raise AdOnlyWorkflowError("group_already_handed_over")
        group.ad_delivery_account_id = target.id
        membership.warmup_status = "ad_eligible"
        membership.probe_status = "success"
        membership.ad_status = "active"
        membership.ad_eligible_after = now
        membership.first_ad_allowed_at = membership.first_ad_allowed_at or now
        membership.last_checked_at = now
        membership.updated_at = now
        await self.db.commit()

        assigned = await self.db.get(Group, group.id)
        if assigned is None or assigned.ad_delivery_account_id != target.id:
            raise AdOnlyWorkflowError("takeover_assignment_verification_failed")

    async def _leave_source_growth(
        self,
        handover: GroupAdHandover,
        group: Group,
        source: TelegramAccount,
    ) -> str | None:
        membership = await self._joined_membership(group.id, source.id)
        if membership is None:
            return None
        pool = get_account_pool()
        await pool.add_account_from_db(source)
        wrapper = await pool.acquire_by_id(
            source.id, purpose="ad_only_handover_growth_leave"
        )
        if wrapper is None:
            return "source_account_session_unavailable"
        try:
            await TelegramExecutionService(
                AccountRiskGuard(self.db)
            ).leave_group_by_id(
                wrapper,
                group.group_id,
                source="ad_only_handover_complete",
            )
        except Exception as exc:
            return _safe_error(exc)
        finally:
            await pool.release(wrapper)
        now = _now()
        membership.status = "left"
        membership.left_at = now
        membership.last_checked_at = now
        membership.warmup_status = "blocked"
        membership.probe_status = "skipped"
        membership.ad_status = "blocked"
        membership.updated_at = now
        await self.db.commit()
        return None

    async def _mark_cleanup_pending(
        self,
        handover: GroupAdHandover,
        error: str,
    ) -> dict[str, Any]:
        handover.status = "cleanup_pending"
        handover.current_step = "growth_leave"
        handover.last_error = error
        handover.retry_count = int(handover.retry_count or 0) + 1
        handover.updated_at = _now()
        await self._add_event(
            group_id=handover.group_id,
            assessment_id=handover.assessment_id,
            handover_id=handover.id,
            event_type="handover_cleanup_pending",
            step="growth_leave",
            status="cleanup_pending",
            message="Target takeover succeeded; Growth leave needs retry",
            payload={"error": error},
        )
        await self.db.commit()
        return {
            "status": "cleanup_pending",
            "handover": self.handover_payload(handover),
        }

    async def _mark_completed(
        self,
        handover: GroupAdHandover,
    ) -> dict[str, Any]:
        completed_at = _now()
        handover.status = "completed"
        handover.current_step = "completed"
        handover.completed_at = completed_at
        handover.active_group_key = None
        handover.invite_link_encrypted = None
        handover.invite_secret_expires_at = None
        handover.last_error = None
        handover.updated_at = completed_at
        await self._add_event(
            group_id=handover.group_id,
            assessment_id=handover.assessment_id,
            handover_id=handover.id,
            event_type="handover_completed",
            step="completed",
            status="completed",
            message=(
                "Direct Ad-only assignment verified"
                if handover.workflow_type == DIRECT_WORKFLOW_TYPE
                else "Ad-only takeover verified and Growth account retired"
            ),
        )
        await self.db.commit()
        return {
            "status": "completed",
            "handover": self.handover_payload(handover),
        }

    async def execute_handover(self, handover_id: int) -> dict[str, Any]:
        handover = (
            await self.db.execute(
                select(GroupAdHandover)
                .where(GroupAdHandover.id == handover_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if handover is None:
            raise AdOnlyWorkflowError("handover_not_found")
        if handover.status in TERMINAL_HANDOVER_STATUSES:
            return {
                "status": "skipped",
                "reason": "handover_already_terminal",
                "handover": self.handover_payload(handover),
            }
        if handover.status == "running":
            return {
                "status": "skipped",
                "reason": "handover_already_running",
                "handover": self.handover_payload(handover),
            }
        if handover.status == "rollback_pending":
            return {
                "status": "skipped",
                "reason": "handover_requires_rollback_retry",
                "handover": self.handover_payload(handover),
            }

        is_direct = handover.workflow_type == DIRECT_WORKFLOW_TYPE
        cleanup_only = not is_direct and (
            handover.status == "cleanup_pending"
            or handover.current_step == "growth_leave"
        )
        now = _now()
        handover.status = "running"
        handover.started_at = handover.started_at or now
        handover.failed_at = None
        handover.last_error = None
        await self.db.commit()
        try:
            if cleanup_only:
                group = await self.db.get(Group, handover.group_id)
                source = await self.db.get(
                    TelegramAccount, handover.source_growth_account_id
                )
                if (
                    group is None
                    or source is None
                    or group.ad_delivery_account_id
                    != handover.target_ad_only_account_id
                ):
                    raise AdOnlyWorkflowError(
                        "cleanup_takeover_state_invalid"
                    )
                await self._record_step(
                    handover,
                    "growth_leave",
                    message="Retrying source Growth account retirement",
                )
                leave_error = await self._leave_source_growth(
                    handover, group, source
                )
                if leave_error is not None:
                    return await self._mark_cleanup_pending(
                        handover, leave_error
                    )
                return await self._mark_completed(handover)

            await self._record_step(
                handover,
                "preflight",
                message=(
                    "Runtime direct assignment preflight started"
                    if is_direct
                    else "Runtime handover preflight started"
                ),
            )
            source: TelegramAccount | None = None
            if is_direct:
                target, _creative = await self._runtime_direct_values(handover)
            else:
                group, source, target, _creative = (
                    await self._runtime_handover_values(handover)
                )

            await self._record_step(
                handover,
                "target_join",
                message="Joining target ad-only account",
            )
            if is_direct:
                group = await self._join_direct_target(handover, target)
            else:
                await self._join_target(handover, group, target)

            await self._record_step(
                handover,
                "campaign_created_disabled",
                message="Creating disabled ad-only campaign",
            )
            campaign = await self._ensure_campaign(handover, group)
            campaign.enabled = False
            campaign.status = "draft"
            await self.db.commit()

            await self._record_step(
                handover,
                "binding_created",
                message="Creating target account campaign binding",
            )
            binding = await self._ensure_binding(handover, campaign)

            await self._record_step(
                handover,
                "schedule_verified",
                message="Creating and verifying durable delivery schedule",
            )
            schedule = await self._ensure_schedule(
                handover, group, campaign
            )

            await self._record_step(
                handover,
                "campaign_enabled",
                message="Enabling verified ad-only campaign",
            )
            campaign.enabled = True
            campaign.status = "active"
            await self.db.commit()

            await self._record_step(
                handover,
                "takeover_verified",
                message="Verifying target membership and assigning ownership",
            )
            await self._verify_and_assign_takeover(
                handover,
                group,
                target,
                campaign,
                binding,
                schedule,
            )

            if source is not None:
                await self._record_step(
                    handover,
                    "growth_leave",
                    message="Retiring source Growth account from group",
                )
                leave_error = await self._leave_source_growth(
                    handover, group, source
                )
                if leave_error is not None:
                    return await self._mark_cleanup_pending(
                        handover, leave_error
                    )
            return await self._mark_completed(handover)
        except Exception as exc:
            await self.db.rollback()
            failed = await self.db.get(GroupAdHandover, handover_id)
            if failed is None:
                raise
            error = _safe_error(exc)
            failed.status = "failed"
            failed.failed_at = _now()
            failed.last_error = error
            failed.retry_count = int(failed.retry_count or 0) + 1
            failed.updated_at = _now()
            if (
                failed.invite_secret_expires_at is not None
                and failed.invite_secret_expires_at <= _now()
            ):
                failed.invite_link_encrypted = None
                failed.invite_secret_expires_at = None
            await self._add_event(
                group_id=failed.group_id,
                assessment_id=failed.assessment_id,
                handover_id=failed.id,
                event_type="handover_failed",
                step=failed.current_step,
                status="failed",
                message="Handover step failed and can be retried or rolled back",
                payload={"error": error},
            )
            await self.db.commit()
            return {
                "status": "failed",
                "error": error,
                "handover": self.handover_payload(failed),
            }

    async def prepare_retry(
        self,
        handover_id: int,
        *,
        actor_user_id: int,
    ) -> GroupAdHandover:
        handover = await self.db.get(GroupAdHandover, handover_id)
        if handover is None:
            raise AdOnlyWorkflowError("handover_not_found")
        if handover.status not in {"failed", "cleanup_pending"}:
            raise AdOnlyWorkflowError("handover_not_retryable")
        handover.status = "queued"
        handover.failed_at = None
        handover.last_error = None
        handover.updated_at = _now()
        await self._add_event(
            group_id=handover.group_id,
            assessment_id=handover.assessment_id,
            handover_id=handover.id,
            event_type="handover_retry_queued",
            step=handover.current_step,
            status="queued",
            actor_user_id=actor_user_id,
            message="Admin queued handover retry",
        )
        await self.db.commit()
        await self.db.refresh(handover)
        return handover

    async def _restore_direct_permission(
        self,
        handover: GroupAdHandover,
        group: Group,
    ) -> None:
        previous = _json_load(handover.permission_previous_json, {})
        if not previous:
            return
        profile = await self._group_profile(group.id)
        if (
            profile is None
            or profile.ad_policy_source != "manual_ad_only_direct"
        ):
            return
        current_mode = profile.ad_policy_mode

        def parse_datetime(value: Any) -> datetime | None:
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return None

        if previous.get("existed"):
            profile.ad_policy_mode = str(
                previous.get("mode") or GroupAdPolicyMode.UNKNOWN.value
            )
            profile.ad_policy_confidence = int(previous.get("confidence") or 0)
            profile.ad_policy_source = previous.get("source")
            profile.ad_policy_verified_at = parse_datetime(
                previous.get("verified_at")
            )
            profile.ad_policy_expires_at = parse_datetime(
                previous.get("expires_at")
            )
            profile.ad_policy_evidence_hash = previous.get("evidence_hash")
            profile.ad_tier = str(
                previous.get("tier") or GroupAdTier.OBSERVING.value
            )
            profile.daily_capacity = int(previous.get("daily_capacity") or 0)
            profile.blocked_at = parse_datetime(previous.get("blocked_at"))
            profile.blocked_reason = previous.get("blocked_reason")
            profile.updated_at = _now()
            restored_mode = profile.ad_policy_mode
        else:
            restored_mode = GroupAdPolicyMode.UNKNOWN.value
            await self.db.delete(profile)
        self.db.add(
            GroupAdPolicyEvent(
                group_id=group.id,
                account_id=handover.target_ad_only_account_id,
                telegram_group_id=group.group_id,
                previous_mode=current_mode,
                new_mode=restored_mode,
                confidence=int(previous.get("confidence") or 0),
                source="manual_ad_only_direct_rollback",
                reason="direct_assignment_rolled_back",
                changed_by_user_id=handover.requested_by_user_id,
            )
        )

    async def rollback_handover(self, handover_id: int) -> dict[str, Any]:
        handover = (
            await self.db.execute(
                select(GroupAdHandover)
                .where(GroupAdHandover.id == handover_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if handover is None:
            raise AdOnlyWorkflowError("handover_not_found")
        if handover.status == "rolled_back":
            return {
                "status": "rolled_back",
                "handover": self.handover_payload(handover),
            }
        if handover.status in {"completed", "cancelled"}:
            raise AdOnlyWorkflowError("completed_handover_cannot_be_rolled_back")

        is_direct = handover.workflow_type == DIRECT_WORKFLOW_TYPE
        if is_direct and handover.group_id is None:
            completed_at = _now()
            handover.status = "rolled_back"
            handover.current_step = "rolled_back"
            handover.active_group_key = None
            handover.invite_link_encrypted = None
            handover.invite_secret_expires_at = None
            handover.completed_at = completed_at
            handover.last_error = None
            handover.updated_at = completed_at
            await self._add_event(
                group_id=None,
                handover_id=handover.id,
                event_type="direct_assignment_rolled_back",
                step="rolled_back",
                status="rolled_back",
                message="Direct assignment cancelled before Telegram group resolution",
            )
            await self.db.commit()
            return {
                "status": "rolled_back",
                "handover": self.handover_payload(handover),
            }

        group = await self.db.get(Group, handover.group_id)
        target = await self.db.get(
            TelegramAccount, handover.target_ad_only_account_id
        )
        source_membership = (
            None
            if is_direct
            else await self._joined_membership(
                handover.group_id, handover.source_growth_account_id
            )
        )
        if group is None or target is None:
            raise AdOnlyWorkflowError("handover_reference_missing")
        if not is_direct and source_membership is None:
            raise AdOnlyWorkflowError("source_growth_not_joined_for_rollback")

        handover.status = "rollback_pending"
        handover.current_step = "rollback"
        handover.updated_at = _now()
        campaign = (
            await self.db.get(AdCampaign, handover.campaign_id)
            if handover.campaign_id
            else None
        )
        if campaign is not None:
            campaign.enabled = False
            campaign.status = "cancelled"
            bindings = await self.db.execute(
                select(AccountAdBinding).where(
                    AccountAdBinding.ad_campaign_id == campaign.id,
                    AccountAdBinding.account_id == target.id,
                )
            )
            for binding in bindings.scalars().all():
                binding.enabled = False
            schedules = await self.db.execute(
                select(AdDeliveryScheduleState).where(
                    AdDeliveryScheduleState.campaign_id == campaign.id,
                    AdDeliveryScheduleState.account_id == target.id,
                    AdDeliveryScheduleState.group_id == group.id,
                )
            )
            for schedule in schedules.scalars().all():
                schedule.status = AdScheduleStatus.PAUSED.value
                schedule.lock_token = None
                schedule.lease_expires_at = None
                schedule.last_reason = "ad_only_handover_rollback"
        if group.ad_delivery_account_id == target.id:
            group.ad_delivery_account_id = None
        if is_direct:
            await self._restore_direct_permission(handover, group)
        await self.db.commit()

        target_membership = await self._joined_membership(group.id, target.id)
        membership_previous = _json_load(
            handover.membership_previous_json, {}
        )
        preserve_joined_membership = bool(
            is_direct
            and membership_previous.get("existed")
            and membership_previous.get("status") == "joined"
        )
        leave_error: str | None = None
        if target_membership is not None and not preserve_joined_membership:
            pool = get_account_pool()
            await pool.add_account_from_db(target)
            wrapper = await pool.acquire_by_id(
                target.id, purpose="ad_only_handover_rollback"
            )
            if wrapper is None:
                leave_error = "target_account_session_unavailable"
            else:
                try:
                    await TelegramExecutionService(
                        AccountRiskGuard(self.db)
                    ).leave_group_by_id(
                        wrapper,
                        group.group_id,
                        source="ad_only_handover_rollback",
                    )
                except Exception as exc:
                    leave_error = _safe_error(exc)
                finally:
                    await pool.release(wrapper)
        if leave_error is not None:
            handover.status = "rollback_pending"
            handover.last_error = leave_error
            handover.retry_count = int(handover.retry_count or 0) + 1
            await self._add_event(
                group_id=handover.group_id,
                assessment_id=handover.assessment_id,
                handover_id=handover.id,
                event_type="handover_rollback_pending",
                step="rollback",
                status="rollback_pending",
                message="Database takeover disabled; target leave needs retry",
                payload={"error": leave_error},
            )
            await self.db.commit()
            return {
                "status": "rollback_pending",
                "handover": self.handover_payload(handover),
            }

        if target_membership is not None and preserve_joined_membership:
            target_membership.status = "joined"
            target_membership.join_method = membership_previous.get("join_method")
            target_membership.joined_at = (
                datetime.fromisoformat(membership_previous["joined_at"])
                if membership_previous.get("joined_at")
                else target_membership.joined_at
            )
            target_membership.left_at = None
            target_membership.warmup_status = membership_previous.get(
                "warmup_status"
            ) or target_membership.warmup_status
            target_membership.probe_status = membership_previous.get(
                "probe_status"
            ) or target_membership.probe_status
            target_membership.ad_status = membership_previous.get(
                "ad_status"
            ) or target_membership.ad_status
            target_membership.updated_at = _now()
        elif target_membership is not None:
            now = _now()
            target_membership.status = "left"
            target_membership.left_at = now
            target_membership.last_checked_at = now
            target_membership.warmup_status = "blocked"
            target_membership.probe_status = "skipped"
            target_membership.ad_status = "blocked"
            target_membership.updated_at = now
        completed_at = _now()
        handover.status = "rolled_back"
        handover.current_step = "rolled_back"
        handover.active_group_key = None
        handover.invite_link_encrypted = None
        handover.invite_secret_expires_at = None
        handover.completed_at = completed_at
        handover.last_error = None
        handover.updated_at = completed_at
        await self._add_event(
            group_id=handover.group_id,
            assessment_id=handover.assessment_id,
            handover_id=handover.id,
            event_type="handover_rolled_back",
            step="rolled_back",
            status="rolled_back",
            message=(
                "Direct Ad-only assignment rolled back"
                if is_direct
                else "Ad-only handover rolled back; Growth membership retained"
            ),
        )
        await self.db.commit()
        return {
            "status": "rolled_back",
            "handover": self.handover_payload(handover),
        }

    async def list_handovers(
        self,
        *,
        group_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = select(GroupAdHandover).order_by(
            desc(GroupAdHandover.created_at),
            desc(GroupAdHandover.id),
        )
        if group_id is not None:
            query = query.where(GroupAdHandover.group_id == group_id)
        if status:
            query = query.where(GroupAdHandover.status == status)
        query = query.limit(max(1, min(int(limit), 500)))
        rows = await self.db.execute(query)
        return [
            self.handover_payload(handover)
            for handover in rows.scalars().unique().all()
        ]

    async def clear_expired_invite_secrets(self) -> int:
        now = _now()
        rows = await self.db.execute(
            select(GroupAdHandover).where(
                GroupAdHandover.invite_link_encrypted.is_not(None),
                GroupAdHandover.invite_secret_expires_at.is_not(None),
                GroupAdHandover.invite_secret_expires_at <= now,
            )
        )
        handovers = list(rows.scalars().all())
        for handover in handovers:
            handover.invite_link_encrypted = None
            handover.invite_secret_expires_at = None
            await self._add_event(
                group_id=handover.group_id,
                assessment_id=handover.assessment_id,
                handover_id=handover.id,
                event_type="invite_secret_expired",
                step=handover.current_step,
                status=handover.status,
                message="Expired handover invite secret cleared",
            )
        if handovers:
            await self.db.commit()
        return len(handovers)


async def evaluate_ad_only_candidates_with_db(
    *,
    limit: int = 200,
    force: bool = False,
) -> dict[str, Any]:
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    async with db_module.get_db_session() as db:
        service = AdOnlyRecommendationService(db)
        expired_secrets = await service.clear_expired_invite_secrets()
        result = await service.evaluate_candidates(limit=limit, force=force)
        result["expired_invite_secrets_cleared"] = expired_secrets
        return result


async def execute_ad_only_handover_with_db(
    handover_id: int,
) -> dict[str, Any]:
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    async with db_module.get_db_session() as db:
        return await AdOnlyRecommendationService(db).execute_handover(
            handover_id
        )


async def rollback_ad_only_handover_with_db(
    handover_id: int,
) -> dict[str, Any]:
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    async with db_module.get_db_session() as db:
        return await AdOnlyRecommendationService(db).rollback_handover(
            handover_id
        )
