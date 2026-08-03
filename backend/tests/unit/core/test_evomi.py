from types import SimpleNamespace

import pytest

from app.core.account.evomi import EvomiClient, ProxyInfo


def test_proxy_info_parses_auth_prefixed_url():
    proxy = ProxyInfo.from_url("http://user:pass@proxy.evomi.com:1000")

    assert proxy.protocol == "http"
    assert proxy.host == "proxy.evomi.com"
    assert proxy.port == 1000
    assert proxy.username == "user"
    assert proxy.password == "pass"


def test_proxy_info_parses_protocol_host_first_format():
    proxy = ProxyInfo.from_url("http://proxy.evomi.com:1000:user:pass")

    assert proxy.protocol == "http"
    assert proxy.host == "proxy.evomi.com"
    assert proxy.port == 1000
    assert proxy.username == "user"
    assert proxy.password == "pass"


def test_proxy_info_parses_host_first_format():
    proxy = ProxyInfo.from_url("proxy.evomi.com:1000:user:pass")

    assert proxy.protocol == "http"
    assert proxy.host == "proxy.evomi.com"
    assert proxy.port == 1000
    assert proxy.username == "user"
    assert proxy.password == "pass"


def test_proxy_info_parses_auth_first_format():
    proxy = ProxyInfo.from_url("user:pass:proxy.evomi.com:1000")

    assert proxy.protocol == "http"
    assert proxy.host == "proxy.evomi.com"
    assert proxy.port == 1000
    assert proxy.username == "user"
    assert proxy.password == "pass"


@pytest.mark.parametrize(
    ("phone", "expected_country"),
    [
        ("+8613800000000", "CN"),
        ("+14155552671", "US"),
        ("+81312345678", "JP"),
        ("+33123456789", "FR"),
        ("004930123456", "DE"),
    ],
)
def test_evomi_infers_country_from_global_phone_numbers(monkeypatch, phone, expected_country):
    settings = SimpleNamespace(
        EVOMI_API_KEY="key",
        EVOMI_PRODUCT_CODE="rp",
        EVOMI_PROTOCOL="http",
        EVOMI_SESSION_TYPE="sticky",
        EVOMI_SESSION_LIFETIME_MINUTES=30,
        EVOMI_SESSION_NAMESPACE="tests",
        EVOMI_ADBLOCK=False,
    )
    monkeypatch.setattr("app.core.account.evomi.get_settings", lambda: settings)

    client = EvomiClient()

    assert client.country_code_from_phone(phone) == expected_country


@pytest.mark.asyncio
async def test_evomi_builds_stable_account_sticky_proxy(monkeypatch):
    settings = SimpleNamespace(
        EVOMI_API_KEY="key",
        EVOMI_PRODUCT_CODE="rp",
        EVOMI_PROTOCOL="http",
        EVOMI_SESSION_TYPE="sticky",
        EVOMI_SESSION_LIFETIME_MINUTES=30,
        EVOMI_SESSION_NAMESPACE="tests",
        EVOMI_ADBLOCK=False,
    )
    monkeypatch.setattr("app.core.account.evomi.get_settings", lambda: settings)
    client = EvomiClient()
    client._proxy_data = {
        "products": {
            "rp": {
                "username": "evomi_user",
                "password": "evomi_pass",
                "endpoint": "rp.evomi.com",
                "ports": {"http": 1000},
            }
        }
    }

    first = await client.get_proxy_for_account("us", account_key="+15550000001")
    second = await client.get_proxy_for_account("us", account_key="+15550000001")

    assert first[0].session_id == second[0].session_id
    assert len(first[0].session_id) == 8
    assert first[0].host == "rp.evomi.com"
    assert first[0].port == 1000
    assert first[0].password.startswith("evomi_pass_country-US_session-")
    assert first[0].password.endswith("_lifetime-30")
    assert first[0].expires_at is not None


@pytest.mark.asyncio
async def test_evomi_static_gateway_does_not_require_api_key(monkeypatch):
    settings = SimpleNamespace(
        EVOMI_API_KEY=None,
        EVOMI_PROXY_HOST="premium-residential.evomi.com",
        EVOMI_PROXY_PORT=1000,
        EVOMI_PROXY_USERNAME="evomi_user",
        EVOMI_PROXY_PASSWORD="evomi_password_session-fixed",
        EVOMI_PROTOCOL="http",
        EVOMI_PRODUCT_CODE="rp",
        EVOMI_SESSION_TYPE="sticky",
        EVOMI_SESSION_LIFETIME_MINUTES=30,
        EVOMI_SESSION_NAMESPACE="tests",
        EVOMI_ADBLOCK=False,
    )
    monkeypatch.setattr("app.core.account.evomi.get_settings", lambda: settings)

    client = EvomiClient()
    proxies = await client.get_proxy_for_account("us", account_key="+15550000002")

    assert proxies[0].protocol == "http"
    assert proxies[0].host == "premium-residential.evomi.com"
    assert proxies[0].port == 1000
    assert proxies[0].username == "evomi_user"
    assert proxies[0].password.startswith("evomi_password_country-US_session-")
    assert proxies[0].password.endswith("_lifetime-30")
    assert proxies[0].session_id == client.sticky_session_id("+15550000002")
    assert proxies[0].expires_at is not None


@pytest.mark.asyncio
async def test_evomi_static_gateway_infers_china_from_phone_and_rebuilds_sticky_password(monkeypatch):
    settings = SimpleNamespace(
        EVOMI_API_KEY=None,
        EVOMI_PROXY_HOST="premium-residential.evomi.com",
        EVOMI_PROXY_PORT=1000,
        EVOMI_PROXY_USERNAME="evomi_user",
        EVOMI_PROXY_PASSWORD="evomi_password_session-fixed",
        EVOMI_PROTOCOL="http",
        EVOMI_PRODUCT_CODE="rp",
        EVOMI_SESSION_TYPE="sticky",
        EVOMI_SESSION_LIFETIME_MINUTES=30,
        EVOMI_SESSION_NAMESPACE="tests",
        EVOMI_ADBLOCK=False,
    )
    monkeypatch.setattr("app.core.account.evomi.get_settings", lambda: settings)

    client = EvomiClient()
    proxies = await client.get_proxy_for_account("US", account_key="+8613800000000")

    assert proxies[0].password.startswith("evomi_password_country-CN_session-")
    assert proxies[0].password.endswith("_lifetime-30")
    assert proxies[0].session_id == client.sticky_session_id("+8613800000000")
    assert proxies[0].expires_at is not None


@pytest.mark.asyncio
async def test_evomi_static_gateway_verifies_country_and_retries_sticky_session(monkeypatch):
    settings = SimpleNamespace(
        EVOMI_API_KEY=None,
        EVOMI_PROXY_HOST="premium-residential.evomi.com",
        EVOMI_PROXY_PORT=1000,
        EVOMI_PROXY_USERNAME="evomi_user",
        EVOMI_PROXY_PASSWORD="evomi_password_session-fixed",
        EVOMI_PROTOCOL="http",
        EVOMI_PRODUCT_CODE="rp",
        EVOMI_SESSION_TYPE="sticky",
        EVOMI_SESSION_LIFETIME_MINUTES=30,
        EVOMI_SESSION_NAMESPACE="tests",
        EVOMI_ADBLOCK=False,
        EVOMI_COUNTRY_VERIFY_ENABLED=True,
        EVOMI_COUNTRY_VERIFY_ATTEMPTS=3,
        EVOMI_COUNTRY_VERIFY_TIMEOUT_SECONDS=8,
        EVOMI_COUNTRY_VERIFY_URL="http://ip-api.test/json",
    )
    monkeypatch.setattr("app.core.account.evomi.get_settings", lambda: settings)

    client = EvomiClient()
    checked_session_ids: list[str | None] = []

    async def fake_proxy_matches_country(proxy: ProxyInfo, target_country: str) -> bool:
        checked_session_ids.append(proxy.session_id)
        assert target_country == "CN"
        return len(checked_session_ids) == 2

    monkeypatch.setattr(client, "proxy_matches_country", fake_proxy_matches_country)

    proxies = await client.get_proxy_for_account("US", account_key="+8613800000000")

    assert checked_session_ids == [
        client.sticky_session_id("+8613800000000", attempt=0, country_code="CN"),
        client.sticky_session_id("+8613800000000", attempt=1, country_code="CN"),
    ]
    assert proxies[0].session_id == checked_session_ids[1]
    assert proxies[0].password.startswith("evomi_password_country-CN_session-")
