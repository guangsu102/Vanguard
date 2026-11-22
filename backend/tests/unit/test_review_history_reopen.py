"""Ordinary page restoration stays independent of new protection admission."""
from dataclasses import replace

import pytest

from app.core.config import settings
from app.modules.acquisition import review_evidence_store as evidence
from app.modules.acquisition import review_read_runtime as runtime
from app.modules.acquisition.group_qualification import EvidenceCollector
from tests.unit.test_group_qualification_sampling import ENTITY
from tests.unit.test_review_history_progress import env as env
from tests.unit.test_review_history_progress import history_calls


@pytest.mark.asyncio
async def test_existing_ordinary_pages_restore_with_admission_disabled_and_no_injected_store(env, monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_EVIDENCE_STORE_ENABLED", False)
    monkeypatch.setattr(settings, "REVIEW_EVIDENCE_STORE_PATH", str(env.store.config.directory))
    monkeypatch.setattr(runtime, "_store", None)
    monkeypatch.setattr(runtime, "_store_path", None)
    # Reopen the existing real fixed file using its small test geometry. The
    # collector receives no store; the runtime still checks the actual path.
    monkeypatch.setattr(evidence, "EvidenceStoreConfig", lambda **kwargs:
                        replace(env.store.config, directory=kwargs["directory"]))
    client = env.client([env.msg(100, age=1), env.msg(50, age=30), env.msg(10, age=54)])
    first_collector = EvidenceCollector(client, account_id=34, history_scope=env.scope,
                                       enable_on_demand_identity=False)
    assert first_collector.history_store is None
    first = await first_collector.collect(ENTITY, now=env.now)
    assert first["history_complete"]
    assert all(row.get("page_key") not in {None, "unavailable"}
               for row in first["collection_progress"]["history_cursors"])
    client.calls.clear()
    second_collector = EvidenceCollector(client, account_id=34, history_scope=env.scope,
                                        enable_on_demand_identity=False)
    second_collector.previous = first
    assert second_collector.history_store is None
    second = await second_collector.collect(ENTITY, now=env.now)
    assert second["history_complete"] and not history_calls(client)
    assert second["history_cached_count"] == 3
    assert (await env.store.list_observations()).data["observations"] == []
    assert list(env.store.config.directory.iterdir()) == [env.store.path]
