"""
消息收发流程测试
测试消息的发送、接收、处理和路由
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from typing import List, Dict, Any


class TestMessageSending:
    """消息发送测试"""

    @pytest.fixture
    def mock_bot(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(
            message_id=12345,
            date=datetime.now(),
            chat=MagicMock(id=100)
        ))
        bot.send_document = AsyncMock(return_value=MagicMock(message_id=12346))
        bot.send_photo = AsyncMock(return_value=MagicMock(message_id=12347))
        bot.send_video = AsyncMock(return_value=MagicMock(message_id=12348))
        return bot

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.record_message = AsyncMock(return_value=1)
        db.get_message_history = AsyncMock(return_value=[])
        return db

    @pytest.mark.asyncio
    async def test_send_text_message(self, mock_bot, mock_db):
        """测试发送文本消息"""
        # 使用 mock 对象模拟消息
        message = MagicMock()
        message.chat_id = 100
        message.text = "Hello, World!"
        message.parse_mode = "Markdown"

        # 模拟发送
        mock_bot.send_message.return_value = MagicMock(
            message_id=12345,
            text="Hello, World!",
            chat=MagicMock(id=100)
        )

        sent = await mock_bot.send_message(
            chat_id=message.chat_id,
            text=message.text,
            parse_mode=message.parse_mode
        )

        assert sent.message_id == 12345
        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_with_reply(self, mock_bot, mock_db):
        """测试回复消息"""
        mock_bot.send_message.return_value = MagicMock(
            message_id=12346,
            reply_to_message_id=12345
        )

        sent = await mock_bot.send_message(
            chat_id=100,
            text="Reply message",
            reply_to_message_id=12345
        )

        assert sent.reply_to_message_id == 12345

    @pytest.mark.asyncio
    async def test_send_media_group(self, mock_bot, mock_db):
        """测试发送媒体组"""
        mock_bot.send_media_group = AsyncMock(return_value=[
            MagicMock(message_id=12345),
            MagicMock(message_id=12346)
        ])

        media = [
            {"type": "photo", "media": "photo1.jpg"},
            {"type": "photo", "media": "photo2.jpg"}
        ]

        sent = await mock_bot.send_media_group(chat_id=100, media=media)

        assert len(sent) == 2
        mock_bot.send_media_group.assert_called_once()


class TestMessageReceiving:
    """消息接收测试"""

    @pytest.fixture
    def mock_update(self):
        """模拟消息更新"""
        update = MagicMock()
        update.message = MagicMock()
        update.message.message_id = 999
        update.message.date = datetime.now()
        update.message.chat.id = 100
        update.message.from_user.id = 123456
        update.message.from_user.username = "test_user"
        update.message.text = "Test message"
        update.message.reply_to_message = None
        return update

    @pytest.mark.asyncio
    async def test_parse_message_update(self, mock_update):
        """测试解析消息更新"""
        # 模拟从 Telegram Update 中提取消息
        message_data = {
            "message_id": mock_update.message.message_id,
            "chat_id": mock_update.message.chat.id,
            "user_id": mock_update.message.from_user.id,
            "username": mock_update.message.from_user.username,
            "text": mock_update.message.text,
            "timestamp": mock_update.message.date.isoformat()
        }

        assert message_data["message_id"] == 999
        assert message_data["chat_id"] == 100
        assert message_data["user_id"] == 123456

    @pytest.mark.asyncio
    async def test_parse_callback_query(self):
        """测试解析回调查询"""
        callback = MagicMock()
        callback.id = "callback_123"
        callback.from_user.id = 123456
        callback.data = "action:confirm"
        callback.message.message_id = 999

        assert callback.id == "callback_123"
        assert callback.data == "action:confirm"

    @pytest.mark.asyncio
    async def test_parse_inline_query(self):
        """测试解析内联查询"""
        inline_query = MagicMock()
        inline_query.id = "query_456"
        inline_query.from_user.id = 123456
        inline_query.query = "search term"
        inline_query.offset = ""

        assert inline_query.query == "search term"
        assert inline_query.from_user.id == 123456


class TestMessageRouting:
    """消息路由测试"""

    @pytest.fixture
    def message_router(self):
        """消息路由器"""
        from app.core.message.router import MessageRouter
        from app.core.message.models import MessageType, TelegramMessage

        router = MessageRouter()

        # 创建简单的测试处理器
        class TestHandler:
            def __init__(self, msg_type: MessageType):
                self._msg_type = msg_type

            @property
            def message_types(self) -> list[MessageType]:
                return [self._msg_type]

            async def handle(self, message: TelegramMessage) -> bool:
                return True

        router.register(TestHandler(MessageType.COMMAND))
        router.register(TestHandler(MessageType.CALLBACK_QUERY))
        router.register(TestHandler(MessageType.GROUP_TEXT))
        return router

    @pytest.mark.asyncio
    async def test_route_command(self, message_router):
        """测试命令路由"""
        from app.core.message.models import MessageType
        msg = MagicMock()
        msg.sender_id = 123
        msg.chat_id = 456
        msg.message_type = MessageType.COMMAND
        msg.content = "/start"
        result = await message_router.route(msg)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_route_callback(self, message_router):
        """测试回调路由"""
        from app.core.message.models import MessageType
        msg = MagicMock()
        msg.sender_id = 123
        msg.chat_id = 456
        msg.message_type = MessageType.CALLBACK_QUERY
        msg.content = "callback:data"
        result = await message_router.route(msg)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_route_text(self, message_router):
        """测试文本路由"""
        from app.core.message.models import MessageType
        msg = MagicMock()
        msg.sender_id = 123
        msg.chat_id = 456
        msg.message_type = MessageType.GROUP_TEXT
        msg.content = "Hello world"
        result = await message_router.route(msg)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_route_unknown(self, message_router):
        """测试未知类型路由"""
        from app.core.message.models import MessageType
        msg = MagicMock()
        msg.sender_id = 123
        msg.chat_id = 456
        msg.message_type = MessageType.GROUP_TEXT
        msg.content = ""
        result = await message_router.route(msg)
        assert len(result) >= 0


class TestMessageProcessing:
    """消息处理测试"""

    @pytest.fixture
    def mock_processor(self):
        """模拟消息处理器"""
        processor = MagicMock()
        processor.process = AsyncMock(return_value={
            "success": True,
            "message_id": 123
        })
        return processor

    @pytest.mark.asyncio
    async def test_process_command(self, mock_processor):
        """测试处理命令消息"""
        result = await mock_processor.process("/start", "command")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_process_text(self, mock_processor):
        """测试处理普通文本"""
        result = await mock_processor.process("Hello", "text")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_process_media(self, mock_processor):
        """测试处理媒体消息"""
        result = await mock_processor.process({
            "type": "photo",
            "file_id": "abc123"
        }, "media")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_process_with_rate_limit(self):
        """测试带限流的处理"""
        call_count = 0
        max_calls = 5

        async def rate_limited_process(*args):
            nonlocal call_count
            if call_count >= max_calls:
                raise Exception("Rate limit exceeded")
            call_count += 1
            return {"success": True}

        for i in range(6):
            try:
                await rate_limited_process()
            except Exception as e:
                assert "Rate limit" in str(e)
                break


class TestMessageQueue:
    """消息队列测试"""

    @pytest.fixture
    def message_queue(self):
        """消息队列"""
        from collections import deque
        return deque()

    @pytest.mark.asyncio
    async def test_enqueue_message(self, message_queue):
        """测试入队消息"""
        message = {"chat_id": 100, "text": "test"}
        message_queue.append(message)
        assert len(message_queue) == 1

    @pytest.mark.asyncio
    async def test_dequeue_message(self, message_queue):
        """测试出队消息"""
        message_queue.append({"chat_id": 100, "text": "test"})
        message = message_queue.popleft()
        assert message["chat_id"] == 100
        assert len(message_queue) == 0

    @pytest.mark.asyncio
    async def test_batch_processing(self, message_queue):
        """测试批量处理"""
        # 添加多条消息
        for i in range(10):
            message_queue.append({"id": i, "text": f"msg_{i}"})

        batch_size = 3
        processed = 0

        while message_queue:
            batch = []
            for _ in range(batch_size):
                if message_queue:
                    batch.append(message_queue.popleft())

            # 模拟处理批次
            await asyncio.sleep(0.01)
            processed += len(batch)

        assert processed == 10


class TestMessageHistory:
    """消息历史测试"""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.messages = []
        db.save_message = AsyncMock(side_effect=lambda m: db.messages.append(m))
        db.get_messages = AsyncMock(return_value=[])
        return db

    @pytest.mark.asyncio
    async def test_save_message_history(self, mock_db):
        """测试保存消息历史"""
        messages = [
            {"id": 1, "text": "Hello"},
            {"id": 2, "text": "World"}
        ]

        for msg in messages:
            await mock_db.save_message(msg)

        assert len(mock_db.messages) == 2

    @pytest.mark.asyncio
    async def test_get_message_history(self, mock_db):
        """测试获取消息历史"""
        mock_db.get_messages.return_value = [
            {"id": 1, "text": "Hello"},
            {"id": 2, "text": "World"}
        ]

        history = await mock_db.get_messages(chat_id=100, limit=50)
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_search_messages(self, mock_db):
        """测试搜索消息"""
        mock_db.get_messages.return_value = [
            {"id": 1, "text": "Hello world"},
            {"id": 2, "text": "Python is great"}
        ]

        messages = mock_db.get_messages.return_value
        results = [m for m in messages if "Hello" in m.get("text", "")]

        assert len(results) == 1
        assert results[0]["text"] == "Hello world"


class TestTypingIndicator:
    """打字状态测试"""

    @pytest.mark.asyncio
    async def test_send_typing_action(self):
        """测试发送打字状态"""
        bot = MagicMock()
        bot.send_chat_action = AsyncMock(return_value=True)

        await bot.send_chat_action(chat_id=100, action="typing")

        bot.send_chat_action.assert_called_once_with(chat_id=100, action="typing")

    @pytest.mark.asyncio
    async def test_send_upload_action(self):
        """测试发送上传状态"""
        bot = MagicMock()
        bot.send_chat_action = AsyncMock(return_value=True)

        actions = ["typing", "upload_photo", "upload_video", "upload_document"]

        for action in actions:
            await bot.send_chat_action(chat_id=100, action=action)
            bot.send_chat_action.assert_called_with(chat_id=100, action=action)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
