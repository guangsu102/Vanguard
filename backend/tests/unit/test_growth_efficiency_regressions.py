"""Regression fixtures for duplicate wakeups and terminal-decision claims."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.models import QualificationActiveGrant
from app.modules.acquisition.qualification_events import account_gap, pending, request
from tests.unit.test_qualification_engine import baseline, clone, run_owner_reviews


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


@pytest.mark.asyncio
async def test_account_gap_observation_cannot_revive_earlier_authorization(test_db):
    account, _, member, old, work = await baseline(test_db)
    await account_gap(test_db, account.id)
    await test_db.commit()
    assert await engine.active_authorization(test_db, member) is None
    assert await engine.queue_pending_events(test_db, account.id) == 1
    claim = await engine.claim_next(test_db, account.id)
    assert claim and claim.event["kinds"] == ["gap"]
    observed = clone(old, member, decision="observe")
    observed.evidence_json = json.dumps({**json.loads(observed.evidence_json), "collection_event": claim.event})
    assert await engine.on_decision(test_db, member, observed, claim=claim)
    await test_db.commit()
    grant = await test_db.scalar(select(QualificationActiveGrant).where(QualificationActiveGrant.membership_id == member.id))
    assert grant.invalidated_at is not None
    assert await engine.active_authorization(test_db, member) is None
    assert (await pending(test_db, member))["kinds"] == ["gap"]
    assert not work.claim_token and not work.claim_until


@pytest.mark.asyncio
async def test_consumed_event_does_not_reopen_completed_work_on_bootstrap(test_db):
    account, _, member, old, work = await baseline(test_db)
    await request(test_db, member, "membership", source_token="permissions-refreshed")
    claim = await engine.claim_next(test_db, account.id)
    final = clone(old, member)
    final.evidence_json = json.dumps({**json.loads(final.evidence_json), "collection_event": claim.event})
    assert await engine.on_decision(test_db, member, final, claim=claim)
    await test_db.commit()
    revision = work.revision
    assert await pending(test_db, member) is None
    assert work.event_version == engine.event_version(None)
    assert json.loads(final.evidence_json)["collection_event"] == claim.event
    for _ in range(3):
        assert await engine.bootstrap_member(test_db, member) is work
    assert work.state == "DONE" and work.revision == revision
    assert await engine.claim_next(test_db, account.id) is None


def test_terminal_provider_failure_waits_for_material_change_or_real_maturity():
    now = datetime.utcnow()
    snapshot = {"ai_final": True, "ai_decision": "fail", "efficiency_v1": True}
    assert engine._wait(snapshot, "group_rules_ai_unavailable", now) == ("WAIT_EVENT", None)
    maturity = now + timedelta(hours=5)
    assert engine._wait({**snapshot, "next_evidence_maturity_at": maturity.isoformat()},
                        "group_rules_ai_unavailable", now) == ("WAIT_MATURITY", maturity)


@pytest.mark.asyncio
async def test_recovered_terminal_failure_never_leaks_a_running_claim_or_reads_telegram(test_db):
    from app.modules.acquisition.automation import AcquisitionAutomationService

    account, _, member, old, work = await baseline(test_db)
    await engine.enqueue(test_db, member, reason="manual")
    claim = await engine.claim_next(test_db, account.id)
    final = clone(old, member, decision="reject")
    final.reason = "group_rules_ai_unavailable"
    final.evidence_json = json.dumps({**json.loads(final.evidence_json), "efficiency_v1": True,
        "ai_final": True, "ai_decision": "fail", "collection_event": claim.event})
    assert await engine.on_decision(test_db, member, final, claim=claim)
    await test_db.commit()
    # Recover an already persisted due provider wait created by an older worker.
    work.state, work.ready_at = "WAIT_PROVIDER", datetime.utcnow() - timedelta(seconds=1)
    await test_db.commit()
    account_pool = SimpleNamespace(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=account_pool)
    result = await run_owner_reviews(service, account_id=account.id)
    assert result["processed"] == 1 and not any(item.get("reason") == "stale_claim" for item in result["results"])
    assert work.state in {"DONE", "WAIT_EVENT"} and not work.claim_token and not work.claim_until
    assert await engine.claim_next(test_db, account.id) is None
    account_pool.acquire_by_id.assert_not_awaited()
