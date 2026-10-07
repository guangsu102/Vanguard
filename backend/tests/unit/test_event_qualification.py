"""Event-driven qualification uses real local DB state and synthetic Telegram facts."""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions.contacts import ResolveUsernameRequest

from app.core.account.rpc_governor import RpcDeferred, RpcGovernor, install_governor
from app.core.account.telegram_execution import TelegramExecutionError
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition.qualification_events import (
    acknowledge,
    pending,
    queue_due_gaps,
    request,
)
from app.modules.acquisition.qualification_renewal import try_light_renewal
from tests.unit.test_qualification_light_renewal import fixture

pytestmark = pytest.mark.asyncio


async def test_unchanged_grant_needs_no_pool_or_network(test_db):
    service, account, group, member, row, before = await fixture(test_db)
    result = await try_light_renewal(service, account, group, member, row, before, force_refresh=False)
    assert result.passed and row.next_retry_at is None
    service.account_pool.acquire_by_id.assert_not_awaited()
    assert json.loads(row.evidence_json)["collected_at"] == before["collected_at"]
    await actions.validate_live_send(test_db, object(), account.id, group.group_id)


async def test_callback_blocks_send_and_newer_callback_survives_ack(test_db):
    _, account, group, member, row, _ = await fixture(test_db)
    await request(test_db, member, "membership")
    await test_db.flush()
    first = await pending(test_db, member)
    with pytest.raises(TelegramExecutionError, match="qualification_event_pending"):
        await actions.validate_live_send(test_db, object(), account.id, group.group_id)
    await request(test_db, member, "rules", message_ids=[91])
    await test_db.flush()
    assert not await acknowledge(test_db, member, first)
    latest = await pending(test_db, member)
    assert set(latest["kinds"]) == {"membership", "rules"}
    assert await acknowledge(test_db, member, latest)
    assert await pending(test_db, member) is None


async def test_old_membership_event_cannot_control_rejoined_scope(test_db):
    _, _, _, member, _, _ = await fixture(test_db)
    await request(test_db, member, "membership")
    await test_db.flush()
    member.joined_at = datetime.utcnow()
    assert await pending(test_db, member) is None


async def test_concrete_permission_event_uses_only_targeted_check(test_db, monkeypatch):
    service, account, group, member, row, before = await fixture(test_db)
    await request(test_db, member, "membership")
    await test_db.flush()
    refresh = AsyncMock()
    monkeypatch.setattr(actions, "refresh_live_authorization", refresh)
    result = await try_light_renewal(service, account, group, member, row, before, force_refresh=False)
    assert result.passed
    assert refresh.await_args.kwargs == {"permissions_only": True, "message_ids": [], "recheck_rules": False}
    # Worker acknowledgement, not the reader, clears a callback generation.
    assert await pending(test_db, member)


async def test_deferred_repair_preserves_grant_and_pending_event(test_db, monkeypatch):
    service, account, group, member, row, before = await fixture(test_db)
    await request(test_db, member, "gap")
    await test_db.flush()
    monkeypatch.setattr(actions, "refresh_live_authorization", AsyncMock(
        side_effect=RpcDeferred("telegram_read_budget", 600)))
    result = await try_light_renewal(service, account, group, member, row, before, force_refresh=False)
    assert not result.passed and row.decision == "trial"
    assert row.next_retry_at >= datetime.utcnow() + timedelta(seconds=590)
    assert await pending(test_db, member)


async def test_actual_read_still_hits_rpc_budget_after_local_validation(test_db, monkeypatch):
    from app.core import database, redis as redis_module

    service, account, group, member, row, before = await fixture(test_db)
    await actions.validate_live_send(test_db, object(), account.id, group.group_id)

    @asynccontextmanager
    async def session():
        yield test_db

    cache = Obj(ttl=AsyncMock(return_value=-2), eval=AsyncMock(return_value=600))
    monkeypatch.setattr(database, "get_db_session", session)
    monkeypatch.setattr(redis_module, "get_redis", AsyncMock(return_value=cache))
    original = AsyncMock()
    client = Obj(_call=original)
    install_governor(client, RpcGovernor(account.id, lambda: "ad_delivery"))
    with pytest.raises(RpcDeferred, match="telegram_read_budget"):
        await client._call(None, ResolveUsernameRequest("synthetic"))
    original.assert_not_awaited()
    cache.eval.assert_awaited_once()


@pytest.mark.parametrize("state,reason,blocked", [
    ("budget_wait", "telegram_read_budget", False),
    ("cooldown", "telegram_rpc_cooldown", True),
    ("unavailable", "telegram_rpc_guard_unavailable", True),
])
async def test_dispatch_only_defers_real_shared_blocks(test_db, monkeypatch, state, reason, blocked):
    from app.core.account import rpc_governor as module
    monkeypatch.setattr(module, "snapshot", AsyncMock(return_value={
        "state": state, "reason": reason, "retry_after_seconds": 600}))
    if blocked:
        with pytest.raises(RpcDeferred, match=reason):
            await module.check_dispatch_ready(test_db, 2)
    else:
        await module.check_dispatch_ready(test_db, 2)


async def test_reconnect_marker_does_not_enqueue_all_groups(test_db):
    from app.modules.acquisition.qualification_events import account_gap
    _, account, _, member, row, _ = await fixture(test_db)
    old_state, old_retry = row.state, row.next_retry_at
    await account_gap(test_db, account.id)
    await test_db.flush()
    first = await pending(test_db, member)
    assert first["kinds"] == ["gap"]
    assert (row.state, row.next_retry_at) == (old_state, old_retry)
    await account_gap(test_db, account.id)
    await test_db.flush()
    assert not await acknowledge(test_db, member, first)
    assert await acknowledge(test_db, member, await pending(test_db, member))
    assert await pending(test_db, member) is None


async def test_due_gap_queue_claims_at_most_two_and_is_idempotent(test_db):
    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.models import GroupQualificationAudit

    _, account, _, _, _, _ = await fixture(test_db)
    members = []
    now = datetime.utcnow()
    for offset in range(3):
        group = Group(
            id=41 + offset,
            group_id=1234567891 + offset,
            username=f"qualification_queue_{offset}",
            title=f"queue-{offset}",
        )
        member = GroupAccountMembership(
            id=61 + offset,
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="joined",
            joined_at=now - timedelta(days=5),
            review_status="approved",
            ad_status="active",
        )
        audit = GroupQualificationAudit(
            batch_id=f"queue-{offset}",
            membership_id=member.id,
            account_id=account.id,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            content_scope="text_profile",
            state="completed",
            decision="trial",
            membership_joined_at=member.joined_at,
            checked_at=now,
            expires_at=now + timedelta(hours=24),
        )
        test_db.add_all([group, member, audit])
        members.append(member)
    await test_db.commit()

    for member in members:
        await request(test_db, member, "gap")
    await test_db.commit()

    assert await queue_due_gaps(test_db, account.id, limit=10) == 2
    await test_db.commit()
    queued = 0
    for member in members:
        event = await pending(test_db, member)
        queued += int(bool(event and event["queued"]))
    assert queued == 2

    assert await queue_due_gaps(test_db, account.id, limit=10) == 1
    await test_db.commit()
    assert await queue_due_gaps(test_db, account.id, limit=10) == 0


async def test_acknowledge_rejects_paused_or_rejoined_membership(test_db):
    _, _, _, member, _, _ = await fixture(test_db)
    await request(test_db, member, "membership")
    await test_db.flush()
    event = await pending(test_db, member)

    member.ad_status = "paused"
    assert not await acknowledge(test_db, member, event)
    assert await pending(test_db, member) == event

    member.ad_status = "active"
    member.joined_at = datetime.utcnow()
    assert not await acknowledge(test_db, member, event)
    assert await pending(test_db, member) is None


async def test_unexpected_repair_error_is_visible_and_keeps_send_blocked(test_db):
    service, account, group, member, row, before = await fixture(test_db)
    await request(test_db, member, "gap")
    await test_db.flush()
    service.account_pool.acquire_by_id.side_effect = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        await try_light_renewal(service, account, group, member, row, before, force_refresh=False)
    assert await pending(test_db, member)
    with pytest.raises(TelegramExecutionError, match="qualification_event_pending"):
        await actions.validate_live_send(test_db, object(), account.id, group.group_id)


async def test_manual_pause_cannot_be_cleared_by_calendar_or_event_job(test_db):
    service, account, group, member, row, before = await fixture(test_db)
    member.ad_status = "paused"
    result = await try_light_renewal(service, account, group, member, row, before, force_refresh=False)
    assert not result.passed and member.ad_status == "paused"
    service.account_pool.acquire_by_id.assert_not_awaited()
