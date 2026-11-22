"""Shared v2-safe fixtures and selected attribution/cutover assertions."""
import asyncio


import json


import mmap


import struct


from collections import Counter


from datetime import UTC, datetime, timedelta


from types import SimpleNamespace as Obj


from unittest.mock import AsyncMock


import pytest


from app.core.account import read_metrics as metrics


from app.core.account import read_work, review_completions


from app.core.account.models import AccountStatus, TelegramAccount


from app.core.group.models import Group, GroupAccountMembership


from app.modules.acquisition.group_qualification import POLICY_VERSION


from app.modules.acquisition.models import GroupQualificationAudit


async def row_factory(db):
    now = datetime.now(UTC).replace(tzinfo=None)
    account = TelegramAccount(id=2, identifier="offline-metrics", session_name="offline-metrics",
                              status=AccountStatus.ONLINE, is_active=True)
    group = Group(id=40, group_id=-1001111222233)
    member = GroupAccountMembership(id=60, account_id=2, group_id=40, telegram_group_id=group.group_id,
        status="joined", joined_at=now-timedelta(days=5), review_status="approved", ad_status="active")
    row = GroupQualificationAudit(id=70, batch_id="offline-metrics", membership_id=60, account_id=2,
        group_id=40, membership_joined_at=member.joined_at, policy_version=POLICY_VERSION,
        content_scope="text_profile", state="completed", decision="trial", checked_at=now,
        expires_at=now+timedelta(days=2), evidence_hash="unchanged-auth-hash",
        evidence_json=json.dumps({"checked_at": now.isoformat(), "advertising_audit": {"ad_allowed": True},
            "system_identity_fingerprint": "identity", "authorization_mode": "events"}))
    db.add_all([account, group, member, row])
    await db.commit()
    return account, group, member, row


def work(kind="review"):
    value = read_work.ReadWork(2, 40, kind)
    value.measurement_stage = "phase1"
    value.measurement_complexity = "standard"
    return value


def decoded(calls):
    totals = Counter()
    for _, _, values in calls:
        totals.update(values)
    return metrics._decode_stage("phase1", [totals[i] for i in range(metrics.METRICS)])


@pytest.mark.asyncio
@pytest.mark.parametrize("lane,purpose,expected", [
    ("sync", "group_qualification", "sync"), ("listener", "ad_qualification_refresh", "listener"),
    ("routine", "growth_listener", "listener"), ("sync", "ad_delivery", "sync"),
    ("critical", "group_qualification", "renewal")])
async def test_sdk_purpose_is_lane_first_and_totals_conserved(monkeypatch, sink, lane, purpose, expected):
    value = work("renewal")
    token = read_work._current.set(value)
    try:
        await metrics.record_rpc(2, ["channels.GetMessagesRequest"]*2, "entered_sdk", purpose=purpose, lane=lane)
    finally:
        read_work._current.reset(token)
    result = decoded(sink)
    assert result["entered_sdk"] == 2
    assert result["rpc"][0]["purpose"] == expected


@pytest.mark.asyncio
async def test_complexity_does_not_change_when_optimized_plan_is_enabled(sink):
    value = work()
    token = read_work._current.set(value)
    try:
        metrics.lock_business_complexity({}, None)
        metrics.set_work_plan("baseline", complexity="complex")
        assert value.measurement_complexity == "complex"
        metrics.set_work_plan("phase2", complexity="simple")
        assert value.measurement_complexity == "complex"
        metrics.lock_business_complexity({"checked_at": "prior"}, {"kinds": ["future_unknown_event"]})
        assert value.measurement_complexity == "unknown"
    finally:
        read_work._current.reset(token)


def test_old_business_semantics_are_fenced_without_resizing(tmp_path, monkeypatch):
    t0 = 1_799_712_000
    clock = [t0]
    monkeypatch.setattr(metrics.time, "time", lambda: clock[0])
    store = metrics.FixedReadMetrics(tmp_path / "meter")
    store.add(2, "phase1", {1: 4}, at=t0)
    path = store.directory / metrics.FILENAME
    with path.open("r+b") as handle:
        with mmap.mmap(handle.fileno(), metrics.FILE_BYTES) as data:
            struct.pack_into("<d", data, metrics.BUSINESS_SEMANTICS_AT, 0)
            data.flush()
    clock[0] = t0+3601
    result = store.snapshot(2, now=t0+3601, hours=1)
    assert not result["complete"] and result["actual_reads"] is None
    assert result["known_entered_sdk"] == 4
    assert result["reason"] == "window_before_business_semantics"
    assert store.mark_business_semantics_cutover(t0+3700) == t0+3700
    assert store.mark_business_semantics_cutover(t0+3601) == t0+3700
    assert path.stat().st_size == metrics.FILE_BYTES == 16_261_120

