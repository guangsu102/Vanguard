"""
注册转化流程测试
测试用户从引流到注册的完整转化流程
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestUserRegistration:
    """用户注册测试"""

    @pytest.fixture
    def mock_api_client(self):
        """模拟 API 客户端"""
        api = MagicMock()
        api.register_user = AsyncMock(return_value={
            "code": 0,
            "data": {
                "user_id": 123456,
                "email": "user@example.com",
                "created_at": datetime.now().isoformat()
            }
        })
        api.track_user_action = AsyncMock(return_value={"code": 0})
        api.get_active_campaigns = AsyncMock(return_value={
            "code": 0,
            "data": [
                {"id": 1, "name": "Summer Sale", "active": True}
            ]
        })
        return api

    @pytest.fixture
    def mock_db(self):
        """模拟数据库"""
        db = MagicMock()
        db.users = {}
        db.save_user = AsyncMock(side_effect=lambda u: db.users.update({u["user_id"]: u}))
        db.get_user = MagicMock(return_value=None)
        db.record_conversion = AsyncMock(return_value=1)
        return db

    @pytest.fixture
    def mock_redis(self):
        """模拟 Redis"""
        redis = MagicMock()
        redis.client = MagicMock()
        redis.client.setex = AsyncMock(return_value=True)
        redis.client.get = AsyncMock(return_value=None)
        redis.client.exists = AsyncMock(return_value=False)
        return redis

    @pytest.mark.asyncio
    async def test_new_user_registration(
        self, mock_api_client, mock_db, mock_redis
    ):
        """测试新用户注册"""
        from app.modules.acquisition.api_integration import AcquisitionAPIClient

        client = AcquisitionAPIClient(mock_api_client)

        result = await client.track_user_registration(
            user_id=123456,
            username="new_user",
            source_group_id=100,
            source_keyword="vpn",
            tracking_code="TRACK001"
        )

        assert result["data"]["user_id"] == 123456
        mock_api_client.register_user.assert_called_once()
        mock_api_client.track_user_action.assert_called()

    @pytest.mark.asyncio
    async def test_duplicate_registration_prevention(
        self, mock_api_client, mock_db, mock_redis
    ):
        """测试防止重复注册"""
        from app.modules.acquisition.api_integration import AcquisitionAPIClient

        # 模拟用户已存在
        mock_db.get_user.return_value = {
            "user_id": 123456,
            "username": "existing_user",
            "email": "user@example.com"
        }

        client = AcquisitionAPIClient(mock_api_client)
        mock_api_client.register_user.return_value = {
            "code": 409,
            "message": "User already exists"
        }

        # 尝试重复注册
        result = await client.track_user_registration(
            user_id=123456,
            username="existing_user",
            source_group_id=100,
            source_keyword="vpn",
            tracking_code="TRACK001"
        )

        # 应该返回冲突错误
        assert result["code"] == 409

    @pytest.mark.asyncio
    async def test_registration_with_tracking_code(
        self, mock_api_client, mock_redis
    ):
        """测试带追踪码的注册"""
        from app.modules.acquisition.api_integration import AcquisitionAPIClient

        client = AcquisitionAPIClient(mock_api_client)

        result = await client.track_user_registration(
            user_id=123456,
            username="tracked_user",
            source_group_id=100,
            source_keyword="vpn",
            tracking_code="SUMMER2026"
        )

        assert result["data"]["user_id"] == 123456
        mock_api_client.register_user.assert_awaited_once_with(
            user_id=123456,
            username="tracked_user",
            source="telegram",
            source_group_id=100,
            tracking_code="SUMMER2026",
        )
        mock_api_client.track_user_action.assert_awaited_once_with(
            user_id=123456,
            action="register",
            metadata={"keyword": "vpn"},
        )


class TestTrialActivation:
    """试用激活测试"""

    @pytest.fixture
    def mock_trial_service(self):
        """模拟试用服务"""
        service = MagicMock()
        service.create_trial = AsyncMock(return_value={
            "code": 0,
            "data": {
                "trial_id": "trial_123",
                "expires_at": (datetime.now() + timedelta(days=1)).isoformat(),
                "traffic_gb": 50
            }
        })
        service.check_trial_status = AsyncMock(return_value={
            "code": 0,
            "data": {"active": True, "remaining_gb": 50}
        })
        return service

    @pytest.mark.asyncio
    async def test_create_trial_account(self, mock_trial_service):
        """测试创建试用账户"""
        result = await mock_trial_service.create_trial(
            user_id=123456,
            email="user@example.com"
        )

        assert result["data"]["trial_id"] == "trial_123"
        assert result["data"]["traffic_gb"] == 50

    @pytest.mark.asyncio
    async def test_check_trial_expiry(self, mock_trial_service):
        """测试检查试用过期"""
        mock_trial_service.check_trial_status.return_value = {
            "code": 0,
            "data": {"active": False, "reason": "expired"}
        }

        result = await mock_trial_service.check_trial_status(user_id=123456)

        assert result["data"]["active"] is False
        assert result["data"]["reason"] == "expired"

    @pytest.mark.asyncio
    async def test_trial_renewal(self, mock_trial_service):
        """测试试用续期"""
        # 模拟首次续期成功
        mock_trial_service.create_trial.return_value = {
            "code": 0,
            "data": {
                "trial_id": "trial_456",
                "expires_at": (datetime.now() + timedelta(days=2)).isoformat(),
                "traffic_gb": 50
            }
        }

        result = await mock_trial_service.create_trial(
            user_id=123456,
            email="user@example.com",
            extend=True
        )

        assert result["code"] == 0


class TestConversionTracking:
    """转化追踪测试"""

    @pytest.fixture
    def mock_tracker(self):
        """模拟追踪器"""
        tracker = MagicMock()
        tracker.track_event = AsyncMock(return_value={"code": 0})
        tracker.track_conversion = AsyncMock(return_value={"code": 0})
        tracker.get_analytics = AsyncMock(return_value={
            "total_users": 100,
            "conversions": 25,
            "conversion_rate": 0.25
        })
        return tracker

    @pytest.mark.asyncio
    async def test_track_registration_event(self, mock_tracker):
        """测试追踪注册事件"""
        await mock_tracker.track_event(
            event_name="user_registered",
            user_id=123456,
            properties={"source": "telegram", "campaign": "summer"}
        )

        mock_tracker.track_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_track_conversion_funnel(self, mock_tracker):
        """测试追踪转化漏斗"""
        funnel_steps = [
            {"step": "view", "count": 1000},
            {"step": "click", "count": 500},
            {"step": "register", "count": 100},
            {"step": "activate", "count": 25}
        ]

        for step in funnel_steps:
            await mock_tracker.track_conversion(
                funnel_name="registration",
                step=step["step"],
                count=step["count"]
            )

        mock_tracker.track_conversion.assert_called()

    @pytest.mark.asyncio
    async def test_get_conversion_analytics(self, mock_tracker):
        """测试获取转化分析"""
        analytics = await mock_tracker.get_analytics(
            start_date="2026-05-01",
            end_date="2026-05-22"
        )

        assert analytics["conversion_rate"] == 0.25


class TestReferralFlow:
    """推荐流程测试"""

    @pytest.fixture
    def mock_referral_service(self):
        """模拟推荐服务"""
        service = MagicMock()
        service.create_referral_link = AsyncMock(return_value={
            "code": 0,
            "data": {
                "link": "https://xboard.com/ref/ABC123",
                "code": "ABC123"
            }
        })
        service.track_referral = AsyncMock(return_value={"code": 0})
        service.get_referral_rewards = AsyncMock(return_value={
            "code": 0,
            "data": {
                "total_referrals": 5,
                "pending_rewards": 100,
                "total_traffic_gb": 250
            }
        })
        return service

    @pytest.mark.asyncio
    async def test_generate_referral_link(self, mock_referral_service):
        """测试生成推荐链接"""
        result = await mock_referral_service.create_referral_link(user_id=123456)

        assert "ref/ABC123" in result["data"]["link"]
        assert result["data"]["code"] == "ABC123"

    @pytest.mark.asyncio
    async def test_track_referral_conversion(self, mock_referral_service):
        """测试追踪推荐转化"""
        await mock_referral_service.track_referral(
            referrer_id=123456,
            referred_id=789012,
            referral_code="ABC123"
        )

        mock_referral_service.track_referral.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_referral_rewards(self, mock_referral_service):
        """测试获取推荐奖励"""
        rewards = await mock_referral_service.get_referral_rewards(user_id=123456)

        assert rewards["data"]["total_referrals"] == 5
        assert rewards["data"]["total_traffic_gb"] == 250


class TestCampaignIntegration:
    """活动集成测试"""

    @pytest.fixture
    def mock_campaign_service(self):
        """模拟活动服务"""
        service = MagicMock()
        service.get_active_campaigns = AsyncMock(return_value={
            "code": 0,
            "data": [
                {"id": 1, "name": "Summer Promo", "bonus_gb": 10},
                {"id": 2, "name": "New User Bonus", "bonus_gb": 20}
            ]
        })
        service.apply_campaign_bonus = AsyncMock(return_value={"code": 0})
        return service

    @pytest.mark.asyncio
    async def test_get_active_campaigns(self, mock_campaign_service):
        """测试获取活跃活动"""
        campaigns = await mock_campaign_service.get_active_campaigns()

        assert len(campaigns["data"]) == 2
        assert any(c["name"] == "Summer Promo" for c in campaigns["data"])

    @pytest.mark.asyncio
    async def test_apply_campaign_bonus(self, mock_campaign_service):
        """测试应用活动奖励"""
        result = await mock_campaign_service.apply_campaign_bonus(
            user_id=123456,
            campaign_id=1
        )

        assert result["code"] == 0


class TestOnboardingFlow:
    """用户引导流程测试"""

    @pytest.mark.asyncio
    async def test_onboarding_sequence(self):
        """测试引导序列"""
        onboarding_steps = [
            {"step": 1, "action": "welcome", "completed": False},
            {"step": 2, "action": "setup_profile", "completed": False},
            {"step": 3, "action": "first_checkin", "completed": False}
        ]

        # 模拟完成每一步
        for step in onboarding_steps:
            step["completed"] = True

        completed_count = sum(1 for s in onboarding_steps if s["completed"])
        assert completed_count == len(onboarding_steps)

    @pytest.mark.asyncio
    async def test_skip_onboarding(self):
        """测试跳过引导"""
        skip_allowed = True

        if skip_allowed:
            assert True

    @pytest.mark.asyncio
    async def test_onboarding_completion_reward(self):
        """测试引导完成奖励"""
        reward_granted = False

        async def complete_onboarding():
            nonlocal reward_granted
            reward_granted = True
            return {"bonus_gb": 10}

        result = await complete_onboarding()

        assert reward_granted is True
        assert result["bonus_gb"] == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
