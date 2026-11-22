"""READY origin, real owner step and retained failure denominator regressions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys

import pytest
import pytest_asyncio

HERE = Path(__file__).resolve().parents[3] / "app" / "modules" / "acquisition"
sys.path.insert(0, str(HERE.parents[2]))
spec = importlib.util.spec_from_file_location("qualification_sla_draft", HERE / "qualification_sla.py")
sla = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sla
spec.loader.exec_module(sla)

T0 = datetime(2026, 11, 6, 0, 0)
NOW = T0 + timedelta(minutes=10)
START = T0 - timedelta(hours=1)


def coverage(now=NOW, start=START):
    return {"started_at": start.isoformat(), "last_scan_at": now.isoformat(), "gaps": [],
        "transaction_frontier_verified": True}


def ready(work_id=1, *, at=T0, origin_unknown=False, health=True, payable=True, required=2, available=4, sequence=0):
    return {"account_id": 33, "work_id": work_id, "revision": 1, "ready_sequence": sequence,
        "ready_at": at.isoformat(), "scope_hash": "scope-a", "family": "qualification",
        "origin_unknown": origin_unknown, "eligibility": {"anchor_ready_at": at.isoformat(),
            "observed_at": at.isoformat(), "health": health, "payable": payable,
            "required_reads": required, "available_reads": available, "plan_fingerprint": "plan-a"}}


def started(event, seconds=30, **updates):
    return {**deepcopy(event), "started_at": (_date(event["ready_at"])+timedelta(seconds=seconds)).isoformat(),
        "actor_verified": True, "actor_account": event["account_id"], "step_entered": True,
        "budget_granted": True, "claim_token": "claim-a", "plan_fingerprint": "plan-a", **updates}


def _date(raw):
    return datetime.fromisoformat(raw)


def row(event, seconds=None, **updates):
    result = sla._new(event, NOW)
    if seconds is not None:
        applied, reason = sla._merge_start(result, started(event, seconds), NOW)
        assert applied and reason is None
    result.update(updates)
    return result


def verified_row(event, seconds=30, **updates):
    result = sla._new(event, NOW)
    actual = _date(event["ready_at"])+timedelta(seconds=seconds)
    admitted, reason = sla._merge_start(result, started(event, seconds, health_verified=True,
        health_observed_at=actual.isoformat(), budget_observed_at=actual.isoformat(), required_reads=2, **updates), NOW)
    assert admitted and reason is None
    return result


def report(rows, marker=None):
    return sla.evaluate(rows, coverage() if marker is None else marker, NOW, START)


def test_zero_sample_is_not_a_pass():
    actual = report([])
    assert actual["strict_rate"] is None
    assert actual["status"] == "not_evaluable"
    assert actual["diagnostic_all_ready"]["observed_start_fraction"] is None
    assert actual["guaranteed_lower_bound"] is None


def test_95_percent_requires_all_mature_eligible_including_never_started():
    rows = [row(ready(identity), 120) for identity in range(1, 20)] + [row(ready(20))]
    actual = report(rows)
    assert actual["eligible_mature"] == 20
    assert actual["started_within_120"] == 19
    assert actual["never_started"] == 1
    assert actual["strict_rate"] == .95 and actual["status"] == "pass"
    rows.append(row(ready(21)))
    assert report(rows)["status"] == "fail"


def test_late_never_started_failed_and_software_cancelled_remain_denominator():
    rows = [row(ready(1), 121), row(ready(2), outcome="failed", closed_at=(T0+timedelta(seconds=40)).isoformat()),
        row(ready(3), outcome="cancelled", closed_at=(T0+timedelta(seconds=60)).isoformat()), row(ready(4), 20, outcome="failed")]
    actual = report(rows)
    assert actual["eligible_mature"] == 4
    assert actual["late"] == 1 and actual["never_started"] == 2
    assert actual["failed"] == 3
    assert actual["strict_rate"] == .25


@pytest.mark.parametrize("field", ["health", "payable"])
def test_unknown_origin_makes_strict_null_even_if_actual_step_was_funded(field):
    event = ready(**{field: None})
    actual = report([row(event, 10)])
    assert actual["strict_rate"] is None and actual["unknown"] == 1
    assert actual["diagnostic_all_ready"]["observed_start_fraction"] == 1
    assert not actual["diagnostic_all_ready"]["is_acceptance_metric"]


def test_default_zero_reads_is_unknown_until_a_real_local_plan_proves_zero():
    event = ready(required=0)
    assert report([row(event)])["unknown"] == 1
    event["eligibility"]["explicit_local_zero_plan"] = True
    actual = row(event)
    entered, reason = sla._merge_start(actual, started(event, budget_granted=False, explicit_local_zero_plan=True, required_reads=0), NOW)
    assert entered and reason is None
    assert report([actual])["strict_rate"] == 1


def test_proved_local_effect_can_start_without_fabricating_its_origin_eligibility():
    event = ready(payable=None)
    actual = row(event)
    admitted, reason = sla._merge_start(actual, started(event, budget_granted=False,
        explicit_local_zero_plan=True, required_reads=0), NOW)
    assert admitted and reason is None
    assert report([actual])["strict_rate"] is None


def test_claim_only_and_unfunded_preflight_do_not_start_a_step():
    event = ready()
    actual = row(event)
    assert sla._merge_start(actual, started(event, step_entered=False), NOW) == (False, "no_actual_step")
    assert sla._merge_start(actual, started(event, actor_verified=False), NOW) == (False, "no_actual_step")
    assert sla._merge_start(actual, started(event, budget_granted=False), NOW) == (False, "step_not_funded_or_local")
    projected = report([actual])
    assert projected["strict_rate"] == 0 and projected["never_started"] == 1


def test_later_snapshot_cannot_reclassify_origin_unknown_or_reset_immutable_start():
    event = ready(payable=None)
    actual = row(event)
    later = ready()
    later["eligibility"]["observed_at"] = (T0+timedelta(seconds=300)).isoformat()
    sla._merge_origin(actual, later, NOW)
    assert report([actual])["strict_rate"] is None
    assert actual["ready_at"] == T0.isoformat()
    historical = ready(origin_unknown=True)
    historical_row = row(historical)
    sla._merge_origin(historical_row, ready(), NOW)
    assert historical_row["origin_unknown"] and report([historical_row])["unknown"] == 1


def test_authentic_origin_proof_delivered_late_is_allowed_but_start_does_not_prove_origin():
    event = ready(payable=None)
    actual = row(event)
    assert sla._merge_start(actual, started(event), NOW)[0]
    assert report([actual])["strict_rate"] is None
    sla._merge_origin(actual, ready(), NOW)
    assert report([actual])["strict_rate"] == 1


@pytest.mark.parametrize("updates", [{"actor_account": 34}, {"scope_hash": "other"}, {"plan_fingerprint": "other"}, {"claim_token": ""}])
def test_wrong_owner_scope_plan_or_generation_cannot_close_the_obligation(updates):
    event = ready()
    actual = row(event)
    assert sla._merge_start(actual, started(event, **updates), NOW)[0] is False
    projected = report([actual])
    assert actual["first_actual_step_at"] is None
    assert projected["strict_rate"] is None and projected["unknown"] == 1


def test_young_tasks_are_reported_separately_and_pending_cannot_age_out():
    old = START - timedelta(days=8)
    actual = report([row(ready(1, at=old)), row(ready(2, at=NOW-timedelta(seconds=119)))])
    assert actual["eligible_mature"] == 1
    assert actual["never_started"] == 1 and actual["young"] == 1
    assert actual["carried_pending"] == 1 and actual["strict_rate"] == 0


@pytest.mark.parametrize("marker", [{}, {"complete_override": True},
    {"started_at": START.isoformat(), "last_scan_at": (NOW-timedelta(seconds=121)).isoformat()},
    {"started_at": START.isoformat(), "last_scan_at": NOW.isoformat(), "gaps": [{"start": T0.isoformat(), "end": (T0+timedelta(seconds=1)).isoformat()}]},
    {"started_at": START.isoformat(), "last_scan_at": NOW.isoformat(), "unknown_until": NOW.isoformat()}])
def test_incomplete_coverage_never_becomes_a_pass(marker):
    actual = report([row(ready(), 10)], marker)
    assert actual["strict_rate"] is None and actual["status"] == "not_evaluable"


@pytest.mark.parametrize("frontier", [None, False, 1, "true"], ids=["missing", "false", "integer", "string"])
def test_unverified_transaction_frontier_preserves_counts_but_never_yields_rates(frontier):
    rows = [verified_row(ready(1)), row(ready(2)), verified_row(ready(3), seconds=121)]
    proven = report(rows)
    assert proven["transaction_frontier_verified"] is True
    assert proven["strict_rate"] == pytest.approx(1/3)
    assert proven["guaranteed_lower_bound"] == pytest.approx(1/3)
    marker = coverage()
    if frontier is None:
        marker.pop("transaction_frontier_verified")
    else:
        marker["transaction_frontier_verified"] = frontier
    actual = report(rows, marker)
    assert actual["transaction_frontier_verified"] is False
    assert actual["strict_rate"] is None and actual["guaranteed_lower_bound"] is None
    assert actual["status"] == "not_evaluable"
    assert "transaction_frontier_unverified" in actual["incomplete_reasons"]
    assert "transaction_frontier_unverified" in actual["guaranteed_lower_bound_incomplete_reasons"]
    for key, expected in {
        "total": 3, "eligible_mature": 3, "started_within_120": 1,
        "never_started": 1, "late": 1, "potential_mature_never_started": 1,
        "guaranteed_lower_bound_numerator": 1, "guaranteed_lower_bound_denominator": 3,
    }.items():
        assert actual[key] == expected
    assert actual["diagnostic_all_ready"]["observed_start_fraction"] == pytest.approx(1/3)


@pytest.mark.parametrize("age, valid", [(60, True), (60.001, False), (-.001, False)])
def test_real_source_inventory_heartbeat_must_be_current_before_episode_matures(age, valid):
    marker = {**coverage(), "expected_source_counts": {"growth": 1},
        "source_inventory_verified": True, "source_pending_events": 0,
        "source_confirmed_through": NOW.isoformat(),
        "source_observed_at": (NOW-timedelta(seconds=age)).isoformat()}
    actual = report([verified_row(ready())], marker)
    assert actual["strict_rate"] == (1 if valid else None)
    assert actual["guaranteed_lower_bound"] == (1 if valid else None)
    assert ("source_heartbeat_stale" in actual["incomplete_reasons"]) is (not valid)


@pytest.mark.parametrize("changes, reason", [
    ({"source_pending_events": 1}, "source_events_pending"),
    ({"source_inventory_verified": False}, "source_inventory_unverified"),
])
def test_real_source_pending_events_or_unverified_inventory_make_both_rates_unknown(changes, reason):
    marker = {**coverage(), "expected_source_counts": {"growth": 1},
        "source_inventory_verified": True, "source_pending_events": 0,
        "source_confirmed_through": NOW.isoformat(),
        "source_observed_at": NOW.isoformat(), **changes}
    actual = report([verified_row(ready())], marker)
    assert actual["strict_rate"] is None and actual["guaranteed_lower_bound"] is None
    assert reason in actual["incomplete_reasons"]
    assert reason in actual["guaranteed_lower_bound_incomplete_reasons"]


@pytest.mark.parametrize("confirmed", [None, (NOW-timedelta(microseconds=1)).isoformat(),
    (NOW+timedelta(microseconds=1)).isoformat()])
def test_source_watermark_never_proves_an_unconfirmed_or_future_window(confirmed):
    marker = {**coverage(), "expected_source_counts": {"growth": 1},
        "source_inventory_verified": True, "source_pending_events": 0,
        "source_observed_at": NOW.isoformat(), "source_confirmed_through": confirmed}
    actual = report([verified_row(ready())], marker)
    assert actual["strict_rate"] is None and actual["guaranteed_lower_bound"] is None
    assert "source_window_unconfirmed" in actual["incomplete_reasons"]


def test_confirmed_historical_end_keeps_actual_observation_clock_and_maturity_separate():
    cutoff = NOW-timedelta(seconds=20)
    marker = {**coverage(), "expected_source_counts": {"growth": 1},
        "source_inventory_verified": True, "source_pending_events": 0,
        "source_observed_at": NOW.isoformat(), "source_confirmed_through": cutoff.isoformat()}
    old = verified_row(ready(1))
    young_at_cutoff = verified_row(ready(2, at=NOW-timedelta(seconds=130)), seconds=10)
    actual = sla.evaluate([old, young_at_cutoff], marker, NOW, START, window_end=cutoff)
    assert actual["strict_rate"] == 1 and actual["guaranteed_lower_bound"] == 1
    assert actual["young"] == 1 and actual["eligible_mature"] == 1
    assert actual["window_end"] == cutoff.isoformat() and actual["observed_at"] == NOW.isoformat()


def test_later_start_cannot_rewrite_an_earlier_confirmed_report():
    actual = sla.evaluate([verified_row(ready(), seconds=121)], coverage(), NOW, START,
        window_end=T0+timedelta(seconds=120))
    assert actual["strict_rate"] == 0 and actual["never_started"] == 1
    assert actual["potential_mature_never_started"] == 1
    assert actual["guaranteed_lower_bound"] is None


def test_known_blocked_is_separate_but_unknown_component_still_invalidates_rate():
    actual = report([row(ready(1), 10), row(ready(2, health=False)), row(ready(3, payable=False, available=0))])
    assert actual["strict_rate"] == 1 and actual["known_blocked"] == 2
    actual = report([row(ready(1), 10), row(ready(2, health=False, payable=None))])
    assert actual["strict_rate"] is None


def test_ever_eligible_lower_bound_is_not_an_origin_eligible_subgroup_rate():
    # Nineteen tasks were blocked at READY but became payable and started in
    # 30s. The sole origin-eligible task never started. Origin SLO is 0, while
    # the explicitly different ever-eligible interval lower bound is .95.
    rows = [verified_row(ready(identity, payable=False, available=0)) for identity in range(1, 20)]
    rows.append(row(ready(20)))
    actual = report(rows)
    assert actual["strict_rate"] == 0 and actual["status"] == "fail"
    assert actual["guaranteed_lower_bound"] == .95
    assert actual["guaranteed_lower_bound_status"] == "proved_at_least_threshold"
    assert actual["rate_type"] == "origin_eligible_exact"
    assert actual["guaranteed_lower_bound_rate_type"] == "eligible_during_ready_interval_lower_bound"
    unknown = [verified_row(ready(identity, payable=None)) for identity in range(1, 20)] + [row(ready(20))]
    assert report(unknown)["strict_rate"] is None
    assert report(unknown)["guaranteed_lower_bound"] == .95


def test_lower_bound_retains_every_mature_potential_unknown_late_and_never_started():
    rows = [verified_row(ready(1, origin_unknown=True)), verified_row(ready(2, payable=None), 121),
        row(ready(3, health=None)), row(ready(4, payable=False, available=0))]
    actual = report(rows)
    assert actual["strict_rate"] is None
    assert actual["guaranteed_lower_bound_numerator"] == 1
    assert actual["guaranteed_lower_bound_denominator"] == 4
    assert actual["guaranteed_lower_bound"] == .25
    assert actual["guaranteed_lower_bound_status"] == "insufficient_lower_bound"


@pytest.mark.parametrize("changes", [{"health_verified": False}, {"required_reads": None}, {"required_reads": 0},
    {"health_observed_at": (T0+timedelta(seconds=32)).isoformat()},
    {"budget_observed_at": (T0+timedelta(seconds=32)).isoformat()}])
def test_lower_success_requires_fresh_start_health_and_actual_known_plan(changes):
    actual = verified_row(ready(payable=None))
    actual["start_proof"].update(changes)
    projected = report([actual])
    assert projected["guaranteed_lower_bound"] is None
    assert projected["guaranteed_lower_bound_numerator"] == 0


@pytest.mark.parametrize("updates", [{"origin_time_unknown": True}, {"origin_event_received": False}, {"invalid_reasons": ["scope_mismatch"]}])
def test_lower_bound_needs_the_complete_immutable_ready_universe(updates):
    actual = verified_row(ready(payable=None))
    actual.update(updates)
    assert report([actual])["guaranteed_lower_bound"] is None


def test_no_source_coverage_never_yields_a_lower_bound():
    assert report([verified_row(ready())], {})["guaranteed_lower_bound"] is None


def test_lower_bound_holds_for_every_consistent_eligible_and_success_subset():
    # For all finite P up to six tasks, observed successes S are proved
    # eligible during READY. Every possible true eligible E must contain S,
    # and every possible true timely T must contain S and be contained by E.
    for count in range(1, 7):
        universe = (1 << count)-1
        for known_success in range(1, universe+1):
            rows = [verified_row(ready(identity+1, payable=None)) if known_success & (1 << identity)
                else row(ready(identity+1, payable=None)) for identity in range(count)]
            bound = report(rows)["guaranteed_lower_bound"]
            assert bound == known_success.bit_count()/count
            for eligible in range(1, universe+1):
                if known_success & ~eligible:
                    continue
                timely = eligible
                while True:
                    if not known_success & ~timely:
                        assert timely.bit_count()/eligible.bit_count() >= bound
                    if timely == 0:
                        break
                    timely = (timely-1) & eligible


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.core.settings_models import SystemSetting
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SystemSetting.__table__.create(sync))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def add_coverage(db, *, now=NOW, start=START):
    from app.core.settings_models import SystemSetting
    db.add(SystemSetting(key=sla.COVERAGE_PREFIX+"33", value=json.dumps(coverage(now, start))))
    await db.flush()


@pytest.mark.asyncio
async def test_sql_duplicate_delivery_and_restart_have_one_denominator(db):
    event = ready()
    await sla.record_ready(db, event, NOW)
    await sla.record_ready(db, deepcopy(event), NOW)
    await sla.record_started(db, started(event), NOW)
    await sla.record_started(db, started(event), NOW)
    await db.commit()
    db.expire_all()
    await add_coverage(db)
    actual = await sla.summary(db, 33, NOW, hours=1)
    assert actual["total"] == 1 and actual["strict_rate"] == 1


@pytest.mark.asyncio
async def test_sql_start_arrives_before_ready_and_cannot_make_missing_origin_look_eligible(db):
    event = ready()
    await sla.record_started(db, started(event), NOW)
    await add_coverage(db)
    assert (await sla.summary(db, 33, NOW, hours=1))["strict_rate"] is None
    await sla.record_ready(db, event, NOW)
    assert (await sla.summary(db, 33, NOW, hours=1))["strict_rate"] == 1


@pytest.mark.asyncio
async def test_sql_same_episode_sequence_cannot_move_ready_origin(db):
    event = ready()
    await sla.record_ready(db, event, NOW)
    moved = ready(at=T0+timedelta(seconds=500))
    actual = await sla.record_ready(db, moved, NOW)
    assert not actual["recorded"] and actual["reason"] == "immutable_ready_conflict"
    rows = await sla._rows(db, 33)
    assert len(rows) == 1 and json.loads(rows[0].value)["ready_at"] == T0.isoformat()


@pytest.mark.asyncio
async def test_sql_claim_only_is_not_actual_start_and_unknown_budget_is_not_upgraded(db):
    event = ready(payable=None)
    await sla.record_ready(db, event, NOW)
    claimed = await sla.record_started(db, started(event, step_entered=False), NOW)
    assert not claimed["recorded"]
    await sla.record_started(db, started(event), NOW)
    await add_coverage(db)
    actual = await sla.summary(db, 33, NOW, hours=1)
    assert actual["unknown"] == 1 and actual["strict_rate"] is None


@pytest.mark.asyncio
async def test_sql_pending_and_failed_never_started_survive_retention_and_capacity(db, monkeypatch):
    old = NOW-timedelta(days=9)
    pending, failed, completed = ready(1, at=old), ready(2, at=old), ready(3, at=old)
    for event in (pending, failed, completed):
        await sla.record_ready(db, event, NOW)
    await sla.record_finished(db, {**failed, "closed_at": (old+timedelta(seconds=200)).isoformat(), "outcome": "failed"}, NOW)
    await sla.record_started(db, started(completed), NOW)
    await sla.record_finished(db, {**completed, "closed_at": (old+timedelta(seconds=200)).isoformat(), "outcome": "completed"}, NOW)
    monkeypatch.setattr(sla, "MAX_ROWS", 3)
    admitted = await sla.record_ready(db, ready(4), NOW)
    assert admitted["recorded"]
    assert {json.loads(item.value)["work_id"] for item in await sla._rows(db, 33)} == {1, 2, 4}
    rejected = await sla.record_ready(db, ready(5), NOW)
    assert not rejected["recorded"] and rejected["reason"] == "episode_capacity_exhausted"
    assert {json.loads(item.value)["work_id"] for item in await sla._rows(db, 33)} == {1, 2, 4}
    actual = await sla.summary(db, 33, NOW, hours=1)
    assert actual["strict_rate"] is None and "persisted_measurement_gap" in actual["incomplete_reasons"]


@pytest.mark.asyncio
async def test_sql_writer_never_commits_or_rolls_back_caller_and_summary_is_read_only(db, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("measurement touched caller transaction")
    monkeypatch.setattr(db, "commit", forbidden)
    monkeypatch.setattr(db, "rollback", forbidden)
    await sla.record_ready(db, ready(), NOW)
    await add_coverage(db)
    monkeypatch.setattr(db, "flush", forbidden)
    result = await sla.summary(db, 33, NOW, hours=1)
    assert result["never_started"] == 1 and result["strict_rate"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("frontier", [True, False])
async def test_read_only_report_exposes_confirmed_cutoff_and_keeps_never_started_denominator(db, monkeypatch, frontier):
    from app.core.settings_models import SystemSetting
    first, pending = ready(1), ready(2)
    for event in (first, pending):
        await sla.record_ready(db, event, NOW)
    at = T0+timedelta(seconds=30)
    await sla.record_started(db, started(first, health_verified=True, required_reads=2,
        health_observed_at=at.isoformat(), budget_observed_at=at.isoformat()), NOW)
    await sla.record_ready(db, ready(3, at=NOW-timedelta(seconds=130)), NOW)
    cutoff = NOW-timedelta(seconds=20)
    marker = {**coverage(), "expected_source_counts": {"growth": 1},
        "source_inventory_verified": True, "source_pending_events": 0,
        "source_observed_at": NOW.isoformat(), "source_confirmed_through": cutoff.isoformat(),
        "transaction_frontier_verified": frontier}
    db.add(SystemSetting(key=sla.COVERAGE_PREFIX+"33", value=json.dumps(marker)))
    await db.flush()
    async def forbidden_async(*args, **kwargs):
        raise AssertionError("report wrote to caller session")
    def forbidden_sync(*args, **kwargs):
        raise AssertionError("report autoflushed caller session")
    db.autoflush = True
    db.add(SystemSetting(key="unrelated.pending.setting", value="untouched"))
    monkeypatch.setattr(db, "flush", forbidden_async)
    monkeypatch.setattr(db, "commit", forbidden_async)
    monkeypatch.setattr(db, "rollback", forbidden_async)
    monkeypatch.setattr(db.sync_session, "flush", forbidden_sync)
    actual = await sla.report(db, 33, NOW, since=START)
    assert actual["requested_end"] == NOW.isoformat() and actual["observed_at"] == NOW.isoformat()
    assert actual["effective_end"] == cutoff.isoformat() and actual["report_lag_seconds"] == 20
    assert actual["young"] == 1 and actual["eligible_mature"] == 2
    assert actual["potential_mature_never_started"] == 1 and actual["never_started"] == 1
    assert actual["strict_rate"] == (.5 if frontier else None)
    assert actual["guaranteed_lower_bound"] == (.5 if frontier else None)
    assert actual["transaction_frontier_verified"] is frontier
    assert actual["source_coverage"]["expected_source_counts"] == {"growth": 1}
    assert actual["source_coverage"]["inventory_verified"] is True
    assert actual["source_coverage"]["transaction_frontier_verified"] is frontier
    assert actual["source_coverage"]["source_confirmed_through"] == cutoff.isoformat()
    assert actual["source_coverage"]["gap_count"] == 0 and "gaps" not in actual["source_coverage"]
    assert (await sla.summary(db, 33, NOW, hours=1))["effective_end"] == cutoff.isoformat()


@pytest.mark.asyncio
async def test_report_beyond_retained_completed_history_cannot_claim_complete_coverage(db):
    await sla.record_ready(db, ready(), NOW)
    await add_coverage(db)
    actual = await sla.report(db, 33, NOW, since=NOW-timedelta(days=8))
    assert actual["strict_rate"] is None and actual["guaranteed_lower_bound"] is None
    assert "retention_window_exceeded" in actual["incomplete_reasons"]


async def resolved(db, kind, event, now=NOW):
    canonical = await sla.resolve_event(db, kind, event, now)
    if canonical is not None:
        await {"ready": sla.record_ready, "started": sla.record_started,
            "finished": sla.record_finished}[kind](db, canonical, now)
    return canonical


@pytest.mark.asyncio
async def test_resolver_pending_budget_owner_retries_share_one_immutable_origin(db):
    initial = ready()
    first = await resolved(db, "ready", initial)
    await resolved(db, "started", started(initial, step_entered=False))
    retry = ready(at=T0+timedelta(seconds=40), origin_unknown=True)
    retry["origin_state"] = "WAIT_BUDGET"
    canonical = await resolved(db, "ready", retry)
    assert canonical["ready_at"] == first["ready_at"] and canonical["ready_sequence"] == 0
    entered = await resolved(db, "started", started(retry, seconds=20))
    assert entered["ready_at"] == T0.isoformat()
    values = [json.loads(item.value) for item in await sla._rows(db, 33)]
    assert len(values) == 1 and values[0]["eligibility"]["state"] == "eligible"
    assert values[0]["first_actual_step_at"] == (T0+timedelta(seconds=60)).isoformat()


@pytest.mark.asyncio
async def test_resolver_new_step_requires_real_previous_start_generation_and_replays_idempotently(db):
    initial = ready()
    await resolved(db, "ready", initial)
    await resolved(db, "started", started(initial))
    following = ready(at=T0+timedelta(seconds=90))
    following["after_claim_token"] = "claim-a"
    second = await resolved(db, "ready", following)
    assert second["ready_sequence"] == 1
    assert (await resolved(db, "ready", following))["ready_sequence"] == 1
    assert (await resolved(db, "ready", initial))["ready_sequence"] == 0
    assert len(await sla._rows(db, 33)) == 2


@pytest.mark.asyncio
async def test_resolver_cannot_use_new_timestamp_or_wrong_claim_as_an_origin_reset(db):
    initial = ready()
    await resolved(db, "ready", initial)
    await resolved(db, "started", started(initial))
    unproved = ready(at=T0+timedelta(seconds=80))
    unproved["after_claim_token"] = "wrong-generation"
    assert await resolved(db, "ready", unproved) is None
    assert len(await sla._rows(db, 33)) == 1
    from app.core.settings_models import SystemSetting
    marker = json.loads((await db.get(SystemSetting, sla.COVERAGE_PREFIX+"33")).value)
    assert marker["denominator_loss"]


@pytest.mark.asyncio
async def test_resolver_known_census_alias_is_read_only_and_does_not_downgrade_origin(db):
    event = ready()
    await resolved(db, "ready", event)
    before = (await sla._rows(db, 33))[0].value
    recovery = {**event, "origin_unknown": True, "recovery_only": True, "eligibility": {}}
    assert await resolved(db, "ready", recovery) is None
    assert (await sla._rows(db, 33))[0].value == before


@pytest.mark.asyncio
async def test_resolver_restart_durable_wait_due_can_create_unknown_next_step(db):
    initial = ready()
    await resolved(db, "ready", initial)
    await resolved(db, "started", started(initial))
    recovered = ready(at=T0+timedelta(seconds=400), origin_unknown=True)
    recovered.update(origin_state="WAIT_MATURITY", recovery_only=True, eligibility={})
    canonical = await resolved(db, "ready", recovered)
    assert canonical["ready_sequence"] == 1
    assert canonical["ready_at"] == recovered["ready_at"]
    values = [json.loads(item.value) for item in await sla._rows(db, 33)]
    assert any(item["origin_unknown"] and item["ready_sequence"] == 1 for item in values)


@pytest.mark.asyncio
async def test_resolver_start_reference_before_ready_is_unknown_without_inventing_origin(db):
    event = ready()
    first_start = started(event)
    first_start["message_text"] = "never persist this"
    assert await resolved(db, "started", first_start) is None
    assert not await sla._rows(db, 33)
    unknown = await sla._prefix_rows(db, sla.UNRESOLVED_PREFIX, 33, sla.MAX_UNRESOLVED)
    assert len(unknown) == 1 and "never persist this" not in unknown[0].value
    from app.core.settings_models import SystemSetting
    marker = json.loads((await db.get(SystemSetting, sla.COVERAGE_PREFIX+"33")).value)
    assert marker["unresolved_references"] == 1
    await resolved(db, "ready", event)
    value = json.loads((await sla._rows(db, 33))[0].value)
    assert value["ready_at"] == T0.isoformat()
    assert value["first_actual_step_at"] == (T0+timedelta(seconds=30)).isoformat()
    assert value["start_proof"]["claim_hash"] == __import__("hashlib").sha256(b"claim-a").hexdigest()
    assert not await sla._prefix_rows(db, sla.UNRESOLVED_PREFIX, 33, sla.MAX_UNRESOLVED)
    marker = json.loads((await db.get(SystemSetting, sla.COVERAGE_PREFIX+"33")).value)
    assert marker["unresolved_references"] == 0
    assert marker["gaps"]  # Out-of-order recovery cannot silently erase a gap.


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", ["MAX_ALIASES", "MAX_CURSORS", "MAX_UNRESOLVED"])
async def test_resolver_all_reference_caps_invalidate_coverage_without_evicting_pending(db, monkeypatch, cap):
    monkeypatch.setattr(sla, cap, 1)
    if cap == "MAX_UNRESOLVED":
        await resolved(db, "started", started(ready(1)))
        await resolved(db, "started", started(ready(2)))
        assert not await sla._rows(db, 33)
        assert len(await sla._prefix_rows(db, sla.UNRESOLVED_PREFIX, 33, 2)) == 1
    else:
        await resolved(db, "ready", ready(1))
        second = ready(2) if cap == "MAX_CURSORS" else ready(1, at=T0+timedelta(seconds=1))
        assert await resolved(db, "ready", second) is None
        assert len(await sla._rows(db, 33)) == 1
    from app.core.settings_models import SystemSetting
    marker = json.loads((await db.get(SystemSetting, sla.COVERAGE_PREFIX+"33")).value)
    assert marker["denominator_loss"]
    assert sla.evaluate([], {**coverage(), **marker}, NOW, START)["guaranteed_lower_bound"] is None


@pytest.mark.asyncio
async def test_resolver_prunes_only_links_whose_started_terminal_episode_retired(db, monkeypatch):
    old = NOW-timedelta(days=9)
    completed, pending = ready(1, at=old), ready(2, at=old)
    await resolved(db, "ready", completed)
    await resolved(db, "started", started(completed))
    await resolved(db, "finished", {**completed, "closed_at": (old+timedelta(seconds=200)).isoformat(), "outcome": "completed"})
    await resolved(db, "ready", pending)
    monkeypatch.setattr(sla, "MAX_ROWS", 2)
    await resolved(db, "ready", ready(3))
    assert await sla.prune_retired_links(db, 33) == 2
    aliases = await sla._prefix_rows(db, sla.ALIAS_PREFIX, 33, sla.MAX_ALIASES)
    assert {json.loads(value.value)["work_id"] for value in aliases} == {2, 3}
    assert {json.loads(value.value)["work_id"] for value in await sla._rows(db, 33)} == {2, 3}


@pytest.mark.asyncio
async def test_resolver_refuses_unknown_historical_ready_time(db):
    event = {**ready(), "origin_time_unknown": True}
    assert await resolved(db, "ready", event) is None
    assert not await sla._rows(db, 33)


@pytest.mark.asyncio
async def test_verified_provider_completion_can_wake_new_step_without_actor_claim(db):
    original = ready()
    await resolved(db, "ready", original)
    await resolved(db, "started", started(original))
    provider = ready(at=T0+timedelta(seconds=200))
    provider.update(origin_state="READY", dependency_ready_verified="provider_completed")
    canonical = await resolved(db, "ready", provider)
    assert canonical["ready_sequence"] == 1 and canonical["origin_unknown"]
    assert (await resolved(db, "ready", provider))["ready_sequence"] == 1
    assert len(await sla._rows(db, 33)) == 2


@pytest.mark.asyncio
async def test_provider_timer_is_not_a_verified_dependency_transition(db):
    original = ready()
    await resolved(db, "ready", original)
    await resolved(db, "started", started(original))
    timer = ready(at=T0+timedelta(seconds=200))
    timer.update(origin_state="WAIT_PROVIDER", dependency_ready_verified="provider_completed")
    assert await resolved(db, "ready", timer) is None
    assert len(await sla._rows(db, 33)) == 1
