"""Short-lived workers queue owner work without claiming or buying recognition."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.account import session_exec
from app.core.config import settings
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_ai as ai, qualification_engine as engine
from app.modules.acquisition import qualification_provider as provider, qualification_service as qualification
from app.modules.acquisition.automation import GroupAdRulesAuditResult
from app.modules.acquisition.models import QualificationWork
from tests.unit.test_qualification_engine import baseline


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


@pytest.mark.asyncio
async def test_runner_without_owner_does_not_bootstrap_or_claim(test_db):
    account, _, member, _, work = await baseline(test_db)
    await engine.enqueue(test_db, member, reason="manual")
    await test_db.commit()
    count = await test_db.scalar(select(func.count(QualificationWork.id)))
    service = SimpleNamespace(db=test_db)
    outcome = await engine.run_reviews(service, account_id=account.id)
    assert outcome["processed"] == 0 and outcome["reason"] == "session_owner_required"
    assert work.state == "READY" and work.claim_token is None
    assert await test_db.scalar(select(func.count(QualificationWork.id))) == count
    assert not provider.TASKS


@pytest.mark.asyncio
async def test_direct_ai_without_owner_does_not_mark_attempt_or_call_provider(test_db):
    call = AsyncMock(side_effect=AssertionError("short-lived worker bought recognition"))
    service = SimpleNamespace(db=test_db, _evaluate_group_ad_rules_with_ai=call)
    snapshot = {"raw_peer_id": 42, "group_type": "supergroup",
        "evidence": [{"source": "full_about", "text": "Advertising permitted"}]}
    outcome = await ai.review_semantics(service, snapshot, SimpleNamespace(id=2, profile_bio=""),
        GroupAdRulesAuditResult(), {})
    assert outcome.reason == "group_rules_ai_owner_required"
    assert snapshot["ai_pending"] and snapshot["ai_request_started"] is False
    assert await test_db.scalar(select(SystemSetting).where(SystemSetting.key.like("qualification.ai.%"))) is None
    call.assert_not_awaited()
    assert not provider.TASKS


@pytest.mark.asyncio
async def test_global_explicit_review_queues_owner_and_never_runs_new_engine(test_db, monkeypatch):
    account, _, member, _, work = await baseline(test_db)
    await engine.enqueue(test_db, member, reason="manual")
    await test_db.commit()
    run = AsyncMock(side_effect=AssertionError("global task entered new owner runner"))
    monkeypatch.setattr(engine, "run_reviews", run)
    monkeypatch.setattr(qualification, "_run_reviews", AsyncMock(return_value={"processed": 0, "results": []}))
    from app.modules.acquisition import review_evidence_handoff
    monkeypatch.setattr(review_evidence_handoff, "maintain_evidence_store", AsyncMock())
    outcome = await qualification.run_reviews(SimpleNamespace(db=test_db, account_pool=None), limit=1)
    assert outcome["processed"] == 0
    assert outcome["results"][0]["account_id"] == account.id
    assert outcome["results"][0]["reason"] == "session_owner_required"
    assert not work.claim_token and work.state == "READY"
    run.assert_not_awaited()
    assert engine._publish_wakes.await_count > 0


@pytest.mark.asyncio
async def test_dispatcher_without_owner_defers_new_review_before_service(test_db, monkeypatch):
    from app.core import redis as redis_module
    from app.core.scheduler import growth_dispatch
    from app.modules.acquisition import automation
    account, _, _, _, _ = await baseline(test_db)
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(session_exec, "owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(automation, "AcquisitionAutomationService", lambda *args, **kwargs: pytest.fail("service created without owner"))
    outcome, interval = await growth_dispatch.execute(test_db, "review", account.id)
    assert outcome["reason"] == "session_owner_required" and outcome["processed"] == 0
    assert interval == 30
