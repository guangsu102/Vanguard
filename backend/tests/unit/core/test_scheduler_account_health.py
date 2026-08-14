"""Regression tests for scheduled Telegram account health checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_health_check_accounts_excludes_inactive_accounts(monkeypatch):
    """Disabled accounts must never be loaded into the health-check pool."""
    from app.core.account.models import AccountStatus
    from app.core.scheduler import tasks

    active_account = SimpleNamespace(
        id=1,
        is_active=True,
        phone="+10000000001",
        status=AccountStatus.ONLINE,
    )
    inactive_account = SimpleNamespace(
        id=2,
        is_active=False,
        phone="+10000000002",
        status=AccountStatus.ONLINE,
    )
    manager = MagicMock()
    manager.list_accounts = AsyncMock(return_value=[active_account, inactive_account])
    pool = MagicMock()
    pool.sync_from_db = AsyncMock()
    pool.health_check = AsyncMock(return_value={"online": 1})
    pool.close_all = AsyncMock()

    async def run_with_test_db(handler):
        return await handler(MagicMock())

    monkeypatch.setattr("app.core.account.manager.AccountManager", lambda _db: manager)
    monkeypatch.setattr("app.core.account.pool.AccountPool", lambda: pool)
    monkeypatch.setattr(
        "app.core.account.pool._resolve_account_api_credentials",
        lambda _account: (12345, "api-hash"),
    )
    monkeypatch.setattr(tasks, "_run_with_db", run_with_test_db)

    result = await tasks._health_check_accounts_async()

    pool.sync_from_db.assert_awaited_once_with([active_account])
    pool.health_check.assert_awaited_once_with()
    pool.close_all.assert_awaited_once_with()
    assert result["checked"] == 1
    assert result["healthy"] == 1
    assert result["unhealthy"] == 0
    assert result["skipped_inactive"] == 1
