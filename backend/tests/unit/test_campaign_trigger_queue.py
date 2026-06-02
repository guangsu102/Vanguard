import pytest
import importlib

from app.core.campaign.models import Campaign, CampaignScope, CampaignType
from app.modules.guardian.models import GroupCampaignTriggerEvent

campaigns_api = importlib.import_module("app.api.campaigns")


class DummyResult:
    id = "campaign-task-123"


@pytest.mark.asyncio
async def test_managed_group_manual_trigger_is_queued(test_db, monkeypatch):
    campaign = Campaign(
        name="manual-broadcast-test",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing="manual",
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    calls = {}

    def fake_apply_async(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return DummyResult()

    monkeypatch.setattr(campaigns_api.execute_campaign_rewards, "apply_async", fake_apply_async)

    response = await campaigns_api.trigger_campaign(campaign.id, db=test_db)

    assert calls["kwargs"] == {"args": [campaign.id], "queue": "default"}
    assert response["data"]["queued"] is True
    assert response["data"]["task_id"] == "campaign-task-123"
