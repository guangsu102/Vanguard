"""Actual candidate claim/draft/defer paths; no Telegram or live services."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import case, func, select
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.core.account.control_models import AccountBudgetReservation
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.models import AutoJoinAttempt, GroupQualificationAudit, QualificationWork
from tests.unit.test_qualification_service import setup
from .test_read_budget_class import lua_store
from .test_initial_join_one_slice import local_services, reservation


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())
    assert Path(engine.__file__).resolve().is_relative_to((Path(__file__).resolve().parents[3] / "app").resolve())


@pytest.fixture(autouse=True)
def local_evidence_output(tmp_path, monkeypatch):
    monkeypatch.setenv("R19_OUTPUT", str(tmp_path))


async def backlog(db, *, old_count=31, now=None):
    account, group, member, audit = await setup(db)
    now = now or datetime.utcnow()
    olds = []
    for index in range(old_count):
        other = Group(id=200+index, group_id=1000000000+index, title=f"older {index}")
        old_member = GroupAccountMembership(id=300+index, group_id=other.id,
            telegram_group_id=other.group_id, account_id=account.id,
            status="joined", joined_at=now-timedelta(days=5),
            review_status="initial_pending", ad_status="warming")
        db.add_all([other, old_member])
        await db.flush()
        olds.append(await engine.enqueue(db, old_member, reason="migration",
            initial_collection_pending=True, now=now-timedelta(days=1)))
    member.joined_at = now
    member.review_status, member.ad_status = "initial_pending", "warming"
    attempt = AutoJoinAttempt(account_id=account.id, group_id=group.id, status="success",
        telegram_action_attempted=True, request_state="sent", reconciliation_status="confirmed", joined_at=now)
    db.add(attempt)
    await db.flush()
    work = await engine.enqueue(db, member, reason="joined", now=now,
        initial_collection_pending=True, join_attempt_id=attempt.id)
    await db.commit()
    return account, group, member, audit, attempt, work, olds, now


async def complete(db, claim):
    work = await db.get(QualificationWork, claim.work_id)
    work.state, work.claim_token, work.claim_until = "DONE", None, None
    await db.commit()


async def test_confirmed_new_join_precedes_31_old_labels_and_preserves_old_fifo(test_db):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db)
    original = (work.ready_at, work.progress_json, audit.evidence_json, audit.checked_at)
    assert await engine.initial_slots(test_db, a.id) == 1
    claim = await engine.claim_next(test_db, a.id, now=now)
    assert claim.work_id == work.id
    assert await engine.initial_join_budget_current(test_db, claim, a.id, g.id)
    await complete(test_db, claim)
    claimed = []
    for old in olds:
        claim = await engine.claim_next(test_db, a.id, now=now)
        claimed.append(claim.work_id)
        assert claim.work_id == old.id
        await complete(test_db, claim)
    assert claimed == [old.id for old in olds]
    assert (work.ready_at, work.progress_json, audit.evidence_json, audit.checked_at) == original
    assert await test_db.scalar(select(func.count(AccountBudgetReservation.reservation_id))) == 0
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 1
    out = Path(os.environ["R19_OUTPUT"]) / "actual-claim-trace.json"
    with out.open("x", encoding="utf-8") as handle:
        json.dump({"actual_claim_next":True,"old_ready_count":31,"new_join_claim_position":1,
            "old_fifo_preserved":True,"old_claim_ids":claimed,"new_work_id":work.id,
            "ready_times_unchanged":True,"authorization_facts_unchanged":True,
            "simulated_terminal_completion":True,"production_latency_measured":False}, handle, indent=2)


async def test_claim_draft_budget_wait_then_due_retains_priority_over_31_old_ready(test_db):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db)
    source = json.loads(work.progress_json)["join_initial_budget"]
    first = await engine.claim_next(test_db, a.id, now=now)
    assert first.work_id == work.id
    draft = await engine._draft(test_db, first)
    assert await engine.defer(test_db, first, snapshot={"read_plan":{"required_reads":8},
        "retry_after_seconds":60}, reason="telegram_read_budget", draft=draft, now=now)
    await test_db.commit()
    assert work.state == "WAIT_BUDGET" and work.ready_at == now+timedelta(seconds=60)
    assert work.checkpoint_stage is None
    assert json.loads(work.progress_json)["join_initial_budget"] == source
    ordinary = await engine.claim_next(test_db, a.id, now=now+timedelta(seconds=1))
    assert ordinary.work_id == olds[0].id
    await complete(test_db, ordinary)
    retry = await engine.claim_next(test_db, a.id, now=now+timedelta(seconds=60))
    assert retry.work_id == work.id and retry.token != first.token
    assert await engine.initial_join_budget_current(test_db, retry, a.id, g.id)
    assert "first_read_token" not in json.loads(work.progress_json)["join_initial_budget"]
    assert not await engine.initial_join_budget_current(test_db, first, a.id, g.id)
    out = Path(os.environ["R19_OUTPUT"]) / "actual-budget-wait-trace.json"
    with out.open("x", encoding="utf-8") as handle:
        json.dump({"actual_claim_draft_defer":True,"budget_denial_simulated":True,
            "old_ready_count":31,"first_work_id":first.work_id,"during_wait_id":ordinary.work_id,
            "budget_due_work_id":retry.work_id,"new_priority_retained_after_wait":True,
            "no_token_created":True,"no_claim_time_consumption_marker":True}, handle, indent=2)


async def test_real_service_assess_budget_denial_creates_no_checkpoint_and_keeps_due_priority(test_db, lua_store, local_services, monkeypatch):
    from types import SimpleNamespace
    from app.core.account.rpc_governor import RpcDeferred
    from app.modules.acquisition import qualification_service as qualification
    from app.modules.acquisition.automation import AcquisitionAutomationService
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db)
    pool = SimpleNamespace(add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(side_effect=RpcDeferred("telegram_read_budget",60)), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr("app.core.account.session_exec.current_actor_account",lambda:a.id)
    result = await qualification._run_reviews(service, account_id=a.id, limit=1)
    assert result["processed"] == 1 and result["results"][0]["work_id"] == work.id
    assert work.state == "WAIT_BUDGET" and work.reason == "telegram_read_budget"
    assert pool.acquire_by_id.await_count == 1
    assert work.checkpoint_stage is None and work.checkpoint_json is None
    assert "first_read_token" not in json.loads(work.progress_json)["join_initial_budget"]
    due = await engine.claim_next(test_db,a.id,now=work.ready_at)
    assert due.work_id == work.id
    assert await engine.initial_join_budget_current(test_db,due,a.id,g.id)


@pytest.mark.parametrize("kind", ["event", "migration", "approval"])
async def test_real_unstarted_source_priority_is_independent_of_old_kind(test_db, kind):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db, old_count=1)
    work.kind, work.attempts = kind, 12
    await test_db.commit()
    assert (await engine.claim_next(test_db, a.id, now=now)).work_id == work.id


@pytest.mark.parametrize("case", ["missing_source","attempt_missing","source_member","source_scope",
    "source_incarnation","attempt_account","attempt_group","pending","unknown","unsent","not_attempted",
    "not_success","confirmed_string","confirmed_number","attempt_string","attempt_bool","attempt_fraction",
    "attempt_huge","member_string","member_bool","source_array","invalid_json","root_array",
    "complete","pending_complete","first_token","token_number","token_array","checkpoint",
    "zero_string","zero_number","zero_without_valid_token"])
async def test_invalid_historic_or_started_work_uses_plain_fifo(test_db, case):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db, old_count=1)
    data = json.loads(work.progress_json)
    source = data["join_initial_budget"]
    if case == "missing_source": data.pop("join_initial_budget")
    elif case == "attempt_missing": source["attempt_id"] += 100
    elif case == "source_member": source["membership_id"] += 100
    elif case == "source_scope": source["scope_key"] = "wrong"
    elif case == "source_incarnation": source["joined_at"] = (now-timedelta(days=1)).isoformat()
    elif case == "attempt_account": attempt.account_id += 100
    elif case == "attempt_group": attempt.group_id += 100
    elif case == "pending": attempt.reconciliation_status = "pending"
    elif case == "unknown": attempt.request_state = "outcome_unknown"
    elif case == "unsent": attempt.request_state = "not_sent"
    elif case == "not_attempted": attempt.telegram_action_attempted = False
    elif case == "not_success": attempt.status = "failed"
    elif case == "confirmed_string": source["confirmed"] = "true"
    elif case == "confirmed_number": source["confirmed"] = 1
    elif case == "attempt_string": source["attempt_id"] = str(attempt.id)
    elif case == "attempt_bool": source["attempt_id"] = True
    elif case == "attempt_fraction": source["attempt_id"] = 1.5
    elif case == "attempt_huge": source["attempt_id"] = 10**100
    elif case == "member_string": source["membership_id"] = str(m.id)
    elif case == "member_bool": source["membership_id"] = True
    elif case == "source_array": data["join_initial_budget"] = []
    elif case == "root_array": data = []
    elif case == "complete": data["collection_complete"] = True
    elif case == "pending_complete": data["pending_collection"] = {"collection_complete":True}
    elif case == "first_token": source["first_read_token"] = "already-started"
    elif case == "token_number": source["first_read_token"] = 1
    elif case == "token_array": source["first_read_token"] = []
    elif case == "checkpoint": work.checkpoint_stage = "collection"
    elif case == "zero_string": source.update(first_read_token="first",zero_sdk_released="true")
    elif case == "zero_number": source.update(first_read_token="first",zero_sdk_released=1)
    elif case == "zero_without_valid_token": source.update(first_read_token=123,zero_sdk_released=True)
    work.progress_json = "{broken" if case == "invalid_json" else json.dumps(data)
    await test_db.commit()
    claim = await engine.claim_next(test_db, a.id, now=now)
    assert claim.work_id == olds[0].id


@pytest.mark.parametrize("case", ["paused","held","left","future","busy"])
async def test_existing_claim_fences_cannot_be_bypassed_by_priority(test_db, case):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db, old_count=1)
    if case == "paused": m.ad_status = "paused"
    elif case == "held": m.review_status = "manual_required"
    elif case == "left": m.left_at = now
    elif case == "future": work.state, work.ready_at = "WAIT_BUDGET", now+timedelta(seconds=60)
    elif case == "busy": olds[0].state, olds[0].claim_token, olds[0].claim_until = "RUNNING", "owner", now+timedelta(seconds=60)
    await test_db.commit()
    claim = await engine.claim_next(test_db, a.id, now=now)
    assert claim is None if case == "busy" else claim.work_id == olds[0].id


@pytest.mark.parametrize("case", ["clean_zero","clean_zero_event","consumed","cancelled","missing_journal","sdk_unknown","bind_unknown"])
async def test_only_certified_zero_sdk_first_token_keeps_first_step_priority(test_db, lua_store, local_services, case):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db)
    first = await engine.claim_next(test_db, a.id, now=now)
    read = await reservation(test_db, lua_store, first, a, g)
    assert await engine.bind_initial_join_read_work(test_db, first, a.id, g.id, read.token, 8)
    from app.core.account import budget_ledger
    if case == "consumed":
        from app.core.account import read_work
        from app.core.account.reserved_reads import RESERVED_BUDGET_LUA,reservation_args
        from .test_read_budget_class import limits
        read.claimed=True
        token=read_work._current.set(read)
        try:
            keys,args=reservation_args(a.id,limits(),"critical",1,0,workload="join")
            assert await lua_store.eval(RESERVED_BUDGET_LUA,len(keys),*keys,*args)==0
        finally:
            read_work._current.reset(token)
        read.reads=read.entered_reads=1
    elif case == "missing_journal":
        await lua_store.delete(budget_ledger.CONSUMED_PREFIX+read.ledger_id)
    elif case == "sdk_unknown":read.entered_reads=1
    elif case == "bind_unknown":read.budget_bind_failed=True
    await budget_ledger.finish_read_work(read)
    read.closed = True
    await engine.finish_initial_join_read_work(read, failed=case=="cancelled")
    certified=case in {"clean_zero","clean_zero_event"}
    assert json.loads(work.progress_json)["join_initial_budget"]["zero_sdk_released"] is certified
    assert await engine.defer(test_db, first, snapshot={"read_plan":{"required_reads":8}},
        reason="telegram_read_budget", state="READY", ready_at=now, now=now)
    await test_db.commit()
    if case == "clean_zero_event":
        from app.modules.acquisition.qualification_events import request
        await request(test_db,m,"metadata",source_token="post-zero-event")
        await test_db.commit()
    retry=await engine.claim_next(test_db,a.id,now=datetime.utcnow()+timedelta(seconds=1))
    assert retry.work_id == (work.id if certified else olds[0].id)
    if certified:
        assert work.initial_collection_pending
        assert await engine.initial_join_budget_current(test_db,retry,a.id,g.id,work_token="next")


async def test_completed_local_checkpoint_without_sdk_releases_queue_priority(test_db):
    a, g, m, audit, attempt, work, olds, now = await backlog(test_db)
    first = await engine.claim_next(test_db, a.id, now=now)
    draft = await engine._draft(test_db, first)
    draft.evidence_json = json.dumps({"collection_complete":False,"collection_progress":{"page":1}})
    draft.checkpoint_stage, draft.checkpoint_revision, draft.checkpoint_json = "collection", "local-result", '{"page":1}'
    assert await engine.checkpoint_draft(test_db, draft)
    assert await engine.defer(test_db, first, snapshot=json.loads(draft.evidence_json),
        reason="telegram_read_slice", state="READY", ready_at=now, draft=draft, now=now)
    await test_db.commit()
    assert "first_read_token" not in json.loads(work.progress_json)["join_initial_budget"]
    assert (await engine.claim_next(test_db, a.id, now=now)).work_id == olds[0].id


@pytest.mark.parametrize("now", [datetime(2026,11,6,0,0,0), datetime(2026,11,6,0,0,0,123000)])
async def test_current_incarnation_comparison_handles_iso_microseconds(test_db, now):
    a, g, m, audit, attempt, work, olds, _ = await backlog(test_db, old_count=1, now=now)
    assert (await engine.claim_next(test_db, a.id, now=now)).work_id == work.id


def test_pg_priority_compiles_safe_json_and_correlated_exists_without_lock_expansion():
    query = select(QualificationWork.id).join(GroupAccountMembership,
        GroupAccountMembership.id == QualificationWork.membership_id).order_by(
            case((engine._first_join_priority("postgresql"),0),else_=1),
            QualificationWork.ready_at, QualificationWork.id)
    compiled = str(query.compile(dialect=postgresql.dialect(),compile_kwargs={"literal_binds":True}))
    assert "pg_input_is_valid" in compiled and "CAST(CASE WHEN" in compiled
    assert "jsonb_typeof" in compiled and f"EXISTS (SELECT {AutoJoinAttempt.__tablename__}.id" in compiled
    assert "FOR UPDATE" not in compiled
    assert "~ '^[1-9][0-9]{0,17}$'" in compiled
    assert "AS BIGINT" in compiled
    path = Path(os.environ["R19_OUTPUT"]) / "priority-pg18.sql"
    path.write_text(compiled+";\n", encoding="utf-8")


def test_unknown_dialect_is_conservative():
    assert str(engine._first_join_priority("unknown")) == "false"
