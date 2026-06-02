"""集成测试 - API 客户端"""
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core.api import XBoardAPIClient


class TestXBoardAPIClient:
    """测试 XBoard API 客户端"""

    def setup_method(self):
        self.api = XBoardAPIClient(
            base_url="http://localhost:8080/api",
            api_key="test_api_key",
            timeout=30,
            retry_times=3,
            retry_delay=1
        )

    @pytest.mark.asyncio
    async def test_create_trial_user(self):
        """测试创建试用用户"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "success": True,
            "data": {
                "user_id": 1,
                "email": "test@example.com"
            }
        })

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch.object(self.api, '_client', mock_client):
            result = await self.api.create_trial_user(
                tg_uid=123456,
                username="testuser",
                validity_hours=24,
                traffic_gb=50
            )

        assert "success" in result

    @pytest.mark.asyncio
    async def test_get_user_info(self):
        """测试获取用户信息"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "success": True,
            "data": {
                "user_id": 1,
                "traffic_remaining_gb": 50
            }
        })

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch.object(self.api, '_client', mock_client):
            result = await self.api.get_user_info(tg_uid=123456)

        assert "success" in result

    @pytest.mark.asyncio
    async def test_add_traffic(self):
        """测试增加流量"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "success": True,
            "data": {"added_mb": 512}
        })

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch.object(self.api, '_client', mock_client):
            result = await self.api.add_traffic(tg_uid=123456, traffic_mb=512)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_subscription_link(self):
        """测试获取订阅链接"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "success": True,
            "data": {
                "clash": "https://example.com/clash",
                "v2ray": "https://example.com/v2ray"
            }
        })

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch.object(self.api, '_client', mock_client):
            result = await self.api.get_subscription_link(user_id=1)

        assert result["success"] is True
        assert "clash" in result.get("data", {})

    @pytest.mark.asyncio
    async def test_get_node_status(self):
        """测试获取节点状态"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "success": True,
            "data": [
                {"id": 1, "name": "Node 1", "status": "online"}
            ]
        })

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch.object(self.api, '_client', mock_client):
            result = await self.api.get_node_status()

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_api_error_handling(self):
        """测试 API 错误处理"""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_response)

        with patch.object(self.api, '_client', mock_client):
            result = await self.api.get_user_info(tg_uid=123456)

        assert result["success"] is False
