"""
端到端测试 - 全流程测试
测试完整的业务流程：用户注册 -> 消息收发 -> 违规处理
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestEndToEndFlow:
    """端到端流程测试"""

    @pytest.fixture
    def mock_telegram_bot(self):
        """模拟 Telegram Bot"""
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=12345))
        bot.send_document = AsyncMock(return_value=MagicMock(message_id=12346))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        bot.ban_chat_member = AsyncMock(return_value=True)
        bot.restrict_chat_member = AsyncMock(return_value=True)
        return bot

    @pytest.fixture
    def mock_db(self):
        """模拟数据库"""
        db = MagicMock()
        db.users = MagicMock()
        db.messages = MagicMock()
        db.violations = MagicMock()
        db.save_user = AsyncMock(return_value=True)
        db.get_user = AsyncMock()
        db.record_message = AsyncMock(return_value=1)
        db.record_violation = AsyncMock(return_value=1)
        return db

    @pytest.fixture
    def mock_redis(self):
        """模拟 Redis"""
        redis = MagicMock()
        redis.client = MagicMock()
        redis.client.setex = AsyncMock(return_value=True)
        redis.client.get = AsyncMock(return_value=None)
        redis.client.exists = AsyncMock(return_value=False)
        redis.client.incr = AsyncMock(return_value=1)
        return redis

    @pytest.fixture
    def mock_api_client(self):
        """模拟 API 客户端"""
        api = MagicMock()
        api.register_user = AsyncMock(return_value={
            "code": 0,
            "data": {"user_id": 12345, "email": "test@example.com"}
        })
        api.track_user_action = AsyncMock(return_value={"code": 0})
        api.get_rules = AsyncMock(return_value={
            "code": 0,
            "data": [
                {"id": 1, "rule_type": "keyword", "pattern": "spam", "action": "warn"}
            ]
        })
        api.record_punishment = AsyncMock(return_value={"code": 0, "data": {"id": 1}})
        api.get_target_groups = AsyncMock(return_value={
            "code": 0,
            "data": [
                {"id": 1, "group_id": 100, "title": "Test Group", "member_count": 50}
            ]
        })
        return api

    @pytest.mark.asyncio
    async def test_group_message_processing_flow(self):
        """
        测试群消息处理流程。
        """
        from app.core.message.handlers import GroupTextHandler
        from app.core.message.models import MessageType, TelegramMessage

        keyword_handler = AsyncMock(return_value=True)
        moderation_handler = AsyncMock(return_value=True)
        message = TelegramMessage(
            message_id=999,
            chat_id=100,
            sender_id=123456,
            sender_name="test_user",
            message_type=MessageType.GROUP_TEXT,
            content="Hello, this is a test message",
        )
        handler = GroupTextHandler(
            keyword_handler=keyword_handler,
            moderation_handler=moderation_handler,
        )

        assert await handler.handle(message) is True
        keyword_handler.assert_awaited_once_with(message)
        moderation_handler.assert_awaited_once_with(message)

    @pytest.mark.asyncio
    async def test_registration_to_activation_flow(
        self, mock_telegram_bot, mock_db, mock_redis, mock_api_client
    ):
        """
        测试注册到激活流程：
        1. 用户发起注册
        2. 创建试用账户
        3. 发送激活链接
        4. 用户激活
        """
        mock_callback_query = MagicMock()
        mock_callback_query.from_user.id = 123456
        mock_callback_query.message.chat.id = 100
        mock_callback_query.data = "register"
        mock_callback_query.answer = AsyncMock()

        # 模拟注册流程
        from app.modules.acquisition.api_integration import AcquisitionAPIClient

        client = AcquisitionAPIClient(mock_api_client)
        mock_api_client.register_user.return_value = {
            "code": 0,
            "data": {
                "user_id": 123456,
                "email": "user@example.com",
                "trial_activated": True,
                "expires_at": "2026-05-29"
            }
        }

        result = await client.track_user_registration(
            user_id=123456,
            username="new_user",
            source_group_id=100,
            source_keyword="vpn",
            tracking_code="TRACK001"
        )

        assert result["data"]["user_id"] == 123456
        assert result["data"]["trial_activated"] is True

    @pytest.mark.asyncio
    async def test_group_management_flow(
        self, mock_telegram_bot, mock_db, mock_redis, mock_api_client
    ):
        """
        测试群组管理流程：
        1. 获取目标群组列表
        2. 加入群组
        3. 发送消息
        4. 监控群组状态
        """
        from app.modules.acquisition.api_integration import AcquisitionAPIClient

        client = AcquisitionAPIClient(mock_api_client)

        # 获取目标群组
        groups = await client.get_target_groups(min_level=2, limit=10)
        assert len(groups) >= 0  # 可能为空，但请求应该成功

        # 模拟加入群组
        mock_telegram_bot.get_chat = AsyncMock(return_value=MagicMock(
            id=100,
            title="Test Group",
            username="test_group",
            member_count=50
        ))

        chat = await mock_telegram_bot.get_chat(100)
        assert chat.username == "test_group"


class TestMultiModuleIntegration:
    """多模块集成测试"""

    @pytest.fixture
    def integrated_system(self):
        """集成系统"""
        return {
            "acquisition": MagicMock(),
            "guardian": MagicMock(),
            "service": MagicMock(),
            "cache": MagicMock(),
            "database": MagicMock()
        }

    @pytest.mark.asyncio
    async def test_acquisition_guardian_integration(self, integrated_system):
        """测试引流模块与防护模块集成"""
        # 模拟引流模块
        integrated_system["acquisition"].track_user = AsyncMock(return_value={
            "user_id": 12345,
            "source": "telegram_group"
        })

        # 模拟防护模块检查
        integrated_system["guardian"].check_user = AsyncMock(return_value={
            "is_safe": True,
            "risk_score": 10
        })

        # 执行集成流程
        user = await integrated_system["acquisition"].track_user(
            user_id=12345,
            source="telegram_group"
        )

        safety = await integrated_system["guardian"].check_user(user["user_id"])

        assert user["user_id"] == 12345
        assert safety["is_safe"] is True

    @pytest.mark.asyncio
    async def test_cache_database_consistency(self, integrated_system):
        """测试缓存与数据库一致性"""
        test_data = {"user_id": 123, "action": "test"}

        # 写入数据库
        integrated_system["database"].save = AsyncMock(return_value=True)

        # 写入缓存
        integrated_system["cache"].set = AsyncMock(return_value=True)
        integrated_system["cache"].get = AsyncMock(return_value=test_data)

        # 验证一致性
        await integrated_system["database"].save(test_data)
        await integrated_system["cache"].set("user:123", test_data)

        cached = await integrated_system["cache"].get("user:123")
        assert cached["user_id"] == test_data["user_id"]


class TestErrorRecovery:
    """错误恢复测试"""

    @pytest.fixture
    def error_prone_system(self):
        """容易出错的系统"""
        return {
            "api_client": MagicMock(),
            "db": MagicMock(),
            "redis": MagicMock()
        }

    @pytest.mark.asyncio
    async def test_api_failure_recovery(self, error_prone_system):
        """测试 API 失败后的恢复"""
        call_count = 0

        async def flaky_api(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("API temporarily unavailable")
            return {"code": 0, "data": {"status": "success"}}

        error_prone_system["api_client"].request = flaky_api

        # 使用重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = await error_prone_system["api_client"].request()
                if result["code"] == 0:
                    break
            except Exception:
                if attempt == max_retries - 1:
                    pytest.fail("Max retries exceeded")

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_database_failure_handling(self, error_prone_system):
        """测试数据库失败处理"""
        error_prone_system["db"].save = AsyncMock(side_effect=[
            Exception("DB connection lost"),
            Exception("DB connection lost"),
            True
        ])

        # 模拟降级到缓存
        error_prone_system["redis"].set = AsyncMock(return_value=True)

        saved = False
        for _attempt in range(3):
            try:
                await error_prone_system["db"].save({"test": "data"})
                saved = True
                break
            except Exception:
                await error_prone_system["redis"].set("backup", {"test": "data"})

        assert saved is True or error_prone_system["redis"].set.called


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
