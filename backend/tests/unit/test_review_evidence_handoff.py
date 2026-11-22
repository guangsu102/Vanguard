"""Durable handoff must precede release and cannot manufacture qualification."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition import review_evidence_handoff as handoff
from app.modules.acquisition import review_read_runtime as runtime
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore
from tests.unit.test_qualification_service import setup


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = ReviewEvidenceStore(EvidenceStoreConfig(
        directory=tmp_path / "evidence", enabled=True,
        protected_slots=4, protected_slot_bytes=64 * 1024,
        protected_control_bytes=8 * 1024, protected_records_per_slot=64,
        protected_record_limit=256, slots_per_account=3, slots_per_account_group=2,
        ordinary_pages=4, ordinary_page_bytes=32 * 1024,
        ordinary_control_bytes=2 * 1024, ordinary_record_limit=100,
        ordinary_records_per_group=20, max_body_bytes=1024,
        physical_limit_bytes=2 * 1024 * 1024,
    ))
    assert store.initialize_sync().ok
    monkeypatch.setattr(runtime, "maintenance_store", lambda _account=None: store, raising=False)
    monkeypatch.setattr(runtime.settings, "REVIEW_READ_OPTIMIZATION_ACCOUNT_IDS", "2,3")
    handoff._last_run.clear()
    handoff._last_sweep.clear()
    handoff._running.clear()
    return store


@pytest.fixture
def transactions(test_db, monkeypatch):
    """A genuinely separate session and commit, rather than a shared flush."""
    factory = async_sessionmaker(test_db.bind, expire_on_commit=False)
    commits = []

    @asynccontextmanager
    async def session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
                commits.append(True)
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr(handoff, "get_db_session", session)
    return factory, commits


def admit(store, member, *, age=32, message_id=999, item=None):
    now = datetime.utcnow()
    date = now - timedelta(hours=age)
    item = item or {
        "message_id": message_id, "text": "优惠出售服务联系 https://seller.example",
        "sender_id": 501, "date": date.isoformat(), "edited_at": None,
        "source": "recent_promotional_message", "sender_role": "ordinary",
        "age_hours": age, "accessible": True, "warning_search_complete": True,
        "warning_reply_ids": [],
    }
    version = runtime.content_version(item["text"], sender_id=item["sender_id"],
                                      date=item["date"], edit_date=item["edited_at"])
    result = store.admit_observation_sync(
        account_id=member.account_id, group_id=member.telegram_group_id,
        message_id=item["message_id"], version=version, joined_at=member.joined_at,
        date=item["date"], edit_date=item["edited_at"],
        now=date + timedelta(hours=1), metadata={"membership_id": member.id},
        records=[{"key": "retained", "body": item["text"]}],
    )
    assert result.ok, result
    return result, item


async def baseline(db, *, decision="unknown"):
    account, group, member, audit = await setup(db, decision=decision)
    if decision not in {"allowed", "trial"}:
        member.review_status, member.ad_status = "review_2h", "blocked"
    await db.commit()
    return account, group, member, audit


@pytest.mark.asyncio
async def test_unknown_timeout_commits_original_queue_and_minimal_location_before_ack(
    test_db, store, transactions, monkeypatch,
):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member)
    factory, commits = transactions
    original_ack = store.ack_handoff

    async def verify_commit(handle, token):
        assert commits
        async with factory() as db:
            saved = await db.get(GroupQualificationAudit, audit.id)
            hint = await db.get(SystemSetting, f"qualification.callback_hints.{member.id}")
            assert saved.state == "queued" and saved.decision == "unknown"
            anchor = json.loads(saved.evidence_json)["evidence_handoffs"][-1]
            assert anchor["outcome"] == "unknown" and anchor["message_id"] == 999
            assert "text" not in anchor and "body" not in anchor and hint is not None
        return await original_ack(handle, token)

    monkeypatch.setattr(store, "ack_handoff", verify_commit)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["unknown"] == 1 and result["failed"] == 0
    assert store.get_observation_sync(observed.handle).reason == "observation_missing"
    await test_db.refresh(member)
    assert (member.review_status, member.ad_status) == ("review_2h", "blocked")


@pytest.mark.asyncio
async def test_commit_failure_keeps_body_and_rolls_back_recovery_cue(test_db, store, monkeypatch):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member)
    factory = async_sessionmaker(test_db.bind, expire_on_commit=False)

    @asynccontextmanager
    async def failing_transaction():
        async with factory() as db:
            try:
                yield db
                await db.flush()  # A flush is specifically insufficient.
                raise RuntimeError("commit failed")
            finally:
                await db.rollback()

    monkeypatch.setattr(handoff, "get_db_session", failing_transaction)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["failed"] == 1 and result["unknown"] == 0
    retained = store.get_observation_sync(observed.handle)
    assert retained.ok and retained.data["records"][0]["body"]
    async with factory() as db:
        saved = await db.get(GroupQualificationAudit, audit.id)
        assert "evidence_handoffs" not in json.loads(saved.evidence_json)
        assert await db.get(SystemSetting, f"qualification.callback_hints.{member.id}") is None


@pytest.mark.asyncio
async def test_new_control_after_commit_prevents_old_token_release(test_db, store, transactions, monkeypatch):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member)
    original_ack = store.ack_handoff

    async def interleaved_control(handle, token):
        assert transactions[1]
        assert store.record_control_sync(handle, "warning", source_id=123).ok
        return await original_ack(handle, token)

    monkeypatch.setattr(store, "ack_handoff", interleaved_control)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["deferred"] == 1
    assert store.get_observation_sync(observed.handle).ok
    monkeypatch.setattr(store, "ack_handoff", original_ack)
    result = await handoff.maintain_evidence_store(account_id=2, force=True)
    assert result["unknown"] == 1
    await test_db.refresh(audit)
    anchor = json.loads(audit.evidence_json)["evidence_handoffs"][-1]
    assert anchor["facts"] == ["warning"]


@pytest.mark.parametrize("decision", ["allowed", "trial"])
@pytest.mark.asyncio
async def test_unrelated_existing_grant_is_not_requeued(test_db, store, transactions, decision):
    _, _, member, audit = await baseline(test_db, decision=decision)
    future = datetime.utcnow() + timedelta(days=3)
    audit.next_retry_at = member.review_next_at = future
    await test_db.commit()
    admit(store, member, message_id=999)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["unknown"] == 1
    await test_db.refresh(audit)
    await test_db.refresh(member)
    assert (audit.state, audit.decision, audit.next_retry_at) == ("completed", decision, future)
    assert (member.review_status, member.ad_status, member.review_next_at) == ("approved", "active", future)
    assert await test_db.get(SystemSetting, f"qualification.event.{member.id}") is None


@pytest.mark.asyncio
async def test_same_committed_trial_releases_verified_without_new_grant(test_db, store, transactions):
    _, _, member, audit = await baseline(test_db, decision="trial")
    observed, item = admit(store, member, age=25, message_id=102)
    saved = json.loads(audit.evidence_json)
    # Include another proof first: trial_proof returns only one item per call.
    saved["evidence"].append(item)
    saved["collected_at"] = datetime.utcnow().isoformat()
    audit.evidence_json = json.dumps(saved)
    await test_db.commit()
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["verified"] == 1 and result["unknown"] == 0
    assert not store.get_observation_sync(observed.handle).ok
    await test_db.refresh(audit)
    assert (audit.state, audit.decision) == ("completed", "trial")


@pytest.mark.asyncio
async def test_prepared_verified_cannot_replace_an_uncommitted_business_result(test_db, store, transactions):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member, age=25)
    prepared = store.prepare_handoff_sync(observed.handle, "verified")
    assert prepared.ok
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["deferred"] == 1 and result["verified"] == 0
    assert store.get_observation_sync(observed.handle).ok
    await test_db.refresh(audit)
    assert audit.decision == "unknown"


@pytest.mark.asyncio
async def test_listener_marked_peer_matches_legacy_raw_membership(test_db, store, transactions):
    _, _, member, audit = await baseline(test_db)
    marked = -1_000_000_000_000 - member.telegram_group_id
    original_peer = member.telegram_group_id
    member.telegram_group_id = marked
    observed, _ = admit(store, member)
    member.telegram_group_id = original_peer
    await test_db.commit()
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["unknown"] == 1 and result["not_applicable"] == 0
    assert not store.get_observation_sync(observed.handle).ok


@pytest.mark.parametrize("change", ["collected_before_ready", "changed_version", "warning"])
@pytest.mark.asyncio
async def test_trial_release_requires_exact_mature_clear_committed_evidence(
    test_db, store, transactions, change,
):
    _, _, member, audit = await baseline(test_db, decision="trial")
    observed, item = admit(store, member, age=25, message_id=102)
    saved = json.loads(audit.evidence_json)
    if change == "changed_version":
        item["text"] += " changed"
    saved["evidence"] = [item]
    saved["collected_at"] = (
        datetime.utcnow() - timedelta(hours=2) if change == "collected_before_ready" else datetime.utcnow()
    ).isoformat()
    audit.evidence_json = json.dumps(saved)
    await test_db.commit()
    if change == "warning":
        assert store.record_control_sync(observed.handle, "warning", source_id=400).ok
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["verified"] == 0
    assert result["unknown"] == (1 if change == "warning" else 0)
    if change != "warning":
        assert store.get_observation_sync(observed.handle).ok
    else:
        assert await test_db.get(SystemSetting, f"qualification.event.{member.id}") is not None


@pytest.mark.parametrize("hold", ["paused", "manual", "exit", "protected", "rollout", "risk"])
@pytest.mark.asyncio
async def test_unknown_handoff_does_not_wake_paused_protected_or_exiting_scope(
    test_db, store, transactions, hold,
):
    account, _, member, audit = await baseline(test_db)
    future = datetime.utcnow() + timedelta(hours=3)
    audit.next_retry_at = member.review_next_at = future
    if hold == "paused":
        member.ad_status = "paused"
    elif hold == "manual":
        member.review_status = "manual_required"
    elif hold == "exit":
        member.review_status = "exit_pending"
    elif hold == "risk":
        account.risk_pause_until = future
    else:
        setting = await test_db.get(SystemSetting, qualification.SETTING_KEY)
        config = json.loads(setting.value)
        if hold == "protected":
            config["protected_membership_ids"] = [member.id]
        else:
            config["rollout_accounts"]["2"]["phase"] = "paused"
        setting.value = json.dumps(config)
    await test_db.commit()
    before = member.review_status, member.ad_status
    admit(store, member)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["unknown"] == 1
    await test_db.refresh(audit)
    await test_db.refresh(member)
    assert audit.state == "completed" and audit.next_retry_at == future
    assert (member.review_status, member.ad_status) == before and member.review_next_at == future


@pytest.mark.parametrize("ended", ["left", "rejoined"])
@pytest.mark.asyncio
async def test_scope_ended_is_not_applicable_and_cannot_wake_current_scope(
    test_db, store, transactions, ended,
):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member)
    future = datetime.utcnow() + timedelta(days=1)
    if ended == "left":
        member.status, member.ad_status = "left", "blocked"
        member.left_at = datetime.utcnow()
    else:
        member.joined_at = datetime.utcnow()
    member.review_next_at = future
    await test_db.commit()
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["not_applicable"] == 1
    assert not store.get_observation_sync(observed.handle).ok
    await test_db.refresh(member)
    await test_db.refresh(audit)
    assert member.review_next_at == future
    assert json.loads(audit.evidence_json)["evidence_handoffs"][-1]["outcome"] == "not_applicable"
    assert await test_db.get(SystemSetting, f"qualification.callback_hints.{member.id}") is None


@pytest.mark.parametrize("fact", ["edited", "deleted"])
@pytest.mark.asyncio
async def test_concrete_invalidations_handoff_early_without_rejecting(test_db, store, transactions, fact):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member, age=2)
    assert store.record_control_sync(observed.handle, fact, source_id=999).ok
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result[fact] == 1 and not store.get_observation_sync(observed.handle).ok
    await test_db.refresh(audit)
    assert audit.decision == "unknown" and audit.state == "queued"


@pytest.mark.parametrize("fact", ["gap", "warning", "unknown"])
@pytest.mark.asyncio
async def test_immature_unknown_waits_across_entries_without_rejections_or_commits(
    test_db, store, transactions, fact,
):
    _, _, member, _ = await baseline(test_db)
    observed, _ = admit(store, member, age=2)
    assert store.record_control_sync(observed.handle, fact).ok
    retained = store.get_observation_sync(observed.handle).data
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["observing"] == 1 and len(transactions[1]) == 1
    generation = store.stats_sync().data["generation"]
    # Another process/entry does not share these in-memory cadence variables.
    for _ in range(3):
        handoff._last_run.clear()
        handoff._last_sweep.clear()
        result = await handoff.maintain_evidence_store(account_id=2)
        assert result["maintenance_waiting"] == 1
    assert len(transactions[1]) == 1
    assert store.stats_sync().data["generation"] == generation
    assert store.stats_sync().data["lifecycle_counters"]["rejected_early_handoffs"] == 0
    assert store.get_observation_sync(observed.handle).data == retained
    ready = handoff._date(retained["control"]["ready_at"])
    assert await handoff._transfer(store, observed.handle, ready) == "unknown"
    assert not store.get_observation_sync(observed.handle).ok


@pytest.mark.parametrize("change", ["edited", "deleted", "not_applicable"])
@pytest.mark.asyncio
async def test_control_change_after_wait_keeps_early_legal_handoff(
    test_db, store, transactions, change,
):
    _, _, member, _ = await baseline(test_db)
    observed, _ = admit(store, member, age=2)
    assert store.record_control_sync(observed.handle, "gap").ok
    assert (await handoff.maintain_evidence_store(account_id=2))["observing"] == 1
    if change == "not_applicable":
        member.status, member.left_at = "left", datetime.utcnow()
        await test_db.commit()
    assert store.record_control_sync(observed.handle, change).ok
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result[change] == 1
    assert not store.get_observation_sync(observed.handle).ok
    counters = store.stats_sync().data["lifecycle_counters"]
    assert counters["legal_early_releases"] == 1 and counters["rejected_early_handoffs"] == 0


@pytest.mark.asyncio
async def test_deadline_bypasses_local_sweep_and_account_cadence(test_db, store, transactions, monkeypatch):
    from app.modules.acquisition import review_evidence_store as evidence

    _, _, member, _ = await baseline(test_db)
    observed, _ = admit(store, member, age=25)
    control = store.get_observation_sync(observed.handle).data["control"]
    clock = [control["deadline_at"] - 1]

    class ClockDate(datetime):
        @classmethod
        def utcnow(cls):
            return datetime.utcfromtimestamp(clock[0])

    monkeypatch.setattr(handoff, "datetime", ClockDate)
    monkeypatch.setattr(handoff.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(evidence.time, "time", lambda: clock[0])
    # Skip the unrelated maturity cue here: the deadline itself must still
    # prepare and durably transfer UNKNOWN even within the same local cadence.
    assert store.mark_maturity_scheduled_sync(observed.handle, now=clock[0]).ok
    assert (await handoff.maintain_evidence_store(account_id=2))["observing"] == 1
    clock[0] += 1
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["swept"] == 1 and result["unknown"] == 1
    assert store.stats_sync().data["lifecycle_counters"]["deadline_unknown"] == 1
    assert not store.get_observation_sync(observed.handle).ok


@pytest.mark.asyncio
async def test_missing_audit_uses_original_idempotent_review_queue(test_db, store, transactions):
    _, _, member, audit = await baseline(test_db)
    await test_db.delete(audit)
    await test_db.commit()
    admit(store, member, message_id=999)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["unknown"] == 1
    await test_db.refresh(member)
    current = await qualification.latest_review(test_db, member.id, include_pending=True)
    assert current.state == "queued" and current.batch_id.startswith("evidence-v5:")
    assert member.review_status == "initial_pending" and member.ad_status == "blocked"


@pytest.mark.asyncio
async def test_budget_wait_is_preserved_by_timeout(test_db, store, transactions):
    from app.modules.acquisition.review_waits import record

    _, _, member, audit = await baseline(test_db)
    future = datetime.utcnow() + timedelta(minutes=30)
    audit.next_retry_at = member.review_next_at = future
    await record(test_db, audit.id, "telegram_read_budget", 1, future, datetime.utcnow())
    await test_db.commit()
    admit(store, member)
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["unknown"] == 1
    await test_db.refresh(audit)
    assert audit.next_retry_at == future


@pytest.mark.asyncio
async def test_process_throttle_is_account_scoped_and_force_bypasses(store):
    first = await handoff.maintain_evidence_store(account_id=2)
    again = await handoff.maintain_evidence_store(account_id=2)
    other_account = await handoff.maintain_evidence_store(account_id=3)
    forced = await handoff.maintain_evidence_store(account_id=2, force=True)
    assert first["swept"] == 1 and again["throttled"] == 1
    assert other_account["throttled"] == 0 and other_account["swept"] == 0
    assert forced["swept"] == 1


@pytest.mark.asyncio
async def test_mismatched_audit_scope_cannot_wake_or_release(test_db, store, transactions):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member)
    audit.content_scope = "other"
    await test_db.commit()
    result = await handoff.maintain_evidence_store(account_id=2)
    assert result["deferred"] == 1 and store.get_observation_sync(observed.handle).ok


def test_terminal_scope_replaces_prepared_unknown_token(store):
    from types import SimpleNamespace

    member = SimpleNamespace(id=60, account_id=2, telegram_group_id=1234,
                             joined_at=datetime.utcnow() - timedelta(days=5))
    observed, _ = admit(store, member)
    store.sweep_sync()
    old = store.get_observation_sync(observed.handle).data["control"]["handoff"]
    assert store.record_control_sync(observed.handle, "not_applicable").ok
    final = store.prepare_handoff_sync(observed.handle, "not_applicable")
    assert final.ok and final.data["outcome"] == "not_applicable"
    assert not store.ack_handoff_sync(observed.handle, old["token"]).ok
    assert store.ack_handoff_sync(observed.handle, final.data["token"]).ok


@pytest.mark.asyncio
async def test_listener_only_candidate_wakes_once_at_ready_and_keeps_protected_body(test_db, store, transactions):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member, age=23, message_id=999)
    control = store.get_observation_sync(observed.handle).data["control"]
    ready = handoff._date(control["ready_at"])
    assert await handoff._transfer(store, observed.handle, ready - timedelta(seconds=1)) == "observing"
    await test_db.refresh(audit)
    assert audit.state == "completed" and audit.next_retry_at is None
    assert await handoff._transfer(store, observed.handle, ready) == "maturity_scheduled"
    await test_db.refresh(audit)
    await test_db.refresh(member)
    assert audit.state == "queued" and audit.next_retry_at == member.review_next_at == ready
    cue = json.loads(audit.evidence_json)["evidence_maturity_cues"][-1]
    assert cue["message_id"] == 999 and cue["scheduled_at"] == ready.isoformat()
    assert "text" not in cue and store.get_observation_sync(observed.handle).data["records"]
    assert await handoff._transfer(store, observed.handle, ready + timedelta(minutes=5)) == "observing"
    assert store.stats_sync().data["lifecycle_counters"]["maturity_scheduled"] == 1


@pytest.mark.asyncio
async def test_maturity_does_not_preempt_budget_retry_and_wakes_when_retry_is_due(test_db, store, transactions):
    from app.modules.acquisition.review_waits import record

    _, _, member, audit = await baseline(test_db)
    future = datetime.utcnow() + timedelta(minutes=30)
    audit.next_retry_at = member.review_next_at = future
    await record(test_db, audit.id, "telegram_read_budget", 12, future, datetime.utcnow())
    await test_db.commit()
    observed, _ = admit(store, member, age=25, message_id=999)
    result = await handoff.maintain_evidence_store(account_id=2, force=True)
    assert result["maturity_waiting"] == 1
    await test_db.refresh(audit)
    assert audit.state == "completed" and audit.next_retry_at == future
    assert store.get_observation_sync(observed.handle).data["control"].get("maturity_scheduled_at") is None
    assert await handoff._transfer(store, observed.handle, future) == "maturity_scheduled"
    await test_db.refresh(audit)
    assert audit.state == "queued" and audit.next_retry_at == future


@pytest.mark.asyncio
async def test_maturity_commit_failure_cannot_ack_schedule_or_release_payload(test_db, store, monkeypatch):
    _, _, member, audit = await baseline(test_db)
    observed, _ = admit(store, member, age=25, message_id=999)
    factory = async_sessionmaker(test_db.bind, expire_on_commit=False)

    @asynccontextmanager
    async def failing_transaction():
        async with factory() as db:
            try:
                yield db
                await db.flush()
                raise RuntimeError("commit failed")
            finally:
                await db.rollback()

    monkeypatch.setattr(handoff, "get_db_session", failing_transaction)
    result = await handoff.maintain_evidence_store(account_id=2, force=True)
    assert result["failed"] == 1
    kept = store.get_observation_sync(observed.handle).data
    assert kept["records"] and kept["control"].get("maturity_scheduled_at") is None
    async with factory() as db:
        saved = await db.get(GroupQualificationAudit, audit.id)
        assert saved.state == "completed" and saved.next_retry_at is None
        assert "evidence_maturity_cues" not in json.loads(saved.evidence_json)
