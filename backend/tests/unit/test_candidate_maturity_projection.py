"""Real local projection/enqueue/hint regression; never grants from a callback."""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.callback_evidence import hint_ids, remove_hints, save_hint, trim
from app.modules.acquisition.models import QualificationWork
from tests.unit.test_qualification_service import setup
from tests.unit.growth_efficiency_join_floor.conftest import test_db


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


def candidate(member, message_id, stamp, at):
    return {"kind": "evidence_candidate", "key": f"candidate:{message_id}",
            "peer": member.telegram_group_id, "at": at.timestamp(),
            "joined_at": member.joined_at.isoformat(), "message_id": message_id,
            "message_date": stamp.isoformat()}


async def target(db):
    account, group, member, audit = await setup(db, decision="observe")
    return member, datetime.utcnow().replace(microsecond=0)


async def work_for(db, member):
    return await db.scalar(select(QualificationWork).where(
        QualificationWork.membership_id == member.id,
        QualificationWork.state != "STALE"))


@pytest.mark.asyncio
async def test_later_candidate_preserves_earlier_maturity_and_exact_replay(test_db):
    member, now = await target(test_db)
    await engine.project_fact(test_db, member, candidate(member, 1, now-timedelta(hours=23), now), now=now)
    work = await work_for(test_db, member)
    earlier = now+timedelta(hours=1)
    assert (work.state, work.ready_at) == ("WAIT_MATURITY", earlier)
    later = now+timedelta(minutes=5)
    fact = candidate(member, 2, later, later)
    await engine.project_fact(test_db, member, fact, now=later)
    assert (work.state, work.ready_at) == ("WAIT_MATURITY", earlier)
    assert await hint_ids(test_db, member, later) == [1, 2]
    before = (work.state, work.ready_at, work.revision, work.event_version)
    await engine.project_fact(test_db, member, fact, now=later)
    assert before == (work.state, work.ready_at, work.revision, work.event_version)
    assert await engine.active_authorization(test_db, member, now=later) is None


@pytest.mark.asyncio
async def test_mature_candidate_remains_runnable_after_new_candidate(test_db):
    member, now = await target(test_db)
    await engine.project_fact(test_db, member, candidate(member, 1, now-timedelta(hours=25), now), now=now)
    work = await work_for(test_db, member)
    first_due = work.ready_at
    later = now+timedelta(seconds=1)
    await engine.project_fact(test_db, member, candidate(member, 2, later, later), now=later)
    assert work.state == "READY" and work.ready_at == first_due
    assert await hint_ids(test_db, member, later) == [1, 2]
    assert await engine.active_authorization(test_db, member, now=later) is None


@pytest.mark.asyncio
async def test_edit_resets_only_that_candidates_age_and_delayed_old_version_cannot_restore_it(test_db):
    member, now = await target(test_db)
    old = candidate(member, 1, now-timedelta(hours=23), now)
    await engine.project_fact(test_db, member, old, now=now)
    work = await work_for(test_db, member)
    edited = now+timedelta(minutes=5)
    await engine.project_fact(test_db, member, candidate(member, 1, edited, edited), now=edited)
    assert (work.state, work.ready_at) == ("WAIT_MATURITY", edited+timedelta(hours=24))
    revision = work.revision
    await engine.project_fact(test_db, member, old, now=edited)
    assert work.revision == revision and work.ready_at == edited+timedelta(hours=24)
    await engine.project_fact(test_db, member, candidate(member, 2, now-timedelta(hours=22), edited), now=edited)
    assert work.ready_at == now+timedelta(hours=2)
    await engine.project_fact(test_db, member, candidate(member, 1, edited+timedelta(minutes=1), edited+timedelta(minutes=1)),
                              now=edited+timedelta(minutes=1))
    assert work.ready_at == now+timedelta(hours=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["WAIT_BUDGET", "WAIT_PROVIDER", "READY"])
async def test_new_candidate_cannot_replace_existing_wait_or_technical_retry(test_db, state):
    member, now = await target(test_db)
    work = await engine.enqueue(test_db, member, now=now)
    work.state, work.ready_at, work.reason = state, now+timedelta(minutes=15), "existing_blocker"
    work.progress_json = json.dumps({"collection_progress": {"history_cursors": [123]}, "ai_material_key": "once"})
    await test_db.commit()
    await engine.project_fact(test_db, member, candidate(member, 1, now, now), now=now)
    assert work.state == state and work.ready_at == now+timedelta(minutes=15)
    assert work.reason == "existing_blocker"
    assert json.loads(work.progress_json)["collection_progress"]["history_cursors"] == [123]
    assert json.loads(work.progress_json)["ai_material_key"] == "once"


@pytest.mark.asyncio
async def test_new_candidate_invalidates_old_claim_without_delaying_continuation(test_db):
    member, now = await target(test_db)
    work = await engine.enqueue(test_db, member, now=now)
    claim = await engine.claim_next(test_db, member.account_id, now=now)
    assert claim and work.state == "RUNNING"
    later = now+timedelta(seconds=1)
    await engine.project_fact(test_db, member, candidate(member, 1, later, later), now=later)
    assert work.state == "READY" and work.ready_at == later
    assert work.revision > claim.revision and work.claim_token is None
    assert await engine._claimed(test_db, claim) == (None, None)


@pytest.mark.asyncio
async def test_callback_does_not_release_unfinished_new_join_first_collection(test_db):
    member, now = await target(test_db)
    work = await engine.enqueue(test_db, member, now=now, initial_collection_pending=True)
    await engine.project_fact(test_db, member, candidate(member, 1, now, now), now=now)
    assert work.state == "READY" and work.initial_collection_pending is True


@pytest.mark.asyncio
async def test_history_derived_maturity_is_retained_for_unrelated_candidate(test_db):
    member, now = await target(test_db)
    work = await engine.enqueue(test_db, member, now=now)
    work.state, work.ready_at = "WAIT_MATURITY", now+timedelta(minutes=15)
    await engine.project_fact(test_db, member, candidate(member, 1, now, now), now=now)
    assert (work.state, work.ready_at) == ("WAIT_MATURITY", now+timedelta(minutes=15))


@pytest.mark.asyncio
async def test_deleted_hint_removed_without_discarding_other_candidate_or_member_scope(test_db):
    member, now = await target(test_db)
    await engine.project_fact(test_db, member, candidate(member, 1, now-timedelta(hours=23), now), now=now)
    await engine.project_fact(test_db, member, candidate(member, 2, now-timedelta(hours=22), now), now=now)
    await engine.project_fact(test_db, member, {"kind": "evidence_candidate_removed", "message_ids": [1],
        "joined_at": member.joined_at.isoformat(), "at": now.timestamp()}, now=now)
    assert await hint_ids(test_db, member, now) == [2]
    work = await work_for(test_db, member)
    assert work.state == "READY" and work.ready_at == now
    old_joined = member.joined_at
    member.joined_at += timedelta(seconds=1)
    assert await hint_ids(test_db, member, now) == []
    assert not await remove_hints(test_db, member, [2], now)
    await engine.project_fact(test_db, member, {"kind": "evidence_candidate_removed", "message_ids": [2],
        "joined_at": old_joined.isoformat(), "at": now.timestamp()}, now=now)
    assert work.state == "READY"


def test_bounded_hints_preserve_nearest_maturity_and_mature_first():
    now = datetime(2026, 11, 6, 12)
    items = [{"id": i+1, "date": (now-timedelta(minutes=i*6)).isoformat()} for i in range(700)]
    kept = trim(items, now)
    assert len(kept) == 80
    recent = [item for item in kept if datetime.fromisoformat(item["date"]) > now-timedelta(hours=24)]
    assert len(recent) == 40
    assert min(datetime.fromisoformat(item["date"]) for item in recent) == now-timedelta(hours=23, minutes=54)
    assert kept[:6] == [item for item in kept if item not in recent][:6]


@pytest.mark.asyncio
async def test_expired_candidates_do_not_supply_an_immediate_obligation(test_db):
    member, now = await target(test_db)
    await save_hint(test_db, member, candidate(member, 1, now-timedelta(hours=71), now), now)
    later = now+timedelta(hours=2)
    await engine.project_fact(test_db, member, candidate(member, 2, later, later), now=later)
    work = await work_for(test_db, member)
    assert await hint_ids(test_db, member, later) == [2]
    assert work.state == "WAIT_MATURITY" and work.ready_at == later+timedelta(hours=24)


@pytest.mark.asyncio
async def test_unrelated_new_candidate_keeps_existing_authorization_and_completed_work(test_db):
    _, _, member, audit = await setup(test_db)
    now = datetime.utcnow()
    work = await engine.bootstrap_member(test_db, member, now=now)
    assert await engine.active_authorization(test_db, member, now=now) is not None
    before = (work.state, work.ready_at, work.revision)
    await engine.project_fact(test_db, member, candidate(member, 999, now, now), now=now)
    assert (work.state, work.ready_at, work.revision) == before
    assert await engine.active_authorization(test_db, member, now=now) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id,invalidated", [(101, True), (999, False)])
async def test_deletion_invalidates_only_grants_referencing_that_message(test_db, message_id, invalidated):
    _, _, member, audit = await setup(test_db)
    now = datetime.utcnow()
    work = await engine.bootstrap_member(test_db, member, now=now)
    original = audit.evidence_json
    assert await engine.active_authorization(test_db, member, now=now) is not None
    await engine.project_fact(test_db, member, {"kind": "evidence_candidate_removed", "message_ids": [message_id],
        "joined_at": member.joined_at.isoformat(), "at": now.timestamp()}, now=now)
    assert (await engine.active_authorization(test_db, member, now=now) is None) is invalidated
    assert audit.evidence_json == original  # The audited conclusion remains immutable.
    assert work.state == ("READY" if invalidated else "DONE")


@pytest.mark.asyncio
async def test_edit_of_grant_reference_uses_related_rules_invalidation(test_db):
    _, _, member, audit = await setup(test_db)
    now = datetime.utcnow()
    await engine.bootstrap_member(test_db, member, now=now)
    assert await engine.active_authorization(test_db, member, now=now) is not None
    # Listener emits the watched-message rule dependency before the optional hint.
    await engine.project_fact(test_db, member, {"kind": "rules", "message_ids": [101],
        "joined_at": member.joined_at.isoformat(), "at": now.timestamp()}, now=now)
    await engine.project_fact(test_db, member, candidate(member, 101, now, now), now=now)
    assert await engine.active_authorization(test_db, member, now=now) is None
    work = await work_for(test_db, member)
    assert work.state == "READY"
