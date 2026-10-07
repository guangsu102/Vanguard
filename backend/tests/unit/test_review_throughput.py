"""Production backlog regressions with isolated SQL and synthetic Telegram reads."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChatAdminRequiredError

from app.core.account import rpc_governor as rpc
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.daily_frequency import DailyFrequencyService
from app.modules.acquisition.group_qualification import POLICY_VERSION, EvidenceCollector, trial_evidence
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_events import acknowledge, pending, request
from app.modules.acquisition.qualification_queue import cursor
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_adaptive_group_frequency import NOW as CYCLE_NOW
from tests.unit.test_evidence_progress import ResumeClient
from tests.unit.test_event_cycle_survival import scenario
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, message
from tests.unit.test_qualification_service import setup
from tests.unit.test_review_efficiency import ai_snapshot


@pytest.mark.asyncio
async def test_permission_denial_stops_additional_pages_without_proving_absence():
    history = [message(4000-i, age=i/100, sender=100+i,
                       text=f"服务优惠联系 https://offer-{i}.example") for i in range(1500)]
    client = ResumeClient(history)
    client.role_exception = ChatAdminRequiredError(None)
    result = await EvidenceCollector(client, identity_limit=12, priority_identities=8).collect(ENTITY, now=NOW)
    history_calls = [kw for kind, kw in client.calls if kind == "messages" and "offset_date" in kw]
    assert len(history_calls) == 3
    assert result["sample_count"] == 100
    assert result["sampling_stopped_reason"] == "member_identity_unavailable"
    assert result["permission_queries"] == 3
    assert not result["history_complete"] and not trial_evidence(result["evidence"])
    assert result["coverage"][0]["unknown_reason"] == "member_identity_unavailable"
    assert result["collection_progress"]["permission_failures"]


@pytest.mark.asyncio
async def test_identity_slice_keeps_rules_unknown_and_resumes_exact_unfinished_messages():
    history = [message(2000-i, sender=100+i, age=1+i/100,
                       text=f"本群禁止刷屏，规则条款 {i}") for i in range(350)]
    first = await EvidenceCollector(ResumeClient(history), identity_limit=12,
                                    priority_identities=8).collect(ENTITY, now=NOW)
    assert first["permission_queries"] == 20
    assert first["sample_count"] == 100
    assert first["sampling_stopped_reason"] == "identity_read_slice_exhausted"
    assert first["rules_incomplete"] and not first["history_complete"]
    ids = first["collection_progress"]["pending_message_ids"]
    assert ids
    client = ResumeClient(history)
    collector = EvidenceCollector(client, identity_limit=12, priority_identities=8)
    collector.previous = first
    second = await collector.collect(ENTITY, now=NOW+timedelta(minutes=15))
    assert client.target_reads[0] == ids[:12]
    assert second["roles"][str(client.by_id[ids[0]].sender_id)] == "ordinary"
    assert second["permission_queries"] <= 20
    assert ids[0] not in second["collection_progress"]["pending_message_ids"]
    assert not trial_evidence(second["evidence"])


@pytest.mark.asyncio
async def test_slice_preserves_priority_identity_checks_after_ordinary_budget():
    client = ResumeClient([], admins={999})
    collector = EvidenceCollector(client, identity_limit=12, priority_identities=8)
    for number in range(12):
        assert await collector.role(ENTITY, message(number, sender=number+1)) == "ordinary"
    assert await collector.role(ENTITY, message(100, sender=100)) == "unknown"
    assert await collector.role(ENTITY, message(101, sender=999, text="本群禁止广告")) == "admin"
    assert collector.permission_queries == 13


@pytest.mark.asyncio
async def test_new_initial_review_gets_turn_ahead_of_repeated_old_observations(test_db, monkeypatch):
    _, _, member, renewal = await setup(test_db)
    now = datetime.utcnow()
    rows = []
    for offset in range(13):
        group = Group(id=41+offset, group_id=1234567891+offset, title="waiting")
        joined = GroupAccountMembership(id=61+offset, group_id=group.id,
            telegram_group_id=group.group_id, account_id=2, status="joined",
            joined_at=member.joined_at, review_status="initial_pending" if offset==12 else "review_2h",
            ad_status="blocked")
        row = GroupQualificationAudit(batch_id=f"throughput-{offset}", membership_id=joined.id,
            account_id=2, group_id=group.id, policy_version=POLICY_VERSION, content_scope="text_profile",
            membership_joined_at=joined.joined_at, state="queued", next_retry_at=now,
            decision="unknown" if offset==12 else "observe",
            checked_at=None if offset==12 else now-timedelta(hours=3))
        test_db.add_all([group,joined,row])
        rows.append(row)
    renewal.state, renewal.next_retry_at = "queued", now
    await test_db.commit()
    monkeypatch.setattr(service,"ensure_membership_reviews",AsyncMock())
    read_wait = AsyncMock(return_value=("telegram_read_budget", now+timedelta(hours=1)))
    monkeypatch.setattr("app.core.account.read_schedule.read_wait",read_wait)
    seen = []

    async def assess(actor, account_id, group, *, row):
        seen.append(row.id)
        row.state, row.next_retry_at, row.checked_at = "completed", None, now
        return Obj(passed=False,reason="reviewed")

    monkeypatch.setattr(service,"assess",assess)
    before = await cursor(test_db,2)
    await service.run_reviews(Obj(db=test_db),account_id=2,limit=1)
    assert not seen and await cursor(test_db,2)==before
    monkeypatch.setattr("app.core.account.read_schedule.read_wait",AsyncMock(return_value=None))
    for _ in range(3):
        for row in [renewal,*rows]:
            if row.id not in seen:
                row.next_retry_at=now
        await test_db.commit()
        await service.run_reviews(Obj(db=test_db),account_id=2,limit=1)
    assert seen == [rows[-1].id, rows[0].id, renewal.id]


async def parked_scenario(db):
    account, config, frequency, state, member, log = await scenario(db)
    log.status, log.error = "pending", "send_outcome_unknown:ValueError"
    log.telegram_message_id = None
    log.created_at = CYCLE_NOW-timedelta(hours=10)
    log.survival_status, log.survival_stage = "not_required", "complete"
    log.survival_check_due_at = log.survival_claim_expires_at = log.survival_claim_token = None
    state.daily_review_error = "qualification_delivery_reconciliation_required"
    state.daily_review_due_at = CYCLE_NOW-timedelta(hours=2)
    await db.commit()
    return account,state,log


@pytest.mark.asyncio
async def test_parked_unknown_lends_only_surplus_and_keeps_receipt_and_group_frozen(test_db, monkeypatch):
    account,state,log = await parked_scenario(test_db)
    mock_usage(monkeypatch,{"hour":(120,1800),"day":(1200,60000)})
    result = await rpc.check_read_ready(test_db,account.id,now=CYCLE_NOW,purpose="group_qualification")
    # The missing receipt is reconciled from local evidence only. It must not
    # reserve a second Telegram read slice for the whole account.
    assert result["survival_lending"]["holds"] == {"hour":20,"day":94}
    assert result["lanes"]["critical"]["remaining"]==16
    assert result["lanes"]["ad"]["remaining"]==84
    assert result["usage"]["hour"]["used"]==120
    assert result["usage"]["hour"]["ttl_seconds"]==1800
    assert (await DailyFrequencyService(test_db).plan(state)).reason=="qualification_delivery_reconciliation_required"
    assert log.status=="pending" and log.telegram_message_id is None
    assert state.daily_review_error=="qualification_delivery_reconciliation_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["recent","message_id","legacy","claimed","sending","unexplained","other_cycle_error"])
async def test_unknown_lending_exception_cannot_hide_actionable_or_unexplained_work(test_db,monkeypatch,change):
    account,state,log=await parked_scenario(test_db)
    if change=="recent": log.created_at=CYCLE_NOW-timedelta(minutes=29)
    elif change=="message_id": log.telegram_message_id=42
    elif change=="legacy": log.survival_status="pending"
    elif change=="claimed": log.survival_claim_token="active"
    elif change=="sending": log.status="sending"
    elif change=="unexplained": log.error=None
    else: state.daily_review_error="message_missing_cause_unconfirmed"
    await test_db.commit()
    mock_usage(monkeypatch,{"hour":(120,1800),"day":(1200,60000)})
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(test_db,account.id,now=CYCLE_NOW,purpose="group_qualification")


@pytest.mark.asyncio
async def test_parked_unknown_without_stored_cycle_deadline_is_still_counted(test_db,monkeypatch):
    account,state,_=await parked_scenario(test_db)
    state.daily_review_error=state.daily_review_due_at=None
    await test_db.commit()
    mock_usage(monkeypatch,{"hour":(120,1800),"day":(1200,60000)})
    result=await rpc.snapshot(test_db,account.id,CYCLE_NOW)
    assert result["survival_lending"]["holds"]=={"hour":10,"day":84}


def test_only_queue_flag_can_change_during_ai_evidence_reuse():
    now,account=datetime.utcnow(),Obj(id=2,profile_bio="test")
    event={"kinds":["rules"],"token":"old","account_generation":"one",
           "message_ids":[9],"joined_at":"scope","queued":False}
    snapshot={**ai_snapshot(now,account),"collection_event":event}
    assert service.ai_evidence_reusable(snapshot,account,now,{**event,"queued":True})
    for field,value in [("token","new"),("account_generation","two"),("message_ids",[10]),
                        ("kinds",["gap","rules"]),("joined_at","new scope")]:
        assert not service.ai_evidence_reusable(snapshot,account,now,{**event,field:value})


@pytest.mark.asyncio
async def test_reuse_comparison_does_not_weaken_event_acknowledgement(test_db):
    _,_,member,_=await setup(test_db)
    await request(test_db,member,"rules",message_ids=[9])
    await test_db.flush()
    event=await pending(test_db,member)
    assert not await acknowledge(test_db,member,{**event,"queued":True})
    await request(test_db,member,"rules",message_ids=[10])
    await test_db.flush()
    assert not await acknowledge(test_db,member,event)
    assert (await pending(test_db,member))["message_ids"]==[9,10]
