"""
Speaker Module

Automatic message sending scheduler and executor for group messaging.
"""

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.ai.llm_client import LLMClient, LLMProvider
from app.core.automation_settings import get_group_ai_interaction_settings
from app.core.config import settings
from app.core.group.manager import GroupManager
from app.core.group.models import Group, GroupLevel
from app.modules.acquisition.auto_reply.safety import sanitize_natural_group_reply
from app.modules.acquisition.auto_reply.templates import TemplateEngine
from app.modules.acquisition.auto_reply.scheduler import SpeakScheduler
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.models import AcquisitionMessage, MessageType
from app.modules.acquisition.exceptions import MessageSendError
from app.modules.acquisition.rate_limit import AcquisitionRateLimitService

logger = structlog.get_logger()


@dataclass
class SpeakResult:
    """Result of a speak operation."""
    success: bool
    message_id: Optional[int] = None
    group_id: Optional[int] = None
    account_id: Optional[int] = None
    error: Optional[str] = None
    sent_at: datetime = field(default_factory=datetime.utcnow)


class Speaker:
    """
    Automatic message sender for Telegram groups.

    Manages scheduled and on-demand message sending to groups,
    respecting rate limits and group-specific configurations.
    """

    def __init__(
        self,
        db: AsyncSession,
        account_pool: AccountPool,
        group_manager: GroupManager,
        template_engine: TemplateEngine,
        config: Optional[AcquisitionConfig] = None,
    ):
        """
        Initialize Speaker.

        Args:
            db: Database session
            account_pool: Account pool for sending messages
            group_manager: Group manager for group information
            template_engine: Template engine for message generation
            config: Optional configuration override
        """
        self.db = db
        self.account_pool = account_pool
        self.group_manager = group_manager
        self.template_engine = template_engine
        self.config = config or AcquisitionConfig()
        self.scheduler = SpeakScheduler(self.config)
        self.logger = logger.bind(module="speaker")
        self.risk_guard = AccountRiskGuard(db)
        self.telegram_execution = TelegramExecutionService(self.risk_guard)
        self.rate_limit_service = AcquisitionRateLimitService(
            key_prefix="acquisition:speak:",
            config=self.config,
        )
        self._llm_client: Optional[LLMClient] = None

    async def _group_ai_settings(self) -> dict:
        return await get_group_ai_interaction_settings(self.db)

    async def _proactive_warmup_enabled(self) -> bool:
        settings = await self._group_ai_settings()
        return bool(settings.get("enabled") and settings.get("allowProactiveWarmup"))

    async def speak_in_group(
        self,
        group_id: int,
        message: str,
        account_id: Optional[int] = None,
    ) -> SpeakResult:
        """
        Send a message in a specified group.

        Args:
            group_id: Target group ID
            message: Message content
            account_id: Optional specific account to use

        Returns:
            SpeakResult with operation status
        """
        self.logger.info("speak_in_group", group_id=group_id, message_length=len(message))

        if not message:
            if not await self._proactive_warmup_enabled():
                self.logger.info("proactive_group_warmup_paused", group_id=group_id)
                return SpeakResult(success=False, error="Proactive group warmup disabled", group_id=group_id)
            message = await self._generate_proactive_warmup_message(group_id)
            if not message:
                return SpeakResult(success=False, error="Proactive group warmup skipped", group_id=group_id)
            message_type = MessageType.AI_WARMUP
        else:
            message_type = None

        # Select account for sending.
        account = None
        if account_id:
            try:
                account = await self.account_pool.acquire_by_id(account_id, purpose="proactive_group_warmup")
            except Exception as exc:
                self.logger.warning("specific_account_unavailable", account_id=account_id, error=str(exc))
                account = None
            if account is None:
                return SpeakResult(
                    success=False,
                    error="Specific account unavailable",
                    group_id=group_id,
                    account_id=account_id,
                )
        if not account:
            try:
                account = await self.account_pool.acquire(purpose="speak")
            except Exception as e:
                self.logger.error("no_available_account", error=str(e))
                return SpeakResult(success=False, error="No available account", group_id=group_id)

        try:
            # 获取群组配置
            group = await self.group_manager.get_group_by_id(group_id)
            if not group:
                return SpeakResult(success=False, error="Group not found", group_id=group_id)

            group_config = self._get_group_config(group)

            # 检查限流
            if not await self._check_rate_limit(group_id, account.account_id, group_config):
                return SpeakResult(
                    success=False,
                    error="Rate limit exceeded",
                    group_id=group_id,
                    account_id=account.account_id,
                )

            # 发送消息
            msg_id = await self._send_message(account, group_id, message)

            # 记录消息
            await self._record_message(account.account_id, group_id, message, msg_id, message_type=message_type)

            # 随机延迟（模拟人类行为）
            await self._random_delay()

            return SpeakResult(
                success=True,
                message_id=msg_id,
                group_id=group_id,
                account_id=account.account_id,
            )

        except Exception as e:
            self.logger.error("speak_failed", group_id=group_id, error=str(e))
            return SpeakResult(success=False, error=str(e), group_id=group_id, account_id=account.account_id)

        finally:
            if account:
                await self.account_pool.release(account)

    async def execute_schedule(
        self,
        schedule: SpeakScheduler,
    ) -> list[SpeakResult]:
        """
        Execute a speak schedule.

        Args:
            schedule: Speak schedule to execute

        Returns:
            List of SpeakResult for each message
        """
        self.logger.info("execute_schedule", schedule_name=schedule.name)
        if not await self._proactive_warmup_enabled():
            self.logger.info("execute_schedule_paused_by_group_ai_config", schedule_name=schedule.name)
            return []

        results = []

        for task in schedule.tasks:
            # 检查是否应该执行
            if not schedule.should_execute_task(task):
                continue

            # 获取群组
            group = await self.group_manager.get_group_by_id(task.group_id)
            if not group:
                continue

            # 生成消息内容
            content = await self._generate_message_content(task.message_type)

            # 发送
            result = await self.speak_in_group(task.group_id, content)
            results.append(result)

            # 延迟
            await asyncio.sleep(self.config.speaker.min_interval_seconds)

        return results

    async def speak_in_level_groups(
        self,
        level: GroupLevel,
        message: Optional[str] = None,
        message_type: Optional[MessageType] = None,
    ) -> list[SpeakResult]:
        """
        Send messages to all groups of a specific level.

        Args:
            level: Target group level
            message: Optional specific message content
            message_type: Optional message type for template selection

        Returns:
            List of SpeakResult for each group
        """
        self.logger.info("speak_in_level_groups", level=level.value)
        if not await self._proactive_warmup_enabled():
            self.logger.info("speak_in_level_groups_paused_by_group_ai_config", level=level.value)
            return []

        groups = await self.group_manager.get_groups_by_level(level)
        results = []

        for group in groups:
            if message:
                content = message
            else:
                content = await self._generate_message_content(message_type or MessageType.INTERACTION)

            result = await self.speak_in_group(group.group_id, content)
            results.append(result)

            # 遵守间隔
            await asyncio.sleep(self.config.speaker.min_interval_seconds)

        return results

    def _get_llm_client(self) -> Optional[LLMClient]:
        provider = (
            LLMProvider(settings.LLM_PROVIDER)
            if settings.LLM_PROVIDER in {item.value for item in LLMProvider}
            else LLMProvider.OPENAI
        )
        api_key = settings.OPENAI_API_KEY if provider == LLMProvider.OPENAI else settings.ANTHROPIC_API_KEY
        if provider != LLMProvider.LOCAL and not api_key:
            return None
        if self._llm_client is None:
            self._llm_client = LLMClient(provider=provider, api_key=api_key)
        return self._llm_client

    async def _generate_proactive_warmup_message(self, group_id: int) -> str:
        ai_settings = await self._group_ai_settings()
        override = self._get_group_warmup_override(ai_settings, group_id)
        if override is not None and not override.get("enabled", True):
            self.logger.info("proactive_warmup_disabled_for_group", group_id=group_id)
            return ""

        fallback = self._configured_warmup_template(ai_settings, group_id, override=override)
        if not fallback:
            fallback = await self._generate_message_content(MessageType.INTERACTION)
        fallback = sanitize_natural_group_reply(fallback, ai_settings)
        if not ai_settings.get("enabled"):
            return fallback

        llm_client = self._get_llm_client()
        if llm_client is None:
            return fallback

        topics = self._configured_warmup_topics(ai_settings, group_id, override=override)
        templates = self._configured_warmup_templates(ai_settings, group_id, override=override)
        override_prompt = str((override or {}).get("prompt") or "").strip()
        prompt = (
            "Generate one natural Chinese Telegram group warmup message.\n"
            f"Group ID: {group_id}\n"
            f"Allowed topics: {', '.join(topics)}\n"
            f"Template style examples: {' / '.join(templates[:5])}\n"
            f"Group-specific instruction: {override_prompt or 'none'}\n"
            "Rules: output only one group message, under 80 Chinese characters, no links, "
            "no sales pitch, no AI/bot/model/system/assistant self-reference, "
            "and sound like a real group member casually asking or reacting."
        )
        try:
            generated = await llm_client.generate(
                prompt=prompt,
                model=llm_client.model_for("fast"),
                temperature=float(ai_settings.get("temperature", 0.6)),
                max_tokens=min(int(ai_settings.get("maxTokens", 180) or 180), 120),
                system_prompt=str(ai_settings.get("systemPrompt") or "Natural Chinese group chat warmup without automation disclosure."),
            )
            return sanitize_natural_group_reply(generated, ai_settings, fallback=fallback)
        except Exception as exc:
            self.logger.warning("proactive_warmup_ai_generation_failed", error=str(exc))
            return fallback

    def _get_group_warmup_override(self, ai_settings: dict, group_id: int) -> Optional[dict]:
        overrides = ai_settings.get("proactiveWarmupGroupOverrides")
        if not isinstance(overrides, dict):
            return None
        candidates = (str(group_id), str(abs(int(group_id))))
        for key in candidates:
            value = overrides.get(key)
            if isinstance(value, dict):
                return value
        return None

    def _configured_warmup_topics(self, ai_settings: dict, group_id: int, *, override: Optional[dict] = None) -> list[str]:
        _ = group_id
        topics = (override or {}).get("topics") if override else None
        if not isinstance(topics, list) or not topics:
            topics = ai_settings.get("proactiveWarmupTopics")
        return [str(item).strip() for item in (topics or []) if str(item).strip()] or ["群内使用体验"]

    def _configured_warmup_templates(self, ai_settings: dict, group_id: int, *, override: Optional[dict] = None) -> list[str]:
        _ = group_id
        templates = (override or {}).get("templates") if override else None
        if not isinstance(templates, list) or not templates:
            templates = ai_settings.get("proactiveWarmupTemplates")
        return [str(item).strip() for item in (templates or []) if str(item).strip()]

    def _configured_warmup_template(self, ai_settings: dict, group_id: int, *, override: Optional[dict] = None) -> str:
        templates = self._configured_warmup_templates(ai_settings, group_id, override=override)
        if not templates:
            return ""
        return self.template_engine.render_string(
            random.choice(templates),
            group_id=group_id,
        )

    async def react_to_message(
        self,
        group_id: int,
        message_id: int,
        emoji: str = "👍",
    ) -> bool:
        """
        React to a message with an emoji.

        Args:
            group_id: Group ID
            message_id: Message ID to react to
            emoji: Emoji to send

        Returns:
            True if successful
        """
        self.logger.info("react_to_message", group_id=group_id, message_id=message_id, emoji=emoji)

        account = await self.account_pool.acquire(purpose="react")
        try:
            await self._send_reaction(account, group_id, message_id, emoji)
            return True
        except Exception as e:
            self.logger.error("react_failed", error=str(e))
            return False
        finally:
            await self.account_pool.release(account)

    async def _generate_message_content(
        self,
        message_type: Optional[MessageType] = None,
    ) -> str:
        """
        Generate message content based on type.

        Args:
            message_type: Type of message to generate

        Returns:
            Generated message content
        """
        # 根据权重选择消息类型
        if not message_type:
            message_type = self._select_message_type()

        # 获取模板
        template = await self.template_engine.get_random_template(message_type)
        if not template:
            return self._get_default_message(message_type)

        # 渲染模板
        return self.template_engine.render(template, register_link="")

    def _select_message_type(self) -> MessageType:
        """Select message type based on configured weights."""
        weights = self.config.speaker.message_type_weights
        types = list(weights.keys())
        probabilities = list(weights.values())

        selected = random.choices(types, weights=probabilities, k=1)[0]
        return MessageType(selected)

    def _get_default_message(self, message_type: MessageType) -> str:
        """Get default message for type."""
        defaults = {
            MessageType.INTERACTION: "大家平时都用哪些节点呀？感觉速度怎么样？",
            MessageType.SHARE: "用了一段时间了，整体还不错，推荐给大家试试",
            MessageType.GUIDE: "有需要可以试试这个：",
            MessageType.QA: "问一下，有人知道怎么设置吗？",
        }
        return defaults.get(message_type, "有需要可以试试")

    def _get_group_config(self, group: Group) -> dict:
        """Get configuration for a specific group."""
        # 默认配置
        default_config = {
            "daily_limit": 10,
            "interval": 60,
        }

        if hasattr(group, "level_config") and group.level_config:
            config = group.level_config
            return {
                "daily_limit": config.daily_message_limit or 10,
                "interval": config.message_interval or 60,
            }

        return default_config

    async def _check_rate_limit(
        self,
        group_id: int,
        account_id: int,
        group_config: dict,
    ) -> bool:
        """Check if rate limits allow sending."""
        group_limit = group_config.get("daily_limit", 10)
        min_interval = group_config.get("interval", self.config.speaker.min_interval_seconds)
        account_daily_limit = max(20, group_limit * 3)
        settings = await self._group_ai_settings()
        if settings.get("enabled"):
            configured_group_limit = int(settings.get("maxRepliesPerGroupPerDay", 0) or 0)
            configured_account_limit = int(settings.get("maxRepliesPerAccountPerDay", 0) or 0)
            configured_cooldown = int(settings.get("cooldownSeconds", 0) or 0)
            if configured_group_limit <= 0 or configured_account_limit <= 0:
                return False
            group_limit = min(group_limit, configured_group_limit)
            account_daily_limit = configured_account_limit
            min_interval = max(min_interval, configured_cooldown)

        account_key = self.rate_limit_service.build_key("speaker", "account", account_id, "group", group_id)
        group_key = self.rate_limit_service.build_key("speaker", "group", group_id, "account", account_id)
        daily_group_key = self.rate_limit_service.build_key("speaker", "group_daily", group_id)
        daily_account_key = self.rate_limit_service.build_key("speaker", "account_daily", account_id)
        cooldown_key = self.rate_limit_service.build_key("speaker", "last_sent", group_id, account_id)

        allowed = await self.rate_limit_service.allow_daily(account_key, rate=group_limit)
        if not allowed:
            return False

        allowed = await self.rate_limit_service.allow_daily(daily_group_key, rate=group_limit * 2)
        if not allowed:
            return False

        allowed = await self.rate_limit_service.allow_daily(daily_account_key, rate=account_daily_limit)
        if not allowed:
            return False

        return await self.rate_limit_service.check_daily_and_cooldown(
            daily_key=group_key,
            cooldown_key=cooldown_key,
            daily_rate=group_limit,
            cooldown_seconds=min_interval,
        )

    async def _send_message(
        self,
        account,
        group_id: int,
        message: str,
    ) -> Optional[int]:
        """
        Send message via Telegram API.

        Args:
            account: Telegram account to use
            group_id: Target group ID
            message: Message content

        Returns:
            Sent message ID
        """
        if getattr(account, "client", None) is None and not hasattr(account, "get_client"):
            self.logger.warning("send_message_no_client", group_id=group_id)
            raise MessageSendError("no available telegram client")

        try:
            message_id = await self.telegram_execution.send_group_message(
                account,
                group_id,
                message,
                source="auto_reply_speaker",
            )
            if message_id is None:
                raise MessageSendError("failed to send group message")
            return message_id
        except Exception as exc:
            self.logger.warning("send_message_failed", group_id=group_id, error=str(exc))
            raise MessageSendError(str(exc)) from exc

    async def _send_reaction(
        self,
        account,
        group_id: int,
        message_id: int,
        emoji: str,
    ) -> None:
        """Send reaction to a message."""
        if getattr(account, "client", None) is None and not hasattr(account, "get_client"):
            self.logger.warning("send_reaction_no_client")
            return

        try:
            ok = await self.telegram_execution.send_reaction(
                account,
                group_id,
                message_id,
                emoji,
                source="auto_reply_speaker",
            )
            if not ok:
                self.logger.warning("send_reaction_not_implemented")
        except Exception as exc:
            self.logger.warning("send_reaction_failed", group_id=group_id, error=str(exc))

    async def _record_message(
        self,
        account_id: int,
        group_id: int,
        content: str,
        message_id: Optional[int],
        message_type: Optional[MessageType] = None,
    ) -> None:
        """Record sent message to database."""
        resolved_type = message_type or self._infer_message_type(content)
        record = AcquisitionMessage(
            account_id=account_id,
            group_id=group_id,
            content=content,
            message_type=resolved_type,
            message_id=message_id,
        )
        self.db.add(record)
        await self.db.commit()

    def _infer_message_type(self, content: str) -> MessageType:
        """Infer message type from content and template intent."""
        normalized = (content or "").strip().lower()
        if any(token in normalized for token in ("注册", "体验", "点击链接", "立即注册", "试试这个")):
            return MessageType.GUIDE
        if any(token in normalized for token in ("分享", "推荐", "不错", "优惠", "活动")):
            return MessageType.SHARE
        if "？" in content or "吗" in content:
            return MessageType.QA
        return MessageType.INTERACTION

    async def _random_delay(self) -> None:
        """Wait a random delay to simulate human behavior."""
        delay = random.randint(
            self.config.speaker.random_delay_min,
            self.config.speaker.random_delay_max,
        )
        await asyncio.sleep(delay)
