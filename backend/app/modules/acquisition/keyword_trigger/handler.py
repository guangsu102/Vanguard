"""
Trigger Handler Module

Handles keyword trigger events and executes appropriate actions.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.keyword.engine import CompiledKeyword
from app.core.account.risk_guard import AccountRiskGuard
from app.core.automation_settings import (
    get_group_ai_interaction_settings,
    is_ai_reply_enabled,
    is_keyword_private_reply_enabled,
)
from app.modules.acquisition.keyword_trigger.matcher import KeywordMatcher, TriggerMatch
from app.modules.acquisition.keyword_trigger.actions import ActionExecutor, TriggerActionType
from app.modules.acquisition.auto_reply.reply_engine import ReplyContext, ReplyEngine
from app.modules.acquisition.constants import ResponseMode
from app.modules.acquisition.private_msg.private_handler import PrivateHandler
from app.modules.acquisition.tracking.tracker import Tracker
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.models import TriggerRecord, TriggerAction
from app.modules.acquisition.rate_limit import AcquisitionRateLimitService

logger = structlog.get_logger()


@dataclass
class TriggerResult:
    """Result of trigger handling."""
    success: bool
    action_taken: TriggerActionType
    reply_content: Optional[str] = None
    error: Optional[str] = None
    matched_keyword: Optional[str] = None
    executed_at: datetime = field(default_factory=datetime.utcnow)


class TriggerHandler:
    """
    Handler for keyword trigger events.

    Coordinates matching, action execution, and result recording.
    """

    def __init__(
        self,
        db: AsyncSession,
        account_pool: AccountPool,
        keyword_matcher: KeywordMatcher,
        reply_engine: ReplyEngine,
        private_handler: PrivateHandler,
        tracker: Tracker,
        config: Optional[AcquisitionConfig] = None,
    ):
        """
        Initialize TriggerHandler.

        Args:
            db: Database session
            account_pool: Account pool for operations
            keyword_matcher: Keyword matcher instance
            reply_engine: Reply engine for generating responses
            private_handler: Private message handler
            tracker: Tracking module
            config: Optional configuration
        """
        self.db = db
        self.account_pool = account_pool
        self.keyword_matcher = keyword_matcher
        self.reply_engine = reply_engine
        self.private_handler = private_handler
        self.tracker = tracker
        self.config = config or AcquisitionConfig()
        self.risk_guard = AccountRiskGuard(db)
        self.action_executor = ActionExecutor(account_pool, risk_guard=self.risk_guard)
        self.logger = logger.bind(module="trigger_handler")
        self._handler_lock = asyncio.Lock()
        self.rate_limit_service = AcquisitionRateLimitService(
            key_prefix="acquisition:trigger:",
            config=self.config,
        )

    async def handle_message(
        self,
        message_text: str,
        user_id: int,
        group_id: int,
        message_id: int,
        context: Optional[dict] = None,
        context_resolver: Optional[Callable[[], Awaitable[dict]]] = None,
    ) -> list[TriggerResult]:
        """
        Handle a message and execute triggers.

        Args:
            message_text: Message text
            user_id: Sender user ID
            group_id: Group ID
            message_id: Telegram message ID
            context: Optional context data

        Returns:
            List of TriggerResult for each matched trigger
        """
        async with self._handler_lock:
            results = []

            # 匹配关键词
            matches = await self.keyword_matcher.match(message_text, group_id)
            if not matches:
                return results

            # 仅对真实关键词命中消耗限流额度。
            if not await self._check_rate_limit(user_id, group_id):
                self.logger.debug("rate_limit_exceeded", user_id=user_id, group_id=group_id)
                return results

            if matches and context_resolver is not None:
                try:
                    resolved_context = await context_resolver()
                    if resolved_context is not None:
                        context = resolved_context
                except Exception as exc:
                    self.logger.debug(
                        "trigger_context_resolve_failed",
                        user_id=user_id,
                        group_id=group_id,
                        error=str(exc),
                    )

            for match in matches:
                try:
                    result = await self._handle_match(
                        match=match,
                        message_text=message_text,
                        user_id=user_id,
                        group_id=group_id,
                        message_id=message_id,
                        context=context,
                    )
                    results.append(result)
                except Exception as e:
                    self.logger.error(
                        "trigger_handle_error",
                        match=match,
                        error=str(e),
                    )
                    results.append(TriggerResult(
                        success=False,
                        action_taken=TriggerActionType.NONE,
                        error=str(e),
                        matched_keyword=match.keyword_text,
                    ))

            return results

    async def _handle_match(
        self,
        match: TriggerMatch,
        message_text: str,
        user_id: int,
        group_id: int,
        message_id: int,
        context: Optional[dict],
    ) -> TriggerResult:
        """Handle a single trigger match."""
        self.logger.info(
            "handling_trigger",
            keyword=match.keyword_text,
            user_id=user_id,
            group_id=group_id,
        )

        # 获取触发配置
        trigger = await self.keyword_matcher.get_trigger_by_id(match.trigger_id)
        if not trigger and match.keyword_id:
            trigger = await self.keyword_matcher.get_trigger_for_keyword(match.keyword_id)
        if not trigger:
            return TriggerResult(
                success=False,
                action_taken=TriggerActionType.NONE,
                matched_keyword=match.keyword_text,
                error="Trigger config not found",
            )

        # 确定动作类型
        action_type = self._map_trigger_action(trigger.action)

        # 执行动作
        if action_type == TriggerActionType.REPLY:
            return await self._execute_reply(
                trigger=trigger,
                match=match,
                message_text=message_text,
                user_id=user_id,
                group_id=group_id,
                message_id=message_id,
                context=context,
            )
        elif action_type == TriggerActionType.PRIVATE_MESSAGE:
            return await self._execute_private_message(
                trigger=trigger,
                match=match,
                user_id=user_id,
                group_id=group_id,
                context=context,
            )
        elif action_type == TriggerActionType.REACT:
            return await self._execute_reaction(
                match=match,
                group_id=group_id,
                message_id=message_id,
            )
        else:
            return TriggerResult(
                success=True,
                action_taken=action_type,
                matched_keyword=match.keyword_text,
            )

    async def _execute_reply(
        self,
        trigger,
        match: TriggerMatch,
        message_text: str,
        user_id: int,
        group_id: int,
        message_id: int,
        context: Optional[dict],
    ) -> TriggerResult:
        """Execute reply action."""
        group_ai_settings = await get_group_ai_interaction_settings(self.db)
        if not group_ai_settings.get("allowKeywordTriggeredReply", False):
            self.logger.info(
                "keyword_group_reply_paused",
                trigger_id=getattr(trigger, "id", None),
                keyword=match.keyword_text,
                group_id=group_id,
            )
            return TriggerResult(
                success=True,
                action_taken=TriggerActionType.NONE,
                matched_keyword=match.keyword_text,
            )

        # 生成回复
        reply_content = await self._generate_reply(
            trigger=trigger,
            match=match,
            message_text=message_text,
            user_id=user_id,
            group_id=group_id,
            context=context,
        )

        if not reply_content:
            return TriggerResult(
                success=True,
                action_taken=TriggerActionType.NONE,
                matched_keyword=match.keyword_text,
            )

        # 获取账号
        account = await self.account_pool.acquire(purpose="reply")
        try:
            if account is None:
                return TriggerResult(
                    success=False,
                    action_taken=TriggerActionType.REPLY,
                    error="No available telegram account",
                    matched_keyword=match.keyword_text,
                )

            # 发送回复
            await self.action_executor.send_group_reply(
                account=account,
                group_id=group_id,
                message=reply_content,
                reply_to=message_id,
            )

            # 记录
            record_action = (
                TriggerAction.REPLY_AI
                if trigger.action == TriggerAction.REPLY_AI
                else TriggerAction.REPLY_TEMPLATE
            )
            await self._record_trigger(
                trigger=trigger,
                match=match,
                user_id=user_id,
                group_id=group_id,
                message_id=message_id,
                action=record_action,
                content=reply_content,
            )

            return TriggerResult(
                success=True,
                action_taken=TriggerActionType.REPLY,
                reply_content=reply_content,
                matched_keyword=match.keyword_text,
            )

        finally:
            await self.account_pool.release(account)

    async def _execute_private_message(
        self,
        trigger,
        match: TriggerMatch,
        user_id: int,
        group_id: int,
        context: Optional[dict],
    ) -> TriggerResult:
        """Execute private message action."""
        if not await is_keyword_private_reply_enabled(self.db):
            self.logger.info(
                "keyword_private_reply_paused",
                trigger_id=getattr(trigger, "id", None),
                keyword=match.keyword_text,
                user_id=user_id,
                group_id=group_id,
            )
            return TriggerResult(
                success=True,
                action_taken=TriggerActionType.NONE,
                matched_keyword=match.keyword_text,
            )

        # 先按触发器配置生成私聊内容；未配置时退回默认注册链接引导。
        message = await self._generate_private_message(
            trigger=trigger,
            match=match,
            user_id=user_id,
            group_id=group_id,
            context=context,
        )

        # 发送私聊
        success = await self.private_handler.send_message(
            user_id=user_id,
            message=message,
        )

        if success:
            await self._record_trigger(
                trigger=trigger,
                match=match,
                user_id=user_id,
                group_id=group_id,
                message_id=None,
                action=TriggerAction.SEND_PRIVATE,
                content=message,
            )

        return TriggerResult(
            success=success,
            action_taken=TriggerActionType.PRIVATE_MESSAGE,
            reply_content=message if success else None,
            matched_keyword=match.keyword_text,
        )

    async def _execute_reaction(
        self,
        match: TriggerMatch,
        group_id: int,
        message_id: int,
    ) -> TriggerResult:
        """Execute reaction action."""
        account = await self.account_pool.acquire(purpose="react")
        try:
            await self.action_executor.send_reaction(
                account=account,
                group_id=group_id,
                message_id=message_id,
                emoji="👍",
            )

            return TriggerResult(
                success=True,
                action_taken=TriggerActionType.REACT,
                matched_keyword=match.keyword_text,
            )

        finally:
            await self.account_pool.release(account)

    async def _generate_reply(
        self,
        trigger,
        match: TriggerMatch,
        message_text: str,
        user_id: int,
        group_id: int,
        context: Optional[dict],
    ) -> Optional[str]:
        """Generate reply content."""
        # 如果配置了模板
        if trigger.template_id:
            template = await self.reply_engine.template_engine.get_template(trigger.template_id)
            if template:
                return self.reply_engine.template_engine.render(
                    template,
                    user_name=context.get("user_name") if context else None,
                    group_name=context.get("group_name") if context else None,
                )

        if trigger.action == TriggerAction.REPLY_AI or trigger.use_ai_reply:
            compiled = self._compiled_keyword_from_match(match)
            reply_context = ReplyContext(
                user_id=user_id,
                group_id=group_id,
                user_name=context.get("user_name") if context else None,
                group_name=context.get("group_name") if context else None,
                matched_keywords=[compiled],
            )
            result = await self.reply_engine.generate_reply(message_text, reply_context)
            if result.should_send and result.mode != ResponseMode.IGNORE:
                return result.content

        # 默认回复
        return None

    async def _generate_private_message(
        self,
        trigger,
        match: TriggerMatch,
        user_id: int,
        group_id: int,
        context: Optional[dict],
    ) -> str:
        """Generate private message content for a keyword trigger."""
        register_link = await self.tracker.generate_tracking_link(
            user_id,
            group_id=group_id,
            keyword=match.keyword_text,
        )

        if trigger.template_id:
            template = await self.reply_engine.template_engine.get_template(trigger.template_id)
            if template:
                return self.reply_engine.template_engine.render(
                    template,
                    user_name=context.get("user_name") if context else None,
                    group_name=context.get("group_name") if context else None,
                    register_link=register_link,
                    keyword=match.keyword_text,
                )

        if trigger.use_ai_reply and await is_ai_reply_enabled(self.db):
            prompt = (
                f"触发关键词：{match.keyword_text}\n"
                f"来源群：{context.get('group_name') if context else ''}\n"
                f"用户昵称：{context.get('user_name') if context else ''}\n"
                f"注册链接：{register_link}\n"
                "请生成一条将要私聊发送给用户的中文营销客服回复。"
            )
            llm_client = getattr(self.reply_engine, "llm_client", None)
            if llm_client is not None:
                try:
                    return await llm_client.generate(
                        prompt=prompt,
                        model=llm_client.model_for("fast"),
                        temperature=0.6,
                        max_tokens=180,
                        system_prompt=(
                            "你是一个中文Telegram营销客服助手。回复要简洁、自然、友好，"
                            "不要提及你是AI，不要夸大承诺，不要超过120字。"
                        ),
                    )
                except Exception as exc:
                    self.logger.warning("private_ai_reply_failed", error=str(exc))

        return await self.private_handler.generate_invite_message(
            user_id=user_id,
            source_group_id=group_id,
            keyword=match.keyword_text,
        )

    def _compiled_keyword_from_match(self, match: TriggerMatch) -> CompiledKeyword:
        """Build a minimal compiled keyword for trigger-driven replies."""
        import re
        from types import SimpleNamespace

        from app.core.keyword.models import KeywordType, MatchMode

        return CompiledKeyword(
            id=match.keyword_id,
            text=match.keyword_text,
            pattern=re.compile(re.escape(match.keyword_text), re.IGNORECASE),
            keyword_type=KeywordType.DEMAND,
            match_mode=MatchMode.FUZZY,
            keyword=SimpleNamespace(id=match.keyword_id, text=match.keyword_text),
        )

    async def _record_trigger(
        self,
        trigger,
        match: TriggerMatch,
        user_id: int,
        group_id: int,
        message_id: Optional[int],
        action: TriggerAction,
        content: str,
    ) -> None:
        """Record trigger execution to database."""
        record = TriggerRecord(
            trigger_id=trigger.id,
            user_id=user_id,
            group_id=group_id,
            message_id=message_id,
            matched_keyword=match.keyword_text,
            user_message=match.matched_text,
            action_taken=action,
            reply_content=content,
        )
        self.db.add(record)
        await self.db.commit()

    async def _check_rate_limit(self, user_id: int, group_id: int) -> bool:
        """Check if rate limit allows trigger."""
        trigger_config = self.config.trigger

        user_limit = max(1, trigger_config.max_triggers_per_user)
        group_limit = max(1, trigger_config.max_triggers_per_group)
        cooldown = max(1, trigger_config.cooldown_seconds)

        user_daily_key = self.rate_limit_service.build_key("trigger", "user_daily", user_id)
        group_daily_key = self.rate_limit_service.build_key("trigger", "group_daily", group_id)
        cooldown_key = self.rate_limit_service.build_key("trigger", "cooldown", user_id, group_id)

        allowed = await self.rate_limit_service.allow_daily(user_daily_key, rate=user_limit)
        if not allowed:
            return False

        return await self.rate_limit_service.check_daily_and_cooldown(
            daily_key=group_daily_key,
            cooldown_key=cooldown_key,
            daily_rate=group_limit,
            cooldown_seconds=cooldown,
        )

    def _map_trigger_action(self, action: TriggerAction) -> TriggerActionType:
        """Map model action to action type."""
        mapping = {
            TriggerAction.REPLY_TEMPLATE: TriggerActionType.REPLY,
            TriggerAction.REPLY_AI: TriggerActionType.REPLY,
            TriggerAction.SEND_PRIVATE: TriggerActionType.PRIVATE_MESSAGE,
            TriggerAction.REACT: TriggerActionType.REACT,
            TriggerAction.PIN_MESSAGE: TriggerActionType.PIN,
        }
        return mapping.get(action, TriggerActionType.NONE)
