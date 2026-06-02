"""
Unit Tests for Message Routing Module

Tests cover:
- TelegramMessage model
- MessageRouter handler registration and routing
- Rate limiting
- Message handlers
- CompositeHandler
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.core.message.models import MessageType, TelegramMessage
from app.core.message.router import MessageRouter, MessageHandler, RouterConfig
from app.core.message.handlers import (
    GroupTextHandler,
    PrivateTextHandler,
    CommandHandler,
    CallbackQueryHandler,
    CompositeHandler,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def router_config():
    """Create test router configuration."""
    return RouterConfig(
        enable_rate_limit=True,
        rate_limit_per_user=5,
        rate_limit_per_chat=20,
        rate_limit_period=60,
    )


@pytest.fixture
def mock_rate_limiter():
    """Create mock rate limiter that always allows."""
    limiter = MagicMock()
    limiter.check = AsyncMock(return_value=True)
    return limiter


@pytest.fixture
def message():
    """Create a test message."""
    return TelegramMessage(
        message_id=1,
        chat_id=123456,
        sender_id=654321,
        sender_name="Test User",
        message_type=MessageType.GROUP_TEXT,
        content="Hello World",
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def command_message():
    """Create a test command message."""
    return TelegramMessage(
        message_id=2,
        chat_id=123456,
        sender_id=654321,
        sender_name="Test User",
        message_type=MessageType.COMMAND,
        content="/start arg1 arg2",
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def callback_message():
    """Create a test callback message."""
    return TelegramMessage(
        message_id=3,
        chat_id=123456,
        sender_id=654321,
        sender_name="Test User",
        message_type=MessageType.CALLBACK_QUERY,
        content="confirm_yes",
        timestamp=datetime.utcnow(),
    )


# ============================================================================
# Test RouterConfig
# ============================================================================

class TestRouterConfig:
    """Test RouterConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = RouterConfig()

        assert config.enable_rate_limit is True
        assert config.rate_limit_per_user == 10
        assert config.rate_limit_per_chat == 100
        assert config.rate_limit_period == 60
        assert config.enable_preprocessing is True
        assert config.max_handlers_per_type == 10

    def test_custom_config(self, router_config):
        """Test custom configuration."""
        assert router_config.rate_limit_per_user == 5
        assert router_config.rate_limit_per_chat == 20


# ============================================================================
# Test MessageRouter
# ============================================================================

class TestMessageRouter:
    """Test MessageRouter class."""

    def test_init_default(self):
        """Test router initialization with defaults."""
        router = MessageRouter()

        assert router.config.enable_rate_limit is True
        assert router.handler_count == 0

    def test_init_with_config(self, router_config, mock_rate_limiter):
        """Test router initialization with custom config."""
        router = MessageRouter(config=router_config, rate_limiter=mock_rate_limiter)

        assert router.config.rate_limit_per_user == 5
        assert router._rate_limiter is mock_rate_limiter

    def test_register_handler(self, mock_rate_limiter):
        """Test handler registration."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        handler = GroupTextHandler()

        router.register(handler)

        assert router.handler_count == 1
        registered = router.get_registered_handlers()
        assert MessageType.GROUP_TEXT in registered

    def test_register_multiple_handlers(self, mock_rate_limiter):
        """Test registering multiple handlers."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)

        router.register(GroupTextHandler())
        router.register(PrivateTextHandler())
        router.register(CommandHandler())

        assert router.handler_count == 3

    def test_register_global_handler(self, mock_rate_limiter):
        """Test registering global handler."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)

        class GlobalHandler(MessageHandler):
            @property
            def message_types(self) -> list[MessageType]:
                return [MessageType.GROUP_TEXT]

            async def handle(self, message: TelegramMessage) -> bool:
                return True

        router.register_global(GlobalHandler())

        assert router.handler_count == 1

    def test_unregister_handler(self, mock_rate_limiter):
        """Test handler unregistration."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        handler = GroupTextHandler()
        router.register(handler)

        result = router.unregister(handler)

        assert result is True
        assert router.handler_count == 0

    def test_unregister_nonexistent_handler(self, mock_rate_limiter):
        """Test unregistering non-existent handler."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        handler = GroupTextHandler()

        result = router.unregister(handler)

        assert result is False

    def test_unregister_all(self, mock_rate_limiter):
        """Test unregistering all handlers."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        router.register(GroupTextHandler())
        router.register(PrivateTextHandler())

        router.unregister_all()

        assert router.handler_count == 0

    def test_get_registered_handlers(self, mock_rate_limiter):
        """Test getting registered handlers."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        router.register(GroupTextHandler())
        router.register(CommandHandler())

        registered = router.get_registered_handlers()

        assert MessageType.GROUP_TEXT in registered
        assert MessageType.COMMAND in registered
        assert MessageType.PRIVATE_TEXT not in registered


# ============================================================================
# Test Message Routing
# ============================================================================

class TestMessageRouting:
    """Test message routing functionality."""

    @pytest.mark.asyncio
    async def test_route_to_matching_handler(self, mock_rate_limiter, message):
        """Test routing message to matching handler."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        handler = GroupTextHandler()
        handler.handle = AsyncMock(return_value=True)
        router.register(handler)

        results = await router.route(message)

        assert len(results) == 1
        assert results[0] is True
        handler.handle.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_route_no_matching_handlers(self, mock_rate_limiter, message):
        """Test routing when no handlers match."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        router.register(PrivateTextHandler())

        results = await router.route(message)

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_route_multiple_handlers(self, mock_rate_limiter, message):
        """Test routing to multiple handlers."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)

        handler1 = GroupTextHandler()
        handler1.handle = AsyncMock(return_value=True)
        handler2 = GroupTextHandler()
        handler2.handle = AsyncMock(return_value=True)

        router.register(handler1)
        router.register(handler2)

        results = await router.route(message)

        assert len(results) == 2
        assert all(results)

    @pytest.mark.asyncio
    async def test_route_global_handler(self, mock_rate_limiter, message):
        """Test global handler is always called."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)

        class GlobalHandler(MessageHandler):
            @property
            def message_types(self) -> list[MessageType]:
                return [MessageType.GROUP_TEXT]

            async def handle(self, message: TelegramMessage) -> bool:
                return True

        global_handler = GlobalHandler()
        global_handler.handle = AsyncMock(return_value=True)
        router.register_global(global_handler)

        results = await router.route(message)

        assert len(results) == 1
        global_handler.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_handler_exception(self, mock_rate_limiter, message):
        """Test handler exception doesn't break routing."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)

        handler1 = GroupTextHandler()
        handler1.handle = AsyncMock(side_effect=Exception("Test error"))
        router.register(handler1)

        results = await router.route(message)

        assert len(results) == 1
        assert results[0] is False

    @pytest.mark.asyncio
    async def test_route_batch(self, mock_rate_limiter, message):
        """Test batch routing."""
        router = MessageRouter(rate_limiter=mock_rate_limiter)
        handler = GroupTextHandler()
        handler.handle = AsyncMock(return_value=True)
        router.register(handler)

        messages = [message, message]
        results = await router.route_batch(messages)

        assert len(results) == 2
        assert all(r == [True] for r in results)


# ============================================================================
# Test Rate Limiting
# ============================================================================

class TestRateLimiting:
    """Test rate limiting functionality."""

    @pytest.mark.asyncio
    async def test_rate_limit_allowed(self, mock_rate_limiter, message):
        """Test message allowed under rate limit."""
        router = MessageRouter(
            rate_limiter=mock_rate_limiter,
            config=RouterConfig(enable_rate_limit=True),
        )
        handler = GroupTextHandler()
        handler.handle = AsyncMock(return_value=True)
        router.register(handler)

        results = await router.route(message)

        assert len(results) == 1
        assert results[0] is True

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, message):
        """Test message blocked when rate limit exceeded."""
        mock_limiter = MagicMock()
        mock_limiter.check = AsyncMock(return_value=False)

        router = MessageRouter(
            rate_limiter=mock_limiter,
            config=RouterConfig(enable_rate_limit=True),
        )
        handler = GroupTextHandler()
        handler.handle = AsyncMock(return_value=True)
        router.register(handler)

        results = await router.route(message)

        assert len(results) == 0
        handler.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_disabled(self, mock_rate_limiter, message):
        """Test routing when rate limit is disabled."""
        mock_limiter = MagicMock()
        mock_limiter.check = AsyncMock(return_value=False)

        router = MessageRouter(
            rate_limiter=mock_limiter,
            config=RouterConfig(enable_rate_limit=False),
        )
        handler = GroupTextHandler()
        handler.handle = AsyncMock(return_value=True)
        router.register(handler)

        results = await router.route(message)

        assert len(results) == 1
        handler.handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_check_per_user_and_chat(self, message):
        """Test rate limit checks both user and chat."""
        mock_limiter = MagicMock()
        mock_limiter.check = AsyncMock(return_value=True)

        router = MessageRouter(
            rate_limiter=mock_limiter,
            config=RouterConfig(
                enable_rate_limit=True,
                rate_limit_per_user=5,
                rate_limit_per_chat=20,
            ),
        )
        handler = GroupTextHandler()
        handler.handle = AsyncMock(return_value=True)
        router.register(handler)

        await router.route(message)

        assert mock_limiter.check.call_count == 2


# ============================================================================
# Test MessageHandlers
# ============================================================================

class TestGroupTextHandler:
    """Test GroupTextHandler."""

    @pytest.mark.asyncio
    async def test_handle_group_text(self, message):
        """Test handling group text message."""
        handler = GroupTextHandler()

        result = await handler.handle(message)

        assert result is True

    @pytest.mark.asyncio
    async def test_handle_with_callback(self, message):
        """Test handling with keyword callback."""
        callback = AsyncMock()
        handler = GroupTextHandler(keyword_handler=callback)

        await handler.handle(message)

        callback.assert_called_once_with(message)

    @pytest.mark.asyncio
    async def test_message_types(self):
        """Test handler message types."""
        handler = GroupTextHandler()

        assert handler.message_types == [MessageType.GROUP_TEXT]


class TestPrivateTextHandler:
    """Test PrivateTextHandler."""

    @pytest.mark.asyncio
    async def test_handle_private_text_command(self):
        """Test handling private command message."""
        message = TelegramMessage(
            message_id=1,
            chat_id=123,
            sender_id=456,
            message_type=MessageType.PRIVATE_TEXT,
            content="/help",
        )
        handler = PrivateTextHandler()

        result = await handler.handle(message)

        assert result is True

    @pytest.mark.asyncio
    async def test_handle_private_text_dialog(self):
        """Test handling private dialog message."""
        message = TelegramMessage(
            message_id=1,
            chat_id=123,
            sender_id=456,
            message_type=MessageType.PRIVATE_TEXT,
            content="Hello there",
        )
        handler = PrivateTextHandler()
        dialog_handler = AsyncMock()
        handler._dialog_handler = dialog_handler

        await handler.handle(message)

        dialog_handler.assert_called_once_with(message)


class TestCommandHandler:
    """Test CommandHandler."""

    @pytest.mark.asyncio
    async def test_handle_registered_command(self, command_message):
        """Test handling registered command."""
        callback = AsyncMock()
        handler = CommandHandler({"start": callback})

        result = await handler.handle(command_message)

        assert result is True
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_unknown_command(self, command_message):
        """Test handling unknown command."""
        handler = CommandHandler({})

        result = await handler.handle(command_message)

        assert result is False

    @pytest.mark.asyncio
    async def test_register_command(self):
        """Test registering command dynamically."""
        handler = CommandHandler()
        callback = AsyncMock()

        handler.register_command("test", callback)

        assert "test" in handler._callbacks
        assert handler._callbacks["test"] is callback


class TestCallbackQueryHandler:
    """Test CallbackQueryHandler."""

    @pytest.mark.asyncio
    async def test_handle_matching_callback(self, callback_message):
        """Test handling matching callback."""
        callback = AsyncMock()
        handler = CallbackQueryHandler({"confirm_": callback})

        result = await handler.handle(callback_message)

        assert result is True
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_unknown_callback(self, callback_message):
        """Test handling unknown callback."""
        handler = CallbackQueryHandler({})

        result = await handler.handle(callback_message)

        assert result is False

    @pytest.mark.asyncio
    async def test_register_callback(self):
        """Test registering callback dynamically."""
        handler = CallbackQueryHandler()
        callback = AsyncMock()

        handler.register_callback("btn_", callback)

        assert "btn_" in handler._callbacks


class TestCompositeHandler:
    """Test CompositeHandler."""

    @pytest.mark.asyncio
    async def test_composite_handles_all_types(self):
        """Test composite handler aggregates message types."""
        handler1 = GroupTextHandler()
        handler2 = PrivateTextHandler()

        composite = CompositeHandler([handler1, handler2])

        assert MessageType.GROUP_TEXT in composite.message_types
        assert MessageType.PRIVATE_TEXT in composite.message_types

    @pytest.mark.asyncio
    async def test_composite_delegates_to_handlers(self, message):
        """Test composite delegates to child handlers."""
        handler1 = GroupTextHandler()
        handler1.handle = AsyncMock(return_value=True)
        handler2 = GroupTextHandler()
        handler2.handle = AsyncMock(return_value=False)

        composite = CompositeHandler([handler1, handler2])

        result = await composite.handle(message)

        assert result is True

    @pytest.mark.asyncio
    async def test_composite_returns_false_when_no_handlers(self, message):
        """Test composite returns False when no handlers match."""
        handler = PrivateTextHandler()

        composite = CompositeHandler([handler])

        result = await composite.handle(message)

        assert result is False


# ============================================================================
# Test MessageType
# ============================================================================

class TestMessageType:
    """Test MessageType enum."""

    def test_all_message_types_defined(self):
        """Test all expected message types are defined."""
        expected_types = {
            MessageType.GROUP_TEXT,
            MessageType.GROUP_PHOTO,
            MessageType.GROUP_VIDEO,
            MessageType.GROUP_AUDIO,
            MessageType.GROUP_DOCUMENT,
            MessageType.PRIVATE_TEXT,
            MessageType.CALLBACK_QUERY,
            MessageType.COMMAND,
        }

        assert set(MessageType) == expected_types

    def test_message_type_values(self):
        """Test message type string values."""
        assert MessageType.GROUP_TEXT.value == "group_text"
        assert MessageType.COMMAND.value == "command"
        assert MessageType.CALLBACK_QUERY.value == "callback_query"


# ============================================================================
# Test TelegramMessage Model
# ============================================================================

class TestTelegramMessage:
    """Test TelegramMessage model."""

    def test_create_message(self):
        """Test creating a message."""
        msg = TelegramMessage(
            message_id=123,
            chat_id=456,
            sender_id=789,
            message_type=MessageType.GROUP_TEXT,
            content="Test",
        )

        assert msg.message_id == 123
        assert msg.chat_id == 456
        assert msg.sender_id == 789
        assert msg.message_type == MessageType.GROUP_TEXT
        assert msg.content == "Test"

    def test_message_optional_fields(self):
        """Test message optional fields."""
        msg = TelegramMessage(
            message_id=123,
            chat_id=456,
            sender_id=789,
            message_type=MessageType.PRIVATE_TEXT,
        )

        assert msg.content is None
        assert msg.sender_name is None
        assert msg.reply_to_message_id is None
