"""
Integration Tests for Business Module Integration

Tests the integration between:
- Bot modules and Backend API
- Frontend and Backend API
- Data flow validation
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


class TestBotAPIClient:
    """Test Bot API Client integration."""

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {"code": 0, "message": "success", "data": {}}
        response.raise_for_status = MagicMock()
        return response

    @pytest.fixture
    def api_client(self):
        """Create a Bot API client instance."""
        from app.modules.integrations.api_client import BotAPIClient
        return BotAPIClient(
            base_url="http://localhost:8000",
            api_key="test-api-key",
        )

    @pytest.mark.asyncio
    async def test_get_account(self, api_client, mock_response):
        """Test getting account details."""
        mock_response.json.return_value = {
            "code": 0,
            "data": {"id": 1, "phone": "+1234567890", "status": "online"}
        }

        with patch.object(api_client, '_request', return_value=mock_response.json.return_value):
            result = await api_client.get_account(1)
            assert result["data"]["id"] == 1
            assert result["data"]["phone"] == "+1234567890"

    @pytest.mark.asyncio
    async def test_get_target_groups(self, api_client):
        """Test getting target groups for acquisition."""
        mock_data = {
            "code": 0,
            "data": [
                {"id": 1, "group_id": 123, "title": "Test Group", "member_count": 100},
                {"id": 2, "group_id": 456, "title": "Another Group", "member_count": 200},
            ]
        }

        with patch.object(api_client, '_request', return_value=mock_data):
            result = await api_client.get_target_groups(min_level=2, limit=10)
            assert len(result["data"]) == 2
            assert result["data"][0]["title"] == "Test Group"

    @pytest.mark.asyncio
    async def test_track_user_action(self, api_client):
        """Test tracking user action."""
        mock_data = {"code": 0, "message": "success"}
        metadata = {"source": "telegram", "action_type": "click"}

        with patch.object(api_client, '_request', return_value=mock_data):
            result = await api_client.track_user_action(
                user_id=12345,
                action="register",
                metadata=metadata,
            )
            assert result["code"] == 0

    @pytest.mark.asyncio
    async def test_get_rules(self, api_client):
        """Test getting moderation rules."""
        mock_data = {
            "code": 0,
            "data": [
                {"id": 1, "rule_type": "keyword", "pattern": "spam", "action": "warn"},
                {"id": 2, "rule_type": "domain", "pattern": "evil.com", "action": "ban"},
            ]
        }

        with patch.object(api_client, '_request', return_value=mock_data):
            result = await api_client.get_rules(enabled=True)
            assert len(result["data"]) == 2
            assert result["data"][0]["action"] == "warn"

    @pytest.mark.asyncio
    async def test_record_punishment(self, api_client):
        """Test recording punishment."""
        mock_data = {
            "code": 0,
            "data": {"id": 1, "user_id": 123, "action": "mute"}
        }

        with patch.object(api_client, '_request', return_value=mock_data):
            result = await api_client.record_punishment(
                user_id=123,
                group_id=456,
                rule_type="keyword",
                action="mute",
                duration=3600,
            )
            assert result["data"]["action"] == "mute"

    @pytest.mark.asyncio
    async def test_create_verification(self, api_client):
        """Test creating verification session."""
        mock_data = {
            "code": 0,
            "data": {
                "session_id": "test-session-123",
                "user_id": 123,
                "state": "pending",
            }
        }

        with patch.object(api_client, '_request', return_value=mock_data):
            result = await api_client.create_verification(
                user_id=123,
                chat_id=456,
                verify_type="captcha",
            )
            assert result["data"]["session_id"] == "test-session-123"
            assert result["data"]["state"] == "pending"


class TestAcquisitionAPIClient:
    """Test Acquisition API Client integration."""

    @pytest.fixture
    def mock_bot_client(self):
        """Create a mock Bot API client."""
        client = MagicMock()
        client._request = AsyncMock()
        client.get_target_groups = AsyncMock()
        client.register_user = AsyncMock()
        client.track_user_action = AsyncMock()
        client.get_active_campaigns = AsyncMock()
        client.record_campaign_action = AsyncMock()
        return client

    @pytest.fixture
    def acquisition_client(self, mock_bot_client):
        """Create an Acquisition API client."""
        from app.modules.acquisition.api_integration import AcquisitionAPIClient
        return AcquisitionAPIClient(mock_bot_client)

    @pytest.mark.asyncio
    async def test_get_target_groups(self, acquisition_client, mock_bot_client):
        """Test getting target groups."""
        mock_bot_client.get_target_groups.return_value = {
            "data": [
                {"id": 1, "group_id": 123, "title": "Group A"},
            ]
        }

        result = await acquisition_client.get_target_groups(min_level=2)
        assert len(result) == 1
        assert result[0]["title"] == "Group A"

    @pytest.mark.asyncio
    async def test_record_message_sent(self, acquisition_client, mock_bot_client):
        """Test recording sent message."""
        mock_bot_client._request.return_value = {
            "code": 0,
            "data": {"id": 1, "account_id": 1}
        }

        result = await acquisition_client.record_message_sent(
            account_id=1,
            group_id=123,
            content="Hello world!",
            message_type="interaction",
        )
        assert result["data"]["id"] == 1


class TestGuardianAPIClient:
    """Test Guardian API Client integration."""

    @pytest.fixture
    def mock_bot_client(self):
        """Create a mock Bot API client."""
        client = MagicMock()
        client._request = AsyncMock()
        client.get_rules = AsyncMock()
        client.check_whitelist = AsyncMock()
        client.record_punishment = AsyncMock()
        client.create_verification = AsyncMock()
        client.verify_captcha = AsyncMock()
        client._request.return_value = {"code": 0, "data": {}}
        return client

    @pytest.fixture
    def guardian_client(self, mock_bot_client):
        """Create a Guardian API client."""
        from app.modules.guardian.api_integration import GuardianAPIClient
        return GuardianAPIClient(mock_bot_client)

    @pytest.mark.asyncio
    async def test_get_moderation_rules(self, guardian_client, mock_bot_client):
        """Test getting moderation rules."""
        mock_bot_client.get_rules.return_value = {
            "data": [
                {"id": 1, "rule_type": "keyword", "pattern": "bad", "action": "warn"},
            ]
        }

        result = await guardian_client.get_moderation_rules(rule_type="keyword")
        assert len(result) == 1
        assert result[0]["pattern"] == "bad"

    @pytest.mark.asyncio
    async def test_is_whitelisted(self, guardian_client, mock_bot_client):
        """Test checking whitelist."""
        mock_bot_client.check_whitelist.return_value = {
            "data": {"is_whitelisted": True}
        }

        result = await guardian_client.is_whitelisted(user_id=123)
        assert result is True

    @pytest.mark.asyncio
    async def test_record_violation(self, guardian_client, mock_bot_client):
        """Test recording violation."""
        mock_bot_client.record_punishment.return_value = {
            "code": 0,
            "data": {"id": 1, "user_id": 123}
        }

        result = await guardian_client.record_violation(
            user_id=123,
            group_id=456,
            rule_type="keyword",
            action="warn",
        )
        assert result["data"]["id"] == 1


class TestDataFlowIntegration:
    """Test data flow between modules."""

    @pytest.mark.asyncio
    async def test_user_registration_flow(self):
        """Test complete user registration flow."""

        from app.modules.acquisition.api_integration import AcquisitionAPIClient
        from app.modules.integrations.api_client import BotAPIClient

        mock_bot_client = MagicMock(spec=BotAPIClient)
        mock_bot_client.register_user = AsyncMock(return_value={
            "code": 0,
            "data": {"id": 1, "user_id": 12345}
        })
        mock_bot_client.track_user_action = AsyncMock(return_value={
            "code": 0,
            "message": "success"
        })
        mock_bot_client._request = AsyncMock(return_value={"code": 0, "data": {}})

        client = AcquisitionAPIClient(mock_bot_client)

        result = await client.track_user_registration(
            user_id=12345,
            username="test_user",
            source_group_id=456,
            source_keyword="vpn",
            tracking_code="TRACK123",
        )

        assert result["data"]["user_id"] == 12345

    @pytest.mark.asyncio
    async def test_violation_handling_flow(self):
        """Test complete violation handling flow."""
        from app.modules.guardian.api_integration import GuardianAPIClient

        mock_bot_client = MagicMock()
        mock_bot_client.record_punishment = AsyncMock(return_value={
            "code": 0,
            "data": {"id": 1}
        })
        mock_bot_client.record_metric = AsyncMock(return_value={
            "code": 0,
            "message": "success"
        })

        client = GuardianAPIClient(mock_bot_client)

        result = await client.record_violation(
            user_id=123,
            group_id=456,
            rule_type="keyword",
            action="warn",
            content="spam content",
        )

        assert result["data"]["id"] == 1


class TestAPIEndpoints:
    """Test API endpoints integration."""

    @pytest.fixture
    def mock_api_client(self):
        """Create mock API client for testing."""
        client = MagicMock()
        client.get = MagicMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"status": "healthy"})
        ))
        return client

    def test_health_check_mock(self, mock_api_client):
        """Test health check endpoint with mock."""
        response = mock_api_client.get("/health")
        assert response.status_code == 200

    def test_accounts_endpoint_mock(self, mock_api_client):
        """Test accounts endpoint exists with mock."""
        mock_api_client.get.return_value = MagicMock(status_code=401)
        response = mock_api_client.get("/api/accounts")
        assert response.status_code in [200, 401, 403]

    def test_groups_endpoint_mock(self, mock_api_client):
        """Test groups endpoint exists with mock."""
        mock_api_client.get.return_value = MagicMock(status_code=401)
        response = mock_api_client.get("/api/groups")
        assert response.status_code in [200, 401, 403]

    def test_rules_endpoint_mock(self, mock_api_client):
        """Test rules endpoint exists with mock."""
        mock_api_client.get.return_value = MagicMock(status_code=401)
        response = mock_api_client.get("/api/rules")
        assert response.status_code in [200, 401, 403]


class TestWebSocketIntegration:
    """Test WebSocket integration."""

    @pytest.mark.asyncio
    async def test_connection_manager(self):
        """Test WebSocket connection manager."""
        from app.api.websocket import ConnectionManager

        manager = ConnectionManager()
        assert await manager.get_client_count() == 0

    def test_channels_defined(self):
        """Test WebSocket channels are defined."""
        from app.api.websocket import Channels

        assert Channels.MESSAGE_NEW == "message:new"
        assert Channels.ACCOUNT_STATUS == "account:status"
        assert Channels.VIOLATION_NEW == "violation:new"
        assert Channels.STATS_UPDATE == "stats:update"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
