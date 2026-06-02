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
