"""Real transactions; completion telemetry writes no audit metadata."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from ._metrics_helpers import (  # noqa: F401
    decoded,
    row_factory,
    test_complexity_does_not_change_when_optimized_plan_is_enabled,
    test_old_business_semantics_are_fenced_without_resizing,
    test_sdk_purpose_is_lane_first_and_totals_conserved,
    work,
)

from app.core.account import read_metrics as metrics
from app.core.account import read_work, review_completions
from app.modules.acquisition import qualification_events as events
from app.modules.acquisition.models import GroupQualificationAudit


@pytest.fixture
def sink(monkeypatch):
    calls = []
    async def persist(account, stage, increments, **kwargs):
        calls.append((account, stage, dict(increments)))
    monkeypatch.setattr(metrics, "_persist", persist)
    return calls


def revision(row):
    row.checked_at += timedelta(seconds=1)


@pytest.mark.asyncio
async def test_new_revision_commit_retry_and_slice_are_distinct(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    payload = row.evidence_json
    revision(row)
    value = work()
    assert await review_completions.stage_completion(test_db, row, value, kind="review")
    assert await review_completions.stage_completion(test_db, row, value, kind="review")
    await test_db.flush()
    assert not sink
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1
    assert row.evidence_json == payload
    assert not await review_completions.stage_completion(test_db, row, value, kind="review")
    await test_db.commit()
    value.outcome = "trial"
    await metrics.record_work(value)
    assert decoded(sink)["business_completions"]["decisions"] == 1
    assert decoded(sink)["work"][0]["completed_slices"] == 1


@pytest.mark.asyncio
async def test_rollback_does_not_count_and_retry_commits_once(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    original = row.checked_at
    revision(row)
    await review_completions.stage_completion(test_db, row, work(), kind="review")
    await test_db.rollback()
    assert not sink
    await test_db.refresh(row)
    assert row.checked_at == original
    revision(row)
    await review_completions.stage_completion(test_db, row, work(), kind="review")
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1


@pytest.mark.asyncio
async def test_savepoint_does_not_publish_before_outer_commit(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    async with test_db.begin_nested():
        revision(row)
        await review_completions.stage_completion(test_db, row, work(), kind="review")
    assert not sink
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1


@pytest.mark.asyncio
async def test_savepoint_rollback_discards_pending(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    nested = await test_db.begin_nested()
    revision(row)
    await review_completions.stage_completion(test_db, row, work(), kind="review")
    await nested.rollback()
    await test_db.commit()
    assert not sink


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("telemetry"), asyncio.CancelledError()])
async def test_publisher_failure_cannot_fail_business_commit(test_db, monkeypatch, error):
    _, _, _, row = await row_factory(test_db)
    gaps = []
    monkeypatch.setattr(review_completions, "_gap", lambda *args: gaps.append(True))
    monkeypatch.setattr(metrics, "_persist", AsyncMock(side_effect=error))
    revision(row)
    expected = row.checked_at
    assert await review_completions.stage_completion(test_db, row, work(), kind="review")
    await test_db.commit()
    await test_db.refresh(row)
    assert row.checked_at == expected and gaps


@pytest.mark.asyncio
async def test_revision_probe_sql_failure_rolls_back_only_telemetry_savepoint(test_db, monkeypatch, sink):
    _, _, _, row = await row_factory(test_db)
    connection = await test_db.connection()
    execute = type(connection).execute
    fired = []
    async def fail(self, statement, *args, **kwargs):
        if not fired:
            fired.append(True)
            return await execute(self, text("SELECT * FROM missing_telemetry_relation"))
        return await execute(self, statement, *args, **kwargs)
    monkeypatch.setattr(type(connection), "execute", fail)
    revision(row)
    expected = row.checked_at
    assert not await review_completions.stage_completion(test_db, row, work(), kind="review")
    await test_db.commit()
    await test_db.refresh(row)
    assert row.checked_at == expected and not sink


@pytest.mark.asyncio
async def test_stale_second_session_same_revision_cannot_double_count(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    sessions = async_sessionmaker(test_db.bind, expire_on_commit=False)
    async with sessions() as other:
        stale = await other.get(GroupQualificationAudit, row.id)
        revision(row)
        stale.checked_at = row.checked_at
        assert await review_completions.stage_completion(test_db, row, work(), kind="review")
        await test_db.commit()
        assert not await review_completions.stage_completion(other, stale, work(), kind="review")
        await other.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1


@pytest.mark.asyncio
async def test_no_autoflush_and_probe_lock_uses_audit_row(test_db, monkeypatch, sink):
    from sqlalchemy.dialects import postgresql
    _, _, _, row = await row_factory(test_db)
    statements = []
    connection = await test_db.connection()
    original = type(connection).execute
    async def observe(self, statement, *args, **kwargs):
        statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return await original(self, statement, *args, **kwargs)
    monkeypatch.setattr(type(connection), "execute", observe)
    old = row.checked_at
    revision(row)
    assert await review_completions.stage_completion(test_db, row, work(), kind="review")
    assert len(statements) == 1 and "FOR UPDATE OF" in statements[0]
    assert "NOWAIT" in statements[0]
    assert "checked_at" in statements[0]
    assert old != row.checked_at
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1


@pytest.mark.asyncio
async def test_technical_wait_and_cancelled_revision_do_not_count(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    revision(row)
    row.decision = "technical_wait"
    await review_completions.stage_completion(test_db, row, work(), kind="review")
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 0
    revision(row)
    row.decision = "trial"
    await review_completions.stage_completion(test_db, row, work(), kind="review")
    row.state = "cancelled"
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 0


@pytest.mark.asyncio
async def test_real_inline_assess_counts_only_after_commit(test_db, monkeypatch, sink):
    from app.modules.acquisition import qualification_service as service
    account, group, _, row = await row_factory(test_db)
    monkeypatch.setattr(service, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(service, "manually_protected", lambda *args: True)
    result = await service.assess(Obj(db=test_db), account.id, group, row=row)
    assert not result.passed and row.decision == "protected"
    assert decoded(sink)["business_completions"]["decisions"] == 0
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1


@pytest.mark.asyncio
async def test_repeated_new_reviews_add_no_persistent_measurement_fields(test_db, sink):
    _, _, _, row = await row_factory(test_db)
    payload = row.evidence_json
    for _ in range(30):
        revision(row)
        await review_completions.stage_completion(test_db, row, work(), kind="review")
        await test_db.commit()
        assert row.evidence_json == payload
    assert decoded(sink)["business_completions"]["decisions"] == 30
    assert len((await test_db.scalars(select(GroupQualificationAudit))).all()) == 1
    assert "measurement_completions" not in row.evidence_json


async def renewal_setup(db, monkeypatch, *, identity="identity", event=False):
    from app.modules.acquisition import (
        qualification_actions,
        qualification_identity_refresh,
        qualification_renewal,
        qualification_service,
        review_fact_owner,
        review_read_plan,
    )
    account, group, member, row = await row_factory(db)
    monkeypatch.setattr(qualification_renewal, "eligible", AsyncMock(return_value=True))
    monkeypatch.setattr(qualification_service, "policy", AsyncMock(return_value={}))
    monkeypatch.setattr(qualification_identity_refresh, "reusable_identity_context", AsyncMock(return_value={
        "fingerprint": identity, "managed_account_ids": [2], "basis": "local"}))
    monkeypatch.setattr(qualification_identity_refresh, "wake_identity_schedule", AsyncMock())
    monkeypatch.setattr(review_read_plan, "for_review", AsyncMock(return_value=Obj(phase="permissions_only", reads=6, bounded=True)))
    monkeypatch.setattr(review_fact_owner, "create_context", lambda *args: None)
    monkeypatch.setattr(qualification_actions, "refresh_live_authorization", AsyncMock())
    pool = Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=Obj(client=Obj())), release=AsyncMock())
    if event:
        await events.request(db, member, "membership", source_token="offline-event-one")
        await db.commit()
    return Obj(db=db, account_pool=pool), account, group, member, row


async def renew(values):
    from app.modules.acquisition.qualification_renewal import try_light_renewal
    service, account, group, member, row = values
    token = read_work._current.set(work("renewal"))
    try:
        return await try_light_renewal(service, account, group, member, row,
            json.loads(row.evidence_json), force_refresh=False)
    finally:
        read_work._current.reset(token)


@pytest.mark.asyncio
async def test_identical_local_grant_reuse_is_never_an_independent_decision(test_db, monkeypatch, sink):
    values = await renewal_setup(test_db, monkeypatch)
    checked = values[-1].checked_at
    for _ in range(5):
        assert (await renew(values)).passed
        await test_db.commit()
    assert not sink and values[-1].checked_at == checked
    assert "measurement_completions" not in values[-1].evidence_json


@pytest.mark.asyncio
async def test_real_identity_transition_counts_once_without_checked_at_change(test_db, monkeypatch, sink):
    values = await renewal_setup(test_db, monkeypatch, identity="changed")
    checked = values[-1].checked_at
    assert (await renew(values)).passed and not sink
    await test_db.commit()
    assert decoded(sink)["work"][1]["completed_decisions"] == 1
    assert values[-1].checked_at == checked
    assert (await renew(values)).passed
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1
    assert "measurement_completions" not in values[-1].evidence_json


@pytest.mark.asyncio
async def test_event_requires_actual_consumption_and_commit(test_db, monkeypatch, sink):
    values = await renewal_setup(test_db, monkeypatch, event=True)
    member, row = values[-2:]
    value = await events.pending(test_db, member)
    assert (await renew(values)).passed
    assert not sink
    assert await events.acknowledge(test_db, member, value)
    assert not sink
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1
    assert not await events.pending(test_db, member)
    # A stale transaction replay cannot consume the already cleared generation.
    assert await review_completions.stage_completion(test_db, row, work("renewal"), kind="renewal", event_value=value)
    assert not await events.acknowledge(test_db, member, value)
    await test_db.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1


@pytest.mark.asyncio
async def test_unacknowledged_event_or_newer_event_cannot_inflate_denominator(test_db, monkeypatch, sink):
    values = await renewal_setup(test_db, monkeypatch, event=True)
    member = values[-2]
    old = await events.pending(test_db, member)
    assert (await renew(values)).passed
    await events.request(test_db, member, "rules", source_token="offline-newer-event")
    assert not await events.acknowledge(test_db, member, old)
    await test_db.commit()
    assert not sink
    assert await events.pending(test_db, member)


@pytest.mark.asyncio
async def test_acknowledged_event_rollback_restores_work_and_does_not_count(test_db, monkeypatch, sink):
    values = await renewal_setup(test_db, monkeypatch, event=True)
    member, row = values[-2:]
    old = await events.pending(test_db, member)
    assert (await renew(values)).passed
    assert await events.acknowledge(test_db, member, old)
    await test_db.rollback()
    await test_db.refresh(member)
    await test_db.refresh(row)
    assert await events.pending(test_db, member) == old
    assert not sink


@pytest.mark.asyncio
async def test_two_sessions_same_event_consume_and_count_once(test_db, monkeypatch, sink):
    from app.core.group.models import GroupAccountMembership
    values = await renewal_setup(test_db, monkeypatch, event=True)
    member, row = values[-2:]
    value = await events.pending(test_db, member)
    sessions = async_sessionmaker(test_db.bind, expire_on_commit=False)
    async with sessions() as other:
        stale = await other.get(GroupQualificationAudit, row.id)
        stale_member = await other.get(GroupAccountMembership, member.id)
        assert await review_completions.stage_completion(test_db, row, work("renewal"), kind="renewal", event_value=value)
        assert await review_completions.stage_completion(other, stale, work("renewal"), kind="renewal", event_value=value)
        assert await events.acknowledge(test_db, member, value)
        await test_db.commit()
        assert not await events.acknowledge(other, stale_member, value)
        await other.commit()
    assert decoded(sink)["business_completions"]["decisions"] == 1
