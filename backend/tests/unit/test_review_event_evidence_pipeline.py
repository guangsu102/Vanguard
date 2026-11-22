"""Exact push references survive projection; only event bodies avoid live reads."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account.listener_facts import apply_fact, enqueue_fact
from app.core.worker_status import TelegramEventInbox
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition import review_read_runtime as runtime
from app.modules.acquisition.adaptive_frequency import canonical
from app.modules.acquisition.qualification_events import acknowledge, pending, request
from app.modules.acquisition.qualification_renewal import try_light_renewal
from app.modules.acquisition.renewal_progress import record_checked, resume
from tests.unit.test_qualification_actions import Client
from tests.unit.test_qualification_light_renewal import fixture
from tests.unit.test_renewal_completion import rule_message

pytestmark = pytest.mark.asyncio


def reference(member, peer, mid, version="v1"):
    return {
        "key": f"message:{mid}:{version}",
        "version": version,
        "account_id": member.account_id,
        "group_id": peer,
        "joined_at": member.joined_at.isoformat(),
        "message_id": mid,
    }


async def test_projection_to_renewal_consumes_exact_event_body_and_still_reads_mature_proof(
    test_db, monkeypatch
):
    actor, account, group, member, row, previous = await fixture(test_db)
    peer = canonical(group.group_id, previous)
    ref = reference(member, peer, 200)
    fact = {
        "kind": "rules",
        "key": "fresh-rule-200",
        "peer": peer,
        "message_ids": [200],
        "at": datetime.utcnow().timestamp(),
        "joined_at": member.joined_at.isoformat(),
        "evidence_refs": {"200": ref},
    }
    await apply_fact(test_db, account.id, fact)
    await test_db.flush()
    event = await pending(test_db, member)
    assert event["evidence_refs"] == {"200": ref}
    assert row.decision == "trial" and member.ad_status == "active"
    client = Client(about="")
    client.entity.id = group.group_id
    for message, prior in zip(client.precedents, previous["evidence"], strict=True):
        message.date = datetime.fromisoformat(prior["date"])
    message = rule_message(200)
    client.get_messages = AsyncMock(wraps=client.get_messages)
    actor.account_pool.acquire_by_id.return_value.client = client
    cached = AsyncMock(return_value=[message])
    from app.modules.acquisition import review_read_flags

    monkeypatch.setattr(review_read_flags, "stage_enabled", lambda _account_id, _stage: True)
    monkeypatch.setattr(runtime, "enabled_for", lambda _account_id: True)
    monkeypatch.setattr(runtime, "cached_event_messages", cached)
    result = await try_light_renewal(
        actor, account, group, member, row, previous, force_refresh=False
    )
    assert result.passed
    cached.assert_awaited_once_with(
        account_id=account.id,
        group_id=peer,
        joined_at=member.joined_at,
        refs={"200": ref},
        client=client,
    )
    assert client.get_messages.await_count == 1
    assert client.get_messages.await_args.kwargs["ids"] == [101]
    assert await pending(test_db, member) == event
    await request(test_db, member, "rules", message_ids=[200], source_token="new-edit-without-body")
    await test_db.flush()
    assert not (await pending(test_db, member)).get("evidence_refs")
    assert not await acknowledge(test_db, member, event)


async def test_reference_merge_is_bounded_and_same_id_missing_ref_cannot_inherit(test_db):
    _, account, group, member, _, previous = await fixture(test_db)
    peer = canonical(group.group_id, previous)
    refs = {str(mid): reference(member, peer, mid) for mid in range(1, 41)}
    await request(test_db, member, "rules", message_ids=list(range(1, 41)), evidence_refs=refs)
    await test_db.flush()
    first = await pending(test_db, member)
    assert first["message_ids"] == list(range(9, 41))
    assert set(first["evidence_refs"]) == {str(mid) for mid in range(9, 41)}
    await request(test_db, member, "rules", message_ids=[40])
    await test_db.flush()
    second = await pending(test_db, member)
    assert "40" not in second["evidence_refs"] and "39" in second["evidence_refs"]
    assert not await acknowledge(test_db, member, first)


async def test_ref_with_wrong_scope_is_never_admitted_to_event(test_db):
    _, _, group, member, _, previous = await fixture(test_db)
    peer = canonical(group.group_id, previous)
    ref = reference(member, peer, 200)
    ref["account_id"] += 1
    await request(test_db, member, "rules", message_ids=[200], evidence_refs={"200": ref})
    await test_db.flush()
    assert not (await pending(test_db, member)).get("evidence_refs")


async def test_gap_never_uses_push_evidence_and_source_replay_preserves_full_event(
    test_db, monkeypatch
):
    actor, account, group, member, row, previous = await fixture(test_db)
    peer = canonical(group.group_id, previous)
    await request(
        test_db,
        member,
        "rules",
        message_ids=[200],
        evidence_refs={"200": reference(member, peer, 200)},
        source_token="rule-v1",
    )
    await request(test_db, member, "gap", source_token="gap-v1")
    await test_db.flush()
    event = await pending(test_db, member)
    await request(test_db, member, "rules", message_ids=[200], source_token="rule-v1")
    await test_db.flush()
    assert await pending(test_db, member) == event
    cached = AsyncMock(return_value=[])
    refreshed = AsyncMock()
    monkeypatch.setattr(runtime, "cached_event_messages", cached)
    monkeypatch.setattr(actions, "refresh_live_authorization", refreshed)
    result = await try_light_renewal(
        actor, account, group, member, row, previous, force_refresh=False
    )
    assert result.passed
    cached.assert_not_awaited()
    assert "event_evidence" not in refreshed.await_args.kwargs
    assert await pending(test_db, member) == event


async def test_unscoped_role_event_cannot_hide_behind_accumulated_message_ids(test_db):
    _, _, _, member, _, previous = await fixture(test_db)
    await request(test_db, member, "rules", message_ids=[200], source_token="message-v1")
    await test_db.flush()
    first = await pending(test_db, member)
    now = datetime.utcnow()
    progress = resume(previous, first, member, now)
    record_checked(progress, "201", "old-role", now)
    previous["renewal_progress"] = progress
    await request(test_db, member, "rules", source_token="role-update-without-message-id")
    await test_db.flush()
    later = await pending(test_db, member)
    assert later["message_ids"] == [200] and later.get("unscoped_rules_token")
    assert not resume(previous, later, member, now)["checked"]


async def test_new_ref_version_is_not_lost_in_existing_five_minute_inbox_bucket(
    test_db, monkeypatch
):
    _, account, group, member, _, previous = await fixture(test_db)
    peer = canonical(group.group_id, previous)
    monkeypatch.setattr(runtime, "enabled_for", lambda _account_id: True)
    fact = {
        "kind": "rules",
        "key": "rules:same-slot",
        "peer": peer,
        "message_ids": [200],
        "at": datetime.utcnow().timestamp(),
        "joined_at": member.joined_at.isoformat(),
        "evidence_refs": {"200": reference(member, peer, 200)},
    }
    await enqueue_fact(test_db, account.id, fact)
    await enqueue_fact(test_db, account.id, fact)
    edited = {**fact, "evidence_refs": {"200": reference(member, peer, 200, "v2")}}
    await enqueue_fact(test_db, account.id, edited)
    await test_db.flush()
    rows = (
        await test_db.scalars(
            select(TelegramEventInbox).where(
                TelegramEventInbox.account_id == account.id,
            )
        )
    ).all()
    assert len(rows) == 2
