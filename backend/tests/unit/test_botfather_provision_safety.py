"""Official BotFather provisioning must use one fenced creation budget, never a PM bypass."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account.risk_guard import (
    DEFAULT_ACTION_BUDGETS,
    OWNER_ACCOUNT_EXTERNAL_PROMOTION_ACTIONS,
    AccountRiskAction,
    AccountRiskGuard,
)
from app.core.account.telegram_execution import TelegramExecutionError, TelegramExecutionService
from app.core.automation_settings import normalize_account_risk_guard_settings
from app.modules.managed_bot_provision.service import (
    _classify_botfather_create_error,
    requeue_unattempted_managed_bot_provision,
)


def operation():
    return Obj(
        id=1,
        owner_account_id=4,
        status="running",
        current_step="create_bot",
        lease_id="test-current-lease",
        lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
        username="pp_ai_test_guard_bot",
        display_name="PP-AI Test",
        idempotency_key="test-operation-key",
        request_hash="a" * 64,
        create_attempted_at=None,
        external_created_at=None,
        bot_user_id=None,
        attempts=1,
        max_attempts=3,
        retryable=False,
        error_code=None,
    )


class MemoryCache:
    def __init__(self):
        self.values = {}
        self.client = self

    async def get(self, key):
        return self.values.get(key)

    async def incr(self, key, amount=1):
        self.values[key] = int(self.values.get(key, 0)) + amount
        return self.values[key]

    async def expire(self, key, ttl):
        return True

    async def set(self, key, value, ttl=None):
        self.values[key] = value
        return True


class BotFatherClient:
    def __init__(self):
        self.official = Obj(id=93372553, username="BotFather", bot=True, verified=True)
        self.sent = []
        self.conversation_peers = []
        self.response_error = None
        self.responses = [
            Obj(message="Please choose a name for your bot"),
            Obj(message="Now choose a username"),
            Obj(message="Use this token: 123456789:" + "a" * 40),
        ]

    async def get_entity(self, username):
        assert username == "BotFather"
        return self.official

    @asynccontextmanager
    async def conversation(self, peer, *, timeout):
        self.conversation_peers.append(peer)
        yield self

    async def send_message(self, text):
        self.sent.append(text)
        return Obj(id=len(self.sent))

    async def get_response(self):
        if self.response_error:
            raise self.response_error
        return self.responses.pop(0)


def setup_execution(row=None):
    row = row or operation()
    cache = MemoryCache()
    budget_guard = AccountRiskGuard(AsyncMock(), cache=cache)
    actions = []

    async def reserve(account, action, **kwargs):
        actions.append((action, kwargs))
        # The ordinary private-message guard remains unchanged for an owner.
        if action in OWNER_ACCOUNT_EXTERNAL_PROMOTION_ACTIONS:
            return Obj(allowed=False, reason="owned_group_owner_protected")
        allowed, reason, retry = await budget_guard._reserve_budget(
            account.account_id,
            action,
            DEFAULT_ACTION_BUDGETS[action],
            normalize_account_risk_guard_settings({}),
            reservation_id=kwargs["details"]["risk_reservation_id"],
        )
        return Obj(allowed=allowed, reason=reason, retry_after_seconds=retry)

    risk = Obj(
        db=Obj(get=AsyncMock(return_value=row)),
        check_and_reserve=AsyncMock(side_effect=reserve),
        record_success=AsyncMock(),
        record_failure=AsyncMock(),
    )
    client = BotFatherClient()
    account = Obj(account_id=4, client=client)
    execution = TelegramExecutionService(risk)

    async def fence():
        row.create_attempted_at = datetime.utcnow()

    async def create(**overrides):
        kwargs = {
            "name": row.display_name,
            "username": row.username,
            "provision_id": row.id,
            "lease_id": row.lease_id,
            "on_username_submitted": fence,
        }
        kwargs.update(overrides)
        return await execution.create_bot_via_botfather(account, **kwargs)

    return row, cache, actions, risk, client, execution, account, create


@pytest.mark.asyncio
async def test_official_conversation_uses_one_creation_budget_and_fences_username():
    row, cache, actions, risk, client, _, _, create = setup_execution()
    token = await create()
    assert token.startswith("123456789:")
    assert client.sent == ["/newbot", row.display_name, row.username]
    assert client.conversation_peers == [client.official]
    assert row.create_attempted_at is not None
    assert len(actions) == 1
    assert actions[0][0] == AccountRiskAction.MANAGED_BOT_CREATE
    assert actions[0][1]["target_id"] == 93372553
    assert actions[0][1]["details"]["risk_reservation_id"] == "managed-bot-1-" + "a" * 16
    counts = [value for key, value in cache.values.items() if ":daily:managed_bot_create:" in key]
    assert counts == [1]
    risk.record_success.assert_awaited_once()
    assert AccountRiskAction.PRIVATE_MESSAGE in OWNER_ACCOUNT_EXTERNAL_PROMOTION_ACTIONS


@pytest.mark.asyncio
async def test_pre_username_retries_share_one_reservation_but_another_operation_is_limited():
    row, cache, _, _, client, _, _, create = setup_execution()
    client.response_error = TimeoutError()
    for _ in range(2):
        with pytest.raises(
            TelegramExecutionError, match="botfather_conversation_failed:TimeoutError"
        ):
            await create()
    assert row.create_attempted_at is None
    assert client.sent == ["/newbot", "/newbot"]
    assert [v for k, v in cache.values.items() if ":daily:managed_bot_create:" in k] == [1]
    row.id = 2
    row.idempotency_key = "different-test-operation"
    with pytest.raises(TelegramExecutionError, match="risk_guard_blocked:managed_bot_create"):
        await create()
    assert client.sent == ["/newbot", "/newbot"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"owner_account_id": 3},
        {"status": "queued"},
        {"current_step": "fetch_token"},
        {"lease_expires_at": datetime.utcnow() - timedelta(seconds=1)},
        {"create_attempted_at": datetime.utcnow()},
        {"external_created_at": datetime.utcnow()},
        {"bot_user_id": 900},
        {"idempotency_key": ""},
        {"request_hash": "invalid"},
    ],
)
async def test_invalid_provision_never_reserves_or_messages(change):
    row, _, _, risk, client, _, _, create = setup_execution()
    for key, value in change.items():
        setattr(row, key, value)
    with pytest.raises(TelegramExecutionError, match="provision_lease_invalid"):
        await create()
    risk.check_and_reserve.assert_not_awaited()
    assert not client.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"id": 123},
        {"username": "other_bot"},
        {"verified": False},
        {"bot": False},
    ],
)
async def test_unverified_official_identity_never_reserves_or_messages(change):
    _, _, _, risk, client, _, _, create = setup_execution()
    for key, value in change.items():
        setattr(client.official, key, value)
    with pytest.raises(TelegramExecutionError, match="official_identity_invalid"):
        await create()
    risk.check_and_reserve.assert_not_awaited()
    assert not client.sent


@pytest.mark.asyncio
async def test_source_string_cannot_grant_provisioning_rights():
    _, _, _, risk, client, execution, account, _ = setup_execution()
    with pytest.raises(TelegramExecutionError, match="provision_context_required"):
        await execution.create_bot_via_botfather(
            account,
            name="PP-AI Test",
            username="pp_ai_test_guard_bot",
            source="managed_bot_provision",
        )
    risk.check_and_reserve.assert_not_awaited()
    assert not client.sent


@pytest.mark.asyncio
async def test_stale_lease_and_mismatched_request_are_blocked():
    _, _, _, risk, client, _, _, create = setup_execution()
    with pytest.raises(TelegramExecutionError, match="provision_lease_invalid"):
        await create(lease_id="stale-lease")
    with pytest.raises(TelegramExecutionError, match="provision_lease_invalid"):
        await create(username="different_target_bot")
    assert not client.sent
    risk.check_and_reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_durable_username_fence_stops_before_creation_submission():
    row, _, _, _, client, _, _, create = setup_execution()
    with pytest.raises(TelegramExecutionError, match="create_fence_missing"):
        await create(on_username_submitted=AsyncMock())
    assert client.sent == ["/newbot", row.display_name]


@pytest.mark.asyncio
async def test_requeue_preserves_identity_attempts_and_original_operation():
    row = operation()
    row.status, row.lease_id, row.retryable = "retry_wait", None, True
    row.error_code = "ACCOUNT_RISK_BLOCKED"
    db = Obj(scalar=AsyncMock(return_value=row), commit=AsyncMock())
    original = (row.id, row.idempotency_key, row.request_hash, row.attempts)
    result = await requeue_unattempted_managed_bot_provision(db, 1)
    assert result is row
    assert (row.id, row.idempotency_key, row.request_hash, row.attempts) == original
    assert row.status == "queued"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_requeue_refuses_unknown_external_creation_outcome():
    row = operation()
    row.status, row.lease_id, row.retryable = "retry_wait", None, True
    row.error_code = "ACCOUNT_RISK_BLOCKED"
    row.create_attempted_at = datetime.utcnow()
    db = Obj(scalar=AsyncMock(return_value=row), commit=AsyncMock())
    with pytest.raises(ValueError, match="REQUIRES_RECONCILIATION"):
        await requeue_unattempted_managed_bot_provision(db, 1)
    db.commit.assert_not_awaited()


def test_invalid_identity_or_operation_is_not_a_retryable_budget_failure():
    failure = _classify_botfather_create_error(
        TelegramExecutionError("botfather_official_identity_invalid")
    )
    assert failure.code == "BOTFATHER_PROVISION_CONTEXT_INVALID"
    assert not failure.retryable
