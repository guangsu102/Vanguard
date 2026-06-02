from datetime import datetime, timedelta

import pytest

from app.api.stats import get_stats_funnel, get_stats_sources
from app.modules.acquisition.models import AcquisitionTracking


@pytest.mark.asyncio
async def test_funnel_uses_acquisition_tracking(test_db):
    now = datetime.utcnow()
    test_db.add_all(
        [
            AcquisitionTracking(
                tracking_code="stats_ref_1",
                source_type="telegram_group",
                click_at=now,
                registered_at=now,
                converted_at=now,
                converted=True,
                created_at=now,
            ),
            AcquisitionTracking(
                tracking_code="stats_ref_2",
                source_type="private_reply",
                click_at=now,
                created_at=now,
            ),
        ]
    )
    await test_db.commit()

    response = await get_stats_funnel(
        start_date=(now - timedelta(minutes=1)).isoformat(),
        end_date=(now + timedelta(minutes=1)).isoformat(),
        db=test_db,
    )

    by_stage = {item["stage"]: item["count"] for item in response["data"]}
    assert by_stage == {
        "Tracking": 2,
        "Clicked": 2,
        "Registered": 1,
        "Activated": 1,
    }


@pytest.mark.asyncio
async def test_sources_use_acquisition_source_type(test_db):
    now = datetime.utcnow()
    test_db.add_all(
        [
            AcquisitionTracking(tracking_code="source_ref_1", source_type="telegram_group", created_at=now),
            AcquisitionTracking(tracking_code="source_ref_2", source_type="telegram_group", created_at=now),
            AcquisitionTracking(tracking_code="source_ref_3", source_type="private_reply", created_at=now),
        ]
    )
    await test_db.commit()

    response = await get_stats_sources(
        start_date=(now - timedelta(minutes=1)).isoformat(),
        end_date=(now + timedelta(minutes=1)).isoformat(),
        db=test_db,
    )

    by_source = {item["source"]: item["count"] for item in response["data"]}
    assert by_source["telegram_group"] == 2
    assert by_source["private_reply"] == 1
