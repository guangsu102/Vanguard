"""Capacity, handoff, process concurrency and crash contracts for bounded cache."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.acquisition import review_evidence_store as evidence
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig, ReviewEvidenceStore


@pytest.fixture
def config(tmp_path):
    return EvidenceStoreConfig(
        directory=tmp_path / "evidence", enabled=True,
        protected_slots=4, protected_slot_bytes=64 * 1024,
        protected_control_bytes=8 * 1024, protected_records_per_slot=64,
        protected_record_limit=256, slots_per_account=3, slots_per_account_group=2,
        ordinary_pages=4, ordinary_page_bytes=32 * 1024,
        ordinary_control_bytes=2 * 1024, ordinary_record_limit=100,
        ordinary_records_per_group=20, max_body_bytes=1024,
        physical_limit_bytes=2 * 1024 * 1024,
    )


@pytest.fixture
def store(config):
    result = ReviewEvidenceStore(config)
    assert result.initialize_sync().ok
    return result


def observation(store, *, account=2, group=-1001, message=10, version="v1", now=None, **kwargs):
    now = now or datetime.now(UTC)
    return store.admit_observation_sync(
        account_id=account, group_id=group, message_id=message, version=version,
        joined_at=now - timedelta(days=2), date=now - timedelta(hours=1), now=now,
        records=kwargs.pop("records", [{"key": "post", "body": "offer contact @person"}]),
        **kwargs,
    )


def test_disabled_and_invalid_geometry_do_not_create_store(config):
    disabled = ReviewEvidenceStore(replace(config, enabled=False))
    assert disabled.initialize_sync().reason == "evidence_store_disabled"
    assert not config.directory.exists()
    invalid = ReviewEvidenceStore(replace(config, physical_limit_bytes=100))
    assert invalid.initialize_sync().reason == "physical_layout_exceeds_limit"
    assert not config.directory.exists()


def test_production_geometry_is_fixed_and_includes_double_banks(tmp_path):
    config = EvidenceStoreConfig(directory=tmp_path)
    config.validate()
    assert config.protected_limit_bytes == 192 * evidence.MIB
    assert config.ordinary_limit_bytes == 64 * evidence.MIB
    assert config.protected_slots * config.protected_records_per_slot == 49152
    assert 512 * evidence.MIB < config.physical_size_bytes < 1024 * evidence.MIB


def test_hard_ceilings_cannot_be_increased_through_configuration(config):
    too_many = ReviewEvidenceStore(replace(config, protected_slots=97))
    assert too_many.initialize_sync().reason == "store_configuration_exceeds_hard_ceiling"
    assert not config.directory.exists()


@pytest.mark.asyncio
async def test_sync_capture_async_lookup_preserves_visibility_and_account_isolation(store):
    now = datetime.now(UTC)
    metadata = {"date": now.isoformat(), "edit_date": None, "from_id": 123,
                "reply_id": 4, "topic": None, "media": False, "replies": 0,
                "visibility": "account2", "joined_at": "join-v1", "pts": 12}
    result = store.capture_sync(account_id=2, group_id=-1001, key="10:v1", body="广告正文",
                                metadata=metadata, event_at=now)
    assert result.ok
    hit = await store.get_cache(2, -1001, "10:v1")
    assert hit.ok and hit.data["body"] == "广告正文"
    assert hit.data["metadata"] == metadata and hit.data["coverage"] == "unknown"
    assert not (await store.get_cache(33, -1001, "10:v1")).ok
    assert not (await store.get_cache(2, -1002, "10:v1")).ok
    stats = await store.stats()
    assert stats.data["physical_file_size"] == os.stat(store.path).st_size
    assert stats.data["disabled_reason"] is None


def test_reads_and_replays_do_not_extend_ttl(store, monkeypatch):
    monkeypatch.setattr(evidence.time, "time", lambda: 10_000.0)
    kwargs = {"account_id": 2, "group_id": -1001, "key": "10:v1", "body": "x", "event_at": 9990, "ttl_seconds": 20}
    assert store.capture_sync(**kwargs).ok
    monkeypatch.setattr(evidence.time, "time", lambda: 10_009.0)
    assert store.capture_sync(**kwargs).reason == "cache_replay"
    assert store.get_cache_sync(2, -1001, "10:v1").data["expires_at"] == 10_010
    monkeypatch.setattr(evidence.time, "time", lambda: 10_011.0)
    assert not store.get_cache_sync(2, -1001, "10:v1").ok
    assert store.capture_sync(account_id=2, group_id=-1, key="future", event_at=20_000).reason == "cache_event_from_future"


def test_utf8_body_limit_and_version_collision_are_not_truncation(store):
    now = datetime.now(UTC)
    too_big = store.capture_sync(account_id=2, group_id=-1001, key="huge", body="汉" * 342)
    assert too_big.reason == "body_too_large_or_not_text"
    assert store.stats_sync().data["ordinary_records"] == 0
    args = {"account_id": 2, "group_id": -1001, "key": "10:v1", "event_at": now}
    assert store.capture_sync(**args, body="original").ok
    assert store.capture_sync(**args, body="changed").reason == "cache_key_version_collision"
    assert store.get_cache_sync(2, -1001, "10:v1").data["body"] == "original"


def test_group_record_limit_evicts_only_ordinary_with_unknown_coverage(config):
    store = ReviewEvidenceStore(replace(config, ordinary_records_per_group=3))
    now = datetime.now(UTC)
    for number in range(5):
        assert store.capture_sync(account_id=2, group_id=-1001, key=str(number),
                                  body="x", event_at=now + timedelta(microseconds=number)).ok
    assert not store.get_cache_sync(2, -1001, "0").ok
    assert store.get_cache_sync(2, -1001, "4").data["coverage"] == "unknown"
    assert store.stats_sync().data["ordinary_records"] == 3


def test_global_ordinary_record_reservation_never_borrows_protected(config):
    store = ReviewEvidenceStore(replace(config, ordinary_record_limit=3))
    for number in range(3):
        store.capture_sync(account_id=2, group_id=-1001, key=str(number), body="x")
    assert store.stats_sync().data["ordinary_records"] <= 3
    admitted = observation(store)
    assert admitted.ok
    assert store.stats_sync().data["protected_reserved_records"] == 64


@pytest.mark.parametrize(
    ("expired_per_group", "expected_evictions", "expected_records"),
    [(18, 0, 10), (1, 1, 76)],
    ids=["expiry_removes_pressure", "remaining_records_still_require_eviction"],
)
def test_sweep_uses_post_expiry_counts_for_pressure(
    config, monkeypatch, expired_per_group, expected_evictions, expected_records,
):
    now = 1_000_000.0
    monkeypatch.setattr(evidence.time, "time", lambda: now)
    config = replace(config, ordinary_pages=8)
    store = ReviewEvidenceStore(config)
    protected = observation(store, now=datetime.fromtimestamp(now, UTC))
    assert protected.ok
    protected_before = store.get_observation_sync(protected.handle).data
    size = store.path.stat().st_size
    for group in range(1, 6):
        for number in range(expired_per_group):
            assert store.capture_sync(
                account_id=2, group_id=-group, key=f"old-{number}", body="old",
                event_at=now - 20, ttl_seconds=30,
            ).ok
        for number in range(19 - expired_per_group):
            assert store.capture_sync(
                account_id=2, group_id=-group, key=f"fresh-{number}", body="keep",
                event_at=now,
            ).ok
    assert store.stats_sync().data["ordinary_records"] == 100

    commits = []
    original_commit = store._commit

    def tracked_commit(fd, index, changes):
        commits.append([protected for protected, *_ in changes])
        return original_commit(fd, index, changes)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_commit", tracked_commit)
        swept = store.sweep_sync(now=now + 11)
    assert swept.ok
    assert len(commits) == 1 and not any(commits[0])
    assert swept.data["expired_ordinary_records"] == expired_per_group * 5
    assert len(swept.data["evicted_groups"]) == expected_evictions

    reopened = ReviewEvidenceStore(config)
    stats = reopened.stats_sync().data
    assert stats["ordinary_pages_used"] == 5 - expected_evictions
    assert stats["ordinary_records"] == expected_records
    evicted = {int(row["group_id"]) for row in swept.data["evicted_groups"]}
    for group in range(1, 6):
        assert not reopened.get_cache_sync(2, -group, "old-0").ok
        for number in range(19 - expired_per_group):
            cached = reopened.get_cache_sync(2, -group, f"fresh-{number}")
            assert cached.ok == (-group not in evicted)
            if cached.ok:
                assert cached.data["body"] == "keep"
    assert reopened.get_observation_sync(protected.handle).data == protected_before
    assert store.path.stat().st_size == size


def test_protected_group_account_and_global_limits_are_independent(store, config):
    assert observation(store, group=-1, message=1).ok
    assert observation(store, group=-1, message=2).ok
    assert observation(store, group=-1, message=3).reason == "protected_account_group_capacity"
    assert observation(store, group=-2, message=3).ok
    assert observation(store, group=-3, message=4).reason == "protected_account_capacity"
    assert observation(store, account=33, group=-3, message=4).ok
    assert observation(store, account=34, group=-4, message=5).reason == "protected_capacity"
    stats = store.stats_sync().data
    assert stats["protected_reserved_bytes"] == 4 * config.protected_slot_bytes
    assert stats["protected_reserved_records"] == 256


def test_before_maturity_ttl_pressure_and_sweep_cannot_delete_protection(store):
    now = datetime.now(UTC)
    admitted = observation(store, now=now)
    handle = admitted.handle
    original_size = store.path.stat().st_size
    for group in range(15):
        assert store.capture_sync(account_id=2, group_id=group, key="a", body="x").ok
    swept = store.sweep_sync(now=now + timedelta(hours=22, minutes=59))
    assert swept.ok and swept.data["handoffs"] == []
    assert store.get_observation_sync(handle).data["records"][0]["body"] == "offer contact @person"
    assert store.prepare_handoff_sync(handle, "verified", now=now).reason == "observation_not_mature"
    assert store.ack_handoff_sync(handle, "guessed").reason == "handoff_token_mismatch"
    assert store.path.stat().st_size == original_size


def test_deadline_unknown_survives_restart_until_durable_handoff_ack(store, config):
    now = datetime.now(UTC)
    admitted = observation(store, now=now)
    deadline = admitted.data["deadline_at"]
    swept = store.sweep_sync(now=deadline + 1)
    item = swept.data["handoffs"][0]
    assert item["outcome"] == "unknown"
    reopened = ReviewEvidenceStore(config)
    assert reopened.get_observation_sync(admitted.handle).ok
    assert reopened.stats_sync().data["pending_handoffs"] == 1
    assert reopened.ack_handoff_sync(admitted.handle, item["token"]).ok
    assert not reopened.get_observation_sync(admitted.handle).ok
    assert reopened.stats_sync().data["protected_reserved_bytes"] == 0


def test_admitting_already_mature_evidence_gets_six_hours_from_admission(store):
    now = datetime.now(UTC)
    result = store.admit_observation_sync(
        account_id=2, group_id=-1, message_id=10, version="mature", now=now,
        date=now - timedelta(hours=30), joined_at=now - timedelta(days=3),
        records=[{"key": "post", "body": "offer"}],
    )
    assert result.ok
    assert result.data["deadline_at"] == now.timestamp() + 6 * 3600


def test_full_payload_does_not_take_reserved_warning_and_invalidation_space(store):
    records = [{"key": str(i), "body": "x" * 1024} for i in range(45)]
    admitted = observation(store, records=records)
    assert admitted.ok
    assert store.record_control_sync(admitted.handle, "warning", source_id=999).ok
    assert store.record_control_sync(admitted.handle, "gap").ok
    control = store.get_observation_sync(admitted.handle).data["control"]
    assert set(control["facts"]) == {"warning", "gap"}
    assert control["state"] == "needs_evidence"


def test_warning_racing_after_prepare_invalidates_old_success_ack(store):
    now = datetime.now(UTC)
    admitted = observation(store, now=now)
    ready = admitted.data["ready_at"]
    prepared = store.prepare_handoff_sync(admitted.handle, "verified", now=ready + 1)
    assert prepared.ok
    assert store.record_control_sync(admitted.handle, "warning", source_id=9).ok
    assert store.ack_handoff_sync(admitted.handle, prepared.data["token"]).reason == "handoff_token_mismatch"
    new = store.get_observation_sync(admitted.handle).data["control"]["handoff"]
    assert new["outcome"] == "unknown"


def test_edit_requires_new_version_admission_and_new_24_hour_clock(store):
    now = datetime.now(UTC)
    args = {"account_id": 2, "group_id": -1001, "message_id": 10, "joined_at": now - timedelta(days=2),
                "date": now - timedelta(hours=1), "records": [{"key": "post", "body": "old"}], "now": now}
    original = store.admit_observation_sync(version="old", **args)
    replay = store.admit_observation_sync(version="old", **{**args, "now": now + timedelta(hours=1)})
    assert replay.handle == original.handle
    assert store.admit_observation_sync(version="new", **args).reason == "previous_version_requires_invalidation"
    assert store.record_control_sync(original.handle, "edited", source_id=10).ok
    changed = store.admit_observation_sync(version="new", edit_date=now, **args)
    assert changed.ok and changed.data["ready_at"] == now.timestamp() + evidence.DAY
    assert changed.data["ready_at"] > original.data["ready_at"]
    assert store.get_observation_sync(original.handle).data["control"]["state"] == "invalidated"


def test_list_lookup_preserve_join_scope_and_no_body_enumeration(store):
    now = datetime.now(UTC)
    admitted = observation(store, now=now)
    joined = now - timedelta(days=2)
    found = store.lookup_observation_sync(account_id=2, group_id=-1001, message_id=10, joined_at=joined)
    assert found.handle == admitted.handle
    assert not store.lookup_observation_sync(account_id=2, group_id=-1001, message_id=10,
                                             joined_at=joined + timedelta(seconds=1)).ok
    rows = store.list_observations_sync(account_id=2).data["observations"]
    assert len(rows) == 1 and "records" not in rows[0]


def test_interrupted_inactive_page_write_does_not_publish_partial_data(store, config, monkeypatch):
    protected = observation(store)
    assert store.capture_sync(account_id=2, group_id=-1, key="old", body="old").ok
    original = store._write_index
    monkeypatch.setattr(store, "_write_index", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
    assert not store.capture_sync(account_id=2, group_id=-1, key="new", body="new").ok
    monkeypatch.setattr(store, "_write_index", original)
    reopened = ReviewEvidenceStore(config)
    assert not reopened.get_cache_sync(2, -1, "old").ok
    assert not reopened.get_cache_sync(2, -1, "new").ok
    recovered = reopened.get_observation_sync(protected.handle)
    assert recovered.ok and recovered.data["records"][0]["body"] == "offer contact @person"
    assert recovered.data["control"]["state"] == "needs_evidence"
    assert "gap" in recovered.data["control"]["facts"]
    assert reopened.capture_sync(account_id=2, group_id=-1, key="new", body="new").ok


def test_lost_committed_index_never_silently_resurrects_old_facts(store):
    assert store.capture_sync(account_id=2, group_id=-1, key="old", body="old").ok
    assert store.capture_sync(account_id=2, group_id=-1, key="new", body="new").ok
    with store.path.open("r+b") as stream:
        stream.seek(evidence.GUARD_BYTES)  # generation 2 is bank 0
        stream.write(b"broken!!")
        stream.flush()
        os.fsync(stream.fileno())
    assert store.get_cache_sync(2, -1, "old").reason == "committed_index_lost"
    assert store.get_cache_sync(2, -1, "new").reason == "committed_index_lost"


def test_corrupt_payload_never_becomes_empty_or_complete_evidence(store):
    admitted = observation(store)
    offset, _size, control = store._page_geometry(True, 0, 0)
    with store.path.open("r+b") as stream:
        stream.seek(offset + control)
        stream.write(b"broken")
    result = store.get_observation_sync(admitted.handle)
    assert not result.ok and result.reason == "evidence_page_corrupt"


def test_unknown_directory_file_and_geometry_change_disable_without_deleting(store, config):
    admitted = observation(store)
    original_size = store.path.stat().st_size
    other = config.directory / "unexpected.wal"
    other.write_bytes(b"unexpected")
    stats = store.stats_sync()
    assert not stats.ok and stats.data["disabled_reason"] == "unverified_files_in_store_directory"
    other.unlink()
    changed = ReviewEvidenceStore(replace(config, ordinary_pages=5))
    assert changed.initialize_sync().reason == "store_size_mismatch"
    assert store.path.stat().st_size == original_size
    assert store.get_observation_sync(admitted.handle).ok


def test_dependencies_share_reserved_slot_and_survive_ordinary_pressure(store, config):
    admitted = observation(store)
    dependency = {"key": "reply:11:v1", "body": "必要回复正文", "metadata": {"reply_to_msg_id": 10}}
    assert store.append_dependencies_sync(admitted.handle, [dependency]).ok
    assert store.append_dependencies_sync(admitted.handle, [dependency]).reason == "dependencies_replay"
    size = store.path.stat().st_size
    for group in range(10):
        assert store.capture_sync(account_id=2, group_id=group, key="pressure", body="x").ok
    assert store.sweep_sync(now=admitted.data["ready_at"] - 1).ok
    reopened = ReviewEvidenceStore(config)
    payload = reopened.get_observation_sync(admitted.handle).data
    assert {record["body"] for record in payload["records"]} == {"offer contact @person", "必要回复正文"}
    stats = reopened.stats_sync().data
    assert stats["protected_reserved_bytes"] == config.protected_slot_bytes
    assert stats["lifecycle_counters"]["dependencies_added"] == 1
    assert stats["lifecycle_counters"]["illegal_early_releases"] == 0
    assert store.path.stat().st_size == size


def test_dependency_overflow_marks_gap_without_losing_existing_body_or_control(store):
    admitted = observation(store)
    records = [{"key": str(i), "body": "x" * 1024} for i in range(64)]
    rejected = store.append_dependencies_sync(admitted.handle, records)
    assert rejected.reason == "dependency_capacity"
    kept = store.get_observation_sync(admitted.handle).data
    assert len(kept["records"]) == 1 and kept["records"][0]["body"]
    assert kept["control"]["facts"]["gap"]["reason"] == "dependency_capacity"
    assert store.record_control_sync(admitted.handle, "warning", source_id=9).ok
    assert "warning" in store.get_observation_sync(admitted.handle).data["control"]["facts"]
    assert store.stats_sync().data["lifecycle_counters"]["dependency_overflows"] == 1


def test_lifecycle_counters_are_fixed_durable_and_saturating(store, config):
    admitted = observation(store)
    assert store.mark_maturity_scheduled_sync(admitted.handle).reason == "observation_not_mature"
    ready = admitted.data["ready_at"]
    assert store.mark_maturity_scheduled_sync(admitted.handle, now=ready + 7).ok
    assert store.mark_maturity_scheduled_sync(admitted.handle, now=ready + 99).reason == "maturity_schedule_replay"
    assert not store.prepare_handoff_sync(admitted.handle, "unknown", now=ready - 1).ok
    assert store.capture_sync(account_id=2, group_id=-55, key="x", body="x").ok
    assert store.get_cache_sync(2, -55, "x").ok
    assert not store.get_cache_sync(2, -55, "missing").ok
    reopened = ReviewEvidenceStore(config)
    counters = reopened.stats_sync().data["lifecycle_counters"]
    assert set(counters) == evidence.COUNTERS
    assert counters["maturity_scheduled"] == 1
    assert counters["maturity_delay_seconds_total"] == counters["maturity_delay_seconds_max"] == 7
    assert counters["cache_hits"] == counters["cache_misses"] == 1
    assert counters["rejected_early_handoffs"] == 1
    index = {"lifecycle_counters": {"cache_hits": evidence.COUNTER_LIMIT}}
    reopened._count(index, "cache_hits")
    assert index["lifecycle_counters"]["cache_hits"] == evidence.COUNTER_LIMIT


def test_history_page_is_atomic_scoped_and_expires_without_replay_extension(store, monkeypatch):
    monkeypatch.setattr(evidence.time, "time", lambda: 10_000.0)
    records = [{"key": str(mid), "body": "historical message", "metadata": {
        "message_id": mid, "date": 9000, "sender_id": 44}} for mid in (10, 11)]
    metadata = {"joined_at": "scope-v1", "event_version": 2, "coverage": {"complete": True}}
    assert store.save_history_page_sync(2, -1001, key="page-v1", records=records,
                                       metadata=metadata, observed_at=10_000).ok
    monkeypatch.setattr(evidence.time, "time", lambda: 10_010.0)
    assert store.save_history_page_sync(2, -1001, key="page-v1", records=records,
        metadata=metadata, observed_at=10_000).ok
    hit = store.load_history_page_sync(2, -1001, key="page-v1")
    assert hit.ok and len(hit.data["records"]) == 2 and hit.data["coverage"] == "unknown"
    assert hit.data["metadata"]["coverage"] == {"complete": True}
    assert hit.data["records"][0]["metadata"]["date"] == 9000
    assert not store.load_history_page_sync(33, -1001, key="page-v1").ok
    assert not store.load_history_page_sync(2, -1002, key="page-v1").ok
    monkeypatch.setattr(evidence.time, "time", lambda: 10_300.0)
    assert not store.load_history_page_sync(2, -1001, key="page-v1").ok
    assert store.save_history_page_sync(2, -1001, key="page-v1", records=records,
        metadata=metadata, observed_at=10_000).reason == "history_page_not_fresh"


def test_history_page_missing_or_deleted_record_is_not_complete(store):
    admitted = observation(store)
    records = [{"key": "10", "body": "historical message", "metadata": {"message_id": 10}}]
    assert store.save_history_page_sync(2, -1001, key="page-v1", records=records, metadata={}).ok
    assert store.load_history_page_sync(2, -1001, key="page-v1").ok
    assert store.invalidate_messages_sync(2, -1001, [10]).ok
    assert not store.load_history_page_sync(2, -1001, key="page-v1").ok
    assert store.get_observation_sync(admitted.handle).data["records"][0]["body"]


def test_history_over_capacity_cannot_partially_commit_or_borrow_protection(store):
    admitted = observation(store)
    records = [{"key": str(i), "body": "x", "metadata": {"message_id": i}} for i in range(20)]
    before = store.stats_sync().data
    assert store.save_history_page_sync(2, -1001, key="huge", records=records,
                                       metadata={}).reason == "history_page_capacity"
    after = store.stats_sync().data
    assert after["ordinary_records"] == before["ordinary_records"] == 0
    assert after["protected_reserved_bytes"] == before["protected_reserved_bytes"]
    assert store.get_observation_sync(admitted.handle).ok


def test_threaded_duplicate_admission_has_one_reservation(store):
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: observation(store, now=now), range(16)))
    assert all(result.ok for result in results)
    assert len({result.handle for result in results}) == 1
    assert store.stats_sync().data["protected_slots_used"] == 1


def test_separate_processes_do_not_lose_committed_captures(store, config):
    options = {**asdict(config), "directory": str(config.directory)}
    script = """
import json,sys
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig,ReviewEvidenceStore
store=ReviewEvidenceStore(EvidenceStoreConfig(**json.loads(sys.argv[1])))
for i in range(3):
    result=store.capture_sync(account_id=2,group_id=-1,key=sys.argv[2]+str(i),body='x')
    assert result.ok, result.reason
"""
    with ThreadPoolExecutor(max_workers=4) as pool:
        processes = list(pool.map(lambda i: subprocess.run(
            [sys.executable, "-c", script, json.dumps(options), str(i)],
            capture_output=True, text=True, timeout=30), range(4)))
    assert all(process.returncode == 0 for process in processes), [p.stderr for p in processes]
    assert store.stats_sync().data["ordinary_records"] == 13  # 12 records + page control
    assert store.path.stat().st_size == config.physical_size_bytes


def test_maintenance_lease_is_shared_and_repeated_waiting_does_not_write(store, config):
    now = datetime.now(UTC)
    admitted = observation(store, now=now)
    before = store.get_observation_sync(admitted.handle).data
    claimed = store.claim_maintenance_sync(account_ids=[2], now=now)
    assert len(claimed.data["observations"]) == 1
    generation = store.stats_sync().data["generation"]
    options = {**asdict(config), "directory": str(config.directory)}
    script = """
import json,sys
from app.modules.acquisition.review_evidence_store import EvidenceStoreConfig,ReviewEvidenceStore
store=ReviewEvidenceStore(EvidenceStoreConfig(**json.loads(sys.argv[1])))
for force in (False, True):
    result=store.claim_maintenance_sync(account_ids=[2],now=sys.argv[2],force=force)
    assert result.ok and not result.data['observations'] and result.data['waiting']==1
"""
    child = subprocess.run([sys.executable, "-c", script, json.dumps(options), now.isoformat()],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert store.stats_sync().data["generation"] == generation
    assert store.finish_maintenance_sync(claimed.data["token"]).ok
    generation = store.stats_sync().data["generation"]
    reopened = ReviewEvidenceStore(config)
    assert not reopened.claim_maintenance_sync(account_ids=[2], now=now).data["observations"]
    assert reopened.stats_sync().data["generation"] == generation
    assert reopened.get_observation_sync(admitted.handle).data == before
    assert reopened.path.stat().st_size == config.physical_size_bytes


@pytest.mark.parametrize("kind", ["gap", "warning", "edited", "deleted", "not_applicable"])
def test_new_control_is_due_without_waiting_for_maintenance_cadence(store, kind):
    now = datetime.now(UTC)
    admitted = observation(store, now=now)
    claimed = store.claim_maintenance_sync(account_ids=[2], now=now)
    assert store.finish_maintenance_sync(claimed.data["token"]).ok
    assert store.record_control_sync(admitted.handle, kind, at=now).ok
    fresh = store.claim_maintenance_sync(account_ids=[2], now=now + timedelta(seconds=1))
    assert len(fresh.data["observations"]) == 1
    assert kind in fresh.data["observations"][0]["control"]["facts"]


@pytest.mark.parametrize("boundary", ["ready_at", "deadline_at"])
def test_maintenance_cadence_does_not_delay_boundary(store, boundary):
    admitted = observation(store)
    due = admitted.data[boundary]
    claimed = store.claim_maintenance_sync(account_ids=[2], now=due - 1)
    assert store.finish_maintenance_sync(claimed.data["token"]).ok
    assert store.claim_maintenance_sync(account_ids=[2], now=due).data["observations"]
    assert store.get_observation_sync(admitted.handle).ok


def test_crashed_maintenance_claim_expires_and_stale_finish_cannot_unlock_new_owner(store):
    now = datetime.now(UTC)
    observation(store, now=now)
    old = store.claim_maintenance_sync(account_ids=[2], now=now)
    claimed = store.claim_maintenance_sync(account_ids=[2], now=now + timedelta(seconds=300))
    assert claimed.data["observations"]
    before = store.stats_sync().data["generation"]
    assert store.finish_maintenance_sync(old.data["token"]).ok
    assert store.stats_sync().data["generation"] == before
    assert not store.claim_maintenance_sync(account_ids=[2], force=True,
        now=now + timedelta(seconds=301)).data["observations"]


def test_shared_maintenance_metadata_fits_full_production_slot_count(config):
    # Small payload banks keep this test cheap; slot/index cardinality matches
    # production, including full SHA256 content versions and ordinary pages.
    full = ReviewEvidenceStore(replace(
        config, protected_slots=96, protected_record_limit=6144,
        slots_per_account=32, ordinary_pages=64,
        ordinary_record_limit=2000, physical_limit_bytes=32 * evidence.MIB,
    ))
    now = datetime.now(UTC)
    for offset in range(96):
        assert observation(full, account=offset // 32 + 1,
                           group=-1_000_000_000_000 - offset,
                           version="a" * 64, now=now).ok
    for offset in range(64):
        assert full.capture_sync(account_id=1, group_id=-offset - 1, key="post", body="x").ok
    claim = full.claim_maintenance_sync(account_ids=[1, 2, 3], now=now)
    assert claim.ok and len(claim.data["observations"]) == 96
    assert full.finish_maintenance_sync(claim.data["token"]).ok
    assert full.stats_sync().data["protected_slots_used"] == 96
    assert full.path.stat().st_size == full.config.physical_size_bytes


@pytest.mark.asyncio
async def test_async_parallel_wrappers_and_sweep_are_local_only(store):
    results = await asyncio.gather(*(store.capture(account_id=2, group_id=-1,
                                                    key=str(i), body="x") for i in range(8)))
    assert all(result.ok for result in results)
    assert (await store.sweep()).ok
    assert (await store.stats()).data["ordinary_records"] == 9
