"""A blocked real provider task cannot occupy the Telegram session executor."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.core.account import session_exec
from app.core.config import settings
from app.modules.acquisition import qualification_engine as engine, qualification_provider as provider
from app.modules.acquisition.automation import AcquisitionAutomationService, GroupAdRulesAuditResult
from tests.fakes.session_exec_redis import FakeRedis
from tests.unit.test_qualification_provider import ready_material


@pytest_asyncio.fixture(autouse=True)
async def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())
    yield
    remaining = list(provider.TASKS)
    for task in remaining:
        task.cancel()
    if remaining:
        await asyncio.gather(*remaining, return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_provider_releases_real_single_slot_owner_for_same_account_ads_and_join(test_db):
    account, _, _, work, _ = await ready_material(test_db)
    started, release = asyncio.Event(), asyncio.Event()
    provider_contexts, actual_owner_stages = [], []
    actor_account = getattr(session_exec, "current_actor_account", lambda: None)
    class Service(AcquisitionAutomationService):
        def _ad_policy_llm(self):
            return SimpleNamespace()

        async def _evaluate_group_ad_rules_with_ai(self, evidence, local, limits):
            from app.core.account.read_work import current
            from app.modules.acquisition.review_batch import current_batch
            assert self.db is not test_db and not self.db.in_transaction()
            provider_contexts.append(actor_account())
            assert actor_account() is None
            assert current(account.id) is None and current_batch.get() is None
            started.set()
            await release.wait()
            return GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_disallow_ads")
    pool = SimpleNamespace(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(), release=AsyncMock())
    service = Service(test_db, account_pool=pool)
    async def execute_stage(identity, stage):
        assert identity == account.id
        if hasattr(session_exec, "current_actor_account"):
            assert actor_account() == identity
        actual_owner_stages.append(stage)
        if stage == "review":
            return await engine.run_reviews(service, account_id=identity), 30
        # The real owner executor must admit both protected operations before
        # the blocked provider is released. No real Telegram write is needed.
        assert stage in {"ads", "join"} and started.is_set() and not release.is_set()
        assert work.state == "WAIT_PROVIDER" and not work.claim_token
        return {"processed": 1, "admitted_stage": stage}, 30
    cache = FakeRedis()
    server = asyncio.create_task(session_exec.serve_requests(cache, owns=lambda identity: identity == account.id,
        execute_stage=execute_stage, poll_interval=0.001, max_concurrency=1))
    try:
        reply = await session_exec.forward(account.id, "review", client=cache, timeout=5)
        assert reply["result"]["processed"] == 1
        await asyncio.wait_for(started.wait(), 2)
        assert len(provider.TASKS) == 1 and provider_contexts == [None]
        replies = await asyncio.wait_for(asyncio.gather(*(
            session_exec.forward(account.id, stage, client=cache, timeout=3) for stage in ("ads", "join")
        )), 4)
        assert [reply["result"]["admitted_stage"] for reply in replies] == ["ads", "join"]
        assert actual_owner_stages == ["review", "ads", "join"]
        assert not release.is_set() and not work.claim_until
        pool.acquire_by_id.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(*list(provider.TASKS), return_exceptions=True)
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
    assert actor_account() is None
    cache.assert_channel_empty()
