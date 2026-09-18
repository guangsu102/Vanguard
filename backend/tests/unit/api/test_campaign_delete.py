import pytest
from sqlalchemy import select

from app.core.campaign.models import (
    Campaign,
    CampaignExecution,
    CampaignExecutionStatus,
    CampaignScope,
    CampaignTriggerTiming,
    CampaignType,
)
from app.core.security import get_current_user
from app.main import app


@pytest.fixture(autouse=True)
def override_authentication():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "campaign-delete-test",
        "role": "admin",
    }
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_delete_campaign_removes_executions_before_parent(client, test_db):
    campaign = Campaign(
        name="Campaign with execution",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing=CampaignTriggerTiming.MANUAL,
        enabled=False,
    )
    test_db.add(campaign)
    await test_db.flush()

    execution = CampaignExecution(
        campaign_id=campaign.id,
        status=CampaignExecutionStatus.COMPLETED,
        trigger_timing=CampaignTriggerTiming.MANUAL.value,
        delivered=True,
    )
    test_db.add(execution)
    await test_db.commit()
    campaign_id = campaign.id
    execution_id = execution.id

    response = await client.delete(f"/api/campaigns/{campaign_id}")

    assert response.status_code == 204
    assert await test_db.scalar(
        select(Campaign).where(Campaign.id == campaign_id)
    ) is None
    assert await test_db.scalar(
        select(CampaignExecution).where(CampaignExecution.id == execution_id)
    ) is None
