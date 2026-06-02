"""
Trigger Actions Module

Action executors for trigger responses.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import structlog

from app.core.account.pool import AccountPool

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

    def __init__(self, account_pool: AccountPool):
        """
        Initialize ActionExecutor.

        Args:
            account_pool: Account pool for operations
        """
        self.account_pool = account_pool
        self.logger = logger.bind(module="action_executor")

    async def send_group_reply(
        self,
        account,
        group_id: int,
        message: str,
        reply_to: Optional[int] = None,
    ) -> Optional[int]:
        """Send a reply in a group via Telethon client."""
        self.logger.info("send_group_reply", group_id=group_id, message_length=len(message))

        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("send_group_reply_no_client", group_id=group_id)
            return None

        try:
            result = await client.send_message(group_id, message, reply_to=reply_to)
            return getattr(result, "id", getattr(result, "message_id", None))
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
        self.logger.info("send_private_message", user_id=user_id, message_length=len(message))

        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("send_private_message_no_client", user_id=user_id)
            return False

        try:
            await client.send_message(user_id, message)
            return True
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

        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("send_reaction_no_client")
            return False

        try:
            if hasattr(client, "send_reaction"):
                await client.send_reaction(group_id, message_id, emoji)
                return True
            if hasattr(client, "send_reaction_request"):
                await client.send_reaction_request(group_id, message_id, emoji)
                return True
            self.logger.warning("send_reaction_not_supported")
            return False
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

        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("pin_message_no_client")
            return False

        try:
            if hasattr(client, "pin_message"):
                await client.pin_message(group_id, message_id)
                return True
            self.logger.warning("pin_message_not_supported")
            return False
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

        client = getattr(account, "client", None)
        if client is None and hasattr(account, "get_client"):
            client = account.get_client()
        if client is None:
            self.logger.warning("forward_message_no_client")
            return None

        try:
            result = await client.forward_messages(to_chat_id, message_id, from_chat_id)
            return getattr(result, "id", getattr(result, "message_id", None))
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
