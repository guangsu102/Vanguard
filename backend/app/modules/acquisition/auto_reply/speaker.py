"""
Speaker Module

Automatic message sending scheduler and executor for group messaging.
"""

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.group.manager import GroupManager
from app.core.group.models import Group, GroupLevel
from app.modules.acquisition.auto_reply.templates import TemplateEngine
from app.modules.acquisition.auto_reply.scheduler import SpeakScheduler
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.models import AcquisitionMessage, MessageType
from app.modules.acquisition.exceptions import MessageSendError, NoAvailableAccountError
from app.modules.acquisition.rate_limit import AcquisitionRateLimitService
from app.modules.acquisition.tracking.url_builder import URLBuilder

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
        self.rate_limit_service = AcquisitionRateLimitService(
            key_prefix="acquisition:speak:",
            config=self.config,
        )

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

        # 获取账号
        account = None
        if account_id:
            account = await self.account_pool.get_account_by_id(account_id)
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
            if not await self._check_rate_limit(group_id, account.id, group_config):
                return SpeakResult(
                    success=False,
                    error="Rate limit exceeded",
                    group_id=group_id,
                    account_id=account.id,
                )

            # 发送消息
            msg_id = await self._send_message(account, group_id, message)

            # 记录消息
            await self._record_message(account.id, group_id, message, msg_id)

            # 随机延迟（模拟人类行为）
            await self._random_delay()

            return SpeakResult(
                success=True,
                message_id=msg_id,
                group_id=group_id,
                account_id=account.id,
            )

        except Exception as e:
            self.logger.error("speak_failed", group_id=group_id, error=str(e))
            return SpeakResult(success=False, error=str(e), group_id=group_id, account_id=account.id)

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
        from app.core.group.models import GroupLevelConfig

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

        allowed = await self.rate_limit_service.allow_daily(daily_account_key, rate=max(20, group_limit * 3))
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
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("send_message_no_client", group_id=group_id)
            raise MessageSendError("no available telegram client")

        try:
            result = await client.send_message(group_id, message)
            return getattr(result, "id", getattr(result, "message_id", None))
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
        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("send_reaction_no_client")
            return

        if hasattr(client, "send_reaction"):
            await client.send_reaction(group_id, message_id, emoji)
            return

        if hasattr(client, "send_reaction_request"):
            await client.send_reaction_request(group_id, message_id, emoji)
            return

        self.logger.warning("send_reaction_not_implemented")

    async def _record_message(
        self,
        account_id: int,
        group_id: int,
        content: str,
        message_id: Optional[int],
    ) -> None:
        """Record sent message to database."""
        message_type = self._infer_message_type(content)
        record = AcquisitionMessage(
            account_id=account_id,
            group_id=group_id,
            content=content,
            message_type=message_type,
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
