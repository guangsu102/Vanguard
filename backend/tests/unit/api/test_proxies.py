import importlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.proxies import (
    ProxyUpdate,
    _proxy_to_response,
    delete_proxy,
    list_proxies,
    proxy_health,
    toggle_proxy,
    update_proxy,
)
from app.core.account.models import (
    AccountStatus,
    AccountType,
    Proxy,
    ProxyMode,
    ProxyType,
    TelegramAccount,
)
from app.core.account.proxy_resolver import MAX_STATIC_PROXY_BINDINGS, resolve_static_proxy

proxies_api = importlib.import_module("app.api.proxies")


def _proxy(
    *,
    is_active: bool,
    consecutive_failures: int,
    proxy_id: int | None = 1,
    success_rate: float | None = None,
    avg_latency: int = 0,
) -> Proxy:
    now = datetime.utcnow()
    return Proxy(
        id=proxy_id,
        proxy_type=ProxyType.DATACENTER,
        host="127.0.0.1",
        port=8080,
        protocol="http",
        country="US",
        is_active=is_active,
        success_rate=((0 if consecutive_failures else 1) if success_rate is None else success_rate),
        avg_latency=avg_latency,
        consecutive_failures=consecutive_failures,
        created_at=now,
        updated_at=now,
    )


def test_proxy_response_keeps_failed_inactive_proxy_inactive():
    response = _proxy_to_response(_proxy(is_active=False, consecutive_failures=3))

    assert response.status == "inactive"


def test_proxy_response_keeps_manually_inactive_proxy_inactive():
    response = _proxy_to_response(_proxy(is_active=False, consecutive_failures=0))

    assert response.status == "inactive"


def test_proxy_response_marks_failed_active_proxy_as_error():
    response = _proxy_to_response(_proxy(is_active=True, consecutive_failures=3))

    assert response.status == "error"


@pytest.mark.asyncio
async def test_update_proxy_response_keeps_bound_accounts(test_db, monkeypatch):
    proxy = _proxy(is_active=True, consecutive_failures=3, proxy_id=None)
    test_db.add(proxy)
    await test_db.flush()
    account = TelegramAccount(
        phone="+15550009999",
        identifier="+15550009999",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="proxy_update_binding",
        proxy_mode=ProxyMode.STATIC,
        static_proxy_id=proxy.id,
        status=AccountStatus.OFFLINE,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    invalidate = AsyncMock()
    monkeypatch.setattr(proxies_api, "_invalidate_bound_static_accounts", invalidate)

    response = await update_proxy(proxy.id, ProxyUpdate(status="active"), test_db)

    payload = response["data"]
    assert payload.status == "active"
    assert payload.bindAccountCount == 1
    assert payload.maxBindAccounts == MAX_STATIC_PROXY_BINDINGS
    assert payload.bindAccounts[0]["id"] == account.id
    assert payload.remainingBindSlots == MAX_STATIC_PROXY_BINDINGS - 1
    invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_toggle_enabling_clears_historical_health_failures(test_db, monkeypatch):
    proxy = _proxy(is_active=False, consecutive_failures=1365, proxy_id=None)
    test_db.add(proxy)
    await test_db.commit()
    invalidate = AsyncMock()
    monkeypatch.setattr(proxies_api, "_invalidate_bound_static_accounts", invalidate)

    response = await toggle_proxy(proxy.id, test_db)
    await test_db.refresh(proxy)

    assert response["data"]["is_active"] is True
    assert proxy.is_active is True
    assert proxy.consecutive_failures == 0
    assert (await resolve_static_proxy(test_db, proxy.id)).proxy_id == proxy.id


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "toggle"])
async def test_proxy_mutation_refreshes_binding_added_after_commit(
    operation,
    test_db,
    monkeypatch,
):
    proxy = _proxy(is_active=True, consecutive_failures=0, proxy_id=None)
    account = TelegramAccount(
        phone=f"+1555000888{1 if operation == 'update' else 2}",
        identifier=f"+1555000888{1 if operation == 'update' else 2}",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name=f"post_commit_binding_{operation}",
        proxy_mode=ProxyMode.DYNAMIC,
        static_proxy_id=None,
        status=AccountStatus.OFFLINE,
    )
    test_db.add_all([proxy, account])
    await test_db.commit()

    original_commit = test_db.commit
    binding_added = False

    async def commit_then_add_binding():
        nonlocal binding_added
        await original_commit()
        if not binding_added:
            binding_added = True
            account.proxy_mode = ProxyMode.STATIC
            account.static_proxy_id = proxy.id
            await original_commit()

    monkeypatch.setattr(test_db, "commit", commit_then_add_binding)
    invalidate = AsyncMock()
    monkeypatch.setattr(proxies_api, "_invalidate_bound_static_accounts", invalidate)

    if operation == "update":
        await update_proxy(proxy.id, ProxyUpdate(country="ca"), test_db)
    else:
        await toggle_proxy(proxy.id, test_db)

    refreshed_accounts = invalidate.await_args.args[0]
    assert [item.id for item in refreshed_accounts] == [account.id]
    assert refreshed_accounts[0].static_proxy_id == proxy.id


@pytest.mark.asyncio
async def test_delete_proxy_locks_proxy_before_binding_snapshot():
    statements = []

    class _MissingResult:
        def scalar_one_or_none(self):
            return None

    class _FakeDb:
        async def execute(self, statement):
            statements.append(statement)
            return _MissingResult()

    with pytest.raises(HTTPException) as exc:
        await delete_proxy(91, _FakeDb())

    assert exc.value.status_code == 404
    assert statements[0]._for_update_arg is not None


@pytest.mark.asyncio
async def test_delete_proxy_reloads_affected_accounts_by_id_after_commit(
    test_db,
    monkeypatch,
):
    proxy = _proxy(is_active=True, consecutive_failures=0, proxy_id=None)
    test_db.add(proxy)
    await test_db.flush()
    account = TelegramAccount(
        phone="+15550007777",
        identifier="+15550007777",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="deleted_proxy_current_policy",
        proxy_mode=ProxyMode.STATIC,
        static_proxy_id=proxy.id,
        status=AccountStatus.OFFLINE,
    )
    test_db.add(account)
    await test_db.commit()

    current_account = SimpleNamespace(
        id=account.id,
        proxy_mode=ProxyMode.DYNAMIC,
        static_proxy_id=None,
    )
    reload_accounts = AsyncMock(return_value=[current_account])
    invalidate = AsyncMock()
    monkeypatch.setattr(proxies_api, "get_accounts_by_ids", reload_accounts)
    monkeypatch.setattr(proxies_api, "_invalidate_bound_static_accounts", invalidate)

    await delete_proxy(proxy.id, test_db)

    reload_accounts.assert_awaited_once_with(
        test_db,
        [account.id],
        populate_existing=True,
    )
    assert invalidate.await_args.args[0] == [current_account]
    assert invalidate.await_args.kwargs["reason"] == "static_proxy_deleted"


@pytest.mark.asyncio
async def test_proxy_health_excludes_inactive_and_failed_proxies_from_averages(test_db):
    test_db.add_all(
        [
            _proxy(
                is_active=False,
                consecutive_failures=1365,
                proxy_id=None,
                success_rate=0,
                avg_latency=190,
            ),
            _proxy(
                is_active=True,
                consecutive_failures=0,
                proxy_id=None,
                success_rate=1,
                avg_latency=400,
            ),
            _proxy(
                is_active=True,
                consecutive_failures=3,
                proxy_id=None,
                success_rate=0.2,
                avg_latency=900,
            ),
        ]
    )
    await test_db.commit()

    response = await proxy_health(test_db)

    assert response.data["total"] == 3
    assert response.data["active"] == 1
    assert response.data["inactive"] == 1
    assert response.data["error"] == 1
    assert response.data["avg_success_rate"] == 100
    assert response.data["avg_latency_ms"] == 400


@pytest.mark.asyncio
async def test_proxy_error_filter_excludes_inactive_failed_proxy(test_db):
    test_db.add_all(
        [
            _proxy(
                is_active=False,
                consecutive_failures=1365,
                proxy_id=None,
            ),
            _proxy(
                is_active=True,
                consecutive_failures=3,
                proxy_id=None,
            ),
        ]
    )
    await test_db.commit()

    response = await list_proxies(status="error", db=test_db)

    assert response.data["total"] == 1
    assert response.data["list"][0].status == "error"
