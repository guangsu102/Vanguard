"""单元测试 - Redis 缓存"""
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core.cache import RedisClient


class TestRedisClient:
    """测试 Redis 客户端"""

    def setup_method(self):
        self.redis = RedisClient(
            host="localhost",
            port=6379,
            password=None,
            db=0
        )
        # Mock 客户端
        self.mock_client = AsyncMock()
        self.redis._client = self.mock_client

    def test_client_property_unconnected(self):
        """测试未连接时访问 client 属性"""
        self.redis._client = None
        with pytest.raises(RuntimeError, match="未连接"):
            _ = self.redis.client

    def test_client_property_connected(self):
        """测试已连接时访问 client 属性"""
        self.redis._client = self.mock_client
        assert self.redis.client == self.mock_client

    @pytest.mark.asyncio
    async def test_get(self):
        """测试 get 方法"""
        self.mock_client.get = AsyncMock(return_value="test_value")
        result = await self.redis.get("test_key")
        assert result == "test_value"

    @pytest.mark.asyncio
    async def test_set(self):
        """测试 set 方法"""
        self.mock_client.set = AsyncMock(return_value=True)
        result = await self.redis.set("test_key", "test_value")
        assert result is True

    @pytest.mark.asyncio
    async def test_set_with_expiry(self):
        """测试带过期时间的 set"""
        self.mock_client.set = AsyncMock(return_value=True)
        result = await self.redis.set("test_key", "test_value", ex=300)
        assert result is True

    @pytest.mark.asyncio
    async def test_setex(self):
        """测试 setex 方法"""
        self.mock_client.setex = AsyncMock(return_value=True)
        result = await self.redis.setex("test_key", 300, "test_value")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete(self):
        """测试 delete 方法"""
        self.mock_client.delete = AsyncMock(return_value=1)
        result = await self.redis.delete("test_key")
        assert result == 1

    @pytest.mark.asyncio
    async def test_exists(self):
        """测试 exists 方法"""
        self.mock_client.exists = AsyncMock(return_value=1)
        result = await self.redis.exists("test_key")
        assert result is True

    @pytest.mark.asyncio
    async def test_incr(self):
        """测试 incr 方法"""
        self.mock_client.incr = AsyncMock(return_value=5)
        result = await self.redis.incr("counter")
        assert result == 5

    @pytest.mark.asyncio
    async def test_expire(self):
        """测试 expire 方法"""
        self.mock_client.expire = AsyncMock(return_value=True)
        result = await self.redis.expire("test_key", 300)
        assert result is True

    @pytest.mark.asyncio
    async def test_lock_acquire(self):
        """测试获取分布式锁"""
        mock_lock = MagicMock()
        mock_lock.acquire = AsyncMock(return_value=True)
        self.mock_client.lock = MagicMock(return_value=mock_lock)

        result = await self.redis.lock("test_lock")
        assert result is not None

    @pytest.mark.asyncio
    async def test_lock_acquire_failed(self):
        """测试获取分布式锁失败"""
        mock_lock = MagicMock()
        mock_lock.acquire = AsyncMock(return_value=False)
        self.mock_client.lock = MagicMock(return_value=mock_lock)

        result = await self.redis.lock("test_lock", blocking_timeout=0.1)
        assert result is None
