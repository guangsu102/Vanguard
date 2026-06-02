"""
Unit Tests for Account Manager Module

Tests cover:
- API configuration CRUD
- Account CRUD operations
- Account status management
- Account filtering and listing
- Session management
- Health statistics
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.manager import AccountManager
from app.core.account.models import AccountStatus, TelegramAccount, TelegramAPIConfig
from app.core.account.exceptions import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    InvalidAPIConfigError,
)


class TestAPIConfigManagement:
    """Test API configuration management."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            return AccountManager(test_db)

    @pytest.mark.asyncio
    async def test_create_api_config(self, manager):
        """Test creating API configuration."""
        config = await manager.create_api_config(
            name="test_config",
            api_id="12345",
            api_hash="test_hash",
            description="Test configuration",
        )

        assert config.name == "test_config"
        assert config.api_id == "12345"
        assert config.api_hash == "test_hash"
        assert config.description == "Test configuration"
        assert config.account_count == 0

    @pytest.mark.asyncio
    async def test_create_duplicate_api_config(self, manager):
        """Test creating duplicate API config raises error."""
        await manager.create_api_config(
            name="duplicate",
            api_id="12345",
            api_hash="test_hash",
        )

        with pytest.raises(InvalidAPIConfigError, match="already exists"):
            await manager.create_api_config(
                name="duplicate",
                api_id="12345",
                api_hash="test_hash",
            )

    @pytest.mark.asyncio
    async def test_get_api_config(self, manager):
        """Test getting API configuration."""
        await manager.create_api_config(
            name="get_test",
            api_id="12345",
            api_hash="test_hash",
        )

        config = await manager.get_api_config("get_test")

        assert config is not None
        assert config.name == "get_test"

    @pytest.mark.asyncio
    async def test_get_api_config_not_found(self, manager):
        """Test getting non-existent API config."""
        config = await manager.get_api_config("nonexistent")
        assert config is None

    @pytest.mark.asyncio
    async def test_list_api_configs(self, manager):
        """Test listing API configurations."""
        await manager.create_api_config(name="config1", api_id="1", api_hash="h1")
        await manager.create_api_config(name="config2", api_id="2", api_hash="h2")

        configs = await manager.list_api_configs()

        assert len(configs) == 2
        names = {c.name for c in configs}
        assert "config1" in names
        assert "config2" in names

    @pytest.mark.asyncio
    async def test_delete_api_config(self, manager):
        """Test deleting API configuration."""
        await manager.create_api_config(name="to_delete", api_id="1", api_hash="h")

        result = await manager.delete_api_config("to_delete")

        assert result is True
        assert await manager.get_api_config("to_delete") is None

    @pytest.mark.asyncio
    async def test_delete_api_config_in_use(self, manager):
        """Test deleting API config that is in use raises error."""
        await manager.create_api_config(name="in_use", api_id="1", api_hash="h")
        await manager.create_account(
            phone="+1234567890",
            api_config_name="in_use",
        )

        with pytest.raises(ValueError, match="accounts are using it"):
            await manager.delete_api_config("in_use")


class TestAccountCreation:
    """Test account creation."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_create_account(self, manager):
        """Test creating a new account."""
        account = await manager.create_account(phone="+1234567890")

        assert account.id is not None
        assert account.phone == "+1234567890"
        assert account.api_config_name == "default"
        assert account.status == AccountStatus.OFFLINE
        assert account.country_code == "US"

    @pytest.mark.asyncio
    async def test_create_account_with_custom_config(self, manager):
        """Test creating account with custom API config."""
        await manager.create_api_config(
            name="custom",
            api_id="54321",
            api_hash="custom_hash",
        )

        account = await manager.create_account(
            phone="+9876543210",
            api_config_name="custom",
        )

        assert account.api_config_name == "custom"

    @pytest.mark.asyncio
    async def test_create_account_with_country(self, manager):
        """Test creating account with country info."""
        account = await manager.create_account(
            phone="+8613800138000",
            country_code="CN",
            country_name="China",
        )

        assert account.country_code == "CN"
        assert account.country_name == "China"

    @pytest.mark.asyncio
    async def test_create_account_with_custom_session(self, manager):
        """Test creating account with custom session name."""
        account = await manager.create_account(
            phone="+1111111111",
            session_name="my_custom_session",
        )

        assert account.session_name == "my_custom_session"

    @pytest.mark.asyncio
    async def test_create_duplicate_account(self, manager):
        """Test creating duplicate account raises error."""
        await manager.create_account(phone="+1234567890")

        with pytest.raises(AccountAlreadyExistsError):
            await manager.create_account(phone="+1234567890")

    @pytest.mark.asyncio
    async def test_create_account_invalid_config(self, manager):
        """Test creating account with invalid API config raises error."""
        with pytest.raises(InvalidAPIConfigError, match="Configuration not found"):
            await manager.create_account(
                phone="+1234567890",
                api_config_name="nonexistent",
            )


class TestAccountRetrieval:
    """Test account retrieval methods."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_get_account_by_id(self, manager):
        """Test getting account by ID."""
        created = await manager.create_account(phone="+1234567890")

        account = await manager.get_account(created.id)

        assert account is not None
        assert account.phone == "+1234567890"

    @pytest.mark.asyncio
    async def test_get_account_by_phone(self, manager):
        """Test getting account by phone."""
        await manager.create_account(phone="+1234567890")

        account = await manager.get_account_by_phone("+1234567890")

        assert account is not None
        assert account.phone == "+1234567890"

    @pytest.mark.asyncio
    async def test_get_account_by_session(self, manager):
        """Test getting account by session name."""
        await manager.create_account(
            phone="+1234567890",
            session_name="test_session",
        )

        account = await manager.get_account_by_session("test_session")

        assert account is not None
        assert account.phone == "+1234567890"

    @pytest.mark.asyncio
    async def test_get_nonexistent_account(self, manager):
        """Test getting non-existent account returns None."""
        account = await manager.get_account(99999)
        assert account is None

        account = await manager.get_account_by_phone("+9999999999")
        assert account is None


class TestAccountListing:
    """Test account listing and filtering."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_list_all_accounts(self, manager):
        """Test listing all accounts."""
        await manager.create_account(phone="+1111111111")
        await manager.create_account(phone="+2222222222")

        accounts = await manager.list_accounts()

        assert len(accounts) >= 2

    @pytest.mark.asyncio
    async def test_list_accounts_with_status_filter(self, manager):
        """Test listing accounts with status filter."""
        account = await manager.create_account(phone="+1111111111")
        await manager.update_account_status(account.id, AccountStatus.ONLINE)

        online_accounts = await manager.list_accounts(status=AccountStatus.ONLINE)
        offline_accounts = await manager.list_accounts(status=AccountStatus.OFFLINE)

        assert len(online_accounts) >= 1
        assert all(a.status == AccountStatus.ONLINE for a in online_accounts)
        assert all(a.status == AccountStatus.OFFLINE for a in offline_accounts)

    @pytest.mark.asyncio
    async def test_list_accounts_with_country_filter(self, manager):
        """Test listing accounts with country filter."""
        await manager.create_account(phone="+1111111111", country_code="US")
        await manager.create_account(phone="+2222222222", country_code="CN")

        us_accounts = await manager.list_accounts(country_code="US")
        cn_accounts = await manager.list_accounts(country_code="CN")

        assert len(us_accounts) >= 1
        assert all(a.country_code == "US" for a in us_accounts)
        assert all(a.country_code == "CN" for a in cn_accounts)

    @pytest.mark.asyncio
    async def test_list_accounts_with_pagination(self, manager):
        """Test listing accounts with pagination."""
        for i in range(5):
            await manager.create_account(phone=f"+111111111{i}")

        first_page = await manager.list_accounts(limit=2, offset=0)
        second_page = await manager.list_accounts(limit=2, offset=2)

        assert len(first_page) == 2
        assert len(second_page) == 2


class TestAccountStatus:
    """Test account status management."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_update_account_status(self, manager):
        """Test updating account status."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.update_account_status(account.id, AccountStatus.ONLINE)

        assert updated.status == AccountStatus.ONLINE
        assert updated.last_active_at is not None
        assert updated.last_connected_at is not None
        assert updated.connection_count == 1

    @pytest.mark.asyncio
    async def test_update_status_to_online_increments_count(self, manager):
        """Test status update to ONLINE increments connection count."""
        account = await manager.create_account(phone="+1234567890")
        await manager.update_account_status(account.id, AccountStatus.ONLINE)

        updated = await manager.update_account_status(account.id, AccountStatus.ONLINE)

        assert updated.connection_count == 2

    @pytest.mark.asyncio
    async def test_update_status_not_found(self, manager):
        """Test updating non-existent account raises error."""
        with pytest.raises(AccountNotFoundError):
            await manager.update_account_status(99999, AccountStatus.ONLINE)

    @pytest.mark.asyncio
    async def test_get_account_status(self, manager):
        """Test getting account status."""
        account = await manager.create_account(phone="+1234567890")

        status = await manager.get_account_status(account.id)

        assert status == AccountStatus.OFFLINE

    @pytest.mark.asyncio
    async def test_get_account_status_not_found(self, manager):
        """Test getting status of non-existent account."""
        with pytest.raises(AccountNotFoundError):
            await manager.get_account_status(99999)


class TestAccountUpdate:
    """Test account update operations."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_update_account_country_code(self, manager):
        """Test updating account country code."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.update_account(account.id, country_code="JP")

        assert updated.country_code == "JP"

    @pytest.mark.asyncio
    async def test_update_account_fingerprint(self, manager):
        """Test updating account fingerprint."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.update_account(account.id, fingerprint_id="fp123")

        assert updated.fingerprint_id == "fp123"

    @pytest.mark.asyncio
    async def test_update_account_active_status(self, manager):
        """Test updating account active status."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.update_account(account.id, is_active=False)

        assert updated.is_active is False

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self, manager):
        """Test updating multiple fields at once."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.update_account(
            account.id,
            country_code="UK",
            fingerprint_id="fp456",
            is_active=True,
        )

        assert updated.country_code == "UK"
        assert updated.fingerprint_id == "fp456"
        assert updated.is_active is True

    @pytest.mark.asyncio
    async def test_update_nonexistent_account(self, manager):
        """Test updating non-existent account raises error."""
        with pytest.raises(AccountNotFoundError):
            await manager.update_account(99999, country_code="US")


class TestSessionManagement:
    """Test session management operations."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_record_session_info(self, manager):
        """Test recording session device information."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.record_session_info(
            account.id,
            device_model="iPhone 15",
            system_version="iOS 17.0",
            app_version="8.5.0",
        )

        assert updated.device_model == "iPhone 15"
        assert updated.system_version == "iOS 17.0"
        assert updated.app_version == "8.5.0"

    @pytest.mark.asyncio
    async def test_bind_fingerprint(self, manager):
        """Test binding device fingerprint."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.bind_fingerprint(account.id, "fp789")

        assert updated.fingerprint_id == "fp789"


class TestAccountDeletion:
    """Test account deletion."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_delete_account(self, manager):
        """Test deleting an account."""
        account = await manager.create_account(phone="+1234567890")
        account_id = account.id

        result = await manager.delete_account(account_id)

        assert result is True
        assert await manager.get_account(account_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_account(self, manager):
        """Test deleting non-existent account raises error."""
        with pytest.raises(AccountNotFoundError):
            await manager.delete_account(99999)


class TestErrorRecording:
    """Test error recording."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_record_error(self, manager):
        """Test recording account error."""
        account = await manager.create_account(phone="+1234567890")

        updated = await manager.record_error(account.id)

        assert updated.error_count == 1

    @pytest.mark.asyncio
    async def test_record_multiple_errors(self, manager):
        """Test recording multiple errors."""
        account = await manager.create_account(phone="+1234567890")
        await manager.record_error(account.id)

        updated = await manager.record_error(account.id)

        assert updated.error_count == 2


class TestStatistics:
    """Test statistics methods."""

    @pytest_asyncio.fixture
    async def manager(self, test_db: AsyncSession):
        """Create AccountManager with test database."""
        with patch('app.core.account.manager.get_settings') as mock_settings:
            mock_settings.return_value = MagicMock()
            mgr = AccountManager(test_db)
            await mgr.create_api_config(
                name="default",
                api_id="12345",
                api_hash="test_hash",
            )
            return mgr

    @pytest.mark.asyncio
    async def test_get_account_health_stats(self, manager):
        """Test getting account health statistics."""
        await manager.create_account(phone="+1111111111")
        await manager.create_account(phone="+2222222222")
        account = await manager.create_account(phone="+3333333333")
        await manager.update_account_status(account.id, AccountStatus.ONLINE)

        stats = await manager.get_account_health_stats()

        assert "total" in stats
        assert "online" in stats
        assert "offline" in stats
        assert stats["total"] >= 3

    @pytest.mark.asyncio
    async def test_get_country_distribution(self, manager):
        """Test getting country distribution."""
        await manager.create_account(phone="+1111111111", country_code="US")
        await manager.create_account(phone="+2222222222", country_code="US")
        await manager.create_account(phone="+3333333333", country_code="CN")

        distribution = await manager.get_country_distribution()

        assert "US" in distribution
        assert "CN" in distribution
        assert distribution["US"] == 2
        assert distribution["CN"] == 1
