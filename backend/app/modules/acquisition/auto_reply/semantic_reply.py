"""Semantic group-reply selector for real Telegram conversations."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.ai.llm_client import LLMClient
from app.core.automation_settings import get_group_ai_interaction_settings
from app.core.redis import RedisCache
from app.modules.acquisition.auto_reply.safety import sanitize_natural_group_reply
from app.modules.acquisition.keyword_trigger.actions import ActionExecutor
from app.modules.acquisition.models import AcquisitionMessage, MessageType

logger = structlog.get_logger()


@dataclass
class SemanticMessage:
    message_id: int
    user_id: int
    user_name: str
    text: str
    timestamp: datetime


@dataclass
class SemanticReplyResult:
    sent: bool
    reason: str
    target_message_id: Optional[int] = None
    reply: str = ""
    intent: str = ""
    confidence: float = 0.0


class SemanticGroupReplyEngine:
    """Select one meaningful group message from recent context and reply naturally."""

    def __init__(
        self,
        db: AsyncSession,
        account_pool: AccountPool,
        llm_client: Optional[LLMClient] = None,
        cache: Optional[RedisCache] = None,
    ):
        self.db = db
        self.account_pool = account_pool
        self.llm_client = llm_client
        self.cache = cache or RedisCache()
        self.risk_guard = AccountRiskGuard(db)
        self.action_executor = ActionExecutor(account_pool, risk_guard=self.risk_guard)
        self.logger = logger.bind(module="semantic_group_reply")

    async def process_message(
        self,
        *,
        account_id: Optional[int],
        group_id: int,
        message_id: int,
        user_id: int,
        user_name: str,
        text: str,
        timestamp: datetime,
    ) -> SemanticReplyResult:
        settings = await get_group_ai_interaction_settings(self.db)
        if not self._enabled(settings):
            return SemanticReplyResult(sent=False, reason="semantic_reply_disabled")

        normalized_text = self._normalize_text(text)
        if not self._message_eligible(normalized_text, settings):
            return SemanticReplyResult(sent=False, reason="message_not_eligible")

        message = SemanticMessage(
            message_id=message_id,
            user_id=user_id,
            user_name=user_name,
            text=normalized_text,
            timestamp=timestamp,
        )
        messages = await self._append_message(group_id, message, settings)
        if not await self._should_evaluate(group_id, message_id, messages, settings):
            return SemanticReplyResult(sent=False, reason="waiting_for_scan_interval")

        allowed, limit_reason = await self._rate_limit_allows_evaluation(
            account_id=account_id,
            group_id=group_id,
            settings=settings,
        )
        if not allowed:
            return SemanticReplyResult(sent=False, reason=limit_reason)

        if self.llm_client is None:
            return SemanticReplyResult(sent=False, reason="llm_unavailable")

        decision = await self._decide(messages, settings)
        if not self._decision_allowed(decision, messages, settings):
            return SemanticReplyResult(
                sent=False,
                reason=str(decision.get("reason") or "decision_rejected"),
                target_message_id=self._int_or_none(decision.get("target_message_id")),
                intent=str(decision.get("intent") or ""),
                confidence=self._float_value(decision.get("confidence")),
            )

        target_message_id = int(decision["target_message_id"])
        target_message = next((item for item in messages if item.message_id == target_message_id), None)
        if target_message is None:
            return SemanticReplyResult(sent=False, reason="target_message_missing")

        sent_key = f"acquisition:semantic_reply:sent:{group_id}:{target_message_id}"
        if not await self._reserve_once(sent_key, ttl=24 * 3600):
            return SemanticReplyResult(sent=False, reason="target_already_replied", target_message_id=target_message_id)

        reply = sanitize_natural_group_reply(str(decision.get("reply") or ""), settings)
        if not reply:
            return SemanticReplyResult(sent=False, reason="empty_reply", target_message_id=target_message_id)

        account = None
        try:
            if account_id:
                account = await self.account_pool.acquire_by_id(account_id, purpose="semantic_group_reply")
            if account is None:
                account = await self.account_pool.acquire(purpose="semantic_group_reply")
            if account is None:
                return SemanticReplyResult(sent=False, reason="no_available_account", target_message_id=target_message_id)

            sent_id = await self.action_executor.send_group_reply(
                account=account,
                group_id=group_id,
                message=reply,
                reply_to=target_message_id,
            )
            if sent_id is None:
                return SemanticReplyResult(sent=False, reason="rate_limited_or_send_failed", target_message_id=target_message_id)

            resolved_account_id = account_id or getattr(account, "account_id", None)
            await self._record_sent_reply(
                account_id=resolved_account_id,
                group_id=group_id,
                reply=reply,
                sent_id=sent_id,
            )

            return SemanticReplyResult(
                sent=True,
                reason="sent",
                target_message_id=target_message_id,
                reply=reply,
                intent=str(decision.get("intent") or ""),
                confidence=self._float_value(decision.get("confidence")),
            )
        finally:
            if account is not None:
                await self.account_pool.release(account)

    async def _record_sent_reply(
        self,
        *,
        account_id: Optional[int],
        group_id: int,
        reply: str,
        sent_id: int,
    ) -> None:
        """Persist successful semantic replies for durable workflow auditing."""
        if not account_id:
            self.logger.warning("semantic_reply_audit_missing_account", group_id=group_id, message_id=sent_id)
            return
        self.db.add(
            AcquisitionMessage(
                account_id=int(account_id),
                group_id=group_id,
                content=reply,
                message_type=MessageType.QA,
                message_id=sent_id,
            )
        )
        try:
            await self.db.commit()
        except Exception as exc:
            await self.db.rollback()
            self.logger.warning(
                "semantic_reply_audit_failed",
                account_id=account_id,
                group_id=group_id,
                message_id=sent_id,
                error=str(exc),
            )

    def _enabled(self, settings: dict[str, Any]) -> bool:
        return bool(
            settings.get("enabled")
            and settings.get("aiEnabled")
            and settings.get("allowSemanticTriggeredReply")
            and settings.get("mode") != "off"
        )

    def _message_eligible(self, text: str, settings: dict[str, Any]) -> bool:
        min_chars = int(settings.get("semanticMinTextChars") or 4)
        if len(text) < min_chars:
            return False
        if text.startswith("/"):
            return False
        if re.fullmatch(r"[\W_]+", text, flags=re.UNICODE):
            return False
        return True

    async def _append_message(
        self,
        group_id: int,
        message: SemanticMessage,
        settings: dict[str, Any],
    ) -> list[SemanticMessage]:
        key = f"acquisition:semantic_reply:window:{group_id}"
        window = int(settings.get("semanticScanWindowMessages") or 100)
        window = min(max(window, 5), 100)
        existing = await self.cache.get_json(key) or []
        rows = existing if isinstance(existing, list) else []
        rows = [
            row
            for row in rows
            if not (
                isinstance(row, dict)
                and self._int_or_none(row.get("message_id")) == message.message_id
            )
        ]
        rows.append(
            {
                "message_id": message.message_id,
                "user_id": message.user_id,
                "user_name": message.user_name,
                "text": message.text,
                "timestamp": message.timestamp.isoformat(),
            }
        )
        rows = rows[-window:]
        await self.cache.set_json(key, rows, ttl=2 * 3600)
        return [self._message_from_row(row) for row in rows if isinstance(row, dict)]

    async def _should_evaluate(
        self,
        group_id: int,
        message_id: int,
        messages: list[SemanticMessage],
        settings: dict[str, Any],
    ) -> bool:
        interval = int(settings.get("semanticEvaluateEveryMessages") or 100)
        interval = min(max(interval, 1), 100)
        if len(messages) < 2 and interval > 1:
            return False
        if len(messages) % interval != 0:
            return False
        key = f"acquisition:semantic_reply:evaluate:{group_id}:{message_id}"
        return await self._reserve_once(key, ttl=15 * 60)

    async def _reserve_once(self, key: str, *, ttl: int) -> bool:
        client = getattr(self.cache, "client", None)
        if client is None:
            return True
        return bool(await client.set(key, "1", ex=ttl, nx=True))

    async def _rate_limit_allows_evaluation(
        self,
        *,
        account_id: Optional[int],
        group_id: int,
        settings: dict[str, Any],
    ) -> tuple[bool, str]:
        group_limit = int(settings.get("maxRepliesPerGroupPerDay", 0) or 0)
        account_limit = int(settings.get("maxRepliesPerAccountPerDay", 0) or 0)
        if group_limit <= 0 or account_limit <= 0:
            return False, "semantic_reply_zero_daily_limit"

        rate_limits = self.action_executor.group_ai_rate_limit
        group_key = rate_limits.build_key("daily", "group", group_id)
        if await self._daily_limit_reached(group_key, group_limit):
            self.logger.info("semantic_reply_group_daily_limit_reached", group_id=group_id, limit=group_limit)
            return False, "group_daily_limit_reached"

        if account_id:
            account_key = rate_limits.build_key("daily", "account", account_id)
            if await self._daily_limit_reached(account_key, account_limit):
                self.logger.info(
                    "semantic_reply_account_daily_limit_reached",
                    account_id=account_id,
                    group_id=group_id,
                    limit=account_limit,
                )
                return False, "account_daily_limit_reached"

            cooldown_key = rate_limits.build_key("cooldown", "account", account_id, "group", group_id)
            if int(settings.get("cooldownSeconds", 0) or 0) > 0 and await self.cache.get(cooldown_key):
                return False, "cooldown_active"

        return True, ""

    async def _daily_limit_reached(self, identifier: str, limit: int, *, period: int = 24 * 3600) -> bool:
        if limit <= 0:
            return True
        client = getattr(self.cache, "client", None)
        if client is None:
            return False
        limiter_prefix = self.action_executor.group_ai_rate_limit.limiter.key_prefix
        redis_key = f"{limiter_prefix}{identifier}:{int(time.time()) // period}"
        value = await client.get(redis_key)
        try:
            return int(value or 0) >= limit
        except (TypeError, ValueError):
            return False

    async def _decide(self, messages: list[SemanticMessage], settings: dict[str, Any]) -> dict[str, Any]:
        recent = messages[-int(settings.get("semanticScanWindowMessages") or 100) :]
        context = "\n".join(
            f"- id={item.message_id}; user={item.user_name or item.user_id}; text={item.text}"
            for item in recent
        )
        prompt = (
            f"{settings.get('semanticDecisionPrompt')}\n\n"
            f"允许意图: {', '.join(settings.get('semanticAllowedIntents') or [])}\n"
            f"禁止意图: {', '.join(settings.get('semanticBlockedIntents') or [])}\n"
            f"最近群聊消息:\n{context}\n\n"
            "请只输出JSON，不要解释。格式："
            '{"should_reply":true/false,"target_message_id":数字或null,'
            '"intent":"question","confidence":0.0到1.0,"reply":"一条自然中文回复","reason":"简短原因"}'
        )
        raw = await self.llm_client.generate(
            prompt=prompt,
            model=self.llm_client.model_for("fast"),
            temperature=float(settings.get("temperature", 0.6)),
            max_tokens=min(int(settings.get("maxTokens", 180) or 180), 260),
            system_prompt=str(settings.get("systemPrompt") or "自然中文群聊成员，不暴露AI身份。"),
        )
        return self._parse_json(raw)

    def _decision_allowed(
        self,
        decision: dict[str, Any],
        messages: list[SemanticMessage],
        settings: dict[str, Any],
    ) -> bool:
        if not decision.get("should_reply"):
            return False
        target_message_id = self._int_or_none(decision.get("target_message_id"))
        if target_message_id is None:
            return False
        if not any(item.message_id == target_message_id for item in messages):
            return False
        confidence = self._float_value(decision.get("confidence"))
        if confidence < float(settings.get("semanticMinConfidence") or 0.78):
            return False
        intent = str(decision.get("intent") or "").strip()
        allowed = {str(item).strip() for item in (settings.get("semanticAllowedIntents") or [])}
        blocked = {str(item).strip() for item in (settings.get("semanticBlockedIntents") or [])}
        if intent in blocked:
            return False
        if allowed and intent not in allowed:
            return False
        return bool(str(decision.get("reply") or "").strip())

    def _parse_json(self, raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            self.logger.warning("semantic_reply_decision_parse_failed", raw=raw[:500])
            return {"should_reply": False, "reason": "invalid_json"}
        return parsed if isinstance(parsed, dict) else {"should_reply": False, "reason": "invalid_json"}

    def _message_from_row(self, row: dict[str, Any]) -> SemanticMessage:
        timestamp_raw = row.get("timestamp")
        try:
            timestamp = datetime.fromisoformat(str(timestamp_raw))
        except (TypeError, ValueError):
            timestamp = datetime.utcnow()
        return SemanticMessage(
            message_id=int(row.get("message_id") or 0),
            user_id=int(row.get("user_id") or 0),
            user_name=str(row.get("user_name") or ""),
            text=str(row.get("text") or ""),
            timestamp=timestamp,
        )

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").replace("\u200b", " ")).strip()[:1000]

    def _int_or_none(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float_value(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
