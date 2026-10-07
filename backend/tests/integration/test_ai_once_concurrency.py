import asyncio
import os
import uuid
from copy import deepcopy
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.settings_models import SystemSetting
from app.modules.acquisition.qualification_ai import review_semantics
from app.modules.acquisition.automation import GroupAdRulesAuditResult


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("VANGUARD_TEST_POSTGRES_URL"), reason="isolated PG18 required")
async def test_thirty_concurrent_same_material_requests_call_provider_once():
    schema = "ai_once_" + uuid.uuid4().hex
    url = os.environ["VANGUARD_TEST_POSTGRES_URL"]
    admin = create_async_engine(url)
    engine = create_async_engine(url, pool_size=5, max_overflow=0,
                                 connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async def provider(*args):
        await asyncio.sleep(0.1)
        return GroupAdRulesAuditResult(ad_allowed=False, reason="group_rules_ai_unknown")
    call = AsyncMock(side_effect=provider)
    async def assess():
        async with sessions() as db:
            service = Obj(db=db, _ad_policy_llm=lambda: None, _evaluate_group_ad_rules_with_ai=call)
            return await review_semantics(service, deepcopy({"raw_peer_id": 42, "evidence": [{"text": "rules", "source": "full_about"}]}),
                                          Obj(id=2, profile_bio="same"), GroupAdRulesAuditResult(), {})
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(lambda c: SystemSetting.__table__.create(c))
        results = await asyncio.gather(*(assess() for _ in range(30)))
        assert call.await_count == 1
        assert all(result.ad_allowed is False for result in results)
        await assess()
        assert call.await_count == 1
    finally:
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()
