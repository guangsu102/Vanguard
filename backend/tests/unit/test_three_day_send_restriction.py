import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.adaptive_frequency import negative_observation
from app.modules.acquisition.send_restriction import (
    restriction_facts,
    track_restriction,
    verified_long_restriction,
)

NOW = datetime(2026, 9, 26, 12)


def denial(at=NOW, *, duration=None, **extra):
    rights = [] if duration is None else [Obj(send_plain=True, until_date=at + duration)]
    return {
        "collected_at": at.isoformat(),
        "decision": "wait" if duration else "observe",
        "reason": "temporary_send_restriction",
        "quality_status": "observe",
        "permissions": {
            "member": True, "can_send_text": False,
            **restriction_facts(rights, at), **extra,
        },
    }


@pytest.mark.parametrize("hours,expected", [(1, False), (72, False), (72.001, True), (96, True), (720, True)])
def test_explicit_duration_and_survival_use_same_strict_three_day_boundary(hours, expected):
    snapshot = denial(duration=timedelta(hours=hours))
    assert verified_long_restriction(snapshot, NOW) is expected
    assert qualification.automatic_exit_reason(snapshot) == (
        "account_long_send_restriction" if expected else None
    )
    facts = {
        "group_accessible": True, "account_readable": True, "member": True,
        "can_send": False, "member_muted": True, "send_restriction": snapshot["permissions"],
    }
    assert negative_observation(facts, [], NOW) == ("muted" if expected else None)


def test_continuous_denial_requires_observations_beyond_three_days():
    previous = {}
    for hours in (0, 24, 48, 72, 73):
        at = NOW + timedelta(hours=hours)
        current = denial(at, duration=timedelta(hours=12))
        track_restriction(current, previous, at)
        assert current["permissions"]["send_restriction_since"] == NOW.isoformat()
        assert verified_long_restriction(current, at) is (hours > 72)
        previous = current


@pytest.mark.parametrize("interruption", ["read_error", "can_send", "left", "verification", "gap", "successful_ad"])
def test_uncertainty_or_recovery_breaks_continuous_denial(interruption):
    previous = denial()
    track_restriction(previous, {}, NOW)
    for days in (1, 2, 3):
        at = NOW + timedelta(days=days)
        current = denial(at)
        track_restriction(current, previous, at)
        previous = current
    at = NOW + timedelta(days=3, hours=1)
    if interruption == "read_error":
        previous["technical_errors"] = ["FloodWait"]
    elif interruption == "can_send":
        previous["permissions"]["can_send_text"] = True
    elif interruption == "left":
        previous["permissions"]["member"] = False
    elif interruption == "verification":
        previous["permissions"]["verification_pending"] = True
    elif interruption == "gap":
        at += timedelta(hours=27)
    current = denial(at)
    track_restriction(current, previous, at, last_sent_at=at if interruption == "successful_ad" else None)
    assert not verified_long_restriction(current, at)
    assert current["permissions"]["send_restriction_since"] == at.isoformat()


@pytest.mark.parametrize("extra", [
    {"verification_pending": True}, {"verification_required": True},
    {"newcomer_until": "2026-09-27T12:00:00"}, {"slowmode_until": "2026-09-27T12:00:00"},
    {"member": False},
])
def test_long_mute_does_not_override_verification_or_nonmembership(extra):
    snapshot = denial(duration=timedelta(days=7), **extra)
    assert qualification.automatic_exit_reason(snapshot) is None


def test_protected_group_and_transport_failure_never_exit_for_duration():
    snapshot = denial(duration=timedelta(days=7))
    assert qualification.automatic_exit_reason({**snapshot, "protected": True}) is None
    assert qualification.automatic_exit_reason({**snapshot, "technical_errors": ["RpcDeferred"]}) is None


def test_short_mute_keeps_periodic_checks_and_known_long_mute_rejects():
    short = denial(duration=timedelta(hours=60))
    result = qualification.review_schedule(short, {}, NOW)
    assert result == ("wait", "temporary_send_restriction", "completed", NOW + timedelta(hours=2))
    long = denial(duration=timedelta(days=7))
    result = qualification.review_schedule(long, {}, NOW)
    assert result == ("reject", "account_long_send_restriction", "completed", None)


def test_newcomer_unknown_mute_waits_but_explicit_long_ban_exits():
    long = denial(duration=timedelta(days=7))
    qualification.annotate_newcomer_restriction(long, NOW - timedelta(hours=1), NOW)
    assert qualification.automatic_exit_reason(long) == "account_long_send_restriction"
    unknown = denial(permanent_send_restriction_verified=True)
    qualification.annotate_newcomer_restriction(unknown, NOW - timedelta(hours=1), NOW)
    assert qualification.automatic_exit_reason(unknown) is None


@pytest.mark.asyncio
async def test_assess_persists_continuous_denial_and_force_refresh_retains_evidence(test_db, monkeypatch):
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from tests.unit.test_qualification_service import setup

    account, group, member, row = await setup(test_db)
    now = datetime.utcnow()
    previous = denial(now - timedelta(hours=1))
    previous["permissions"].update(
        send_restriction_since=(now - timedelta(days=3, hours=1)).isoformat(),
        send_restriction_observed_at=(now - timedelta(hours=1)).isoformat(),
    )
    row.evidence_json = json.dumps(previous)
    await test_db.commit()
    snapshot = denial(now)
    snapshot.update(quality_reason="verification_or_send_restriction", technical_errors=[], evidence=[])
    client = Obj(get_entity=AsyncMock(return_value=Obj(id=1234567890, megagroup=True)),
                 get_me=AsyncMock(return_value=Obj(id=200)))
    pool = Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=Obj(client=client)), release=AsyncMock())
    runtime = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr(qualification.EvidenceCollector, "collect", AsyncMock(return_value=copy.deepcopy(snapshot)))
    await qualification.assess(runtime, account.id, group, row=row, force_refresh=True)
    stored = json.loads(row.evidence_json)
    assert row.decision == "reject" and row.reason == "account_long_send_restriction"
    assert stored["permissions"]["send_restriction_since"] == previous["permissions"]["send_restriction_since"]
    assert qualification.qualifies_for_automatic_exit(stored)


@pytest.mark.asyncio
async def test_dispatcher_really_runs_two_accounts_concurrently_with_separate_sessions(test_db, monkeypatch):
    from app.core import database
    from app.modules.acquisition import automation
    from app.modules.acquisition.automation import AcquisitionAutomationService, AutomationRunResult

    service = AcquisitionAutomationService(test_db, account_pool=Obj())
    service._resume_expired_membership_ad_pauses = AsyncMock()
    service._list_enabled_ad_bindings = AsyncMock(return_value=[Obj(id=1, account_id=2), Obj(id=2, account_id=3)])
    service._sync_account_pool = AsyncMock()
    monkeypatch.setattr(automation, "get_ad_delivery_execution_settings", AsyncMock(return_value={
        "enabled": True, "dispatcher_batch_size": 2, "max_parallel_accounts": 2, "job_lease_seconds": 300,
    }))
    sessions = []
    @asynccontextmanager
    async def session():
        db = Obj()
        sessions.append(db)
        yield db
    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(AcquisitionAutomationService, "_claim_ad_account_worker_lock", AsyncMock(return_value="lock"))
    monkeypatch.setattr(AcquisitionAutomationService, "_release_ad_account_worker_lock", AsyncMock())
    arrived = set()
    both = asyncio.Event()
    async def worker(self, account_id, **kwargs):
        arrived.add(account_id)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=2)
        async with kwargs["delivery_budget_lock"]:
            assert kwargs["delivery_budget"]["remaining"] > 0
            kwargs["delivery_budget"]["remaining"] -= 1
        return AutomationRunResult()
    monkeypatch.setattr(AcquisitionAutomationService, "_run_ad_delivery_for_account", worker)
    result = await service.run_ad_delivery()
    assert arrived == {2, 3} and len(sessions) == 2 and sessions[0] is not sessions[1]
    assert not result["errors"]
