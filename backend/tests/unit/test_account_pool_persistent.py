import time
from types import SimpleNamespace

import pytest

from app.core.account.evomi import ProxyInfo
from app.core.account.models import AccountStatus, AccountType
from app.core.account.pool import AccountPool


class FakeClient:
    def __init__(self) -> None:
        self.disconnected = False

    def is_connected(self) -> bool:
        return not self.disconnected

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeEvomiProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def get_proxy_for_account(
        self,
        country_code: str,
        count: int = 1,
        account_key: str | None = None,
    ) -> list[ProxyInfo]:
        self.calls.append((country_code, account_key))
        call_no = len(self.calls)
        return [
            ProxyInfo(
                protocol="http",
                host="rp.evomi.test",
                port=1000,
                username="user",
                password=f"pass_session-{call_no}",
                session_id=f"session{call_no}",
                expires_at=time.time() + 60,
            )
        ]


@pytest.mark.asyncio
async def test_release_keeps_persistent_listener_client_connected():
    pool = AccountPool()
    account = await pool.add_account(
        account_id=1,
        phone="+10000000001",
        session_name="listener_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
    )
    client = FakeClient()
    account.client = client
    account.keep_connected = True
    account.status = AccountStatus.WORKING

    await pool.release(account)

    assert client.disconnected is False
    assert account.client is client
    assert account.status == AccountStatus.IDLE


@pytest.mark.asyncio
async def test_release_disconnects_non_persistent_client():
    pool = AccountPool()
    account = await pool.add_account(
        account_id=2,
        phone="+10000000002",
        session_name="short_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
    )
    client = FakeClient()
    account.client = client
    account.status = AccountStatus.WORKING

    await pool.release(account)

    assert client.disconnected is True
    assert account.client is None
    assert account.status == AccountStatus.IDLE


@pytest.mark.asyncio
async def test_sync_from_db_preserves_connected_listener_runtime_status():
    pool = AccountPool()
    account = await pool.add_account(
        account_id=3,
        phone="+10000000003",
        session_name="synced_listener",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
    )
    account.client = FakeClient()
    account.keep_connected = True
    account.status = AccountStatus.IDLE

    db_account = SimpleNamespace(
        id=3,
        phone="+10000000003",
        session_name="synced_listener",
        country_code="US",
        api_config=SimpleNamespace(api_id="67890", api_hash="new_hash"),
        api_config_name="default",
        fingerprint_id=None,
        session_string="new_session",
        device_model=None,
        system_version=None,
        app_version=None,
        status=AccountStatus.OFFLINE,
    )

    await pool.sync_from_db([db_account])

    assert account.status == AccountStatus.IDLE
    assert account.api_id == "67890"
    assert account.session_string == "new_session"


@pytest.mark.asyncio
async def test_promoter_reuses_sticky_proxy_until_expired(monkeypatch):
    settings = SimpleNamespace(PROXY_PROVIDER="evomi", PROMOTER_PROXY_REQUIRED=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    pool = AccountPool()
    provider = FakeEvomiProvider()
    pool.set_evomi_client(provider)
    account = await pool.add_account(
        account_id=4,
        phone="+10000000004",
        session_name="sticky_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        account_type=AccountType.PROMOTER,
    )

    await pool._ensure_proxy(account)
    first_proxy = account.current_proxy
    await pool._ensure_proxy(account)

    assert provider.calls == [("US", "+10000000004")]
    assert account.current_proxy is first_proxy

    account.current_proxy.expires_at = time.time() - 1
    await pool._ensure_proxy(account)

    assert len(provider.calls) == 2
    assert account.current_proxy is not first_proxy
    assert account.current_proxy.session_id == "session2"


@pytest.mark.asyncio
async def test_guardian_bot_does_not_require_proxy(monkeypatch):
    settings = SimpleNamespace(PROXY_PROVIDER="evomi", PROMOTER_PROXY_REQUIRED=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    pool = AccountPool()
    account = await pool.add_account(
        account_id=5,
        phone="",
        session_name="guardian_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        account_type=AccountType.GUARDIAN_BOT,
    )

    async def fail_if_called(_account):
        raise AssertionError("guardian bot should not acquire a promoter proxy")

    pool._acquire_proxy = fail_if_called
    await pool._ensure_proxy(account)

    assert account.current_proxy is None
