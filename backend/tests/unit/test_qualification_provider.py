"""A paid AI request waits off the account execution path and is never replayed."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.core.config import settings
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_ai as ai, qualification_engine as engine
from app.modules.acquisition import qualification_provider as provider
from app.modules.acquisition.automation import AcquisitionAutomationService, GroupAdRulesAuditResult
from tests.unit.test_qualification_engine import baseline, run_owner_reviews


@pytest_asyncio.fixture(autouse=True)
async def rollout(monkeypatch, test_db):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())
    yield
    tasks = list(provider.TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def ready_material(db):
    account, group, member, old, work = await baseline(db)
    await engine.enqueue(db, member, reason="manual")
    snapshot = json.loads(old.evidence_json)
    snapshot.update(ai_pending=True, ai_final=False, ai_request_started=False,
        quality_reason="evidence_sufficient", quality_status="qualified", collection_complete=True,
        collected_at=datetime.utcnow().isoformat(), collection_event=None, decision="technical_wait",
        evidence=[{"source": "full_about", "text": "欢迎讨论技术", "sender_role": "admin"}])
    work.progress_json = json.dumps(snapshot)
    await db.commit()
    return account, group, member, work, snapshot


def service_for(db, started, release, calls):
    class Service(AcquisitionAutomationService):
        def _ad_policy_llm(self):
            return SimpleNamespace()

        async def _evaluate_group_ad_rules_with_ai(self, evidence, local, limits):
            assert self.db is not db
            assert not self.db.in_transaction() and self.db.sync_session._transaction is None
            calls.append(self.db)
            started.set()
            await release.wait()
            assert not self.db.in_transaction()
            return GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_disallow_ads")
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), release=AsyncMock(),
        acquire_by_id=AsyncMock(side_effect=AssertionError("provider retry touched Telegram")))
    return Service(db, account_pool=pool)


@pytest.mark.asyncio
async def test_blocked_provider_returns_wait_without_holding_account_claim_or_db(test_db):
    account, _, member, work, _ = await ready_material(test_db)
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    service = service_for(test_db, started, release, calls)
    result = await asyncio.wait_for(run_owner_reviews(service, account_id=account.id), 5)
    await asyncio.wait_for(started.wait(), 2)
    assert result["processed"] == 1 and work.state == "WAIT_PROVIDER"
    assert not work.claim_token and not work.claim_until and work.next_step_reads == 0
    assert await engine.initial_slots(test_db, account.id) == 0
    assert len(provider.TASKS) == 1 and len(calls) == 1
    await test_db.commit()
    assert not test_db.in_transaction()
    # The provider owns no account claim, so another local operation can claim
    # this account while the first request is still blocked at its upstream.
    await engine.enqueue(test_db, member, reason="manual")
    other = await engine.claim_next(test_db, account.id)
    assert other is not None
    await test_db.commit()
    release.set()
    await asyncio.wait_for(asyncio.gather(*list(provider.TASKS)), 5)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_fast_provider_completion_before_defer_leaves_durable_ready_work(test_db, monkeypatch):
    account, _, _, work, _ = await ready_material(test_db)
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    release.set()
    service = service_for(test_db, started, release, calls)
    original = engine.defer
    async def after_provider(*args, **kwargs):
        await asyncio.wait_for(asyncio.gather(*list(provider.TASKS)), 5)
        return await original(*args, **kwargs)
    monkeypatch.setattr(engine, "defer", after_provider)
    result = await run_owner_reviews(service, account_id=account.id)
    assert result["processed"] == 1 and work.state == "READY"
    assert not work.claim_token and work.ready_at <= datetime.utcnow()
    monkeypatch.setattr(engine, "defer", original)
    await run_owner_reviews(service, account_id=account.id)
    assert work.state == "WAIT_EVENT" and len(calls) == 1
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_provider_keeps_once_result_and_never_repurchases(test_db):
    account, _, _, work, snapshot = await ready_material(test_db)
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    service = service_for(test_db, started, release, calls)
    await run_owner_reviews(service, account_id=account.id)
    await started.wait()
    tasks = list(provider.TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await test_db.refresh(work)
    assert work.state == "READY"
    await run_owner_reviews(service, account_id=account.id)
    assert len(calls) == 1 and work.state == "WAIT_EVENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_restart_with_attempted_marker_waits_or_fails_without_repurchase(test_db, expired):
    account, _, _, _, snapshot = await ready_material(test_db)
    material = ai.semantic_key(snapshot, [], account, {})
    test_db.add(SystemSetting(key="qualification.ai.once."+material, value=json.dumps({
        "phase": "attempted", "background": True,
        "deadline_at": (datetime.utcnow()+timedelta(seconds=-1 if expired else 90)).isoformat(),
        "result": ai.saved_result(GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_attempt_interrupted")),
    })))
    await test_db.commit()
    call = AsyncMock(side_effect=AssertionError("restarted attempted recognition was purchased twice"))
    service = SimpleNamespace(db=test_db, _ad_policy_llm=lambda: SimpleNamespace(), _evaluate_group_ad_rules_with_ai=call)
    result = await ai.review_semantics(service, deepcopy(snapshot), account, GroupAdRulesAuditResult(), {})
    assert result.reason == ("group_rules_ai_attempt_interrupted" if expired else "group_rules_ai_pending")
    assert not provider.TASKS
    call.assert_not_awaited()
