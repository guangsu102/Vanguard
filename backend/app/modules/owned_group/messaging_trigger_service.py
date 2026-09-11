"""Trigger producers for stage-two owned-group messages.

All four trigger types end by creating the same immutable queued execution.
This module never calls Telegram and never asks AccountPool for an account.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload, undefer_group
from sqlalchemy.orm.attributes import set_committed_value

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.account.persona import (
    PERSONA_PROMPT_TEMPLATE_VERSION,
    AccountPersonaError,
    AccountPersonaService,
    PersonaSnapshotResult,
)
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.automation_settings import (
    get_group_ai_interaction_settings,
    get_owned_group_ai_persona_settings,
)
from app.core.config import settings as app_settings
from app.core.keyword.regex_guard import RegexTimeoutError, safe_compile, safe_search
from app.core.persona_observability import (
    record_persona_account_mismatch,
    record_persona_resolve,
    record_persona_snapshot,
)
from app.core.redis import RedisCache
from app.core.telegram_chat_lock import (
    acquire_telegram_chat_transaction_lock,
    telegram_chat_advisory_lock,
)
from app.modules.acquisition.auto_reply.semantic_reply import (
    SemanticGroupReplyEngine,
    SemanticMessage,
)
from app.modules.acquisition.models import KeywordTrigger, TriggerAction
from app.modules.owned_group.lock_queries import message_policy_for_update_query
from app.modules.owned_group.messaging_ai_budget import OwnedGroupAIBudgetGate
from app.modules.owned_group.messaging_contracts import (
    RESERVING_EXECUTION_STATUSES,
    MessageContentCategory,
    MessageExecutionStatus,
    MessageMode,
    MessagePurpose,
    OwnedGroupMessagingError,
)
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.messaging_prompt_context import (
    PROMPT_CONTEXT_MAX_TTL_SECONDS,
    PROMPT_CONTEXT_TTL_SECONDS,
    OwnedGroupPromptContextStore,
    requires_ephemeral_prompt_context,
    summarize_prompt_context,
    summary_requires_ephemeral_prompt_context,
)
from app.modules.owned_group.messaging_target import (
    OwnedGroupMessageTarget,
    OwnedGroupMessageTargetResolver,
)
from app.modules.owned_group.models_extra import (
    OwnedGroupAuditEvent,
    OwnedGroupMembership,
)
from app.modules.owned_group.security import redact_sensitive_value

logger = structlog.get_logger()
_DEFERRED_ELIGIBILITY_REASONS = {"membership_verification_stale"}
_SEMANTIC_THROTTLE_TTL_SECONDS = 48 * 3600
_SEMANTIC_THROTTLE_LUA = """
if redis.call('SETNX', KEYS[2], '1') == 0 then
    return 0
end
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if count % tonumber(ARGV[1]) == 0 then
    return 1
end
return 0
"""

SemanticEvaluator = Callable[
    [str, Sequence[Mapping[str, Any]], GroupAccountMessagePolicy],
    Awaitable[bool],
]


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


@dataclass(frozen=True, slots=True)
class IncomingOwnedGroupMessage:
    telegram_chat_id: int
    source_message_id: int
    sender_id: int
    sender_name: str
    text: str
    occurred_at: datetime
    listener_account_id: int | None = None
    reply_to_message_id: int | None = None
    reply_to_sender_id: int | None = None
    mentioned_usernames: tuple[str, ...] = ()
    mentioned_user_ids: tuple[int, ...] = ()
    recent_context: tuple[Mapping[str, Any], ...] = ()


class OwnedGroupMessageTriggerService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        semantic_evaluator: SemanticEvaluator | None = None,
        semantic_llm_client: LLMClient | None = None,
        cache: RedisCache | None = None,
        ai_budget_gate: OwnedGroupAIBudgetGate | None = None,
        prompt_context_store: OwnedGroupPromptContextStore | None = None,
    ) -> None:
        self.db = db
        self.semantic_evaluator = semantic_evaluator
        self.semantic_llm_client = semantic_llm_client
        self.cache = cache or RedisCache()
        self.ai_budget_gate = ai_budget_gate or OwnedGroupAIBudgetGate(self.cache)
        self.prompt_context_store = prompt_context_store or OwnedGroupPromptContextStore(
            self.cache
        )
        self.logger = logger.bind(module="owned_group_message_trigger")
        self.resolver = OwnedGroupMessageTargetResolver(db)

    async def _lock_sender_account(
        self,
        account_id: int,
    ) -> tuple[TelegramAccount, AccountOperationConfig | None]:
        """Use the global account -> operation-config row-lock order."""

        account = await self.db.scalar(
            select(TelegramAccount)
            .options(noload(TelegramAccount.operation_config))
            .where(TelegramAccount.id == int(account_id))
            .with_for_update(of=TelegramAccount)
            .execution_options(populate_existing=True)
        )
        if account is None:
            raise OwnedGroupMessagingError(
                "ACCOUNT_NOT_FOUND",
                "发送账号不存在",
                http_status=404,
            )
        operation_config = await self.db.scalar(
            select(AccountOperationConfig)
            .options(noload(AccountOperationConfig.account))
            .where(AccountOperationConfig.account_id == int(account_id))
            .with_for_update(of=AccountOperationConfig)
            .execution_options(populate_existing=True)
        )
        set_committed_value(account, "operation_config", operation_config)
        return account, operation_config

    async def _snapshot_persona(
        self,
        account: TelegramAccount,
        operation_config: AccountOperationConfig | None,
    ) -> PersonaSnapshotResult:
        loaded = await self.db.scalar(
            select(TelegramAccount)
            .options(
                noload(TelegramAccount.operation_config),
                undefer_group("account_persona"),
            )
            .where(TelegramAccount.id == int(account.id))
            .with_for_update(of=TelegramAccount)
            .execution_options(populate_existing=True)
        )
        if loaded is None:
            raise OwnedGroupMessagingError(
                "ACCOUNT_NOT_FOUND",
                "发送账号不存在",
                http_status=404,
            )
        set_committed_value(loaded, "operation_config", operation_config)
        feature = await get_owned_group_ai_persona_settings(self.db)
        try:
            snapshot = AccountPersonaService.snapshot_for_new_execution(
                loaded,
                feature_enabled=bool(feature["effectiveEnabled"]),
            )
        except AccountPersonaError as exc:
            record_persona_resolve(
                account_id=int(account.id),
                persona_source="unknown",
                revision=0,
                persona_hash=None,
                result="failed",
                reason_code=exc.code,
            )
            raise OwnedGroupMessagingError(
                exc.code,
                exc.message,
                http_status=exc.http_status,
                details=exc.details,
            ) from exc
        record_persona_resolve(
            account_id=int(snapshot.account_id),
            persona_source=str(snapshot.source.value),
            revision=int(snapshot.revision),
            persona_hash=str(snapshot.persona_hash),
            result="success",
        )
        return snapshot

    @staticmethod
    def _clean_business_text(value: Any, *, max_length: int) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        clean = "".join(
            character
            for character in normalized
            if character in {"\n", "\r", "\t"}
            or not unicodedata.category(character).startswith("C")
        )
        return " ".join(clean.replace("\r", "\n").splitlines()).strip()[:max_length]

    @classmethod
    def _business_snapshot(
        cls,
        *,
        policy: GroupAccountMessagePolicy,
        target: OwnedGroupMessageTarget,
    ) -> dict[str, Any]:
        allowed_topics: list[str] = []
        seen: set[str] = set()
        for item in policy.allowed_topics or ():
            value = cls._clean_business_text(item, max_length=100)
            if not value or value.casefold() in seen:
                continue
            seen.add(value.casefold())
            allowed_topics.append(value)
            if len(allowed_topics) >= 20:
                break
        return {
            "allowed_topics": allowed_topics,
            "group_title": cls._clean_business_text(
                target.group_title,
                max_length=255,
            ),
        }

    async def create_execution(
        self,
        *,
        target: OwnedGroupMessageTarget,
        policy: GroupAccountMessagePolicy,
        trigger_type: str,
        content_category: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        source_message_id: int | None = None,
        reply_to_message_id: int | None = None,
        keyword_trigger_id: int | None = None,
        template_id: int | None = None,
        topic: str | None = None,
        prompt_context: Mapping[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        requested_by: int | None = None,
    ) -> tuple[GroupAccountMessageExecution, bool]:
        """Create one queued execution, returning the existing row on replay."""

        category = str(content_category)
        ephemeral_context = self._redact_prompt_context(prompt_context or {})
        existing = await self.db.scalar(
            select(GroupAccountMessageExecution).where(
                GroupAccountMessageExecution.idempotency_key == str(idempotency_key)
            )
        )
        if existing is not None:
            self._assert_idempotency_match(
                existing,
                target=target,
                policy=policy,
                trigger_type=trigger_type,
                prompt_context=prompt_context or {},
            )
            await self._rehydrate_existing_prompt_context(existing, ephemeral_context)
            return existing, False

        # Serialize policy mutation and execution creation on the same Telegram
        # chat. Re-read the policy after acquiring the lock so a request that
        # started before disable/re-enable cannot enqueue work with a stale
        # policy revision.
        await acquire_telegram_chat_transaction_lock(self.db, int(target.telegram_chat_id))
        existing = await self.db.scalar(
            select(GroupAccountMessageExecution).where(
                GroupAccountMessageExecution.idempotency_key == str(idempotency_key)
            )
        )
        if existing is not None:
            self._assert_idempotency_match(
                existing,
                target=target,
                policy=policy,
                trigger_type=trigger_type,
                prompt_context=prompt_context or {},
            )
            await self._rehydrate_existing_prompt_context(existing, ephemeral_context)
            return existing, False

        current_policy = await self.db.scalar(
            message_policy_for_update_query()
            .where(
                GroupAccountMessagePolicy.id == int(policy.id),
                GroupAccountMessagePolicy.owned_group_asset_id
                == int(target.owned_group_asset_id),
                GroupAccountMessagePolicy.account_id == int(policy.account_id),
            )
            .execution_options(populate_existing=True)
        )
        if current_policy is None:
            raise OwnedGroupMessagingError("POLICY_NOT_FOUND", "消息策略不存在", http_status=404)
        if int(current_policy.revision) != int(policy.revision):
            raise OwnedGroupMessagingError(
                "POLICY_REVISION_CONFLICT",
                "消息策略已在请求期间变更，请重新触发",
                details={
                    "policy_id": int(policy.id),
                    "requested_revision": int(policy.revision),
                    "current_revision": int(current_policy.revision),
                },
            )
        if not bool(current_policy.enabled):
            raise OwnedGroupMessagingError("POLICY_DISABLED", "消息策略已停用")
        sender_account, operation_config = await self._lock_sender_account(
            int(current_policy.account_id)
        )
        policy = current_policy

        mode = self.mode_for_category(policy, category)
        if mode == MessageMode.OFF.value:
            code = (
                "OWNED_GROUP_PROMOTION_DISABLED"
                if category == MessageContentCategory.PROMOTION.value
                else "MESSAGE_MODE_DISABLED"
            )
            raise OwnedGroupMessagingError(code, "当前消息类别未启用")
        ephemeral_context["generation_policy_snapshot"] = {
            "mode": mode,
            "require_review": bool(policy.require_review),
            "allowed_topics": [str(item) for item in (policy.allowed_topics or [])],
            "default_template_id": (
                int(policy.default_template_id) if policy.default_template_id else None
            ),
        }
        purpose = MessagePurpose.COMMUNITY_AI.value
        initial_status = MessageExecutionStatus.QUEUED.value
        frozen_content: str | None = None
        frozen_content_hash: str | None = None
        persona_revision_snapshot: int | None = None
        persona_source_snapshot: str | None = None
        persona_snapshot_payload: dict[str, Any] | None = None
        persona_hash: str | None = None
        prompt_template_version: str | None = None
        frozen_promotion_config = (
            dict(policy.promotion_config or {})
            if category == MessageContentCategory.PROMOTION.value
            else None
        )
        if template_id is None and mode == MessageMode.TEMPLATE.value:
            template_id = self.default_template_for_category(policy, category)
        if mode == MessageMode.AI.value:
            persona = await self._snapshot_persona(sender_account, operation_config)
            if int(persona.account_id) != int(policy.account_id):
                record_persona_account_mismatch(
                    account_id=int(policy.account_id),
                    persona_source=str(persona.source.value),
                    revision=int(persona.revision),
                    persona_hash=str(persona.persona_hash),
                    request_id=correlation_id,
                )
                raise OwnedGroupMessagingError(
                    "PERSONA_ACCOUNT_MISMATCH",
                    "Persona 与最终发送账号不一致",
                )
            record_persona_snapshot(
                account_id=int(persona.account_id),
                asset_id=int(target.owned_group_asset_id),
                core_group_id=int(target.core_group_id),
                persona_source=str(persona.source.value),
                revision=int(persona.revision),
                persona_hash=str(persona.persona_hash),
                request_id=correlation_id,
            )
            persona_revision_snapshot = int(persona.revision)
            persona_source_snapshot = str(persona.source.value)
            persona_snapshot_payload = dict(persona.persona.model_dump(mode="json"))
            persona_hash = str(persona.persona_hash)
            prompt_template_version = PERSONA_PROMPT_TEMPLATE_VERSION
            ephemeral_context.pop("business_snapshot_v1", None)
            ephemeral_context["business_snapshot_v1"] = self._business_snapshot(
                policy=policy,
                target=target,
            )
            from app.modules.owned_group.messaging_content_service import (
                OwnedGroupMessageContentService,
            )

            topic = await OwnedGroupMessageContentService(self.db)._select_topic(
                policy,
                topic,
                trigger_type=str(trigger_type),
                mode=mode,
            )
        elif mode == MessageMode.TEMPLATE.value:
            # Render and validate templates while the execution is created.
            # A future scheduled_at delays only delivery; later template edits
            # must never mutate already accepted work.
            from app.modules.owned_group.messaging_content_service import (
                OwnedGroupMessageContentService,
            )

            frozen = await OwnedGroupMessageContentService(self.db).generate(
                target=target,
                policy=policy,
                trigger_type=str(trigger_type),
                content_category=category,
                template_id=template_id,
                variables=(
                    ephemeral_context.get("variables")
                    if isinstance(ephemeral_context.get("variables"), Mapping)
                    else {}
                ),
                topic=topic,
                user_name=str(ephemeral_context.get("user_name") or "") or None,
                source_text=str(ephemeral_context.get("source_text") or "") or None,
                recent_context=(
                    ephemeral_context.get("recent_context")
                    if isinstance(ephemeral_context.get("recent_context"), list)
                    else ()
                ),
                matched_keyword=(
                    str(ephemeral_context.get("matched_keyword") or "") or None
                ),
                keyword_requires_review=bool(
                    ephemeral_context.get("keyword_requires_review", False)
                ),
            )
            purpose = frozen.message_purpose
            template_id = frozen.template_id
            topic = frozen.topic
            frozen_content = frozen.content
            frozen_content_hash = frozen.content_hash
            frozen_promotion_config = frozen.promotion_config_snapshot
            initial_status = (
                MessageExecutionStatus.PENDING_REVIEW.value
                if frozen.would_require_review
                else MessageExecutionStatus.READY_TO_SEND.value
            )

        execution = GroupAccountMessageExecution(
            policy_id=int(policy.id),
            owned_group_asset_id=int(target.owned_group_asset_id),
            core_group_id=int(target.core_group_id),
            telegram_chat_id=int(target.telegram_chat_id),
            account_id=int(policy.account_id),
            trigger_type=str(trigger_type),
            message_purpose=purpose,
            content_category=category,
            mode_snapshot=mode,
            policy_revision=int(policy.revision),
            persona_revision_snapshot=persona_revision_snapshot,
            persona_source_snapshot=persona_source_snapshot,
            persona_snapshot=persona_snapshot_payload,
            persona_hash=persona_hash,
            prompt_template_version=prompt_template_version,
            prompt_hash=None,
            governance_rules_hash=None,
            status=initial_status,
            source_message_id=source_message_id,
            reply_to_message_id=reply_to_message_id,
            keyword_trigger_id=keyword_trigger_id,
            template_id=template_id,
            topic=topic,
            prompt_context=summarize_prompt_context(ephemeral_context),
            promotion_config_snapshot=frozen_promotion_config,
            content=frozen_content,
            content_hash=frozen_content_hash,
            idempotency_key=str(idempotency_key)[:128],
            correlation_id=(correlation_id or uuid.uuid4().hex)[:128],
            scheduled_at=scheduled_at,
            requested_by=requested_by,
        )
        self.db.add(execution)
        stored_context_id: int | None = None
        try:
            await self.db.flush()
            if mode == MessageMode.AI.value and requires_ephemeral_prompt_context(
                ephemeral_context
            ):
                await self.prompt_context_store.save(
                    int(execution.id),
                    ephemeral_context,
                    ttl_seconds=self._prompt_context_ttl(scheduled_at),
                )
                stored_context_id = int(execution.id)
            self._add_audit(
                execution,
                "message_execution_created",
                after={
                    "trigger_type": trigger_type,
                    "content_category": category,
                    "mode_snapshot": mode,
                    "policy_revision": int(policy.revision),
                    "persona_source": persona_source_snapshot,
                    "persona_revision": persona_revision_snapshot,
                    "persona_hash_prefix": persona_hash[:12] if persona_hash else None,
                    "prompt_template_version": prompt_template_version,
                    "idempotency_key": str(idempotency_key),
                },
            )
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing = await self.db.scalar(
                select(GroupAccountMessageExecution).where(
                    GroupAccountMessageExecution.idempotency_key == str(idempotency_key)
                )
            )
            if existing is None:
                if stored_context_id is not None:
                    await self.prompt_context_store.discard(stored_context_id)
                raise
            self._assert_idempotency_match(
                existing,
                target=target,
                policy=policy,
                trigger_type=trigger_type,
                prompt_context=prompt_context or {},
            )
            await self._rehydrate_existing_prompt_context(existing, ephemeral_context)
            if stored_context_id is not None and stored_context_id != int(existing.id):
                await self.prompt_context_store.discard(stored_context_id)
            return existing, False
        except Exception as exc:
            await self.db.rollback()
            try:
                existing = await self.db.scalar(
                    select(GroupAccountMessageExecution).where(
                        GroupAccountMessageExecution.idempotency_key == str(idempotency_key)
                    )
                )
            except Exception as lookup_error:
                # Commit outcome is unknown. Preserve the encrypted context for
                # its bounded TTL so a later idempotent replay can recover it.
                raise exc from lookup_error
            if existing is None:
                if stored_context_id is not None:
                    await self.prompt_context_store.discard(stored_context_id)
                raise exc
            self._assert_idempotency_match(
                existing,
                target=target,
                policy=policy,
                trigger_type=trigger_type,
                prompt_context=prompt_context or {},
            )
            await self._rehydrate_existing_prompt_context(existing, ephemeral_context)
            if stored_context_id is not None and stored_context_id != int(existing.id):
                await self.prompt_context_store.discard(stored_context_id)
            return existing, False
        await self.db.refresh(execution)
        return execution, True

    async def create_due_scheduled_executions(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[GroupAccountMessageExecution]:
        now_utc = now or datetime.utcnow()
        aware_utc = now_utc.replace(tzinfo=UTC) if now_utc.tzinfo is None else now_utc
        local_now = aware_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        created: list[GroupAccountMessageExecution] = []
        page_size = max(50, max(1, int(limit)) * 4)
        last_policy_id = 0
        scan_ceiling = await self.db.scalar(
            select(func.max(GroupAccountMessagePolicy.id)).where(
                GroupAccountMessagePolicy.enabled.is_(True)
            )
        )
        if scan_ceiling is None:
            return created
        scan_ceiling = int(scan_ceiling)
        # Scan the complete current minute. ``limit`` only controls the keyset
        # page size: capping created rows would permanently lose policies after
        # the clock advances to the next HH:MM slot. Freeze the current max id
        # so concurrent inserts are picked up by the next tick instead of making
        # this scan chase a moving tail forever.
        while last_policy_id < scan_ceiling:
            policies = list(
                (
                    await self.db.scalars(
                        select(GroupAccountMessagePolicy)
                        .where(
                            GroupAccountMessagePolicy.enabled.is_(True),
                            GroupAccountMessagePolicy.id > last_policy_id,
                            GroupAccountMessagePolicy.id <= scan_ceiling,
                        )
                        .order_by(GroupAccountMessagePolicy.id)
                        .limit(page_size)
                    )
                ).all()
            )
            if not policies:
                break
            last_policy_id = int(policies[-1].id)

            for policy in policies:
                scheduled = dict((policy.trigger_config or {}).get("scheduled") or {})
                if not bool(scheduled.get("enabled")):
                    continue
                if local_now.isoweekday() not in {
                    int(item) for item in scheduled.get("weekdays") or []
                }:
                    continue
                slot = local_now.strftime("%H:%M")
                if slot not in {str(item) for item in scheduled.get("times") or []}:
                    continue
                category = str(
                    scheduled.get("content_category") or MessageContentCategory.COMMUNITY.value
                )
                if self.mode_for_category(policy, category) == MessageMode.OFF.value:
                    continue
                try:
                    target = await self.resolver.resolve(policy.owned_group_asset_id)
                except OwnedGroupMessagingError:
                    continue
                eligibility = await self.resolver.validate_account_eligibility(
                    policy.owned_group_asset_id,
                    policy.account_id,
                    probe_stale=False,
                )
                if set(eligibility.blocking_reasons) - _DEFERRED_ELIGIBILITY_REASONS:
                    continue
                local_slot = local_now.replace(second=0, microsecond=0)
                slot_key = local_slot.strftime("%Y%m%d%H%M")
                key = f"schedule:{policy.id}:{slot_key}"
                jitter_max = max(
                    0,
                    min(int(scheduled.get("jitter_seconds") or 0), 900),
                )
                jitter = (
                    int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % (jitter_max + 1)
                    if jitter_max
                    else 0
                )
                scheduled_at = local_slot.astimezone(UTC).replace(tzinfo=None) + timedelta(
                    seconds=jitter
                )
                execution, was_created = await self.create_execution(
                    target=target,
                    policy=policy,
                    trigger_type="scheduled",
                    content_category=category,
                    idempotency_key=key,
                    scheduled_at=scheduled_at,
                    prompt_context={"scheduled_slot": slot_key},
                )
                if was_created:
                    created.append(execution)
        return created

    async def handle_incoming_message(
        self,
        *,
        asset_id: int,
        message: IncomingOwnedGroupMessage,
    ) -> GroupAccountMessageExecution | None:
        target = await self.resolver.resolve(int(asset_id))
        policies = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessagePolicy)
                    .where(
                        GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id),
                        GroupAccountMessagePolicy.enabled.is_(True),
                    )
                    .order_by(GroupAccountMessagePolicy.id)
                )
            ).all()
        )
        if not policies:
            return None

        keyword_result = await self._keyword_execution(
            target=target,
            policies=policies,
            message=message,
        )
        if keyword_result is not None:
            return keyword_result
        return await self._reply_execution(
            target=target,
            policies=policies,
            message=message,
        )

    async def _keyword_execution(
        self,
        *,
        target: OwnedGroupMessageTarget,
        policies: list[GroupAccountMessagePolicy],
        message: IncomingOwnedGroupMessage,
    ) -> GroupAccountMessageExecution | None:
        ids: set[int] = set()
        for policy in policies:
            config = dict((policy.trigger_config or {}).get("keyword") or {})
            if config.get("enabled"):
                ids.update(int(item) for item in config.get("trigger_ids") or [])
        if not ids:
            return None
        triggers = list(
            (
                await self.db.scalars(
                    select(KeywordTrigger).where(
                        KeywordTrigger.id.in_(sorted(ids)),
                        KeywordTrigger.enabled.is_(True),
                    )
                )
            ).all()
        )
        matched: list[KeywordTrigger] = []
        for trigger in triggers:
            if await self._matches_trigger(trigger, message.text):
                matched.append(trigger)
        matched.sort(key=lambda item: (-int(item.priority or 0), int(item.id)))
        if not matched:
            return None

        # Priority selection is final for this source message. If the winning
        # trigger cannot execute, never fall through to a lower-priority match.
        trigger = matched[0]
        async with telegram_chat_advisory_lock(self.db, target.telegram_chat_id):
            return await self._create_keyword_execution_locked(
                target=target,
                policies=policies,
                message=message,
                trigger=trigger,
            )

    async def _create_keyword_execution_locked(
        self,
        *,
        target: OwnedGroupMessageTarget,
        policies: list[GroupAccountMessagePolicy],
        message: IncomingOwnedGroupMessage,
        trigger: KeywordTrigger,
    ) -> GroupAccountMessageExecution | None:
        """Recheck trigger cooldown and reserve while the chat lock is held."""

        candidates = []
        for policy in policies:
            config = dict((policy.trigger_config or {}).get("keyword") or {})
            if not bool(config.get("enabled")) or int(trigger.id) not in {
                int(item) for item in config.get("trigger_ids") or []
            }:
                continue
            category = str(config.get("content_category") or "community")
            mode = self.mode_for_category(policy, category)
            expected_action = (
                TriggerAction.REPLY_TEMPLATE.value
                if mode == MessageMode.TEMPLATE.value
                else TriggerAction.REPLY_AI.value
            )
            if _enum_value(trigger.action) != expected_action:
                continue
            if await self._keyword_cooldown_active(target, trigger):
                continue
            candidates.append(policy)
        policy = await self._choose_policy(candidates)
        if policy is None:
            return None
        config = dict((policy.trigger_config or {}).get("keyword") or {})
        category = str(config.get("content_category") or "community")
        key = f"keyword:{target.owned_group_asset_id}:{message.source_message_id}:{trigger.id}"
        execution, _ = await self.create_execution(
            target=target,
            policy=policy,
            trigger_type="keyword",
            content_category=category,
            idempotency_key=key,
            source_message_id=int(message.source_message_id),
            reply_to_message_id=(
                int(message.source_message_id)
                if bool(config.get("reply_to_source", True))
                else None
            ),
            keyword_trigger_id=int(trigger.id),
            prompt_context={
                "source_text": message.text[:1000],
                "user_name": message.sender_name[:100],
                "recent_context": list(message.recent_context)[-20:],
                "matched_keyword": trigger.keyword_text,
                "keyword_requires_review": bool(trigger.requires_review),
            },
        )
        return execution

    async def _reply_execution(
        self,
        *,
        target: OwnedGroupMessageTarget,
        policies: list[GroupAccountMessagePolicy],
        message: IncomingOwnedGroupMessage,
    ) -> GroupAccountMessageExecution | None:
        candidates: list[GroupAccountMessagePolicy] = []
        strategies: dict[int, str] = {}
        semantic_due: bool | None = None
        semantic_settings: Mapping[str, Any] | None = None
        for policy in policies:
            config = dict((policy.trigger_config or {}).get("reply") or {})
            if not bool(config.get("enabled")):
                continue
            category = str(config.get("content_category") or "community")
            mode = self.mode_for_category(policy, category)
            if mode == MessageMode.OFF.value:
                continue
            strategy = str(config.get("strategy") or "directed")
            if strategy == "directed":
                if not await self._is_directed_at_policy(
                    target=target,
                    policy=policy,
                    message=message,
                ):
                    continue
            elif strategy == "semantic":
                if mode != MessageMode.AI.value:
                    continue
                if semantic_settings is None:
                    semantic_settings = await get_group_ai_interaction_settings(self.db)
                if not bool(semantic_settings.get("enabled")) or not bool(
                    semantic_settings.get("allowSemanticTriggeredReply")
                ):
                    continue
                if semantic_due is None:
                    semantic_due = await self._semantic_evaluation_due(
                        asset_id=target.owned_group_asset_id,
                        source_message_id=message.source_message_id,
                        settings=semantic_settings,
                    )
                if not semantic_due:
                    continue
                if not await self._semantic_match(
                    message,
                    policy,
                    settings=semantic_settings,
                    minimum=float(config.get("semantic_min_confidence") or 0.75),
                ):
                    continue
            else:
                continue
            candidates.append(policy)
            strategies[int(policy.id)] = strategy

        policy = await self._choose_policy(candidates)
        if policy is None:
            return None
        config = dict((policy.trigger_config or {}).get("reply") or {})
        category = str(config.get("content_category") or "community")
        strategy = strategies[int(policy.id)]
        key = f"reply:{target.owned_group_asset_id}:{message.source_message_id}:{strategy}"
        execution, _ = await self.create_execution(
            target=target,
            policy=policy,
            trigger_type="reply",
            content_category=category,
            idempotency_key=key,
            source_message_id=int(message.source_message_id),
            reply_to_message_id=int(message.source_message_id),
            prompt_context={
                "source_text": message.text[:1000],
                "user_name": message.sender_name[:100],
                "recent_context": list(message.recent_context)[
                    -max(1, min(int(config.get("context_messages") or 6), 20)) :
                ],
                "strategy": strategy,
            },
        )
        return execution

    async def _choose_policy(
        self,
        policies: Sequence[GroupAccountMessagePolicy],
    ) -> GroupAccountMessagePolicy | None:
        ranked: list[tuple[datetime, int, GroupAccountMessagePolicy]] = []
        for policy in policies:
            eligibility = await self.resolver.validate_account_eligibility(
                policy.owned_group_asset_id,
                policy.account_id,
                probe_stale=False,
            )
            if set(eligibility.blocking_reasons) - _DEFERRED_ELIGIBILITY_REASONS:
                continue
            last_sent = await self.db.scalar(
                select(func.max(GroupAccountMessageExecution.sent_at)).where(
                    GroupAccountMessageExecution.policy_id == int(policy.id),
                    GroupAccountMessageExecution.status == MessageExecutionStatus.SENT.value,
                )
            )
            ranked.append((last_sent or datetime.min, int(policy.id), policy))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        return ranked[0][2]

    async def _is_directed_at_policy(
        self,
        *,
        target: OwnedGroupMessageTarget,
        policy: GroupAccountMessagePolicy,
        message: IncomingOwnedGroupMessage,
    ) -> bool:
        membership_user_id = await self.db.scalar(
            select(OwnedGroupMembership.telegram_user_id).where(
                OwnedGroupMembership.group_asset_id == target.owned_group_asset_id,
                OwnedGroupMembership.resource_type == "user",
                OwnedGroupMembership.resource_id == int(policy.account_id),
            )
        )
        if (
            membership_user_id is not None
            and message.reply_to_sender_id is not None
            and int(membership_user_id) == int(message.reply_to_sender_id)
        ):
            return True
        if membership_user_id is not None and int(membership_user_id) in set(
            message.mentioned_user_ids
        ):
            return True
        if message.reply_to_message_id is not None:
            prior = await self.db.scalar(
                select(GroupAccountMessageExecution.id).where(
                    GroupAccountMessageExecution.owned_group_asset_id
                    == int(target.owned_group_asset_id),
                    GroupAccountMessageExecution.policy_id == int(policy.id),
                    GroupAccountMessageExecution.account_id == int(policy.account_id),
                    GroupAccountMessageExecution.telegram_message_id
                    == int(message.reply_to_message_id),
                    GroupAccountMessageExecution.status == MessageExecutionStatus.SENT.value,
                    GroupAccountMessageExecution.sent_at
                    >= datetime.utcnow() - timedelta(days=7),
                )
            )
            if prior is not None:
                return True
        return False

    async def _semantic_match(
        self,
        message: IncomingOwnedGroupMessage,
        policy: GroupAccountMessagePolicy,
        *,
        settings: Mapping[str, Any],
        minimum: float,
    ) -> bool:
        if self.semantic_evaluator is not None:
            await self.ai_budget_gate.reserve(settings, max_tokens_cap=260)
            return bool(
                await self.semantic_evaluator(
                    message.text,
                    message.recent_context,
                    policy,
                )
            )

        client = self.semantic_llm_client or self._build_llm_client()
        if client is None:
            self.logger.warning(
                "owned_group_semantic_provider_unavailable",
                policy_id=int(policy.id),
            )
            return False

        await self.ai_budget_gate.reserve(settings, max_tokens_cap=260)
        rows: list[SemanticMessage] = []
        semantic_window = max(
            1,
            min(int(settings.get("semanticScanWindowMessages") or 100), 100),
        )
        for row in list(message.recent_context)[-semantic_window:]:
            message_id = self._safe_int(row.get("message_id"))
            text = str(row.get("text") or "").strip()
            if message_id is None or not text:
                continue
            rows.append(
                SemanticMessage(
                    message_id=message_id,
                    user_id=self._safe_int(row.get("user_id")) or 0,
                    user_name=str(row.get("user_name") or "")[:100],
                    text=text[:1000],
                    timestamp=self._context_timestamp(
                        row.get("occurred_at") or row.get("timestamp"),
                        fallback=message.occurred_at,
                    ),
                )
            )
        rows.append(
            SemanticMessage(
                message_id=int(message.source_message_id),
                user_id=int(message.sender_id),
                user_name=str(message.sender_name or "")[:100],
                text=str(message.text or "")[:1000],
                timestamp=message.occurred_at,
            )
        )
        try:
            decision = await SemanticGroupReplyEngine.evaluate_only(
                llm_client=client,
                messages=rows,
                settings=settings,
                allowed_topics=policy.allowed_topics or (),
                minimum_confidence=minimum,
                required_target_message_id=int(message.source_message_id),
            )
        except Exception as exc:
            self.logger.warning(
                "owned_group_semantic_evaluation_failed",
                policy_id=int(policy.id),
                error_type=type(exc).__name__,
            )
            return False
        return bool(decision.should_reply)

    async def _semantic_evaluation_due(
        self,
        *,
        asset_id: int,
        source_message_id: int,
        settings: Mapping[str, Any],
    ) -> bool:
        interval = max(
            1,
            min(int(settings.get("semanticEvaluateEveryMessages") or 100), 100),
        )
        client = getattr(self.cache, "client", None)
        if client is None:
            self.logger.warning(
                "owned_group_semantic_throttle_unavailable",
                asset_id=int(asset_id),
            )
            return False
        hash_tag = f"{{{int(asset_id)}}}"
        counter_key = f"owned_group:message:semantic:{hash_tag}:count"
        seen_key = (
            f"owned_group:message:semantic:{hash_tag}:seen:{int(source_message_id)}"
        )
        try:
            result = await client.eval(
                _SEMANTIC_THROTTLE_LUA,
                2,
                counter_key,
                seen_key,
                interval,
                _SEMANTIC_THROTTLE_TTL_SECONDS,
            )
            return int(result) == 1
        except Exception as exc:
            self.logger.warning(
                "owned_group_semantic_throttle_unavailable",
                asset_id=int(asset_id),
                error_type=type(exc).__name__,
            )
            return False

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _context_timestamp(value: Any, *, fallback: datetime) -> datetime:
        if isinstance(value, datetime):
            return value
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                pass
        return fallback

    @staticmethod
    def _build_llm_client() -> LLMClient | None:
        provider_value = str(app_settings.LLM_PROVIDER or LLMProvider.OPENAI.value)
        try:
            provider = LLMProvider(provider_value)
        except ValueError:
            provider = LLMProvider.OPENAI
        api_key = (
            app_settings.OPENAI_API_KEY
            if provider == LLMProvider.OPENAI
            else app_settings.ANTHROPIC_API_KEY
        )
        if provider != LLMProvider.LOCAL and not api_key:
            return None
        return LLMClient(provider=provider, api_key=api_key)

    async def _matches_trigger(self, trigger: KeywordTrigger, text: str) -> bool:
        pattern_text = str(trigger.keyword_text or "")
        if not pattern_text:
            return False
        try:
            pattern = safe_compile(pattern_text, re.IGNORECASE)
            return bool(await safe_search(pattern, str(text or "")[:10000], timeout=0.5))
        except (ValueError, re.error, RegexTimeoutError):
            return pattern_text.casefold() in str(text or "").casefold()

    async def _keyword_cooldown_active(
        self,
        target: OwnedGroupMessageTarget,
        trigger: KeywordTrigger,
    ) -> bool:
        cooldown = max(0, int(trigger.cooldown_seconds or 0))
        if cooldown <= 0:
            return False
        recent = await self.db.scalar(
            select(GroupAccountMessageExecution.id)
            .where(
                GroupAccountMessageExecution.core_group_id == target.core_group_id,
                GroupAccountMessageExecution.keyword_trigger_id == int(trigger.id),
                GroupAccountMessageExecution.status.in_(
                    {
                        *RESERVING_EXECUTION_STATUSES,
                        MessageExecutionStatus.QUEUED.value,
                        MessageExecutionStatus.GENERATING.value,
                    }
                ),
                GroupAccountMessageExecution.created_at
                >= datetime.utcnow() - timedelta(seconds=cooldown),
            )
            .limit(1)
        )
        return recent is not None

    @staticmethod
    def mode_for_category(policy: GroupAccountMessagePolicy, category: str) -> str:
        if category == MessageContentCategory.COMMUNITY.value:
            return _enum_value(policy.mode)
        if category == MessageContentCategory.PROMOTION.value:
            return str((policy.promotion_config or {}).get("mode") or MessageMode.OFF.value)
        raise OwnedGroupMessagingError(
            "INVALID_TRIGGER_CONFIG",
            "消息内容类别无效",
            http_status=400,
        )

    @staticmethod
    def default_template_for_category(
        policy: GroupAccountMessagePolicy,
        category: str,
    ) -> int | None:
        if category == MessageContentCategory.COMMUNITY.value:
            return int(policy.default_template_id) if policy.default_template_id else None
        value = (policy.promotion_config or {}).get("default_template_id")
        return int(value) if value else None

    @staticmethod
    def _redact_prompt_context(value: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "instruction",
            "topic",
            "variables",
            "source_text",
            "user_name",
            "recent_context",
            "matched_keyword",
            "keyword_requires_review",
            "strategy",
            "scheduled_slot",
            "request_fingerprint",
            "generation_policy_snapshot",
        }
        redacted = redact_sensitive_value(
            {key: value[key] for key in allowed if key in value},
            max_length=1000,
        )
        serialized = json.dumps(redacted, ensure_ascii=False, default=str)
        if len(serialized) > 12000:
            redacted.pop("recent_context", None)
        # Convert datetime-like values to JSON-safe strings before handing the
        # payload to RedisCache.set_json.
        return json.loads(json.dumps(redacted, ensure_ascii=False, default=str))

    @staticmethod
    def _prompt_context_ttl(scheduled_at: datetime | None) -> int:
        if scheduled_at is None:
            return PROMPT_CONTEXT_TTL_SECONDS
        target = scheduled_at
        if target.tzinfo is not None:
            target = target.astimezone(UTC).replace(tzinfo=None)
        delay = max(0, int((target - datetime.utcnow()).total_seconds()))
        return min(
            PROMPT_CONTEXT_MAX_TTL_SECONDS,
            PROMPT_CONTEXT_TTL_SECONDS + delay,
        )

    async def _rehydrate_existing_prompt_context(
        self,
        existing: GroupAccountMessageExecution,
        ephemeral_context: Mapping[str, Any],
    ) -> None:
        """Restore a replayed queued execution's encrypted Redis companion."""

        if _enum_value(existing.status) not in {
            MessageExecutionStatus.QUEUED.value,
            MessageExecutionStatus.GENERATING.value,
        }:
            return
        if not requires_ephemeral_prompt_context(ephemeral_context):
            return
        if not summary_requires_ephemeral_prompt_context(existing.prompt_context):
            return
        await self.prompt_context_store.save(
            int(existing.id),
            ephemeral_context,
            ttl_seconds=self._prompt_context_ttl(existing.scheduled_at),
        )

    @staticmethod
    def _assert_idempotency_match(
        existing: GroupAccountMessageExecution,
        *,
        target: OwnedGroupMessageTarget,
        policy: GroupAccountMessagePolicy,
        trigger_type: str,
        prompt_context: Mapping[str, Any],
    ) -> None:
        expected_fingerprint = prompt_context.get("request_fingerprint")
        actual_fingerprint = dict(existing.prompt_context or {}).get("request_fingerprint")
        if (
            int(existing.owned_group_asset_id) != int(target.owned_group_asset_id)
            or int(existing.policy_id) != int(policy.id)
            or int(existing.account_id) != int(policy.account_id)
            or str(existing.trigger_type) != str(trigger_type)
            or actual_fingerprint != expected_fingerprint
        ):
            raise OwnedGroupMessagingError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key 已用于不同请求",
                details={"execution_id": int(existing.id)},
            )

    def _add_audit(
        self,
        execution: GroupAccountMessageExecution,
        event_type: str,
        *,
        after: Mapping[str, Any],
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
                        **dict(after),
                        "account_id": int(execution.account_id),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                result="success",
                correlation_id=execution.correlation_id,
            )
        )


async def create_scheduled_executions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[GroupAccountMessageExecution]:
    return await OwnedGroupMessageTriggerService(db).create_due_scheduled_executions(
        now=now,
        limit=limit,
    )


__all__ = [
    "IncomingOwnedGroupMessage",
    "OwnedGroupMessageTriggerService",
    "create_scheduled_executions",
]
