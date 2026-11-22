"""Candidate edits/deletions reach qualification without reading chat history."""
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from telethon.tl import types

from app.core.account.listener_facts import apply_fact, enqueue_fact
from app.core.account.listener_interest import project
from app.core.worker_status import TelegramEventInbox
from app.modules.acquisition import qualification_engine as engine
from tests.unit.test_qualification_engine import baseline, rollout
from tests.unit.growth_efficiency_join_floor.conftest import test_db


@pytest.mark.asyncio
async def test_candidate_edit_in_same_bucket_keeps_both_input_versions(test_db, rollout):
    account, _, member, _, _ = await baseline(test_db)
    now = datetime.utcnow()
    fact = {"kind": "evidence_candidate", "key": "candidate:scope:10", "peer": -1000000000022,
            "message_id": 10, "message_date": now.isoformat(), "at": now.timestamp(),
            "joined_at": member.joined_at.isoformat()}
    await enqueue_fact(test_db, account.id, fact)
    await enqueue_fact(test_db, account.id, fact)
    await enqueue_fact(test_db, account.id, {**fact, "message_date": (now+timedelta(seconds=1)).isoformat()})
    rows = (await test_db.scalars(select(TelegramEventInbox).where(
        TelegramEventInbox.account_id == account.id))).all()
    assert len(rows) == 2
    assert len({json.loads(row.payload_json)["message_date"] for row in rows}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,extra,confirmed", [
    ("deleted", {"message_id": 10}, True),
    ("deletion_candidates", {"ids": [10], "overflow": False}, True),
    ("deletion_candidates", {"ids": [10], "overflow": True}, False),
    ("ad_gap", {}, False),
])
async def test_only_definite_scoped_deletions_reach_candidate_invalidation(
    test_db, rollout, monkeypatch, kind, extra, confirmed,
):
    account, _, member, _, _ = await baseline(test_db)
    projection = AsyncMock()
    monkeypatch.setattr(engine, "project_fact", projection)
    fact = {"kind": kind, "peer": member.telegram_group_id, "at": datetime.utcnow().timestamp(),
            "joined_at": member.joined_at.isoformat(), **extra}
    await apply_fact(test_db, account.id, fact)
    assert projection.await_count == int(confirmed)
    if confirmed:
        projected = projection.await_args.args[2]
        assert projected["kind"] == "evidence_candidate_removed" and projected["message_ids"] == [10]
        projection.reset_mock()
        await apply_fact(test_db, account.id, {**fact, "joined_at": "old-incarnation"})
        projection.assert_not_awaited()


def test_edit_without_promotional_content_removes_old_candidate_hint():
    now = datetime.now(UTC)
    message = types.Message(id=10, peer_id=types.PeerChannel(22), from_id=types.PeerUser(44),
                            date=now-timedelta(hours=1), edit_date=now, message="ordinary conversation")
    envelope = types.UpdateShort(types.UpdateEditChannelMessage(message, 11, 1), now)
    policy = {"selective": True, "loaded_at": time.monotonic(), "ordinary_required": False,
              "owned_chats": set(), "ads": {}, "groups": {-1000000000022: {
                  "joined_at": "2026-11-01T00:00:00", "evidence_needed": True}},
              "verification": {}, "account_id": 2, "self_id": 99}
    _, _, facts = project(envelope, policy)
    assert len(facts) == 1
    assert facts[0]["kind"] == "evidence_candidate_removed"
    assert facts[0]["message_ids"] == [10] and facts[0]["at"] == now.timestamp()
    assert "ordinary conversation" not in json.dumps(facts)
