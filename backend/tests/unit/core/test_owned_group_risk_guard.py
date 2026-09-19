from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountRiskLevel, AccountStatus
from app.core.account.risk_guard import (
    ACCOUNT_WIDE_GROUP_WRITE_ACTIONS,
    AccountRiskAction,
    AccountRiskGuard,
)
from app.core.account.telegram_execution import TelegramExecutionService


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.client = self

    async def get(self, key: str):
        return self.values.get(key)

    async def incr(self, key: str, amount: int = 1):
        self.values[key] = int(self.values.get(key, 0)) + amount
        return self.values[key]

    async def expire(self, key: str, ttl: int):
        return True

    async def set(self, key: str, value, ttl=None):
        self.values[key] = value
        return True

    async def delete(self, key: str):
        return 1 if self.values.pop(key, None) is not None else 0


def _settings() -> dict:
    return {
        "enabled": True,
        "account_outbound_message_hard_cap_default": 30,
        "actions": {
            "owned_group_message": {
                "daily_limit": 999,
                "cooldown_seconds": 999,
            }
        },
        "lifecycle": {},
    }


def test_owned_group_budget_cannot_be_overridden() -> None:
    budget = AccountRiskGuard._budget_for_action(
        AccountRiskAction.OWNED_GROUP_MESSAGE,
        _settings(),
    )
    assert budget.daily_limit == 0
    assert budget.cooldown_seconds == 0


@pytest.mark.asyncio
async def test_allow_audit_failure_still_sends_once_and_reserves_outbound_once(
    monkeypatch,
) -> None:
    from app.core.account import risk_guard as risk_module

    monkeypatch.setattr(
        risk_module,
        "get_account_risk_guard_settings",
        AsyncMock(return_value=_settings()),
    )
    db = AsyncMock()
    cache = MemoryCache()
    guard = AccountRiskGuard(db, cache=cache)
    guard._get_db_account = AsyncMock(return_value=None)
    guard._outbound_message_hard_cap = AsyncMock(return_value=30)
    guard.record_event = AsyncMock(side_effect=RuntimeError("audit unavailable"))
    client = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(id=901))
    )
    account = SimpleNamespace(account_id=7, client=client)

    message_id = await TelegramExecutionService(guard).send_owned_group_message(
        account,
        -1007,
        "hello",
        execution_id=77,
    )

    assert message_id == 901
    assert client.send_message.await_count == 1
    outbound = {
        key: value
        for key, value in cache.values.items()
        if ":daily:outbound_message:" in key
    }
    assert list(outbound.values()) == [1]
    assert not any(":daily:owned_group_message:" in key for key in cache.values)
    assert not any(":cooldown:owned_group_message" in key for key in cache.values)


@pytest.mark.asyncio
async def test_platform_group_write_ban_blocks_owned_group_message(monkeypatch) -> None:
    from app.core.account import risk_guard as risk_module

    monkeypatch.setattr(
        risk_module,
        "get_account_risk_guard_settings",
        AsyncMock(return_value=_settings()),
    )
    account_row = SimpleNamespace(
        is_active=True,
        status=AccountStatus.ONLINE,
        risk_reason="platform_group_write_banned",
        risk_level=AccountRiskLevel.FROZEN.value,
        risk_pause_until=None,
    )
    guard = AccountRiskGuard(AsyncMock(), cache=MemoryCache())
    guard._get_db_account = AsyncMock(return_value=account_row)
    guard._apply_risk_lifecycle = AsyncMock()
    guard._block = AsyncMock(
        return_value=SimpleNamespace(
            allowed=False,
            reason="platform_group_write_banned",
        )
    )

    result = await guard.check_and_reserve(
        SimpleNamespace(account_id=7),
        AccountRiskAction.OWNED_GROUP_MESSAGE,
        target_type="group",
        target_id=-1007,
    )

    assert AccountRiskAction.OWNED_GROUP_MESSAGE in ACCOUNT_WIDE_GROUP_WRITE_ACTIONS
    assert result.allowed is False
    assert result.reason == "platform_group_write_banned"


@pytest.mark.asyncio
async def test_owned_group_preflight_release_is_idempotent_and_attempt_scoped(
    monkeypatch,
) -> None:
    from app.core.account import risk_guard as risk_module

    monkeypatch.setattr(
        risk_module,
        "get_account_risk_guard_settings",
        AsyncMock(return_value=_settings()),
    )
    cache = MemoryCache()
    guard = AccountRiskGuard(AsyncMock(), cache=cache)
    guard._get_db_account = AsyncMock(return_value=None)
    guard._outbound_message_hard_cap = AsyncMock(return_value=30)
    guard.record_event = AsyncMock()
    account = SimpleNamespace(account_id=7)

    first = await guard.check_and_reserve(
        account,
        AccountRiskAction.OWNED_GROUP_MESSAGE,
        target_type="group",
        target_id=-1007,
        details={"risk_reservation_id": "owned-group-attempt-first"},
    )
    repeated = await guard.check_and_reserve(
        account,
        AccountRiskAction.OWNED_GROUP_MESSAGE,
        target_type="group",
        target_id=-1007,
        details={"risk_reservation_id": "owned-group-attempt-first"},
    )

    assert first.allowed is True
    assert repeated.allowed is True
    outbound_key = next(key for key in cache.values if ":daily:outbound_message:" in key)
    assert cache.values[outbound_key] == 1
    assert await guard.release_owned_group_message_reservation(
        account,
        "owned-group-attempt-first",
    )
    assert await guard.release_owned_group_message_reservation(
        account,
        "owned-group-attempt-first",
    )
    assert cache.values[outbound_key] == 0

    second = await guard.check_and_reserve(
        account,
        AccountRiskAction.OWNED_GROUP_MESSAGE,
        target_type="group",
        target_id=-1007,
        details={"risk_reservation_id": "owned-group-attempt-second"},
    )
    assert second.allowed is True
    assert cache.values[outbound_key] == 1


def test_owned_group_join_budget_cannot_be_overridden() -> None:
    budget = AccountRiskGuard._budget_for_action(
        AccountRiskAction.OWNED_GROUP_JOIN,
        {
            "enabled": True,
            "actions": {
                "owned_group_join": {"daily_limit": 999, "cooldown_seconds": 999},
                "join": {"daily_limit": 1, "cooldown_seconds": 1},
            },
            "lifecycle": {},
        },
    )
    assert budget.daily_limit == 300
    assert budget.cooldown_seconds == 0
