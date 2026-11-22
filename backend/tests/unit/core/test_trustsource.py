from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.account.evomi import ProxyInfo
from app.core.account.exceptions import ProxyProviderError
from app.core.account.models import AccountType, ProxyMode, ProxyType
from app.core.account.pool import AccountPool
from app.core.account.trustsource import TrustSourceClient


def proxy_row(
    proxy_id: int,
    *,
    country: str = "US",
    username: str | None = "user",
    password: str | None = "pass",
):
    return SimpleNamespace(
        id=proxy_id,
        proxy_type=ProxyType.RESIDENTIAL,
        host="global.rotgb.trustsource.live",
        port=10000,
        protocol="http",
        username=username,
        password=password,
        country=country,
    )


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _query):
        return FakeResult(self.rows)


def client_for(rows):
    @asynccontextmanager
    async def session_factory():
        yield FakeSession(rows)

    return TrustSourceClient(session_factory=session_factory)


@pytest.mark.asyncio
async def test_prefers_exact_country_and_keeps_account_assignment_stable():
    client = client_for(
        [
            proxy_row(16, country="US"),
            proxy_row(17, country="US"),
            proxy_row(18, country="CA"),
        ]
    )

    first = await client.get_proxy_for_account("CA", account_key="account-33")
    second = await client.get_proxy_for_account("CA", account_key="account-33")

    assert [item.session_id for item in first] == ["trustsource-18"]
    assert [item.session_id for item in second] == ["trustsource-18"]
    assert first[0].host == "global.rotgb.trustsource.live"


@pytest.mark.asyncio
async def test_falls_back_to_healthy_inventory_when_country_has_no_row():
    client = client_for([proxy_row(16), proxy_row(17)])

    proxies = await client.get_proxy_for_account("MM", account_key="account-35", count=2)

    assert len(proxies) == 2
    assert {item.session_id for item in proxies} == {"trustsource-16", "trustsource-17"}
    assert all(isinstance(item, ProxyInfo) for item in proxies)


@pytest.mark.asyncio
async def test_raises_provider_error_when_inventory_is_empty():
    client = client_for([])

    with pytest.raises(ProxyProviderError, match="No healthy TrustSource proxy"):
        await client.get_proxy_for_account("US", account_key="account-2")


@pytest.mark.asyncio
async def test_skips_rows_without_credentials_before_assignment():
    client = client_for(
        [
            proxy_row(16, username=None),
            proxy_row(17),
        ]
    )

    proxies = await client.get_proxy_for_account("US", account_key="account-2", count=3)

    assert [item.session_id for item in proxies] == ["trustsource-17"]


def test_exclusive_assignment_keeps_active_slots_and_resolves_collisions():
    rows = [proxy_row(proxy_id) for proxy_id in range(17, 26)]
    roster = [
        (2, "online", "+19513799309"),
        (4, "online", "+17857563372"),
        (5, "idle", "guardian_managed_8563774152"),
        (31, "idle", "guardian_managed_8219475794"),
        (32, "idle", "guardian_managed_8903772056"),
        (33, "online", "+12492080346"),
        (34, "online", "+959777138691"),
        (35, "online", "+959794419799"),
    ]

    assignments = TrustSourceClient._exclusive_assignments(rows, roster, 33)

    assert len(assignments) == len(roster)
    assert len({proxy.id for proxy in assignments.values()}) == len(roster)
    assert assignments[2].id == 19
    assert assignments[33].id == 24
    assert assignments[34].id == 25
    assert assignments[35].id == 21
    assert len({assignments[item_id].id for item_id in (5, 31, 32)}) == 3
    assert {assignments[item_id].id for item_id in (5, 31, 32)}.isdisjoint(
        {19, 21, 23, 24, 25}
    )


@pytest.mark.asyncio
async def test_account_pool_uses_trustsource_for_dynamic_accounts(monkeypatch):
    class FakeTrustSource:
        async def get_proxy_for_account(
            self,
            country_code,
            count=1,
            account_key=None,
            account_id=None,
        ):
            assert country_code == "US"
            assert account_key == "+15550000002"
            assert account_id == 2
            return [
                ProxyInfo(
                    protocol="http",
                    host="global.rotgb.trustsource.live",
                    port=10000,
                    username="user",
                    password="pass",
                    session_id="trustsource-16",
                )
            ]

    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(PROXY_PROVIDER="trustsource", PROMOTER_PROXY_REQUIRED=True),
    )
    monkeypatch.setattr(
        "app.core.account.trustsource.get_trustsource_client",
        lambda: FakeTrustSource(),
    )

    pool = AccountPool()
    account = await pool.add_account(
        account_id=2,
        phone="+15550000002",
        session_name="trustsource_dynamic",
        country_code="US",
        api_id="12345",
        api_hash="hash",
        session_string="session",
        account_type=AccountType.PROMOTER,
        proxy_mode=ProxyMode.DYNAMIC,
    )

    proxy = await pool._acquire_proxy(account)

    assert proxy is not None
    assert proxy.session_id == "trustsource-16"
