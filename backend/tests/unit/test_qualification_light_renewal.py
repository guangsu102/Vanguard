"""Qualification renewal uses live checks without repeating history/AI scans."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account.rpc_governor import RpcDeferred
from app.modules.acquisition.qualification_lifetime import evidence_expiry, renewal_at
from app.modules.acquisition.qualification_renewal import try_light_renewal
from app.modules.acquisition import qualification_actions as actions
from tests.unit.test_qualification_service import setup


def test_expiry_and_renewal_use_collection_time_not_delayed_review_time():
    collected = datetime(2026, 9, 1)
    reviewed = collected + timedelta(hours=5)
    assert evidence_expiry(collected, reviewed) == collected + timedelta(hours=720)
    assert renewal_at(collected, reviewed) == collected + timedelta(hours=696)


async def fixture(db):
    account, group, member, row = await setup(db, decision="trial")
    old = datetime.utcnow() - timedelta(days=2)
    previous = json.loads(row.evidence_json)
    previous.update(collected_at=old.isoformat(), checked_at=old.isoformat(),
                    advertising_audit={"ad_allowed": True, "policy_mode": "soft_ad_trial",
                                       "confidence": 100, "decision_source": "verified_ordinary_member_precedent"})
    row.evidence_json = json.dumps(previous)
    client = Obj(get_me=AsyncMock(return_value=Obj(id=200)))
    wrapper = Obj(client=client)
    pool = Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock())
    return Obj(db=db, account_pool=pool), account, group, member, row, previous


@pytest.mark.asyncio
async def test_renewal_keeps_original_evidence_and_reuses_live_send_validation(monkeypatch, test_db):
    service, account, group, member, row, previous = await fixture(test_db)
    live = AsyncMock()
    monkeypatch.setattr(actions, "validate_live_send", live)
    result = await try_light_renewal(service, account, group, member, row, previous, force_refresh=False)
    assert result.passed is True
    assert result.ad_rule_details["policy_mode"] == "soft_ad_trial"
    assert result.ad_rule_details["confidence"] == 100
    live.assert_awaited_once_with(test_db, service.account_pool.acquire_by_id.return_value.client, account.id, group.group_id)
    saved = json.loads(row.evidence_json)
    assert saved["full_collected_at"] == previous["collected_at"]
    assert saved["evidence"] == previous["evidence"]
    assert row.expires_at - row.checked_at == timedelta(hours=720)
    assert row.next_retry_at - row.checked_at == timedelta(hours=696)
    service.account_pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_budget_wait_does_not_extend_or_revoke_old_approval(monkeypatch, test_db):
    service, account, group, member, row, previous = await fixture(test_db)
    old_deadline, old_evidence = row.expires_at, row.evidence_json
    monkeypatch.setattr(actions, "validate_live_send", AsyncMock(side_effect=RpcDeferred("telegram_read_budget", 60)))
    result = await try_light_renewal(service, account, group, member, row, previous, force_refresh=False)
    assert not result.passed
    assert row.decision == "trial" and row.expires_at == old_deadline and row.evidence_json == old_evidence


@pytest.mark.asyncio
async def test_permission_change_invalidates_and_queues_full_review(monkeypatch, test_db):
    service, account, group, member, row, previous = await fixture(test_db)
    from app.core.account.telegram_execution import TelegramExecutionError
    monkeypatch.setattr(actions, "validate_live_send", AsyncMock(side_effect=TelegramExecutionError("qualification_current_permission_changed")))
    result = await try_light_renewal(service, account, group, member, row, previous, force_refresh=False)
    assert not result.passed and row.decision == "observe" and row.expires_at <= datetime.utcnow()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["profile", "forced", "membership", "unapproved"])
async def test_light_path_cannot_authorize_changed_scope(monkeypatch, test_db, change):
    service, account, group, member, row, previous = await fixture(test_db)
    if change == "profile": previous["profile_fingerprint"] = "changed"
    if change == "membership": member.status = "left"
    if change == "unapproved": row.decision = "observe"
    result = await try_light_renewal(service, account, group, member, row, previous, force_refresh=change == "forced")
    assert result is None
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expired", [(696, False), (721, True)])
async def test_actual_send_gate_honors_720_hour_deadline(test_db, age, expired):
    from app.modules.acquisition.qualification_service import send_gate
    account, group, member, row = await setup(test_db)
    # Keep the membership scope intact while moving both evidence timestamps.
    row.checked_at = datetime.utcnow() - timedelta(hours=age)
    row.expires_at = row.checked_at + timedelta(hours=720)
    await test_db.flush()
    reason = await send_gate(test_db, account.id, group.group_id, "check profile", None)
    assert (reason == "qualification_expired") is expired
    from app.modules.acquisition.automation import AcquisitionAutomationService
    context, context_reason = await AcquisitionAutomationService(test_db)._qualified_ad_context(
        account.id, group.group_id, datetime.utcnow()
    )
    assert (context_reason == "qualification_expired") is expired
    if not expired:
        assert reason is None and context_reason is None and context is not None
