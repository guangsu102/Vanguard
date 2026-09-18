"""Durable generation and exact-account delivery for owned-group messages."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import TelegramAccount
from app.core.account.pool import AccountPool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.automation_settings import (
    get_group_ai_interaction_settings,
    get_owned_group_messaging_settings,
)
from app.core.config import settings
from app.core.group.manager import GroupManager
from app.core.operating_time import operating_day_start
from app.core.telegram_chat_lock import (
    telegram_account_advisory_lock,
    telegram_chat_advisory_lock,
)
from app.modules.acquisition.auto_reply.speaker import Speaker, SpeakResult
from app.modules.acquisition.auto_reply.templates import TemplateEngine
from app.modules.acquisition.models import (
    AcquisitionMessage,
    KeywordTrigger,
    MessageType,
    TriggerAction,
)
from app.modules.owned_group.lock_queries import message_execution_for_update_query
from app.modules.owned_group.messaging_content_service import (
    OwnedGroupMessageContentService,
)
from app.modules.owned_group.messaging_contracts import (
    RESERVING_EXECUTION_STATUSES,
    MessageContentCategory,
    MessageExecutionStatus,
    MessageMode,
    OwnedGroupMessagingError,
)
from app.modules.owned_group.messaging_governance_integration import (
    govern_before_send,
)
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.messaging_prompt_context import (
    OwnedGroupPromptContextStore,
    requires_ephemeral_prompt_context,
    summarize_prompt_context,
    summary_requires_ephemeral_prompt_context,
)
from app.modules.owned_group.messaging_target import (
    OwnedGroupAccountMembershipProbe,
    OwnedGroupMessageTargetResolver,
)
from app.modules.owned_group.messaging_trigger_service import (
    OwnedGroupMessageTriggerService,
)
from app.modules.owned_group.models_extra import (
    OwnedGroupAuditEvent,
    OwnedGroupMembership,
)
from app.modules.owned_group.security import redact_sensitive_text

logger = structlog.get_logger()
_SEND_LEASE_SECONDS = 120
_RETRY_DELAYS_SECONDS = (60, 300, 900)
_DEFERRED_ELIGIBILITY_REASONS = {"membership_verification_stale"}
_GOVERNANCE_AUDIT_KEYS = frozenset(
    {
        "generation_governance_rules_hash",
        "governance_reason_code",
        "governance_rules_hash",
        "matched_rule_ids",
        "matched_term_hashes",
        "send_governance_rules_hash",
    }
)


def _uncertain_delivery_slot_predicate():
    """Rows that may already represent a real Telegram message."""

    return and_(
        GroupAccountMessageExecution.write_started_at.is_not(None),
        or_(
            GroupAccountMessageExecution.status == MessageExecutionStatus.SENDING.value,
            and_(
                GroupAccountMessageExecution.status == MessageExecutionStatus.FAILED.value,
                GroupAccountMessageExecution.error_code == "TELEGRAM_SEND_OUTCOME_UNKNOWN",
            ),
        ),
    )


def _delivery_quota_slot_predicate():
    return or_(
        GroupAccountMessageExecution.status == MessageExecutionStatus.SENT.value,
        _uncertain_delivery_slot_predicate(),
    )


def _content_reservation_predicate():
    return or_(
        GroupAccountMessageExecution.status.in_(RESERVING_EXECUTION_STATUSES),
        _uncertain_delivery_slot_predicate(),
    )


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _governance_audit_after(error: OwnedGroupMessagingError) -> dict[str, Any] | None:
    """Keep only content-free governance evidence from a domain failure."""

    safe = {
        key: value
        for key, value in error.details.items()
        if key in _GOVERNANCE_AUDIT_KEYS
    }
    return safe or None


@dataclass(frozen=True, slots=True)
class ManualExecutionResult:
    execution_id: int
    status: str
    correlation_id: str
    scheduled_at: datetime | None
    created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "correlation_id": self.correlation_id,
            "scheduled_at": (
                self.scheduled_at.isoformat() if self.scheduled_at is not None else None
            ),
            "created": self.created,
        }


class OwnedGroupMessageExecutionService:
    """Apply final gates, then call the exact-account Speaker bridge once."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        account_pool: AccountPool | None = None,
        content_service: OwnedGroupMessageContentService | None = None,
        speaker: Speaker | None = None,
        prompt_context_store: OwnedGroupPromptContextStore | None = None,
    ) -> None:
        self.db = db
        self.account_pool = account_pool or AccountPool()
        self.content_service = content_service or OwnedGroupMessageContentService(db)
        self.prompt_context_store = prompt_context_store or OwnedGroupPromptContextStore(
            getattr(self.content_service, "cache", None)
        )
        self._speaker = speaker
        self.resolver = OwnedGroupMessageTargetResolver(
            db,
            membership_probe=OwnedGroupAccountMembershipProbe(self.account_pool),
        )
        self.logger = logger.bind(module="owned_group_message_execution")

    def _build_speaker(self, asset_id: int) -> Speaker:
        if self._speaker is not None:
            return self._speaker
        return Speaker(
            self.db,
            self.account_pool,
            GroupManager(self.db),
            TemplateEngine(
                self.db,
                scope="owned_group",
                owned_group_asset_id=int(asset_id),
            ),
        )

    @staticmethod
    def _mode_for_category(
        policy: GroupAccountMessagePolicy,
        category: str,
    ) -> str:
        if category == MessageContentCategory.COMMUNITY.value:
            return _enum_value(policy.mode)
        if category == MessageContentCategory.PROMOTION.value:
            return str((policy.promotion_config or {}).get("mode") or MessageMode.OFF.value)
        return MessageMode.OFF.value

    @staticmethod
    def _trigger_enabled(
        policy: GroupAccountMessagePolicy,
        trigger_type: str,
        category: str,
    ) -> bool:
        config = dict((policy.trigger_config or {}).get(trigger_type) or {})
        if not bool(config.get("enabled", trigger_type == "manual")):
            return False
        if trigger_type == "manual":
            allowed = config.get(
                "allowed_content_categories",
                ["community", "promotion"],
            )
            return category in allowed
        return str(config.get("content_category") or "community") == category

    async def _load_policy(
        self,
        execution: GroupAccountMessageExecution,
    ) -> GroupAccountMessagePolicy:
        policy = await self.db.get(
            GroupAccountMessagePolicy,
            int(execution.policy_id),
            populate_existing=True,
        )
        if policy is None or int(policy.owned_group_asset_id) != int(
            execution.owned_group_asset_id
        ):
            raise OwnedGroupMessagingError("POLICY_NOT_FOUND", "消息策略不存在", http_status=404)
        return policy

    async def _policy_gate(
        self,
        execution: GroupAccountMessageExecution,
        policy: GroupAccountMessagePolicy,
    ) -> None:
        category = _enum_value(execution.content_category)
        trigger_type = _enum_value(execution.trigger_type)
        if not bool(policy.enabled):
            raise OwnedGroupMessagingError("POLICY_DISABLED", "消息策略已停用")
        if self._mode_for_category(policy, category) == MessageMode.OFF.value:
            code = (
                "OWNED_GROUP_PROMOTION_DISABLED"
                if category == MessageContentCategory.PROMOTION.value
                else "MESSAGE_MODE_DISABLED"
            )
            raise OwnedGroupMessagingError(code, "当前消息类别未启用")
        if not self._trigger_enabled(policy, trigger_type, category):
            raise OwnedGroupMessagingError("TRIGGER_DISABLED", "本次消息触发器已停用")
        if trigger_type == "keyword":
            config = dict((policy.trigger_config or {}).get("keyword") or {})
            trigger_id = execution.keyword_trigger_id
            configured_ids = {int(item) for item in (config.get("trigger_ids") or [])}
            if trigger_id is None or int(trigger_id) not in configured_ids:
                raise OwnedGroupMessagingError("TRIGGER_DISABLED", "关键词触发器已被策略撤销")
            trigger = await self.db.get(
                KeywordTrigger,
                int(trigger_id),
                populate_existing=True,
            )
            current_mode = self._mode_for_category(policy, category)
            expected_action = (
                TriggerAction.REPLY_TEMPLATE.value
                if current_mode == MessageMode.TEMPLATE.value
                else TriggerAction.REPLY_AI.value
            )
            if (
                trigger is None
                or not bool(trigger.enabled)
                or _enum_value(trigger.action) != expected_action
            ):
                raise OwnedGroupMessagingError(
                    "TRIGGER_DISABLED",
                    "关键词触发器已停用或动作与当前策略不兼容",
                )

    def _add_audit(
        self,
        execution: GroupAccountMessageExecution,
        event_type: str,
        *,
        result: str,
        reason_code: str | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        self.db.add(
            OwnedGroupAuditEvent(
                event_type=event_type,
                group_asset_id=int(execution.owned_group_asset_id),
                resource_type="message_execution",
                resource_id=int(execution.id),
                actor_id=execution.requested_by,
                after_state=json.dumps(
                    {
                        "execution_id": int(execution.id),
                        "policy_id": int(execution.policy_id),
                        "account_id": int(execution.account_id),
                        "status": _enum_value(execution.status),
                        **dict(after or {}),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                result=result,
                reason_code=reason_code,
                correlation_id=execution.correlation_id,
            )
        )

    @staticmethod
    def _summarize_prompt_context(
        execution: GroupAccountMessageExecution,
    ) -> None:
        """Remove raw generation inputs once they are no longer needed."""

        context = getattr(execution, "prompt_context", None)
        if not context:
            return
        execution.prompt_context = summarize_prompt_context(context)

    async def _generation_prompt_context(
        self,
        execution: GroupAccountMessageExecution,
    ) -> dict[str, Any]:
        """Load raw inputs without ever copying them back onto the ORM row."""

        persisted = dict(getattr(execution, "prompt_context", None) or {})
        if requires_ephemeral_prompt_context(persisted):
            raise OwnedGroupMessagingError(
                "PROMPT_CONTEXT_INVALID",
                "消息生成上下文违反临时存储约束，请重新触发",
                http_status=422,
            )
        if not summary_requires_ephemeral_prompt_context(persisted):
            return persisted
        ephemeral = await self.prompt_context_store.load(int(execution.id))
        return {**persisted, **ephemeral}

    @staticmethod
    def _generation_policy_snapshot(
        execution: GroupAccountMessageExecution,
        prompt_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = prompt_context.get("generation_policy_snapshot")
        if not isinstance(snapshot, Mapping):
            raise OwnedGroupMessagingError(
                "POLICY_SNAPSHOT_INVALID",
                "消息执行缺少生成策略快照，请重新触发",
                http_status=422,
            )
        mode = str(snapshot.get("mode") or "")
        if mode != _enum_value(execution.mode_snapshot):
            raise OwnedGroupMessagingError(
                "POLICY_SNAPSHOT_INVALID",
                "消息执行生成模式快照不一致，请重新触发",
                http_status=422,
            )
        return {
            "id": int(execution.policy_id),
            "account_id": int(execution.account_id),
            "mode": mode,
            "require_review": bool(snapshot.get("require_review", True)),
            "allowed_topics": [
                str(item) for item in (snapshot.get("allowed_topics") or [])
            ],
            "default_template_id": snapshot.get("default_template_id"),
            "promotion_config": dict(execution.promotion_config_snapshot or {}),
        }

    async def _finish_without_send(
        self,
        execution: GroupAccountMessageExecution,
        *,
        status: str,
        code: str,
        message: str,
        after: Mapping[str, Any] | None = None,
    ) -> GroupAccountMessageExecution:
        self._summarize_prompt_context(execution)
        execution.status = status
        execution.error_code = code
        execution.error_message = redact_sensitive_text(message, max_length=500)
        execution.lease_id = None
        execution.lease_expires_at = None
        execution.next_retry_at = None
        execution.revision = int(execution.revision) + 1
        self._add_audit(
            execution,
            (
                "message_execution_skipped"
                if status == MessageExecutionStatus.SKIPPED.value
                else "message_execution_failed"
            ),
            result=("skipped" if status == MessageExecutionStatus.SKIPPED.value else "failed"),
            reason_code=code,
            after=after,
        )
        await self.db.commit()
        await self.prompt_context_store.discard(int(execution.id))
        return execution

    async def _finish_generation_error_if_owned(
        self,
        execution_id: int,
        lease_id: str,
        *,
        status: str,
        code: str,
        message: str,
        after: Mapping[str, Any] | None = None,
    ) -> GroupAccountMessageExecution | None:
        """Persist a generation error only while this worker still owns its lease."""

        await self.db.rollback()
        current = await self.db.scalar(
            message_execution_for_update_query()
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.GENERATING.value,
                GroupAccountMessageExecution.lease_id == lease_id,
            )
        )
        if current is None:
            await self.db.rollback()
            return await self.db.get(
                GroupAccountMessageExecution,
                int(execution_id),
                populate_existing=True,
            )
        return await self._finish_without_send(
            current,
            status=status,
            code=code,
            message=message,
            after=after,
        )

    async def _defer_generation_error_if_owned(
        self,
        execution_id: int,
        lease_id: str,
        *,
        code: str,
        message: str,
        retry_delay_seconds: int = 30,
    ) -> GroupAccountMessageExecution | None:
        """Release an owned generation lease after a transient dependency outage."""

        await self.db.rollback()
        current = await self.db.scalar(
            message_execution_for_update_query()
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.GENERATING.value,
                GroupAccountMessageExecution.lease_id == lease_id,
            )
        )
        if current is None:
            await self.db.rollback()
            return await self.db.get(
                GroupAccountMessageExecution,
                int(execution_id),
                populate_existing=True,
            )
        current.status = MessageExecutionStatus.QUEUED.value
        current.error_code = code
        current.error_message = redact_sensitive_text(message, max_length=500)
        current.lease_id = None
        current.lease_expires_at = None
        current.next_retry_at = datetime.utcnow() + timedelta(
            seconds=max(1, int(retry_delay_seconds))
        )
        current.revision = int(current.revision) + 1
        self._add_audit(
            current,
            "message_execution_generation_deferred",
            result="deferred",
            reason_code=code,
            after={"next_retry_at": current.next_retry_at.isoformat()},
        )
        await self.db.commit()
        return current

    async def _duplicate_execution_id(
        self,
        execution: GroupAccountMessageExecution,
        *,
        content_hash: str,
        dedupe_window_seconds: int,
    ) -> int | None:
        since = datetime.utcnow() - timedelta(seconds=max(1, dedupe_window_seconds))
        return await self.db.scalar(
            select(GroupAccountMessageExecution.id)
            .where(
                GroupAccountMessageExecution.id != int(execution.id),
                GroupAccountMessageExecution.core_group_id == int(execution.core_group_id),
                GroupAccountMessageExecution.content_hash == content_hash,
                _content_reservation_predicate(),
                func.coalesce(
                    GroupAccountMessageExecution.sent_at,
                    GroupAccountMessageExecution.write_started_at,
                    GroupAccountMessageExecution.updated_at,
                )
                >= since,
            )
            .order_by(GroupAccountMessageExecution.id)
            .limit(1)
        )

    async def prepare_execution(
        self,
        execution_id: int,
    ) -> GroupAccountMessageExecution | None:
        """Claim one queued row, generate its frozen content, and reserve it."""

        now = datetime.utcnow()
        execution = await self.db.scalar(
            message_execution_for_update_query(skip_locked=True)
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.QUEUED.value,
                or_(
                    GroupAccountMessageExecution.scheduled_at.is_(None),
                    GroupAccountMessageExecution.scheduled_at <= now,
                ),
                or_(
                    GroupAccountMessageExecution.next_retry_at.is_(None),
                    GroupAccountMessageExecution.next_retry_at <= now,
                ),
            )
        )
        if execution is None:
            await self.db.rollback()
            return None

        lease_id = f"generate:{uuid.uuid4().hex}"
        execution.status = MessageExecutionStatus.GENERATING.value
        execution.lease_id = lease_id
        execution.lease_expires_at = now + timedelta(seconds=_SEND_LEASE_SECONDS)
        execution.error_code = None
        execution.error_message = None
        await self.db.commit()

        try:
            policy = await self._load_policy(execution)
            await self._policy_gate(execution, policy)
            target = await self.resolver.resolve(
                int(execution.owned_group_asset_id),
                require_managed=True,
            )
            runtime = await get_owned_group_messaging_settings(self.db)
            policy_window = int(
                (policy.trigger_config or {}).get("dedupe_window_seconds", 21600) or 21600
            )
            dedupe_window = max(
                policy_window,
                int(runtime.get("contentDedupeWindowSeconds", 21600) or 21600),
            )
            max_generation_attempts = (
                3 if _enum_value(execution.mode_snapshot) == MessageMode.AI.value else 1
            )
            generation_context = await self._generation_prompt_context(execution)
            generation_policy = self._generation_policy_snapshot(
                execution,
                generation_context,
            )

            for generation_attempt in range(max_generation_attempts):
                generated = await self.content_service.generate_for_execution(
                    execution,
                    generation_policy,
                    target,
                    prompt_context=generation_context,
                )
                async with telegram_chat_advisory_lock(
                    self.db,
                    int(target.telegram_chat_id),
                ):
                    current = await self.db.get(
                        GroupAccountMessageExecution,
                        int(execution.id),
                        populate_existing=True,
                    )
                    if (
                        current is None
                        or _enum_value(current.status) != MessageExecutionStatus.GENERATING.value
                        or current.lease_id != lease_id
                    ):
                        await self.db.rollback()
                        return current

                    duplicate = await self._duplicate_execution_id(
                        current,
                        content_hash=generated.content_hash,
                        dedupe_window_seconds=dedupe_window,
                    )
                    if duplicate is not None:
                        if generation_attempt + 1 < max_generation_attempts:
                            # End the read transaction without expiring the
                            # frozen execution inputs before regenerating.
                            await self.db.commit()
                            continue
                        return await self._finish_without_send(
                            current,
                            status=MessageExecutionStatus.SKIPPED.value,
                            code="DUPLICATE_CONTENT",
                            message="同一自建群在去重窗口内已有相同内容",
                        )

                    current.content = generated.content
                    current.content_hash = generated.content_hash
                    generated_prompt_hash = getattr(generated, "prompt_hash", None)
                    generated_governance_hash = getattr(
                        generated,
                        "governance_rules_hash",
                        None,
                    )
                    if generated_prompt_hash is not None:
                        if current.prompt_hash not in {None, generated_prompt_hash}:
                            raise OwnedGroupMessagingError(
                                "POLICY_SNAPSHOT_INVALID",
                                "消息执行的 Prompt 指纹已存在且不一致",
                            )
                        current.prompt_hash = str(generated_prompt_hash)
                    if generated_governance_hash is not None:
                        if current.governance_rules_hash not in {
                            None,
                            generated_governance_hash,
                        }:
                            raise OwnedGroupMessagingError(
                                "POLICY_SNAPSHOT_INVALID",
                                "消息执行的治理指纹已存在且不一致",
                            )
                        current.governance_rules_hash = str(generated_governance_hash)
                    self._summarize_prompt_context(current)
                    current.status = (
                        MessageExecutionStatus.PENDING_REVIEW.value
                        if generated.would_require_review
                        else MessageExecutionStatus.READY_TO_SEND.value
                    )
                    current.lease_id = None
                    current.lease_expires_at = None
                    current.error_code = None
                    current.error_message = None
                    current.revision = int(current.revision) + 1
                    await self.db.commit()
                    await self.prompt_context_store.discard(int(current.id))
                    return current
        except OwnedGroupMessagingError as exc:
            if exc.code in {
                "PROMPT_CONTEXT_UNAVAILABLE",
                "AI_PROVIDER_COOLDOWN",
                "AI_PROVIDER_TEMPORARY_FAILURE",
            }:
                return await self._defer_generation_error_if_owned(
                    execution_id,
                    lease_id,
                    code=exc.code,
                    message=exc.message,
                )
            technical = exc.code in {
                "AI_PROVIDER_UNAVAILABLE",
                "AI_PROVIDER_UNSAFE",
                "AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED",
                "AI_GENERATION_FAILED",
                "AI_TOKEN_BUDGET_UNAVAILABLE",
                "AI_PROMPT_TOO_LARGE",
                "EXECUTION_BUSINESS_SNAPSHOT_MISSING",
                "GOVERNANCE_CONTEXT_UNAVAILABLE",
                "PERSONA_CONFIG_INVALID",
                "PROMPT_CONTEXT_EXPIRED",
                "PROMPT_CONTEXT_INVALID",
                "POLICY_SNAPSHOT_INVALID",
            }
            return await self._finish_generation_error_if_owned(
                execution_id,
                lease_id,
                status=(
                    MessageExecutionStatus.FAILED.value
                    if technical
                    else MessageExecutionStatus.SKIPPED.value
                ),
                code=exc.code,
                message=exc.message,
            )
        except Exception as exc:
            self.logger.error(
                "owned_group_message_generation_failed",
                execution_id=execution_id,
                error_type=type(exc).__name__,
                error=redact_sensitive_text(exc, max_length=500),
            )
            return await self._finish_generation_error_if_owned(
                execution_id,
                lease_id,
                status=MessageExecutionStatus.FAILED.value,
                code="CONTENT_GENERATION_FAILED",
                message=str(exc),
            )

        return None

    async def _check_send_gates(
        self,
        execution: GroupAccountMessageExecution,
        policy: GroupAccountMessagePolicy,
        runtime: Mapping[str, Any],
    ) -> str | None:
        await self._policy_gate(execution, policy)
        if _enum_value(execution.mode_snapshot) == MessageMode.AI.value:
            ai_settings = await get_group_ai_interaction_settings(self.db)
            if not bool(ai_settings.get("enabled", False)):
                raise OwnedGroupMessagingError(
                    "AI_INTERACTION_DISABLED",
                    "群 AI 总开关已关闭",
                )
        eligibility = await self.resolver.validate_account_eligibility(
            int(execution.owned_group_asset_id),
            int(execution.account_id),
            probe_stale=False,
        )
        blocking_reasons = set(eligibility.blocking_reasons)
        hard_reasons = blocking_reasons - _DEFERRED_ELIGIBILITY_REASONS
        if hard_reasons:
            code = (
                "ACCOUNT_MODE_NOT_ALLOWED"
                if "account_mode_not_allowed" in hard_reasons
                else "ACCOUNT_NOT_ELIGIBLE"
            )
            raise OwnedGroupMessagingError(
                code,
                "策略账号当前不符合自建群消息准入",
                details={"blocking_reasons": sorted(blocking_reasons)},
            )

        account = await self.db.get(
            TelegramAccount,
            int(execution.account_id),
            populate_existing=True,
        )
        if account is None:
            raise OwnedGroupMessagingError(
                "ACCOUNT_NOT_ELIGIBLE",
                "指定账号不存在",
            )
        if not execution.content or not execution.content_hash:
            raise OwnedGroupMessagingError(
                "CONTENT_SAFETY_BLOCKED",
                "执行没有可发送的冻结内容",
            )
        validated = await self.content_service.validate_final_content(
            execution.content,
            content_category=_enum_value(execution.content_category),
            promotion_config=execution.promotion_config_snapshot or {},
            mode_snapshot=_enum_value(execution.mode_snapshot),
        )
        if validated.content_hash != execution.content_hash:
            raise OwnedGroupMessagingError(
                "CONTENT_HASH_MISMATCH",
                "冻结内容与内容哈希不一致",
            )

        now = datetime.utcnow()
        day_start = operating_day_start(now)
        group_sent = int(
            await self.db.scalar(
                select(func.count(GroupAccountMessageExecution.id)).where(
                    GroupAccountMessageExecution.core_group_id == int(execution.core_group_id),
                    _delivery_quota_slot_predicate(),
                    func.coalesce(
                        GroupAccountMessageExecution.sent_at,
                        GroupAccountMessageExecution.write_started_at,
                    )
                    >= day_start,
                )
            )
            or 0
        )
        account_sent = int(
            await self.db.scalar(
                select(func.count(GroupAccountMessageExecution.id)).where(
                    GroupAccountMessageExecution.account_id == int(execution.account_id),
                    _delivery_quota_slot_predicate(),
                    func.coalesce(
                        GroupAccountMessageExecution.sent_at,
                        GroupAccountMessageExecution.write_started_at,
                    )
                    >= day_start,
                )
            )
            or 0
        )
        policy_sent = int(
            await self.db.scalar(
                select(func.count(GroupAccountMessageExecution.id)).where(
                    GroupAccountMessageExecution.policy_id == int(execution.policy_id),
                    _delivery_quota_slot_predicate(),
                    func.coalesce(
                        GroupAccountMessageExecution.sent_at,
                        GroupAccountMessageExecution.write_started_at,
                    )
                    >= day_start,
                )
            )
            or 0
        )
        group_limit = int(runtime.get("globalMaxPerGroupPerDay", 20) or 0)
        account_limit = int(runtime.get("globalMaxPerAccountPerDay", 30) or 0)
        policy_limit = int(policy.daily_limit)
        if group_limit <= 0 or group_sent >= group_limit:
            raise OwnedGroupMessagingError(
                "GROUP_DAILY_LIMIT_REACHED",
                "自建群今日消息额度已耗尽",
            )
        if account_limit <= 0 or account_sent >= account_limit:
            raise OwnedGroupMessagingError(
                "ACCOUNT_DAILY_LIMIT_REACHED",
                "指定账号今日自建群消息额度已耗尽",
            )
        if policy_limit <= 0:
            raise OwnedGroupMessagingError(
                "POLICY_DAILY_LIMIT_ZERO",
                "策略每日额度为零",
            )
        if policy_sent >= policy_limit:
            raise OwnedGroupMessagingError(
                "POLICY_DAILY_LIMIT_REACHED",
                "该账号在此群的今日发送额度已用完",
            )

        last_group_sent = await self.db.scalar(
            select(
                func.max(
                    func.coalesce(
                        GroupAccountMessageExecution.sent_at,
                        GroupAccountMessageExecution.write_started_at,
                    )
                )
            ).where(
                GroupAccountMessageExecution.core_group_id == int(execution.core_group_id),
                _delivery_quota_slot_predicate(),
            )
        )
        cooldown_seconds = max(
            int(policy.cooldown_seconds),
            int(runtime.get("minGroupCooldownSeconds", 300) or 300),
        )
        if (
            last_group_sent is not None
            and last_group_sent + timedelta(seconds=cooldown_seconds) > now
        ):
            raise OwnedGroupMessagingError(
                "GROUP_COOLDOWN_ACTIVE",
                "自建群消息冷却中",
            )

        if execution.source_message_id is not None:
            source_duplicate = await self.db.scalar(
                select(GroupAccountMessageExecution.id)
                .where(
                    GroupAccountMessageExecution.id != int(execution.id),
                    GroupAccountMessageExecution.owned_group_asset_id
                    == int(execution.owned_group_asset_id),
                    GroupAccountMessageExecution.source_message_id
                    == int(execution.source_message_id),
                    _content_reservation_predicate(),
                )
                .order_by(GroupAccountMessageExecution.id)
                .limit(1)
            )
            if source_duplicate is not None:
                raise OwnedGroupMessagingError(
                    "SOURCE_MESSAGE_ALREADY_HANDLED",
                    "该来源消息已被自建群消息执行占用",
                )

        dedupe_window = max(
            int(
                (policy.trigger_config or {}).get(
                    "dedupe_window_seconds",
                    21600,
                )
                or 21600
            ),
            int(runtime.get("contentDedupeWindowSeconds", 21600) or 21600),
        )
        duplicate = await self._duplicate_execution_id(
            execution,
            content_hash=execution.content_hash,
            dedupe_window_seconds=dedupe_window,
        )
        if duplicate is not None:
            raise OwnedGroupMessagingError(
                "DUPLICATE_CONTENT",
                "同一自建群在去重窗口内已有相同内容",
            )
        governance_decision = await govern_before_send(
            db=self.db,
            execution=execution,
            text=validated.content,
        )
        send_governance_hash = getattr(
            governance_decision,
            "governance_rules_hash",
            None,
        )
        return send_governance_hash if isinstance(send_governance_hash, str) else None

    async def _mark_telegram_write_started(
        self,
        *,
        execution_id: int,
        lease_id: str,
    ) -> None:
        """Persist the last safe recovery boundary immediately before Telegram I/O."""

        execution = await self.db.scalar(
            message_execution_for_update_query()
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.SENDING.value,
                GroupAccountMessageExecution.lease_id == lease_id,
            )
            .execution_options(populate_existing=True)
        )
        if execution is None:
            await self.db.rollback()
            raise RuntimeError("owned-group send lease lost before Telegram write")
        execution.write_started_at = datetime.utcnow()
        execution.revision = int(execution.revision) + 1
        await self.db.commit()

    async def send_execution(
        self,
        execution_id: int,
    ) -> GroupAccountMessageExecution | None:
        """Run every final gate under the shared chat lock, then send once."""

        preliminary = await self.db.get(
            GroupAccountMessageExecution,
            int(execution_id),
            populate_existing=True,
        )
        if preliminary is None:
            return None
        if _enum_value(preliminary.status) != MessageExecutionStatus.READY_TO_SEND.value:
            return preliminary
        if (
            preliminary.scheduled_at is not None and preliminary.scheduled_at > datetime.utcnow()
        ) or (
            preliminary.next_retry_at is not None and preliminary.next_retry_at > datetime.utcnow()
        ):
            return preliminary

        async with AsyncExitStack() as locks:
            await locks.enter_async_context(
                telegram_chat_advisory_lock(
                    self.db,
                    int(preliminary.telegram_chat_id),
                )
            )
            await locks.enter_async_context(
                telegram_account_advisory_lock(
                    self.db,
                    int(preliminary.account_id),
                )
            )
            execution = await self.db.scalar(
                message_execution_for_update_query(skip_locked=True)
                .where(
                    GroupAccountMessageExecution.id == int(execution_id),
                    GroupAccountMessageExecution.status
                    == MessageExecutionStatus.READY_TO_SEND.value,
                    or_(
                        GroupAccountMessageExecution.next_retry_at.is_(None),
                        GroupAccountMessageExecution.next_retry_at <= datetime.utcnow(),
                    ),
                )
            )
            if execution is None:
                await self.db.rollback()
                return None

            send_governance_hash: str | None = None
            try:
                runtime = await get_owned_group_messaging_settings(self.db)
                if not bool(settings.OWNED_GROUP_MESSAGING_ENABLED):
                    raise OwnedGroupMessagingError(
                        "OWNED_GROUP_MESSAGING_DISABLED",
                        "自建群消息静态开关未启用",
                    )
                if not bool(runtime.get("enabled", False)):
                    raise OwnedGroupMessagingError(
                        "OWNED_GROUP_MESSAGING_DISABLED",
                        "自建群消息运行开关未启用",
                    )
                if bool(runtime.get("dryRun", True)):
                    raise OwnedGroupMessagingError(
                        "DRY_RUN_ENABLED",
                        "自建群消息处于 dry-run",
                    )
                # Resolver is deliberately inside the final shared lock: a
                # concurrent mapping/governance change cannot race Telegram.
                target = await self.resolver.resolve(
                    int(execution.owned_group_asset_id),
                    require_managed=True,
                )
                if int(target.core_group_id) != int(execution.core_group_id) or int(
                    target.telegram_chat_id
                ) != int(execution.telegram_chat_id):
                    raise OwnedGroupMessagingError(
                        "TARGET_MAPPING_INVALID",
                        "执行目标快照与当前双 ID 映射不一致",
                    )
                policy = await self._load_policy(execution)
                send_governance_hash = await self._check_send_gates(
                    execution,
                    policy,
                    runtime,
                )
                if not isinstance(send_governance_hash, str):
                    send_governance_hash = None
            except OwnedGroupMessagingError as exc:
                return await self._finish_without_send(
                    execution,
                    status=MessageExecutionStatus.SKIPPED.value,
                    code=exc.code,
                    message=exc.message,
                    after=_governance_audit_after(exc),
                )

            lease_id = f"send:{uuid.uuid4().hex}"
            execution.status = MessageExecutionStatus.SENDING.value
            execution.lease_id = lease_id
            execution.lease_expires_at = datetime.utcnow() + timedelta(seconds=_SEND_LEASE_SECONDS)
            execution.write_started_at = None
            execution.next_retry_at = None
            execution.attempt_count = int(execution.attempt_count) + 1
            execution.error_code = None
            execution.error_message = None
            await self.db.commit()

            async def mark_telegram_write_started() -> None:
                await self._mark_telegram_write_started(
                    execution_id=int(execution.id),
                    lease_id=lease_id,
                )

            result = await self._build_speaker(
                int(execution.owned_group_asset_id)
            ).send_owned_group_message(
                target=target,
                account_id=int(execution.account_id),
                content=execution.content or "",
                purpose=_enum_value(execution.message_purpose),
                content_category=_enum_value(execution.content_category),
                execution_id=int(execution.id),
                send_attempt_id=lease_id,
                reply_to_message_id=execution.reply_to_message_id,
                before_telegram_write=mark_telegram_write_started,
            )
            return await self._finalize_send(
                execution_id=int(execution.id),
                lease_id=lease_id,
                result=result,
                runtime=runtime,
                send_governance_rules_hash=send_governance_hash,
            )

    async def _finalize_send(
        self,
        *,
        execution_id: int,
        lease_id: str,
        result: SpeakResult,
        runtime: Mapping[str, Any],
        send_governance_rules_hash: str | None = None,
    ) -> GroupAccountMessageExecution | None:
        # A preflight database callback may have left this shared session in a
        # failed transaction. Reload the fenced execution from committed state.
        await self.db.rollback()
        execution = await self.db.get(
            GroupAccountMessageExecution,
            int(execution_id),
            populate_existing=True,
        )
        if (
            execution is None
            or _enum_value(execution.status) != MessageExecutionStatus.SENDING.value
            or execution.lease_id != lease_id
        ):
            await self.db.rollback()
            self.logger.error(
                "owned_group_send_lease_lost",
                execution_id=execution_id,
                telegram_message_id=result.message_id,
            )
            return execution

        now = datetime.utcnow()
        if result.error_code == "RISK_RESERVATION_RELEASE_PENDING":
            execution.error_code = result.error_code
            execution.error_message = redact_sensitive_text(
                result.error or "风险额度回退待重试",
                max_length=500,
            )
            execution.lease_expires_at = now
            execution.next_retry_at = None
            execution.revision = int(execution.revision) + 1
            await self.db.commit()
            return execution
        if result.success and result.message_id is not None:
            execution.status = MessageExecutionStatus.SENT.value
            execution.telegram_message_id = int(result.message_id)
            execution.sent_at = now
            execution.lease_id = None
            execution.lease_expires_at = None
            execution.next_retry_at = None
            execution.error_code = None
            execution.error_message = None
            execution.revision = int(execution.revision) + 1

            existing_message = await self.db.scalar(
                select(AcquisitionMessage.id).where(
                    AcquisitionMessage.owned_group_execution_id == int(execution.id)
                )
            )
            if existing_message is None:
                self.db.add(
                    AcquisitionMessage(
                        account_id=int(execution.account_id),
                        group_id=int(execution.telegram_chat_id),
                        core_group_id=int(execution.core_group_id),
                        content=execution.content,
                        message_type=(
                            MessageType.GUIDE
                            if _enum_value(execution.content_category)
                            == MessageContentCategory.PROMOTION.value
                            else MessageType.INTERACTION
                        ),
                        message_id=int(result.message_id),
                        message_purpose=_enum_value(execution.message_purpose),
                        content_category=_enum_value(execution.content_category),
                        owned_group_execution_id=int(execution.id),
                        content_hash=execution.content_hash,
                        sent_at=now,
                    )
                )
            sent_audit: dict[str, Any] = {
                "telegram_message_id": int(result.message_id)
            }
            if isinstance(send_governance_rules_hash, str):
                sent_audit.update(
                    {
                        "generation_governance_rules_hash": (
                            execution.governance_rules_hash
                        ),
                        "send_governance_rules_hash": send_governance_rules_hash,
                    }
                )
            self._add_audit(
                execution,
                "message_execution_sent",
                result="success",
                after=sent_audit,
            )
            await self.db.commit()
            return execution

        code = result.error_code or "TELEGRAM_SEND_FAILED"
        message = result.error or "Telegram message send failed"
        max_attempts = min(
            3,
            max(1, int(runtime.get("maxSendAttempts", 3) or 3)),
        )
        can_retry = (
            bool(result.retryable)
            and not bool(result.outcome_unknown)
            and int(execution.attempt_count) < max_attempts
        )
        if can_retry:
            delay_index = min(
                max(0, int(execution.attempt_count) - 1),
                len(_RETRY_DELAYS_SECONDS) - 1,
            )
            retry_delay = int(
                getattr(result, "retry_after_seconds", 0) or _RETRY_DELAYS_SECONDS[delay_index]
            )
            execution.status = MessageExecutionStatus.READY_TO_SEND.value
            execution.next_retry_at = now + timedelta(seconds=retry_delay)
            execution.error_code = code
            execution.error_message = redact_sensitive_text(message, max_length=500)
            execution.lease_id = None
            execution.lease_expires_at = None
            execution.write_started_at = None
            execution.revision = int(execution.revision) + 1
            await self.db.commit()
            return execution

        if code == "GROUP_WRITE_FORBIDDEN":
            membership = await self.db.scalar(
                select(OwnedGroupMembership).where(
                    OwnedGroupMembership.group_asset_id == int(execution.owned_group_asset_id),
                    OwnedGroupMembership.resource_type == "user",
                    OwnedGroupMembership.resource_id == int(execution.account_id),
                )
            )
            if membership is not None:
                membership.status = "unknown_needs_reconcile"

        execution.status = MessageExecutionStatus.FAILED.value
        execution.next_retry_at = None
        execution.error_code = "TELEGRAM_SEND_OUTCOME_UNKNOWN" if result.outcome_unknown else code
        execution.error_message = redact_sensitive_text(message, max_length=500)
        execution.lease_id = None
        execution.lease_expires_at = None
        if not result.outcome_unknown:
            execution.write_started_at = None
        execution.revision = int(execution.revision) + 1
        self._add_audit(
            execution,
            "message_execution_failed",
            result="failed",
            reason_code=execution.error_code,
        )
        await self.db.commit()
        return execution

    async def fail_stale_sending(
        self,
        execution_id: int,
    ) -> GroupAccountMessageExecution | None:
        """Never replay an expired post-attempt lease with unknown outcome."""

        preliminary = await self.db.get(
            GroupAccountMessageExecution,
            int(execution_id),
            populate_existing=True,
        )
        if preliminary is None:
            return None
        if (
            _enum_value(preliminary.status) != MessageExecutionStatus.SENDING.value
            or preliminary.lease_expires_at is None
            or preliminary.lease_expires_at >= datetime.utcnow()
        ):
            return preliminary

        async with AsyncExitStack() as locks:
            await locks.enter_async_context(
                telegram_chat_advisory_lock(
                    self.db,
                    int(preliminary.telegram_chat_id),
                )
            )
            await locks.enter_async_context(
                telegram_account_advisory_lock(
                    self.db,
                    int(preliminary.account_id),
                )
            )
            execution = await self.db.scalar(
                message_execution_for_update_query(skip_locked=True)
                .where(
                    GroupAccountMessageExecution.id == int(execution_id),
                    GroupAccountMessageExecution.status == MessageExecutionStatus.SENDING.value,
                    GroupAccountMessageExecution.lease_expires_at < datetime.utcnow(),
                )
            )
            if execution is None:
                await self.db.rollback()
                return None
            confirmed_prewrite = (
                execution.write_started_at is None
                or execution.error_code == "RISK_RESERVATION_RELEASE_PENDING"
            )
            if confirmed_prewrite:
                # A worker can die after the Redis hard-cap reservation but
                # before the durable Telegram-write marker. The lease-derived
                # token lets recovery return exactly that unused slot.
                if execution.lease_id:
                    try:
                        released = await AccountRiskGuard(
                            self.db
                        ).release_owned_group_message_reservation(
                            int(execution.account_id),
                            AccountRiskGuard.owned_group_reservation_id(execution.lease_id),
                        )
                    except Exception as exc:
                        await self.db.rollback()
                        self.logger.error(
                            "owned_group_stale_risk_release_failed",
                            execution_id=int(execution.id),
                            error_type=type(exc).__name__,
                            error=redact_sensitive_text(exc, max_length=500),
                        )
                        return execution
                    if not released:
                        await self.db.rollback()
                        self.logger.error(
                            "owned_group_stale_risk_release_unconfirmed",
                            execution_id=int(execution.id),
                        )
                        return execution
                execution.status = MessageExecutionStatus.READY_TO_SEND.value
                execution.lease_id = None
                execution.lease_expires_at = None
                execution.write_started_at = None
                execution.next_retry_at = datetime.utcnow()
                execution.attempt_count = max(0, int(execution.attempt_count) - 1)
                execution.error_code = None
                execution.error_message = None
                execution.revision = int(execution.revision) + 1
                await self.db.commit()
                return execution
            return await self._finish_without_send(
                execution,
                status=MessageExecutionStatus.FAILED.value,
                code="TELEGRAM_SEND_OUTCOME_UNKNOWN",
                message="发送租约超时，无法确认 Telegram 是否已接收消息",
            )

    async def fail_stale_generating(
        self,
        execution_id: int,
    ) -> GroupAccountMessageExecution | None:
        """Terminalize an expired generation lease without another LLM call."""

        execution = await self.db.scalar(
            message_execution_for_update_query(skip_locked=True)
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.status
                == MessageExecutionStatus.GENERATING.value,
                or_(
                    GroupAccountMessageExecution.lease_expires_at.is_(None),
                    GroupAccountMessageExecution.lease_expires_at < datetime.utcnow(),
                ),
            )
        )
        if execution is None:
            await self.db.rollback()
            return None

        execution.status = MessageExecutionStatus.FAILED.value
        execution.lease_id = None
        execution.lease_expires_at = None
        execution.error_code = "AI_GENERATION_LEASE_EXPIRED"
        execution.error_message = "AI 生成租约已过期；为避免重复生成，任务已终止"
        execution.next_retry_at = None
        execution.revision = int(execution.revision) + 1
        self._add_audit(
            execution,
            "message_execution_failed",
            result="failed",
            reason_code="AI_GENERATION_LEASE_EXPIRED",
            after={"generation_lease_expired": True},
        )
        await self.db.commit()
        return execution

    async def recover_stale_generating(
        self,
        execution_id: int,
    ) -> GroupAccountMessageExecution | None:
        """Backward-compatible method name with stage-three terminal semantics."""

        return await self.fail_stale_generating(execution_id)


async def create_manual_execution(
    *,
    db: AsyncSession,
    asset_id: int,
    policy: GroupAccountMessagePolicy,
    request: Any,
    idempotency_key: str,
    actor_id: int | None,
    correlation_id: str,
) -> ManualExecutionResult:
    """Public API bridge for durable, request-fingerprinted manual execution."""

    raw_key = str(idempotency_key or "").strip()
    if not 8 <= len(raw_key) <= 128:
        raise OwnedGroupMessagingError(
            "INVALID_TRIGGER_CONFIG",
            "Idempotency-Key 长度必须为 8 到 128",
            http_status=400,
        )
    if int(policy.owned_group_asset_id) != int(asset_id):
        raise OwnedGroupMessagingError("POLICY_NOT_FOUND", "策略不属于当前自建群", http_status=404)

    if hasattr(request, "model_dump"):
        request_payload = request.model_dump(mode="json")
    elif isinstance(request, Mapping):
        request_payload = dict(request)
    else:
        request_payload = {
            key: getattr(request, key)
            for key in (
                "trigger_type",
                "content_category",
                "topic",
                "instruction",
                "template_id",
                "variables",
                "reply_to_message_id",
                "scheduled_at",
            )
            if hasattr(request, key)
        }
    fingerprint_payload = {
        "asset_id": int(asset_id),
        "policy_id": int(policy.id),
        "request": request_payload,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    durable_key = raw_key
    trigger_service = OwnedGroupMessageTriggerService(db)
    replay_context = trigger_service._redact_prompt_context(
        {
            "instruction": _value(request, "instruction"),
            "topic": _value(request, "topic"),
            "variables": dict(_value(request, "variables", {}) or {}),
            "request_fingerprint": fingerprint,
        }
    )
    existing = await db.scalar(
        select(GroupAccountMessageExecution).where(
            GroupAccountMessageExecution.idempotency_key == durable_key
        )
    )
    if existing is not None:
        existing_fingerprint = dict(existing.prompt_context or {}).get("request_fingerprint")
        if (
            int(existing.owned_group_asset_id) != int(asset_id)
            or int(existing.policy_id) != int(policy.id)
            or int(existing.account_id) != int(policy.account_id)
            or _enum_value(existing.trigger_type) != "manual"
            or existing_fingerprint != fingerprint
        ):
            raise OwnedGroupMessagingError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key 已用于不同请求",
                details={"execution_id": int(existing.id)},
            )
        await trigger_service._rehydrate_existing_prompt_context(existing, replay_context)
        return ManualExecutionResult(
            execution_id=int(existing.id),
            status=_enum_value(existing.status),
            correlation_id=existing.correlation_id,
            scheduled_at=existing.scheduled_at,
            created=False,
        )

    runtime = await get_owned_group_messaging_settings(db)
    if not bool(settings.OWNED_GROUP_MESSAGING_ENABLED) or not bool(runtime.get("enabled", False)):
        raise OwnedGroupMessagingError(
            "OWNED_GROUP_MESSAGING_DISABLED",
            "自建群消息功能未启用",
        )
    if not bool(policy.enabled):
        raise OwnedGroupMessagingError("POLICY_DISABLED", "消息策略已停用")

    category = str(_value(request, "content_category", ""))
    manual = dict((policy.trigger_config or {}).get("manual") or {})
    if not bool(manual.get("enabled", True)):
        raise OwnedGroupMessagingError("MANUAL_TRIGGER_DISABLED", "手动触发已关闭")
    allowed_categories = manual.get(
        "allowed_content_categories",
        ["community", "promotion"],
    )
    if category not in allowed_categories:
        raise OwnedGroupMessagingError(
            "MANUAL_TRIGGER_DISABLED",
            "手动触发不允许当前内容类别",
        )
    mode = OwnedGroupMessageExecutionService._mode_for_category(policy, category)
    if mode == MessageMode.OFF.value:
        code = (
            "OWNED_GROUP_PROMOTION_DISABLED"
            if category == MessageContentCategory.PROMOTION.value
            else "MESSAGE_MODE_DISABLED"
        )
        raise OwnedGroupMessagingError(code, "当前消息类别未启用")

    target = await OwnedGroupMessageTargetResolver(db).resolve(
        int(asset_id),
        require_managed=True,
    )
    eligibility = await OwnedGroupMessageTargetResolver(db).validate_account_eligibility(
        int(asset_id),
        int(policy.account_id),
        probe_stale=False,
    )
    eligibility_reasons = set(eligibility.blocking_reasons)
    hard_eligibility_reasons = eligibility_reasons - _DEFERRED_ELIGIBILITY_REASONS
    if hard_eligibility_reasons:
        code = (
            "ACCOUNT_MODE_NOT_ALLOWED"
            if "account_mode_not_allowed" in hard_eligibility_reasons
            else "ACCOUNT_NOT_ELIGIBLE"
        )
        raise OwnedGroupMessagingError(
            code,
            "策略账号当前不符合自建群消息准入",
            details={"blocking_reasons": sorted(eligibility_reasons)},
        )

    topic = _value(request, "topic")
    if mode == MessageMode.AI.value and topic:
        allowed_topics = {str(item).strip().casefold() for item in (policy.allowed_topics or [])}
        if str(topic).strip().casefold() not in allowed_topics:
            raise OwnedGroupMessagingError(
                "INVALID_TRIGGER_CONFIG",
                "手动消息 topic 不在策略允许话题中",
                http_status=400,
            )
    template_id = _value(request, "template_id")
    if mode != MessageMode.TEMPLATE.value and template_id is not None:
        raise OwnedGroupMessagingError(
            "INVALID_TRIGGER_CONFIG",
            "AI 模式不接受 template_id",
            http_status=400,
        )
    if category == MessageContentCategory.PROMOTION.value and template_id is not None:
        configured = (policy.promotion_config or {}).get("default_template_id")
        if configured is None or int(template_id) != int(configured):
            raise OwnedGroupMessagingError(
                "PROMOTION_TEMPLATE_REQUIRED",
                "群内广告只能使用策略配置的默认模板",
                http_status=400,
            )

    scheduled_at = _value(request, "scheduled_at")
    if scheduled_at is not None:
        scheduled_at = _utc_naive(scheduled_at)
        now = datetime.utcnow()
        if scheduled_at > now + timedelta(days=7):
            raise OwnedGroupMessagingError(
                "INVALID_TRIGGER_CONFIG",
                "手动执行最多预约未来 7 天",
                http_status=400,
            )

    execution, created = await trigger_service.create_execution(
        target=target,
        policy=policy,
        trigger_type="manual",
        content_category=category,
        idempotency_key=durable_key,
        correlation_id=correlation_id,
        reply_to_message_id=_value(request, "reply_to_message_id"),
        template_id=template_id,
        topic=topic,
        prompt_context={
            "instruction": _value(request, "instruction"),
            "topic": topic,
            "variables": dict(_value(request, "variables", {}) or {}),
            "request_fingerprint": fingerprint,
        },
        scheduled_at=scheduled_at,
        requested_by=actor_id,
    )
    if not created:
        actual_fingerprint = dict(execution.prompt_context or {}).get("request_fingerprint")
        if (
            int(execution.owned_group_asset_id) != int(asset_id)
            or int(execution.policy_id) != int(policy.id)
            or int(execution.account_id) != int(policy.account_id)
            or _enum_value(execution.trigger_type) != "manual"
            or actual_fingerprint != fingerprint
        ):
            raise OwnedGroupMessagingError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key 已用于不同请求",
                details={"execution_id": int(execution.id)},
            )
    return ManualExecutionResult(
        execution_id=int(execution.id),
        status=_enum_value(execution.status),
        correlation_id=execution.correlation_id,
        scheduled_at=execution.scheduled_at,
        created=created,
    )


__all__ = [
    "ManualExecutionResult",
    "OwnedGroupMessageExecutionService",
    "create_manual_execution",
]
