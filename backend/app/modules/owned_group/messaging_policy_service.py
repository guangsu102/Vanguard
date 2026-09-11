"""Policy, template, eligibility and review queries for owned-group messaging."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Integer, desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountType,
    TelegramAccount,
)
from app.core.account.persona import (
    AccountPersonaError,
    AccountPersonaService,
    hash_persona,
    normalize_persona_payload,
)
from app.core.automation_settings import (
    get_owned_group_ai_persona_settings,
    get_owned_group_messaging_settings,
)
from app.core.config import settings
from app.core.operating_time import operating_day_start
from app.core.telegram_chat_lock import (
    acquire_telegram_chat_transaction_lock,
    telegram_chat_advisory_lock,
)
from app.modules.acquisition.models import (
    KeywordTrigger,
    MessageTemplate,
    MessageType,
    TriggerAction,
)
from app.modules.owned_group.lock_queries import message_execution_for_update_query
from app.modules.owned_group.messaging_content_safety import contains_url_like
from app.modules.owned_group.messaging_contracts import (
    ALLOWED_OWNED_GROUP_TEMPLATE_VARIABLES,
    RESERVING_EXECUTION_STATUSES,
    UNSENT_CANCELLABLE_STATUSES,
    MessageExecutionStatus,
    MessageMode,
    OwnedGroupMessagingError,
    normalize_message_content,
)
from app.modules.owned_group.messaging_governance_integration import (
    govern_review_override,
)
from app.modules.owned_group.messaging_models import (
    GroupAccountMessageExecution,
    GroupAccountMessagePolicy,
)
from app.modules.owned_group.messaging_prompt_context import (
    OwnedGroupPromptContextStore,
    summarize_prompt_context,
)
from app.modules.owned_group.messaging_schemas import (
    ExecutionApprove,
    ExecutionReject,
    OwnedGroupTemplateWrite,
    PolicyCreate,
    PolicyUpdate,
)
from app.modules.owned_group.messaging_target import OwnedGroupMessageTargetResolver
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent
from app.modules.owned_group.security import redact_sensitive_text, redact_sensitive_value

_TEMPLATE_VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_TEMPLATE_EXPRESSION_RE = re.compile(r"{{(.*?)}}", re.DOTALL)
_TEMPLATE_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LOWER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PERSONA_SNAPSHOT_SOURCES = {
    "configured",
    "neutral_default",
    "feature_disabled_default",
    "legacy_default",
}


def _value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _actor_id(actor: Any) -> int | None:
    if actor is None:
        return None
    if isinstance(actor, dict):
        value = actor.get("id") or actor.get("user_id")
    else:
        value = getattr(actor, "id", None) or getattr(actor, "user_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() + ("Z" if value.tzinfo is None else "")


def _json_snapshot(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        redact_sensitive_value(value),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


class OwnedGroupMessagingPolicyService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        prompt_context_store: OwnedGroupPromptContextStore | None = None,
    ):
        self.db = db
        self.target_resolver = OwnedGroupMessageTargetResolver(db)
        self.prompt_context_store = prompt_context_store or OwnedGroupPromptContextStore()

    async def _asset(self, asset_id: int) -> OwnedGroupAsset:
        return await self.target_resolver.get_asset(asset_id)

    def _add_audit(
        self,
        *,
        event_type: str,
        asset_id: int,
        actor: Any,
        correlation_id: str,
        before_state: dict[str, Any] | None = None,
        after_state: dict[str, Any] | None = None,
        result: str = "success",
        reason_code: str | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
    ) -> None:
        self.db.add(
            OwnedGroupAuditEvent(
                event_type=event_type,
                group_asset_id=int(asset_id),
                resource_type=resource_type,
                resource_id=resource_id,
                actor_id=_actor_id(actor),
                before_state=_json_snapshot(before_state),
                after_state=_json_snapshot(after_state),
                result=result,
                reason_code=reason_code,
                correlation_id=correlation_id,
            )
        )

    @staticmethod
    def _policy_snapshot(policy: GroupAccountMessagePolicy) -> dict[str, Any]:
        return {
            "policy_id": policy.id,
            "owned_group_asset_id": policy.owned_group_asset_id,
            "core_group_id": policy.core_group_id,
            "account_id": policy.account_id,
            "mode": str(_value(policy.mode)),
            "default_template_id": policy.default_template_id,
            "trigger_config": policy.trigger_config,
            "promotion_config": policy.promotion_config,
            "daily_limit": policy.daily_limit,
            "cooldown_seconds": policy.cooldown_seconds,
            "allowed_topics": policy.allowed_topics,
            "require_review": policy.require_review,
            "enabled": policy.enabled,
            "revision": policy.revision,
        }

    async def _require_account_eligible(self, asset_id: int, account_id: int) -> None:
        eligibility = await self.target_resolver.validate_account_eligibility(
            asset_id,
            account_id,
            probe_stale=True,
        )
        if eligibility.eligible:
            return
        code = (
            "ACCOUNT_MODE_NOT_ALLOWED"
            if "account_mode_not_allowed" in eligibility.blocking_reasons
            else "ACCOUNT_NOT_ELIGIBLE"
        )
        message = (
            "账号为 ad_only，不允许用于自建群消息"
            if code == "ACCOUNT_MODE_NOT_ALLOWED"
            else "账号不符合自建群消息准入条件"
        )
        raise OwnedGroupMessagingError(
            code,
            message,
            details={
                "account_id": int(account_id),
                "blocking_reasons": list(eligibility.blocking_reasons),
            },
        )

    async def _owned_template(
        self,
        asset_id: int,
        template_id: int,
        *,
        enabled: bool | None = None,
        category: str | None = None,
    ) -> MessageTemplate:
        conditions = [
            MessageTemplate.id == int(template_id),
            MessageTemplate.scope == "owned_group",
            MessageTemplate.owned_group_asset_id == int(asset_id),
        ]
        if enabled is not None:
            conditions.append(MessageTemplate.enabled == enabled)
        template = await self.db.scalar(select(MessageTemplate).where(*conditions))
        if template is None:
            raise OwnedGroupMessagingError(
                "OWNED_GROUP_TEMPLATE_NOT_FOUND",
                "自建群模板不存在、已停用或不属于当前资产",
                http_status=404,
                details={"template_id": int(template_id), "asset_id": int(asset_id)},
            )
        value = str(_value(template.message_type))
        allowed = {"interaction", "qa"} if category == "community" else {"share", "guide"}
        if category is not None and value not in allowed:
            raise OwnedGroupMessagingError(
                "PROMOTION_TEMPLATE_REQUIRED"
                if category == "promotion"
                else "INVALID_TRIGGER_CONFIG",
                "模板类型与内容类别不匹配",
                http_status=400,
                details={"template_id": int(template_id), "content_category": category},
            )
        return template

    async def _validate_keyword_triggers(
        self,
        request: PolicyCreate | PolicyUpdate,
    ) -> None:
        config = request.trigger_config.keyword
        if not config.enabled:
            return
        triggers = list(
            (
                await self.db.scalars(
                    select(KeywordTrigger).where(KeywordTrigger.id.in_(config.trigger_ids))
                )
            ).all()
        )
        by_id = {item.id: item for item in triggers}
        invalid_ids = [
            item
            for item in config.trigger_ids
            if item not in by_id or not bool(by_id[item].enabled)
        ]
        mode = (
            request.mode
            if config.content_category == "community"
            else request.promotion_config.mode
        )
        expected_action = (
            TriggerAction.REPLY_TEMPLATE.value
            if mode == MessageMode.TEMPLATE.value
            else TriggerAction.REPLY_AI.value
        )
        incompatible_ids = [
            item.id for item in triggers if str(_value(item.action)) != expected_action
        ]
        if invalid_ids or incompatible_ids:
            raise OwnedGroupMessagingError(
                "INVALID_TRIGGER_CONFIG",
                "关键词触发器不存在、未启用或动作与消息模式不兼容",
                http_status=400,
                details={
                    "invalid_trigger_ids": invalid_ids,
                    "incompatible_trigger_ids": incompatible_ids,
                    "expected_action": expected_action,
                },
            )

    async def _validate_policy_references(
        self,
        asset_id: int,
        request: PolicyCreate | PolicyUpdate,
    ) -> None:
        community_template: MessageTemplate | None = None
        promotion_template: MessageTemplate | None = None
        if request.mode == MessageMode.TEMPLATE.value:
            community_template = await self._owned_template(
                asset_id,
                int(request.default_template_id),
                enabled=True,
                category="community",
            )
            self._validate_template_content(
                content=community_template.content,
                message_type=str(_value(community_template.message_type)),
                template_variables=community_template.get_variables(),
            )
        promotion = request.promotion_config
        if promotion.mode == MessageMode.TEMPLATE.value:
            promotion_template = await self._owned_template(
                asset_id,
                int(promotion.default_template_id),
                enabled=True,
                category="promotion",
            )
            self._validate_template_content(
                content=promotion_template.content,
                message_type=str(_value(promotion_template.message_type)),
                template_variables=promotion_template.get_variables(),
            )
        scheduled = request.trigger_config.scheduled
        scheduled_template = (
            community_template if scheduled.content_category == "community" else promotion_template
        )
        if (
            scheduled.enabled
            and scheduled_template is not None
            and "user_name" in set(_TEMPLATE_VARIABLE_RE.findall(scheduled_template.content))
        ):
            raise OwnedGroupMessagingError(
                "INVALID_TEMPLATE_VARIABLE",
                "定时模板不能使用 user_name",
                http_status=400,
                details={"template_id": int(scheduled_template.id), "variable": "user_name"},
            )
        await self._validate_keyword_triggers(request)

    async def _validate_enabling(
        self,
        asset_id: int,
        account_id: int,
        *,
        enabled: bool,
    ) -> None:
        if not enabled:
            return
        if not bool(settings.OWNED_GROUP_MESSAGING_ENABLED):
            raise OwnedGroupMessagingError(
                "OWNED_GROUP_MESSAGING_DISABLED",
                "自建群消息静态功能开关未启用",
                details={"asset_id": int(asset_id)},
            )
        await self.target_resolver.resolve(asset_id, require_managed=True)
        await self._require_account_eligible(asset_id, account_id)

    async def eligible_accounts(self, asset_id: int) -> dict[str, Any]:
        asset = await self._asset(asset_id)
        accounts = await self.target_resolver.list_account_eligibility(
            asset_id,
            probe_stale=False,
        )
        runtime = await get_owned_group_messaging_settings(self.db)
        start = operating_day_start()
        policy_count, enabled_policy_count = (
            await self.db.execute(
                select(
                    func.count(GroupAccountMessagePolicy.id),
                    func.sum(func.cast(GroupAccountMessagePolicy.enabled, Integer)),
                ).where(GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id))
            )
        ).one()
        sent_today = await self.db.scalar(
            select(func.count(GroupAccountMessageExecution.id)).where(
                GroupAccountMessageExecution.owned_group_asset_id == int(asset_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.SENT.value,
                GroupAccountMessageExecution.sent_at >= start,
            )
        )
        pending = await self.db.scalar(
            select(func.count(GroupAccountMessageExecution.id)).where(
                GroupAccountMessageExecution.owned_group_asset_id == int(asset_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.PENDING_REVIEW.value,
            )
        )
        static_enabled = bool(settings.OWNED_GROUP_MESSAGING_ENABLED)
        runtime_enabled = bool(runtime.get("enabled", False))
        dry_run = bool(runtime.get("dryRun", True))
        return {
            "accounts": [item.to_dict() for item in accounts],
            "asset": {
                "asset_id": int(asset.id),
                "core_group_id": asset.core_group_id,
                "telegram_chat_id": asset.telegram_chat_id,
                "governance_status": str(_value(asset.governance_status)),
                "messaging_status": {
                    "static_enabled": static_enabled,
                    "runtime_enabled": runtime_enabled,
                    "dry_run": dry_run,
                    "can_send": static_enabled and runtime_enabled and not dry_run,
                },
                "runtime_limits": {
                    "global_group_daily_limit": int(runtime.get("globalMaxPerGroupPerDay", 20)),
                    "global_account_daily_limit": int(runtime.get("globalMaxPerAccountPerDay", 30)),
                    "min_group_cooldown_seconds": int(runtime.get("minGroupCooldownSeconds", 300)),
                    "dedupe_window_seconds": int(runtime.get("contentDedupeWindowSeconds", 21600)),
                    "review_ttl_hours": int(runtime.get("reviewTtlHours", 24)),
                    "max_attempts": int(runtime.get("maxSendAttempts", 3)),
                },
                "stats": {
                    "policy_count": int(policy_count or 0),
                    "enabled_policy_count": int(enabled_policy_count or 0),
                    "sent_today": int(sent_today or 0),
                    "pending_review_count": int(pending or 0),
                },
            },
        }

    @staticmethod
    def _unavailable_policy_persona_summary(
        account_id: int,
        *,
        effective_enabled: bool,
    ) -> dict[str, Any]:
        return {
            "account_id": int(account_id),
            "configured": False,
            "name": None,
            "revision": 0,
            "applicable": False,
            "effective_enabled": bool(effective_enabled),
        }

    @classmethod
    def _policy_persona_summary_from_projection(
        cls,
        row: Mapping[str, Any],
        *,
        effective_enabled: bool,
    ) -> dict[str, Any]:
        account_id = int(row["account_id"])
        raw_revision = row["ai_persona_revision"]
        revision = raw_revision if type(raw_revision) is int and raw_revision >= 0 else 0
        configured = row["ai_persona"] is not None
        account_type = str(_value(row["account_type"]))
        operation_mode = row["operation_mode"]
        applicable = (
            account_type == AccountType.PROMOTER.value
            and operation_mode is not None
            and str(_value(operation_mode)) == AccountOperationMode.GROWTH.value
        )
        account = SimpleNamespace(
            id=account_id,
            account_type=row["account_type"],
            operation_config=(
                SimpleNamespace(operation_mode=operation_mode)
                if operation_mode is not None
                else None
            ),
            ai_persona=row["ai_persona"],
            ai_persona_revision=raw_revision,
            ai_persona_hash=row["ai_persona_hash"],
            ai_persona_updated_at=row["ai_persona_updated_at"],
            ai_persona_updated_by=row["ai_persona_updated_by"],
        )
        name: str | None = None
        try:
            state = AccountPersonaService.get(account)
        except AccountPersonaError:
            # A policy summary must never echo malformed free-form Persona data.
            # Keep enough metadata for the UI to offer reset while disabling use.
            applicable = False
        else:
            if state.configured and state.persona is not None:
                name = state.persona.name
        return {
            "account_id": account_id,
            "configured": configured,
            "name": name,
            "revision": revision,
            "applicable": applicable,
            "effective_enabled": bool(effective_enabled),
        }

    async def _policy_persona_summaries(
        self,
        account_ids: list[int] | tuple[int, ...] | set[int],
        *,
        effective_enabled: bool | None = None,
    ) -> dict[int, dict[str, Any]]:
        ids = sorted({int(account_id) for account_id in account_ids})
        if not ids:
            return {}
        if effective_enabled is None:
            feature = await get_owned_group_ai_persona_settings(self.db)
            effective_enabled = bool(feature.get("effectiveEnabled", False))

        # Project only the fields needed by the public summary.  The outer join
        # loads every account Persona and operation mode in one bounded query,
        # without materializing system_prompt-bearing ORM entities later.
        result = await self.db.execute(
            select(
                TelegramAccount.id.label("account_id"),
                TelegramAccount.account_type.label("account_type"),
                TelegramAccount.ai_persona.label("ai_persona"),
                TelegramAccount.ai_persona_revision.label("ai_persona_revision"),
                TelegramAccount.ai_persona_hash.label("ai_persona_hash"),
                TelegramAccount.ai_persona_updated_at.label("ai_persona_updated_at"),
                TelegramAccount.ai_persona_updated_by.label("ai_persona_updated_by"),
                AccountOperationConfig.operation_mode.label("operation_mode"),
            )
            .select_from(TelegramAccount)
            .outerjoin(
                AccountOperationConfig,
                AccountOperationConfig.account_id == TelegramAccount.id,
            )
            .where(TelegramAccount.id.in_(ids))
            .order_by(TelegramAccount.id)
        )
        summaries = {
            int(row["account_id"]): self._policy_persona_summary_from_projection(
                row,
                effective_enabled=effective_enabled,
            )
            for row in result.mappings().all()
        }
        for account_id in ids:
            summaries.setdefault(
                account_id,
                self._unavailable_policy_persona_summary(
                    account_id,
                    effective_enabled=effective_enabled,
                ),
            )
        return summaries

    async def _policy_response(
        self,
        policy: GroupAccountMessagePolicy,
        *,
        runtime: Mapping[str, Any] | None = None,
        persona_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.utcnow()
        start = operating_day_start(now)
        if runtime is None:
            runtime = await get_owned_group_messaging_settings(self.db)
        effective_cooldown_seconds = max(
            int(policy.cooldown_seconds),
            int(runtime.get("minGroupCooldownSeconds", 300) or 300),
        )
        eligibility = await self.target_resolver.validate_account_eligibility(
            policy.owned_group_asset_id,
            policy.account_id,
            probe_stale=False,
        )
        sent_today, community_today, promotion_today, last_sent_at = (
            await self.db.execute(
                select(
                    func.count(GroupAccountMessageExecution.id),
                    func.count(GroupAccountMessageExecution.id).filter(
                        GroupAccountMessageExecution.content_category == "community"
                    ),
                    func.count(GroupAccountMessageExecution.id).filter(
                        GroupAccountMessageExecution.content_category == "promotion"
                    ),
                    func.max(GroupAccountMessageExecution.sent_at),
                ).where(
                    GroupAccountMessageExecution.policy_id == policy.id,
                    GroupAccountMessageExecution.status == MessageExecutionStatus.SENT.value,
                    GroupAccountMessageExecution.sent_at >= start,
                )
            )
        ).one()
        group_sent_today = await self.db.scalar(
            select(func.count(GroupAccountMessageExecution.id)).where(
                GroupAccountMessageExecution.core_group_id == policy.core_group_id,
                GroupAccountMessageExecution.status == MessageExecutionStatus.SENT.value,
                GroupAccountMessageExecution.sent_at >= start,
            )
        )
        pending = await self.db.scalar(
            select(func.count(GroupAccountMessageExecution.id)).where(
                GroupAccountMessageExecution.policy_id == policy.id,
                GroupAccountMessageExecution.status == MessageExecutionStatus.PENDING_REVIEW.value,
            )
        )
        if persona_summary is None:
            persona_summary = (await self._policy_persona_summaries([int(policy.account_id)]))[
                int(policy.account_id)
            ]
        response = self._policy_snapshot(policy)
        response.update(
            {
                "account_display_name": eligibility.display_name,
                "account_eligible": eligibility.eligible,
                "account_blocking_reasons": list(eligibility.blocking_reasons),
                "sent_today": int(sent_today or 0),
                "community_sent_today": int(community_today or 0),
                "promotion_sent_today": int(promotion_today or 0),
                "group_sent_today": int(group_sent_today or 0),
                "remaining_today": max(0, int(policy.daily_limit) - int(sent_today or 0)),
                "last_sent_at": _iso(last_sent_at),
                "cooldown_until": (
                    _iso(last_sent_at + timedelta(seconds=effective_cooldown_seconds))
                    if last_sent_at is not None
                    else None
                ),
                "pending_review_count": int(pending or 0),
                "created_by": policy.created_by,
                "updated_by": policy.updated_by,
                "created_at": _iso(policy.created_at),
                "updated_at": _iso(policy.updated_at),
                "persona": dict(persona_summary),
            }
        )
        return response

    async def list_policies(
        self,
        asset_id: int,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        await self._asset(asset_id)
        query = select(GroupAccountMessagePolicy).where(
            GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id)
        )
        if enabled is not None:
            query = query.where(GroupAccountMessagePolicy.enabled == enabled)
        if mode is not None:
            query = query.where(GroupAccountMessagePolicy.mode == mode)
        if account_id is not None:
            query = query.where(GroupAccountMessagePolicy.account_id == int(account_id))
        policies = list((await self.db.scalars(query.order_by(GroupAccountMessagePolicy.id))).all())
        runtime = await get_owned_group_messaging_settings(self.db)
        persona_summaries = await self._policy_persona_summaries(
            {int(item.account_id) for item in policies}
        )
        return [
            await self._policy_response(
                item,
                runtime=runtime,
                persona_summary=persona_summaries[int(item.account_id)],
            )
            for item in policies
        ]

    async def get_policy(
        self,
        asset_id: int,
        policy_id: int,
    ) -> GroupAccountMessagePolicy:
        policy = await self.db.scalar(
            select(GroupAccountMessagePolicy).where(
                GroupAccountMessagePolicy.id == int(policy_id),
                GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id),
            )
        )
        if policy is None:
            raise OwnedGroupMessagingError(
                "POLICY_NOT_FOUND",
                "策略不存在或不属于当前自建群",
                http_status=404,
                details={"asset_id": int(asset_id), "policy_id": int(policy_id)},
            )
        return policy

    async def get_policy_response(self, asset_id: int, policy_id: int) -> dict[str, Any]:
        return await self._policy_response(await self.get_policy(asset_id, policy_id))

    async def create_policy(
        self,
        asset_id: int,
        request: PolicyCreate,
        *,
        actor: Any,
        correlation_id: str,
    ) -> dict[str, Any]:
        asset = await self._asset(asset_id)
        existing = await self.db.scalar(
            select(GroupAccountMessagePolicy).where(
                GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id),
                GroupAccountMessagePolicy.account_id == int(request.account_id),
            )
        )
        if existing is not None:
            raise OwnedGroupMessagingError(
                "POLICY_ALREADY_EXISTS",
                "同一自建群和账号的消息策略已存在",
                details={"policy_id": int(existing.id)},
            )
        if asset.core_group_id is None:
            raise OwnedGroupMessagingError(
                "TARGET_MAPPING_INVALID",
                "资产尚未建立内部群映射，无法保存策略",
                details={"asset_id": int(asset_id)},
            )
        await self._validate_enabling(asset_id, request.account_id, enabled=request.enabled)
        await self._validate_policy_references(asset_id, request)
        payload = request.model_dump(mode="json", exclude={"account_id"})
        policy = GroupAccountMessagePolicy(
            owned_group_asset_id=int(asset_id),
            core_group_id=int(asset.core_group_id),
            account_id=int(request.account_id),
            **payload,
            revision=1,
            created_by=_actor_id(actor),
            updated_by=_actor_id(actor),
        )
        self.db.add(policy)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise OwnedGroupMessagingError(
                "POLICY_ALREADY_EXISTS",
                "同一自建群和账号的消息策略已存在",
                details={"asset_id": int(asset_id), "account_id": int(request.account_id)},
            ) from exc
        self._add_audit(
            event_type="message_policy_created",
            asset_id=asset_id,
            actor=actor,
            correlation_id=correlation_id,
            after_state=self._policy_snapshot(policy),
            resource_type="message_policy",
            resource_id=policy.id,
        )
        if policy.enabled:
            self._add_audit(
                event_type="message_policy_enabled",
                asset_id=asset_id,
                actor=actor,
                correlation_id=correlation_id,
                after_state={"policy_id": policy.id, "enabled": True},
                resource_type="message_policy",
                resource_id=policy.id,
            )
        await self.db.flush()
        return await self._policy_response(policy)

    async def update_policy(
        self,
        asset_id: int,
        policy_id: int,
        request: PolicyUpdate,
        *,
        actor: Any,
        correlation_id: str,
    ) -> dict[str, Any]:
        asset = await self._asset(asset_id)
        if asset.telegram_chat_id is not None:
            await acquire_telegram_chat_transaction_lock(
                self.db,
                int(asset.telegram_chat_id),
            )
        policy = await self.get_policy(asset_id, policy_id)
        if int(policy.revision) != int(request.revision):
            raise OwnedGroupMessagingError(
                "POLICY_REVISION_CONFLICT",
                "策略已被其他请求修改",
                details={
                    "policy_id": int(policy.id),
                    "expected_revision": int(request.revision),
                    "current_revision": int(policy.revision),
                },
            )
        await self._validate_enabling(asset_id, policy.account_id, enabled=request.enabled)
        await self._validate_policy_references(asset_id, request)
        before = self._policy_snapshot(policy)
        values = request.model_dump(mode="json", exclude={"revision"})
        values.update(
            revision=int(request.revision) + 1,
            updated_by=_actor_id(actor),
            updated_at=datetime.utcnow(),
        )
        result = await self.db.execute(
            update(GroupAccountMessagePolicy)
            .where(
                GroupAccountMessagePolicy.id == int(policy_id),
                GroupAccountMessagePolicy.owned_group_asset_id == int(asset_id),
                GroupAccountMessagePolicy.revision == int(request.revision),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise OwnedGroupMessagingError(
                "POLICY_REVISION_CONFLICT",
                "策略已被其他请求修改",
                details={"policy_id": int(policy_id), "expected_revision": int(request.revision)},
            )

        cancellation_categories: set[str] = set()
        cancelled_execution_ids: list[int] = []
        cancel_all = bool(before["enabled"] and not request.enabled)
        if not cancel_all and before["mode"] != "off" and request.mode == "off":
            cancellation_categories.add("community")
        old_promotion_mode = str((before["promotion_config"] or {}).get("mode", "off"))
        if (
            not cancel_all
            and old_promotion_mode != "off"
            and request.promotion_config.mode == "off"
        ):
            cancellation_categories.add("promotion")
        if cancel_all or cancellation_categories:
            conditions = [
                GroupAccountMessageExecution.policy_id == int(policy_id),
                GroupAccountMessageExecution.status.in_(UNSENT_CANCELLABLE_STATUSES),
            ]
            if not cancel_all:
                conditions.append(
                    GroupAccountMessageExecution.content_category.in_(cancellation_categories)
                )
            cancelled_at = datetime.utcnow()
            executions = list(
                (
                    await self.db.scalars(message_execution_for_update_query().where(*conditions))
                ).all()
            )
            for execution in executions:
                cancelled_execution_ids.append(int(execution.id))
                execution.prompt_context = summarize_prompt_context(execution.prompt_context)
                execution.status = MessageExecutionStatus.CANCELLED.value
                execution.error_code = "POLICY_DISABLED"
                execution.error_message = "策略更新后取消未发送执行"
                execution.lease_id = None
                execution.lease_expires_at = None
                execution.write_started_at = None
                execution.next_retry_at = None
                execution.revision = int(execution.revision) + 1
                execution.updated_at = cancelled_at

        refreshed = await self.get_policy(asset_id, policy_id)
        await self.db.refresh(refreshed)
        after = self._policy_snapshot(refreshed)
        self._add_audit(
            event_type="message_policy_updated",
            asset_id=asset_id,
            actor=actor,
            correlation_id=correlation_id,
            before_state=before,
            after_state=after,
            resource_type="message_policy",
            resource_id=policy_id,
        )
        if bool(before["enabled"]) != bool(request.enabled):
            self._add_audit(
                event_type=(
                    "message_policy_enabled" if request.enabled else "message_policy_disabled"
                ),
                asset_id=asset_id,
                actor=actor,
                correlation_id=correlation_id,
                before_state={"policy_id": policy_id, "enabled": bool(before["enabled"])},
                after_state={"policy_id": policy_id, "enabled": request.enabled},
                resource_type="message_policy",
                resource_id=policy_id,
            )
        await self.db.flush()
        response = await self._policy_response(refreshed)
        # Commit while the transaction-scoped chat lock is still held. A sender
        # that already owns the session lock finishes first; every later sender
        # observes the disabled policy before Telegram I/O.
        await self.db.commit()
        for execution_id in cancelled_execution_ids:
            await self.prompt_context_store.discard(execution_id)
        return response

    @staticmethod
    def _template_category(message_type: object) -> str:
        value = str(_value(message_type))
        if value in {"interaction", "qa"}:
            return "community"
        if value in {"share", "guide"}:
            return "promotion"
        raise OwnedGroupMessagingError(
            "INVALID_TRIGGER_CONFIG",
            "自建群模板类型只能是 interaction、qa、share 或 guide",
            http_status=400,
            details={"message_type": value},
        )

    @classmethod
    def _validate_template_content(
        cls,
        *,
        content: str,
        message_type: str,
        template_variables: list[str],
    ) -> set[str]:
        expressions = [match.strip() for match in _TEMPLATE_EXPRESSION_RE.findall(content)]
        remainder = _TEMPLATE_EXPRESSION_RE.sub("", content)
        invalid_expressions = sorted(
            {
                expression
                for expression in expressions
                if _TEMPLATE_VARIABLE_NAME_RE.fullmatch(expression) is None
            }
        )
        if invalid_expressions or "{{" in remainder or "}}" in remainder:
            raise OwnedGroupMessagingError(
                "INVALID_TEMPLATE_VARIABLE",
                "模板包含表达式、过滤器或未闭合占位符",
                http_status=400,
                details={"invalid_expressions": invalid_expressions[:10]},
            )

        actual = set(expressions)
        declared = set(template_variables)
        unknown = actual - ALLOWED_OWNED_GROUP_TEMPLATE_VARIABLES
        if unknown:
            raise OwnedGroupMessagingError(
                "INVALID_TEMPLATE_VARIABLE",
                "模板包含不支持的变量",
                http_status=400,
                details={"variables": sorted(unknown)},
            )
        if actual != declared:
            raise OwnedGroupMessagingError(
                "INVALID_TEMPLATE_VARIABLE",
                "template_variables 必须与模板正文中的变量完全一致",
                http_status=400,
                details={
                    "missing_declarations": sorted(actual - declared),
                    "unused_declarations": sorted(declared - actual),
                },
            )
        category = cls._template_category(message_type)
        forbidden_promotion_variables = actual & {"promotion_url", "promotion_cta"}
        if category == "community" and forbidden_promotion_variables:
            raise OwnedGroupMessagingError(
                "INVALID_TEMPLATE_VARIABLE",
                "普通消息模板不得使用推广变量",
                http_status=400,
                details={"variables": sorted(forbidden_promotion_variables)},
            )
        if contains_url_like(content):
            raise OwnedGroupMessagingError(
                "CONTENT_SAFETY_BLOCKED",
                "群内模板不得硬编码链接；推广链接必须使用 promotion_url",
                http_status=400,
            )
        return actual

    @classmethod
    def _validate_template_body(cls, request: OwnedGroupTemplateWrite) -> None:
        cls._validate_template_content(
            content=request.content,
            message_type=request.message_type,
            template_variables=request.template_variables,
        )

    @classmethod
    def _template_response(cls, template: MessageTemplate) -> dict[str, Any]:
        return {
            "id": int(template.id),
            "name": template.name,
            "content": template.content,
            "message_type": str(_value(template.message_type)),
            "content_category": cls._template_category(template.message_type),
            "template_variables": template.get_variables(),
            "enabled": bool(template.enabled),
            "created_at": _iso(template.created_at),
            "updated_at": _iso(template.updated_at),
        }

    async def list_templates(
        self,
        asset_id: int,
        *,
        message_type: str | None = None,
        content_category: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        await self._asset(asset_id)
        query = select(MessageTemplate).where(
            MessageTemplate.scope == "owned_group",
            MessageTemplate.owned_group_asset_id == int(asset_id),
        )
        if message_type is not None:
            query = query.where(MessageTemplate.message_type == message_type)
        if enabled is not None:
            query = query.where(MessageTemplate.enabled == enabled)
        items = list(
            (await self.db.scalars(query.order_by(desc(MessageTemplate.updated_at)))).all()
        )
        result = [self._template_response(item) for item in items]
        if content_category is not None:
            result = [item for item in result if item["content_category"] == content_category]
        return result

    async def create_template(
        self,
        asset_id: int,
        request: OwnedGroupTemplateWrite,
    ) -> dict[str, Any]:
        await self._asset(asset_id)
        self._template_category(request.message_type)
        self._validate_template_body(request)
        template = MessageTemplate(
            name=request.name,
            content=request.content,
            template_variables=",".join(request.template_variables) or None,
            message_type=MessageType(request.message_type),
            cooldown_seconds=300,
            max_uses_per_day=100,
            enabled=request.enabled,
            scope="owned_group",
            owned_group_asset_id=int(asset_id),
        )
        self.db.add(template)
        await self.db.flush()
        return self._template_response(template)

    async def update_template(
        self,
        asset_id: int,
        template_id: int,
        request: OwnedGroupTemplateWrite,
    ) -> dict[str, Any]:
        template = await self._owned_template(asset_id, template_id)
        self._template_category(request.message_type)
        self._validate_template_body(request)
        template.name = request.name
        template.content = request.content
        template.template_variables = ",".join(request.template_variables) or None
        template.message_type = MessageType(request.message_type)
        template.enabled = request.enabled
        template.updated_at = datetime.utcnow()
        await self.db.flush()
        return self._template_response(template)

    async def _expire_pending_review(
        self,
        execution: GroupAccountMessageExecution,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        if execution.status != MessageExecutionStatus.PENDING_REVIEW.value:
            return
        runtime = runtime or await get_owned_group_messaging_settings(self.db)
        expires_at = execution.updated_at + timedelta(hours=int(runtime.get("reviewTtlHours", 24)))
        if expires_at <= datetime.utcnow():
            execution.status = MessageExecutionStatus.EXPIRED.value
            execution.error_code = "REVIEW_EXPIRED"
            execution.error_message = "审核窗口已过期"
            execution.revision += 1
            execution.updated_at = datetime.utcnow()
            await self.db.flush()

    async def get_execution(
        self,
        asset_id: int,
        execution_id: int,
    ) -> GroupAccountMessageExecution:
        execution = await self.db.scalar(
            select(GroupAccountMessageExecution).where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.owned_group_asset_id == int(asset_id),
            )
        )
        if execution is None:
            raise OwnedGroupMessagingError(
                "EXECUTION_NOT_FOUND",
                "执行不存在或不属于当前自建群",
                http_status=404,
                details={"asset_id": int(asset_id), "execution_id": int(execution_id)},
            )
        await self._expire_pending_review(execution)
        return execution

    @staticmethod
    def _hash_prefix(value: object) -> str | None:
        return value[:12] if isinstance(value, str) and _LOWER_SHA256_RE.fullmatch(value) else None

    @classmethod
    def _execution_persona_summary(
        cls,
        execution: GroupAccountMessageExecution,
    ) -> dict[str, Any] | None:
        if str(_value(execution.mode_snapshot)) != MessageMode.AI.value:
            return None

        raw_source = execution.persona_source_snapshot
        source = str(_value(raw_source)) if raw_source is not None else "legacy_untracked"
        if source not in _PERSONA_SNAPSHOT_SOURCES:
            source = "legacy_untracked"

        raw_revision = execution.persona_revision_snapshot
        revision = raw_revision if type(raw_revision) is int and raw_revision >= 0 else None
        persona_hash = execution.persona_hash
        hash_prefix = cls._hash_prefix(persona_hash)
        name: str | None = None
        snapshot = execution.persona_snapshot
        if source != "legacy_untracked" and isinstance(snapshot, Mapping) and hash_prefix:
            try:
                persona = normalize_persona_payload(snapshot)
            except Exception:
                pass
            else:
                if hash_persona(persona) == persona_hash:
                    name = persona.name

        return {
            "source": source,
            "name": name,
            "revision": revision,
            "hash_prefix": hash_prefix,
        }

    async def _execution_response(
        self,
        execution: GroupAccountMessageExecution,
        *,
        include_content: bool,
        include_sensitive: bool = True,
    ) -> dict[str, Any]:
        runtime = await get_owned_group_messaging_settings(self.db)
        review_expires_at = (
            execution.updated_at + timedelta(hours=int(runtime.get("reviewTtlHours", 24)))
            if execution.status == MessageExecutionStatus.PENDING_REVIEW.value
            else None
        )
        audits: list[int] = []
        if include_sensitive:
            audits = list(
                (
                    await self.db.scalars(
                        select(OwnedGroupAuditEvent.id)
                        .where(
                            OwnedGroupAuditEvent.group_asset_id == execution.owned_group_asset_id,
                            OwnedGroupAuditEvent.resource_type == "message_execution",
                            OwnedGroupAuditEvent.resource_id == execution.id,
                        )
                        .order_by(OwnedGroupAuditEvent.created_at)
                    )
                ).all()
            )
        timeline = [
            {
                "status": MessageExecutionStatus.QUEUED.value,
                "at": _iso(execution.created_at),
                "timestamp": _iso(execution.created_at),
            }
        ]
        if execution.reviewed_at is not None:
            review_status = (
                MessageExecutionStatus.REJECTED.value
                if execution.status == MessageExecutionStatus.REJECTED.value
                else MessageExecutionStatus.READY_TO_SEND.value
            )
            timeline.append(
                {
                    "status": review_status,
                    "at": _iso(execution.reviewed_at),
                    "timestamp": _iso(execution.reviewed_at),
                    "actor_id": execution.reviewer_id,
                }
            )
        if execution.sent_at is not None:
            timeline.append(
                {
                    "status": MessageExecutionStatus.SENT.value,
                    "at": _iso(execution.sent_at),
                    "timestamp": _iso(execution.sent_at),
                }
            )
        current = str(_value(execution.status))
        if current not in {item["status"] for item in timeline}:
            timeline.append(
                {
                    "status": current,
                    "at": _iso(execution.updated_at),
                    "timestamp": _iso(execution.updated_at),
                }
            )
        content = execution.content if include_content and include_sensitive else None
        is_ai = str(_value(execution.mode_snapshot)) == MessageMode.AI.value
        return {
            "id": int(execution.id),
            "execution_id": int(execution.id),
            "policy_id": int(execution.policy_id),
            "owned_group_asset_id": int(execution.owned_group_asset_id),
            "core_group_id": int(execution.core_group_id),
            "telegram_chat_id": int(execution.telegram_chat_id),
            "account_id": int(execution.account_id),
            "trigger_type": str(_value(execution.trigger_type)),
            "message_purpose": str(_value(execution.message_purpose)),
            "content_category": str(_value(execution.content_category)),
            "mode_snapshot": str(_value(execution.mode_snapshot)),
            "policy_revision": int(execution.policy_revision),
            "status": current,
            "source_message_id": execution.source_message_id,
            "reply_to_message_id": execution.reply_to_message_id,
            "keyword_trigger_id": execution.keyword_trigger_id,
            "template_id": execution.template_id,
            "topic": execution.topic,
            "prompt_context": (
                summarize_prompt_context(execution.prompt_context)
                if include_content and include_sensitive
                else None
            ),
            "promotion_config_snapshot": (
                execution.promotion_config_snapshot
                if include_content and include_sensitive
                else None
            ),
            "content": content,
            "content_summary": (
                redact_sensitive_text(execution.content or "", max_length=160)
                if execution.content and include_sensitive
                else None
            ),
            "content_hash": execution.content_hash,
            "persona": self._execution_persona_summary(execution),
            "prompt_template_version": (execution.prompt_template_version if is_ai else None),
            "prompt_hash_prefix": (self._hash_prefix(execution.prompt_hash) if is_ai else None),
            "governance_rules_hash_prefix": (
                self._hash_prefix(execution.governance_rules_hash) if is_ai else None
            ),
            "correlation_id": execution.correlation_id,
            "scheduled_at": _iso(execution.scheduled_at),
            "next_retry_at": _iso(execution.next_retry_at),
            "attempt_count": int(execution.attempt_count),
            "revision": int(execution.revision),
            "requested_by": execution.requested_by,
            "reviewer_id": execution.reviewer_id,
            "reviewed_at": _iso(execution.reviewed_at),
            "review_expires_at": _iso(review_expires_at),
            "telegram_message_id": execution.telegram_message_id,
            "error_code": execution.error_code,
            "error_message": (
                redact_sensitive_text(
                    execution.error_message,
                    max_length=500 if include_sensitive else 160,
                )
                if execution.error_message
                else None
            ),
            "created_at": _iso(execution.created_at),
            "updated_at": _iso(execution.updated_at),
            "sent_at": _iso(execution.sent_at),
            "timeline": timeline,
            "audit_event_ids": [int(item) for item in audits],
        }

    async def list_executions(
        self,
        asset_id: int,
        *,
        policy_id: int | None = None,
        account_id: int | None = None,
        trigger_type: str | None = None,
        content_category: str | None = None,
        status: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        await self._asset(asset_id)
        conditions = [GroupAccountMessageExecution.owned_group_asset_id == int(asset_id)]
        optional = (
            (GroupAccountMessageExecution.policy_id, policy_id),
            (GroupAccountMessageExecution.account_id, account_id),
            (GroupAccountMessageExecution.trigger_type, trigger_type),
            (GroupAccountMessageExecution.content_category, content_category),
            (GroupAccountMessageExecution.status, status),
        )
        for column, value in optional:
            if value is not None:
                conditions.append(column == value)
        if created_from is not None:
            conditions.append(GroupAccountMessageExecution.created_at >= created_from)
        if created_to is not None:
            conditions.append(GroupAccountMessageExecution.created_at <= created_to)
        total = await self.db.scalar(
            select(func.count(GroupAccountMessageExecution.id)).where(*conditions)
        )
        items = list(
            (
                await self.db.scalars(
                    select(GroupAccountMessageExecution)
                    .where(*conditions)
                    .order_by(desc(GroupAccountMessageExecution.created_at))
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
        for item in items:
            await self._expire_pending_review(item)
        return {
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
            "items": [
                await self._execution_response(
                    item,
                    include_content=False,
                    include_sensitive=include_sensitive,
                )
                for item in items
            ],
        }

    async def get_execution_response(
        self,
        asset_id: int,
        execution_id: int,
        *,
        include_sensitive: bool = True,
    ) -> dict[str, Any]:
        return await self._execution_response(
            await self.get_execution(asset_id, execution_id),
            include_content=True,
            include_sensitive=include_sensitive,
        )

    async def approve_execution(
        self,
        asset_id: int,
        execution_id: int,
        request: ExecutionApprove,
        *,
        actor: Any,
        correlation_id: str,
    ) -> dict[str, Any]:
        telegram_chat_id = await self.db.scalar(
            select(GroupAccountMessageExecution.telegram_chat_id).where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.owned_group_asset_id == int(asset_id),
            )
        )
        if telegram_chat_id is None:
            raise OwnedGroupMessagingError(
                "EXECUTION_NOT_FOUND",
                "执行不存在或不属于当前自建群",
                http_status=404,
                details={"asset_id": int(asset_id), "execution_id": int(execution_id)},
            )
        async with telegram_chat_advisory_lock(self.db, int(telegram_chat_id)):
            return await self._approve_execution_locked(
                asset_id,
                execution_id,
                request,
                actor=actor,
                correlation_id=correlation_id,
            )

    async def _approve_execution_locked(
        self,
        asset_id: int,
        execution_id: int,
        request: ExecutionApprove,
        *,
        actor: Any,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Re-read, validate and reserve approved content under the chat lock."""

        execution = await self.get_execution(asset_id, execution_id)
        if execution.status != MessageExecutionStatus.PENDING_REVIEW.value:
            raise OwnedGroupMessagingError(
                "REVIEW_STATUS_INVALID",
                "当前执行状态不可审核通过",
                details={"execution_id": int(execution_id), "status": execution.status},
            )
        if int(execution.revision) != int(request.revision):
            raise OwnedGroupMessagingError(
                "EXECUTION_REVISION_CONFLICT",
                "执行已被其他审核请求修改",
                details={
                    "execution_id": int(execution_id),
                    "expected_revision": int(request.revision),
                    "current_revision": int(execution.revision),
                },
            )
        content = request.content_override or execution.content
        if not content or not normalize_message_content(content):
            raise OwnedGroupMessagingError(
                "CONTENT_SAFETY_BLOCKED",
                "审核后的消息内容不能为空",
                http_status=400,
            )
        from app.modules.owned_group.messaging_content_service import (
            OwnedGroupMessageContentService,
        )

        promotion_snapshot = dict(execution.promotion_config_snapshot or {})
        validated = await OwnedGroupMessageContentService(self.db).validate_final_content(
            content,
            content_category=str(_value(execution.content_category)),
            promotion_config=promotion_snapshot,
            mode_snapshot=str(_value(execution.mode_snapshot)),
        )
        governance_decision = await govern_review_override(
            db=self.db,
            execution=execution,
            text=validated.content,
        )
        digest = validated.content_hash
        runtime = await get_owned_group_messaging_settings(self.db)
        since = datetime.utcnow() - timedelta(
            seconds=int(runtime.get("contentDedupeWindowSeconds", 21600))
        )
        duplicate = await self.db.scalar(
            select(GroupAccountMessageExecution.id).where(
                GroupAccountMessageExecution.id != int(execution_id),
                GroupAccountMessageExecution.core_group_id == execution.core_group_id,
                GroupAccountMessageExecution.content_hash == digest,
                GroupAccountMessageExecution.status.in_(RESERVING_EXECUTION_STATUSES),
                func.coalesce(
                    GroupAccountMessageExecution.sent_at,
                    GroupAccountMessageExecution.updated_at,
                )
                >= since,
            )
        )
        if duplicate is not None:
            raise OwnedGroupMessagingError(
                "CONTENT_DUPLICATE",
                "同群去重窗口内已有相同内容",
                details={"conflicting_execution_id": int(duplicate)},
            )
        now = datetime.utcnow()
        result = await self.db.execute(
            update(GroupAccountMessageExecution)
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.owned_group_asset_id == int(asset_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.PENDING_REVIEW.value,
                GroupAccountMessageExecution.revision == int(request.revision),
            )
            .values(
                status=MessageExecutionStatus.READY_TO_SEND.value,
                content=validated.content,
                content_hash=digest,
                reviewer_id=_actor_id(actor),
                reviewed_at=now,
                updated_at=now,
                revision=int(request.revision) + 1,
            )
        )
        if result.rowcount != 1:
            raise OwnedGroupMessagingError(
                "EXECUTION_REVISION_CONFLICT",
                "执行已被其他审核请求修改",
                details={"execution_id": int(execution_id)},
            )
        governance_audit: dict[str, Any] = {}
        review_governance_hash = getattr(
            governance_decision,
            "governance_rules_hash",
            None,
        )
        if isinstance(review_governance_hash, str):
            governance_audit = {
                "generation_governance_rules_hash": execution.governance_rules_hash,
                "review_governance_rules_hash": review_governance_hash,
            }
        self._add_audit(
            event_type="message_review_approved",
            asset_id=asset_id,
            actor=actor,
            correlation_id=correlation_id,
            before_state={
                "execution_id": execution_id,
                "status": "pending_review",
                "content_hash": execution.content_hash,
            },
            after_state={
                "execution_id": execution_id,
                "status": "ready_to_send",
                "content_hash": digest,
                "content_overridden": request.content_override is not None,
                **governance_audit,
            },
            resource_type="message_execution",
            resource_id=execution_id,
        )
        await self.db.flush()
        response = await self.get_execution_response(asset_id, execution_id)
        # The session-scoped lock lives on a dedicated connection.  Commit the
        # approved hash/status before releasing it so the next reviewer sees
        # this reservation during its in-lock duplicate query.
        await self.db.commit()
        return response

    async def reject_execution(
        self,
        asset_id: int,
        execution_id: int,
        request: ExecutionReject,
        *,
        actor: Any,
        correlation_id: str,
    ) -> dict[str, Any]:
        execution = await self.get_execution(asset_id, execution_id)
        if execution.status != MessageExecutionStatus.PENDING_REVIEW.value:
            raise OwnedGroupMessagingError(
                "REVIEW_STATUS_INVALID",
                "当前执行状态不可拒绝",
                details={"execution_id": int(execution_id), "status": execution.status},
            )
        if int(execution.revision) != int(request.revision):
            raise OwnedGroupMessagingError(
                "EXECUTION_REVISION_CONFLICT",
                "执行已被其他审核请求修改",
                details={
                    "execution_id": int(execution_id),
                    "expected_revision": int(request.revision),
                    "current_revision": int(execution.revision),
                },
            )
        now = datetime.utcnow()
        result = await self.db.execute(
            update(GroupAccountMessageExecution)
            .where(
                GroupAccountMessageExecution.id == int(execution_id),
                GroupAccountMessageExecution.owned_group_asset_id == int(asset_id),
                GroupAccountMessageExecution.status == MessageExecutionStatus.PENDING_REVIEW.value,
                GroupAccountMessageExecution.revision == int(request.revision),
            )
            .values(
                status=MessageExecutionStatus.REJECTED.value,
                reviewer_id=_actor_id(actor),
                reviewed_at=now,
                error_code="REVIEW_REJECTED",
                error_message=redact_sensitive_text(request.reason, max_length=500),
                updated_at=now,
                revision=int(request.revision) + 1,
            )
        )
        if result.rowcount != 1:
            raise OwnedGroupMessagingError(
                "EXECUTION_REVISION_CONFLICT",
                "执行已被其他审核请求修改",
                details={"execution_id": int(execution_id)},
            )
        self._add_audit(
            event_type="message_review_rejected",
            asset_id=asset_id,
            actor=actor,
            correlation_id=correlation_id,
            before_state={"execution_id": execution_id, "status": "pending_review"},
            after_state={
                "execution_id": execution_id,
                "status": "rejected",
                "reason_provided": bool(request.reason.strip()),
            },
            resource_type="message_execution",
            resource_id=execution_id,
        )
        await self.db.flush()
        return await self.get_execution_response(asset_id, execution_id)
