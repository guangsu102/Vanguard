"""Renewal keeps stable work without extending live role/permission evidence."""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import errors

from app.core.account.telegram_execution import TelegramExecutionError
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition.renewal_progress import (
    MAX_CHECKED,
    administrator_ids,
    checked_fresh,
    record_checked,
    resume,
)
from tests.unit.test_qualification_actions import Client
from tests.unit.test_qualification_actions import live_auth as original_live_auth
from tests.unit.test_renewal_completion import channel, rule_message


@pytest.fixture(autouse=True)
def enable_read_optimization(monkeypatch):
    from app.modules.acquisition import review_read_flags

    monkeypatch.setattr(review_read_flags, "stage_enabled", lambda _account_id, _stage: True)


@pytest.fixture(name="live_auth")
def renewal_authorization(monkeypatch):
    return original_live_auth.__wrapped__(monkeypatch)


def checkpoint(now, event=None):
    member = Obj(id=60, account_id=2, group_id=456, joined_at=now - timedelta(days=3))
    event = event or {"kinds": ["rules"], "message_ids": [22], "token": "one"}
    previous = {"policy_version": "policy", "profile_fingerprint": "profile"}
    progress = resume(previous, event, member, now)
    previous["renewal_progress"] = progress
    return member, event, previous, progress


def test_wait_retains_signatures_but_expires_individual_role_checks():
    now = datetime.utcnow()
    member, event, previous, progress = checkpoint(now)
    record_checked(progress, "22", "signature", now)
    later = resume(previous, event, member, now + timedelta(hours=3))
    assert later["checked"] == {"22": "signature"}
    assert not checked_fresh(later, "22", "signature", now + timedelta(hours=3))
    record_checked(later, "22", "signature", now + timedelta(hours=3))
    assert checked_fresh(later, "22", "signature", now + timedelta(hours=3, minutes=4))
    assert not checked_fresh(later, "22", "signature", now + timedelta(hours=3, minutes=6))


def test_concrete_message_change_keeps_unrelated_work_without_acknowledging_event():
    now = datetime.utcnow()
    member, event, previous, progress = checkpoint(now)
    record_checked(progress, "22", "changed", now)
    record_checked(progress, "23", "unaffected", now)
    later_event = {**event, "token": "two"}
    later = resume(previous, later_event, member, now)
    assert later["checked"] == {"23": "unaffected"}
    assert later["checked_at"] == {"23": now.isoformat()}
    assert later["event"] == later_event
    assert progress["event"] == event and progress["checked"]["22"] == "changed"


@pytest.mark.parametrize(
    "change", ["gap", "roles", "generation", "membership", "account", "group", "system"]
)
def test_unknown_dependency_or_changed_scope_cannot_reuse_checks(change):
    now = datetime.utcnow()
    member, event, previous, progress = checkpoint(now)
    record_checked(progress, "23", "signature", now)
    changed = {**event, "token": "two"}
    if change == "gap":
        changed["kinds"] = ["gap"]
    elif change == "roles":
        changed["message_ids"] = []
    elif change == "generation":
        changed["account_generation"] = "gap:changed"
    elif change == "membership":
        member.joined_at += timedelta(seconds=1)
    elif change == "account":
        member.account_id += 1
    elif change == "group":
        member.group_id += 1
    elif change == "system":
        previous["system_user_ids"] = [123]
    assert not resume(previous, changed, member, now)["checked"]


def test_legacy_checkpoint_migration_keeps_signature_without_renewing_role_age():
    now = datetime.utcnow()
    member, event, previous, _ = checkpoint(now)
    scope = {
        "event": event,
        "membership": member.id,
        "joined_at": member.joined_at.isoformat(),
        "policy": "policy",
        "profile": "profile",
    }
    previous["renewal_progress"] = {
        "scope": hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest(),
        "started_at": (now - timedelta(minutes=6)).isoformat(),
        "checked": {"22": "sig"},
    }
    later = resume(previous, event, member, now)
    assert later["checked"] == {"22": "sig"} and later["version"] == 2
    assert not checked_fresh(later, "22", "sig", now)


def test_progress_metadata_is_bounded_and_event_snapshot_does_not_alias_input():
    now = datetime.utcnow()
    member, event, previous, progress = checkpoint(
        now,
        {
            "kinds": ["rules"],
            "message_ids": [22],
            "token": "one",
            "sources": {"rules": "v1"},
        },
    )
    event["sources"]["rules"] = "v2"
    assert progress["event"]["sources"]["rules"] == "v1"
    for mid in range(MAX_CHECKED + 1):
        record_checked(progress, str(mid), "sig", now)
    assert len(progress["checked"]) == len(progress["checked_at"]) == MAX_CHECKED
    assert "0" not in progress["checked"]
    assert not checked_fresh(progress, "1", "sig", now - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_admin_denial_expires_independently_of_retained_progress():
    now = datetime.utcnow()
    progress = {"started_at": (now - timedelta(hours=2)).isoformat(), "checked": {"22": "sig"}}
    client = AsyncMock(side_effect=errors.ChatAdminRequiredError(None))
    assert await administrator_ids(client, channel(), progress) is None
    assert await administrator_ids(client, channel(), progress) is None
    assert client.await_count == 1
    progress["admin_list_retry_at"] = (now - timedelta(seconds=1)).isoformat()
    client.side_effect = None
    client.return_value = Obj(count=1, participants=[Obj(user_id=999)])
    assert await administrator_ids(client, channel(), progress) == {999}
    assert client.await_count == 2 and progress["checked"] == {"22": "sig"}


@pytest.mark.asyncio
async def test_proof_event_and_admin_ids_share_one_batch_and_author_read(live_auth):
    row, _, db = live_auth
    snapshot = json.loads(row.evidence_json)
    snapshot["evidence"].append(
        {
            "source": "admin_rule",
            "message_id": 200,
            "text": "旧规则转述",
            "edited_at": None,
        }
    )
    row.evidence_json = json.dumps(snapshot)
    client = Client()
    latest = rule_message(200)
    latest.sender_id = client.precedents[0].sender_id
    latest.sender = client.precedents[0].sender
    latest.message = "旧规则转述"
    client.history = [latest]
    client.old_admin = [latest]
    messages, permissions = client.get_messages, client.get_permissions
    client.get_messages = AsyncMock(wraps=messages)
    client.get_permissions = AsyncMock(wraps=permissions)
    await actions.refresh_live_authorization(
        db,
        client,
        2,
        456,
        recheck_rules=True,
        message_ids=[101, 200, 200],
        progress={},
    )
    assert client.get_messages.await_count == 1
    requested = client.get_messages.await_args.kwargs["ids"]
    assert requested == [101, 200]
    authors = [
        call.args[1] for call in client.get_permissions.await_args_list if call.args[1] != "me"
    ]
    assert len(authors) == 1 and authors[0].id == 501
    assert row.decision == "trial"


@pytest.mark.asyncio
async def test_a_later_renewal_does_not_reuse_previous_context_role(live_auth):
    row, _, db = live_auth
    client = Client()
    await actions.refresh_live_authorization(db, client, 2, 456)
    client.sender_permissions[501] = Obj(
        is_admin=True, is_creator=False, has_left=False, is_banned=False
    )
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert (
        json.loads(row.evidence_json)["reason"]
        == "advertising_precedent_identity_changed_before_send"
    )


@pytest.mark.asyncio
async def test_expired_rule_role_is_rechecked_while_stable_progress_survives(live_auth):
    row, _, db = live_auth
    row.decision = "allowed"
    client = Client()
    client.history = [rule_message(200)]
    progress = {}
    await actions.refresh_live_authorization(
        db,
        client,
        2,
        456,
        recheck_rules=True,
        message_ids=[200],
        progress=progress,
    )
    assert "200" in progress["checked"]
    progress["checked_at"]["200"] = (datetime.utcnow() - timedelta(minutes=6)).isoformat()
    client.sender_permissions[1200] = Obj(
        is_admin=True, is_creator=False, has_left=False, is_banned=False
    )
    with pytest.raises(TelegramExecutionError, match="rules_changed"):
        await actions.refresh_live_authorization(
            db,
            client,
            2,
            456,
            recheck_rules=True,
            message_ids=[200],
            progress=progress,
        )


@pytest.mark.asyncio
async def test_missing_event_in_union_is_unknown_and_never_silently_acknowledged(live_auth):
    row, member, db = live_auth
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(
            db,
            Client(),
            2,
            456,
            recheck_rules=True,
            message_ids=[999],
            progress={},
        )
    assert member.ad_status == "warming"
    assert json.loads(row.evidence_json)["reason"] == "event_rules_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["low_online", "unknown_online", "rules", "permission_only"])
async def test_unread_rules_becoming_relevant_return_to_original_evidence_path(live_auth, change):
    row, member, db = live_auth
    snapshot = json.loads(row.evidence_json)
    snapshot["rules_evaluation_status"] = "NOT_EVALUATED"
    row.evidence_json = json.dumps(snapshot)
    client = Client()
    client.full.online_count = None if change == "unknown_online" else 1
    if change == "rules":
        client.full.online_count = 2
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(
            db,
            client,
            2,
            456,
            permissions_only=change == "permission_only",
            recheck_rules=change == "rules",
        )
    assert row.decision == "observe" and member.ad_status == "warming"
    assert json.loads(row.evidence_json)["reason"] == "deferred_rules_require_evidence"


@pytest.mark.asyncio
async def test_push_cannot_replace_live_precedent_or_old_admin_rule(live_auth):
    row, _, db = live_auth
    snapshot = json.loads(row.evidence_json)
    snapshot["evidence"].append(
        {
            "source": "admin_rule",
            "message_id": 200,
            "text": "原规则",
            "edited_at": None,
        }
    )
    row.evidence_json = json.dumps(snapshot)
    client = Client()
    old_rule = rule_message(200)
    old_rule.message = "原规则"
    client.history = [old_rule]
    client.get_messages = AsyncMock(wraps=client.get_messages)
    await actions.refresh_live_authorization(
        db,
        client,
        2,
        456,
        recheck_rules=True,
        message_ids=[101, 200],
        event_evidence=[client.precedents[0], old_rule],
        progress={},
    )
    assert client.get_messages.await_args.kwargs["ids"] == [101, 200]


@pytest.mark.asyncio
async def test_disabled_optimization_ignores_event_body_and_uses_original_reads(
    live_auth, monkeypatch
):
    from app.modules.acquisition import review_read_flags

    monkeypatch.setattr(review_read_flags, "stage_enabled", lambda _account_id, _stage: False)
    _, _, db = live_auth
    client = Client()
    message = rule_message(200)
    client.history = [message]
    client.get_messages = AsyncMock(wraps=client.get_messages)
    await actions.refresh_live_authorization(
        db,
        client,
        2,
        456,
        recheck_rules=True,
        message_ids=[200],
        event_evidence=[message],
        progress={},
    )
    assert [call.kwargs["ids"] for call in client.get_messages.await_args_list] == [[101], [200]]
