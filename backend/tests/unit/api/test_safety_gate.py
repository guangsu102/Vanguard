import importlib
from unittest.mock import AsyncMock

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.governance_gate import GovernanceGateState
from app.core.p0_safety_gate import get_safety_gate_state, set_global_stop
from app.core.security import get_current_user
from app.main import app

safety_gate_api = importlib.import_module("app.api.safety_gate")


def _valid_string_session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


async def _create_account(test_db, **overrides):
    account = TelegramAccount(
        identifier=overrides.get("identifier", "safety-gate-account"),
        session_name=overrides.get("session_name", "safety-gate-account"),
        account_type=overrides.get("account_type", AccountType.PROMOTER),
        status=overrides.get("status", AccountStatus.ONLINE),
        is_active=overrides.get("is_active", True),
        session_string=overrides.get("session_string", _valid_string_session()),
        risk_level=overrides.get("risk_level", "normal"),
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    return account


def _set_admin_override():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "admin",
        "role": "admin",
    }


def _clear_overrides():
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_safety_gate_precheck_allows_eligible_promoter(client, test_db):
    await _create_account(test_db)
    _set_admin_override()

    response = await client.post(
        "/api/safety-gate/precheck",
        json={"account_id": 1},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["allowed"] is True
    assert body["data"]["reason"] == "eligible"
    _clear_overrides()


@pytest.mark.asyncio
async def test_safety_gate_precheck_blocks_inactive_or_non_promoter(client, test_db):
    await _create_account(test_db, is_active=False, account_type=AccountType.GUARDIAN_BOT)
    _set_admin_override()

    response = await client.post(
        "/api/safety-gate/precheck",
        json={"account_id": 1},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "account_type_not_promoter"
    _clear_overrides()


@pytest.mark.asyncio
async def test_safety_gate_global_stop_requires_admin_and_blocks_precheck(client, test_db):
    await _create_account(test_db)
    _set_admin_override()

    stop_response = await client.post(
        "/api/safety-gate/stop",
        json={"enabled": True, "reason": "manual lockdown"},
    )
    assert stop_response.status_code == 200
    assert stop_response.json()["data"]["global_stop"] is True

    state_response = await client.get("/api/safety-gate/state")
    assert state_response.status_code == 200
    assert state_response.json()["data"]["global_stop"] is True

    precheck_response = await client.post(
        "/api/safety-gate/precheck",
        json={"account_id": 1},
    )
    assert precheck_response.status_code == 400
    assert precheck_response.json()["detail"]["reason"] == "global_stop_enabled"

    _clear_overrides()
    state = await get_safety_gate_state()
    assert state.global_stop is True
    await set_global_stop(enabled=False, reason="test cleanup")


@pytest.mark.asyncio
async def test_admin_can_operate_independent_governance_stop(client, monkeypatch):
    _set_admin_override()
    set_stop = AsyncMock(
        return_value=GovernanceGateState(
            governance_stop=True,
            reason="guardian maintenance",
            updated_at="2026-09-10T00:00:00+00:00",
            backend_available=True,
        )
    )
    read_state = AsyncMock(return_value=set_stop.return_value)
    monkeypatch.setattr(safety_gate_api, "set_governance_stop", set_stop)
    monkeypatch.setattr(safety_gate_api, "get_governance_gate_state", read_state)

    stop_response = await client.post(
        "/api/safety-gate/governance/stop",
        json={"enabled": True, "reason": "guardian maintenance"},
    )
    state_response = await client.get("/api/safety-gate/governance/state")

    assert stop_response.status_code == 200
    assert stop_response.json()["data"]["governance_stop"] is True
    assert state_response.status_code == 200
    assert state_response.json()["data"]["reason"] == "guardian maintenance"
    set_stop.assert_awaited_once_with(
        enabled=True,
        reason="guardian maintenance",
        operator="admin",
    )
    read_state.assert_awaited_once()
    _clear_overrides()


@pytest.mark.asyncio
async def test_governance_stop_rejects_non_durable_write(client, monkeypatch):
    _set_admin_override()
    monkeypatch.setattr(
        safety_gate_api,
        "set_governance_stop",
        AsyncMock(
            return_value=GovernanceGateState(
                governance_stop=True,
                reason="emergency",
                backend_available=False,
            )
        ),
    )

    response = await client.post(
        "/api/safety-gate/governance/stop",
        json={"enabled": True, "reason": "emergency"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "governance_gate_backend_unavailable"
    _clear_overrides()
