from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.settings_models import SystemSetting
from app.modules.acquisition import candidate_inventory, qualification_service
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.candidate_preview import CandidatePreview


@pytest.mark.asyncio
async def test_ranker_filters_fresh_rejoin_block_before_preview_or_join(monkeypatch, test_db):
    group = SimpleNamespace(
        id=427,
        group_id=-1003690554044,
        username="zhihu01",
        title="知乎-日结兼职",
    )
    fact = candidate_inventory.record(
        group,
        2,
        CandidatePreview(
            status="metadata_checked",
            peer_namespace="channel",
            member_count=52,
        ),
        datetime.utcnow(),
    )
    await candidate_inventory.save(test_db, 2, group.id, fact)
    test_db.add(
        SystemSetting(
            key="automation.group_qualification",
            value='{"enabled": true}',
        )
    )
    await test_db.commit()

    monkeypatch.setattr(
        qualification_service,
        "policy",
        AsyncMock(return_value={"enabled": True}),
    )
    import app.modules.acquisition.qualification_join_gate as gate_module

    gate = AsyncMock(
        return_value="qualification_rejoin_requires_changed_evidence_clearance"
    )
    monkeypatch.setattr(gate_module, "qualification_join_gate", gate)

    service = object.__new__(AcquisitionAutomationService)
    service.db = test_db
    service.logger = Mock()
    service.account_pool = SimpleNamespace(
        acquire_by_id=AsyncMock(),
        release=AsyncMock(),
    )

    assert await service._rank_pending_join_candidates(2, [group]) == []
    assert service._candidate_preview_ready_ids == set()
    gate.assert_awaited_once()
    service.account_pool.acquire_by_id.assert_not_awaited()
