"""P5 rollout: cohort math, fail-closed modes, gates, kill switch and lifecycle.

The release control plane is decision-only; these tests pin its contracts:

* stable cohort hash membership (expansion only adds members),
* the P5 mode truth table for the P2 lanes and pacing scope (fail closed),
* gate stop/hold/pass rules against baselines, trends and SLO limits,
* the kill switch: a stopped cohort receives no new ads/join tickets,
* deterministic canary selection with hard exclusions,
* the release lifecycle: freeze (immutable) -> activate -> gate -> rollback
  / completion, with audit rows for every observation window.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.control_models import ReconciliationTask
from app.core.account.models import TelegramAccount
from app.core.config import p5_cohort_rank, settings
from app.core.scheduler import growth_dispatch
from app.modules.acquisition import ad_fairness_pacing as pacing
from app.modules.acquisition import p5_rollout
from app.modules.acquisition.history_governance import run_pass
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, AutoJoinAttempt
from app.modules.acquisition.p2_models import GrowthOperation, GrowthOutbox
from app.modules.acquisition.p5_models import P5GateEvaluation, P5ReleaseRecord

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 24, 12, 0, 0)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCache:
    """Redis surface used by the observer state, windows and counters."""

    def __init__(self):
        self.store: dict = {}
        self.lists: dict = {}

    async def get(self, key, *args, **kwargs):
        return self.store.get(key)

    async def set(self, key, value, *args, **kwargs):
        self.store[key] = value
        return True

    async def delete(self, key, *args, **kwargs):
        return 1 if self.store.pop(key, None) is not None else 0

    async def hgetall(self, key, *args, **kwargs):
        return {}

    async def rpush(self, key, value, *args, **kwargs):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end, *args, **kwargs):
        return True

    async def expire(self, key, seconds, *args, **kwargs):
        return True

    async def lrange(self, key, start, end, *args, **kwargs):
        return list(self.lists.get(key, []))


class DispatchRedis(FakeCache):
    """The dispatcher's Redis surface: tickets, due zsets and the p5 keys."""

    def __init__(self):
        super().__init__()
        self.zsets: dict = {}
        self.tickets: dict = {}
        self.counters: dict = {}

    async def incr(self, key, *args, **kwargs):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def hset(self, key, field, value, *args, **kwargs):
        self.store.setdefault(key, {})[field] = value
        return 1

    async def zadd(self, key, mapping, nx=False, *args, **kwargs):
        members = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            if nx and member in members:
                continue
            members[member] = score
        return len(mapping)

    async def zrange(self, key, start=0, end=-1, *args, **kwargs):
        return list(self.zsets.get(key, {}))

    async def zrem(self, key, *members):
        members_set = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if member in members_set:
                del members_set[member]
                removed += 1
        return removed

    async def zmscore(self, key, members, *args, **kwargs):
        return [self.zsets.get(key, {}).get(member) for member in members]

    async def eval(self, script, numkeys, *args):
        if "ZADD', KEYS[4], ARGV[3]" in script:
            # FINISH: release the stage ticket and return the next due time.
            key = args[0]
            if key in self.tickets:
                del self.tickets[key]
                return 1
            return 0
        if "~= 'q:'" in script:
            # START: the ticket exists only after dispatch reserved it.
            return 1 if args[0] in self.tickets else 0
        if "ZREMRANGEBYSCORE" in script:
            # RESERVE: first claim wins, capacity is unbounded in tests.
            key = args[0]
            if key in self.tickets:
                return 0
            self.tickets[key] = "reserved"
            return 1
        if "DEL" in script:
            # FINISH clears the stage ticket; UNLOCK clears a lock value.
            key = args[0]
            if key in self.tickets:
                del self.tickets[key]
                return 1
            if self.store.get(key) == args[-1]:
                del self.store[key]
                return 1
            return 0
        return 0


class FakeBroker:
    def __init__(self):
        self.depth = 0

    async def llen(self, *args, **kwargs):
        return self.depth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _account(db, account_id, *, risk_pause_until=None, risk_level="normal",
                   is_active=True, risk_reason=None):
    account = TelegramAccount(
        id=account_id,
        identifier=f"p5-{account_id}",
        session_name=f"p5-{account_id}",
        is_active=is_active,
        status="online",
        risk_level=risk_level,
        risk_score=0.0,
        risk_pause_until=risk_pause_until,
        risk_reason=risk_reason,
    )
    db.add(account)
    await db.flush()
    return account


async def _campaign(db, campaign_id=77):
    row = AdCampaign(id=campaign_id, name="p5-rollout")
    db.add(row)
    await db.flush()
    return row


def _ad(db, account_id, *, status="pending", created=None, campaign_id=77,
        survival_status="not_required"):
    db.add(AdDeliveryLog(
        account_id=account_id,
        telegram_group_id=-100000 + account_id,
        ad_campaign_id=campaign_id,
        status=status,
        survival_status=survival_status,
        survival_stage="two_minute",
        created_at=created or (NOW - timedelta(hours=2)),
        sent_at=None,
        telegram_message_id=None,
    ))


async def _freeze(db, release_id="p5-test-r1", *, mode=None, canary="5",
                  percent=0, p2_enabled=True):
    if mode is not None:
        settings.P5_ROLLOUT_MODE = mode
    settings.P5_CANARY_ACCOUNT_IDS = canary
    settings.P5_COHORT_PERCENT = percent
    settings.P5_RELEASE_ID = release_id
    settings.P2_OUTBOX_ENABLED = p2_enabled
    record = await p5_rollout.freeze_release_candidate(
        db,
        release_id=release_id,
        image_digest="sha256:test",
        migration_revision="052_p5_release_control",
        now=NOW,
    )
    await db.commit()
    return record


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """Every test starts from the fail-closed P5 defaults."""
    defaults = {
        "P5_ROLLOUT_MODE": "off",
        "P5_CANARY_ACCOUNT_IDS": "",
        "P5_COHORT_PERCENT": 0,
        "P5_HISTORY_APPLY": False,
        "P5_RELEASE_ID": "",
        "P2_OUTBOX_ENABLED": False,
        "P2_JOIN_OUTBOX_ENABLED": False,
        "P2_AD_OUTBOX_ENABLED": False,
        "P2_ACCOUNT_IDS": "",
        "P2_SHADOW_MODE": True,
        "P4_AD_PACING": False,
        "P4_AD_PACING_SHADOW": False,
        "P4_AD_PACING_ACCOUNT_IDS": "",
        "P4_ADMIN_METRICS": False,
        "P4_HISTORY_GOVERNANCE": False,
        "P4_HISTORY_GOVERNANCE_DRY_RUN": True,
        "BUDGET_LEDGER_ENABLED": False,
        "P3_QUALIFICATION_SNAPSHOT": False,
    }
    for key, value in defaults.items():
        monkeypatch.setattr(settings, key, value)
    yield


def _clean_sample(**overrides):
    sample = {
        "at": NOW.timestamp(),
        "cohort_account_ids": [5],
        "ad_success_rate_24h": 0.9,
        "ad_unresolved_aged": 0,
        "ad_unresolved_inflight": 0,
        "survival_backlog": 0,
        "survival_overdue": 0,
        "join_unknown": 0,
        "parent_group_invariant_failures": 0,
        "reconciliation_oldest_age_seconds": 0,
        "reconciliation_unexplained": 0,
        "illegal_transitions": 0,
        "orphan_outbox": 0,
        "duplicate_active_operations": 0,
        "outbox_oldest_age_seconds": 0,
        "receipt_p95_24h": 100.0,
        "floodwait_events_24h": 0,
        "read_factor_avg": 1.0,
        "fair_share_max_ratio": 1.0,
        "cohort_bypass_accounts": 0,
        "ads_oldest_dispatch_wait_seconds": 0,
        "session_lease_contention_total": 10,
        "duplicate_targets_total": 0,
        "infra_ready": True,
    }
    sample.update(overrides)
    return sample


def _baseline(**overrides):
    baseline = {
        "receipt_p95_24h": 100.0,
        "outbox_oldest_age_seconds": 60.0,
        "survival_backlog": 10.0,
        "ad_success_rate_24h": 0.95,
        "floodwait_events_24h": 2,
        "read_factor_avg": 1.0,
        "session_lease_contention_total": 10,
    }
    baseline.update(overrides)
    return baseline


def _samples(*vectors):
    return [_clean_sample(**overrides) for overrides in vectors]


# ---------------------------------------------------------------------------
# Cohort math and mode truth table
# ---------------------------------------------------------------------------


def test_cohort_rank_is_stable_and_growth_only_adds_members():
    assert p5_cohort_rank(5) == p5_cohort_rank(5)
    ranks = {account_id: p5_cohort_rank(account_id) for account_id in range(1, 200)}
    for percent in range(0, 101):
        members = {a for a, r in ranks.items() if r < percent}
        bigger = {a for a, r in ranks.items() if r < min(100, percent + 1)}
        assert members <= bigger
    assert {a for a, r in ranks.items() if r < 100} == set(ranks)


def test_p2_scoping_truth_table(monkeypatch):
    monkeypatch.setattr(settings, "P2_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P2_AD_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P2_JOIN_OUTBOX_ENABLED", True)
    # mode off keeps the pre-P5 baseline in charge (transparent).
    monkeypatch.setattr(settings, "P2_ACCOUNT_IDS", "34")
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "off")
    assert settings.p2_outbox_enabled_for("send_ad", 34)
    assert not settings.p2_outbox_enabled_for("send_ad", 35)
    # shadow is record-only: nobody runs the release unit.
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "shadow")
    assert not settings.p2_outbox_enabled_for("send_ad", 34)
    # canary with an empty allowlist fails closed.
    monkeypatch.setattr(settings, "P5_CANARY_ACCOUNT_IDS", "")
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "canary")
    assert not settings.p2_outbox_enabled_for("send_ad", 34)
    # canary governs the scope and replaces the underlying allowlist.
    monkeypatch.setattr(settings, "P5_CANARY_ACCOUNT_IDS", "5")
    assert settings.p2_outbox_enabled_for("join_group", 5)
    assert not settings.p2_outbox_enabled_for("join_group", 34)
    # cohort percent 0 keeps only the canary member.
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "cohort")
    assert settings.p2_outbox_enabled_for("send_ad", 5)
    # percent 100 includes every account, and masters still rule.
    monkeypatch.setattr(settings, "P5_COHORT_PERCENT", 100)
    assert settings.p2_outbox_enabled_for("send_ad", 34)
    monkeypatch.setattr(settings, "P2_AD_OUTBOX_ENABLED", False)
    assert not settings.p2_outbox_enabled_for("send_ad", 34)
    # full mode is unrestricted but never bypasses a disabled master.
    monkeypatch.setattr(settings, "P2_AD_OUTBOX_ENABLED", True)
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "full")
    assert settings.p2_outbox_enabled_for("send_ad", 34)


def test_cohort_membership_keeps_canary_at_every_percent(monkeypatch):
    monkeypatch.setattr(settings, "P5_CANARY_ACCOUNT_IDS", "5")
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "cohort")
    for percent in (0, 25, 50, 100):
        monkeypatch.setattr(settings, "P5_COHORT_PERCENT", percent)
        assert settings.p5_cohort_member(5)


def test_rollout_mode_rejects_unknown_values(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "bogus")
    with pytest.raises(ValidationError):
        settings.__class__(P5_ROLLOUT_MODE=settings.P5_ROLLOUT_MODE)
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "off")


def test_pacing_scope_follows_the_p5_mode(monkeypatch):
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "off")
    monkeypatch.setattr(settings, "P4_AD_PACING_ACCOUNT_IDS", "9")
    assert pacing._p5_pacing_scope([1, 2, 9]) == {9}
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "canary")
    monkeypatch.setattr(settings, "P5_CANARY_ACCOUNT_IDS", "")
    assert pacing._p5_pacing_scope([1, 2]) is None  # fail closed
    monkeypatch.setattr(settings, "P5_CANARY_ACCOUNT_IDS", "1")
    assert pacing._p5_pacing_scope([1, 2]) == {1}
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "cohort")
    monkeypatch.setattr(settings, "P5_COHORT_PERCENT", 100)
    assert pacing._p5_pacing_scope([1, 2]) == {1, 2}
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "full")
    assert pacing._p5_pacing_scope([1, 2]) == set()  # empty set = everyone


async def test_pacing_fail_closed_keeps_legacy_order(test_db, monkeypatch):
    from app.modules.acquisition.p4_models import GrowthPacingDecision

    monkeypatch.setattr(settings, "P4_AD_PACING", True)
    monkeypatch.setattr(settings, "P4_AD_PACING_SHADOW", False)
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "canary")
    monkeypatch.setattr(settings, "P5_CANARY_ACCOUNT_IDS", "")
    order = await pacing.apply_to_dispatch(
        test_db, [11, 12], NOW.timestamp(), window_seconds=60
    )
    assert order == [11, 12]
    row = await test_db.scalar(
        select(GrowthPacingDecision).order_by(GrowthPacingDecision.id.desc())
    )
    assert row is not None
    assert row.applied is False
    await test_db.rollback()


# ---------------------------------------------------------------------------
# Gate rules
# ---------------------------------------------------------------------------


def _evaluate(baseline, samples, **overrides):
    current = _clean_sample(**overrides)
    return p5_rollout.evaluate_gate(
        baseline=baseline,
        samples=samples,
        current=current,
        stage_age_seconds=7200,
        stage_min_seconds=7200,
    )


def test_gate_passes_on_a_clean_window():
    result = _evaluate(_baseline(), _samples({}, {}, {}))
    assert result.result == "pass"
    assert result.reasons == []


def test_gate_stops_on_session_lease_contention():
    result = _evaluate(_baseline(), _samples({}, {}), session_lease_contention_total=11)
    assert result.result == "stop"
    assert "session_lease_contention" in [r.code for r in result.reasons]


def test_gate_stops_on_parent_group_invariant_and_aged_ads():
    assert _evaluate(_baseline(), [], parent_group_invariant_failures=1).result == "stop"
    assert _evaluate(_baseline(), [], ad_unresolved_aged=2).result == "stop"


def test_gate_stops_on_join_unknown_growth_over_two_consecutive_windows():
    rising = _samples(
        {"join_unknown": 1, "session_lease_contention_total": 10},
        {"join_unknown": 3, "session_lease_contention_total": 10},
        {"join_unknown": 7, "session_lease_contention_total": 10},
    )
    assert _evaluate(_baseline(), rising).result == "stop"
    single = _samples(
        {"join_unknown": 1, "session_lease_contention_total": 10},
        {"join_unknown": 3, "session_lease_contention_total": 10},
    )
    result = _evaluate(_baseline(), single)
    assert "join_unknown_growth" not in [r.code for r in result.reasons]


def test_gate_stops_on_invariant_breaches_and_infra_and_bypass():
    for code, override in (
        ("illegal_state_transition", {"illegal_transitions": 1}),
        ("orphan_outbox", {"orphan_outbox": 1}),
        ("duplicate_active_operation", {"duplicate_active_operations": 1}),
        ("reconciliation_unexplained_aging", {"reconciliation_unexplained": 1}),
        ("infra_unavailable", {"infra_ready": False}),
        ("cohort_allowlist_bypass", {"cohort_bypass_accounts": 1}),
    ):
        result = _evaluate(_baseline(), [], **override)
        assert result.result == "stop", code
        assert code in [r.code for r in result.reasons]


def test_gate_holds_on_baseline_regressions():
    result = _evaluate(_baseline(), [], receipt_p95_24h=120.0)  # +20%
    assert result.result == "hold"
    codes = [r.code for r in result.reasons]
    assert "receipt_p95_regression" in codes
    result = _evaluate(_baseline(), [], ad_success_rate_24h=0.85)  # -10pp
    assert "ad_success_rate_drop" in [r.code for r in result.reasons]
    result = _evaluate(_baseline(), [], floodwait_events_24h=3)  # +50%
    assert "floodwait_increase" in [r.code for r in result.reasons]
    result = _evaluate(_baseline(), [], read_factor_avg=0.7)  # -30%
    assert "read_budget_consumption_increase" in [r.code for r in result.reasons]
    result = _evaluate(_baseline(), [], ads_oldest_dispatch_wait_seconds=2000)
    assert "account_starvation" in [r.code for r in result.reasons]
    result = _evaluate(_baseline(), [], fair_share_max_ratio=2.5)
    assert "fair_share_exceeded" in [r.code for r in result.reasons]


def test_gate_holds_on_rising_trends_and_missing_baseline_skips_relative():
    rising_deltas = _samples(
        {"duplicate_targets_total": 0, "ads_oldest_dispatch_wait_seconds": 10,
         "survival_backlog": 1, "session_lease_contention_total": 10},
        {"duplicate_targets_total": 1, "ads_oldest_dispatch_wait_seconds": 20,
         "survival_backlog": 2, "session_lease_contention_total": 10},
        {"duplicate_targets_total": 3, "ads_oldest_dispatch_wait_seconds": 40,
         "survival_backlog": 4, "session_lease_contention_total": 10},
        {"duplicate_targets_total": 7, "ads_oldest_dispatch_wait_seconds": 90,
         "survival_backlog": 8, "session_lease_contention_total": 10},
    )
    result = _evaluate(_baseline(), rising_deltas)
    codes = [r.code for r in result.reasons]
    assert "duplicate_target_rising" in codes
    assert "queue_age_rising" in codes
    assert "survival_backlog_rising" in codes
    # Without pre-release data a relative comparison cannot fire.
    empty_baseline = _baseline(receipt_p95_24h=None, outbox_oldest_age_seconds=0,
                               survival_backlog=0, ad_success_rate_24h=None,
                               floodwait_events_24h=0, read_factor_avg=None)
    result = _evaluate(empty_baseline, [])
    assert result.result in {"pass", "hold"}
    assert "receipt_p95_regression" not in [r.code for r in result.reasons]


def test_gate_hold_for_stage_min_observation_and_stop_beats_hold():
    result = p5_rollout.evaluate_gate(
        baseline=_baseline(), samples=[], current=_clean_sample(),
        stage_age_seconds=600, stage_min_seconds=7200,
    )
    assert result.result == "hold"
    assert "stage_min_observation_not_met" in [r.code for r in result.reasons]
    combined = _evaluate(_baseline(), [], ad_unresolved_aged=1)
    assert combined.result == "stop"
    assert any(r.severity == "stop" for r in combined.reasons)


def test_gate_reads_limits_from_the_existing_slo_table():
    from app.core.metrics import SLO

    assert SLO["session_lease_contention_15m_limit"] == 0
    assert SLO["parent_group_invariant_failures_limit"] == 0
    assert SLO["ad_unknown_warning_seconds"] == 900
    assert SLO["reconciliation_critical_age_seconds"] == 1800


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


async def test_stopped_cohort_is_blocked_from_new_protected_tickets():
    cache = FakeCache()
    await cache.set(p5_rollout.STOPPED_KEY, json.dumps({
        "release_id": "p5-test-r1",
        "reasons": ["ad_unresolved_aged"],
        "cohort_account_ids": [5],
    }))
    assert await p5_rollout.dispatch_blocked_accounts(cache) == {5}
    assert await p5_rollout.ticket_blocked(cache, "ads", 5)
    assert await p5_rollout.ticket_blocked(cache, "join", 5)
    # Maintenance lanes keep running for the stopped cohort.
    assert not await p5_rollout.ticket_blocked(cache, "survival", 5)
    assert not await p5_rollout.ticket_blocked(cache, "reconcile", 5)
    # Other accounts are untouched.
    assert not await p5_rollout.ticket_blocked(cache, "ads", 6)


async def test_dispatch_removes_stopped_cohort_from_protected_lanes(
    test_db, monkeypatch
):
    async def _noop(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "app.core.account.backlog_discard.maybe_sweep_ineligible_backlog", _noop
    )
    monkeypatch.setattr("app.modules.acquisition.qualification_recovery.repair", _noop)

    async def _sets(db, now):
        return {"ads": [5, 6], "join": [5, 6], "review": [], "exit": [],
                "verify": [], "survival": [5], "reconcile": [5]}

    # account_sets is imported inside dispatch(); patch the module symbol it
    # resolves through.
    monkeypatch.setattr(
        "app.core.scheduler.growth_dispatch.account_sets", _sets
    )
    await _account(test_db, 5)
    await _account(test_db, 6)
    await test_db.commit()

    client = DispatchRedis()
    await client.set(p5_rollout.STOPPED_KEY, json.dumps({
        "release_id": "p5-test-r1", "reasons": ["x"], "cohort_account_ids": [5],
    }))
    published: list[tuple] = []

    async def publish(stage, account_id, token, queue):
        published.append((stage, int(account_id)))

    result = await growth_dispatch.dispatch(
        test_db, client, FakeBroker(), publish, now=NOW.timestamp()
    )
    stages = {}
    for stage, account_id in published:
        stages.setdefault(stage, set()).add(account_id)
    assert stages.get("ads") == {6}
    assert stages.get("join") == {6}
    # Maintenance lanes are never filtered by the kill switch.
    assert stages.get("survival") == {5}
    assert stages.get("reconcile") == {5}
    assert result["enqueued"] == len(published)


async def test_run_ticket_skips_execution_when_stopped(monkeypatch):
    client = DispatchRedis()
    await client.set(p5_rollout.STOPPED_KEY, json.dumps({
        "release_id": "p5-test-r1", "reasons": ["x"], "cohort_account_ids": [5],
    }))
    # The dispatcher reserved this ticket before the stop engaged.
    client.tickets[growth_dispatch.ticket_key("ads", 5)] = "reserved"
    client.tickets[growth_dispatch.ticket_key("survival", 5)] = "reserved"

    async def fake_redis():
        return client

    async def fake_run_with_db(handler):
        raise AssertionError("execution must not start while stopped")

    monkeypatch.setattr("app.core.redis.get_redis", fake_redis)
    monkeypatch.setattr(
        "app.core.scheduler.tasks._run_with_db", fake_run_with_db
    )
    result = await growth_dispatch.run_ticket("ads", 5, "tok")
    assert result["reason"] == "p5_rollout_stopped"
    assert result["skipped"] == 1
    # The ticket is returned for a bounded retry once the stop is cleared.
    assert not client.tickets.get(growth_dispatch.ticket_key("ads", 5))
    live = await growth_dispatch.run_ticket("survival", 5, "tok")
    assert live.get("reason") != "p5_rollout_stopped"


# ---------------------------------------------------------------------------
# Canary selection
# ---------------------------------------------------------------------------


async def test_canary_selection_is_deterministic_and_excludes_unhealthy(test_db):
    await _campaign(test_db)
    await _account(test_db, 1)  # healthy
    await _account(test_db, 2, risk_pause_until=NOW + timedelta(hours=1))
    await _account(test_db, 3)
    await _account(test_db, 4)
    await _account(test_db, 5)
    _ad(test_db, 3, created=NOW - timedelta(hours=2))  # aged pending
    db_add_join_unknown(test_db, 4)
    test_db.add(ReconciliationTask(
            task_key="sel:1", kind="ad", source_id=1, state="pending", account_id=5,
        available_at=NOW, first_discovered_at=NOW,
    ))
    test_db.add(GrowthOutbox(
        operation_id=999, kind="send_ad", account_id=2, state="leased",
        lease_owner="w", lease_expires_at=NOW - timedelta(minutes=5),
        available_at=NOW, payload_version=1, attempts=1,
    ))
    await test_db.commit()

    first = await p5_rollout.select_canary_account(test_db, NOW)
    second = await p5_rollout.select_canary_account(test_db, NOW)
    assert first.account_id == second.account_id
    assert first.account_id == 1
    excluded = {
        entry["account_id"]: entry["excluded"]
        for entry in first.report["candidates"]
    }
    assert excluded[2] in {"gated_risk_pause", "lease_contention_evidence"}
    assert excluded[3] == "ad_unresolved"
    assert excluded[4] == "join_outcome_unknown"
    assert excluded[5] == "reconciliation_backlog"
    assert excluded[1] is None


def db_add_join_unknown(db, account_id):
    db.add(AutoJoinAttempt(
        account_id=account_id,
        group_id=1,
        target_key=f"join:{account_id}",
        status="pending",
        request_state="outcome_unknown",
        reconciliation_status="pending",
        reservation_key=f"rk-{account_id}",
    ))


async def test_canary_selection_orders_by_health_and_fails_closed(test_db):
    await _campaign(test_db)
    await _account(test_db, 7)
    await _account(test_db, 8, risk_pause_until=NOW + timedelta(hours=1))
    await test_db.commit()
    pick = await p5_rollout.select_canary_account(test_db, NOW)
    assert pick.account_id == 7
    # A risk-paused-only fleet fails closed instead of picking someone.
    test_db.expire_all()
    for account_id in (7, 8):
        account = await test_db.get(TelegramAccount, account_id)
        account.risk_pause_until = NOW + timedelta(hours=1)
    await test_db.commit()
    pick = await p5_rollout.select_canary_account(test_db, NOW)
    assert pick.account_id is None
    assert pick.reason == "no_healthy_canary_candidate"


# ---------------------------------------------------------------------------
# Release lifecycle
# ---------------------------------------------------------------------------


async def test_release_activates_samples_and_persists_audit(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await test_db.commit()
    release = await _freeze(test_db, mode="canary")
    cache = FakeCache()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    assert summary["mode"] == "canary"
    assert summary["cohort_size"] == 1
    assert summary["result"] in {"pass", "hold"}
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    assert record.status == "active"
    assert record.started_at == NOW
    assert json.loads(record.baseline_json)["cohort_account_ids"] == [5]
    assert json.loads(record.cohort_account_ids_json) == [5]
    stages = json.loads(record.stage_history_json)
    assert stages[-1]["stage"] == "canary"
    evaluations = (
        await test_db.scalars(select(P5GateEvaluation))
    ).all()
    assert len(evaluations) == 1
    assert evaluations[0].release_id == "p5-test-r1"
    assert evaluations[0].result == summary["result"]
    assert evaluations[0].cohort_account_ids_json == "[5]"


async def test_gate_hard_stop_engages_kill_switch_and_rollback(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    _ad(test_db, 5, created=NOW - timedelta(hours=2))  # aged pending ad
    await test_db.commit()
    release = await _freeze(test_db, mode="canary")
    cache = FakeCache()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    assert summary["result"] == "stop"
    stopped = await p5_rollout.read_stopped(cache)
    assert stopped["cohort_account_ids"] == [5]
    assert "ad_unresolved_aged" in stopped["reasons"]
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    assert record.status == "rolled_back"
    assert record.rollback_at == NOW
    assert "ad_unresolved_aged" in record.rollback_reason
    assert json.loads(record.affected_cohort_json) == [5]
    evaluations = (
        await test_db.scalars(select(P5GateEvaluation))
    ).all()
    assert evaluations[-1].result == "stop"
    # The blocked cohort is exactly the release cohort.
    assert await p5_rollout.dispatch_blocked_accounts(cache) == {5}


async def test_mode_off_rolls_back_the_active_release(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await test_db.commit()
    release = await _freeze(test_db, mode="canary")
    cache = FakeCache()
    await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    settings.P5_ROLLOUT_MODE = "off"
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW + timedelta(minutes=2))
    assert summary["result"] == "off"
    # Turning P5 off must not block the legacy dispatcher.
    assert await p5_rollout.read_stopped(cache) is None
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    assert record.status == "rolled_back"
    assert record.rollback_at is not None
    # Repeated off ticks do not rewrite the rollback evidence.
    rollback_at = record.rollback_at
    await p5_rollout.observe_cycle(test_db, cache, now=NOW + timedelta(minutes=3))
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    assert record.rollback_at == rollback_at


async def test_governing_without_a_frozen_record_fails_closed(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await test_db.commit()
    settings.P5_ROLLOUT_MODE = "canary"
    settings.P5_CANARY_ACCOUNT_IDS = "5"
    settings.P5_RELEASE_ID = "ghost-release"
    cache = FakeCache()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW)
    assert summary["result"] == "stop"
    stopped = await p5_rollout.read_stopped(cache)
    assert stopped["reasons"] == ["p5_release_record_missing"]
    assert stopped["cohort_account_ids"] == [5]


async def test_new_release_clears_the_stop_and_records_recovery(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await test_db.commit()
    release = await _freeze(test_db, mode="canary")
    cache = FakeCache()
    await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    settings.P5_ROLLOUT_MODE = "off"
    await p5_rollout.observe_cycle(test_db, cache, now=NOW)
    assert await p5_rollout.read_stopped(cache) is None

    release_two = await _freeze(test_db, release_id="p5-test-r2", mode="canary")
    summary = await p5_rollout.observe_cycle(
        test_db, cache, now=NOW + timedelta(hours=1), infra_ready=True
    )
    assert summary["result"] in {"pass", "hold"}
    assert await p5_rollout.read_stopped(cache) is None
    await test_db.commit()
    first = await test_db.get(P5ReleaseRecord, release.id)
    # ``mode=off`` clears the kill switch without creating a stopped cohort,
    # so a later release has no P5 stop to recover.
    assert first.recovered_at is None
    second = await test_db.get(P5ReleaseRecord, release_two.id)
    assert second.status == "active"


async def test_full_stage_completes_after_the_stability_window(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await _account(test_db, 6)
    await test_db.commit()
    release = await _freeze(test_db, mode="full", canary="", percent=100)
    cache = FakeCache()
    # Prime the stage history with a 25-hour-old full stage.
    settings.P5_ROLLOUT_MODE = "full"
    settings.P5_COHORT_PERCENT = 100
    cohort = sorted([5, 6])
    release.stage_history_json = json.dumps([{
        "stage": "full", "cohort_account_ids": cohort,
        "started_at": (NOW - timedelta(hours=25)).isoformat(), "percent": 100,
    }])
    release.cohort_account_ids_json = json.dumps(cohort)
    release.baseline_json = json.dumps(await p5_rollout.compute_baseline(
        test_db, cache, cohort, NOW
    ))
    await test_db.commit()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    assert summary["result"] == "pass"
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    assert record.status == "completed"
    assert record.completed_at == NOW


async def test_canary_without_resolved_ids_holds_and_records_the_pick(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await test_db.commit()
    release = await _freeze(test_db, mode="canary", canary="")
    cache = FakeCache()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    assert summary["result"] == "hold"
    assert "canary_unresolved" in summary["reasons"]
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    # The cohort stays empty (fail closed) but the auto-selection is audited.
    assert json.loads(record.cohort_account_ids_json) == []
    assert record.selected_canary_account_id == 5


async def test_cohort_bypass_stops_the_release(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await _account(test_db, 7)
    release = await _freeze(test_db, mode="canary")
    release.status = "active"
    release.started_at = NOW - timedelta(minutes=10)
    release.cohort_account_ids_json = json.dumps([5])
    release.baseline_json = json.dumps(await p5_rollout.compute_baseline(
        test_db, FakeCache(), [5], NOW
    ))
    test_db.add(GrowthOperation(
        kind="send_ad", idempotency_key="bypass-1", account_id=7, target_key="g:x",
        snapshot_id="s", group_version="1", rule_version="1", evidence_hash="e",
        budget_reservation_id="r1", payload_json="{}", payload_hash="h",
        state="reserved", state_version=0, reserved_at=NOW,
    ))
    await test_db.commit()
    cache = FakeCache()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW, infra_ready=True)
    assert summary["result"] == "stop"
    assert "cohort_allowlist_bypass" in summary["reasons"]


async def test_shadow_mode_previews_without_activating(test_db):
    await _campaign(test_db)
    await _account(test_db, 5)
    await test_db.commit()
    release = await _freeze(test_db, mode="shadow")
    cache = FakeCache()
    summary = await p5_rollout.observe_cycle(test_db, cache, now=NOW)
    assert summary["selected_canary"] == 5
    assert summary["cohort_preview"] == []
    await test_db.commit()
    record = await test_db.get(P5ReleaseRecord, release.id)
    assert record.status == "frozen"
    assert record.started_at is None
    assert await p5_rollout.read_stopped(cache) is None


async def test_freeze_is_immutable_per_release_id(test_db):
    first = await _freeze(test_db, release_id="p5-immutable")
    again = await _freeze(test_db, release_id="p5-immutable")
    assert first.id == again.id
    settings.P5_RELEASE_ID = "p5-immutable"
    settings.P2_OUTBOX_ENABLED = True
    with pytest.raises(ValueError):
        await p5_rollout.freeze_release_candidate(
            test_db, release_id="p5-immutable",
            image_digest="sha256:different", migration_revision="x",
        )


# ---------------------------------------------------------------------------
# History governance stays dry-run during P5
# ---------------------------------------------------------------------------


async def test_history_governance_requires_explicit_p5_apply(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P4_HISTORY_GOVERNANCE", True)
    monkeypatch.setattr(settings, "P4_HISTORY_GOVERNANCE_DRY_RUN", False)
    monkeypatch.setattr(settings, "P5_HISTORY_APPLY", False)
    monkeypatch.setattr(settings, "P5_ROLLOUT_MODE", "shadow")
    first = await run_pass(test_db, now=NOW)
    assert first["skipped"] == "dry_run_mode"
    assert "archive" not in first
    again = await run_pass(test_db, now=NOW + timedelta(minutes=1))
    assert again["skipped"] == "dry_run_mode"
    # The explicit post-observation approval is the only path to real archiving.
    monkeypatch.setattr(settings, "P5_HISTORY_APPLY", True)
    approved = await run_pass(test_db, now=NOW + timedelta(minutes=2))
    assert "archive" in approved


# ---------------------------------------------------------------------------
# Redis key contract with the dispatcher
# ---------------------------------------------------------------------------


def test_growth_last_runs_key_matches_the_dispatcher_prefix():
    assert (
        p5_rollout.GROWTH_LAST_RUNS_KEY
        == growth_dispatch.PREFIX + "last_runs"
    )
