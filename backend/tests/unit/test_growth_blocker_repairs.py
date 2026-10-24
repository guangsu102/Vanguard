"""Behavioral regressions for owner reuse, due work and bounded recovery."""

from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError
from telethon import errors, types

from app.core.account import event_inbox
from app.core.account.session_lease import PROCESS_ID
from app.core.group.models import Group
from app.core.scheduler import growth_dispatch
from app.core.worker_status import TelegramEventInbox
from app.modules.acquisition import candidate_inventory, qualification_service
from app.modules.acquisition.candidate_preview import public_candidate_preview
from app.modules.acquisition.group_qualification import EvidenceCollector
from app.modules.acquisition.qualification_verification import run_verifications
from tests.unit.test_qualification_verification import setup


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "completed"])
@pytest.mark.parametrize("owner", [PROCESS_ID, "foreign-process"])
async def test_verification_on_leased_session_runs_only_in_owner(test_db, monkeypatch, state, owner):
    service, client, _, row = await setup(test_db)
    row.state = state
    await test_db.commit()
    cache = Obj(exists=AsyncMock(return_value=1), get=AsyncMock(return_value=owner))
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=cache))
    result = await run_verifications(service)
    assert result["attempted"] == int(owner == PROCESS_ID)
    assert service.account_pool.acquire_by_id.await_count == int(owner == PROCESS_ID)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,decision,delay,expected", [
    ("queued", "unknown", 3600, False), ("queued", "unknown", None, True),
    ("completed", "observe", -1, True), ("completed", "wait", -1, True),
    ("completed", "reject", -1, True), ("completed", "allowed", -1, True),
    ("completed", "trial", -1, True), ("waiting_ai", "observe", -1, True),
    ("reviewing_ai", "observe", -1, True), ("completed", "reject", None, False),
])
async def test_dispatches_all_due_review_families_only_for_current_scope(test_db, state, decision, delay, expected):
    _, _, member, row = await setup(test_db)
    now = datetime.utcnow()
    row.state, row.decision = state, decision
    row.next_retry_at = now + timedelta(seconds=delay) if delay is not None else None
    await test_db.commit()
    sets = await growth_dispatch.account_sets(test_db, now)
    assert (2 in sets["review"]) == expected
    member.joined_at += timedelta(seconds=1)
    await test_db.commit()
    assert 2 not in (await growth_dispatch.account_sets(test_db, now))["review"]


@pytest.mark.asyncio
async def test_safe_recovery_drains_peer_head_and_preserves_unknown_write(test_db):
    await setup(test_db)
    now = datetime.utcnow()
    safe = TelegramEventInbox(account_id=2, event_key="safe", kind="deleted", chat_id=-10040,
                              state="reconciliation_required", external_attempted=False, payload_json="{}")
    unknown = TelegramEventInbox(account_id=2, event_key="unknown", kind="join", chat_id=-10041,
                                 state="reconciliation_required", external_attempted=True, payload_json="{}")
    test_db.add_all([safe, unknown])
    await test_db.flush()
    later = TelegramEventInbox(account_id=2, event_key="later", kind="message", chat_id=-10040,
                               state="pending", next_attempt_at=now, payload_json="{}")
    test_db.add(later)
    await test_db.commit()
    assert await event_inbox.recover_safe_events(test_db) == 1
    await test_db.commit()
    first = await event_inbox.claim(test_db, [2], local_only=False)
    assert first.id == safe.id
    await event_inbox.finish(test_db, first.id)
    await test_db.commit()
    assert (await event_inbox.claim(test_db, [2], local_only=False)).id == later.id
    assert unknown.state == "reconciliation_required" and unknown.external_attempted
    with pytest.raises(ValueError, match="evidence_required"):
        await event_inbox.resolve_reconciliation(test_db, unknown.id, evidence="unknown", actor="admin")
    resolved = await event_inbox.resolve_reconciliation(
        test_db, unknown.id, evidence="Operator verified Telegram and durable send receipts", actor="admin",
    )
    assert resolved.state == "reconciled" and resolved.external_attempted


@pytest.mark.asyncio
async def test_forwarding_releases_database_connection_before_wait(test_db, monkeypatch):
    await setup(test_db)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=Obj()))
    monkeypatch.setattr("app.core.account.session_exec.owner_id", AsyncMock(return_value="foreign"))
    async def forward(*args):
        assert not test_db.in_transaction()
        return {"result": {"processed": 1}, "interval": 60}
    monkeypatch.setattr("app.core.account.session_exec.forward", forward)
    assert (await growth_dispatch.execute(test_db, "review", 2))[0]["processed"] == 1


@pytest.mark.asyncio
async def test_temporary_preview_failure_expires_and_performs_a_fresh_read():
    now = datetime.utcnow()
    group = Obj(group_id=123, username="candidate")
    client = AsyncMock()
    client.get_entity.return_value = Obj(id=123, megagroup=True)
    client.return_value = Obj(full_chat=Obj(about="允许广告", participants_count=100, online_count=4))
    old = {"version": 1, "account_id": 2, "telegram_group_id": 123, "username": "candidate",
           "status": "entity_unknown", "preview_terminal": True,
           "checked_at": (now - timedelta(hours=1)).isoformat(),
           "expires_at": (now - timedelta(minutes=30)).isoformat()}
    assert candidate_inventory.due(old, group, 2, now)
    hint = await public_candidate_preview(client, group, own_user_ids=set(), identity_coverage=True, now=now, progress=old)
    assert hint.status == "metadata_checked" and hint.member_count == 100
    client.get_entity.assert_awaited_once()


@pytest.mark.asyncio
async def test_candidate_page_after_300_excluded_targets_is_reachable(test_db):
    service, _, _, _ = await setup(test_db)
    test_db.add_all([Group(group_id=10000 + i, username=f"candidate{i}", status="pending_join") for i in range(301)])
    await test_db.commit()
    pages = []
    async def rank(account_id, groups):
        pages.append([group.id for group in groups])
        return [] if len(groups) == 300 else groups
    service._rank_pending_join_candidates = rank
    assert await service._next_pending_join_group(account_id=2, preview=True) is None
    chosen = await service._next_pending_join_group(account_id=2, preview=True)
    assert chosen is not None and len(pages[1]) == 1 and chosen.id not in pages[0]


@pytest.mark.asyncio
async def test_denied_participant_query_uses_only_confirmed_roster_objects():
    client = Obj(get_permissions=AsyncMock(side_effect=errors.ChatAdminRequiredError(None)),
                 get_participants=AsyncMock(return_value=[
                     Obj(id=11, bot=False, deleted=False, participant=types.ChannelParticipant(11, datetime.utcnow())),
                     Obj(id=12, bot=False, deleted=False, participant=None),
                 ]))
    collector = EvidenceCollector(client)
    entity = Obj(id=40)
    for sender_id, expected in [(11, "ordinary"), (12, "unknown"), (13, "unknown")]:
        msg = Obj(sender_id=sender_id, id=sender_id, sender=Obj(id=sender_id, bot=False, deleted=False), message="广告")
        assert await collector.role(entity, msg) == expected
    client.get_participants.assert_awaited_once()
    assert client.get_permissions.await_count == 1


def test_business_deadline_and_successful_quantum_schedule_differ():
    now = 1000.0
    date = datetime(1970, 1, 1) + timedelta(seconds=4600)
    result = {"processed": 0, "details": [{"next_allowed_at": date.isoformat()}]}
    assert growth_dispatch.next_due(result, 60, now=now) == 4600
    assert growth_dispatch.next_due(
        {"processed": 0, "details": [{"retry_after_seconds": 3600}]}, 60, now=now
    ) == 4600
    assert growth_dispatch.next_due(
        {"processed": 0, "details": [{"next_retry_at": date.isoformat()}]}, 60, now=now
    ) == 4600
    assert growth_dispatch.next_due({**result, "succeeded": 1}, 60, now=now) == 1060


def test_technical_recovery_deadline_does_not_move_on_retry():
    now = datetime.utcnow()
    prior = {"technical_recovery_started_at": (now - timedelta(hours=23)).isoformat(), "technical_failures": 1}
    check = {"decision": "technical_wait", "reason": "evidence_read_failed", "technical_errors": ["TimeoutError"]}
    _, _, _, retry = qualification_service.review_schedule(check, prior, now)
    assert retry == now + timedelta(hours=1)
    assert qualification_service.review_schedule(dict(check), check, retry)[3] is None


@pytest.mark.asyncio
async def test_worker_keeps_running_after_transient_pool_timeout(monkeypatch):
    from app.workers import telegram_worker as module
    worker = module.TelegramWorker(module.TelegramWorkerRole.GUARDIAN_BOT)
    for name in ("init_db", "init_redis", "close_db", "close_redis", "start_account_proxy_policy_listener", "stop_account_proxy_policy_listener"):
        monkeypatch.setattr(module, name, AsyncMock())
    worker._heartbeat = AsyncMock()
    runs = 0
    async def cycle():
        nonlocal runs
        runs += 1
        if runs == 1:
            raise DatabaseTimeoutError("pool busy")
        worker._running = False
    worker.run_once = cycle
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    await worker.run()
    assert runs == 2
    assert worker._heartbeat.await_count == 1
