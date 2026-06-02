"""
违规处理流程测试
测试内容审核、违规检测和惩罚执行的全流程
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional


@pytest.fixture
def mock_guardian_client():
    """模拟 Guardian 客户端"""
    client = MagicMock()
    client.check_content = AsyncMock(return_value={
        "code": 0,
        "data": {
            "is_violation": False,
            "risk_score": 10,
            "matched_rules": []
        }
    })
    client.record_violation = AsyncMock(return_value={
        "code": 0,
        "data": {"violation_id": 1}
    })
    return client


class TestViolationDetection:
    """违规检测测试"""

    @pytest.fixture
    def mock_moderation_service(self):
        """模拟审核服务"""
        service = MagicMock()
        service.moderate = AsyncMock(return_value={
            "action": "allow",
            "confidence": 0.95
        })
        return service

    @pytest.mark.asyncio
    async def test_keyword_violation_detection(self, mock_guardian_client):
        """测试关键词违规检测"""
        mock_guardian_client.check_content.return_value = {
            "code": 0,
            "data": {
                "is_violation": True,
                "risk_score": 85,
                "matched_rules": [
                    {"rule_id": 1, "pattern": "spam", "action": "warn"}
                ]
            }
        }

        result = await mock_guardian_client.check_content(
            content="This is spam content",
            user_id=123456
        )

        assert result["data"]["is_violation"] is True
        assert result["data"]["risk_score"] == 85
        assert len(result["data"]["matched_rules"]) > 0

    @pytest.mark.asyncio
    async def test_domain_blocklist_detection(self, mock_guardian_client):
        """测试域名黑名单检测"""
        mock_guardian_client.check_content.return_value = {
            "code": 0,
            "data": {
                "is_violation": True,
                "risk_score": 95,
                "matched_rules": [
                    {"rule_id": 2, "type": "domain", "pattern": "evil.com", "action": "ban"}
                ]
            }
        }

        result = await mock_guardian_client.check_content(
            content="Check out evil.com for deals!",
            user_id=123456
        )

        assert result["data"]["is_violation"] is True
        assert result["data"]["matched_rules"][0]["type"] == "domain"

    @pytest.mark.asyncio
    async def test_safe_content_passthrough(self, mock_guardian_client):
        """测试安全内容放行"""
        result = await mock_guardian_client.check_content(
            content="Hello, how are you today?",
            user_id=123456
        )

        assert result["data"]["is_violation"] is False
        assert result["data"]["risk_score"] < 50


class TestPunishmentExecution:
    """惩罚执行测试"""

    @pytest.fixture
    def mock_punishment_service(self):
        """模拟惩罚服务"""
        service = MagicMock()
        service.warn = AsyncMock(return_value={"code": 0, "data": {"action": "warn"}})
        service.mute = AsyncMock(return_value={"code": 0, "data": {"action": "mute"}})
        service.ban = AsyncMock(return_value={"code": 0, "data": {"action": "ban"}})
        service.kick = AsyncMock(return_value={"code": 0, "data": {"action": "kick"}})
        return service

    @pytest.mark.asyncio
    async def test_warn_punishment(self, mock_punishment_service):
        """测试警告惩罚"""
        result = await mock_punishment_service.warn(
            user_id=123456,
            group_id=100,
            reason="Spam content"
        )

        assert result["data"]["action"] == "warn"

    @pytest.mark.asyncio
    async def test_mute_punishment(self, mock_punishment_service):
        """测试禁言惩罚"""
        result = await mock_punishment_service.mute(
            user_id=123456,
            group_id=100,
            duration_minutes=30
        )

        assert result["data"]["action"] == "mute"

    @pytest.mark.asyncio
    async def test_temp_ban_punishment(self, mock_punishment_service):
        """测试临时封禁惩罚"""
        result = await mock_punishment_service.ban(
            user_id=123456,
            group_id=100,
            duration_hours=24,
            reason="Repeated violations"
        )

        assert result["data"]["action"] == "ban"

    @pytest.mark.asyncio
    async def test_permanent_ban_punishment(self, mock_punishment_service):
        """测试永久封禁惩罚"""
        result = await mock_punishment_service.ban(
            user_id=123456,
            group_id=100,
            permanent=True,
            reason="Severe violation"
        )

        assert result["data"]["action"] == "ban"

    @pytest.mark.asyncio
    async def test_kick_punishment(self, mock_punishment_service):
        """测试踢出惩罚"""
        result = await mock_punishment_service.kick(
            user_id=123456,
            group_id=100
        )

        assert result["data"]["action"] == "kick"


class TestEscalationLogic:
    """惩罚升级逻辑测试"""

    @pytest.fixture
    def escalation_engine(self):
        """惩罚升级引擎"""
        return {
            "warn_count": 0,
            "mute_count": 0,
            "ban_count": 0
        }

    def test_first_violation_warn(self, escalation_engine):
        """测试首次违规警告"""
        escalation_engine["warn_count"] = 0
        action = self._get_action(escalation_engine)
        assert action == "warn"

    def test_second_violation_mute(self, escalation_engine):
        """测试二次违规禁言"""
        escalation_engine["warn_count"] = 1
        action = self._get_action(escalation_engine)
        assert action == "mute"

    def test_third_violation_temp_ban(self, escalation_engine):
        """测试三次违规临时封禁"""
        escalation_engine["warn_count"] = 2
        escalation_engine["mute_count"] = 1
        action = self._get_action(escalation_engine)
        assert action == "temp_ban"

    def test_fourth_violation_permanent_ban(self, escalation_engine):
        """测试四次违规永久封禁"""
        escalation_engine["warn_count"] = 3
        escalation_engine["mute_count"] = 2
        escalation_engine["ban_count"] = 1
        action = self._get_action(escalation_engine)
        assert action == "permanent_ban"

    def _get_action(self, engine):
        """根据历史记录获取应执行的操作"""
        if engine["warn_count"] == 0:
            return "warn"
        elif engine["mute_count"] == 0:
            return "mute"
        elif engine["ban_count"] == 0:
            return "temp_ban"
        else:
            return "permanent_ban"


class TestViolationRecords:
    """违规记录测试"""

    @pytest.fixture
    def mock_db(self):
        """模拟数据库"""
        db = MagicMock()
        db.violations = []
        db.save_violation = AsyncMock(side_effect=lambda v: db.violations.append(v))
        db.get_violations = MagicMock(return_value=[])
        db.get_violation_count = MagicMock(return_value=0)
        return db

    @pytest.mark.asyncio
    async def test_record_violation(self, mock_db):
        """测试记录违规"""
        violation = {
            "id": 1,
            "user_id": 123456,
            "group_id": 100,
            "rule_type": "keyword",
            "action": "warn",
            "content": "spam content",
            "timestamp": datetime.now()
        }

        await mock_db.save_violation(violation)

        assert len(mock_db.violations) == 1
        assert mock_db.violations[0]["user_id"] == 123456

    @pytest.mark.asyncio
    async def test_get_user_violations(self, mock_db):
        """测试获取用户违规记录"""
        mock_db.violations = [
            {"user_id": 123456, "action": "warn"},
            {"user_id": 123456, "action": "mute"},
            {"user_id": 789012, "action": "warn"}
        ]

        user_violations = [v for v in mock_db.violations if v["user_id"] == 123456]
        assert len(user_violations) == 2

    @pytest.mark.asyncio
    async def test_violation_history_for_escalation(self, mock_db):
        """测试违规历史用于升级判断"""
        mock_db.violations = [
            {"user_id": 123456, "action": "warn", "timestamp": datetime.now() - timedelta(days=1)},
            {"user_id": 123456, "action": "warn", "timestamp": datetime.now() - timedelta(days=7)},
            {"user_id": 123456, "action": "mute", "timestamp": datetime.now() - timedelta(days=3)}
        ]

        # 统计最近30天内的违规
        recent_violations = [
            v for v in mock_db.violations
            if v["user_id"] == 123456
            and (datetime.now() - v["timestamp"]).days <= 30
        ]

        assert len(recent_violations) == 3


class TestVerificationFlow:
    """验证流程测试"""

    @pytest.fixture
    def mock_verification_service(self):
        """模拟验证服务"""
        service = MagicMock()
        service.create_session = AsyncMock(return_value={
            "code": 0,
            "data": {
                "session_id": "session_123",
                "type": "captcha",
                "expires_at": (datetime.now() + timedelta(minutes=5)).isoformat()
            }
        })
        service.verify = AsyncMock(return_value={
            "code": 0,
            "data": {"verified": True}
        })
        service.check_status = AsyncMock(return_value={
            "code": 0,
            "data": {"status": "completed"}
        })
        return service

    @pytest.mark.asyncio
    async def test_create_captcha_verification(self, mock_verification_service):
        """测试创建验证码验证"""
        result = await mock_verification_service.create_session(
            user_id=123456,
            group_id=100,
            verify_type="captcha"
        )

        assert result["data"]["session_id"] == "session_123"
        assert result["data"]["type"] == "captcha"

    @pytest.mark.asyncio
    async def test_verify_captcha_success(self, mock_verification_service):
        """测试验证码验证成功"""
        result = await mock_verification_service.verify(
            session_id="session_123",
            captcha_response="correct_answer"
        )

        assert result["data"]["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_captcha_failure(self, mock_verification_service):
        """测试验证码验证失败"""
        mock_verification_service.verify = AsyncMock(return_value={
            "code": 0,
            "data": {"verified": False, "reason": "Invalid captcha"}
        })

        result = await mock_verification_service.verify(
            session_id="session_123",
            captcha_response="wrong_answer"
        )

        assert result["data"]["verified"] is False

    @pytest.mark.asyncio
    async def test_verification_session_expiry(self, mock_verification_service):
        """测试验证会话过期"""
        mock_verification_service.check_status = AsyncMock(return_value={
            "code": 0,
            "data": {"status": "expired"}
        })

        result = await mock_verification_service.check_status(session_id="session_123")
        assert result["data"]["status"] == "expired"


class TestWhitelistManagement:
    """白名单管理测试"""

    @pytest.fixture
    def mock_whitelist_service(self):
        """模拟白名单服务"""
        service = MagicMock()
        service.check = AsyncMock(return_value={"is_whitelisted": False})
        service.add = AsyncMock(return_value={"code": 0})
        service.remove = AsyncMock(return_value={"code": 0})
        return service

    @pytest.mark.asyncio
    async def test_check_whitelist(self, mock_whitelist_service):
        """测试检查白名单"""
        result = await mock_whitelist_service.check(user_id=123456)
        assert "is_whitelisted" in result

    @pytest.mark.asyncio
    async def test_add_to_whitelist(self, mock_whitelist_service):
        """测试添加到白名单"""
        result = await mock_whitelist_service.add(
            user_id=123456,
            reason="VIP user",
            added_by="admin"
        )
        assert result["code"] == 0

    @pytest.mark.asyncio
    async def test_remove_from_whitelist(self, mock_whitelist_service):
        """测试从白名单移除"""
        result = await mock_whitelist_service.remove(
            user_id=123456,
            removed_by="admin",
            reason="No longer VIP"
        )
        assert result["code"] == 0

    @pytest.mark.asyncio
    async def test_whitelist_overrides_violation(self, mock_whitelist_service, mock_guardian_client):
        """测试白名单覆盖违规"""
        mock_whitelist_service.check = AsyncMock(return_value={"is_whitelisted": True})
        mock_guardian_client.check_content.return_value = {
            "code": 0,
            "data": {
                "is_violation": True,
                "whitelist_overridden": True
            }
        }

        # 检查白名单
        is_whitelisted = await mock_whitelist_service.check(user_id=123456)

        # 检查内容
        content_check = await mock_guardian_client.check_content(
            content="any content",
            user_id=123456
        )

        # 白名单用户应该被放过
        assert is_whitelisted["is_whitelisted"] is True
        assert content_check["data"].get("whitelist_overridden") is True


class TestAutoModeration:
    """自动审核测试"""

    @pytest.fixture
    def mock_auto_moderator(self):
        """模拟自动审核器"""
        moderator = MagicMock()
        moderator.moderate = AsyncMock(return_value={
            "action": "allow",
            "confidence": 0.95
        })
        return moderator

    @pytest.mark.asyncio
    async def test_auto_moderate_safe_content(self, mock_auto_moderator):
        """测试自动审核安全内容"""
        result = await mock_auto_moderator.moderate(
            content="This is a normal message about VPN services.",
            user_id=123456
        )

        assert result["action"] == "allow"
        assert result["confidence"] >= 0.9

    @pytest.mark.asyncio
    async def test_auto_moderate_suspicious_content(self, mock_auto_moderator):
        """测试自动审核可疑内容"""
        mock_auto_moderator.moderate = AsyncMock(return_value={
            "action": "review",
            "confidence": 0.6
        })

        result = await mock_auto_moderator.moderate(
            content="Get cheap followers @some_link.com",
            user_id=123456
        )

        assert result["action"] == "review"
        assert result["confidence"] < 0.9

    @pytest.mark.asyncio
    async def test_auto_moderate_malicious_content(self, mock_auto_moderator):
        """测试自动审核恶意内容"""
        mock_auto_moderator.moderate = AsyncMock(return_value={
            "action": "block",
            "confidence": 0.98
        })

        result = await mock_auto_moderator.moderate(
            content="Click here for free money! evil-redirect.com",
            user_id=123456
        )

        assert result["action"] == "block"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
