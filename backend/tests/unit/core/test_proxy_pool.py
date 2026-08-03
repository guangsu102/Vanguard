"""
Unit Tests for Proxy Pool Module

Tests cover:
- Proxy CRUD operations
- Proxy health checking
- Account-proxy binding
- Country-based proxy matching
- Statistics
"""

import pytest
import pytest_asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.network import proxy_pool as proxy_pool_module
from app.core.network.proxy_pool import ProxyPool, ProxyConfig, ProxyHealth
from app.core.account.models import ProxyType, TelegramAccount


class TestProxyConfig:
    """Test ProxyConfig dataclass."""

    def test_to_url_without_auth(self):
        """Test URL generation without authentication."""
        config = ProxyConfig(
            proxy_id=1,
            proxy_type=ProxyType.DATACENTER,
            host="192.168.1.1",
            port=8080,
            country="US",
            protocol="http",
        )
        assert config.to_url() == "http://192.168.1.1:8080"

    def test_to_url_with_auth(self):
        """Test URL generation with authentication."""
        config = ProxyConfig(
            proxy_id=1,
            proxy_type=ProxyType.RESIDENTIAL,
            host="192.168.1.1",
            port=8080,
            country="US",
            protocol="socks5",
            username="user",
            password="pass",
        )
        assert config.to_url() == "socks5://user:pass@192.168.1.1:8080"

    def test_proxy_config_defaults(self):
        """Test ProxyConfig default values."""
        config = ProxyConfig(
            proxy_id=1,
            proxy_type=ProxyType.DATACENTER,
            host="192.168.1.1",
            port=8080,
            country="CN",
        )
        assert config.protocol == "http"
        assert config.username is None
        assert config.password is None


class TestProxyHealth:
    """Test ProxyHealth dataclass."""

    def test_default_health(self):
        """Test default health values."""
        health = ProxyHealth(proxy_id=1)
        assert health.proxy_id == 1
        assert health.is_active is True
        assert health.success_rate == 1.0
        assert health.avg_latency == 0
        assert health.last_checked is None
        assert health.consecutive_failures == 0

    def test_custom_health(self):
        """Test custom health values."""
        health = ProxyHealth(
            proxy_id=1,
            is_active=False,
            success_rate=0.75,
            avg_latency=150,
            consecutive_failures=2,
        )
        assert health.is_active is False
        assert health.success_rate == 0.75
        assert health.avg_latency == 150
        assert health.consecutive_failures == 2


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeSession:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, *args, **kwargs):
        return _FakeResponse(self.status)


class TestProxyHealthCheck:
    def test_default_health_check_uses_ipify(self, test_db):
        pool = ProxyPool(test_db)

        assert pool.health_check_url == "https://api.ipify.org?format=json"

    @pytest.mark.asyncio
    async def test_non_200_response_is_reported_as_failure(self, test_db, monkeypatch):
        pool = ProxyPool(test_db)
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER,
            "1.1.1.1",
            8080,
            "US",
        )
        monkeypatch.setattr(
            proxy_pool_module.aiohttp,
            "ClientSession",
            lambda *args, **kwargs: _FakeSession(503),
        )

        result = await pool.health_check(proxy.id)
        await test_db.refresh(proxy)

        assert result[proxy.id]["success"] is False
        assert result[proxy.id]["status"] == 503
        assert proxy.is_active is False
        assert proxy.consecutive_failures == 1
        assert proxy.success_rate == pytest.approx(0.8)


class TestProxyPoolAddRemove:
    """Test proxy add/remove operations."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        return ProxyPool(test_db)

    @pytest.mark.asyncio
    async def test_add_proxy(self, pool, test_db):
        """Test adding a new proxy."""
        proxy = await pool.add_proxy(
            proxy_type=ProxyType.DATACENTER,
            host="192.168.1.1",
            port=8080,
            country="US",
            protocol="http",
        )

        assert proxy.id is not None
        assert proxy.host == "192.168.1.1"
        assert proxy.port == 8080
        assert proxy.country == "US"
        assert proxy.protocol == "http"
        assert proxy.is_active is True
        assert proxy.success_rate == 1.0

    @pytest.mark.asyncio
    async def test_add_proxy_with_auth(self, pool, test_db):
        """Test adding proxy with authentication."""
        proxy = await pool.add_proxy(
            proxy_type=ProxyType.RESIDENTIAL,
            host="192.168.1.1",
            port=8080,
            country="CN",
            username="myuser",
            password="mypass",
        )

        assert proxy.username == "myuser"
        assert proxy.password == "mypass"

    @pytest.mark.asyncio
    async def test_add_proxy_country_uppercase(self, pool, test_db):
        """Test country code is converted to uppercase."""
        proxy = await pool.add_proxy(
            proxy_type=ProxyType.DATACENTER,
            host="192.168.1.1",
            port=8080,
            country="us",
        )

        assert proxy.country == "US"

    @pytest.mark.asyncio
    async def test_get_proxy(self, pool, test_db):
        """Test getting proxy configuration."""
        created = await pool.add_proxy(
            proxy_type=ProxyType.DATACENTER,
            host="192.168.1.1",
            port=8080,
            country="US",
        )

        config = await pool.get_proxy(created.id)

        assert config is not None
        assert config.proxy_id == created.id
        assert config.host == "192.168.1.1"
        assert config.port == 8080

    @pytest.mark.asyncio
    async def test_get_nonexistent_proxy(self, pool):
        """Test getting non-existent proxy returns None."""
        config = await pool.get_proxy(99999)
        assert config is None

    @pytest.mark.asyncio
    async def test_delete_proxy(self, pool, test_db):
        """Test deleting a proxy."""
        created = await pool.add_proxy(
            proxy_type=ProxyType.DATACENTER,
            host="192.168.1.1",
            port=8080,
            country="US",
        )
        proxy_id = created.id

        result = await pool.delete_proxy(proxy_id)

        assert result is True
        assert await pool.get_proxy(proxy_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_proxy(self, pool):
        """Test deleting non-existent proxy returns False."""
        result = await pool.delete_proxy(99999)
        assert result is False


class TestProxyListing:
    """Test proxy listing and filtering."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        p = ProxyPool(test_db)
        await p.add_proxy(ProxyType.DATACENTER, "1.1.1.1", 8080, "US")
        await p.add_proxy(ProxyType.RESIDENTIAL, "2.2.2.2", 8080, "CN")
        await p.add_proxy(ProxyType.MOBILE, "3.3.3.3", 8080, "US")
        return p

    @pytest.mark.asyncio
    async def test_list_all_proxies(self, pool):
        """Test listing all proxies."""
        proxies = await pool.list_proxies()
        assert len(proxies) >= 3

    @pytest.mark.asyncio
    async def test_list_by_type(self, pool):
        """Test listing proxies by type."""
        proxies = await pool.list_proxies(proxy_type=ProxyType.DATACENTER)
        assert all(p.proxy_type == ProxyType.DATACENTER for p in proxies)

    @pytest.mark.asyncio
    async def test_list_by_country(self, pool):
        """Test listing proxies by country."""
        proxies = await pool.list_proxies(country="US")
        assert all(p.country == "US" for p in proxies)

    @pytest.mark.asyncio
    async def test_list_active_only(self, pool, test_db):
        """Test listing only active proxies."""
        proxies = await pool.list_proxies(active_only=True)
        assert all(p.is_active for p in proxies)


class TestProxyBinding:
    """Test proxy-account binding."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        return ProxyPool(test_db)

    @pytest.mark.asyncio
    async def test_bind_to_account(self, pool, test_db):
        """Test binding proxy to account."""
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER,
            "192.168.1.1",
            8080,
            "US",
        )

        await pool.bind_to_account(account_id=123, proxy_id=proxy.id)

        bound = await pool.get_account_proxy(123)
        assert bound is not None
        assert bound.proxy_id == proxy.id

    @pytest.mark.asyncio
    async def test_unbind_account(self, pool, test_db):
        """Test unbinding proxy from account."""
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER,
            "192.168.1.1",
            8080,
            "US",
        )

        await pool.bind_to_account(account_id=123, proxy_id=proxy.id)
        result = await pool.unbind_account(123)

        assert result == proxy.id
        assert await pool.get_account_proxy(123) is None

    @pytest.mark.asyncio
    async def test_unbind_nonexistent_account(self, pool):
        """Test unbinding non-existent account returns None."""
        result = await pool.unbind_account(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_rebind_account(self, pool, test_db):
        """Test rebinding account to different proxy."""
        proxy1 = await pool.add_proxy(
            ProxyType.DATACENTER, "1.1.1.1", 8080, "US"
        )
        proxy2 = await pool.add_proxy(
            ProxyType.RESIDENTIAL, "2.2.2.2", 8080, "US"
        )

        await pool.bind_to_account(account_id=123, proxy_id=proxy1.id)
        await pool.bind_to_account(account_id=123, proxy_id=proxy2.id)

        bound = await pool.get_account_proxy(123)
        assert bound.proxy_id == proxy2.id


class TestAvailableProxy:
    """Test available proxy selection."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        p = ProxyPool(test_db)
        await p.add_proxy(ProxyType.DATACENTER, "1.1.1.1", 8080, "US")
        await p.add_proxy(ProxyType.RESIDENTIAL, "2.2.2.2", 8080, "CN")
        await p.add_proxy(ProxyType.MOBILE, "3.3.3.3", 8080, "US")
        return p

    @pytest.mark.asyncio
    async def test_get_available_proxy(self, pool):
        """Test getting any available proxy."""
        proxy = await pool.get_available_proxy()
        assert proxy is not None

    @pytest.mark.asyncio
    async def test_get_available_proxy_by_type(self, pool):
        """Test getting available proxy by type."""
        proxy = await pool.get_available_proxy(proxy_type=ProxyType.DATACENTER)
        assert proxy is not None
        assert proxy.proxy_type == ProxyType.DATACENTER

    @pytest.mark.asyncio
    async def test_bound_proxy_not_available(self, pool, test_db):
        """Test that bound proxies are not returned."""
        proxy = await pool.get_available_proxy()
        await pool.bind_to_account(account_id=999, proxy_id=proxy.proxy_id)

        # Get another available proxy - should be different
        proxy2 = await pool.get_available_proxy()
        assert proxy2.proxy_id != proxy.proxy_id

    @pytest.mark.asyncio
    async def test_country_matching(self, pool, test_db):
        """Test country-based proxy matching."""
        # Create account with US country
        account = TelegramAccount(
            phone="+1234567890",
            identifier="+1234567890",
            api_config_name="default",
            session_name="test_session",
            country_code="US",
            country_match_enabled=True,
        )
        test_db.add(account)
        await test_db.commit()
        await test_db.refresh(account)

        proxy = await pool.get_available_proxy(account_id=account.id)

        assert proxy is not None
        assert proxy.country == "US"


class TestProxyFailure:
    """Test proxy failure handling."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        return ProxyPool(test_db)

    @pytest.mark.asyncio
    async def test_on_proxy_failure_decreases_rate(self, pool, test_db):
        """Test failure decreases success rate."""
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER, "1.1.1.1", 8080, "US"
        )

        initial_rate = pool._health[proxy.id].success_rate
        await pool.on_proxy_failure(proxy.id)

        assert pool._health[proxy.id].success_rate < initial_rate

    @pytest.mark.asyncio
    async def test_on_proxy_failure_increments_consecutive(self, pool, test_db):
        """Test failure increments consecutive failures."""
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER, "1.1.1.1", 8080, "US"
        )

        await pool.on_proxy_failure(proxy.id)

        assert pool._health[proxy.id].consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_proxy_disabled_after_3_failures(self, pool, test_db):
        """Test proxy is disabled after 3 consecutive failures."""
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER, "1.1.1.1", 8080, "US"
        )

        for _ in range(3):
            await pool.on_proxy_failure(proxy.id)

        assert pool._health[proxy.id].is_active is False


class TestHealthChecker:
    """Test background health checker."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        return ProxyPool(test_db)

    @pytest.mark.asyncio
    async def test_start_health_checker(self, pool):
        """Test starting background health checker."""
        await pool.start_health_checker(interval_seconds=1)
        assert pool._health_check_task is not None
        await pool.stop_health_checker()

    @pytest.mark.asyncio
    async def test_stop_health_checker(self, pool):
        """Test stopping background health checker."""
        await pool.start_health_checker()
        await pool.stop_health_checker()
        assert pool._health_check_task is None


class TestStatistics:
    """Test proxy pool statistics."""

    @pytest_asyncio.fixture
    async def pool(self, test_db: AsyncSession):
        """Create ProxyPool with test database."""
        p = ProxyPool(test_db)
        await p.add_proxy(ProxyType.DATACENTER, "1.1.1.1", 8080, "US")
        await p.add_proxy(ProxyType.RESIDENTIAL, "2.2.2.2", 8080, "CN")
        return p

    @pytest.mark.asyncio
    async def test_get_statistics(self, pool):
        """Test getting proxy pool statistics."""
        stats = await pool.get_statistics()

        assert "total_proxies" in stats
        assert "average_success_rate" in stats
        assert "average_latency" in stats
        assert "by_type" in stats
        assert stats["total_proxies"] >= 2

    @pytest.mark.asyncio
    async def test_statistics_by_type(self, pool):
        """Test statistics breakdown by type."""
        stats = await pool.get_statistics()

        assert "datacenter" in stats["by_type"]
        assert "residential" in stats["by_type"]
