"""Exercise real SQLite queue, event generations and send gate without Telegram."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_events as events
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.qualification_due import current_clause, due_clause
from app.modules.acquisition.models import GroupQualificationAudit
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


async def exhausted(db, *, event=True, nested=False):
    account, group, member, audit = await setup(db, decision="technical_wait")
    now = datetime.utcnow()
    member.review_status, member.ad_status = "review_2h", "warming"
    audit.reason, audit.next_retry_at = "KeyError", None
    previous = json.loads(audit.evidence_json)
    previous.update(technical_failures=5, technical_recovery_exhausted=True,
                    technical_recovery_started_at=(now-timedelta(hours=25)).isoformat(),
                    technical_recovery_deadline_at=(now-timedelta(hours=1)).isoformat(),
                    lease_wait_started_at=(now-timedelta(hours=25)).isoformat(),
                    review_trigger="new_evidence_required")
    if nested:
        previous["pending_collection"] = dict(previous)
    audit.evidence_json = json.dumps(previous)
    if event:
        await events.request(db, member, "rules", message_ids=[99], source_token="rule-version-1")
    await db.commit()
    return account, group, member, audit, previous


async def test_new_evidence_makes_audit_due_without_grant_or_ack(test_db):
    from app.core.account.telegram_execution import TelegramExecutionError
    from app.modules.acquisition.qualification_actions import validate_live_send

    account, group, member, audit, before = await exhausted(test_db, nested=True)
    assert not (await test_db.scalars(select(GroupQualificationAudit).where(due_clause(datetime.utcnow())))).all()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1
    await test_db.commit()
    assert audit.state == "queued" and audit.decision == "technical_wait"
    assert (member.ad_status, member.review_status) == ("warming", "review_2h")
    due = (await test_db.scalars(select(GroupQualificationAudit).where(
        current_clause(), due_clause(datetime.utcnow())))).all()
    assert [row.id for row in due] == [audit.id]
    event = await events.pending(test_db, member)
    assert event["queued"] and event["message_ids"] == [99]
    saved = json.loads(audit.evidence_json)
    assert saved["evidence"] == before["evidence"]
    assert saved["technical_failures"] == saved["pending_collection"]["technical_failures"] == 0
    assert "lease_wait_started_at" not in saved and "lease_wait_started_at" not in saved["pending_collection"]
    start = datetime.fromisoformat(saved["technical_recovery_started_at"])
    assert datetime.fromisoformat(saved["technical_recovery_deadline_at"])-start == timedelta(hours=24)
    with pytest.raises(TelegramExecutionError):
        await validate_live_send(test_db, object(), account.id, group.group_id)
    assert await events.pending(test_db, member) == event


async def test_same_event_has_three_failures_then_stops_until_new_source(test_db):
    account, _, member, audit, _ = await exhausted(test_db)
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1
    await test_db.commit()
    event = await events.pending(test_db, member)
    prior = json.loads(audit.evidence_json)
    started = datetime.fromisoformat(prior["technical_recovery_started_at"])
    for index in range(3):
        snapshot = {"decision": "technical_wait", "reason": "KeyError", "collection_event": event}
        decision, reason, state, retry = service.review_schedule(snapshot, prior, started+timedelta(minutes=index))
        audit.decision, audit.reason, audit.state, audit.next_retry_at = decision, reason, state, retry
        audit.evidence_json = json.dumps(snapshot)
        prior = snapshot
    await test_db.commit()
    assert prior["technical_failures"] == 3 and prior["technical_recovery_exhausted"] is True
    assert audit.next_retry_at is None
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0
    await events.request(test_db, member, "rules", message_ids=[99], source_token="rule-version-1")
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0
    await events.request(test_db, member, "rules", message_ids=[99], source_token="rule-version-2")
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1
    assert audit.decision == "technical_wait"


async def test_legacy_queued_event_gets_one_recovery_and_newer_event_survives(test_db):
    account, _, member, audit, _ = await exhausted(test_db)
    stored = await test_db.get(SystemSetting, events.key(member.id))
    data = json.loads(stored.value)
    data["queued"] = True
    stored.value = json.dumps(data)
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1
    prior_key = json.loads(audit.evidence_json)["technical_recovery_event_key"]
    await events.request(test_db, member, "membership", source_token="newer")
    await test_db.commit()
    newest = await events.pending(test_db, member)
    assert newest["queued"] is False and events.recovery_event_key(newest) != prior_key


async def test_last_collected_event_cannot_restart_legacy_terminal(test_db):
    account, _, member, audit, previous = await exhausted(test_db)
    previous["collection_event"] = await events.pending(test_db, member)
    audit.evidence_json = json.dumps(previous)
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0


async def test_real_account_gap_without_member_event_can_wake_terminal(test_db):
    account, _, member, audit, _ = await exhausted(test_db, event=False)
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0
    await events.account_gap(test_db, account.id)
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1
    assert (await events.pending(test_db, member))["kinds"] == ["gap"]
    assert audit.decision == "technical_wait"


@pytest.mark.parametrize("changed", ["paused", "left", "protected", "rejoined", "future_retry", "running", "superseded", "not_exhausted"])
async def test_terminal_recovery_keeps_scope_and_explicit_holds(test_db, changed):
    account, group, member, audit, previous = await exhausted(test_db)
    if changed == "paused":
        member.ad_status = "paused"
    elif changed == "left":
        member.status, member.left_at = "left", datetime.utcnow()
        member.ad_status = "blocked"
    elif changed == "protected":
        member.review_status = "owned_group_excluded"
    elif changed == "rejoined":
        member.joined_at = datetime.utcnow()
    elif changed == "future_retry":
        audit.next_retry_at = datetime.utcnow()+timedelta(hours=1)
    elif changed == "running":
        audit.state = "running"
    elif changed == "not_exhausted":
        previous["technical_recovery_exhausted"] = False
        audit.evidence_json = json.dumps(previous)
    else:
        test_db.add(GroupQualificationAudit(batch_id="newer", membership_id=member.id,
            account_id=account.id, group_id=group.id, membership_joined_at=member.joined_at,
            policy_version=audit.policy_version, content_scope="text_profile", decision="unknown", state="queued"))
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0
    assert audit.decision == "technical_wait"


async def test_pending_initial_technical_wait_is_recoverable(test_db):
    account, _, member, _, _ = await exhausted(test_db)
    member.status, member.review_status, member.ad_status = "pending", "initial_pending", "blocked"
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1


async def test_scan_rotates_beyond_300_unchanged_terminal_events(test_db):
    account, group, member, audit, _ = await exhausted(test_db)
    current = await events.pending(test_db, member)
    for index in range(301):
        gid, mid = 1000+index, 2000+index
        scoped = {**current, "joined_at": member.joined_at.isoformat(), "token": f"event-{index}"}
        prior = {"technical_recovery_exhausted": True, "technical_failures": 3}
        if index < 300:
            prior["technical_recovery_event_key"] = events.recovery_event_key(scoped)
        test_db.add_all([Group(id=gid, group_id=gid, title="synthetic"),
            GroupAccountMembership(id=mid, account_id=account.id, group_id=gid, telegram_group_id=gid,
                status="joined", joined_at=member.joined_at, review_status="review_2h", ad_status="warming"),
            GroupQualificationAudit(batch_id=f"synthetic-{index}", membership_id=mid, account_id=account.id,
                group_id=gid, membership_joined_at=member.joined_at, policy_version=audit.policy_version,
                content_scope="text_profile", state="completed", decision="technical_wait", evidence_json=json.dumps(prior)),
            SystemSetting(key=events.key(mid), value=json.dumps(scoped))])
    # The original row is unchanged too. The first 300 candidates produce no work.
    original = json.loads(audit.evidence_json)
    original["technical_recovery_event_key"] = events.recovery_event_key(current)
    audit.evidence_json = json.dumps(original)
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id) == 1
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0


async def test_one_recovery_scan_never_enqueues_more_than_two(test_db):
    account, _, member, audit, previous = await exhausted(test_db)
    for index in range(2):
        gid, mid = 3000+index, 4000+index
        test_db.add_all([Group(id=gid, group_id=gid, title="synthetic"),
            GroupAccountMembership(id=mid, account_id=account.id, group_id=gid, telegram_group_id=gid,
                status="joined", joined_at=member.joined_at, review_status="review_2h", ad_status="warming"),
            GroupQualificationAudit(batch_id=f"bounded-{index}", membership_id=mid, account_id=account.id,
                group_id=gid, membership_joined_at=member.joined_at, policy_version=audit.policy_version,
                content_scope="text_profile", state="completed", decision="technical_wait", evidence_json=json.dumps(previous)),
            SystemSetting(key=events.key(mid), value=json.dumps({
                "joined_at": member.joined_at.isoformat(), "kinds": ["rules"], "token": f"new-{index}"}))])
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id, limit=0) == 0
    assert await events.queue_terminal_technical_events(test_db, account.id, limit=99) == 2
    await test_db.commit()
    assert await events.queue_terminal_technical_events(test_db, account.id, limit=99) == 1
    assert await events.queue_terminal_technical_events(test_db, account.id) == 0


async def test_diagnostic_never_contains_exception_args_identifiers_or_dynamic_names(monkeypatch):
    namespace = {"__name__": "app.modules.acquisition.review_read_runtime"}
    exec("def candidate_ids():\n    raise KeyError('sensitive-body-or-proxy-password')", namespace)
    try:
        namespace["candidate_ids"]()
    except KeyError as exc:
        diagnostic = service.technical_diagnostic(exc, "candidate_hints")
    encoded = json.dumps(diagnostic)
    assert diagnostic["reason"] == "KeyError"
    assert diagnostic["counts"]["module_code"] == service.TECHNICAL_MODULES[namespace["__name__"]]
    assert diagnostic["counts"]["function_code"] == service.TECHNICAL_FUNCTIONS["candidate_ids"]
    assert "sensitive" not in encoded and "candidate_ids" not in encoded and "app." not in encoded
    assert all(type(value) is int for value in diagnostic["counts"].values())
    assert service.technical_diagnostic(type("SensitiveDynamicName", (Exception,), {})(), "dynamic")["reason"] == "other"


async def test_diagnostic_storage_failure_cannot_replace_original_error(monkeypatch):
    import sys
    from unittest.mock import Mock
    append = Mock(side_effect=OSError("private-local-path"))
    monkeypatch.setitem(sys.modules, "app.modules.acquisition.review_diagnostics", SimpleNamespace(append=append))
    result = service.record_technical_diagnostic(KeyError("secret"), "evidence_collect")
    assert result["reason"] == "KeyError"
    append.assert_called_once()
    assert "secret" not in str(append.call_args) and "private-local-path" not in str(result)


async def test_invalid_event_shapes_fail_closed():
    assert events.recovery_event_key({"kinds": ["gap"], "token": None}) is None
    assert events.recovery_event_key({"kinds": ["rules"], "token": "x", "bad": float("nan")}) is None
    first = {"kinds": ["rules"], "token": "x", "queued": False}
    assert events.recovery_event_key(first) == events.recovery_event_key({**first, "queued": True})


async def test_real_assess_error_records_stage_and_attempted_event_without_payload(test_db, monkeypatch):
    import sys
    from unittest.mock import Mock
    account, group, member, audit, _ = await exhausted(test_db)
    client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(id=group.group_id)),
                             get_me=AsyncMock(return_value=SimpleNamespace(id=200)),
                             send_message=AsyncMock(), send_file=AsyncMock())
    pool = SimpleNamespace(add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=SimpleNamespace(client=client)), release=AsyncMock())
    collector = SimpleNamespace(collect=AsyncMock(side_effect=KeyError("never-persist-this-secret")))
    monkeypatch.setattr(service, "EvidenceCollector", lambda *args, **kwargs: collector)
    monkeypatch.setattr("app.modules.acquisition.review_read_runtime.enabled_for", lambda _: False)
    append = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, "app.modules.acquisition.review_diagnostics", SimpleNamespace(append=append))
    fake = SimpleNamespace(db=test_db, account_pool=pool,
        _join_review_account_block_reason=lambda *_: None,
        _telegram_entity_matches_group_id=lambda *_: True)
    result = await service._assess(fake, account.id, group, row=audit, force_refresh=True)
    assert not result.passed and audit.reason == "KeyError" and audit.next_retry_at is None
    saved = json.loads(audit.evidence_json)
    assert "never-persist" not in audit.evidence_json and "technical_diagnostic" not in saved
    assert saved["technical_recovery_event_key"] == events.recovery_event_key(await events.pending(test_db, member))
    append.assert_called_once()
    assert append.call_args.args == ("review_collection_error",)
    assert append.call_args.kwargs["counts"]["stage_code"] == service.TECHNICAL_STAGES["evidence_collect"]
    client.send_message.assert_not_awaited()
    client.send_file.assert_not_awaited()
    pool.release.assert_awaited_once()


async def test_local_repair_reaches_terminal_event_without_audit_timer(test_db, monkeypatch):
    from app.modules.acquisition import qualification_recovery
    account, _, member, audit, _ = await exhausted(test_db)
    cache = SimpleNamespace(set=AsyncMock(return_value=True))
    monkeypatch.setattr("app.core.account.event_inbox.recover_safe_events", AsyncMock(return_value=0))
    result = await qualification_recovery.repair(test_db, cache, datetime.utcnow())
    assert result["technical_event_reviews"] == result["event_reviews"] == 1
    assert result["budget_wakeups"] == 0 and audit.state == "queued"
    assert (await events.pending(test_db, member))["queued"]
