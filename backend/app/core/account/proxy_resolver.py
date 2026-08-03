"""Proxy resolution helpers for Telegram accounts."""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.evomi import ProxyInfo
from app.core.account.models import AccountType, Proxy, ProxyMode


@dataclass(frozen=True)
class ResolvedProxy:
    """A proxy selected for a Telegram client."""

    protocol: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    source: str = "static"
    proxy_id: Optional[int] = None
    session_id: Optional[str] = None
    expires_at: Optional[float] = None

    def to_telethon(self) -> tuple:
        """Convert to Telethon/PySocks proxy tuple."""
        return (
            self.protocol,
            self.host,
            self.port,
            True,
            self.username,
            self.password,
        )

    def to_proxy_info(self) -> ProxyInfo:
        """Convert to the runtime shape AccountPool already stores."""
        return ProxyInfo(
            protocol=self.protocol,
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            session_id=self.session_id or (f"static-{self.proxy_id}" if self.proxy_id else None),
            expires_at=self.expires_at,
        )


def normalize_proxy_mode(value: object) -> ProxyMode:
    """Normalize user/DB proxy mode values."""
    if isinstance(value, ProxyMode):
        return value
    if value is None:
        return ProxyMode.DYNAMIC
    return ProxyMode(str(value))


def _proxy_from_db(proxy: Proxy) -> ResolvedProxy:
    return ResolvedProxy(
        protocol=proxy.protocol,
        host=proxy.host,
        port=proxy.port,
        username=proxy.username,
        password=proxy.password,
        source="static",
        proxy_id=proxy.id,
    )


def _proxy_from_provider(proxy: ProxyInfo) -> ResolvedProxy:
    return ResolvedProxy(
        protocol=proxy.protocol,
        host=proxy.host,
        port=proxy.port,
        username=proxy.username,
        password=proxy.password,
        source="dynamic",
        session_id=proxy.session_id,
        expires_at=proxy.expires_at,
    )


async def resolve_static_proxy(db: AsyncSession, proxy_id: int) -> ResolvedProxy:
    """Load and validate a static proxy from DB."""
    proxy = await db.get(Proxy, proxy_id)
    if not proxy:
        raise ValueError(f"Static proxy {proxy_id} not found")
    if not proxy.is_active:
        raise ValueError(f"Static proxy {proxy_id} is inactive")
    if proxy.consecutive_failures >= 3:
        raise ValueError(f"Static proxy {proxy_id} is unhealthy")
    return _proxy_from_db(proxy)


async def resolve_provider_proxy(
    country_code: str,
    account_key: str,
    provider: str,
) -> ResolvedProxy:
    """Acquire a dynamic residential proxy from the configured provider."""
    if provider == "decodo":
        from app.core.account.decodo import get_decodo_client

        proxies = await get_decodo_client().get_proxy_for_account(country_code)
    else:
        from app.core.account.evomi import get_evomi_client

        proxies = await get_evomi_client().get_proxy_for_account(
            country_code,
            account_key=account_key,
        )

    if not proxies:
        raise ValueError("Proxy provider returned no proxy")
    return _proxy_from_provider(proxies[0])


async def resolve_auth_proxy(
    *,
    db: Optional[AsyncSession],
    account_type: AccountType,
    proxy_mode: ProxyMode,
    country_code: str,
    account_key: str,
    static_proxy_id: Optional[int],
    provider: str,
    proxy_required: bool,
) -> Optional[ResolvedProxy]:
    """Resolve the proxy for registration/login flows."""
    if proxy_mode == ProxyMode.NONE:
        return None

    if proxy_mode == ProxyMode.STATIC:
        if static_proxy_id is None:
            raise ValueError("static_proxy_id is required when proxy_mode is static")
        if db is None:
            raise ValueError("Database session is required for static proxy mode")
        return await resolve_static_proxy(db, static_proxy_id)

    if account_type == AccountType.PROMOTER and proxy_required:
        return await resolve_provider_proxy(country_code, account_key, provider)

    return None
