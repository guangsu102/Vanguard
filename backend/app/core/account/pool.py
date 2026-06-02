"""
Account Pool Module

Manages a pool of Telegram accounts with load balancing and health monitoring.

Features:
- Account lifecycle management (add, remove, acquire, release)
- Load balancing strategies (round-robin, least-used, random)
- Session persistence for auto-relogin
- Dynamic proxy acquisition based on country
- Health checking and automatic recovery
- Thread-safe operations with asyncio locks
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import structlog
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.core.account.decodo import DecodoClient, get_decodo_client
from app.core.account.evomi import EvomiClient, ProxyInfo, get_evomi_client
from app.core.account.models import AccountStatus, AccountType

if TYPE_CHECKING:
    from app.core.account.models import TelegramAccount

logger = structlog.get_logger()


@dataclass
class TelegramAccountWrapper:
    """
    Wrapper for Telegram account with connection management.

    Provides a high-level interface for managing individual Telegram account
    connections, including connection state, proxy settings, and health metrics.

    Attributes:
        account_id: Database ID of the account
        phone: Phone number
        session_name: Session file name
        status: Current account status
        country_code: Country code for proxy matching
        api_id: Telegram API ID
        api_hash: Telegram API Hash
        fingerprint_id: Device fingerprint ID (if any)
    """

    account_id: int
    phone: str
    session_name: str
    country_code: str
    api_id: str
    api_hash: str
    session_string: Optional[str] = None
    account_type: AccountType = AccountType.PROMOTER
    status: AccountStatus = AccountStatus.OFFLINE
    api_config_name: str = "default"
    fingerprint_id: Optional[str] = None
    device_model: Optional[str] = None
    system_version: Optional[str] = None
    app_version: Optional[str] = None
    client: Optional[TelegramClient] = field(default=None, repr=False)
    current_proxy: Optional[ProxyInfo] = field(default=None, repr=False)
    current_proxy_country: Optional[str] = field(default=None, repr=False)
    keep_connected: bool = field(default=False, repr=False)

    def get_client(self) -> Optional[TelegramClient]:
        """Get the bound Telegram client."""
        return self.client

    def set_client(self, client: Optional[TelegramClient]) -> None:
        """Bind a Telegram client to this account."""
        self.client = client

    # Health metrics
    _message_count: int = field(default=0, repr=False)
    _error_count: int = field(default=0, repr=False)
    _last_message_at: float = field(default=0, repr=False)

    @property
    def health_score(self) -> float:
        """
        Calculate health score based on metrics.

        Returns:
            Health score between 0 and 1
        """
        if self._message_count == 0:
            return 1.0

        error_rate = self._error_count / max(self._message_count, 1)
        return max(0.0, 1.0 - error_rate)

    @property
    def is_available(self) -> bool:
        """Check if account is available for use."""
        return self.status in [AccountStatus.IDLE, AccountStatus.ONLINE]

    @property
    def session_file_path(self) -> Path:
        """Get the session file path."""
        from app.core.config import get_settings
        settings = get_settings()
        session_dir = Path(settings.TELEGRAM_SESSION_DIR)
        return session_dir / f"{self.session_name}.session"

    @property
    def session_exists(self) -> bool:
        """Check if session file exists."""
        return bool(self.session_string) or self.session_file_path.exists()

    def record_message(self, success: bool = True) -> None:
        """Record message sent for health tracking."""
        self._message_count += 1
        if not success:
            self._error_count += 1
        self._last_message_at = time.time()


class AccountPool:
    """
    Pool of Telegram accounts with load balancing.

    Manages multiple TelegramAccountWrapper instances and provides:
    - Automatic account acquisition and release
    - Multiple load balancing strategies
    - Dynamic proxy acquisition from Evomi by default
    - Session persistence management
    - Health checking and monitoring
    - Graceful shutdown

    Usage:
        pool = AccountPool()
        await pool.add_account(wrapped_account)
        account = await pool.acquire(purpose="send_message")
        # ... use account ...
        await pool.release(account)
    """

    def __init__(self, strategy: str = "least_used"):
        """
        Initialize AccountPool.

        Args:
            strategy: Load balancing strategy ("least_used", "round_robin", "random")
        """
        self._accounts: dict[str, TelegramAccountWrapper] = {}
        self._lock = asyncio.Lock()
        self._round_robin_index: int = 0
        self._strategy = strategy
        self._evomi_client: Optional[EvomiClient] = None
        self._decodo_client: Optional[DecodoClient] = None
        self._session_dir: Optional[Path] = None
        self.logger = logger.bind(module="account_pool")

    def _ensure_session_dir(self) -> Path:
        """Ensure session directory exists."""
        if self._session_dir is None:
            from app.core.config import get_settings
            settings = get_settings()
            self._session_dir = Path(settings.TELEGRAM_SESSION_DIR)
            self._session_dir.mkdir(parents=True, exist_ok=True)
        return self._session_dir

    def set_decodo_client(self, client: DecodoClient) -> None:
        """
        Set Decodo client for proxy acquisition.

        Args:
            client: DecodoClient instance
        """
        self._decodo_client = client
        self.logger.info("decodo_client_set")

    def set_evomi_client(self, client: EvomiClient) -> None:
        """
        Set Evomi client for proxy acquisition.

        Args:
            client: EvomiClient instance
        """
        self._evomi_client = client
        self.logger.info("evomi_client_set")

    async def add_account(
        self,
        account_id: int,
        phone: str,
        session_name: str,
        country_code: str,
        api_id: str,
        api_hash: str,
        session_string: Optional[str] = None,
        account_type: AccountType = AccountType.PROMOTER,
        api_config_name: str = "default",
        fingerprint_id: Optional[str] = None,
        device_model: Optional[str] = None,
        system_version: Optional[str] = None,
        app_version: Optional[str] = None,
    ) -> TelegramAccountWrapper:
        """
        Add account to the pool.

        Args:
            account_id: Database ID
            phone: Phone number
            session_name: Session file name
            country_code: Country code for proxy matching
            api_id: Telegram API ID
            api_hash: Telegram API Hash
            api_config_name: API config name
            fingerprint_id: Optional fingerprint ID
            device_model: Device model for session persistence
            system_version: System version for session persistence
            app_version: App version for session persistence

        Returns:
            Created TelegramAccountWrapper
        """
        async with self._lock:
            if session_name in self._accounts:
                self.logger.warning("account_already_exists", session_name=session_name)
                return self._accounts[session_name]

            wrapper = TelegramAccountWrapper(
                account_id=account_id,
                phone=phone,
                session_name=session_name,
                country_code=country_code,
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                account_type=account_type,
                api_config_name=api_config_name,
                fingerprint_id=fingerprint_id,
                device_model=device_model,
                system_version=system_version,
                app_version=app_version,
            )

            self._accounts[session_name] = wrapper
            self.logger.info(
                "account_added",
                session_name=session_name,
                phone=phone,
                country=country_code,
                total_accounts=len(self._accounts),
            )

            return wrapper

    async def add_account_from_db(self, account: "TelegramAccount") -> TelegramAccountWrapper:
        """
        Add account from database model.

        Args:
            account: TelegramAccount database model

        Returns:
            TelegramAccountWrapper
        """
        return await self.add_account(
            account_id=account.id,
            phone=account.phone,
            session_name=account.session_name,
            country_code=account.country_code,
            api_id=account.api_config.api_id if account.api_config else "",
            api_hash=account.api_config.api_hash if account.api_config else "",
            session_string=account.session_string,
            account_type=getattr(account, "account_type", AccountType.PROMOTER),
            api_config_name=account.api_config_name,
            fingerprint_id=account.fingerprint_id,
            device_model=account.device_model,
            system_version=account.system_version,
            app_version=account.app_version,
        )

    async def remove_account(self, session_name: str) -> bool:
        """
        Remove account from the pool.

        Args:
            session_name: Session name of account to remove

        Returns:
            True if removed, False if not found
        """
        async with self._lock:
            if session_name not in self._accounts:
                return False

            account = self._accounts[session_name]

            if account.status == AccountStatus.WORKING:
                self.logger.warning(
                    "removing_working_account",
                    session_name=session_name,
                )

            del self._accounts[session_name]
            self.logger.info(
                "account_removed",
                session_name=session_name,
                remaining=len(self._accounts),
            )

            return True

    async def get_account(self, session_name: str) -> Optional[TelegramAccountWrapper]:
        """
        Get account wrapper by session name.

        Args:
            session_name: Session name

        Returns:
            TelegramAccountWrapper if found, None otherwise
        """
        return self._accounts.get(session_name)

    async def get_account_by_id(self, account_id: int) -> Optional[TelegramAccountWrapper]:
        """
        Get account wrapper by database ID.

        Args:
            account_id: Database ID of account

        Returns:
            TelegramAccountWrapper if found, None otherwise
        """
        for account in self._accounts.values():
            if account.account_id == account_id:
                return account
        return None

    async def acquire(
        self,
        purpose: str = "default",
        country_code: Optional[str] = None,
        require_session: bool = True,
    ) -> Optional[TelegramAccountWrapper]:
        """
        Acquire an available account from the pool.

        Args:
            purpose: Purpose for acquiring (for logging/debugging)
            country_code: Prefer accounts from this country
            require_session: If True, only return accounts with existing session

        Returns:
            TelegramAccountWrapper with proxy acquired, None if no accounts available
        """
        async with self._lock:
            available = self._get_available_accounts(require_session)

            if not available:
                self.logger.warning("no_available_accounts", purpose=purpose)
                return None

            if country_code:
                filtered = [a for a in available if a.country_code == country_code.upper()]
                if filtered:
                    available = filtered

            selected = self._select_account(available)
            selected.status = AccountStatus.WORKING

            try:
                await self._ensure_proxy(selected)
                if selected.client is None or not getattr(selected.client, "is_connected", lambda: False)():
                    selected.client = await self._create_client(selected)
                self.logger.info(
                    "proxy_acquired",
                    session_name=selected.session_name,
                    country=selected.country_code,
                    proxy_host=selected.current_proxy.host if selected.current_proxy else None,
                )
            except Exception as e:
                selected.status = AccountStatus.ERROR
                self.logger.warning(
                    "account_acquire_failed",
                    session_name=selected.session_name,
                    country=selected.country_code,
                    error=str(e),
                )
                return None

            self.logger.debug(
                "account_acquired",
                session_name=selected.session_name,
                purpose=purpose,
                strategy=self._strategy,
                has_proxy=selected.current_proxy is not None,
                has_client=selected.client is not None,
            )

            return selected

    async def acquire_by_id(
        self,
        account_id: int,
        purpose: str = "default",
        require_session: bool = True,
    ) -> Optional[TelegramAccountWrapper]:
        """
        Acquire a specific account by database ID.

        Args:
            account_id: Telegram account database ID
            purpose: Purpose for logging/debugging
            require_session: If True, only return accounts with a session

        Returns:
            TelegramAccountWrapper if available, None otherwise
        """
        async with self._lock:
            selected = None
            for account in self._accounts.values():
                if account.account_id == account_id:
                    selected = account
                    break

            if selected is None:
                self.logger.warning("account_not_in_pool", account_id=account_id, purpose=purpose)
                return None

            if selected.status not in [AccountStatus.IDLE, AccountStatus.ONLINE, AccountStatus.OFFLINE]:
                self.logger.warning(
                    "account_not_available",
                    account_id=account_id,
                    status=selected.status.value,
                    purpose=purpose,
                )
                return None

            if require_session and not selected.session_exists:
                self.logger.warning("account_session_missing", account_id=account_id, purpose=purpose)
                return None

            selected.status = AccountStatus.WORKING

            try:
                await self._ensure_proxy(selected)
                if selected.client is None or not getattr(selected.client, "is_connected", lambda: False)():
                    selected.client = await self._create_client(selected)
            except Exception as e:
                selected.status = AccountStatus.ERROR
                self.logger.warning(
                    "account_acquire_by_id_failed",
                    account_id=account_id,
                    purpose=purpose,
                    error=str(e),
                )
                raise

            self.logger.debug(
                "account_acquired_by_id",
                account_id=account_id,
                session_name=selected.session_name,
                purpose=purpose,
            )
            return selected

    async def connect_by_id(
        self,
        account_id: int,
        purpose: str = "listener",
        require_session: bool = True,
        keep_connected: bool = True,
    ) -> Optional[TelegramAccountWrapper]:
        """
        Ensure a specific account has a connected client for long-lived listeners.

        Unlike acquire_by_id, this keeps the account available for pooled send
        operations and release() will not disconnect the client while
        keep_connected is True.
        """
        async with self._lock:
            selected = None
            for account in self._accounts.values():
                if account.account_id == account_id:
                    selected = account
                    break

            if selected is None:
                self.logger.warning("account_not_in_pool", account_id=account_id, purpose=purpose)
                return None

            if selected.status in [AccountStatus.ERROR, AccountStatus.BANNED]:
                self.logger.warning(
                    "account_not_connectable",
                    account_id=account_id,
                    status=selected.status.value,
                    purpose=purpose,
                )
                return None

            if require_session and not selected.session_exists:
                self.logger.warning("account_session_missing", account_id=account_id, purpose=purpose)
                return None

            try:
                await self._ensure_proxy(selected)
                if selected.client is None or not getattr(selected.client, "is_connected", lambda: False)():
                    selected.client = await self._create_client(selected)
                selected.keep_connected = keep_connected
                selected.status = AccountStatus.IDLE
            except Exception as e:
                selected.status = AccountStatus.ERROR
                self.logger.warning(
                    "account_connect_by_id_failed",
                    account_id=account_id,
                    purpose=purpose,
                    error=str(e),
                )
                raise

            self.logger.info(
                "account_connected_by_id",
                account_id=account_id,
                session_name=selected.session_name,
                purpose=purpose,
                keep_connected=keep_connected,
            )
            return selected

    async def _create_client(self, account: TelegramAccountWrapper) -> TelegramClient:
        """Create a Telethon client bound to the account session."""
        session_path = account.session_file_path
        session_path.parent.mkdir(parents=True, exist_ok=True)
        proxy = account.current_proxy
        proxy_config = None
        if proxy:
            proxy_config = (
                proxy.protocol,
                proxy.host,
                proxy.port,
                True,
                proxy.username,
                proxy.password,
            )

        session = StringSession(account.session_string) if account.session_string else (
            str(session_path) if session_path.exists() else StringSession()
        )

        client = TelegramClient(
            session,
            int(account.api_id),
            account.api_hash,
            proxy=proxy_config,
            device_model=account.device_model,
            system_version=account.system_version,
            app_version=account.app_version,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(f"account {account.session_name} is not authorized")
        return client

    def _proxy_required_for_account(self, account: TelegramAccountWrapper) -> bool:
        """Return whether this account must use a proxy."""
        if account.account_type != AccountType.PROMOTER:
            return False

        from app.core.config import get_settings
        settings = get_settings()
        return bool(getattr(settings, "PROMOTER_PROXY_REQUIRED", True))

    async def _ensure_proxy(self, account: TelegramAccountWrapper) -> None:
        """Attach a proxy to an account and enforce promoter proxy policy."""
        proxy_required = self._proxy_required_for_account(account)
        if not proxy_required:
            return

        if self._has_valid_proxy(account):
            return

        previous_proxy = account.current_proxy
        proxy = await self._acquire_proxy(account)
        account.current_proxy = proxy
        account.current_proxy_country = account.country_code.upper()

        if account.client is not None and previous_proxy != proxy:
            try:
                if account.client.is_connected():
                    await account.client.disconnect()
            except Exception as e:
                self.logger.warning(
                    "proxy_refresh_disconnect_failed",
                    session_name=account.session_name,
                    error=str(e),
                )
            finally:
                account.client = None

        if proxy is None:
            raise RuntimeError("Promoter account proxy is required but no proxy was acquired")

    def _has_valid_proxy(self, account: TelegramAccountWrapper) -> bool:
        """Return whether the account can keep using its current sticky proxy."""
        proxy = account.current_proxy
        if proxy is None:
            return False

        if account.current_proxy_country != account.country_code.upper():
            return False

        expires_at = getattr(proxy, "expires_at", None)
        if expires_at is None:
            return True

        return float(expires_at) > time.time()

    def _proxy_account_key(self, account: TelegramAccountWrapper) -> str:
        """Stable key used by providers for account-scoped sticky sessions."""
        return account.phone or account.session_name or str(account.account_id)

    async def _acquire_proxy(self, account: TelegramAccountWrapper) -> Optional[ProxyInfo]:
        """
        Acquire proxy for an account.

        Args:
            account: Account wrapper

        Returns:
            ProxyInfo or None if acquisition fails
        """
        from app.core.config import get_settings
        settings = get_settings()
        provider = getattr(settings, "PROXY_PROVIDER", "evomi").lower()

        try:
            if provider == "decodo":
                if self._decodo_client is None:
                    self._decodo_client = get_decodo_client()
                proxies = await self._decodo_client.get_proxy_for_account(account.country_code)
            else:
                if self._evomi_client is None:
                    self._evomi_client = get_evomi_client()
                proxies = await self._evomi_client.get_proxy_for_account(
                    account.country_code,
                    account_key=self._proxy_account_key(account),
                )

            if proxies:
                return proxies[0]
        except Exception as e:
            self.logger.error(
                "proxy_acquisition_error",
                provider=provider,
                country=account.country_code,
                session_name=account.session_name,
                error=str(e),
            )

        return None

    async def release(self, account: Optional[TelegramAccountWrapper]) -> None:
        """
        Release account back to the pool.

        Args:
            account: Account wrapper to release
        """
        if account is None:
            return

        async with self._lock:
            if account.session_name not in self._accounts:
                self.logger.warning(
                    "releasing_unknown_account",
                    session_name=account.session_name,
                )
                return

            if account.client is not None and not account.keep_connected:
                try:
                    if account.client.is_connected():
                        await account.client.disconnect()
                except Exception as e:
                    self.logger.warning(
                        "release_disconnect_failed",
                        session_name=account.session_name,
                        error=str(e),
                    )
            if not account.keep_connected:
                account.client = None
            account.status = AccountStatus.IDLE

            self.logger.debug(
                "account_released",
                session_name=account.session_name,
            )

    async def set_online(self, session_name: str) -> bool:
        """
        Set account status to online.

        Args:
            session_name: Session name

        Returns:
            True if successful
        """
        async with self._lock:
            if session_name not in self._accounts:
                return False

            self._accounts[session_name].status = AccountStatus.ONLINE
            return True

    async def set_offline(self, session_name: str) -> bool:
        """
        Set account status to offline.

        Args:
            session_name: Session name

        Returns:
            True if successful
        """
        async with self._lock:
            if session_name not in self._accounts:
                return False

            account = self._accounts[session_name]
            if account.client is not None:
                try:
                    if account.client.is_connected():
                        await account.client.disconnect()
                except Exception as e:
                    self.logger.warning(
                        "set_offline_disconnect_failed",
                        session_name=session_name,
                        error=str(e),
                    )
            account.status = AccountStatus.OFFLINE
            account.client = None
            return True

    async def set_error(self, session_name: str, error: str = "") -> bool:
        """
        Set account status to error.

        Args:
            session_name: Session name
            error: Error message

        Returns:
            True if successful
        """
        async with self._lock:
            if session_name not in self._accounts:
                return False

            self._accounts[session_name].status = AccountStatus.ERROR
            self._accounts[session_name]._error_count += 1
            self.logger.error(
                "account_error",
                session_name=session_name,
                error=error,
            )
            return True

    async def set_banned(self, session_name: str) -> bool:
        """
        Set account status to banned.

        Args:
            session_name: Session name

        Returns:
            True if successful
        """
        async with self._lock:
            if session_name not in self._accounts:
                return False

            self._accounts[session_name].status = AccountStatus.BANNED
            self.logger.warning("account_banned", session_name=session_name)
            return True

    def _get_available_accounts(self, require_session: bool = True) -> list[TelegramAccountWrapper]:
        """
        Get list of available accounts.

        Args:
            require_session: Only return accounts with existing session files

        Returns:
            List of available accounts
        """
        accounts = [
            acc
            for acc in self._accounts.values()
            if acc.status in [AccountStatus.IDLE, AccountStatus.ONLINE]
        ]

        if require_session:
            accounts = [acc for acc in accounts if acc.session_exists]

        return accounts

    def _select_account(
        self,
        available: list[TelegramAccountWrapper],
    ) -> TelegramAccountWrapper:
        """
        Select account based on load balancing strategy.

        Args:
            available: List of available accounts

        Returns:
            Selected account
        """
        if self._strategy == "least_used":
            return min(available, key=lambda a: a._message_count)
        elif self._strategy == "round_robin":
            selected = available[self._round_robin_index % len(available)]
            self._round_robin_index += 1
            return selected
        elif self._strategy == "random":
            import random
            return random.choice(available)
        else:
            return available[0]

    async def health_check(self) -> dict:
        """
        Perform health check on all accounts.

        Returns:
            Dictionary with health statistics
        """
        async with self._lock:
            results = {
                "total": len(self._accounts),
                "online": 0,
                "offline": 0,
                "error": 0,
                "banned": 0,
                "working": 0,
                "idle": 0,
                "healthy_ratio": 0.0,
                "with_session": 0,
            }

            total_health = 0.0

            for account in self._accounts.values():
                status_key = account.status.value
                if status_key in results:
                    results[status_key] += 1

                if account.session_exists:
                    results["with_session"] += 1

                if account.status not in [AccountStatus.ERROR, AccountStatus.BANNED]:
                    results["online"] += 1
                    total_health += account.health_score

            if results["online"] > 0:
                results["healthy_ratio"] = total_health / results["online"]

            return results

    async def get_stats(self) -> dict:
        """
        Get detailed statistics about the pool.

        Returns:
            Dictionary with pool statistics
        """
        async with self._lock:
            return {
                "total_accounts": len(self._accounts),
                "available_accounts": len(self._get_available_accounts()),
                "with_sessions": sum(1 for acc in self._accounts.values() if acc.session_exists),
                "strategy": self._strategy,
                "accounts": [
                    {
                        "session_name": acc.session_name,
                        "phone": acc.phone,
                        "country": acc.country_code,
                        "status": acc.status.value,
                        "health_score": acc.health_score,
                        "message_count": acc._message_count,
                        "error_count": acc._error_count,
                        "has_session": acc.session_exists,
                        "has_proxy": acc.current_proxy is not None,
                        "keep_connected": acc.keep_connected,
                    }
                    for acc in self._accounts.values()
                ],
            }

    async def sync_from_db(self, accounts: list["TelegramAccount"]) -> int:
        """
        Sync accounts from database to pool.

        Args:
            accounts: List of TelegramAccount models from database

        Returns:
            Number of accounts synced
        """
        synced = 0
        for account in accounts:
            existing = self._accounts.get(account.session_name)
            
            if existing:
                keep_runtime_status = (
                    existing.keep_connected
                    and existing.client is not None
                    and existing.client.is_connected()
                    and account.status not in [AccountStatus.ERROR, AccountStatus.BANNED]
                )
                if not keep_runtime_status:
                    existing.status = account.status
                existing.fingerprint_id = account.fingerprint_id
                existing.session_string = account.session_string
                existing.api_id = account.api_config.api_id if account.api_config else existing.api_id
                existing.api_hash = account.api_config.api_hash if account.api_config else existing.api_hash
                existing.account_type = getattr(account, "account_type", existing.account_type)
                existing.country_code = account.country_code
                existing.device_model = account.device_model
                existing.system_version = account.system_version
                existing.app_version = account.app_version
            else:
                await self.add_account_from_db(account)
                synced += 1

        self.logger.info("accounts_synced_from_db", count=synced)
        return synced

    async def close_all(self) -> None:
        """Close all account connections and clear the pool."""
        async with self._lock:
            for account in self._accounts.values():
                if account.client is not None:
                    try:
                        if account.client.is_connected():
                            await account.client.disconnect()
                    except Exception as e:
                        self.logger.warning(
                            "error_disconnecting",
                            session_name=account.session_name,
                            error=str(e),
                        )
                    finally:
                        account.client = None

            self._accounts.clear()
            self.logger.info("pool_cleared")

    async def cleanup_sessions(self, dry_run: bool = False) -> list[str]:
        """
        Clean up orphaned session files not in database.

        Args:
            dry_run: If True, only return list without deleting

        Returns:
            List of session files that would be/were deleted
        """
        session_dir = self._ensure_session_dir()
        orphaned = []

        if not session_dir.exists():
            return []

        for session_file in session_dir.glob("*.session"):
            session_name = session_file.stem
            if session_name not in self._accounts:
                orphaned.append(session_name)
                if not dry_run:
                    try:
                        session_file.unlink()
                        self.logger.info("session_file_deleted", session_name=session_name)
                    except Exception as e:
                        self.logger.error(
                            "session_delete_failed",
                            session_name=session_name,
                            error=str(e),
                        )

        if dry_run:
            self.logger.info("session_cleanup_dry_run", orphaned_count=len(orphaned))

        return orphaned

    @property
    def size(self) -> int:
        """Get current pool size."""
        return len(self._accounts)


# Global account pool instance
_account_pool: Optional[AccountPool] = None


def get_account_pool() -> AccountPool:
    """
    Get the global account pool instance.

    Returns:
        AccountPool singleton
    """
    global _account_pool
    if _account_pool is None:
        _account_pool = AccountPool()
    return _account_pool


async def init_account_pool(
    strategy: str = "least_used",
    evomi_api_key: Optional[str] = None,
    decodo_api_key: Optional[str] = None,
) -> AccountPool:
    """
    Initialize the global account pool.

    Args:
        strategy: Load balancing strategy
        evomi_api_key: Optional Evomi API key
        decodo_api_key: Optional Decodo API key

    Returns:
        Initialized AccountPool
    """
    global _account_pool
    _account_pool = AccountPool(strategy=strategy)

    from app.core.config import get_settings
    settings = get_settings()
    provider = getattr(settings, "PROXY_PROVIDER", "evomi").lower()

    if provider == "decodo" and decodo_api_key:
        from app.core.account.decodo import init_decodo_client
        decodo_client = await init_decodo_client(decodo_api_key)
        _account_pool.set_decodo_client(decodo_client)
    elif provider == "evomi" and evomi_api_key:
        from app.core.account.evomi import init_evomi_client
        evomi_client = await init_evomi_client(evomi_api_key)
        _account_pool.set_evomi_client(evomi_client)

    return _account_pool


async def close_account_pool() -> None:
    """Close the global account pool."""
    global _account_pool
    if _account_pool is not None:
        await _account_pool.close_all()
        _account_pool = None

    from app.core.account.decodo import close_decodo_client
    from app.core.account.evomi import close_evomi_client
    await close_evomi_client()
    await close_decodo_client()
