"""
Decodo Proxy Client

Client for interacting with Decodo proxy API.
Handles proxy extraction based on location (country/state/city).
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import httpx
import structlog

from app.core.account.exceptions import (
    ProxyConnectionError,
    ProxyNotFoundError,
    ProxyProviderError,
)
from app.core.config import get_settings

logger = structlog.get_logger()


class SessionType(str, Enum):
    """Proxy session type."""
    STICKY = "sticky"
    RANDOM = "random"


class ProxyType(str, Enum):
    """Proxy type."""
    RESIDENTIAL = "residential_proxies"


@dataclass
class ProxyInfo:
    """Proxy connection information."""
    protocol: str
    host: str
    port: int
    username: str
    password: str
    
    @property
    def url(self) -> str:
        """Get proxy URL."""
        return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
    
    @property
    def full_address(self) -> str:
        """Get full proxy address."""
        return f"{self.host}:{self.port}"
    
    @classmethod
    def from_url(cls, url: str) -> "ProxyInfo":
        """Parse proxy URL into ProxyInfo."""
        parsed = urlparse(url)
        
        if "@" not in url:
            raise ValueError(f"Invalid proxy URL format: {url}")
        
        auth_host = parsed.netloc
        if "@" in auth_host:
            auth, host_port = auth_host.split("@", 1)
            username, password = auth.split(":", 1)
        else:
            host_port = auth_host
            username = password = ""
        
        if ":" in host_port:
            host, port_str = host_port.split(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 8080
        
        protocol = parsed.scheme if parsed.scheme else "http"
        
        return cls(
            protocol=protocol,
            host=host,
            port=port,
            username=username,
            password=password,
        )


@dataclass
class DecodoEndpoint:
    """Decodo endpoint information."""
    location: str
    hostname: str
    port_range: str


class DecodoClient:
    """
    Client for Decodo proxy API.
    
    Handles authentication, proxy extraction, and health checking
    with the Decodo proxy service.
    
    Usage:
        client = DecodoClient()
        proxy = await client.get_proxy(country="us", state="new_york")
    """
    
    BASE_URL = "https://api.decodo.com/v2"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Decodo client.
        
        Args:
            api_key: Decodo API key. If not provided, reads from settings.
        """
        settings = get_settings()
        self.api_key = api_key or getattr(settings, "DECODO_API_KEY", None)
        
        if not self.api_key:
            raise ProxyProviderError(
                "Decodo",
                "API key not configured. Set DECODO_API_KEY in environment."
            )
        
        self._client: Optional[httpx.AsyncClient] = None
        self.logger = logger.bind(module="decodo_client")
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
                timeout=30.0,
            )
        return self._client
    
    async def close(self) -> None:
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    async def get_subscriptions(self) -> dict:
        """
        Get subscription information.
        
        Returns:
            Subscription details including traffic limits and validity.
        """
        client = await self._get_client()
        
        try:
            response = await client.get("/subscriptions")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            self.logger.error(
                "decodo_subscription_error",
                status=e.response.status_code,
                detail=e.response.text,
            )
            raise ProxyProviderError("Decodo", f"Subscription error: {e.response.text}")
        except httpx.RequestError as e:
            self.logger.error("decodo_request_error", error=str(e))
            raise ProxyProviderError("Decodo", f"Request failed: {str(e)}")
    
    async def get_endpoints(self, endpoint_type: str = "random") -> list[DecodoEndpoint]:
        """
        Get available endpoints by type.
        
        Args:
            endpoint_type: "random" or "sticky"
            
        Returns:
            List of available endpoints.
        """
        client = await self._get_client()
        
        try:
            response = await client.get(f"/endpoints/{endpoint_type}")
            response.raise_for_status()
            data = response.json()
            
            return [
                DecodoEndpoint(
                    location=ep.get("location", ""),
                    hostname=ep.get("hostname", ""),
                    port_range=ep.get("port_range", ""),
                )
                for ep in data
            ]
        except httpx.HTTPStatusError as e:
            self.logger.error("decodo_endpoints_error", status=e.response.status_code)
            raise ProxyProviderError("Decodo", f"Endpoints error: {e.response.text}")
    
    async def get_proxies(
        self,
        country: Optional[str] = None,
        state: Optional[str] = None,
        city: Optional[str] = None,
        session_type: SessionType = SessionType.STICKY,
        session_duration: int = 10,
        count: int = 1,
        protocol: str = "http",
    ) -> list[ProxyInfo]:
        """
        Get custom proxies with location targeting.
        
        Args:
            country: Country code (ISO 3166-1 alpha-2, lowercase)
            state: State name (for US, use lowercase with underscores)
            city: City name (lowercase with underscores)
            session_type: STICKY or RANDOM
            session_duration: Session duration in minutes (1-1440)
            count: Number of proxies to generate
            protocol: Proxy protocol (http, https)
            
        Returns:
            List of ProxyInfo objects.
            
        Raises:
            ProxyProviderError: If API call fails.
        """
        client = await self._get_client()
        
        # Build location parameter
        if city and country:
            location = f"{city}"
        elif state and country:
            location = f"{state}"
        elif country:
            location = country
        else:
            location = "random"
        
        params = {
            "proxyType": ProxyType.RESIDENTIAL.value,
            "authType": "basic",
            "sessionType": session_type.value,
            "sessionDuration": session_duration,
            "location": location,
            "outputFormat": "protocol:auth@endpoint",
            "count": count,
            "responseType": "json",
            "protocol": protocol,
        }
        
        if country:
            params["country"] = country.lower()
        if state:
            params["state"] = state.lower()
        if city:
            params["city"] = city.lower()
        
        self.logger.info(
            "fetching_proxies",
            location=location,
            country=country,
            session_type=session_type.value,
            count=count,
        )
        
        try:
            response = await client.get("/endpoints-custom", params=params)
            response.raise_for_status()
            data = response.json()
            
            proxies = []
            for url in data:
                try:
                    proxy = ProxyInfo.from_url(url)
                    proxies.append(proxy)
                except ValueError as e:
                    self.logger.warning("proxy_parse_error", url=url, error=str(e))
                    continue
            
            self.logger.info(
                "proxies_fetched",
                requested=count,
                received=len(proxies),
            )
            
            return proxies
            
        except httpx.HTTPStatusError as e:
            self.logger.error(
                "decodo_proxy_error",
                status=e.response.status_code,
                detail=e.response.text,
            )
            raise ProxyProviderError("Decodo", f"Proxy fetch error: {e.response.text}")
        except httpx.RequestError as e:
            self.logger.error("decodo_proxy_request_error", error=str(e))
            raise ProxyProviderError("Decodo", f"Request failed: {str(e)}")
    
    async def get_proxy_for_account(self, country_code: str, count: int = 1) -> list[ProxyInfo]:
        """
        Get proxy for a specific country code.
        
        This is the main method used by account management to get
        location-matched proxies for Telegram accounts.
        
        Args:
            country_code: ISO 3166-1 alpha-2 country code (e.g., "US", "GB")
            count: Number of proxies to fetch
            
        Returns:
            List of ProxyInfo objects for the specified country.
        """
        # Map country codes to Decodo location names
        country_map = {
            "US": "United States",
            "GB": "United Kingdom",
            "DE": "Germany",
            "FR": "France",
            "ES": "Spain",
            "IT": "Italy",
            "NL": "Netherlands",
            "CA": "Canada",
            "AU": "Australia",
            "JP": "Japan",
            "KR": "South Korea",
            "BR": "Brazil",
            "MX": "Mexico",
            "IN": "India",
            "RU": "Russia",
            "CN": "China",
            "HK": "Hong Kong",
            "SG": "Singapore",
            "AE": "United Arab Emirates",
            "SA": "Saudi Arabia",
        }
        
        location = country_map.get(country_code.upper(), country_code)
        
        return await self.get_proxies(
            country=country_code.lower() if len(country_code) == 2 else None,
            session_type=SessionType.STICKY,
            session_duration=10,
            count=count,
        )


# Singleton instance
_decodo_client: Optional[DecodoClient] = None


def get_decodo_client() -> DecodoClient:
    """Get singleton Decodo client instance."""
    global _decodo_client
    if _decodo_client is None:
        _decodo_client = DecodoClient()
    return _decodo_client


async def init_decodo_client(api_key: Optional[str] = None) -> DecodoClient:
    """Initialize Decodo client with optional API key."""
    global _decodo_client
    if _decodo_client:
        await _decodo_client.close()
    _decodo_client = DecodoClient(api_key)
    return _decodo_client


async def close_decodo_client() -> None:
    """Close Decodo client."""
    global _decodo_client
    if _decodo_client:
        await _decodo_client.close()
        _decodo_client = None
