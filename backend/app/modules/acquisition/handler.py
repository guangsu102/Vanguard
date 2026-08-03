"""
Acquisition Event Handler

Main event handler for Telegram messages in the acquisition bot.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.keyword.engine import KeywordEngine
from app.core.group.manager import GroupManager
from app.core.ai.intent_classifier import IntentClassifier
from app.core.ai.llm_client import LLMClient, LLMProvider

from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.search import Searcher, GroupFinder
from app.modules.acquisition.auto_reply import Speaker, ReplyEngine, SemanticGroupReplyEngine, TemplateEngine
from app.modules.acquisition.keyword_trigger import KeywordMatcher, TriggerHandler
from app.modules.acquisition.private_msg import PrivateHandler, DialogManager, WelcomeGenerator, GuideFlowManager
from app.modules.acquisition.tracking import Tracker, URLBuilder
from app.modules.acquisition.context import ContextManager

logger = structlog.get_logger()


@dataclass
class MessageEvent:
    """Telegram message event."""
    message_id: int
    chat_id: int
    sender_id: int
    sender_name: str
    content: str
    is_group: bool
    timestamp: datetime
    account_id: Optional[int] = None
    sender_name_resolver: Optional[Callable[[], Awaitable[str]]] = None


@dataclass
class MemberJoinEvent:
    """Member joined event."""
    user_id: int
    user_name: str
    chat_id: int
    inviter_id: Optional[int] = None


@dataclass
class CallbackQueryEvent:
    """Callback query event."""
    query_id: str
    user_id: int
    data: str


class AcquisitionEventHandler:
    """
    Main event handler for acquisition bot.

    Coordinates all acquisition modules to process Telegram events
    and route them to appropriate handlers.
    """

    def __init__(
        self,
        db: AsyncSession,
        account_pool: AccountPool,
        config: Optional[AcquisitionConfig] = None,
    ):
        """
        Initialize AcquisitionEventHandler.

        Args:
            db: Database session
            account_pool: Account pool for operations
            config: Optional configuration
        """
        self.db = db
        self.account_pool = account_pool
        self.config = config or AcquisitionConfig()
        self.logger = logger.bind(module="acquisition_handler")

        # 初始化组件
        self._init_components()

    def _init_components(self) -> None:
        """Initialize all acquisition components."""
        # 核心组件
        self.keyword_engine = KeywordEngine(self.db)
        self.group_manager = GroupManager(self.db)
        self.llm_client = LLMClient(provider=LLMProvider.OPENAI)
        self.intent_classifier = IntentClassifier(llm_client=self.llm_client)

        # 搜索模块
        self.group_finder = GroupFinder(self.account_pool)
        self.searcher = Searcher(
            db=self.db,
            account_pool=self.account_pool,
            group_manager=self.group_manager,
            keyword_engine=self.keyword_engine,
            config=self.config,
        )

        # 发言模块
        self.template_engine = TemplateEngine(self.db)
        self.speaker = Speaker(
            db=self.db,
            account_pool=self.account_pool,
            group_manager=self.group_manager,
            template_engine=self.template_engine,
            config=self.config,
        )
        self.reply_engine = ReplyEngine(
            keyword_engine=self.keyword_engine,
            intent_classifier=self.intent_classifier,
            template_engine=self.template_engine,
            llm_client=self.llm_client,
        )
        self.semantic_reply_engine = SemanticGroupReplyEngine(
            db=self.db,
            account_pool=self.account_pool,
            llm_client=self.llm_client,
        )

        # 追踪模块
        self.url_builder = URLBuilder(config=self.config.tracking)
        self.tracker = Tracker(
            db=self.db,
            url_builder=self.url_builder,
            config=self.config,
        )

        # 对话模块
        self.dialog_manager = DialogManager(self.db)
        self.welcome_generator = WelcomeGenerator()
        self.guide_flow_manager = GuideFlowManager(
            db=self.db,
            url_builder=self.url_builder,
        )

        # 触发模块
        self.keyword_matcher = KeywordMatcher(
            db=self.db,
            keyword_engine=self.keyword_engine,
        )

        # 私聊处理
        self.private_handler = PrivateHandler(
            db=self.db,
            account_pool=self.account_pool,
            dialog_manager=self.dialog_manager,
            guide_flow_manager=self.guide_flow_manager,
            tracker=self.tracker,
            config=self.config,
        )

        # 触发处理
        self.trigger_handler = TriggerHandler(
            db=self.db,
            account_pool=self.account_pool,
            keyword_matcher=self.keyword_matcher,
            reply_engine=self.reply_engine,
            private_handler=self.private_handler,
            tracker=self.tracker,
            config=self.config,
        )

        # 上下文管理
        self.context_manager = ContextManager(self.db)

    async def initialize(self) -> None:
        """Initialize components that need async setup."""
        await self.keyword_engine.load_keywords()
        await self.keyword_matcher.load_triggers()
        await self.template_engine.load_templates()
        self.logger.info("acquisition_handler_initialized")

    async def on_message(self, event: MessageEvent) -> None:
        """
        Handle incoming message event.

        Args:
            event: Message event
        """
        self.logger.info(
            "processing_message",
            message_id=event.message_id,
            chat_id=event.chat_id,
            sender_id=event.sender_id,
            is_group=event.is_group,
        )

        # 更新上下文
        await self.context_manager.set_user_context(
            user_id=event.sender_id,
            group_id=event.chat_id if event.is_group else None,
            metadata={
                "sender_name": event.sender_name,
                "last_message_id": event.message_id,
            },
        )

        if event.is_group:
            await self._handle_group_message(event)
        else:
            await self._handle_private_message(event)

    async def _handle_group_message(self, event: MessageEvent) -> None:
        """Handle group message."""
        context = {
            "user_name": event.sender_name,
            "group_id": event.chat_id,
        }

        async def resolve_context_on_match() -> dict:
            if event.sender_name_resolver is None:
                return context

            try:
                sender_name = await event.sender_name_resolver()
            except Exception as exc:
                self.logger.debug(
                    "sender_name_resolve_failed",
                    sender_id=event.sender_id,
                    chat_id=event.chat_id,
                    error=str(exc),
                )
                return context

            if sender_name:
                event.sender_name = sender_name
                context["user_name"] = sender_name
                await self.context_manager.set_user_context(
                    user_id=event.sender_id,
                    group_id=event.chat_id,
                    metadata={
                        "sender_name": sender_name,
                        "last_message_id": event.message_id,
                    },
                )
            return context

        # 1. 关键词触发检测
        trigger_results = await self.trigger_handler.handle_message(
            message_text=event.content,
            user_id=event.sender_id,
            group_id=event.chat_id,
            message_id=event.message_id,
            context=context,
            context_resolver=resolve_context_on_match,
        )

        if trigger_results:
            self.logger.info(
                "triggers_executed",
                message_id=event.message_id,
                trigger_count=len(trigger_results),
            )

        # 2. 更新对话上下文
        await self.dialog_manager.add_message(
            user_id=event.sender_id,
            role="user",
            content=event.content,
        )

        # 3. 语义识别真实群聊消息，必要时只回复一条最合适的消息。
        semantic_result = await self.semantic_reply_engine.process_message(
            account_id=event.account_id,
            group_id=event.chat_id,
            message_id=event.message_id,
            user_id=event.sender_id,
            user_name=event.sender_name,
            text=event.content,
            timestamp=event.timestamp,
        )
        if semantic_result.sent:
            self.logger.info(
                "semantic_group_reply_sent",
                message_id=event.message_id,
                target_message_id=semantic_result.target_message_id,
                intent=semantic_result.intent,
                confidence=semantic_result.confidence,
            )

    async def _handle_private_message(self, event: MessageEvent) -> None:
        """Handle private message."""
        result = await self.private_handler.handle_message(
            user_id=event.sender_id,
            message_text=event.content,
            source="private",
        )

        if result.success:
            self.logger.info(
                "private_message_handled",
                user_id=event.sender_id,
                action=result.action_taken,
            )
        else:
            self.logger.error(
                "private_message_failed",
                user_id=event.sender_id,
                error=result.error,
            )

    async def on_member_joined(self, event: MemberJoinEvent) -> None:
        """
        Handle member joined event.

        Args:
            event: Member join event
        """
        self.logger.info(
            "member_joined",
            user_id=event.user_id,
            chat_id=event.chat_id,
        )

        # 发送欢迎消息（私聊）
        success = await self.private_handler.send_welcome(
            user_id=event.user_id,
            source_info={
                "source": "group_join",
                "group_id": event.chat_id,
                "user_name": event.user_name,
            },
        )

        if success:
            # 创建引导流程
            await self.guide_flow_manager.start_flow(
                user_id=event.user_id,
                source_info={
                    "source": "group_join",
                    "group_id": event.chat_id,
                },
            )

    async def on_callback_query(self, event: CallbackQueryEvent) -> None:
        """
        Handle callback query event.

        Args:
            event: Callback query event
        """
        self.logger.info(
            "callback_query",
            query_id=event.query_id,
            user_id=event.user_id,
            data=event.data,
        )

        # 处理按钮回调
        if event.data.startswith("guide_"):
            step = event.data.replace("guide_", "")
            await self.guide_flow_manager.skip_to_step(event.user_id, step)

        elif event.data == "register":
            await self.private_handler.send_registration_link(event.user_id)

    async def on_command(self, event: MessageEvent) -> None:
        """
        Handle command message.

        Args:
            event: Command message event
        """
        command = event.content.split()[0].lower() if event.content else ""

        if command == "/start":
            # 解析 /start 后面的追踪码
            parts = event.content.split()
            tracking_code = parts[1] if len(parts) > 1 else None

            if tracking_code:
                try:
                    await self.tracker.record_click(tracking_code)
                except Exception as e:
                    self.logger.warning("tracking_code_error", code=tracking_code, error=str(e))

            await self._handle_private_message(event)

        elif command == "/help":
            await self._handle_private_message(event)

    async def cleanup(self) -> None:
        """Cleanup resources."""
        self.logger.info("acquisition_handler_cleanup")

        cleanup_targets = [
            getattr(self, "context_manager", None),
            getattr(self, "dialog_manager", None),
        ]

        for target in cleanup_targets:
            if target is None:
                continue
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    self.logger.warning("cleanup_failed", target=type(target).__name__, error=str(exc))

        for pool in [getattr(self, "account_pool", None)]:
            if pool is None:
                continue
            close_all = getattr(pool, "close_all", None)
            if callable(close_all):
                try:
                    await close_all()
                except Exception as exc:
                    self.logger.warning("pool_cleanup_failed", error=str(exc))
