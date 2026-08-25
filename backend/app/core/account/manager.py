"""
Account Manager Module

Manages Telegram account lifecycle, connection, and health monitoring.

Responsible for:
- Creating, updating, and deleting Telegram accounts
- Managing API configurations (multiple configs supported)
- Session persistence for auto-relogin
- Health monitoring
"""

from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.exceptions import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    InvalidAPIConfigError,
)
from app.core.account.models import AccountAssetTier, AccountStatus, AccountType, ProxyMode, TelegramAccount, TelegramAPIConfig
from app.core.config import get_settings
from app.core.network.fingerprint import FingerprintManager

logger = structlog.get_logger()


class AccountManager:
    """
    Telegram account lifecycle manager.

    Provides CRUD operations for Telegram accounts and manages
    API configuration bindings. Works with database session.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize AccountManager with database session.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self.logger = logger.bind(module="account_manager")
        self.settings = get_settings()

    async def create_api_config(
        self,
        name: str,
        api_id: str,
        api_hash: str,
        description: Optional[str] = None,
    ) -> TelegramAPIConfig:
        """
        Create a new Telegram API configuration.

        Args:
            name: Configuration name (unique identifier)
            api_id: Telegram API ID
            api_hash: Telegram API Hash
            description: Optional description

        Returns:
            Created TelegramAPIConfig instance
        """
        existing = await self.db.execute(
            select(TelegramAPIConfig).where(TelegramAPIConfig.name == name)
        )
        if existing.scalar_one_or_none():
            raise InvalidAPIConfigError(name, "Configuration with this name already exists")

        config = TelegramAPIConfig(
            name=name,
            api_id=api_id,
            api_hash=api_hash,
            description=description,
        )

        self.db.add(config)
        await self.db.commit()
        await self.db.refresh(config)

        self.logger.info("api_config_created", name=name, api_id=api_id)
        return config

    async def get_api_config(self, name: str) -> Optional[TelegramAPIConfig]:
        """
        Get API configuration by name.

        Args:
            name: Configuration name

        Returns:
            TelegramAPIConfig if found, None otherwise
        """
        result = await self.db.execute(
            select(TelegramAPIConfig).where(TelegramAPIConfig.name == name)
        )
        return result.scalar_one_or_none()

    async def list_api_configs(self) -> list[TelegramAPIConfig]:
        """
        List all API configurations.

        Returns:
            List of TelegramAPIConfig instances
        """
        result = await self.db.execute(select(TelegramAPIConfig))
        return list(result.scalars().all())

    async def delete_api_config(self, name: str) -> bool:
        """
        Delete an API configuration.

        Args:
            name: Configuration name

        Returns:
            True if deleted, False if not found

        Raises:
            ValueError: If configuration is in use by accounts
        """
        config = await self.get_api_config(name)
        if not config:
            return False

        if config.account_count > 0:
            raise ValueError(
                f"Cannot delete config '{name}': {config.account_count} accounts are using it"
            )

        await self.db.delete(config)
        await self.db.commit()
        self.logger.info("api_config_deleted", name=name)
        return True

    async def create_account(
        self,
        phone: Optional[str],
        api_config_name: str = "default",
        country_code: str = "US",
        country_name: Optional[str] = None,
        session_name: Optional[str] = None,
        *,
        identifier: Optional[str] = None,
        display_name: Optional[str] = None,
        profile_bio: Optional[str] = None,
        asset_tier: str = AccountAssetTier.UNKNOWN.value,
        registered_at: Optional[datetime] = None,
        asset_note: Optional[str] = None,
        managed_started_at: Optional[datetime] = None,
        warmup_hold_until: Optional[datetime] = None,
        warmup_note: Optional[str] = None,
        account_type: AccountType = AccountType.PROMOTER,
        proxy_mode: ProxyMode = ProxyMode.DYNAMIC,
        static_proxy_id: Optional[int] = None,
    ) -> TelegramAccount:
        """
        Create a new Telegram account.

        Args:
            phone: Phone number with country code
            api_config_name: Name of API configuration to use
            country_code: ISO 3166-1 alpha-2 country code
            country_name: Full country name
            session_name: Optional custom session name

        Returns:
            Created TelegramAccount instance

        Raises:
            AccountAlreadyExistsError: If phone already registered
            InvalidAPIConfigError: If API config not found
        """
        resolved_identifier = (identifier or phone or "").strip()
        if not resolved_identifier:
            raise ValueError("identifier or phone is required")

        existing_query = select(TelegramAccount).where(TelegramAccount.identifier == resolved_identifier)
        existing = await self.db.execute(existing_query)
        if existing.scalar_one_or_none():
            raise AccountAlreadyExistsError(resolved_identifier)

        config = await self.get_api_config(api_config_name)
        if not config:
            raise InvalidAPIConfigError(api_config_name, "Configuration not found")

        if session_name is None:
            session_seed = resolved_identifier.replace("+", "").replace("@", "").replace(" ", "_")
            session_name = f"session_{session_seed}"

        device_profile = FingerprintManager().generate_telegram_device_profile(resolved_identifier or session_name)

        account = TelegramAccount(
            phone=phone,
            account_type=account_type,
            identifier=resolved_identifier,
            display_name=display_name,
            profile_bio=profile_bio,
            asset_tier=asset_tier,
            registered_at=registered_at,
            asset_verified_at=datetime.utcnow() if asset_tier != AccountAssetTier.UNKNOWN.value else None,
            asset_note=asset_note,
            managed_started_at=managed_started_at or datetime.utcnow(),
            warmup_stage_updated_at=datetime.utcnow(),
            warmup_hold_until=warmup_hold_until,
            warmup_note=warmup_note,
            api_config_name=api_config_name,
            country_code=country_code.upper()[:2],
            country_name=country_name,
            session_name=session_name,
            proxy_mode=proxy_mode,
            static_proxy_id=static_proxy_id,
            fingerprint_id=device_profile["fingerprint_id"],
            device_model=device_profile["device_model"],
            system_version=device_profile["system_version"],
            app_version=device_profile["app_version"],
            status=AccountStatus.OFFLINE,
        )

        self.db.add(account)
        
        config.account_count += 1
        self.db.add(config)
        
        await self.db.commit()
        await self.db.refresh(account)

        self.logger.info(
            "account_created",
            account_id=account.id,
            identifier=resolved_identifier,
            session_name=session_name,
            country=country_code,
        )

        return account

    async def get_account(self, account_id: int) -> Optional[TelegramAccount]:
        """
        Get account by ID.

        Args:
            account_id: Account database ID

        Returns:
            TelegramAccount if found, None otherwise
        """
        result = await self.db.execute(
            select(TelegramAccount)
            .options(selectinload(TelegramAccount.static_proxy))
            .where(TelegramAccount.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_account_by_phone(self, phone: str) -> Optional[TelegramAccount]:
        """
        Get account by phone number.

        Args:
            phone: Phone number

        Returns:
            TelegramAccount if found, None otherwise
        """
        result = await self.db.execute(
            select(TelegramAccount).where(TelegramAccount.phone == phone)
        )
        return result.scalar_one_or_none()

    async def get_account_by_session(self, session_name: str) -> Optional[TelegramAccount]:
        """
        Get account by session name.

        Args:
            session_name: Session name

        Returns:
            TelegramAccount if found, None otherwise
        """
        result = await self.db.execute(
            select(TelegramAccount).where(TelegramAccount.session_name == session_name)
        )
        return result.scalar_one_or_none()

    async def list_accounts(
        self,
        status: Optional[AccountStatus] = None,
        account_type: Optional[AccountType] = None,
        country_code: Optional[str] = None,
        api_config_name: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TelegramAccount]:
        """
        List accounts with optional filters.

        Args:
            status: Optional status filter
            country_code: Optional country code filter
            api_config_name: Optional API config filter
            limit: Maximum number of results
            offset: Offset for pagination

        Returns:
            List of TelegramAccount instances
        """
        query = select(TelegramAccount).options(selectinload(TelegramAccount.static_proxy))

        if status:
            query = query.where(TelegramAccount.status == status)
        if account_type:
            query = query.where(TelegramAccount.account_type == account_type)
        if country_code:
            query = query.where(TelegramAccount.country_code == country_code.upper())
        if api_config_name:
            query = query.where(TelegramAccount.api_config_name == api_config_name)

        query = query.limit(limit).offset(offset).order_by(TelegramAccount.created_at)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_account_status(self, account_id: int) -> AccountStatus:
        """
        Get account status by ID.

        Args:
            account_id: Account database ID

        Returns:
            Current AccountStatus

        Raises:
            AccountNotFoundError: If account not found
        """
        account = await self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(str(account_id))
        return account.status

    async def update_account_status(
        self,
        account_id: int,
        status: AccountStatus,
    ) -> TelegramAccount:
        """
        Update account status.

        Args:
            account_id: Account database ID
            status: New status

        Returns:
            Updated TelegramAccount

        Raises:
            AccountNotFoundError: If account not found
        """
        account = await self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(str(account_id))

        old_status = account.status
        account.status = status
        account.last_active_at = datetime.utcnow()

        if status == AccountStatus.ONLINE:
            account.last_connected_at = datetime.utcnow()
            account.connection_count += 1

        await self.db.commit()
        await self.db.refresh(account)

        self.logger.info(
            "account_status_updated",
            account_id=account_id,
            old_status=old_status.value,
            new_status=status.value,
        )

        return account

    async def update_account(
        self,
        account_id: int,
        display_name: Optional[str] = None,
        profile_bio: Optional[str] = None,
        asset_tier: Optional[str] = None,
        registered_at: Optional[datetime] = None,
        asset_note: Optional[str] = None,
        managed_started_at: Optional[datetime] = None,
        warmup_hold_until: Optional[datetime] = None,
        warmup_note: Optional[str] = None,
        country_code: Optional[str] = None,
        fingerprint_id: Optional[str] = None,
        is_active: Optional[bool] = None,
        proxy_mode: Optional[ProxyMode] = None,
        static_proxy_id: Optional[int] = None,
    ) -> TelegramAccount:
        """
        Update account settings.

        Args:
            account_id: Account database ID
            country_code: New country code
            fingerprint_id: New fingerprint ID
            is_active: Active status

        Returns:
            Updated TelegramAccount

        Raises:
            AccountNotFoundError: If account not found
        """
        account = await self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(str(account_id))

        if country_code is not None:
            account.country_code = country_code.upper()[:2]
        if display_name is not None:
            account.display_name = display_name
        if profile_bio is not None:
            account.profile_bio = profile_bio
            account.profile_bio_synced_at = None
        if asset_tier is not None:
            if account.asset_tier != asset_tier:
                account.asset_verified_at = datetime.utcnow()
            account.asset_tier = asset_tier
        if registered_at is not None:
            account.registered_at = registered_at
        if asset_note is not None:
            account.asset_note = asset_note
        if managed_started_at is not None:
            account.managed_started_at = managed_started_at
            account.warmup_stage_updated_at = datetime.utcnow()
        if warmup_hold_until is not None:
            account.warmup_hold_until = warmup_hold_until
        if warmup_note is not None:
            account.warmup_note = warmup_note
        if fingerprint_id is not None:
            account.fingerprint_id = fingerprint_id
        if is_active is not None:
            account.is_active = is_active
        if proxy_mode is not None:
            account.proxy_mode = proxy_mode
        if static_proxy_id is not None:
            account.static_proxy_id = static_proxy_id

        await self.db.commit()
        await self.db.refresh(account)

        self.logger.info("account_updated", account_id=account_id)
        return account

    async def record_session_info(
        self,
        account_id: int,
        device_model: Optional[str] = None,
        system_version: Optional[str] = None,
        app_version: Optional[str] = None,
    ) -> TelegramAccount:
        """
        Record device information for session persistence.
        This allows auto-relogin without re-authentication.

        Args:
            account_id: Account database ID
            device_model: Device model string
            system_version: System version string
            app_version: App version string

        Returns:
            Updated TelegramAccount
        """
        account = await self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(str(account_id))

        if device_model:
            account.device_model = device_model
        if system_version:
            account.system_version = system_version
        if app_version:
            account.app_version = app_version

        await self.db.commit()
        await self.db.refresh(account)

        self.logger.info("session_info_recorded", account_id=account_id)
        return account

    async def bind_fingerprint(
        self,
        account_id: int,
        fingerprint_id: str,
    ) -> TelegramAccount:
        """
        Bind device fingerprint to account.

        Args:
            account_id: Account database ID
            fingerprint_id: Fingerprint ID

        Returns:
            Updated TelegramAccount

        Raises:
            AccountNotFoundError: If account not found
        """
        account = await self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(str(account_id))

        account.fingerprint_id = fingerprint_id
        await self.db.commit()
        await self.db.refresh(account)

        self.logger.info(
            "fingerprint_bound",
            account_id=account_id,
            fingerprint_id=fingerprint_id,
        )
        return account

    async def delete_account(self, account_id: int) -> bool:
        """
        Delete an account.

        Args:
            account_id: Account database ID

        Returns:
            True if deleted successfully

        Raises:
            AccountNotFoundError: If account not found
        """
        account = await self.get_account(account_id)
        if not account:
            raise AccountNotFoundError(str(account_id))

        api_config = await self.get_api_config(account.api_config_name)
        if api_config:
            api_config.account_count = max(0, api_config.account_count - 1)
            self.db.add(api_config)

        await self.db.delete(account)
        await self.db.commit()

        self.logger.info("account_deleted", account_id=account_id)
        return True

    async def record_error(self, account_id: int) -> TelegramAccount:
        """
        Record an error for the account.

        Args:
            account_id: Account database ID

        Returns:
            Updated TelegramAccount
        """
        account = await self.get_account(account_id)
        if account:
            account.error_count += 1
            await self.db.commit()
            await self.db.refresh(account)
        return account

    async def get_account_health_stats(self) -> dict:
        """
        Get health statistics for all accounts.

        Returns:
            Dictionary with health statistics
        """
        result = await self.db.execute(
            select(
                TelegramAccount.status,
                func.count(TelegramAccount.id).label("count"),
            ).group_by(TelegramAccount.status)
        )
        
        stats = {
            "total": 0,
            "online": 0,
            "offline": 0,
            "error": 0,
            "banned": 0,
            "working": 0,
            "idle": 0,
        }

        for row in result.all():
            status = row[0].value if hasattr(row[0], 'value') else str(row[0])
            count = row[1]
            stats[status] = count
            stats["total"] += count

        return stats

    async def get_country_distribution(self) -> dict[str, int]:
        """
        Get account distribution by country.

        Returns:
            Dictionary mapping country codes to account counts
        """
        result = await self.db.execute(
            select(
                TelegramAccount.country_code,
                func.count(TelegramAccount.id).label("count"),
            ).group_by(TelegramAccount.country_code)
        )

        return {row[0]: row[1] for row in result.all()}
