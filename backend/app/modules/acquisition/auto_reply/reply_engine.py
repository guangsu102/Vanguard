"""
Reply Engine Module

Intelligent reply generation and selection for keyword triggers.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Optional

import structlog

from app.core.keyword.engine import KeywordEngine, CompiledKeyword
from app.core.ai.intent_classifier import IntentClassifier, IntentType
from app.core.ai.llm_client import LLMClient
from app.core.runtime_settings import is_ai_reply_enabled
from app.modules.acquisition.auto_reply.templates import TemplateEngine
from app.modules.acquisition.constants import ResponseMode, INTENT_RESPONSE_MAP
from app.modules.acquisition.exceptions import TemplateNotFoundError

logger = structlog.get_logger()


@dataclass
class ReplyContext:
    """Context for reply generation."""
    user_id: int
    group_id: int
    user_name: Optional[str] = None
    group_name: Optional[str] = None
    matched_keywords: list[CompiledKeyword] = None
    intent: Optional[IntentType] = None
    conversation_history: list[dict] = None


@dataclass
class ReplyResult:
    """Result of reply generation."""
    content: str
    mode: ResponseMode
    should_send: bool
    template_id: Optional[int] = None


class ReplyEngine:
    """
    Intelligent reply engine for message responses.

    Generates appropriate replies based on:
    - Keyword matches
    - User intent classification
    - Conversation context
    - Template selection
    """

    def __init__(
        self,
        keyword_engine: KeywordEngine,
        intent_classifier: Optional[IntentClassifier] = None,
        template_engine: Optional[TemplateEngine] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        """
        Initialize ReplyEngine.

        Args:
            keyword_engine: Keyword engine for matching
            intent_classifier: Optional intent classifier
            template_engine: Optional template engine
            llm_client: Optional LLM client for dynamic replies
        """
        self.keyword_engine = keyword_engine
        self.intent_classifier = intent_classifier
        self.template_engine = template_engine or TemplateEngine()
        self.llm_client = llm_client or getattr(intent_classifier, "llm_client", None)
        self.logger = logger.bind(module="reply_engine")
        self._reply_lock = asyncio.Lock()

    def _ai_enabled(self) -> bool:
        return is_ai_reply_enabled()

    async def should_reply(self, message_text: str) -> bool:
        """
        Determine if a message should be replied to.

        Args:
            message_text: Message text to check

        Returns:
            True if should reply
        """
        # 检查关键词匹配
        matches = await self.keyword_engine.match(message_text)
        return len(matches) > 0

    async def generate_reply(
        self,
        message_text: str,
        context: ReplyContext,
    ) -> ReplyResult:
        """
        Generate a reply for a message.

        Args:
            message_text: Original message text
            context: Reply context

        Returns:
            ReplyResult with generated content
        """
        async with self._reply_lock:
            # 匹配关键词；触发器驱动的回复可能已经给出了命中的关键词。
            matched_keywords = (
                context.matched_keywords
                if context.matched_keywords is not None
                else await self.keyword_engine.match(message_text)
            )
            context.matched_keywords = matched_keywords

            if not matched_keywords:
                return ReplyResult(content="", mode=ResponseMode.IGNORE, should_send=False)

            # 意图分类。AI开关关闭时只走本地规则，避免分类阶段消耗 token。
            if self.intent_classifier and self._ai_enabled():
                intent_result = await self.intent_classifier.classify(message_text)
                context.intent = intent_result.intent
            else:
                context.intent = self._rule_based_intent(message_text)

            # 选择回复模式
            mode = self._select_reply_mode(context)

            # 生成回复内容
            content = await self._generate_content(message_text, context, mode)

            return ReplyResult(
                content=content,
                mode=mode,
                should_send=mode != ResponseMode.IGNORE,
            )

    def _select_reply_mode(self, context: ReplyContext) -> ResponseMode:
        """
        Select reply mode based on context.

        Args:
            context: Reply context

        Returns:
            Selected ResponseMode
        """
        if context.intent:
            intent_key = context.intent.value
            mode = INTENT_RESPONSE_MAP.get(intent_key, ResponseMode.TEMPLATE)
            if mode == ResponseMode.AI and (self.llm_client is None or not self._ai_enabled()):
                return ResponseMode.TEMPLATE
            return mode

        if self.llm_client is not None and self._ai_enabled():
            return ResponseMode.AI

        return ResponseMode.TEMPLATE

    async def _generate_content(
        self,
        message_text: str,
        context: ReplyContext,
        mode: ResponseMode,
    ) -> str:
        """
        Generate reply content based on mode.

        Args:
            message_text: Original message
            context: Reply context
            mode: Selected reply mode

        Returns:
            Generated reply content
        """
        if mode == ResponseMode.IGNORE:
            return ""

        if mode == ResponseMode.AI:
            return await self._generate_ai_reply(message_text, context)

        if mode == ResponseMode.TEMPLATE:
            return await self._generate_template_reply(context)

        if mode == ResponseMode.PRIVATE:
            return await self._generate_private_reply(context)

        if mode == ResponseMode.GROUP:
            return await self._generate_group_reply(context)

        # 默认模板回复
        return await self._generate_template_reply(context)

    async def _generate_ai_reply(
        self,
        message_text: str,
        context: ReplyContext,
    ) -> str:
        """Generate AI-powered reply."""
        if not self._ai_enabled():
            self.logger.info("ai_reply_disabled_by_settings")
            return await self._generate_template_reply(context)

        prompt = self._build_ai_prompt(message_text, context)
        llm_client = self.llm_client
        if llm_client is not None:
            try:
                return await llm_client.generate(
                    prompt=prompt,
                    model="gpt-4o-mini",
                    temperature=0.6,
                    max_tokens=180,
                    system_prompt="你是一个中文Telegram社群客服助手，回复要简洁、自然、友好，不要提及你是AI。",
                )
            except Exception as exc:
                self.logger.warning("ai_reply_failed", error=str(exc))

        return "感谢您的提问！有需要可以私信了解更多~"

    async def _generate_template_reply(self, context: ReplyContext) -> str:
        """Generate template-based reply."""
        if not context.matched_keywords:
            return ""

        # 获取第一个匹配的关键词
        matched = context.matched_keywords[0]

        # 获取模板
        template = await self.template_engine.get_template_by_keyword(matched.id)
        if not template:
            return self._get_default_reply(matched.text)

        # 渲染模板
        return self.template_engine.render(
            template,
            user_name=context.user_name or "朋友",
            group_name=context.group_name or "",
        )

    async def _generate_private_reply(self, context: ReplyContext) -> str:
        """Generate private chat reply with invite."""
        return "已收到您的消息！有更多问题欢迎私信咨询，我会为您详细介绍~"

    async def _generate_group_reply(self, context: ReplyContext) -> str:
        """Generate group chat reply."""
        if not context.matched_keywords:
            return ""

        matched = context.matched_keywords[0]

        # 根据关键词类型生成不同回复
        replies = {
            "demand": "有需要可以试试哦，欢迎私信了解~",
            "inquiry": "这个功能挺不错的，需要可以试试",
            "price": "价格挺实惠的，有需要可以了解一下",
            "competitor": "各有各的优势吧",
        }

        keyword_type = matched.keyword_type.value if hasattr(matched, 'keyword_type') else "demand"
        return replies.get(keyword_type, "需要可以试试~")

    def _get_default_reply(self, keyword: str) -> str:
        """Get default reply for keyword."""
        defaults = [
            "这个话题不错，有需要可以了解一下",
            "说得对，需要可以试试",
            "有道理，欢迎私信了解更多",
        ]
        return random.choice(defaults)

    def _build_ai_prompt(self, message_text: str, context: ReplyContext) -> str:
        """Build prompt for AI reply generation."""
        keywords = ", ".join(k.text for k in (context.matched_keywords or [])) or "无"
        history = context.conversation_history or []
        history_text = "\n".join(f"{item.get('role')}: {item.get('content')}" for item in history[-6:])
        return (
            f"你是一个中文客服助手。\n"
            f"当前消息: {message_text}\n"
            f"匹配关键词: {keywords}\n"
            f"用户意图: {context.intent.value if context.intent else 'unknown'}\n"
            f"最近对话:\n{history_text}\n"
            f"请给出简洁自然的回复。"
        )

    def _rule_based_intent(self, text: str) -> IntentType:
        """Rule-based intent classification fallback."""
        text_lower = text.lower()

        if any(kw in text_lower for kw in ["买", "想要", "试试", "套餐", "推荐"]):
            return IntentType.DEMAND
        if any(kw in text_lower for kw in ["怎么", "如何", "什么", "能", "支持"]):
            return IntentType.INQUIRY
        if any(kw in text_lower for kw in ["价格", "多少", "钱"]):
            return IntentType.PRICE
        if any(kw in text_lower for kw in ["谢谢", "好的", "ok", "👋"]):
            return IntentType.CHITCHAT

        return IntentType.OTHER

    async def select_reply_mode_for_trigger(
        self,
        keyword_type: str,
        user_intent: Optional[IntentType] = None,
    ) -> ResponseMode:
        """
        Select reply mode for a keyword trigger.

        Args:
            keyword_type: Type of triggered keyword
            user_intent: Detected user intent

        Returns:
            Recommended ResponseMode
        """
        # 高意向用户 -> 私聊
        if user_intent in [IntentType.DEMAND, IntentType.PRICE]:
            return ResponseMode.PRIVATE

        # 咨询类 -> 群内回复
        if user_intent == IntentType.INQUIRY:
            return ResponseMode.GROUP

        # 根据关键词类型
        keyword_modes = {
            "demand": ResponseMode.PRIVATE,
            "inquiry": ResponseMode.GROUP,
            "price": ResponseMode.PRIVATE,
            "competitor": ResponseMode.IGNORE,
        }

        return keyword_modes.get(keyword_type, ResponseMode.TEMPLATE)
