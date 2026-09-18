from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy import update

from app.core.account import pool as account_pool_module
from app.core.account import proxy_policy_events
from app.core.account.models import (
    AccountStatus,
    AccountType,
    Proxy,
    ProxyMode,
    ProxyType,
    TelegramAccount,
)
from app.core.network import proxy_pool as proxy_pool_module
from app.core.network.proxy_pool import ProxyPool


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeSession:
    def __init__(self, status: int):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, *args, **kwargs):
        return _FakeResponse(self.status)


def _account(phone: str, *, proxy_id: int | None) -> TelegramAccount:
    return TelegramAccount(
        phone=phone,
        identifier=phone,
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name=f"proxy_refresh_{phone.removeprefix('+')}",
        proxy_mode=ProxyMode.STATIC if proxy_id is not None else ProxyMode.DYNAMIC,
        static_proxy_id=proxy_id,
        status=AccountStatus.OFFLINE,
    )


@pytest.mark.asyncio
async def test_static_proxy_refresh_invalidates_and_publishes_every_bound_account(
    test_db,
    monkeypatch,
):
    proxy = Proxy(
        proxy_type=ProxyType.RESIDENTIAL,
        host="127.0.0.10",
        port=1080,
        protocol="socks5",
        country="US",
        is_active=True,
    )
    other_proxy = Proxy(
        proxy_type=ProxyType.RESIDENTIAL,
        host="127.0.0.11",
        port=1080,
        protocol="socks5",
        country="US",
        is_active=True,
    )
    test_db.add_all([proxy, other_proxy])
    await test_db.flush()
    bound_accounts = [
        _account("+15551000001", proxy_id=proxy.id),
        _account("+15551000002", proxy_id=proxy.id),
    ]
    unrelated_account = _account("+15551000003", proxy_id=other_proxy.id)
    test_db.add_all([*bound_accounts, unrelated_account])
    await test_db.commit()

    calls = []

    async def invalidate(account_id: int, *, reason: str) -> int:
        calls.append(("invalidate", account_id, reason))
        return 1

    async def publish(account_id: int, proxy_mode: str, static_proxy_id: int | None):
        calls.append(("publish", account_id, proxy_mode, static_proxy_id))

    monkeypatch.setattr(
        account_pool_module,
        "invalidate_account_in_all_pools",
        invalidate,
    )
    monkeypatch.setattr(
        proxy_policy_events,
        "publish_account_proxy_policy_changed",
        publish,
    )

    refreshed = await proxy_policy_events.refresh_static_proxy_bindings(
        test_db,
        proxy.id,
        reason="static_proxy_health_unavailable",
    )

    assert refreshed == 2
    assert calls == [
        (
            "invalidate",
            bound_accounts[0].id,
            "static_proxy_health_unavailable",
        ),
        ("publish", bound_accounts[0].id, ProxyMode.STATIC.value, proxy.id),
        (
            "invalidate",
            bound_accounts[1].id,
            "static_proxy_health_unavailable",
        ),
        ("publish", bound_accounts[1].id, ProxyMode.STATIC.value, proxy.id),
    ]


@pytest.mark.asyncio
async def test_account_reload_by_id_populates_current_proxy_policy(test_db):
    proxy = Proxy(
        proxy_type=ProxyType.RESIDENTIAL,
        host="127.0.0.12",
        port=1080,
        protocol="socks5",
        country="US",
        is_active=True,
    )
    test_db.add(proxy)
    await test_db.flush()
    account = _account("+15551000004", proxy_id=proxy.id)
    test_db.add(account)
    await test_db.commit()

    await test_db.execute(
        update(TelegramAccount)
        .where(TelegramAccount.id == account.id)
        .values(proxy_mode=ProxyMode.DYNAMIC.value, static_proxy_id=None)
        .execution_options(synchronize_session=False)
    )
    await test_db.commit()
    assert account.proxy_mode == ProxyMode.STATIC
    assert account.static_proxy_id == proxy.id

    reloaded = await proxy_policy_events.get_accounts_by_ids(
        test_db,
        [account.id],
        populate_existing=True,
    )

    assert reloaded == [account]
    assert account.proxy_mode == ProxyMode.DYNAMIC
    assert account.static_proxy_id is None


@pytest.mark.asyncio
async def test_health_check_retries_refresh_while_unhealthy_and_once_on_recovery(
    test_db,
    monkeypatch,
):
    pool = ProxyPool(test_db)
    proxy = await pool.add_proxy(
        ProxyType.DATACENTER,
        "127.0.0.20",
        8080,
        "US",
    )
    proxy.consecutive_failures = 2
    await test_db.commit()
    await pool.sync_from_db()

    response_status = {"value": 503}
    monkeypatch.setattr(
        proxy_pool_module.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(response_status["value"]),
    )
    refresh = AsyncMock(return_value=1)
    monkeypatch.setattr(
        proxy_pool_module,
        "refresh_static_proxy_bindings",
        refresh,
    )

    await pool.health_check(proxy.id)
    await pool.health_check(proxy.id)
    response_status["value"] = 200
    await pool.health_check(proxy.id)

    assert refresh.await_args_list == [
        call(
            test_db,
            proxy.id,
            reason="static_proxy_health_unavailable",
        ),
        call(
            test_db,
            proxy.id,
            reason="static_proxy_health_unavailable",
        ),
        call(
            test_db,
            proxy.id,
            reason="static_proxy_health_recovered",
        ),
    ]


@pytest.mark.asyncio
async def test_refresh_attempts_every_account_before_raising_publish_failure(monkeypatch):
    accounts = [
        SimpleNamespace(id=101, proxy_mode=ProxyMode.STATIC, static_proxy_id=7),
        SimpleNamespace(id=102, proxy_mode=ProxyMode.STATIC, static_proxy_id=7),
    ]
    calls = []

    async def invalidate(account_id: int, *, reason: str) -> int:
        calls.append(("invalidate", account_id, reason))
        return 1

    async def publish(account_id: int, proxy_mode: str, static_proxy_id: int | None):
        calls.append(("publish", account_id, proxy_mode, static_proxy_id))
        if account_id == 101:
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(
        account_pool_module,
        "invalidate_account_in_all_pools",
        invalidate,
    )
    monkeypatch.setattr(
        proxy_policy_events,
        "publish_account_proxy_policy_changed",
        publish,
    )

    with pytest.raises(RuntimeError, match="Failed to refresh 1"):
        await proxy_policy_events.refresh_bound_static_accounts(
            accounts,
            reason="static_proxy_health_unavailable",
        )

    assert calls == [
        ("invalidate", 101, "static_proxy_health_unavailable"),
        ("publish", 101, ProxyMode.STATIC.value, 7),
        ("invalidate", 102, "static_proxy_health_unavailable"),
        ("publish", 102, ProxyMode.STATIC.value, 7),
    ]


@pytest.mark.asyncio
async def test_failed_refresh_retries_after_pool_is_recreated(test_db, monkeypatch):
    pool = ProxyPool(test_db)
    proxy = await pool.add_proxy(
        ProxyType.DATACENTER,
        "127.0.0.30",
        8080,
        "US",
    )
    proxy.consecutive_failures = 2
    await test_db.commit()
    await pool.sync_from_db()

    monkeypatch.setattr(
        proxy_pool_module.aiohttp,
        "ClientSession",
        lambda *args, **kwargs: _FakeSession(503),
    )
    first_refresh = AsyncMock(side_effect=RuntimeError("redis unavailable"))
    monkeypatch.setattr(
        proxy_pool_module,
        "refresh_static_proxy_bindings",
        first_refresh,
    )

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await pool.health_check(proxy.id)
    await test_db.refresh(proxy)
    assert proxy.consecutive_failures == 3
    first_refresh.assert_awaited_once_with(
        test_db,
        proxy.id,
        reason="static_proxy_health_unavailable",
    )

    retry_pool = ProxyPool(test_db)
    await retry_pool.sync_from_db()
    retry_refresh = AsyncMock(return_value=1)
    monkeypatch.setattr(
        proxy_pool_module,
        "refresh_static_proxy_bindings",
        retry_refresh,
    )

    await retry_pool.health_check(proxy.id)
    retry_refresh.assert_awaited_once_with(
        test_db,
        proxy.id,
        reason="static_proxy_health_unavailable",
    )