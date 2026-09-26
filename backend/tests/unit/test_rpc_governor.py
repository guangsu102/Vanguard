from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.messages import SendMessageRequest

from app.core.account.outbound_budget import effective_capacity_limits
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.rpc_governor import (
    RpcDeferred,
    RpcGovernor,
    install_governor,
    limits_for,
    record_flood,
    snapshot,
)
from app.modules.acquisition.search.group_finder import extract_flood_wait_seconds
from tests.unit.test_dynamic_capacity_feedback import NOW, pair

pytestmark = pytest.mark.asyncio


async def test_flood_persists_and_does_not_shorten_or_double_penalize(test_db, monkeypatch):
    from app.core.account import rpc_governor
    monkeypatch.setattr(rpc_governor, 'read_budget_state', AsyncMock(return_value={'usage': {}, 'blocked_windows': [], 'retry_after_seconds': 0, 'emergency_cooldown_seconds': 0}))
    await record_flood(
        test_db, 2, 100, "contacts.ResolveUsernameRequest", "join_candidate_preview", NOW
    )
    await test_db.commit()
    state = await record_flood(test_db, 2, 10, "users.GetUsersRequest", "metadata", NOW)
    assert state["pause_until"] == (NOW + timedelta(seconds=160)).isoformat()
    assert limits_for(state, NOW)["hour"] == 120
    assert (await snapshot(test_db, 2, NOW))["state"] == "cooldown"
    assert (await snapshot(test_db, 2, NOW + timedelta(seconds=160)))["state"] == "recovering"
    assert limits_for(state, NOW + timedelta(days=5))["hour"] == 120
    state["last_success_at"] = (NOW + timedelta(minutes=3)).isoformat()
    assert limits_for(state, NOW + timedelta(days=2))["hour"] == 144


async def test_read_pause_expiry_restores_ads_without_changing_existing_risk(test_db):
    account, config = await pair(test_db)
    account.risk_pause_until = NOW
    account.risk_reason = "telegram_read_flood_wait"
    account.risk_score = 0
    await AccountRiskGuard(test_db)._apply_risk_lifecycle(account, NOW)
    assert account.risk_pause_until is None and account.risk_recovery_until is None
    assert (await effective_capacity_limits(test_db, account, config, NOW))["ad"] == 30
    account.risk_pause_until = NOW
    account.risk_reason = "platform_group_write_banned"
    account.risk_level = "frozen"
    account.risk_score = 75
    await AccountRiskGuard(test_db)._apply_risk_lifecycle(account, NOW)
    assert account.risk_recovery_until > NOW
    assert account.risk_reason == "platform_group_write_banned"


async def test_shared_cooldown_blocks_reads_and_writes_without_rpc(monkeypatch, test_db):
    from app.core import database

    @asynccontextmanager
    async def session():
        yield test_db

    monkeypatch.setattr(database, "get_db_session", session)
    await record_flood(test_db, 2, 100, "contacts.ResolveUsernameRequest", "preview")
    for request in [ResolveUsernameRequest("example"), SendMessageRequest("example", "not sent")]:
        original = AsyncMock()
        client = SimpleNamespace(_call=original)
        install_governor(client, RpcGovernor(2, lambda: "ad_delivery"))
        with pytest.raises(RpcDeferred, match="telegram_rpc_cooldown") as error:
            await client._call(None, request)
        original.assert_not_awaited()
        assert extract_flood_wait_seconds(error.value) is None


async def test_rpc_exception_is_recorded_with_method_without_retry():
    original = AsyncMock(side_effect=FloodWaitError(request=None, capture=99))
    client = SimpleNamespace(_call=original)
    governor = SimpleNamespace(before=AsyncMock(), failed=AsyncMock(), succeeded=AsyncMock())
    install_governor(client, governor)
    with pytest.raises(FloodWaitError):
        await client._call(None, ResolveUsernameRequest("private_input"))
    original.assert_awaited_once()
    assert original.await_args.kwargs["flood_sleep_threshold"] == 0
    assert governor.failed.await_args.args[0] == ["contacts.ResolveUsernameRequest"]
    governor.succeeded.assert_not_awaited()


async def test_successful_write_is_not_retried_on_telemetry_failure():
    original = AsyncMock(return_value="receipt")
    client = SimpleNamespace(_call=original)
    governor = SimpleNamespace(
        before=AsyncMock(), failed=AsyncMock(), succeeded=AsyncMock(side_effect=RuntimeError())
    )
    install_governor(client, governor)
    assert await client._call(None, SendMessageRequest("example", "not sent")) == "receipt"
    original.assert_awaited_once()


async def test_local_budget_wait_preserves_review_attempt_counter():
    from app.modules.acquisition.qualification_service import review_schedule

    result = {
        "decision": "technical_wait",
        "reason": "telegram_read_budget",
        "retry_after_seconds": 120,
    }
    verdict, reason, state, due = review_schedule(result, {"technical_failures": 2}, NOW)
    assert (verdict, state, due) == ("technical_wait", "completed", NOW + timedelta(seconds=120))
    assert result["technical_failures"] == 2


async def test_read_flood_does_not_reduce_ad_action_capacity(test_db):
    from app.core.account.risk_guard import AccountRiskAction

    account, config = await pair(test_db)
    exc = FloodWaitError(request=None, capture=120)
    exc._vanguard_read_flood = True
    await AccountRiskGuard(test_db).record_failure(account, AccountRiskAction.AD_DELIVERY, exc)
    limits = await effective_capacity_limits(test_db, account, config, NOW)
    assert limits["ad"] == 30 and limits["action_feedback"] == {}
    assert account.risk_pause_until is None


async def test_collector_keeps_partial_evidence_on_local_budget_wait():
    from app.modules.acquisition.group_qualification import EvidenceCollector

    result = {"technical_errors": [], "evidence": [{"message_id": 123}]}
    collector = EvidenceCollector(SimpleNamespace())
    assert collector._error(result, "history", RpcDeferred("telegram_read_budget", 3600))
    assert result["evidence"] == [{"message_id": 123}]
    assert result["technical_errors"] == []
    assert result["retry_after_seconds"] == 3600


async def test_original_flood_survives_database_checkpoint_outage(monkeypatch):
    from app.core import database, redis as redis_module
    cache = SimpleNamespace(eval=AsyncMock(return_value=1))
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=cache))
    @asynccontextmanager
    async def broken_session():
        raise ConnectionError("database unavailable")
        yield
    monkeypatch.setattr(database, "get_db_session", broken_session)
    exc = FloodWaitError(request=None, capture=300)
    await RpcGovernor(2, lambda: "group_qualification").failed(["contacts.ResolveUsernameRequest"], exc)
    assert exc._vanguard_read_flood is True
    assert cache.eval.await_args.args[-1] == 360


async def test_listener_local_cooldown_does_not_make_account_error():
    from app.core.account.pool import AccountPool
    from app.core.account.models import AccountStatus
    pool = AccountPool()
    account = await pool.add_account(account_id=2, phone="+10000000002", session_name="test-cooldown", country_code="US", api_id="1", api_hash="test")
    pool._assert_proxy_policy_current = AsyncMock()
    pool._ensure_proxy = AsyncMock()
    pool._create_client = AsyncMock(side_effect=RpcDeferred("telegram_rpc_cooldown", 300))
    previous = account.status
    with pytest.raises(RpcDeferred):
        await pool.connect_by_id(2, require_session=False)
    assert account.status == previous and account.status != AccountStatus.ERROR
