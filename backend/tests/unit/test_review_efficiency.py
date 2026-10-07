"""Exercise real SDK RPC costs, evidence invalidation and join pressure."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, types
from telethon.errors import FloodWaitError, UserNotParticipantError
from telethon.sessions import MemorySession
from telethon.tl.functions.channels import GetChannelsRequest, GetParticipantRequest

from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.qualification_ai import profile_fingerprint
from app.modules.acquisition.qualification_reads import current_permissions
from app.modules.acquisition.review_inventory import waiting_for_evidence
from tests.unit.test_qualification_service import setup


@pytest.mark.asyncio
async def test_real_sdk_sixty_identities_cost_sixty_not_one_hundred_twenty_rpcs():
    now = datetime.now(UTC)
    entity = types.Channel(id=42, title="test", photo=types.ChatPhotoEmpty(), date=now,
                           megagroup=True, access_hash=123)
    user = types.User(id=7, access_hash=234, first_name="test")
    client = TelegramClient(MemorySession(), 123, "test")
    calls = []

    async def dispatch(sender, request, **kwargs):
        calls.append(type(request).__name__)
        if isinstance(request, GetChannelsRequest):
            return Obj(chats=[entity])
        assert isinstance(request, GetParticipantRequest)
        return Obj(participant=types.ChannelParticipant(user_id=7, date=now))

    client._call = dispatch
    for _ in range(60):
        assert not (await client.get_permissions(entity, user)).is_admin
    assert len(calls) == 120 and calls.count("GetChannelsRequest") == 60
    calls.clear()
    for _ in range(60):
        assert not (await current_permissions(client, entity, user)).is_admin
    assert calls == ["GetParticipantRequest"] * 60


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [FloodWaitError(request=None, capture=300), UserNotParticipantError(None)])
async def test_direct_permission_read_preserves_flood_and_membership_failures(error):
    entity = types.Channel(id=42, title="test", photo=types.ChatPhotoEmpty(),
                           date=datetime.now(UTC), megagroup=True, access_hash=123)
    client = AsyncMock(side_effect=error)
    client.get_input_entity.return_value = types.InputPeerSelf()
    with pytest.raises(type(error)):
        await current_permissions(client, entity, "me")


@pytest.mark.asyncio
async def test_basic_chat_reuses_current_participants_and_never_invents_membership():
    entity = types.Chat(id=42, title="test", photo=types.ChatPhotoEmpty(),
                        participants_count=1, date=datetime.now(UTC), version=1)
    full = Obj(participants=Obj(participants=[types.ChatParticipantCreator(user_id=7)]))
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerUser(7, 99)
    assert (await current_permissions(client, entity, 7, full_chat=full)).is_creator
    client.assert_not_awaited()
    client.get_input_entity.return_value = types.InputPeerUser(8, 99)
    with pytest.raises(UserNotParticipantError):
        await current_permissions(client, entity, 8, full_chat=full)
    with pytest.raises(ValueError, match="participant_list_unavailable"):
        await current_permissions(client, entity, 8, full_chat=Obj())


def ai_snapshot(now, account):
    return {"ai_pending": False, "ai_review_incomplete": True,
            "quality_status": "qualified", "quality_reason": "quality_and_permissions_passed",
            "collected_at": now.isoformat(), "account_id": account.id,
            "policy_version": POLICY_VERSION, "profile_fingerprint": profile_fingerprint(account),
            "decision": "observe", "reason": "group_rules_ai_consensus_failed", "evidence": []}


@pytest.mark.parametrize("change", [
    {"invalidated_at": "changed"}, {"collection_needs_refresh": "ordinary_ad_hint"},
    {"collected_at": "2000-01-01T00:00:00"}, {"technical_errors": ["FloodWaitError"]},
    {"policy_version": "old"}, {"profile_fingerprint": "old"}, {"account_id": 99},
    {"collection_event": {"token": "old"}}, {"quality_status": "observe"},
])
def test_ai_retry_reuses_evidence_only_while_scope_is_valid(change):
    now, account = datetime.utcnow(), Obj(id=2, profile_bio="test")
    data = ai_snapshot(now, account)
    assert service.ai_evidence_reusable(data, account, now)
    assert not service.ai_evidence_reusable({**data, **change}, account, now)
    event = {"kinds": ["rules"], "token": "new", "account_generation": "one"}
    assert not service.ai_evidence_reusable(data, account, now, event)
    assert service.ai_evidence_reusable({**data, "collection_event": event}, account, now, event)
    assert not service.ai_evidence_reusable({**data, "collection_event": event}, account, now,
                                           {**event, "account_generation": "two"})


def test_unrelated_promotions_do_not_reset_provider_rejection_backoff():
    now = datetime.utcnow()
    data = {"decision": "observe", "reason": "group_rules_ai_provider_content_rejected",
            "ai_review_incomplete": True,
            "evidence": [{"source": "full_about", "text": "fixed rules"},
                         {"source": "recent_promotional_message", "message_id": 0,
                          "text": "unrelated offer"}]}
    result = service.review_schedule(data, {}, now)
    assert result[3] is None and data["unchanged_rejection_count"] == 1
    duplicate = {**data, "decision": "observe", "ai_final": False,
                 "ai_review_incomplete": True}
    assert service.review_schedule(duplicate, data, now)[3] is None
    changed = {**data, "evidence": [{"source": "full_about", "text": "changed rules"}],
               "ai_final": False, "ai_review_incomplete": True}
    assert service.review_schedule(changed, data, now)[3] == now + timedelta(hours=2)


@pytest.mark.asyncio
async def test_ai_only_retry_does_not_acquire_telegram_or_renew_collection_date(test_db, monkeypatch):
    from app.modules.acquisition.automation import AcquisitionAutomationService, GroupAdRulesAuditResult
    account, group, member, row = await setup(test_db, decision="observe")
    collected = datetime.utcnow() - timedelta(hours=1)
    snapshot = {**json.loads(row.evidence_json), **ai_snapshot(collected, account)}
    row.evidence_json = json.dumps(snapshot)
    await test_db.commit()
    pool = Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(), release=AsyncMock())
    agent = AcquisitionAutomationService(test_db, account_pool=pool)
    review = AsyncMock(return_value=GroupAdRulesAuditResult(reason="group_rules_ai_consensus_failed"))
    monkeypatch.setattr("app.modules.acquisition.qualification_ai.review_semantics", review)
    await service.assess(agent, account.id, group, row=row)
    pool.acquire_by_id.assert_not_awaited()
    review.assert_awaited_once()
    result = json.loads(row.evidence_json)
    assert result["collection_reused_for_ai"]
    assert result["collected_at"] == collected.isoformat()
    assert row.decision == "reject"


@pytest.mark.asyncio
async def test_exhausted_evidence_is_visible_but_not_join_pressure(test_db):
    from app.core.account.models import AccountOperationConfig
    from app.modules.acquisition.capacity import inventory_snapshot
    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    account, _, member, row = await setup(test_db, decision="observe")
    test_db.add(AccountOperationConfig(account_id=account.id, dynamic_capacity_enabled=True))
    now = datetime.utcnow()
    data = {"decision": "observe", "reason": "rules_coverage_incomplete",
            "observation_started_at": (now - timedelta(days=2)).isoformat(), "evidence": []}
    row.reason, row.evidence_json = data["reason"], json.dumps(data)
    row.next_retry_at = now - timedelta(hours=1)
    member.review_status = "review_2h"
    await test_db.commit()
    result = await inventory_snapshot(test_db, account.id, now)
    assert result["unresolved_reviews"] == result["evidence_waiting"] == 1
    assert result["probe_active_backlog"] == 1
    assert result["probe_review_pressure"] == result["probe_review_credit"] == 0
    assert await JoinRequestBudgetService(test_db)._review_backlog(account.id, now) == (0, 0)
    row.state = "queued"  # Explicit new work is always capacity pressure.
    await test_db.flush()
    assert (await inventory_snapshot(test_db, account.id, now))["probe_review_pressure"] == 1
    row.state, row.decision, row.reason = "completed", "technical_wait", "telegram_read_budget"
    await test_db.flush()
    assert (await inventory_snapshot(test_db, account.id, now))["probe_review_pressure"] == 1


@pytest.mark.asyncio
async def test_fresh_hint_waits_for_maturity_then_requires_live_evidence(test_db):
    from app.core.account.listener_facts import apply_fact
    account, _, member, row = await setup(test_db, decision="observe")
    now = datetime.utcnow()
    row.checked_at, row.next_retry_at = now - timedelta(hours=3), None
    row.evidence_json = json.dumps({**ai_snapshot(now, account), "review_trigger": "evidence_changed"})
    fact = {"kind": "evidence_candidate", "peer": member.telegram_group_id,
            "at": now.replace(tzinfo=UTC).timestamp(), "joined_at": member.joined_at.isoformat(),
            "message_id": 123, "message_date": now.isoformat()}
    await apply_fact(test_db, account.id, fact)
    assert row.state == "queued" and row.next_retry_at == now + timedelta(hours=24)
    assert waiting_for_evidence(row, now)
    assert not waiting_for_evidence(row, row.next_retry_at)
    assert not service.ai_evidence_reusable(json.loads(row.evidence_json), account, now)


@pytest.mark.asyncio
async def test_twelve_future_observations_keep_inventory_credit_without_blocking_five_real_jobs(test_db):
    from app.core.account.models import AccountOperationConfig
    from app.modules.acquisition.capacity import inventory_snapshot
    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    from tests.unit.test_dynamic_outbound_capacity import add_member
    account, _, _, row = await setup(test_db, decision="observe")
    now = datetime.utcnow()
    config = AccountOperationConfig(account_id=account.id, dynamic_capacity_enabled=True,
                                    join_review_backlog_paused=True)
    test_db.add(config)
    row.reason = "group_rules_ai_consensus_failed"
    row.next_retry_at = now + timedelta(hours=1)
    future = [row]
    for index in range(1, 17):
        _, audit = await add_member(test_db, account.id, index,
                                    next_retry=now + timedelta(hours=1))
        audit.checked_at = now - timedelta(hours=1)
        if index >= 12:
            audit.decision, audit.reason = "technical_wait", "telegram_read_budget"
        else:
            audit.reason = "group_rules_ai_consensus_failed"
            future.append(audit)
    await test_db.commit()
    inventory = await inventory_snapshot(test_db, account.id, now)
    assert inventory["probe_active_backlog"] == inventory["unresolved_reviews"] == 17
    assert inventory["probe_scheduled_reviews"] == 12
    assert inventory["probe_review_pressure"] == 5
    assert inventory["probe_review_credit"] == 17
    assert await JoinRequestBudgetService(test_db).refresh_review_backlog_pause(
        account.id, now=now) == (5, 0, False)
    for audit in future:
        audit.next_retry_at = now
    await test_db.flush()
    assert (await inventory_snapshot(test_db, account.id, now))["probe_review_pressure"] == 17
    assert await JoinRequestBudgetService(test_db).refresh_review_backlog_pause(
        account.id, now=now) == (17, 0, True)


@pytest.mark.asyncio
async def test_exact_fact_replay_coalesces_but_new_edit_invalidates_acknowledgement(test_db):
    from app.modules.acquisition.qualification_events import request, pending, acknowledge
    _, _, member, _ = await setup(test_db)
    await request(test_db, member, "rules", message_ids=[123], source_token="one")
    before = await pending(test_db, member)
    await request(test_db, member, "rules", message_ids=[123], source_token="one")
    assert await pending(test_db, member) == before
    await request(test_db, member, "rules", message_ids=[123], source_token="edit-two")
    assert await pending(test_db, member) != before
    assert not await acknowledge(test_db, member, before)


@pytest.mark.asyncio
async def test_local_pump_amortizes_thirty_account_scan_across_ninety_six_facts():
    from app.workers.telegram_worker import TelegramWorker
    worker = object.__new__(TelegramWorker)
    worker._running = True
    worker._project_growth_listener_facts = AsyncMock()
    consumed = []

    async def consume(**kwargs):
        consumed.append(len(consumed) % 30 + 1)
        if len(consumed) == 96:
            worker._running = False
        return True

    worker._process_growth_inbox_once = consume
    await asyncio.wait_for(worker._growth_inbox_lane(local_only=True), timeout=2)
    assert len(consumed) == 96
    assert worker._project_growth_listener_facts.await_count == 3
    assert set(consumed[:30]) == set(range(1, 31))
