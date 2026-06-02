"""
Proxy Pool Module

Manages proxy IPs for Telegram account protection.

Features:
- Proxy CRUD operations
- Proxy health checking
- Automatic failover
- Account-proxy binding
- Country-based proxy matching
- Provider API auto-discovery
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp
import structlog
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import Proxy, ProxyType, TelegramAccount
from app.core.exceptions import ProxyNotFoundError

logger = structlog.get_logger()


@dataclass
class ProxyConfig:
    """
    Runtime proxy configuration.

    Attributes:
        proxy_id: Database ID
        proxy_type: Type of proxy
        host: Proxy host
        port: Proxy port
        protocol: Protocol (http/socks5)
        username: Auth username (optional)
        password: Auth password (optional)
        country: Country code (ISO 3166-1 alpha-2)
    """

    proxy_id: int
    proxy_type: ProxyType
    host: str
    port: int
    country: str
    protocol: str = "http"
    username: Optional[str] = None
    password: Optional[str] = None

    def to_url(self) -> str:
        """Convert to proxy URL."""
        if self.username and self.password:
            return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"


@dataclass
class ProxyHealth:
    """
    Proxy health metrics.

    Attributes:
        proxy_id: Proxy ID
        is_active: Whether proxy is active
        success_rate: Success rate (0-1)
        avg_latency: Average latency in ms
        last_checked: Last check time
        consecutive_failures: Consecutive failures
    """

    proxy_id: int
    is_active: bool = True
    success_rate: float = 1.0
    avg_latency: int = 0
    last_checked: Optional[datetime] = None
    consecutive_failures: int = 0


class ProxyPool:
    """
    Proxy pool manager for managing proxy IPs.

    Features:
    - Add/remove proxies
    - Health checking
    - Automatic failover
    - Account-proxy binding
    - Country-based matching
    - Provider API integration
    """

    def __init__(
        self,
        db: AsyncSession,
        health_check_url: Optional[str] = None,
    ):
        """
        Initialize ProxyPool.

        Args:
            db: SQLAlchemy async session
            health_check_url: URL for health check (env: HEALTH_CHECK_URL or default httpbin.org)
        """
        self.db = db
        self._proxies: dict[int, ProxyConfig] = {}
        self._health: dict[int, ProxyHealth] = {}
        self._account_bindings: dict[int, int] = {}
        self._lock = asyncio.Lock()
        self._health_check_task: Optional[asyncio.Task] = None
        self.health_check_url = (
            health_check_url
            or os.getenv("HEALTH_CHECK_URL", "https://httpbin.org/ip")
        )
        self.logger = logger.bind(module="proxy_pool")

    async def sync_from_db(self, proxies: Optional[list[Proxy]] = None) -> int:
        """Load proxy runtime state from database records."""
        proxy_rows = proxies if proxies is not None else await self.list_proxies()
        self._proxies = {}
        self._health = {}

        for proxy in proxy_rows:
            self._proxies[proxy.id] = ProxyConfig(
                proxy_id=proxy.id,
                proxy_type=proxy.proxy_type,
                host=proxy.host,
                port=proxy.port,
                country=proxy.country,
                protocol=proxy.protocol,
                username=proxy.username,
                password=proxy.password,
            )
            self._health[proxy.id] = ProxyHealth(
                proxy_id=proxy.id,
                is_active=proxy.is_active,
                success_rate=float(proxy.success_rate or 0),
                avg_latency=proxy.avg_latency or 0,
                last_checked=proxy.last_checked,
                consecutive_failures=proxy.consecutive_failures or 0,
            )

        self.logger.info("proxies_synced_from_db", count=len(proxy_rows))
        return len(proxy_rows)

    async def health_check_all(self) -> dict:
        """Run health check for all known proxies and return summary."""
        await self.sync_from_db()
        results = await self.health_check()
        healthy = sum(1 for item in results.values() if item.get("success"))
        unhealthy = len(results) - healthy
        return {
            "checked": len(results),
            "healthy": healthy,
            "unhealthy": unhealthy,
            "results": results,
        }

    async def add_proxy(
        self,
        proxy_type: ProxyType,
        host: str,
        port: int,
        country: str,
        protocol: str = "http",
        username: Optional[str] = None,
        password: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Proxy:
        """
        Add a new proxy.

        Args:
            proxy_type: Type of proxy
            host: Proxy host
            port: Proxy port
            country: Country code (ISO 3166-1 alpha-2)
            protocol: Protocol (http/socks5)
            username: Auth username
            password: Auth password
            provider: Source provider name

        Returns:
            Created Proxy
        """
        proxy = Proxy(
            proxy_type=proxy_type,
            host=host,
            port=port,
            country=country.upper(),
            protocol=protocol,
            username=username,
            password=password,
            provider=provider,
            is_active=True,
            success_rate=1.0,
        )

        self.db.add(proxy)
        await self.db.commit()
        await self.db.refresh(proxy)

        self._proxies[proxy.id] = ProxyConfig(
            proxy_id=proxy.id,
            proxy_type=proxy_type,
            host=host,
            port=port,
            country=country.upper(),
            protocol=protocol,
            username=username,
            password=password,
        )

        self._health[proxy.id] = ProxyHealth(proxy_id=proxy.id)

        self.logger.info(
            "proxy_added",
            proxy_id=proxy.id,
            host=host,
            country=country,
            proxy_type=proxy_type.value,
        )

        return proxy

    async def get_proxy(self, proxy_id: int) -> Optional[ProxyConfig]:
        """
        Get proxy configuration.

        Args:
            proxy_id: Proxy database ID

        Returns:
            ProxyConfig or None
        """
        return self._proxies.get(proxy_id)

    async def list_proxies(
        self,
        proxy_type: Optional[ProxyType] = None,
        country: Optional[str] = None,
        active_only: bool = False,
    ) -> list[Proxy]:
        """
        List proxies from database.

        Args:
            proxy_type: Filter by type
            country: Filter by country code
            active_only: Only return active proxies

        Returns:
            List of Proxy
        """
        query = select(Proxy)

        if proxy_type:
            query = query.where(Proxy.proxy_type == proxy_type)
        if country:
            query = query.where(Proxy.country == country.upper())
        if active_only:
            query = query.where(Proxy.is_active == True)

        query = query.order_by(Proxy.success_rate.desc())

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def delete_proxy(self, proxy_id: int) -> bool:
        """
        Delete a proxy.

        Args:
            proxy_id: Proxy ID

        Returns:
            True if deleted
        """
        proxy = await self.db.execute(
            select(Proxy).where(Proxy.id == proxy_id)
        )
        proxy_obj = proxy.scalar_one_or_none()

        if not proxy_obj:
            return False

        await self.db.execute(delete(Proxy).where(Proxy.id == proxy_id))
        await self.db.commit()

        if proxy_id in self._proxies:
            del self._proxies[proxy_id]
        if proxy_id in self._health:
            del self._health[proxy_id]

        self.logger.info("proxy_deleted", proxy_id=proxy_id)

        return True

    async def bind_to_account(self, account_id: int, proxy_id: int) -> None:
        """
        Bind proxy to account.

        Args:
            account_id: Account database ID
            proxy_id: Proxy database ID
        """
        async with self._lock:
            self._account_bindings[account_id] = proxy_id

        self.logger.info(
            "proxy_bound",
            account_id=account_id,
            proxy_id=proxy_id,
        )

    async def unbind_account(self, account_id: int) -> Optional[int]:
        """
        Unbind proxy from account.

        Args:
            account_id: Account database ID

        Returns:
            Previous proxy ID or None
        """
        async with self._lock:
            return self._account_bindings.pop(account_id, None)

    async def get_account_proxy(self, account_id: int) -> Optional[ProxyConfig]:
        """
        Get proxy bound to account.

        Args:
            account_id: Account database ID

        Returns:
            ProxyConfig or None
        """
        proxy_id = self._account_bindings.get(account_id)
        if proxy_id:
            return await self.get_proxy(proxy_id)
        return None

    async def get_available_proxy(
        self,
        account_id: Optional[int] = None,
        proxy_type: Optional[ProxyType] = None,
    ) -> Optional[ProxyConfig]:
        """
        Get an available proxy, optionally matched by country.

        Args:
            account_id: Account ID to match country
            proxy_type: Preferred proxy type

        Returns:
            ProxyConfig or None
        """
        async with self._lock:
            available = [
                p for p in self._proxies.values()
                if self._health.get(p.proxy_id, ProxyHealth(p.proxy_id)).is_active
                and self._health.get(p.proxy_id, ProxyHealth(p.proxy_id)).success_rate >= 0.8
                and p.proxy_id not in self._account_bindings.values()
            ]

            # Country matching
            if account_id:
                account = await self.db.get(TelegramAccount, account_id)
                if account and account.country_match_enabled:
                    target_country = (
                        account.preferred_country
                        if account.preferred_country
                        else account.country_code
                    )
                    available = [p for p in available if p.country == target_country]
            elif proxy_type:
                available = [p for p in available if p.proxy_type == proxy_type]

            if not available:
                return None

            available.sort(
                key=lambda p: (
                    -self._health.get(p.proxy_id, ProxyHealth(p.proxy_id)).success_rate,
                    self._health.get(p.proxy_id, ProxyHealth(p.proxy_id)).avg_latency,
                )
            )

            return available[0]

    async def health_check(self, proxy_id: Optional[int] = None) -> dict:
        """
        Perform health check on proxies.

        Args:
            proxy_id: Specific proxy to check, or None for all

        Returns:
            Health check results
        """
        targets = [proxy_id] if proxy_id else list(self._proxies.keys())
        results = {}

        for pid in targets:
            proxy = self._proxies.get(pid)
            if not proxy:
                continue

            health = self._health.get(pid, ProxyHealth(pid))
            try:
                start_time = time.time()

                async with aiohttp.ClientSession() as session:
                    timeout = aiohttp.ClientTimeout(total=10)
                    async with session.get(
                        self.health_check_url,
                        proxy=proxy.to_url(),
                        timeout=timeout,
                    ) as resp:
                        latency = int((time.time() - start_time) * 1000)
                        is_active = resp.status == 200

                        health.last_checked = datetime.utcnow()
                        health.avg_latency = latency
                        health.is_active = is_active

                        if is_active:
                            health.success_rate = min(1.0, health.success_rate + 0.1)
                            health.consecutive_failures = 0
                        else:
                            health.consecutive_failures += 1

                        results[pid] = {
                            "success": True,
                            "latency": latency,
                            "status": resp.status,
                        }

            except Exception as e:
                health.is_active = False
                health.consecutive_failures += 1
                health.success_rate = max(0, health.success_rate - 0.2)
                health.last_checked = datetime.utcnow()

                results[pid] = {
                    "success": False,
                    "error": str(e),
                }

            self._health[pid] = health
            proxy_result = await self.db.execute(select(Proxy).where(Proxy.id == pid))
            proxy_obj = proxy_result.scalar_one_or_none()
            if proxy_obj:
                proxy_obj.is_active = health.is_active
                proxy_obj.avg_latency = health.avg_latency
                proxy_obj.success_rate = health.success_rate
                proxy_obj.last_checked = health.last_checked
                proxy_obj.consecutive_failures = health.consecutive_failures
                await self.db.commit()

        return results

    async def check_and_cleanup(self) -> dict:
        """Refresh proxy status and report inactive proxies."""
        summary = await self.health_check_all()
        inactive = [
            proxy_id for proxy_id, item in summary.get("results", {}).items() if not item.get("success")
        ]
        return {
            "total": summary.get("checked", 0),
            "removed": 0,
            "inactive": len(inactive),
            "inactive_proxy_ids": inactive,
        }

    async def validate_batch(self, proxy_ids: list[int]) -> dict:
        """Validate a list of proxies."""
        await self.sync_from_db()
        aggregated: dict[int, dict] = {}
        valid = 0
        invalid = 0

        for proxy_id in proxy_ids:
            result = await self.health_check(proxy_id=proxy_id)
            item = result.get(proxy_id)
            if item and item.get("success"):
                valid += 1
                aggregated[proxy_id] = item
            else:
                invalid += 1
                aggregated[proxy_id] = item or {"success": False, "error": "proxy_not_found"}

        return {
            "total": len(proxy_ids),
            "valid": valid,
            "invalid": invalid,
            "results": aggregated,
        }

    async def on_proxy_failure(self, proxy_id: int) -> None:
        """
        Handle proxy failure.

        Args:
            proxy_id: Failed proxy ID
        """
        health = self._health.get(proxy_id)
        if not health:
            return

        health.consecutive_failures += 1
        health.success_rate = max(0, health.success_rate - 0.2)

        if health.consecutive_failures >= 3:
            health.is_active = False
            self.logger.warning(
                "proxy_disabled",
                proxy_id=proxy_id,
                consecutive_failures=health.consecutive_failures,
            )

            affected_accounts = [
                acc for acc, pid in self._account_bindings.items()
                if pid == proxy_id
            ]

            for account_id in affected_accounts:
                new_proxy = await self.get_available_proxy(account_id=account_id)
                if new_proxy:
                    await self.bind_to_account(account_id, new_proxy.proxy_id)
                    self.logger.info(
                        "proxy_switched",
                        account_id=account_id,
                        old_proxy=proxy_id,
                        new_proxy=new_proxy.proxy_id,
                    )

    async def start_health_checker(self, interval_seconds: int = 300) -> None:
        """
        Start background health checker.

        Args:
            interval_seconds: Check interval
        """
        async def _health_check_loop():
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self.health_check()
                except Exception as e:
                    self.logger.error("health_check_error", error=str(e))

        self._health_check_task = asyncio.create_task(_health_check_loop())
        self.logger.info("health_checker_started", interval=interval_seconds)

    async def stop_health_checker(self) -> None:
        """Stop background health checker."""
        if self._health_check_task:
            self._health_check_task.cancel()
            self._health_check_task = None
            self.logger.info("health_checker_stopped")

    async def get_statistics(self) -> dict:
        """
        Get proxy pool statistics.

        Returns:
            Statistics dictionary
        """
        result = await self.db.execute(
            select(
                func.count(Proxy.id).label("total"),
                func.avg(Proxy.success_rate).label("avg_success_rate"),
                func.avg(Proxy.avg_latency).label("avg_latency"),
            )
        )
        row = result.one()

        type_counts = {}
        for ptype in ProxyType:
            count_result = await self.db.execute(
                select(func.count(Proxy.id)).where(Proxy.proxy_type == ptype)
            )
            type_counts[ptype.value] = count_result.scalar()

        return {
            "total_proxies": row.total or 0,
            "average_success_rate": float(row.avg_success_rate or 0),
            "average_latency": int(row.avg_latency or 0),
            "by_type": type_counts,
            "account_bindings": len(self._account_bindings),
        }
