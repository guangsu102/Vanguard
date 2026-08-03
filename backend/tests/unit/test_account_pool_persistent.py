import time
from types import SimpleNamespace

import pytest

from app.core.account.evomi import ProxyInfo
from app.core.account.models import AccountStatus, AccountType, ProxyMode
from app.core.account.pool import AccountPool, invalidate_account_in_all_pools
from app.core.account.proxy_policy_events import ProxyPolicyState
from app.core.account.proxy_resolver import ResolvedProxy


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
async def test_invalidate_account_disconnects_and_evicts_every_local_pool():
    pool = AccountPool()
    account = await pool.add_account(
        account_id=12,
        phone="+10000000012",
        session_name="invalidate_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
    )
    client = FakeClient()
    account.client = client
    account.current_proxy = ProxyInfo(
        protocol="http",
        host="old.proxy.test",
        port=1000,
        username=None,
        password=None,
    )

    invalidated = await invalidate_account_in_all_pools(12, reason="test_policy_change")

    assert invalidated >= 1
    assert client.disconnected is True
    assert account.client is None
    assert account.current_proxy is None
    assert await pool.get_account_by_id(12) is None


@pytest.mark.asyncio
async def test_acquire_fails_closed_when_published_policy_is_newer(monkeypatch):
    async def load_policy(_account_id: int) -> ProxyPolicyState:
        return ProxyPolicyState(
            account_id=13,
            version=2,
            proxy_mode=ProxyMode.STATIC.value,
            static_proxy_id=77,
        )

    monkeypatch.setattr(
        "app.core.account.pool.get_account_proxy_policy_state",
        load_policy,
    )
    pool = AccountPool()
    account = await pool.add_account(
        account_id=13,
        phone="+10000000013",
        session_name="stale_policy_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        proxy_mode=ProxyMode.DYNAMIC,
        proxy_policy_version=1,
    )
    client = FakeClient()
    account.client = client

    with pytest.raises(RuntimeError, match="Proxy policy changed"):
        await pool.acquire_by_id(13, purpose="stale_policy_test")

    assert client.disconnected is True
    assert await pool.get_account_by_id(13) is None


@pytest.mark.asyncio
async def test_add_account_from_db_refreshes_existing_static_proxy_policy(monkeypatch):
    async def load_policy(_account_id: int) -> ProxyPolicyState:
        return ProxyPolicyState(
            account_id=14,
            version=4,
            proxy_mode=ProxyMode.STATIC.value,
            static_proxy_id=88,
        )

    monkeypatch.setattr(
        "app.core.account.pool.get_account_proxy_policy_state",
        load_policy,
    )
    pool = AccountPool()
    account = await pool.add_account(
        account_id=14,
        phone="+10000000014",
        session_name="refresh_static_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        proxy_mode=ProxyMode.DYNAMIC,
    )
    client = FakeClient()
    account.client = client
    db_account = SimpleNamespace(
        id=14,
        phone="+10000000014",
        session_name="refresh_static_session",
        country_code="US",
        api_config=SimpleNamespace(api_id="12345", api_hash="hash"),
        api_config_name="default",
        fingerprint_id=None,
        session_string="session",
        account_type=AccountType.PROMOTER,
        proxy_mode=ProxyMode.STATIC,
        static_proxy_id=88,
        static_proxy=SimpleNamespace(
            id=88,
            protocol="socks5",
            host="static.proxy.test",
            port=1080,
            username="user",
            password="pass",
        ),
        device_model=None,
        system_version=None,
        app_version=None,
        status=AccountStatus.ONLINE,
    )

    refreshed = await pool.add_account_from_db(db_account)

    assert refreshed is account
    assert client.disconnected is True
    assert account.client is None
    assert account.proxy_mode == ProxyMode.STATIC
    assert account.static_proxy_id == 88
    assert account.proxy_policy_version == 4


@pytest.mark.asyncio
async def test_add_account_from_db_falls_back_to_env_api_credentials(monkeypatch):
    settings = SimpleNamespace(TELEGRAM_API_ID="24680", TELEGRAM_API_HASH="env_hash")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    pool = AccountPool()
    db_account = SimpleNamespace(
        id=6,
        phone="+10000000006",
        session_name="env_api_session",
        country_code="US",
        api_config=None,
        api_config_name="default",
        fingerprint_id=None,
        session_string="session",
        account_type=AccountType.PROMOTER,
        device_model=None,
        system_version=None,
        app_version=None,
    )

    account = await pool.add_account_from_db(db_account)

    assert account.api_id == "24680"
    assert account.api_hash == "env_hash"


@pytest.mark.asyncio
async def test_create_client_reports_missing_api_credentials():
    pool = AccountPool()
    account = await pool.add_account(
        account_id=7,
        phone="+10000000007",
        session_name="missing_api_session",
        country_code="US",
        api_id="",
        api_hash="",
        session_string="session",
    )

    with pytest.raises(RuntimeError, match="Telegram API credentials missing"):
        await pool._create_client(account)


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


@pytest.mark.asyncio
async def test_static_proxy_is_used_without_provider(monkeypatch):
    settings = SimpleNamespace(PROXY_PROVIDER="evomi", PROMOTER_PROXY_REQUIRED=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    pool = AccountPool()
    provider = FakeEvomiProvider()
    pool.set_evomi_client(provider)
    account = await pool.add_account(
        account_id=8,
        phone="+10000000008",
        session_name="static_proxy_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        account_type=AccountType.PROMOTER,
        proxy_mode=ProxyMode.STATIC,
        static_proxy_id=10,
        static_proxy=ResolvedProxy(
            protocol="socks5",
            host="static.proxy.test",
            port=1080,
            username="user",
            password="pass",
            proxy_id=10,
        ),
    )

    await pool._ensure_proxy(account)

    assert provider.calls == []
    assert account.current_proxy is not None
    assert account.current_proxy.host == "static.proxy.test"
    assert account.current_proxy.session_id == "static-10"


@pytest.mark.asyncio
async def test_proxy_mode_none_skips_promoter_proxy(monkeypatch):
    settings = SimpleNamespace(PROXY_PROVIDER="evomi", PROMOTER_PROXY_REQUIRED=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    pool = AccountPool()
    provider = FakeEvomiProvider()
    pool.set_evomi_client(provider)
    account = await pool.add_account(
        account_id=9,
        phone="+10000000009",
        session_name="no_proxy_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        account_type=AccountType.PROMOTER,
        proxy_mode=ProxyMode.NONE,
    )

    await pool._ensure_proxy(account)

    assert provider.calls == []
    assert account.current_proxy is None


@pytest.mark.asyncio
async def test_static_proxy_resolver_fallback_is_used(monkeypatch):
    settings = SimpleNamespace(PROXY_PROVIDER="evomi", PROMOTER_PROXY_REQUIRED=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    async def resolve_static(proxy_id: int) -> ResolvedProxy:
        return ResolvedProxy(
            protocol="http",
            host=f"static-{proxy_id}.proxy.test",
            port=8080,
            proxy_id=proxy_id,
        )

    pool = AccountPool(static_proxy_resolver=resolve_static)
    account = await pool.add_account(
        account_id=10,
        phone="+10000000010",
        session_name="static_resolver_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        account_type=AccountType.PROMOTER,
        proxy_mode=ProxyMode.STATIC,
        static_proxy_id=42,
    )

    await pool._ensure_proxy(account)

    assert account.current_proxy is not None
    assert account.current_proxy.host == "static-42.proxy.test"

@pytest.mark.asyncio
async def test_create_client_uses_stable_telegram_device_profile(monkeypatch, tmp_path):
    settings = SimpleNamespace(TELEGRAM_SESSION_DIR=str(tmp_path))
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    captured = {}

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured["session"] = session
            captured["api_id"] = api_id
            captured["api_hash"] = api_hash
            captured.update(kwargs)

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

    monkeypatch.setattr("app.core.account.pool.TelegramClient", FakeTelegramClient)

    pool = AccountPool()
    account = await pool.add_account(
        account_id=11,
        phone="+10000000011",
        session_name="stable_fingerprint_session",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        fingerprint_id="stable-fp-key",
    )

    client = await pool._create_client(account)

    assert isinstance(client, FakeTelegramClient)
    assert captured["device_model"] == account.device_model
    assert captured["system_version"] == account.system_version
    assert captured["app_version"] == account.app_version
    assert captured["lang_code"] == captured["system_lang_code"]

    first_profile = {
        "device_model": account.device_model,
        "system_version": account.system_version,
        "app_version": account.app_version,
        "lang_code": captured["lang_code"],
    }

    account.client = None
    await pool._create_client(account)

    assert first_profile == {
        "device_model": account.device_model,
        "system_version": account.system_version,
        "app_version": account.app_version,
        "lang_code": captured["lang_code"],
    }
