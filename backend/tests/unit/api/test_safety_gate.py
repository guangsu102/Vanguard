import pytest

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.p0_safety_gate import get_safety_gate_state, set_global_stop
from app.core.security import get_current_user
from app.main import app


async def _create_account(test_db, **overrides):
    account = TelegramAccount(
        identifier=overrides.get("identifier", "safety-gate-account"),
        session_name=overrides.get("session_name", "safety-gate-account"),
        account_type=overrides.get("account_type", AccountType.PROMOTER),
        status=overrides.get("status", AccountStatus.ONLINE),
        is_active=overrides.get("is_active", True),
        session_string=overrides.get("session_string", "session-string"),
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
