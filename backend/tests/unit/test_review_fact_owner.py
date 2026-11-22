"""Raw task handoff cannot share authorization, visibility or live proof."""
from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon._updates.messagebox import ENTRY_ACCOUNT

from app.modules.acquisition.group_qualification import EvidenceCollector
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore
from app.modules.acquisition.review_fact_owner import (
    REFERENCE,
    ReviewFactOwner,
    create_context,
    publish_context,
)
from tests.unit.test_qualification_actions import live_auth as original_live_auth
from tests.unit.test_qualification_read_context import ENTITY, Client, root


@pytest.fixture
def live_auth(monkeypatch):
    return original_live_auth.__wrapped__(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.modules.acquisition.review_evidence_ingest.cache_safe", lambda *_: True)
    store = ReviewEvidenceStore(EvidenceStoreConfig(
        directory=tmp_path / "facts", enabled=True,
        protected_slots=1, protected_slot_bytes=64*1024, protected_control_bytes=4096,
        protected_records_per_slot=8, protected_record_limit=8, slots_per_account=1,
        slots_per_account_group=1, ordinary_pages=1, ordinary_page_bytes=1024*1024,
        physical_limit_bytes=4*1024*1024,
    ))
    assert store.initialize_sync().ok
    now = datetime.now(UTC).replace(tzinfo=None)
    messages = [root(7), root(8)]
    for message in messages:
        message.date = now-timedelta(hours=30)
    client = Client(messages)
    client._sender = Obj(_connection=Obj())
    client._message_box = Obj(seq=10, map={ENTRY_ACCOUNT: Obj(pts=20), 42: Obj(pts=30)},
                              getting_diff_for=set(), possible_gaps={})
    client.session = Obj(journal=Obj(
        ordered_complete=True, qsize=lambda: 0, observed={}, protocol_recovery=set(),
        hold_global_protocol=False, failure=None, facts=lambda **_: [],
    ))
    client.session.journal.session = Obj(_cursor=lambda: Obj(
        execute=lambda *_: Obj(fetchone=lambda: None), close=lambda: None))
    client.get_entity = AsyncMock(side_effect=lambda target: Obj(id=target, bot=False))
    wrapper = Obj(client=client, operation_lease=Obj(account_id=34, token="private-owner-1"),
                  operation_lease_lost=False)
    scope = {"membership_id": 91, "joined_at": (now-timedelta(days=3)).isoformat(),
             "event": {"kind": "rules", "token": "one"}, "policy": {"version": "v5"},
             "profile": "p1", "system_identity": "s1"}

    async def context(previous=None, *, scope_override=None):
        value = ReviewFactOwner(account_id=34, wrapper=wrapper, store=store)
        await value.bind(ENTITY, scope_override or scope, previous or {})
        return value

    async def donor():
        value = await context()
        await value.get_messages(client, ENTITY, [7, 8])
        snapshot = {"collected_at": now.isoformat()}
        await publish_context(value, snapshot)
        assert REFERENCE in snapshot
        client.calls.clear()
        return snapshot

    return Obj(store=store, client=client, wrapper=wrapper, scope=scope, now=now,
               context=context, donor=donor)


@pytest.mark.asyncio
async def test_initial_pending_collection_to_observation_consumes_one_raw_bundle(env):
    # Exercise the collector's actual task continuation entry, not a synthetic
    # cache-hit counter. Roles and root maturity checks still use current reads.
    first_context = await env.context()
    first = EvidenceCollector(env.client, read_context=first_context, account_id=34)
    first.previous = {"collection_progress": {"pending_message_ids": [7, 8]}}
    material = await first._resume_messages(ENTITY, {"unknowns": [], "technical_errors": []}, env.now)
    assert [message.id for message in material] == [7, 8]
    assert await first.role(ENTITY, material[0]) == "ordinary"
    snapshot = {"collection_progress": {"pending_message_ids": [7, 8]},
                "collected_at": env.now.isoformat()}
    await publish_context(first_context, snapshot)
    env.client.calls.clear()
    env.wrapper.operation_lease = Obj(account_id=34, token="private-owner-2")
    second_context = await env.context(snapshot)
    second = EvidenceCollector(env.client, read_context=second_context, account_id=34)
    second.previous = snapshot
    resumed = await second._resume_messages(ENTITY, {"unknowns": [], "technical_errors": []}, env.now)
    assert [message.id for message in resumed] == [7, 8]
    assert env.client.calls == []
    assert await second.role(ENTITY, resumed[0]) == "ordinary"
    assert env.client.permissions == [(42, 2), (42, 2)]
    assert second_context.role(env.client, ENTITY, 2) == "ordinary"
    proof = {"message_id": 7, "text": root().message, "accessible": True}
    await second._verify_warning_thread(ENTITY, proof, env.now,
        {"unknowns": [], "technical_errors": [], "rules_incomplete": False})
    assert env.client.calls == [7]  # Mature proof does not inherit the old root.
    assert proof["warning_search_complete"] is True


@pytest.mark.asyncio
async def test_established_rule_and_selected_precedent_remain_live(env):
    previous = await env.donor()
    previous.update({"evidence": [
        {"source": "admin_rule", "message_id": 7},
        {"source": "recent_promotional_message", "message_id": 8, "sender_role": "ordinary",
         "accessible": True},
    ]})
    context = await env.context(previous)
    collector = EvidenceCollector(env.client, read_context=context, account_id=34)
    collector.previous = previous
    await collector._resume_messages(ENTITY, {"unknowns": [], "technical_errors": []}, env.now)
    assert env.client.calls == [[7, 8]]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "account", "joined", "event", "policy", "profile", "identity", "pts", "seq",
    "sender", "connection", "gap", "pending_edit", "queued_delete", "applying_delete",
    "lost_lease", "replaced_lease", "released_lease", "expired", "missing_record", "unresolved_raw",
])
async def test_changed_scope_or_unapplied_update_never_lends_old_raw(env, monkeypatch, change):
    previous = await env.donor()
    scope = copy.deepcopy(env.scope)
    if change in {"joined", "event", "policy", "profile", "identity"}:
        key = {"joined": "joined_at", "identity": "system_identity"}.get(change, change)
        scope[key] = "changed"
    elif change == "account":
        env.wrapper.operation_lease.account_id = 2
    elif change == "pts":
        env.client._message_box.map[42].pts += 1
    elif change == "seq":
        env.client._message_box.seq += 1
    elif change == "sender":
        env.client._sender = Obj(_connection=Obj())
    elif change == "connection":
        env.client._sender._connection = Obj()
    elif change == "gap":
        env.client._message_box.possible_gaps = {42: Obj()}
    elif change == "pending_edit":
        env.client.session.journal.facts = lambda **_: [("edit", json.dumps({"kind": "evidence_candidate", "peer": -1000000000042}))]
    elif change == "queued_delete":
        # DEPENDENCY_RESUME is deliberately not involved: old SDK PTS alone
        # cannot conceal a just-received delete before SDK application.
        monkeypatch.setattr("app.modules.acquisition.review_read_flags.stage_enabled", lambda *_: False)
        env.client.session.journal.qsize = lambda: 1
    elif change == "applying_delete":
        env.client.session.journal.ordered_complete = False
    elif change == "unresolved_raw":
        env.client.session.journal.session = Obj(_cursor=lambda: Obj(
            execute=lambda *_: Obj(fetchone=lambda: (1,)), close=lambda: None))
    elif change == "expired":
        later = time.time()+301
        monkeypatch.setattr("app.modules.acquisition.review_fact_owner.time.time", lambda: later)
    elif change == "missing_record":
        env.store.load_history_page = AsyncMock(return_value=Obj(ok=False))
    context = await env.context(previous, scope_override=scope)
    if change == "lost_lease":
        env.wrapper.operation_lease_lost = True
    elif change == "replaced_lease":
        env.wrapper.operation_lease = Obj(account_id=34, token="new-owner")
    elif change == "released_lease":
        env.wrapper.operation_lease = None
    result = await context.get_messages(env.client, ENTITY, [7])
    assert result[0] is env.client.messages[7]
    assert env.client.calls == [[7]]
    assert context.inherited_used_at is None


@pytest.mark.asyncio
async def test_explicit_live_read_and_local_permissions_do_not_share_across_tasks(env):
    previous = await env.donor()
    context = await env.context(previous)
    await context.get_messages(env.client, ENTITY, [7], require_live=True)
    await context.permissions(env.client, ENTITY, Obj(id=2))
    assert env.client.calls == [[7]]
    assert env.client.permissions == [(42, 2)]
    another = await env.context(previous)
    await another.permissions(env.client, ENTITY, Obj(id=2))
    assert env.client.permissions == [(42, 2), (42, 2)]


@pytest.mark.asyncio
async def test_update_during_missing_subset_read_discards_preselected_inherited_values(env):
    previous = await env.donor()
    context = await env.context(previous)
    original = env.client.get_messages

    async def read(entity, ids):
        if ids == [9]:
            edited = root(7, edited=env.now)
            edited.message = "changed after the handoff was selected"
            env.client.messages[7] = edited
            env.client._message_box.map[42].pts += 1
        return await original(entity, ids)

    env.client.get_messages = read
    values = await context.get_messages(env.client, ENTITY, [7, 9])
    assert values == [env.client.messages[7], None]
    assert values[0].message == "changed after the handoff was selected"
    assert env.client.calls == [[9], [7]]


@pytest.mark.asyncio
async def test_handoff_reference_has_no_token_body_or_renewed_freshness(env):
    previous = await env.donor()
    raw = json.dumps(previous[REFERENCE])
    assert "private-owner" not in raw and root().message not in raw
    loaded = await env.store.load_history_page(34, -1000000000042, key=previous[REFERENCE]["key"])
    oldest = loaded.data["metadata"]["observed_at"]
    context = await env.context(previous)
    await context.get_messages(env.client, ENTITY, [7])
    target = {"collected_at": (env.now+timedelta(minutes=1)).isoformat()}
    await publish_context(context, target)
    assert target[REFERENCE] == previous[REFERENCE]
    assert abs(datetime.fromisoformat(target["collected_at"]).replace(tzinfo=UTC).timestamp()-oldest) < 0.000001
    assert list(env.store.config.directory.iterdir()) == [env.store.path]


@pytest.mark.asyncio
async def test_publisher_lease_loss_or_store_full_never_creates_authorization(env):
    context = await env.context()
    await context.get_messages(env.client, ENTITY, [7])
    env.store.save_history_page = AsyncMock(return_value=Obj(ok=False, reason="capacity"))
    snapshot = {}
    await publish_context(context, snapshot)
    assert snapshot == {}
    env.wrapper.operation_lease = None
    env.store.save_history_page.reset_mock()
    await publish_context(context, snapshot)
    env.store.save_history_page.assert_not_awaited()
    assert snapshot == {}


@pytest.mark.asyncio
async def test_message_empty_and_missing_target_do_not_become_inherited_absence(env):
    env.client.messages[7] = Obj(id=7)  # Telegram MessageEmpty has no body/date/sender.
    env.client.messages.pop(8)
    context = await env.context()
    response = await context.get_messages(env.client, ENTITY, [7, 8])
    assert response == [env.client.messages[7], None]
    snapshot = {}
    await publish_context(context, snapshot)
    assert REFERENCE not in snapshot
    env.client.calls.clear()
    later = await env.context(snapshot)
    await later.get_messages(env.client, ENTITY, [7, 8])
    assert env.client.calls == [[7, 8]]


@pytest.mark.asyncio
async def test_actual_renewal_consumes_raw_event_but_refreshes_proof_and_permissions(env, live_auth):
    from app.modules.acquisition import qualification_actions as actions
    from tests.unit.test_qualification_actions import Client as RenewalClient

    row, _, db = live_auth
    client = RenewalClient()
    client._sender = Obj(_connection=Obj())
    client._message_box = Obj(seq=10, map={ENTRY_ACCOUNT: Obj(pts=20), 456: Obj(pts=30)},
                              getting_diff_for=set(), possible_gaps={})
    client.session = env.client.session
    client.history = [Obj(id=200, sender_id=777, sender=Obj(id=777, bot=False),
                         message="下午好", date=env.now, edit_date=None)]
    client.get_messages = AsyncMock(wraps=client.get_messages)
    client.get_permissions = AsyncMock(wraps=client.get_permissions)
    client.__call__ = AsyncMock(wraps=client.__call__)
    wrapper = Obj(client=client, operation_lease=Obj(account_id=34, token="first-owner"),
                  operation_lease_lost=False)
    first = ReviewFactOwner(account_id=34, wrapper=wrapper, store=env.store)
    await first.bind(client.entity, env.scope, {})
    await first.get_messages(client, client.entity, [101, 200])
    snapshot = json.loads(row.evidence_json)
    await publish_context(first, snapshot)
    row.evidence_json = json.dumps(snapshot)
    client.get_messages.reset_mock()
    wrapper.operation_lease = Obj(account_id=34, token="next-owner")
    next_context = ReviewFactOwner(account_id=34, wrapper=wrapper, store=env.store)
    await actions.refresh_live_authorization(
        db, client, 34, 456, recheck_rules=True, message_ids=[200], progress={},
        shared_read_context=next_context, handoff_scope=env.scope,
    )
    assert client.get_messages.await_count == 1
    assert client.get_messages.await_args.kwargs["ids"] == [101]
    assert any(call.args[1] == "me" for call in client.get_permissions.await_args_list)
    assert row.decision == "trial"
    assert next_context.inherited_used_at is not None
    client.send_message.assert_not_awaited()


def test_factory_and_phase_flags_default_to_legacy_path(env, monkeypatch):
    from app.modules.acquisition import review_read_flags as flags
    from app.modules.acquisition.qualification_read_context import QualificationReadContext

    monkeypatch.setattr(flags, "stage_enabled", lambda *_: False)
    context = create_context(34, env.wrapper)
    assert type(context) is QualificationReadContext and not context.enabled
    from app.core.config import settings
    assert settings.REVIEW_READ_FACT_HANDOFF is False
    assert settings.REVIEW_READ_DEPENDENCY_RESUME is False
