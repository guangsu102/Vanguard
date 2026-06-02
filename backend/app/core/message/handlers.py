"""
Message Handlers Module

Provides concrete message handlers for various message types.

Handlers:
- GroupTextHandler: Handles group text messages
- PrivateTextHandler: Handles private text messages
- CommandHandler: Handles command messages
- CallbackHandler: Handles callback queries
"""

import asyncio
from abc import ABC
from typing import Optional

import structlog

from app.core.message.models import MessageType, TelegramMessage
from app.core.message.router import MessageHandler

logger = structlog.get_logger()


class GroupTextHandler(MessageHandler):
    """
    Handler for group text messages.

    Processes incoming messages in Telegram groups, typically for:
    - Keyword matching and response
    - Content moderation
    - User interaction tracking
    """

    def __init__(
        self,
        keyword_handler: Optional[callable] = None,
        moderation_handler: Optional[callable] = None,
    ):
        """
        Initialize GroupTextHandler.

        Args:
            keyword_handler: Optional callback for keyword matches
            moderation_handler: Optional callback for content moderation
        """
        self._keyword_handler = keyword_handler
        self._moderation_handler = moderation_handler
        self.logger = logger.bind(module="group_text_handler")

    @property
    def message_types(self) -> list[MessageType]:
        return [MessageType.GROUP_TEXT]

    async def handle(self, message: TelegramMessage) -> bool:
        """
        Handle group text message.

        Args:
            message: Group text message

        Returns:
            True if handled successfully
        """
        self.logger.debug(
            "handling_group_text",
            message_id=message.message_id,
            chat_id=message.chat_id,
            sender_id=message.sender_id,
        )

        if self._keyword_handler:
            try:
                await self._keyword_handler(message)
            except Exception as e:
                self.logger.error(
                    "keyword_handler_error",
                    error=str(e),
                    message_id=message.message_id,
                )

        if self._moderation_handler:
            try:
                await self._moderation_handler(message)
            except Exception as e:
                self.logger.error(
                    "moderation_handler_error",
                    error=str(e),
                    message_id=message.message_id,
                )

        return True


class PrivateTextHandler(MessageHandler):
    """
    Handler for private text messages.

    Processes incoming direct messages from users.
    """

    def __init__(
        self,
        dialog_handler: Optional[callable] = None,
        command_handler: Optional[callable] = None,
    ):
        """
        Initialize PrivateTextHandler.

        Args:
            dialog_handler: Handler for natural language dialog
            command_handler: Handler for commands
        """
        self._dialog_handler = dialog_handler
        self._command_handler = command_handler
        self.logger = logger.bind(module="private_text_handler")

    @property
    def message_types(self) -> list[MessageType]:
        return [MessageType.PRIVATE_TEXT]

    async def handle(self, message: TelegramMessage) -> bool:
        """
        Handle private text message.

        Args:
            message: Private text message

        Returns:
            True if handled successfully
        """
        self.logger.debug(
            "handling_private_text",
            message_id=message.message_id,
            sender_id=message.sender_id,
        )

        content = message.content or ""

        if content.startswith("/"):
            if self._command_handler:
                return await self._command_handler(message)
        elif self._dialog_handler:
            return await self._dialog_handler(message)

        return True


class CommandHandler(MessageHandler):
    """
    Handler for command messages.

    Processes Telegram bot commands like /start, /help, etc.
    """

    def __init__(self, command_callbacks: Optional[dict[str, callable]] = None):
        """
        Initialize CommandHandler.

        Args:
            command_callbacks: Dict mapping command names to callbacks
        """
        self._callbacks = command_callbacks or {}
        self.logger = logger.bind(module="command_handler")

    @property
    def message_types(self) -> list[MessageType]:
        return [MessageType.COMMAND]

    def register_command(self, command: str, callback: callable) -> None:
        """
        Register a command callback.

        Args:
            command: Command name (without /)
            callback: Async function to call
        """
        self._callbacks[command] = callback

    async def handle(self, message: TelegramMessage) -> bool:
        """
        Handle command message.

        Args:
            message: Command message

        Returns:
            True if handled successfully
        """
        content = message.content or ""
        if not content.startswith("/"):
            return False

        parts = content[1:].split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        self.logger.debug(
            "handling_command",
            command=command,
            args=args,
            sender_id=message.sender_id,
        )

        callback = self._callbacks.get(command)
        if callback:
            try:
                await callback(message, args)
                return True
            except Exception as e:
                self.logger.error(
                    "command_callback_error",
                    command=command,
                    error=str(e),
                )
                return False

        self.logger.warning("unknown_command", command=command)
        return False


class CallbackQueryHandler(MessageHandler):
    """
    Handler for callback queries.

    Processes inline button callbacks from Telegram.
    """

    def __init__(self, callback_callbacks: Optional[dict[str, callable]] = None):
        """
        Initialize CallbackQueryHandler.

        Args:
            callback_callbacks: Dict mapping callback data prefixes to callbacks
        """
        self._callbacks = callback_callbacks or {}
        self.logger = logger.bind(module="callback_query_handler")

    @property
    def message_types(self) -> list[MessageType]:
        return [MessageType.CALLBACK_QUERY]

    def register_callback(self, data_prefix: str, callback: callable) -> None:
        """
        Register a callback query handler.

        Args:
            data_prefix: Prefix of callback data to match
            callback: Async function to call
        """
        self._callbacks[data_prefix] = callback

    async def handle(self, message: TelegramMessage) -> bool:
        """
        Handle callback query.

        Args:
            message: Callback query message

        Returns:
            True if handled successfully
        """
        content = message.content or ""

        self.logger.debug(
            "handling_callback",
            data=content,
            message_id=message.message_id,
        )

        for prefix, callback in self._callbacks.items():
            if content.startswith(prefix):
                try:
                    await callback(message, content[len(prefix):])
                    return True
                except Exception as e:
                    self.logger.error(
                        "callback_error",
                        prefix=prefix,
                        error=str(e),
                    )
                    return False

        return False


class CompositeHandler(MessageHandler):
    """
    Composite handler that delegates to multiple handlers.

    Allows grouping related handlers together.
    """

    def __init__(self, handlers: list[MessageHandler]):
        """
        Initialize CompositeHandler.

        Args:
            handlers: List of handlers to delegate to
        """
        self._handlers = handlers
        self.logger = logger.bind(module="composite_handler")

    @property
    def message_types(self) -> list[MessageType]:
        """All message types from child handlers."""
        types = set()
        for handler in self._handlers:
            types.update(handler.message_types)
        return list(types)

    async def handle(self, message: TelegramMessage) -> bool:
        """
        Handle message with all child handlers.

        Args:
            message: Message to handle

        Returns:
            True if at least one handler succeeded
        """
        results = []
        for handler in self._handlers:
            if await handler.can_handle(message):
                try:
                    result = await handler.handle(message)
                    results.append(result)
                except Exception as e:
                    self.logger.error(
                        "composite_handler_error",
                        handler=handler.__class__.__name__,
                        error=str(e),
                    )
                    results.append(False)

        return any(results) if results else False
