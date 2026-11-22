"""A confirmed new join owns only its unfinished first collection budget."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.models import AutoJoinAttempt
from tests.unit.test_qualification_engine import clone
from tests.unit.test_qualification_service import setup


@pytest.fixture(autouse=True)
def isolated_rollout(monkeypatch):
    assert Path(engine.__file__).resolve().is_relative_to((Path(__file__).resolve().parents[3] / "app").resolve())
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


async def joined(db, *, initial=True, source=True):
    account, group, member, audit = await setup(db)
    member.joined_at = datetime.utcnow()
    member.review_status, member.ad_status = "initial_pending", "warming"
    attempt = AutoJoinAttempt(account_id=account.id, group_id=group.id,
        status="success", telegram_action_attempted=True, request_state="sent",
        reconciliation_status="confirmed", joined_at=member.joined_at)
    db.add(attempt)
    await db.flush()
    work = await engine.enqueue(db, member, reason="joined" if source else "migration",
        initial_collection_pending=initial, join_attempt_id=attempt.id if source else None)
    await db.commit()
    claim = await engine.claim_next(db, account.id)
    assert claim is not None
    await db.commit()
    return account, group, member, audit, attempt, work, claim


async def test_new_confirmed_join_has_durable_current_budget_and_one_slot(test_db):
    a, g, _, _, _, work, claim = await joined(test_db)
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id)
    assert await engine.initial_slots(test_db, a.id) == 1
    assert json.loads(work.progress_json)["join_initial_budget"]["confirmed"] is True


@pytest.mark.parametrize("initial", [False, True])
async def test_historical_initial_labels_without_join_source_cannot_get_floor(test_db, initial):
    a, g, member, _, _, _, claim = await joined(test_db, initial=initial, source=False)
    assert member.review_status == "initial_pending"
    assert not await engine.initial_join_budget_current(test_db, claim, a.id, g.id)
    assert await engine.initial_slots(test_db, a.id) == 0


async def test_two_old_initial_flags_do_not_take_new_join_two_slots(test_db):
    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition.growth_admission import admission_plan
    a, _, _, _, _, _, _ = await joined(test_db, initial=True, source=False)
    group = Group(id=41, group_id=1234567891, title="old second")
    member = GroupAccountMembership(id=61, group_id=41, telegram_group_id=group.group_id,
        account_id=a.id, status="joined", joined_at=datetime.utcnow()-timedelta(days=2),
        review_status="initial_pending", ad_status="warming")
    test_db.add_all([group, member])
    await test_db.flush()
    await engine.enqueue(test_db, member, reason="migration", initial_collection_pending=True)
    await test_db.commit()
    assert await engine.initial_slots(test_db, a.id) == 0
    assert (await admission_plan(test_db, a.id, datetime.utcnow()))["remaining"] == 2


@pytest.mark.parametrize("case", ["token", "revision", "scope", "event", "expiry", "member_left",
    "account", "group", "source_attempt", "source_scope", "source_incarnation", "source_member",
    "unconfirmed", "unsent", "no_sdk", "attempt_account", "attempt_group", "source_missing", "flag_false"])
async def test_first_collection_floor_revalidates_every_current_fence(test_db, case):
    a, g, member, _, attempt, work, claim = await joined(test_db)
    source = json.loads(work.progress_json)["join_initial_budget"]
    account_id, group_id = a.id, g.id
    if case == "token": claim = replace(claim, token="stale")
    elif case == "revision": claim = replace(claim, revision=claim.revision+1)
    elif case == "scope": claim = replace(claim, scope_key="stale")
    elif case == "event": claim = replace(claim, event_version="stale")
    elif case == "expiry": work.claim_until = datetime.utcnow()-timedelta(seconds=1)
    elif case == "member_left": member.left_at = datetime.utcnow()
    elif case == "account": account_id += 1
    elif case == "group": group_id += 1
    elif case == "source_attempt": source["attempt_id"] = attempt.id+100
    elif case == "source_scope": source["scope_key"] = "other"
    elif case == "source_incarnation": source["joined_at"] = "other"
    elif case == "source_member": source["membership_id"] += 100
    elif case == "unconfirmed": attempt.reconciliation_status = "pending"
    elif case == "unsent": attempt.request_state = "outcome_unknown"
    elif case == "no_sdk": attempt.telegram_action_attempted = False
    elif case == "attempt_account": attempt.account_id += 100
    elif case == "attempt_group": attempt.group_id += 100
    elif case == "source_missing": source = {}
    elif case == "flag_false": work.initial_collection_pending = False
    work.progress_json = json.dumps({"join_initial_budget": source})
    await test_db.flush()
    assert not await engine.initial_join_budget_current(test_db, claim, account_id, group_id)


async def test_failed_first_slice_retains_source_and_retry_uses_new_live_claim(test_db):
    a, g, _, _, _, work, old = await joined(test_db)
    assert await engine.defer(test_db, old, snapshot={"read_plan": {"required_reads": 8}},
        reason="telegram_read_budget", state="READY", ready_at=datetime.utcnow())
    await test_db.commit()
    assert work.initial_collection_pending
    assert not await engine.initial_join_budget_current(test_db, old, a.id, g.id)
    current = await engine.claim_next(test_db, a.id)
    await test_db.commit()
    assert current is not None and current.token != old.token
    assert await engine.initial_join_budget_current(test_db, current, a.id, g.id)
    assert await engine.initial_slots(test_db, a.id) == 1


async def test_checkpoint_preserves_source_until_effective_collection_then_releases_slot(test_db):
    a, g, _, _, _, work, claim = await joined(test_db)
    draft = await engine._draft(test_db, claim)
    draft.evidence_json = json.dumps({"collection_complete": False, "collection_progress": {"page": 1}})
    assert await engine.checkpoint_draft(test_db, draft)
    await test_db.commit()
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id)
    assert "join_initial_budget" in json.loads(draft.evidence_json)
    draft.evidence_json = json.dumps({"collection_complete": True, "ai_pending": True})
    assert await engine.checkpoint_draft(test_db, draft)
    await test_db.commit()
    assert not work.initial_collection_pending
    assert not await engine.initial_join_budget_current(test_db, claim, a.id, g.id)
    assert await engine.initial_slots(test_db, a.id) == 0


@pytest.mark.parametrize("state", ["WAIT_PROVIDER", "WAIT_MATURITY", "WAIT_EVENT", "DONE", "STALE"])
async def test_future_obligation_never_retains_new_join_floor_or_slot(test_db, state):
    a, g, _, _, _, work, claim = await joined(test_db)
    assert await engine.defer(test_db, claim, snapshot={}, reason="evidence_required", state=state)
    await test_db.commit()
    assert not work.initial_collection_pending
    assert await engine.initial_slots(test_db, a.id) == 0
    assert not await engine.initial_join_budget_current(test_db, claim, a.id, g.id)


async def test_duplicate_confirmed_join_does_not_regrant_after_collection(test_db):
    a, g, member, _, attempt, work, claim = await joined(test_db)
    draft = await engine._draft(test_db, claim)
    draft.evidence_json = json.dumps({"collection_complete": True})
    assert await engine.checkpoint_draft(test_db, draft)
    before = (work.revision, work.state, work.claim_token)
    replay = await engine.enqueue(test_db, member, reason="joined", initial_collection_pending=True,
                                   join_attempt_id=attempt.id)
    await test_db.commit()
    assert replay.id == work.id and not work.initial_collection_pending
    assert (work.revision, work.state, work.claim_token) == before
    assert not await engine.initial_join_budget_current(test_db, claim, a.id, g.id)


@pytest.mark.parametrize("pending_state", ["WAIT_EVENT", "READY", "WAIT_BUDGET"])
async def test_pending_approval_confirmation_advances_only_original_first_work(test_db, pending_state):
    a, g, member, _ = await setup(test_db)
    member.review_status, member.ad_status = "initial_pending", "warming"
    attempt = AutoJoinAttempt(account_id=a.id, group_id=g.id, status="pending",
        request_state="sent", telegram_action_attempted=True, reconciliation_status="pending")
    test_db.add(attempt)
    await test_db.flush()
    work = await engine.enqueue(test_db, member, reason="approval", join_attempt_id=attempt.id)
    work.state, work.ready_at = pending_state, None
    if pending_state != "WAIT_EVENT":
        work.kind = "event"
    await test_db.commit()
    assert not work.initial_collection_pending and await engine.initial_slots(test_db, a.id) == 0
    attempt.status, attempt.reconciliation_status = "success", "confirmed"
    attempt.joined_at = member.joined_at
    confirmed = await engine.enqueue(test_db, member, reason="joined", initial_collection_pending=True,
                                     join_attempt_id=attempt.id)
    await test_db.commit()
    assert confirmed.id == work.id and work.state == "READY" and work.initial_collection_pending
    claim = await engine.claim_next(test_db, a.id)
    await test_db.commit()
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id)


async def test_real_final_decision_releases_initial_slot(test_db):
    a, _, member, audit, _, work, claim = await joined(test_db)
    decision = clone(audit, member, decision="protected")
    assert await engine.on_decision(test_db, member, decision, claim=claim)
    await test_db.commit()
    assert not work.initial_collection_pending
    assert await engine.initial_slots(test_db, a.id) == 0
