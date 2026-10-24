"""Crash recovery and invalidation contracts for P1 qualification stages."""

import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.automation import GroupAdRulesAuditResult
from app.modules.acquisition.models import GroupQualificationAudit
from tests.unit.test_qualification_service import setup


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", True)
    monkeypatch.setattr("app.core.metrics.record_checkpoint", AsyncMock())


def test_revision_invalidates_profile_rules_cycle_and_retains_event_generation():
    now = datetime.utcnow()
    event = {"token": "one", "queued": False}
    options = {"profile": "bio", "rules_identity": "rules", "review_cycle": "cycle"}
    original = qualification.checkpoint_revision(60, now, event, **options)
    assert qualification.checkpoint_revision(60, now, {**event, "queued": True}, **options) == original
    for key in options:
        assert qualification.checkpoint_revision(60, now, event, **{**options, key: "changed"}) != original
    assert qualification.checkpoint_revision(60, now, {"token": "two"}, **options) != original
    assert qualification.checkpoint_revision(60, now + timedelta(seconds=1), event, **options) != original


@pytest.mark.asyncio
async def test_disabled_checkpoint_does_not_change_or_commit_legacy_work(test_db, monkeypatch):
    _, _, _, row = await setup(test_db, decision="unknown")
    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", False)
    await qualification.save_checkpoint(test_db, row, stage="collection", revision="one", payload={"facts": True})
    assert row.checkpoint_json is None
    assert not qualification.checkpoint_reusable(row, stage="collection", revision="one")
    assert not qualification.transient_review_wait("TimeoutError")


@pytest.mark.asyncio
async def test_stage_commits_survive_rollback_and_ai_preserves_collection(test_db, enabled):
    _, _, _, row = await setup(test_db, decision="unknown")
    await qualification.save_checkpoint(test_db, row, stage="collection", revision="facts", payload={"facts": [1]})
    await qualification.save_checkpoint(test_db, row, stage="ai", revision="material", payload={"verdict": "pass"})
    await test_db.rollback()
    await test_db.refresh(row)
    assert qualification.checkpoint_reusable(row, stage="collection", revision="facts")
    assert qualification.checkpoint_payload(row, stage="collection") == {"facts": [1]}
    assert qualification.checkpoint_reusable(row, stage="ai", revision="material")
    await qualification.save_checkpoint(test_db, row, stage="collection", revision="new-facts", payload={"facts": [2]})
    assert not qualification.checkpoint_reusable(row, stage="ai", revision="material")


@pytest.mark.parametrize("reason", ["group_rules_ai_queued", "group_rules_ai_unavailable", "group_rules_ai_capacity_unavailable", "group_rules_ai_attempt_interrupted"])
def test_incomplete_semantics_cannot_be_saved_as_completed(reason):
    assert not qualification.checkpoint_ai_complete(
        GroupAdRulesAuditResult(reason=reason), {"ai_final": True, "ai_pending": False}
    )


@pytest.mark.parametrize("reason", ["telegram_read_budget", "AccountOperationLeaseBusy", "AccountSessionLeaseBusy", "TimeoutError", "network_failed"])
def test_repeated_transport_deferral_keeps_retry_and_failure_counter(enabled, reason):
    now = datetime.utcnow()
    prior = {"technical_failures": 3, "checked_at": (now - timedelta(days=2)).isoformat()}
    snapshot = {"decision": "technical_wait", "reason": reason, "technical_errors": ["TimeoutError"]}
    decision, _, state, retry = qualification.review_schedule(snapshot, prior, now)
    assert (decision, state) == ("technical_wait", "completed")
    assert snapshot["technical_failures"] == 3
    assert retry > now


@pytest.mark.asyncio
async def test_crashes_between_stages_reuse_durable_collection_and_completed_ai(test_db, monkeypatch, enabled):
    account, group, _, row = await setup(test_db, decision="unknown")
    client = Obj(get_entity=AsyncMock(return_value=Obj(id=group.group_id)), get_me=AsyncMock(return_value=Obj(id=200)))
    pool = Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=Obj(client=client)), release=AsyncMock())
    facts = {
        "quality_status": "qualified", "quality_reason": "quality_verified", "member_count": 1000,
        "evidence": [{"source": "full_about", "text": "Members may advertise", "sender_role": "group_metadata"}],
        "permissions": {"member": True, "can_send_text": True}, "roles": {},
        "technical_errors": [], "unknowns": [],
    }
    collect = AsyncMock(side_effect=lambda *a, **kw: copy.deepcopy(facts))
    monkeypatch.setattr(qualification, "EvidenceCollector", lambda *a, **kw: Obj(collect=collect))
    fake = Obj(db=test_db, account_pool=pool, _join_review_account_block_reason=lambda *_: None,
               _telegram_entity_matches_group_id=lambda *_: True, _evaluate_group_ad_rules=lambda *_: GroupAdRulesAuditResult())
    semantic = AsyncMock(side_effect=TimeoutError("crash after collection"))
    monkeypatch.setattr("app.modules.acquisition.qualification_ai.review_semantics", semantic)
    with pytest.raises(TimeoutError):
        await qualification.assess(fake, account.id, group, row=row)
    row_id = row.id
    await test_db.rollback()
    row = await test_db.get(GroupQualificationAudit, row_id, populate_existing=True)
    assert qualification.checkpoint_payload(row, stage="collection")["evidence"] == facts["evidence"]

    async def recognized(service, snapshot, account, local, limits, **kwargs):
        snapshot.update(ai_final=True, ai_pending=False, ai_decision="pass")
        return GroupAdRulesAuditResult(ad_allowed=True, policy_mode="soft_ad_allowed", confidence=99)

    semantic.side_effect = recognized
    assert (await qualification.assess(fake, account.id, group, row=row)).passed
    # Roll back the final verdict only; the committed stage remains sufficient
    # for another process to finish without another Telegram or model call.
    await test_db.rollback()
    row = await test_db.get(GroupQualificationAudit, row_id, populate_existing=True)
    await test_db.refresh(group)
    assert (await qualification.assess(fake, 2, group, row=row)).passed
    assert collect.await_count == 1
    assert semantic.await_count == 2
    assert qualification.checkpoint_payload(row, stage="collection")["collection_complete"] is True


@pytest.mark.asyncio
async def test_observation_and_maturity_require_new_collection(test_db, enabled):
    _, _, _, row = await setup(test_db, decision="observe")
    now = datetime.utcnow()
    await qualification.save_checkpoint(test_db, row, stage="collection", revision="one", payload={
        "collected_at": now.isoformat(), "collection_complete": True,
    })
    assert not qualification.checkpoint_collection_current(row, {}, now)
    assert qualification.checkpoint_collection_current(row, {"ai_pending": True}, now)
    assert not qualification.checkpoint_collection_current(row, {
        "ai_pending": True, "next_evidence_maturity_at": (now - timedelta(seconds=1)).isoformat(),
    }, now)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "exception"])
async def test_transport_wait_after_claim_does_not_consume_attempt(test_db, monkeypatch, enabled, failure):
    account, _, _, row = await setup(test_db, decision="unknown")
    row.state = "queued"
    row.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    await test_db.commit()
    monkeypatch.setattr(qualification, "ensure_membership_reviews", AsyncMock())
    monkeypatch.setattr("app.modules.acquisition.qualification_identity_refresh.queue_identity_refreshes", AsyncMock())
    monkeypatch.setattr("app.core.account.read_schedule.read_wait", AsyncMock(return_value=None))

    async def interrupted(actor, account_id, group, *, row):
        if failure == "exception":
            raise TimeoutError("network unavailable")
        row.state, row.decision, row.reason = "completed", "technical_wait", "TimeoutError"
        row.evidence_json = json.dumps({"technical_errors": ["TimeoutError"]})
        row.next_retry_at = datetime.utcnow() + timedelta(seconds=60)
        return Obj(passed=False, reason="join_audit_failed")

    monkeypatch.setattr(qualification, "assess", interrupted)
    result = await qualification.run_reviews(Obj(db=test_db), account_id=account.id, limit=1)
    await test_db.refresh(row)
    assert row.attempts == 0
    assert row.next_retry_at > datetime.utcnow()
    assert result.get("processed", 0) + result.get("failed", 0) == 1
