"""Terminal rejects must not occupy join admission capacity, whatever wrote them."""
from datetime import datetime, timedelta

import pytest


class Row:
    def __init__(self, *, decision="reject", state="completed", reason="r",
                 evidence=None, next_retry_at=None):
        self.decision = decision
        self.state = state
        self.reason = reason
        import json
        self.evidence_json = json.dumps(evidence or {}, ensure_ascii=False)
        self.next_retry_at = next_retry_at


NOW = datetime(2026, 10, 8, 12, 0, 0)


@pytest.mark.asyncio
async def test_non_ai_terminal_reject_is_exempt_from_capacity():
    # account_permanent_send_restriction and other restriction facts conclude
    # without an AI verdict; they schedule no future work either.
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    row = Row(reason="account_permanent_send_restriction", evidence={})
    assert waiting_for_evidence(row, NOW) is True


@pytest.mark.asyncio
async def test_ai_terminal_reject_still_exempt():
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    row = Row(evidence={"ai_final": True, "ai_decision": "fail"})
    assert waiting_for_evidence(row, NOW) is True


@pytest.mark.asyncio
async def test_pending_collection_refresh_keeps_pressure():
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    row = Row(evidence={"collection_needs_refresh": "ordinary_ad_hint"})
    assert waiting_for_evidence(row, NOW) is False


@pytest.mark.asyncio
async def test_reject_with_timer_stays_pressure():
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    row = Row(next_retry_at=NOW + timedelta(hours=20))
    assert waiting_for_evidence(row, NOW) is False


@pytest.mark.asyncio
async def test_non_completed_reject_stays_pressure():
    from app.modules.acquisition.review_inventory import waiting_for_evidence

    row = Row(state="queued")
    assert waiting_for_evidence(row, NOW) is False
