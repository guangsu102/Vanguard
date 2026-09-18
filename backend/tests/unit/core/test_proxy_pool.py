"""
Unit Tests for Proxy Pool Module

Tests cover:
- Proxy CRUD operations
- Proxy health checking
- Account-proxy binding
- Country-based proxy matching
- Statistics
"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import Proxy, ProxyType, TelegramAccount
from app.core.account.proxy_resolver import MAX_STATIC_PROXY_BINDINGS
from app.core.network import proxy_pool as proxy_pool_module
from app.core.network.proxy_pool import ProxyConfig, ProxyHealth, ProxyPool


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
        assert proxy.is_active is True
        assert pool._health[proxy.id].is_active is True
        assert proxy.consecutive_failures == 1
        assert proxy.success_rate == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_repeated_failures_become_error_and_success_recovers(self, test_db, monkeypatch):
        pool = ProxyPool(test_db)
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER,
            "1.1.1.2",
            8080,
            "US",
        )
        response_status = {"value": 503}
        monkeypatch.setattr(
            proxy_pool_module.aiohttp,
            "ClientSession",
            lambda *args, **kwargs: _FakeSession(response_status["value"]),
        )

        for _ in range(3):
            await pool.health_check(proxy.id)
        await test_db.refresh(proxy)

        assert proxy.is_active is True
        assert proxy.consecutive_failures == 3
        assert pool._health[proxy.id].is_active is False

        response_status["value"] = 200
        result = await pool.health_check(proxy.id)
        await test_db.refresh(proxy)

        assert result[proxy.id]["success"] is True
        assert proxy.is_active is True
        assert proxy.consecutive_failures == 0
        assert pool._health[proxy.id].is_active is True
        assert (await pool.get_available_proxy()).proxy_id == proxy.id

    @pytest.mark.asyncio
    async def test_sync_marks_repeatedly_failing_enabled_proxy_unhealthy(self, test_db):
        proxy = Proxy(
            proxy_type=ProxyType.DATACENTER,
            host="1.1.1.3",
            port=8080,
            protocol="http",
            country="US",
            is_active=True,
            success_rate=1.0,
            consecutive_failures=3,
        )
        test_db.add(proxy)
        await test_db.commit()
        await test_db.refresh(proxy)

        pool = ProxyPool(test_db)
        await pool.sync_from_db()

        assert pool._proxies[proxy.id].is_enabled is True
        assert pool._health[proxy.id].is_active is False
        assert await pool.get_available_proxy() is None

    @pytest.mark.asyncio
    async def test_sync_keeps_manually_disabled_proxy_out_of_selection(self, test_db):
        proxy = Proxy(
            proxy_type=ProxyType.DATACENTER,
            host="1.1.1.4",
            port=8080,
            protocol="http",
            country="US",
            is_active=False,
            success_rate=1.0,
            consecutive_failures=0,
        )
        test_db.add(proxy)
        await test_db.commit()
        await test_db.refresh(proxy)

        pool = ProxyPool(test_db)
        await pool.sync_from_db()

        assert pool._proxies[proxy.id].is_enabled is False
        assert pool._health[proxy.id].is_active is True
        assert await pool.get_available_proxy() is None

    @pytest.mark.asyncio
    async def test_health_check_all_excludes_inactive_proxies(self, test_db):
        pool = ProxyPool(test_db)
        active = await pool.add_proxy(
            ProxyType.DATACENTER,
            "1.1.1.1",
            8080,
            "US",
        )
        inactive = await pool.add_proxy(
            ProxyType.DATACENTER,
            "2.2.2.2",
            8080,
            "US",
        )
        inactive.is_active = False
        inactive.consecutive_failures = 1328
        await test_db.commit()
        pool.health_check = AsyncMock(
            return_value={active.id: {"success": True, "latency": 10}}
        )

        result = await pool.health_check_all()

        assert result["checked"] == 1
        assert result["healthy"] == 1
        assert result["unhealthy"] == 0
        assert set(pool._proxies) == {active.id}


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
    async def test_allows_max_bindings_and_rejects_one_more(self, pool, test_db):
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER,
            "192.168.1.2",
            8080,
            "US",
        )

        assert MAX_STATIC_PROXY_BINDINGS == 6
        for account_id in range(1, MAX_STATIC_PROXY_BINDINGS + 1):
            await pool.bind_to_account(account_id=account_id, proxy_id=proxy.id)

        with pytest.raises(ValueError, match="already has 6 bound accounts"):
            await pool.bind_to_account(
                account_id=MAX_STATIC_PROXY_BINDINGS + 1,
                proxy_id=proxy.id,
            )

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
    async def test_selection_prefers_less_loaded_proxy(self, pool, test_db):
        """Prefer a less-loaded proxy while allowing reuse below capacity."""
        proxy = await pool.get_available_proxy()
        await pool.bind_to_account(account_id=999, proxy_id=proxy.proxy_id)

        # With equal health, selection spreads accounts across less-loaded proxies.
        proxy2 = await pool.get_available_proxy()
        assert proxy2.proxy_id != proxy.proxy_id

    @pytest.mark.asyncio
    async def test_single_proxy_remains_available_until_twentieth_binding(self, test_db):
        pool = ProxyPool(test_db)
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER,
            "4.4.4.4",
            8080,
            "US",
        )

        for account_id in range(1, MAX_STATIC_PROXY_BINDINGS):
            await pool.bind_to_account(account_id=account_id, proxy_id=proxy.id)

        assert (await pool.get_available_proxy()).proxy_id == proxy.id
        await pool.bind_to_account(
            account_id=MAX_STATIC_PROXY_BINDINGS,
            proxy_id=proxy.id,
        )
        assert await pool.get_available_proxy() is None

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
    async def test_proxy_marked_unhealthy_after_3_failures_without_manual_disable(self, pool, test_db):
        """Repeated failures affect health without changing the manual switch."""
        proxy = await pool.add_proxy(
            ProxyType.DATACENTER, "1.1.1.1", 8080, "US"
        )

        for _ in range(3):
            await pool.on_proxy_failure(proxy.id)

        assert pool._health[proxy.id].is_active is False
        assert proxy.is_active is True


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
