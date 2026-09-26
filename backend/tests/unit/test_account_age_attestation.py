import json
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace as Obj

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.accounts import AccountAgeAttestationRequest, update_age_attestation
from app.core.account.age import age_evidence, account_age_eligibility_reason
from app.core.account.models import TelegramAccount, AccountRiskEvent, AccountStatus

NOW = datetime(2026, 9, 23, 12)


def statement(days=180, **kwargs):
    return json.dumps(dict(minimum_age_days=days, confirmed_at=NOW.isoformat(),
        source="owner_confirmation", confirmed_by=1, version=1, **kwargs))


@pytest.mark.parametrize("days,allowed", [(179, False), (180, True)])
def test_age_boundary_from_lower_bound(days, allowed):
    account = Obj(registered_at=None, age_attestation_json=statement(days))
    assert (account_age_eligibility_reason(account, NOW) is None) is allowed
    assert account.registered_at is None


def test_age_attestation_revocation_conflict_and_growth():
    a = Obj(registered_at=None, age_attestation_json=statement())
    assert age_evidence(a, NOW + timedelta(days=2))["minimum_age_days"] == 182
    a.registered_at = NOW - timedelta(days=179)
    assert account_age_eligibility_reason(a, NOW) == "account_age_evidence_conflict"
    a.registered_at = None
    a.age_attestation_json = statement(revoked_at=NOW.isoformat())
    assert age_evidence(a, NOW)["minimum_age_days"] is None
    a.age_attestation_json = statement()
    assert age_evidence(a, NOW - timedelta(seconds=1))["minimum_age_days"] is None


def test_exact_date_and_timezone_remain_distinct():
    a = Obj(registered_at=(NOW - timedelta(days=180)).replace(tzinfo=UTC), age_attestation_json=None)
    assert age_evidence(a, NOW) == {"minimum_age_days": 180, "source": "registered_at", "conflict": False}
    a.registered_at = NOW + timedelta(days=1)
    assert age_evidence(a, NOW)["conflict"] is True


@pytest.mark.asyncio
async def test_version_idempotency_revoke_preserve_restrictions(test_db):
    account = TelegramAccount(identifier="age-restricted", session_name="age-restricted",
        status=AccountStatus.RESTRICTED, is_active=True, risk_level="frozen")
    test_db.add(account)
    await test_db.commit()
    req = AccountAgeAttestationRequest(minimum_age_days=180)
    first = await update_age_attestation(account.id, req, "age-test-key-1", {"id": 1}, test_db)
    repeat = await update_age_attestation(account.id, req, "age-test-key-1", {"id": 1}, test_db)
    assert first == repeat
    assert first["data"]["version"] == 1
    assert account.registered_at is None
    assert account.status == AccountStatus.RESTRICTED and account.risk_level == "frozen"
    events = (await test_db.scalars(select(AccountRiskEvent))).all()
    assert len(events) == 1
    with pytest.raises(HTTPException) as stale:
        await update_age_attestation(account.id, req, "age-test-key-2", {"id": 1}, test_db)
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as mismatch:
        await update_age_attestation(account.id, AccountAgeAttestationRequest(minimum_age_days=181), "age-test-key-1", {"id": 1}, test_db)
    assert mismatch.value.status_code == 409
    revoked = await update_age_attestation(account.id,
        AccountAgeAttestationRequest(expected_version=1, revoke=True), "age-test-key-3", {"id": 1}, test_db)
    assert revoked["data"]["revoked_at"]
    assert age_evidence(account, datetime.utcnow())["minimum_age_days"] is None
    assert account.status == AccountStatus.RESTRICTED


def test_age_endpoint_requires_admin():
    from app.api.accounts import router
    from app.api.accounts import require_admin
    route = next(route for route in router.routes if route.path.endswith('/age-attestation'))
    assert any(item.call == require_admin for item in route.dependant.dependencies)
