"""
Evomi Proxy Client.

Retrieves Evomi proxy strings using the public API and converts them into
Telethon-compatible proxy connection details.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx
import structlog

from app.core.account.exceptions import ProxyProviderError
from app.core.config import get_settings

logger = structlog.get_logger()


@dataclass
class ProxyInfo:
    """Proxy connection information."""

    protocol: str
    host: str
    port: int
    username: str
    password: str
    session_id: str | None = None
    expires_at: float | None = None

    @property
    def url(self) -> str:
        """Get proxy URL."""
        return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"

    @property
    def full_address(self) -> str:
        """Get host:port."""
        return f"{self.host}:{self.port}"

    @classmethod
    def from_url(cls, proxy_line: str, default_protocol: str = "http") -> ProxyInfo:
        """Parse Evomi proxy output formats into ProxyInfo."""
        value = proxy_line.strip()
        if not value:
            raise ValueError("empty proxy line")

        protocol = default_protocol
        raw = value
        if "://" in value:
            protocol, raw = value.split("://", 1)
            parsed = urlparse(value)
            protocol = parsed.scheme or default_protocol
            try:
                parsed_port = parsed.port
            except ValueError:
                parsed_port = None
            if parsed.username and parsed.password and parsed.hostname and parsed_port:
                return cls(
                    protocol=protocol,
                    host=parsed.hostname,
                    port=parsed_port,
                    username=unquote(parsed.username),
                    password=unquote(parsed.password),
                )

        if "@" in raw:
            auth, host_port = raw.rsplit("@", 1)
            username, password = auth.split(":", 1)
            host, port = host_port.rsplit(":", 1)
            return cls(
                protocol=protocol,
                host=host,
                port=int(port),
                username=unquote(username),
                password=unquote(password),
            )

        parts = raw.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = parts
            return cls(
                protocol=protocol,
                host=host,
                port=int(port),
                username=unquote(username),
                password=unquote(password),
            )
        if len(parts) == 4 and parts[3].isdigit():
            username, password, host, port = parts
            return cls(
                protocol=protocol,
                host=host,
                port=int(port),
                username=unquote(username),
                password=unquote(password),
            )

        raise ValueError(f"Unsupported proxy format: {proxy_line}")


class EvomiClient:
    """Client for Evomi Public API proxy generation."""

    BASE_URL = "https://api.evomi.com"

    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        self.api_key = api_key or getattr(settings, "EVOMI_API_KEY", None)
        if not self.api_key:
            raise ProxyProviderError(
                "Evomi",
                "API key not configured. Set EVOMI_API_KEY in environment.",
            )

        self.product_code = getattr(settings, "EVOMI_PRODUCT_CODE", "rp")
        self.protocol = getattr(settings, "EVOMI_PROTOCOL", "http")
        self.session_type = getattr(settings, "EVOMI_SESSION_TYPE", "sticky")
        self.session_lifetime = getattr(settings, "EVOMI_SESSION_LIFETIME_MINUTES", 30)
        self.session_namespace = getattr(settings, "EVOMI_SESSION_NAMESPACE", "vanguard")
        self.adblock = getattr(settings, "EVOMI_ADBLOCK", False)
        self._client: httpx.AsyncClient | None = None
        self._proxy_data: dict | None = None
        self.logger = logger.bind(module="evomi_client")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "x-apikey": self.api_key,
                    "Accept": "application/json, text/plain",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_proxy_data(self, refresh: bool = False) -> dict:
        """Fetch Evomi account proxy product data."""
        if self._proxy_data is not None and not refresh:
            return self._proxy_data

        client = await self._get_client()
        try:
            response = await client.get("/public")
            response.raise_for_status()
            data = response.json()
            if not data.get("success", True):
                raise ProxyProviderError("Evomi", str(data.get("error", "proxy data failed")))
            self._proxy_data = data
            return data
        except httpx.HTTPStatusError as exc:
            raise ProxyProviderError("Evomi", f"Proxy data error: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise ProxyProviderError("Evomi", f"Request failed: {exc}") from exc

    async def generate_proxies(
        self,
        *,
        product: str | None = None,
        countries: str | None = None,
        amount: int = 1,
        protocol: str | None = None,
        session: str | None = None,
        lifetime: int | None = None,
        adblock: bool | None = None,
    ) -> list[ProxyInfo]:
        """Generate Evomi proxy strings with targeting parameters."""
        client = await self._get_client()
        resolved_product = product or self.product_code
        resolved_protocol = protocol or self.protocol

        params: dict[str, object] = {
            "product": resolved_product,
            "amount": max(1, min(amount, 100)),
            "format": 1,
            "prepend_protocol": True,
            "protocol": resolved_protocol,
        }
        if countries:
            params["countries"] = countries.upper()
        if session:
            params["session"] = session
        if lifetime and session != "hard":
            params["lifetime"] = lifetime
        if adblock is not None and resolved_product != "rpc":
            params["adblock"] = bool(adblock)

        try:
            response = await client.get("/public/generate", params=params)
            response.raise_for_status()
            text = response.text.strip()
        except httpx.HTTPStatusError as exc:
            raise ProxyProviderError("Evomi", f"Proxy generation error: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise ProxyProviderError("Evomi", f"Request failed: {exc}") from exc

        proxies: list[ProxyInfo] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                proxies.append(ProxyInfo.from_url(line, default_protocol=resolved_protocol))
            except ValueError as exc:
                self.logger.warning("evomi_proxy_parse_error", line=line, error=str(exc))

        return proxies

    def sticky_session_id(self, account_key: str) -> str:
        """Build a deterministic Evomi session id for one Vanguard account."""
        normalized = (account_key or "default").strip().lower()
        seed = f"{self.session_namespace}:{normalized}".encode()
        return hashlib.sha256(seed).hexdigest()[:8]

    async def build_account_proxy(
        self,
        *,
        account_key: str,
        country_code: str,
        product: str | None = None,
        protocol: str | None = None,
        lifetime: int | None = None,
    ) -> ProxyInfo:
        """Construct a stable sticky Evomi proxy for one Telegram account."""
        resolved_product = product or self.product_code
        resolved_protocol = protocol or self.protocol
        data = await self.get_proxy_data()
        product_data = (data.get("products") or {}).get(resolved_product)
        if not product_data:
            raise ProxyProviderError("Evomi", f"Proxy product '{resolved_product}' is not available")

        username = product_data.get("username")
        base_password = product_data.get("password")
        endpoint = product_data.get("endpoint")
        ports = product_data.get("ports") or {}
        port = ports.get(resolved_protocol) or ports.get("http")
        if not username or not base_password or not endpoint or not port:
            raise ProxyProviderError("Evomi", f"Proxy product '{resolved_product}' has incomplete credentials")

        session_id = self.sticky_session_id(account_key)
        configured_session_type = (self.session_type or "sticky").lower()
        session_mode = "hardsession" if configured_session_type in {"hard", "hardsession"} else "session"
        resolved_lifetime = max(1, min(int(lifetime or self.session_lifetime), 120))
        password_parts = [str(base_password)]
        country = (country_code or "").upper()[:2]
        if country:
            password_parts.append(f"country-{country}")
        password_parts.append(f"{session_mode}-{session_id}")
        if session_mode != "hardsession":
            password_parts.append(f"lifetime-{resolved_lifetime}")

        return ProxyInfo(
            protocol=resolved_protocol,
            host=str(endpoint),
            port=int(port),
            username=str(username),
            password="_".join(password_parts),
            session_id=session_id,
            expires_at=None if session_mode == "hardsession" else time.time() + resolved_lifetime * 60,
        )

    async def get_proxy_for_account(
        self,
        country_code: str,
        count: int = 1,
        account_key: str | None = None,
    ) -> list[ProxyInfo]:
        """Get a required proxy for a Telegram account country."""
        if account_key:
            return [await self.build_account_proxy(account_key=account_key, country_code=country_code)]

        country = (country_code or "").upper()[:2] or None
        proxies = await self.generate_proxies(
            countries=country,
            amount=count,
            session=self.session_type,
            lifetime=self.session_lifetime,
            adblock=self.adblock,
        )
        if not proxies:
            raise ProxyProviderError("Evomi", "No proxies returned for requested country")
        return proxies


_evomi_client: EvomiClient | None = None


def get_evomi_client() -> EvomiClient:
    """Get singleton Evomi client."""
    global _evomi_client
    if _evomi_client is None:
        _evomi_client = EvomiClient()
    return _evomi_client


async def init_evomi_client(api_key: str | None = None) -> EvomiClient:
    """Initialize singleton Evomi client."""
    global _evomi_client
    if _evomi_client:
        await _evomi_client.close()
    _evomi_client = EvomiClient(api_key)
    return _evomi_client


async def close_evomi_client() -> None:
    """Close singleton Evomi client."""
    global _evomi_client
    if _evomi_client:
        await _evomi_client.close()
        _evomi_client = None
