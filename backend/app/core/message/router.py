"""
Message Router Module

Routes Telegram messages to appropriate handlers based on message type.

Features:
- Message type detection
- Handler registration and routing
- Rate limiting
- Message preprocessing
"""

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import structlog

from app.core.message.models import MessageType, TelegramMessage
from app.core.redis import RateLimiter

logger = structlog.get_logger()

# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get or create global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(key_prefix="message_ratelimit:")
    return _rate_limiter


@dataclass
class RouterConfig:
    """Configuration for message router."""

    enable_rate_limit: bool = True
    rate_limit_per_user: int = 10
    rate_limit_per_chat: int = 100
    rate_limit_period: int = 60
    enable_preprocessing: bool = True
    max_handlers_per_type: int = 10


class MessageHandler(ABC):
    """
    Abstract base class for message handlers.

    Handlers process specific types of messages.
    """

    @property
    @abstractmethod
    def message_types(self) -> list[MessageType]:
        """Return list of message types this handler handles."""
        pass

    @abstractmethod
    async def handle(self, message: TelegramMessage) -> bool:
        """
        Handle a message.

        Args:
            message: The message to handle

        Returns:
            True if handled successfully, False otherwise
        """
        pass

    async def can_handle(self, message: TelegramMessage) -> bool:
        """
        Check if this handler can handle the message.

        Args:
            message: Message to check

        Returns:
            True if handler can process this message
        """
        return message.message_type in self.message_types


class MessageRouter:
    """
    Routes messages to appropriate handlers.

    Manages handler registration and dispatches messages to handlers
    based on message type.
    """

    def __init__(
        self,
        config: Optional[RouterConfig] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        """
        Initialize MessageRouter.

        Args:
            config: Optional router configuration
            rate_limiter: Optional rate limiter instance (uses global if not provided)
        """
        self.config = config or RouterConfig()
        self._rate_limiter = rate_limiter or get_rate_limiter()
        self._handlers: dict[MessageType, list[MessageHandler]] = {
            mt: [] for mt in MessageType
        }
        self._global_handlers: list[MessageHandler] = []
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="message_router")

    def register(self, handler: MessageHandler) -> None:
        """
        Register a message handler.

        Args:
            handler: Handler to register
        """
        for msg_type in handler.message_types:
            if msg_type not in self._handlers:
                self._handlers[msg_type] = []

            if handler not in self._handlers[msg_type]:
                self._handlers[msg_type].append(handler)

        self.logger.info(
            "handler_registered",
            handler=handler.__class__.__name__,
            types=[t.value for t in handler.message_types],
        )

    def register_global(self, handler: MessageHandler) -> None:
        """
        Register a global handler that runs for all message types.

        Args:
            handler: Handler to register
        """
        if handler not in self._global_handlers:
            self._global_handlers.append(handler)

    def unregister(self, handler: MessageHandler) -> bool:
        """
        Unregister a handler.

        Args:
            handler: Handler to unregister

        Returns:
            True if handler was found and removed
        """
        removed = False

        for msg_type in handler.message_types:
            if msg_type in self._handlers:
                if handler in self._handlers[msg_type]:
                    self._handlers[msg_type].remove(handler)
                    removed = True

        if handler in self._global_handlers:
            self._global_handlers.remove(handler)
            removed = True

        if removed:
            self.logger.info(
                "handler_unregistered",
                handler=handler.__class__.__name__,
            )

        return removed

    def unregister_all(self) -> None:
        """Unregister all handlers."""
        self._handlers = {mt: [] for mt in MessageType}
        self._global_handlers.clear()
        self.logger.info("all_handlers_unregistered")

    async def route(self, message: TelegramMessage) -> list[bool]:
        """
        Route a message to appropriate handlers.

        Args:
            message: Message to route

        Returns:
            List of results from handlers
        """
        if self.config.enable_rate_limit:
            if not await self._check_rate_limit(message):
                self.logger.warning(
                    "rate_limit_exceeded",
                    sender_id=message.sender_id,
                    chat_id=message.chat_id,
                )
                return []

        results = []

        handlers = self._handlers.get(message.message_type, [])

        all_handlers = self._global_handlers + handlers

        if not all_handlers:
            self.logger.debug(
                "no_handlers",
                message_type=message.message_type.value,
                message_id=message.message_id,
            )
            return results

        self.logger.debug(
            "routing_message",
            message_type=message.message_type.value,
            message_id=message.message_id,
            handler_count=len(all_handlers),
        )

        for handler in all_handlers:
            try:
                if await handler.can_handle(message):
                    result = await handler.handle(message)
                    results.append(result)
            except Exception as e:
                self.logger.error(
                    "handler_error",
                    handler=handler.__class__.__name__,
                    message_id=message.message_id,
                    error=str(e),
                )
                results.append(False)

        return results

    async def _check_rate_limit(self, message: TelegramMessage) -> bool:
        """
        Check if message passes rate limits.

        Args:
            message: Message to check

        Returns:
            True if allowed, False if rate limited
        """
        user_key = f"user:{message.sender_id}"
        chat_key = f"chat:{message.chat_id}"

        user_allowed = await self._rate_limiter.check(
            user_key,
            self.config.rate_limit_per_user,
            self.config.rate_limit_period,
        )

        if not user_allowed:
            return False

        chat_allowed = await self._rate_limiter.check(
            chat_key,
            self.config.rate_limit_per_chat,
            self.config.rate_limit_period,
        )

        return chat_allowed

    async def route_batch(self, messages: list[TelegramMessage]) -> list[list[bool]]:
        """
        Route multiple messages.

        Args:
            messages: List of messages to route

        Returns:
            List of result lists
        """
        tasks = [self.route(msg) for msg in messages]
        return await asyncio.gather(*tasks)

    def get_registered_handlers(self) -> dict[MessageType, list[str]]:
        """
        Get list of registered handlers.

        Returns:
            Dictionary mapping message types to handler names
        """
        result = {}
        for msg_type, handlers in self._handlers.items():
            if handlers:
                result[msg_type] = [
                    h.__class__.__name__ for h in handlers
                ]

        return result

    @property
    def handler_count(self) -> int:
        """Get total number of registered handlers."""
        return (
            len(self._global_handlers)
            + sum(len(h) for h in self._handlers.values())
        )
