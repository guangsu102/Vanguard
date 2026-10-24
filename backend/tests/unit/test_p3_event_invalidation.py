from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.account import event_inbox
from app.core.account.listener_facts import enqueue_fact
from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.core.worker_status import TelegramEventInbox
from app.modules.acquisition.qualification_snapshot import QualificationFactVersion

pytestmark = pytest.mark.asyncio


async def test_listener_fact_finish_invalidates_lane_and_advances_fact_version(test_db, monkeypatch):
    test_db.add(TelegramAccount(id=2, identifier="2", session_name="2"))
    await test_db.flush()
    monkeypatch.setattr(settings, "P3_ENTITY_CACHE", True)
    monkeypatch.setattr(settings, "P3_GROUP_FACT_CACHE", True)
    monkeypatch.setattr(settings, "P3_SURVIVAL_CACHE", True)
    monkeypatch.setattr(settings, "P3_QUALIFICATION_SNAPSHOT", True)

    invalidated = []

    async def capture(event):
        invalidated.append(event)
        return 2

    monkeypatch.setattr(
        "app.core.account.read_cache.invalidate_telegram_read_cache", capture
    )
    await enqueue_fact(
        test_db,
        2,
        {
            "kind": "membership",
            "peer": -100123,
            "key": "membership:-100123",
            "at": 100,
            "joined_at": None,
        },
    )
    await test_db.commit()
    row = await event_inbox.claim(test_db, [2], owner="p3-test")
    owner = row.claim_owner
    await event_inbox.finish(test_db, row.id, owner=owner)
    await test_db.commit()

    version = await test_db.get(QualificationFactVersion, str(-100123))
    assert version is not None and version.version == 1
    assert invalidated and invalidated[0]["event_type"] == "membership"
    assert invalidated[0]["target_key"] == -100123
    saved = await test_db.scalar(
        select(TelegramEventInbox).where(TelegramEventInbox.id == row.id)
    )
    assert saved.state == "completed"
