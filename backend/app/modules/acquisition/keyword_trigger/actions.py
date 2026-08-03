"""
Trigger Actions Module

Action executors for trigger responses.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import structlog

from app.core.account.pool import AccountPool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.automation_settings import get_group_ai_interaction_settings, is_private_messaging_enabled
from app.modules.acquisition.rate_limit import AcquisitionRateLimitService

logger = structlog.get_logger()


class TriggerActionType(str, Enum):
    """Trigger action types."""
    REPLY = "reply"
    PRIVATE_MESSAGE = "private_message"
    REACT = "react"
    PIN = "pin"
    FORWARD = "forward"
    NONE = "none"


@dataclass
class ActionContext:
    """Context for action execution."""
    user_id: int
    group_id: int
    message_id: Optional[int] = None
    user_name: Optional[str] = None
    group_name: Optional[str] = None
    register_link: Optional[str] = None


class ActionExecutor:
    """
    Executor for trigger actions.

    Handles the actual execution of trigger responses
    like sending messages, reactions, etc.
    """

    def __init__(self, account_pool: AccountPool, risk_guard: Optional[AccountRiskGuard] = None):
        """
        Initialize ActionExecutor.

        Args:
            account_pool: Account pool for operations
        """
        self.account_pool = account_pool
        self.risk_guard = risk_guard
        self.telegram_execution = TelegramExecutionService(risk_guard)
        self.logger = logger.bind(module="action_executor")
        self.group_ai_rate_limit = AcquisitionRateLimitService(key_prefix="acquisition:group_ai_reply:")

    async def _private_messaging_enabled(self) -> bool:
        db = getattr(self.risk_guard, "db", None)
        if db is not None:
            return await is_private_messaging_enabled(db, initiated_by_user=False)

        from app.core import database as db_module

        if db_module.async_session_factory is None:
            await db_module.init_db(create_tables=False)
        async with db_module.get_db_session() as session:
            return await is_private_messaging_enabled(session, initiated_by_user=False)

    async def _group_ai_settings(self) -> dict:
        db = getattr(self.risk_guard, "db", None)
        if db is not None:
            return await get_group_ai_interaction_settings(db)

        from app.core import database as db_module

        if db_module.async_session_factory is None:
            await db_module.init_db(create_tables=False)
        async with db_module.get_db_session() as session:
            return await get_group_ai_interaction_settings(session)

    async def _group_ai_reply_allowed(self, account_id: int, group_id: int) -> bool:
        settings = await self._group_ai_settings()
        if not settings.get("enabled"):
            return True

        group_limit = int(settings.get("maxRepliesPerGroupPerDay", 0) or 0)
        account_limit = int(settings.get("maxRepliesPerAccountPerDay", 0) or 0)
        cooldown_seconds = int(settings.get("cooldownSeconds", 0) or 0)
        if group_limit <= 0 or account_limit <= 0:
            self.logger.info("group_ai_reply_blocked_by_zero_limit", account_id=account_id, group_id=group_id)
            return False

        account_key = self.group_ai_rate_limit.build_key("daily", "account", account_id)
        group_key = self.group_ai_rate_limit.build_key("daily", "group", group_id)
        cooldown_key = self.group_ai_rate_limit.build_key("cooldown", "account", account_id, "group", group_id)

        if not await self.group_ai_rate_limit.allow_daily(account_key, rate=account_limit):
            self.logger.info(
                "group_ai_account_daily_limit_reached",
                account_id=account_id,
                group_id=group_id,
                limit=account_limit,
            )
            return False

        if not await self.group_ai_rate_limit.check_daily_and_cooldown(
            daily_key=group_key,
            cooldown_key=cooldown_key,
            daily_rate=group_limit,
            cooldown_seconds=cooldown_seconds,
        ):
            self.logger.info(
                "group_ai_group_limit_or_cooldown_reached",
                account_id=account_id,
                group_id=group_id,
                group_limit=group_limit,
                cooldown_seconds=cooldown_seconds,
            )
            return False

        return True

    async def send_group_reply(
        self,
        account,
        group_id: int,
        message: str,
        reply_to: Optional[int] = None,
    ) -> Optional[int]:
        """Send a reply in a group via Telethon client."""
        self.logger.info("send_group_reply", group_id=group_id, message_length=len(message))

        try:
            account_id = getattr(account, "id", None) or getattr(account, "account_id", 0)
            if account_id and not await self._group_ai_reply_allowed(account_id, group_id):
                return None

            return await self.telegram_execution.send_group_message(
                account,
                group_id,
                message,
                reply_to=reply_to,
                source="keyword_trigger",
            )
        except Exception as e:
            self.logger.error("send_group_reply_failed", group_id=group_id, error=str(e))
            raise

    async def send_private_message(
        self,
        account,
        user_id: int,
        message: str,
    ) -> bool:
        """Send a private message to a user via Telethon client."""
        if not await self._private_messaging_enabled():
            self.logger.info(
                "private_messaging_paused",
                user_id=user_id,
                initiated_by_user=False,
                message_length=len(message),
            )
            return False

        self.logger.info("send_private_message", user_id=user_id, message_length=len(message))

        try:
            return await self.telegram_execution.send_private_message(
                account,
                user_id,
                message,
                source="keyword_trigger",
            )
        except Exception as e:
            self.logger.error("send_private_message_failed", user_id=user_id, error=str(e))
            return False

    async def send_reaction(
        self,
        account,
        group_id: int,
        message_id: int,
        emoji: str = "👍",
    ) -> bool:
        """Send a reaction to a message via Telethon client."""
        self.logger.info("send_reaction", group_id=group_id, message_id=message_id, emoji=emoji)

        try:
            return await self.telegram_execution.send_reaction(
                account,
                group_id,
                message_id,
                emoji,
                source="keyword_trigger",
            )
        except Exception as e:
            self.logger.error("send_reaction_failed", error=str(e))
            return False

    async def pin_message(
        self,
        account,
        group_id: int,
        message_id: int,
    ) -> bool:
        """Pin a message in a group via Telethon client."""
        self.logger.info("pin_message", group_id=group_id, message_id=message_id)

        try:
            return await self.telegram_execution.pin_message(
                account,
                group_id,
                message_id,
                source="keyword_trigger",
            )
        except Exception as e:
            self.logger.error("pin_message_failed", error=str(e))
            return False

    async def forward_message(
        self,
        account,
        from_chat_id: int,
        to_chat_id: int,
        message_id: int,
    ) -> Optional[int]:
        """Forward a message via Telethon client."""
        self.logger.info(
            "forward_message",
            from_chat_id=from_chat_id,
            to_chat_id=to_chat_id,
            message_id=message_id,
        )

        try:
            return await self.telegram_execution.forward_message(
                account,
                from_chat_id,
                to_chat_id,
                message_id,
                source="keyword_trigger",
            )
        except Exception as e:
            self.logger.error("forward_message_failed", error=str(e))
            return None

    async def execute_action(
        self,
        action_type: TriggerActionType,
        context: ActionContext,
        **kwargs,
    ) -> bool:
        """
        Execute an action by type.

        Args:
            action_type: Type of action to execute
            context: Action context
            **kwargs: Additional action-specific parameters

        Returns:
            True if successful
        """
        account = await self.account_pool.acquire(purpose="action")
        if account is None:
            self.logger.warning("execute_action_no_account", action_type=action_type.value)
            return False

        try:
            if action_type == TriggerActionType.REPLY:
                return await self.send_group_reply(
                    account,
                    context.group_id,
                    kwargs.get("message", ""),
                    kwargs.get("reply_to"),
                ) is not None

            elif action_type == TriggerActionType.PRIVATE_MESSAGE:
                return await self.send_private_message(
                    account,
                    context.user_id,
                    kwargs.get("message", ""),
                )

            elif action_type == TriggerActionType.REACT:
                return await self.send_reaction(
                    account,
                    context.group_id,
                    context.message_id or 0,
                    kwargs.get("emoji", "👍"),
                )

            elif action_type == TriggerActionType.PIN:
                return await self.pin_message(
                    account,
                    context.group_id,
                    context.message_id or 0,
                )

            elif action_type == TriggerActionType.FORWARD:
                return await self.forward_message(
                    account,
                    kwargs.get("from_chat_id", 0),
                    kwargs.get("to_chat_id", 0),
                    context.message_id or 0,
                ) is not None

            return False

        finally:
            await self.account_pool.release(account)
