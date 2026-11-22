"""Durable exact-fact replay identities stay scoped to a membership incarnation."""
from datetime import timedelta

import pytest

from app.modules.acquisition.qualification_events import acknowledge, pending, request
from tests.unit.test_qualification_engine import baseline, rollout
from tests.unit.growth_efficiency_join_floor.conftest import test_db


pytestmark = pytest.mark.asyncio


async def test_consumed_source_replay_does_not_reopen_work(test_db, rollout):
    _, _, member, _, work = await baseline(test_db)
    await request(test_db, member, "rules", message_ids=[10], source_token="edit-a")
    first = await pending(test_db, member)
    assert await acknowledge(test_db, member, first)
    await test_db.commit()
    version = work.revision
    await request(test_db, member, "rules", message_ids=[10], source_token="edit-a")
    assert await pending(test_db, member) is None
    assert work.revision == version


async def test_older_exact_source_replay_preserves_new_pending_event(test_db, rollout):
    _, _, member, _, work = await baseline(test_db)
    await request(test_db, member, "rules", message_ids=[10], source_token="edit-a")
    first = await pending(test_db, member)
    await request(test_db, member, "rules", message_ids=[10], source_token="edit-b")
    second = await pending(test_db, member)
    version = work.revision
    await request(test_db, member, "rules", message_ids=[10], source_token="edit-a")
    assert await pending(test_db, member) == second
    assert work.revision == version
    assert not await acknowledge(test_db, member, first)
    assert await acknowledge(test_db, member, second)
    await request(test_db, member, "rules", message_ids=[10], source_token="edit-a")
    assert await pending(test_db, member) is None


async def test_source_identity_includes_kind_and_membership_incarnation(test_db, rollout):
    _, _, member, _, _ = await baseline(test_db)
    await request(test_db, member, "rules", source_token="same-source")
    assert await acknowledge(test_db, member, await pending(test_db, member))
    await request(test_db, member, "membership", source_token="same-source")
    assert (await pending(test_db, member))["kinds"] == ["membership"]
    assert await acknowledge(test_db, member, await pending(test_db, member))
    member.joined_at += timedelta(seconds=1)
    await request(test_db, member, "rules", source_token="same-source")
    assert (await pending(test_db, member))["kinds"] == ["rules"]


async def test_source_without_retained_identity_is_never_assumed_consumed(test_db, rollout):
    _, _, member, _, _ = await baseline(test_db)
    for index in range(70):
        await request(test_db, member, "rules", source_token=f"edit-{index}")
        assert await acknowledge(test_db, member, await pending(test_db, member))
    await request(test_db, member, "rules", source_token="edit-0")
    assert await pending(test_db, member)
