"""P2 producer gates for qualification scope and immutable snapshots."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.modules.acquisition.p2_dispatch import produce_ad_outbox, produce_join_outbox
from app.modules.acquisition.qualification_cache import (
    get_valid_qualification_snapshot,
    persist_qualification_snapshot,
)


def _fresh_evidence(*, scope: str) -> dict:
    return {
        "authorization_scope": scope,
        "quality_status": "qualified",
        "collection_complete": True,
        "read_status": "fresh",
        "collected_at": datetime.now(UTC).isoformat(),
        "evidence": [{"source": "unit", "allowed": True}],
    }


@pytest.mark.asyncio
async def test_ad_producer_rejects_join_scope_snapshot(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P3_QUALIFICATION_SNAPSHOT", True)
    snapshot = await persist_qualification_snapshot(
        test_db,
        _fresh_evidence(scope="join"),
        target_key="-100500",
        fact_version="0",
        rule_version="rules-v1",
        verdict="allowed",
    )
    assert snapshot is not None
    assert await get_valid_qualification_snapshot(
        test_db,
        target_key="-100500",
        account_id=1,
        required_scope="ad",
    ) is None
    envelope = await get_valid_qualification_snapshot(
        test_db,
        target_key="-100500",
        account_id=1,
    )
    assert envelope is not None
    assert envelope.snapshot_id == snapshot.snapshot_id


@pytest.mark.asyncio
async def test_join_producer_is_fail_closed_without_budget_ledger(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P2_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P2_JOIN_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P2_ACCOUNT_IDS", "1")
    monkeypatch.setattr(settings, "P3_QUALIFICATION_SNAPSHOT", True)
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", False)

    result = await produce_join_outbox(test_db, 1, limit=1)

    assert result == {"enqueued": 0, "skipped": 0, "reason": "budget_ledger_disabled"}


@pytest.mark.asyncio
async def test_ad_producer_is_fail_closed_without_snapshot_lane(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P2_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P2_AD_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P2_ACCOUNT_IDS", "1")
    monkeypatch.setattr(settings, "P3_QUALIFICATION_SNAPSHOT", False)

    result = await produce_ad_outbox(test_db, 1, limit=1)

    assert result == {"enqueued": 0, "skipped": 0, "reason": "p3_snapshot_disabled"}
