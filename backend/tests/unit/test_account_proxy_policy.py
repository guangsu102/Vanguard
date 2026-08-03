import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.accounts import _ensure_static_proxy_capacity, _propagate_account_proxy_policy_change
from app.core.account.models import (
    AccountStatus,
    AccountType,
    Proxy,
    ProxyMode,
    ProxyType,
    TelegramAccount,
)


@pytest.mark.asyncio
async def test_static_proxy_capacity_allows_three_accounts(test_db):
    proxy = Proxy(
        proxy_type=ProxyType.RESIDENTIAL,
        host="127.0.0.1",
        port=1080,
        protocol="socks5",
        country="US",
        is_active=True,
    )
    test_db.add(proxy)
    await test_db.flush()

    for idx in range(3):
        test_db.add(
            TelegramAccount(
                phone=f"+1555000000{idx}",
                identifier=f"+1555000000{idx}",
                account_type=AccountType.PROMOTER,
                api_config_name="default",
                country_code="US",
                session_name=f"capacity_session_{idx}",
                proxy_mode=ProxyMode.STATIC,
                static_proxy_id=proxy.id,
                status=AccountStatus.OFFLINE,
            )
        )
    await test_db.commit()

    with pytest.raises(HTTPException) as exc:
        await _ensure_static_proxy_capacity(test_db, proxy.id)

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_static_proxy_capacity_excludes_current_account(test_db):
    proxy = Proxy(
        proxy_type=ProxyType.RESIDENTIAL,
        host="127.0.0.2",
        port=1080,
        protocol="socks5",
        country="US",
        is_active=True,
    )
    test_db.add(proxy)
    await test_db.flush()

    accounts = []
    for idx in range(3):
        account = TelegramAccount(
            phone=f"+1555000010{idx}",
            identifier=f"+1555000010{idx}",
            account_type=AccountType.PROMOTER,
            api_config_name="default",
            country_code="US",
            session_name=f"capacity_exclude_session_{idx}",
            proxy_mode=ProxyMode.STATIC,
            static_proxy_id=proxy.id,
            status=AccountStatus.OFFLINE,
        )
        accounts.append(account)
        test_db.add(account)
    await test_db.commit()

    await _ensure_static_proxy_capacity(test_db, proxy.id, exclude_account_id=accounts[0].id)


@pytest.mark.asyncio
async def test_proxy_policy_change_invalidates_locally_before_publish(monkeypatch):
    accounts_api = importlib.import_module("app.api.accounts")
    calls = []

    async def invalidate(account_id: int, *, reason: str) -> int:
        calls.append(("invalidate", account_id, reason))
        return 1

    async def publish(account_id: int, proxy_mode: str, static_proxy_id: int | None):
        calls.append(("publish", account_id, proxy_mode, static_proxy_id))

    monkeypatch.setattr(accounts_api, "invalidate_account_in_all_pools", invalidate)
    monkeypatch.setattr(accounts_api, "publish_account_proxy_policy_changed", publish)
    account = SimpleNamespace(
        id=42,
        proxy_mode=ProxyMode.STATIC,
        static_proxy_id=9,
    )

    await _propagate_account_proxy_policy_change(account)

    assert calls == [
        ("invalidate", 42, "proxy_policy_updated"),
        ("publish", 42, ProxyMode.STATIC.value, 9),
    ]
