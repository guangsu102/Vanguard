"""
Proxy Provider Module

Automatic proxy discovery from third-party proxy provider APIs.

Supported providers:
- ProxyScrape
- SmartProxy
- Custom providers
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp
import structlog

from app.core.account.models import ProxyType
from app.core.network.proxy_pool import ProxyPool

logger = structlog.get_logger()


@dataclass
class ProviderConfig:
    """Proxy provider configuration."""

    name: str
    api_url: str
    api_key: str
    countries: list[str]
    proxy_type: ProxyType
    refresh_interval: int = 3600
    enabled: bool = True


@dataclass
class ProxyProviderResult:
    """Result from fetching proxies."""

    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    country: Optional[str] = None
    protocol: str = "http"


class ProxyProvider:
    """
    Proxy provider for automatic proxy discovery.

    Fetches proxies from configured providers and syncs to the proxy pool.
    """

    def __init__(self, pool: ProxyPool):
        """
        Initialize ProxyProvider.

        Args:
            pool: ProxyPool instance to sync proxies to
        """
        self.pool = pool
        self.logger = logger.bind(module="proxy_provider")
        self._sync_task: Optional[asyncio.Task] = None

    async def fetch_proxyscrape(
        self,
        api_url: str,
        api_key: str,
        countries: list[str],
    ) -> list[ProxyProviderResult]:
        """
        Fetch proxies from ProxyScrape API.

        API: https://api.proxyscrape.com/

        Args:
            api_url: API endpoint
            api_key: API key
            countries: Target countries

        Returns:
            List of proxy results
        """
        results = []
        timeout = aiohttp.ClientTimeout(total=30)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for country in countries:
                    params = {
                        "request": "displayproxies",
                        "protocol": "http",
                        "timeout": "5000",
                        "country": country,
                        "ssl": "yes",
                        "anonymity": "all",
                    }
                    if api_key:
                        params["key"] = api_key

                    async with session.get(api_url, params=params) as resp:
                        if resp.status != 200:
                            self.logger.warning(
                                "proxyscrape_fetch_failed",
                                country=country,
                                status=resp.status,
                            )
                            continue

                        text = await resp.text()
                        proxies = self._parse_proxy_list(text)

                        for proxy in proxies:
                            proxy.country = country
                            results.append(proxy)

                        self.logger.info(
                            "proxyscrape_fetched",
                            country=country,
                            count=len(proxies),
                        )

        except Exception as e:
            self.logger.error("proxyscrape_error", error=str(e))

        return results

    async def fetch_smartproxy(
        self,
        api_url: str,
        api_key: str,
        countries: list[str],
    ) -> list[ProxyProviderResult]:
        """
        Fetch proxies from SmartProxy API.

        API: https://docs.smartproxy.com/

        Args:
            api_url: API endpoint
            api_key: API key
            countries: Target countries

        Returns:
            List of proxy results
        """
        results = []
        timeout = aiohttp.ClientTimeout(total=30)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                country_param = ",".join(countries)
                params = {
                    "countries": country_param,
                    "state": "all",
                    "city": "all",
                }

                async with session.get(
                    f"{api_url}/v1/proxies",
                    headers=headers,
                    params=params,
                ) as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            "smartproxy_fetch_failed",
                            status=resp.status,
                        )
                        return results

                    data = await resp.json()
                    proxies = data.get("results", [])

                    for p in proxies:
                        results.append(
                            ProxyProviderResult(
                                host=p.get("hostname"),
                                port=p.get("port"),
                                username=p.get("username"),
                                password=p.get("password"),
                                country=p.get("country"),
                                protocol="http",
                            )
                        )

                    self.logger.info(
                        "smartproxy_fetched",
                        count=len(results),
                    )

        except Exception as e:
            self.logger.error("smartproxy_error", error=str(e))

        return results

    async def fetch_from_provider(
        self,
        config: ProviderConfig,
    ) -> list[ProxyProviderResult]:
        """
        Fetch proxies from a configured provider.

        Args:
            config: Provider configuration

        Returns:
            List of proxy results
        """
        if not config.enabled:
            return []

        provider_lower = config.name.lower()

        if "proxyscrape" in provider_lower:
            return await self.fetch_proxyscrape(
                api_url=config.api_url,
                api_key=config.api_key,
                countries=config.countries,
            )
        elif "smartproxy" in provider_lower:
            return await self.fetch_smartproxy(
                api_url=config.api_url,
                api_key=config.api_key,
                countries=config.countries,
            )
        else:
            self.logger.warning("unknown_provider", name=config.name)
            return []

    async def sync_to_pool(
        self,
        config: ProviderConfig,
        deduplicate: bool = True,
    ) -> int:
        """
        Fetch proxies from provider and add to pool.

        Args:
            config: Provider configuration
            deduplicate: Skip existing proxies by host:port

        Returns:
            Number of new proxies added
        """
        proxies = await self.fetch_from_provider(config)
        if not proxies:
            return 0

        added_count = 0

        for proxy in proxies:
            if deduplicate:
                existing = await self.pool.list_proxies(country=proxy.country)
                existing_hosts = {(p.host, p.port) for p in existing}
                if (proxy.host, proxy.port) in existing_hosts:
                    continue

            try:
                await self.pool.add_proxy(
                    proxy_type=config.proxy_type,
                    host=proxy.host,
                    port=proxy.port,
                    country=proxy.country or config.countries[0],
                    protocol=proxy.protocol,
                    username=proxy.username,
                    password=proxy.password,
                    provider=config.name,
                )
                added_count += 1
            except Exception as e:
                self.logger.warning(
                    "proxy_add_failed",
                    host=proxy.host,
                    error=str(e),
                )

        self.logger.info(
            "pool_synced",
            provider=config.name,
            total=len(proxies),
            added=added_count,
        )

        return added_count

    async def start_auto_sync(
        self,
        config: ProviderConfig,
    ) -> None:
        """
        Start automatic sync from provider.

        Args:
            config: Provider configuration
        """
        async def _sync_loop():
            while True:
                await asyncio.sleep(config.refresh_interval)
                try:
                    await self.sync_to_pool(config)
                except Exception as e:
                    self.logger.error("auto_sync_error", error=str(e))

        self._sync_task = asyncio.create_task(_sync_loop())
        self.logger.info(
            "auto_sync_started",
            provider=config.name,
            interval=config.refresh_interval,
        )

    async def stop_auto_sync(self) -> None:
        """Stop automatic sync."""
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None
            self.logger.info("auto_sync_stopped")

    def _parse_proxy_list(self, text: str) -> list[ProxyProviderResult]:
        """
        Parse proxy list from text format.

        Expected format: host:port or host:port:username:password

        Args:
            text: Raw proxy list text

        Returns:
            List of proxy results
        """
        results = []
        lines = text.strip().split("\n")

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split(":")
            if len(parts) >= 2:
                try:
                    host = parts[0]
                    port = int(parts[1])

                    if len(parts) >= 4:
                        username = parts[2]
                        password = parts[3]
                    else:
                        username = None
                        password = None

                    results.append(
                        ProxyProviderResult(
                            host=host,
                            port=port,
                            username=username,
                            password=password,
                            protocol="http",
                        )
                    )
                except ValueError:
                    continue

        return results
