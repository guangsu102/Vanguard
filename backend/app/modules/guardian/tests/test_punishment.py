"""
Unit Tests for Punishment Manager
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from app.modules.guardian.punishment.punishment_mgr import (
    PunishmentManager,
    PunishmentResult,
)
from app.modules.guardian.models import (
    ViolationAction,
    ViolationLevel,
)


class TestPunishmentManager:
    """Tests for PunishmentManager."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = AsyncMock()
        return db

    @pytest.fixture
    def punishment_manager(self, mock_db):
        """Create punishment manager instance."""
        with patch('app.modules.guardian.punishment.punishment_mgr.get_guardian_config') as mock_config:
            mock_config.return_value.warning_threshold = 3
            mock_config.return_value.mute_duration_seconds = 300
            mock_config.return_value.ban_threshold = 5
            mock_config.return_value.low_violation_mute_seconds = 300
            mock_config.return_value.medium_violation_mute_seconds = 1800
            mock_config.return_value.high_violation_mute_seconds = 3600
            manager = PunishmentManager(mock_db)
        return manager

    def test_punishment_result(self):
        """Test PunishmentResult dataclass."""
        result = PunishmentResult(
            action=ViolationAction.WARN,
            duration=None,
            reason="First warning",
            should_escalate=False
        )
        
        assert result.action == ViolationAction.WARN
        assert result.duration is None
        assert result.reason == "First warning"
        assert result.should_escalate is False

    def test_punishment_result_with_duration(self):
        """Test PunishmentResult with duration."""
        result = PunishmentResult(
            action=ViolationAction.MUTE,
            duration=300,
            reason="Exceeded warnings",
            should_escalate=True
        )
        
        assert result.action == ViolationAction.MUTE
        assert result.duration == 300
        assert result.should_escalate is True

    def test_get_mute_duration_low(self, punishment_manager):
        """Test mute duration for low severity."""
        duration = punishment_manager._get_mute_duration(ViolationLevel.LOW)
        assert duration == 300

    def test_get_mute_duration_medium(self, punishment_manager):
        """Test mute duration for medium severity."""
        duration = punishment_manager._get_mute_duration(ViolationLevel.MEDIUM)
        assert duration == 1800

    def test_get_mute_duration_high(self, punishment_manager):
        """Test mute duration for high severity."""
        duration = punishment_manager._get_mute_duration(ViolationLevel.HIGH)
        assert duration == 3600

    @pytest.mark.asyncio
    async def test_check_user_muted_not_muted(self, punishment_manager, mock_db):
        """Test checking user who is not muted."""
        mock_user = MagicMock()
        mock_user.muted_until = None
        
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = AsyncMock(return_value=mock_user)
        mock_db.execute = AsyncMock(return_value=mock_result)
        
        is_muted, expires = await punishment_manager.check_user_muted(1)
        
        assert is_muted is False
        assert expires is None

    @pytest.mark.asyncio
    async def test_check_user_muted_active(self, punishment_manager, mock_db):
        """Test checking user who is currently muted."""
        future_time = datetime.utcnow() + timedelta(hours=1)
        
        mock_user = MagicMock()
        mock_user.muted_until = future_time
        
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = AsyncMock(return_value=mock_user)
        mock_db.execute = AsyncMock(return_value=mock_result)
        
        is_muted, expires = await punishment_manager.check_user_muted(1)
        
        assert is_muted is True
        assert expires == future_time

    @pytest.mark.asyncio
    async def test_check_user_muted_expired(self, punishment_manager, mock_db):
        """Test checking user whose mute has expired."""
        past_time = datetime.utcnow() - timedelta(hours=1)
        
        mock_user = MagicMock()
        mock_user.muted_until = past_time
        
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none = AsyncMock(return_value=mock_user)
        mock_db.execute = AsyncMock(return_value=mock_result)
        
        is_muted, expires = await punishment_manager.check_user_muted(1)
        
        assert is_muted is False
        assert expires is None


class TestPunishmentResult:
    """Tests for PunishmentResult dataclass."""

    def test_warning_action(self):
        """Test warning action."""
        result = PunishmentResult(
            action=ViolationAction.WARN,
            duration=None,
            reason="Low violation",
            should_escalate=False
        )
        
        assert result.action == ViolationAction.WARN
        assert result.duration is None

    def test_mute_action(self):
        """Test mute action."""
        result = PunishmentResult(
            action=ViolationAction.MUTE,
            duration=300,
            reason="Repeated violation",
            should_escalate=True
        )
        
        assert result.action == ViolationAction.MUTE
        assert result.duration == 300
        assert result.should_escalate is True

    def test_ban_action(self):
        """Test ban action."""
        result = PunishmentResult(
            action=ViolationAction.BAN,
            duration=None,
            reason="Exceeded thresholds",
            should_escalate=False
        )
        
        assert result.action == ViolationAction.BAN
        assert result.duration is None
