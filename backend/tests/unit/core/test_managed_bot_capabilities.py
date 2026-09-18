from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl.functions.bots import CheckUsernameRequest, CreateBotRequest

from app.core.account.risk_guard import (
    DEFAULT_ACTION_BUDGETS,
    AccountRiskAction,
    AccountRiskGuard,
    RiskDecision,
)
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
)
from app.core.automation_settings import normalize_account_risk_guard_settings
from app.integrations.telegram.client import (
    TelegramAPIError,
    TelegramClient,
    TelegramConfig,
    User,
)


class FakeManagedBotClient:
    def __init__(
        self,
        *,
        username_available: bool = True,
        manager_capable: bool = True,
        create_error: Exception | None = None,
    ):
        self.username_available = username_available
        self.create_error = create_error
        self.manager = SimpleNamespace(
            id=7001,
            username="manager_bot",
            bot=True,
            bot_can_manage_bots=manager_capable,
        )
        self.manager_input = SimpleNamespace(user_id=7001, access_hash=99)
        self.requests = []

    async def get_entity(self, reference):
        assert reference == "manager_bot"
        return self.manager

    async def get_input_entity(self, entity):
        assert entity is self.manager
        return self.manager_input

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, CheckUsernameRequest):
            return self.username_available
        if isinstance(request, CreateBotRequest):
            if self.create_error is not None:
                raise self.create_error
            return SimpleNamespace(
                id=8001,
                username=request.username,
                first_name=request.name,
                bot=True,
            )
        raise AssertionError(f"unexpected request type: {type(request).__name__}")


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


def test_bot_api_user_preserves_manager_capability():
    user = User.from_dict(
        {
            "id": 7001,
            "username": "manager_bot",
            "is_bot": True,
            "can_manage_bots": True,
        }
    )

    assert user.is_bot is True
    assert user.can_manage_bots is True


@pytest.mark.asyncio
async def test_bot_api_fetches_managed_token_without_exposing_it_in_errors():
    client = TelegramClient(TelegramConfig(bot_token="manager-secret-token"))
    client._request = AsyncMock(return_value="8001:managed-child-secret")

    token = await client.get_managed_bot_token(8001)

    assert token == "8001:managed-child-secret"
    client._request.assert_awaited_once_with(
        "getManagedBotToken",
        {"user_id": 8001},
    )

    client._request = AsyncMock(
        side_effect=TelegramAPIError(
            "https://api.telegram.org/botmanager-secret-token/getManagedBotToken failed",
            code=502,
        )
    )
    with pytest.raises(TelegramAPIError) as exc_info:
        await client.get_managed_bot_token(8001)

    assert str(exc_info.value) == "Managed bot token request failed"
    assert "manager-secret-token" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_create_managed_bot_uses_official_requests_and_create_risk_budget():
    telegram = FakeManagedBotClient()
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=RiskDecision(True)),
        record_success=AsyncMock(),
        record_failure=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard=risk_guard)
    account = SimpleNamespace(client=telegram, account_id=41)

    created = await service.create_managed_bot(
        account,
        name="Managed Child",
        username="managed_child_bot",
        manager_bot="manager_bot",
    )

    assert created.id == 8001
    assert [type(request) for request in telegram.requests] == [
        CheckUsernameRequest,
        CreateBotRequest,
    ]
    create_request = telegram.requests[1]
    assert create_request.name == "Managed Child"
    assert create_request.username == "managed_child_bot"
    assert create_request.manager_id is telegram.manager_input
    reserve = risk_guard.check_and_reserve.await_args
    assert reserve.args[1] == AccountRiskAction.MANAGED_BOT_CREATE
    assert reserve.kwargs["target_type"] == "manager_bot"
    assert reserve.kwargs["target_id"] == 7001
    risk_guard.record_success.assert_awaited_once()
    risk_guard.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_managed_bot_success_is_not_masked_by_risk_audit_failure():
    telegram = FakeManagedBotClient()
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=RiskDecision(True)),
        record_success=AsyncMock(side_effect=RuntimeError("risk audit unavailable")),
        record_failure=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard=risk_guard)

    created = await service.create_managed_bot(
        SimpleNamespace(client=telegram, account_id=41),
        name="Managed Child",
        username="managed_child_bot",
        manager_bot="manager_bot",
    )

    assert created.id == 8001
    risk_guard.record_success.assert_awaited_once()
    risk_guard.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_managed_bot_rpc_failure_is_not_masked_by_risk_audit_failure():
    telegram = FakeManagedBotClient(
        create_error=RuntimeError("telegram create unavailable")
    )
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=RiskDecision(True)),
        record_success=AsyncMock(),
        record_failure=AsyncMock(side_effect=RuntimeError("risk audit unavailable")),
    )
    service = TelegramExecutionService(risk_guard=risk_guard)

    with pytest.raises(
        TelegramExecutionError,
        match="managed_bot_create_failed:RuntimeError",
    ):
        await service.create_managed_bot(
            SimpleNamespace(client=telegram, account_id=41),
            name="Managed Child",
            username="managed_child_bot",
            manager_bot="manager_bot",
        )

    risk_guard.record_success.assert_not_awaited()
    risk_guard.record_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_managed_bot_allow_audit_failure_keeps_reserved_budget_and_allows(
    monkeypatch,
):
    from app.core.account import risk_guard as risk_module

    monkeypatch.setattr(
        risk_module,
        "get_account_risk_guard_settings",
        AsyncMock(return_value={"enabled": True, "actions": {}, "lifecycle": {}}),
    )
    monkeypatch.setattr(
        risk_module,
        "get_account_warmup_policy_settings",
        AsyncMock(return_value={}),
    )
    db = AsyncMock()
    cache = MemoryCache()
    guard = AccountRiskGuard(db, cache=cache)
    guard._get_db_account = AsyncMock(return_value=None)
    audit_error = (
        "https://api.telegram.org/"
        "bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi/getMe failed"
    )
    guard.record_event = AsyncMock(side_effect=RuntimeError(audit_error))
    guard.logger = SimpleNamespace(error=Mock())
    account = SimpleNamespace(account_id=41)
    details = {"risk_reservation_id": "managed-bot-provision-42"}

    first = await guard.check_and_reserve(
        account,
        AccountRiskAction.MANAGED_BOT_CREATE,
        target_type="manager_bot",
        target_id=7001,
        details=details,
    )
    repeated = await guard.check_and_reserve(
        account,
        AccountRiskAction.MANAGED_BOT_CREATE,
        target_type="manager_bot",
        target_id=7001,
        details=details,
    )

    assert first.allowed is True
    assert repeated.allowed is True
    action_counts = {
        key: value
        for key, value in cache.values.items()
        if ":daily:managed_bot_create:" in key
    }
    assert list(action_counts.values()) == [1]
    assert db.rollback.await_count == 2
    assert guard.logger.error.call_count == 2
    (event,) = guard.logger.error.call_args.args
    assert event == "managed_bot_risk_allow_audit_failed"
    assert audit_error not in guard.logger.error.call_args.kwargs["error"]

@pytest.mark.asyncio
async def test_unavailable_username_does_not_consume_create_budget():
    telegram = FakeManagedBotClient(username_available=False)
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=RiskDecision(True)),
        record_success=AsyncMock(),
        record_failure=AsyncMock(),
    )
    service = TelegramExecutionService(risk_guard=risk_guard)

    with pytest.raises(TelegramExecutionError, match="managed_bot_username_unavailable"):
        await service.create_managed_bot(
            SimpleNamespace(client=telegram, account_id=41),
            name="Managed Child",
            username="managed_child_bot",
            manager_bot="manager_bot",
        )

    assert [type(request) for request in telegram.requests] == [CheckUsernameRequest]
    risk_guard.check_and_reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_capability_is_required_before_check_or_create():
    telegram = FakeManagedBotClient(manager_capable=False)
    service = TelegramExecutionService()

    with pytest.raises(
        TelegramExecutionError,
        match="managed_bot_manager_permission_missing",
    ):
        await service.create_managed_bot(
            SimpleNamespace(client=telegram, account_id=41),
            name="Managed Child",
            username="managed_child_bot",
            manager_bot="manager_bot",
        )

    assert telegram.requests == []


@pytest.mark.parametrize(
    "username",
    ["short", "missing_suffix", "has-hyphen-bot", "x" * 33 + "bot"],
)
@pytest.mark.asyncio
async def test_invalid_managed_bot_username_never_calls_telegram(username):
    telegram = FakeManagedBotClient()
    service = TelegramExecutionService()

    with pytest.raises(TelegramExecutionError, match="invalid managed bot username"):
        await service.check_managed_bot_username(
            SimpleNamespace(client=telegram, account_id=41),
            username,
        )

    assert telegram.requests == []


def test_managed_bot_create_budget_is_fixed_to_safe_defaults():
    budget = DEFAULT_ACTION_BUDGETS[AccountRiskAction.MANAGED_BOT_CREATE]
    assert budget.daily_limit == 1
    assert budget.cooldown_seconds == 3600

    normalized = normalize_account_risk_guard_settings(
        {
            "actions": {
                "managed_bot_create": {
                    "daily_limit": 999,
                    "cooldown_seconds": 1,
                }
            }
        }
    )
    assert normalized["actions"]["managed_bot_create"] == {
        "daily_limit": 1,
        "cooldown_seconds": 3600,
    }


@pytest.mark.asyncio
async def test_same_provision_retry_reserves_managed_bot_budget_once():
    cache = MemoryCache()
    guard = AccountRiskGuard(AsyncMock(), cache=cache)
    action = AccountRiskAction.MANAGED_BOT_CREATE
    reservation_id = "managed-bot-provision-42"

    first = await guard._reserve_budget(
        41,
        action,
        DEFAULT_ACTION_BUDGETS[action],
        {},
        reservation_id=reservation_id,
    )
    repeated = await guard._reserve_budget(
        41,
        action,
        DEFAULT_ACTION_BUDGETS[action],
        {},
        reservation_id=reservation_id,
    )

    assert first == (True, "reserved", None)
    assert repeated == (True, "reserved", None)
    action_counts = {
        key: value
        for key, value in cache.values.items()
        if ":daily:managed_bot_create:" in key
    }
    assert list(action_counts.values()) == [1]
    assert any(
        key.endswith(f":managed_bot_reservation:{reservation_id}")
        for key in cache.values
    )
