"""Real send-gate regressions for historical uncertain and resolved deliveries."""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountOperationConfig
from app.core.config import settings
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog
from app.modules.acquisition.qualification_service import send_gate
from tests.unit.test_qualification_engine import baseline


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "send_unknown", "reconciliation_required", "failed", "skipped"])
async def test_adaptive_send_gate_fences_old_unknown_but_releases_definite_no_send(test_db, status):
    account, group, _, audit, _ = await baseline(test_db)
    test_db.add(AdCampaign(id=901, name="send fence", enabled=True))
    test_db.add(AccountOperationConfig(account_id=account.id, enabled=True,
        auto_ads_enabled=True, adaptive_ads_enabled=True, dynamic_capacity_enabled=True))
    test_db.add(AdDeliveryLog(account_id=account.id, group_id=group.id,
        ad_campaign_id=901, telegram_group_id=group.group_id, status=status, created_at=datetime.utcnow()-timedelta(days=5),
        qualification_context_json=audit.evidence_json))
    await test_db.commit()
    reason = await send_gate(test_db, account.id, group.group_id, "plain profile", None)
    assert reason == ("qualification_delivery_reconciliation_required" if status in {
        "unknown", "send_unknown", "reconciliation_required"} else None)


@pytest.mark.asyncio
async def test_resolved_success_still_counts_toward_daily_quota(test_db):
    from app.modules.acquisition.adaptive_frequency import FrequencyService
    account, group, _, audit, _ = await baseline(test_db)
    test_db.add(AdCampaign(id=901, name="resolved receipt fence", enabled=True))
    test_db.add(AccountOperationConfig(account_id=account.id, enabled=True,
        auto_ads_enabled=True, adaptive_ads_enabled=True, dynamic_capacity_enabled=True))
    state = await FrequencyService(test_db).state(group.group_id, json.loads(audit.evidence_json), create=True)
    snapshot = {**json.loads(audit.evidence_json), "frequency": FrequencyService.context(state)}
    test_db.add(AdDeliveryLog(account_id=account.id, group_id=group.id,
        ad_campaign_id=901, telegram_group_id=group.group_id, status="success", telegram_message_id=91,
        sent_at=datetime.utcnow()-timedelta(hours=1), survival_status="survived",
        qualification_context_json=json.dumps(snapshot)))
    await test_db.commit()
    assert await send_gate(test_db, account.id, group.group_id, "plain profile", None) == "frequency_group_daily_cap"
