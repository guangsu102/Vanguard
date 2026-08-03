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

try:
    import phonenumbers
except ImportError:  # pragma: no cover - production installs this dependency.
    phonenumbers = None

from app.core.account.exceptions import ProxyProviderError
from app.core.config import get_settings

logger = structlog.get_logger()


EVOMI_PASSWORD_PARAM_MARKERS: tuple[str, ...] = (
    "_country-",
    "_session-",
    "_hardsession-",
    "_lifetime-",
)


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
        self.proxy_host = getattr(settings, "EVOMI_PROXY_HOST", None)
        self.proxy_port = getattr(settings, "EVOMI_PROXY_PORT", None)
        self.proxy_username = getattr(settings, "EVOMI_PROXY_USERNAME", None)
        self.proxy_password = getattr(settings, "EVOMI_PROXY_PASSWORD", None)
        self._has_static_proxy = all(
            [
                self.proxy_host,
                self.proxy_port,
                self.proxy_username,
                self.proxy_password,
            ]
        )
        if not self.api_key and not self._has_static_proxy:
            raise ProxyProviderError(
                "Evomi",
                "Proxy credentials not configured. Set EVOMI_API_KEY or "
                "EVOMI_PROXY_HOST/EVOMI_PROXY_PORT/EVOMI_PROXY_USERNAME/EVOMI_PROXY_PASSWORD.",
            )

        self.product_code = getattr(settings, "EVOMI_PRODUCT_CODE", "rp")
        self.protocol = getattr(settings, "EVOMI_PROTOCOL", "http")
        self.session_type = getattr(settings, "EVOMI_SESSION_TYPE", "sticky")
        self.session_lifetime = getattr(settings, "EVOMI_SESSION_LIFETIME_MINUTES", 30)
        self.session_namespace = getattr(settings, "EVOMI_SESSION_NAMESPACE", "vanguard")
        self.adblock = getattr(settings, "EVOMI_ADBLOCK", False)
        self.country_verify_enabled = bool(getattr(settings, "EVOMI_COUNTRY_VERIFY_ENABLED", False))
        self.country_verify_attempts = max(1, int(getattr(settings, "EVOMI_COUNTRY_VERIFY_ATTEMPTS", 5)))
        self.country_verify_timeout = max(1, int(getattr(settings, "EVOMI_COUNTRY_VERIFY_TIMEOUT_SECONDS", 8)))
        self.country_verify_url = getattr(
            settings,
            "EVOMI_COUNTRY_VERIFY_URL",
            "http://ip-api.com/json/?fields=status,countryCode,query",
        )
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

    def sticky_session_id(self, account_key: str, attempt: int = 0, country_code: str | None = None) -> str:
        """Build a deterministic Evomi session id for one Vanguard account."""
        normalized = (account_key or "default").strip().lower()
        retry_key = "" if attempt <= 0 else f":{(country_code or '').upper()}:{attempt}"
        seed = f"{self.session_namespace}:{normalized}{retry_key}".encode()
        return hashlib.sha256(seed).hexdigest()[:8]

    def resolve_country_code(self, country_code: str, account_key: str | None = None) -> str:
        """Resolve proxy country from the phone/account key first, then the country field."""
        phone_country = self.country_code_from_phone(account_key)
        if phone_country:
            return phone_country

        return (country_code or "").upper()[:2]

    def country_code_from_phone(self, phone: str | None) -> str | None:
        """Infer ISO alpha-2 country code from an E.164 phone number."""
        if not phone or phonenumbers is None:
            return None

        normalized = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        candidates = [normalized]
        if normalized.startswith("00"):
            candidates.append(f"+{normalized[2:]}")
        elif normalized.startswith("+"):
            candidates.append(normalized)

        for candidate in dict.fromkeys(candidates):
            if not candidate.startswith("+"):
                continue
            try:
                parsed = phonenumbers.parse(candidate, None)
            except phonenumbers.NumberParseException:
                continue
            country = phonenumbers.region_code_for_number(parsed)
            if not country:
                country = phonenumbers.region_code_for_country_code(parsed.country_code)
            if country and country != "001":
                return country.upper()

        return None

    def _static_base_password(self) -> str:
        """Return the configured Evomi password without existing routing modifiers."""
        password = str(self.proxy_password)
        marker_positions = [
            position
            for marker in EVOMI_PASSWORD_PARAM_MARKERS
            if (position := password.find(marker)) >= 0
        ]
        if not marker_positions:
            return password
        return password[:min(marker_positions)]

    def build_routed_password(
        self,
        *,
        base_password: str,
        account_key: str,
        country_code: str,
        lifetime: int | None = None,
        session_attempt: int = 0,
    ) -> tuple[str, str, float | None]:
        """Build an Evomi password with country routing and account-scoped sticky session."""
        resolved_country = self.resolve_country_code(country_code, account_key)
        session_id = self.sticky_session_id(account_key, attempt=session_attempt, country_code=resolved_country)
        configured_session_type = (self.session_type or "sticky").lower()
        session_mode = "hardsession" if configured_session_type in {"hard", "hardsession"} else "session"
        resolved_lifetime = max(1, min(int(lifetime or self.session_lifetime), 120))

        password_parts = [base_password]
        if resolved_country:
            password_parts.append(f"country-{resolved_country}")
        password_parts.append(f"{session_mode}-{session_id}")
        if session_mode != "hardsession":
            password_parts.append(f"lifetime-{resolved_lifetime}")

        expires_at = None if session_mode == "hardsession" else time.time() + resolved_lifetime * 60
        return "_".join(password_parts), session_id, expires_at

    def build_static_account_proxy(
        self,
        *,
        account_key: str,
        country_code: str,
        protocol: str | None = None,
        lifetime: int | None = None,
        session_attempt: int = 0,
    ) -> ProxyInfo:
        """Build a proxy from static Evomi gateway credentials."""
        if not self._has_static_proxy:
            raise ProxyProviderError("Evomi", "Static proxy credentials are incomplete")
        password, session_id, expires_at = self.build_routed_password(
            base_password=self._static_base_password(),
            account_key=account_key,
            country_code=country_code,
            lifetime=lifetime,
            session_attempt=session_attempt,
        )

        return ProxyInfo(
            protocol=protocol or self.protocol,
            host=str(self.proxy_host),
            port=int(self.proxy_port),
            username=str(self.proxy_username),
            password=password,
            session_id=session_id,
            expires_at=expires_at,
        )

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
        if self._has_static_proxy:
            return await self.build_verified_static_account_proxy(
                account_key=account_key,
                country_code=country_code,
                protocol=protocol,
                lifetime=lifetime,
            )

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

        password, session_id, expires_at = self.build_routed_password(
            base_password=str(base_password),
            account_key=account_key,
            country_code=country_code,
            lifetime=lifetime,
        )

        return ProxyInfo(
            protocol=resolved_protocol,
            host=str(endpoint),
            port=int(port),
            username=str(username),
            password=password,
            session_id=session_id,
            expires_at=expires_at,
        )

    async def build_verified_static_account_proxy(
        self,
        *,
        account_key: str,
        country_code: str,
        protocol: str | None = None,
        lifetime: int | None = None,
    ) -> ProxyInfo:
        """Build a static proxy and optionally verify its real exit country."""
        target_country = self.resolve_country_code(country_code, account_key)
        for attempt in range(self.country_verify_attempts):
            proxy = self.build_static_account_proxy(
                account_key=account_key,
                country_code=country_code,
                protocol=protocol,
                lifetime=lifetime,
                session_attempt=attempt,
            )
            if not self.country_verify_enabled or not target_country:
                return proxy
            if await self.proxy_matches_country(proxy, target_country):
                return proxy

        raise ProxyProviderError(
            "Evomi",
            f"Unable to acquire proxy for country {target_country} after "
            f"{self.country_verify_attempts} attempts",
        )

    async def proxy_matches_country(self, proxy: ProxyInfo, target_country: str) -> bool:
        """Return whether the proxy exits from the requested country."""
        try:
            async with httpx.AsyncClient(proxy=proxy.url, timeout=float(self.country_verify_timeout)) as client:
                response = await client.get(self.country_verify_url)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            self.logger.warning(
                "evomi_country_verify_failed",
                target_country=target_country,
                proxy_host=proxy.host,
                error=str(exc),
            )
            return False

        actual_country = str(data.get("countryCode") or "").upper()
        matched = actual_country == target_country.upper()
        if not matched:
            self.logger.warning(
                "evomi_country_mismatch",
                target_country=target_country,
                actual_country=actual_country or None,
                proxy_host=proxy.host,
            )
        return matched

    async def get_proxy_for_account(
        self,
        country_code: str,
        count: int = 1,
        account_key: str | None = None,
    ) -> list[ProxyInfo]:
        """Get a required proxy for a Telegram account country."""
        if account_key:
            return [await self.build_account_proxy(account_key=account_key, country_code=country_code)]

        if self._has_static_proxy:
            return [
                self.build_static_account_proxy(
                    account_key=f"{country_code or 'default'}:{index}",
                    country_code=country_code,
                )
                for index in range(max(1, count))
            ]

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
