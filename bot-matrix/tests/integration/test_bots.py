"""集成测试 - Bot 流程"""
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.bots.lead_gen import LeadGenBot
from src.bots.service import ServiceBot


class TestLeadGenBotFlow:
    """测试引流 Bot 流程"""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.client = AsyncMock()
        redis.client.exists = AsyncMock(return_value=False)
        redis.client.setex = AsyncMock(return_value=True)
        return redis

    @pytest.fixture
    def mock_api(self):
        api = AsyncMock()
        api.create_trial_user = AsyncMock(return_value={
            "success": True,
            "data": {
                "user_id": 1,
                "email": "test@example.com",
                "expires_at": "2026-05-18 20:00:00"
            }
        })
        api.get_subscription_link = AsyncMock(return_value={
            "clash": "https://xboard.com/clash",
            "v2ray": "https://xboard.com/v2ray"
        })
        return api

    @pytest.fixture
    def lead_gen_config(self):
        return {
            "telegram": {
                "lead_gen_bot_token": "test_token",
                "official_channels": ["@test_channel"]
            },
            "lead_gen": {
                "trial": {
                    "enabled": True,
                    "validity_hours": 24,
                    "traffic_gb": 50,
                    "enable_duplicate_check": True
                },
                "risk_control": {
                    "max_requests_per_ip_per_hour": 10,
                    "max_trials_per_uid_per_day": 1
                }
            }
        }

    @pytest.mark.asyncio
    async def test_check_subscription_already_subscribed(self, mock_db, mock_redis, mock_api, lead_gen_config):
        """测试用户已订阅时的检查"""
        mock_bot = AsyncMock()
        mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        mock_account_manager = AsyncMock()

        lead_gen = LeadGenBot(
            account_manager=mock_account_manager,
            db=mock_db,
            redis=mock_redis,
            api=mock_api,
            config=lead_gen_config
        )
        lead_gen.bot = mock_bot

        is_subscribed, not_subscribed = await lead_gen.check_subscription(123456)

        assert is_subscribed is True
        assert len(not_subscribed) == 0

    @pytest.mark.asyncio
    async def test_check_subscription_not_subscribed(self, mock_db, mock_redis, mock_api, lead_gen_config):
        """测试用户未订阅时的检查"""
        mock_bot = AsyncMock()
        mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))
        mock_account_manager = AsyncMock()

        lead_gen = LeadGenBot(
            account_manager=mock_account_manager,
            db=mock_db,
            redis=mock_redis,
            api=mock_api,
            config=lead_gen_config
        )
        lead_gen.bot = mock_bot

        is_subscribed, not_subscribed = await lead_gen.check_subscription(123456)

        assert is_subscribed is False
        assert "@test_channel" in not_subscribed


class TestServiceBotFlow:
    """测试运营 Bot 流程"""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.client = AsyncMock()
        redis.client.exists = AsyncMock(return_value=False)
        redis.client.incr = AsyncMock(return_value=1)
        redis.client.setex = AsyncMock(return_value=True)
        return redis

    @pytest.fixture
    def mock_api(self):
        api = AsyncMock()
        api.add_traffic = AsyncMock(return_value={
            "success": True,
            "data": {"added_mb": 512}
        })
        api.get_user_info = AsyncMock(return_value={
            "success": True,
            "data": {
                "traffic_remaining_gb": 50,
                "expiry_date": "2026-05-18"
            }
        })
        api.get_affiliate_link = AsyncMock(return_value={
            "success": True,
            "data": {
                "aff_link": "https://xboard.com/register?aff=ABC123"
            }
        })
        return api

    @pytest.fixture
    def mock_poster(self):
        poster = AsyncMock()
        poster.generate_affiliate_poster = AsyncMock(
            return_value="/tmp/poster_123456.png"
        )
        return poster

    @pytest.fixture
    def service_config(self):
        return {
            "telegram": {
                "service_bot_token": "test_token"
            },
            "service": {
                "checkin": {
                    "enabled": True,
                    "min_traffic_mb": 100,
                    "max_traffic_mb": 1024,
                    "cooldown_hours": 24
                },
                "poster": {
                    "enabled": True,
                    "output_dir": "./assets/posters"
                }
            }
        }

    @pytest.mark.asyncio
    async def test_checkin_success(self, mock_db, mock_redis, mock_api, mock_poster, service_config):
        """测试签到成功"""
        mock_message = MagicMock()
        mock_message.from_user.id = 123456
        mock_message.from_user.username = "testuser"
        mock_message.from_user.first_name = "Test"
        mock_message.answer = AsyncMock()

        mock_redis.client.exists = AsyncMock(return_value=False)
        mock_db.record_checkin = AsyncMock()
        mock_account_manager = AsyncMock()

        service = ServiceBot(
            account_manager=mock_account_manager,
            db=mock_db,
            redis=mock_redis,
            api=mock_api,
            poster=mock_poster,
            config=service_config
        )

        await service.handle_checkin(mock_message, MagicMock())

        mock_message.answer.assert_called_once()
        mock_db.record_checkin.assert_called_once()

    @pytest.mark.asyncio
    async def test_checkin_cooldown(self, mock_db, mock_redis, mock_api, mock_poster, service_config):
        """测试签到冷却中"""
        mock_message = MagicMock()
        mock_message.from_user.id = 123456
        mock_message.from_user.first_name = "Test"
        mock_message.answer = AsyncMock()

        mock_redis.client.exists = AsyncMock(return_value=True)
        mock_redis.client.ttl = AsyncMock(return_value=3600)
        mock_account_manager = AsyncMock()

        service = ServiceBot(
            account_manager=mock_account_manager,
            db=mock_db,
            redis=mock_redis,
            api=mock_api,
            poster=mock_poster,
            config=service_config
        )

        await service.handle_checkin(mock_message, MagicMock())

        # 应该返回冷却提示
        mock_message.answer.assert_called_once()
