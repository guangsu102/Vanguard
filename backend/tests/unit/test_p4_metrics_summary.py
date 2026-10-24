"""P4 admin metrics: label safety, SLO targets, aggregate gauges and summary API."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.core import metrics
from app.core.config import settings
from app.modules.acquisition.p4_models import GrowthPacingDecision

NOW = datetime(2026, 10, 22, 12, 0, 0)


class FakeCache:
    def __init__(self):
        self.store: dict = {}

    async def get(self, key, *args, **kwargs):
        return self.store.get(key)

    async def set(self, key, value, *args, **kwargs):
        self.store[key] = value
        return True

    async def hgetall(self, key, *args, **kwargs):
        return {}

    async def zrange(self, *args, **kwargs):
        return []

    async def zrangebyscore(self, *args, **kwargs):
        return []

    async def zcount(self, *args, **kwargs):
        return 0

    async def zmscore(self, *args, **kwargs):
        return []

    async def llen(self, *args, **kwargs):
        return 0


def test_p4_gauge_label_safety_accepts_only_positive_account_ids():
    for name in (
        "vanguard_p4_pacing_score",
        "vanguard_p4_fair_share",
        "vanguard_p4_actual_share",
        "vanguard_p4_read_recovery_factor",
        "vanguard_p4_account_risk_level",
    ):
        assert metrics._safe_gauge_labels(name, ["42"])
        assert not metrics._safe_gauge_labels(name, ["0"])
        assert not metrics._safe_gauge_labels(name, ["-1"])
        assert not metrics._safe_gauge_labels(name, ["group:-100123"])
        assert not metrics._safe_gauge_labels(name, ["a:b"])
        assert not metrics._safe_gauge_labels(name, [])
        assert not metrics._safe_gauge_labels(name, ["1", "2"])


def test_p4_counter_label_safety():
    assert metrics._safe_counter_labels(
        "vanguard_p4_pacing_decisions_total", ["ads", "selected"]
    )
    assert not metrics._safe_counter_labels(
        "vanguard_p4_pacing_decisions_total", ["join", "selected"]
    )
    assert not metrics._safe_counter_labels(
        "vanguard_p4_pacing_decisions_total", ["ads", "gated_survival_backlog", "extra"]
    )
    assert not metrics._safe_counter_labels(
        "vanguard_p4_pacing_decisions_total", ["ads", "gated_new_reason"]
    )
    assert metrics._safe_counter_labels(
        "vanguard_p4_duplicate_targets_total", ["send_ad"]
    )
    assert not metrics._safe_counter_labels(
        "vanguard_p4_duplicate_targets_total", ["send"]
    )
    assert metrics._safe_counter_labels(
        "vanguard_p4_history_batches_total", ["dry_run", "succeeded"]
    )
    assert not metrics._safe_counter_labels(
        "vanguard_p4_history_batches_total", ["archive", "running"]
    )


async def test_record_functions_validate_labels(monkeypatch):
    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", True)
    with pytest.raises(ValueError):
        await metrics.record_pacing_decision("ads", "gated_unknown_reason")
    with pytest.raises(ValueError):
        await metrics.record_pacing_decision("join", "selected")
    with pytest.raises(ValueError):
        await metrics.record_duplicate_target("send")
    with pytest.raises(ValueError):
        await metrics.record_history_batch("archive", "running")
    with pytest.raises(ValueError):
        await metrics.record_receipt_latency(-5)
    # Valid labels never raise even without Redis.
    await metrics.record_pacing_decision("ads", "selected")
    await metrics.record_duplicate_target("send_ad")
    await metrics.record_history_batch("dry_run", "succeeded")
    await metrics.record_receipt_latency(12.5)


async def test_record_functions_off_without_flag(monkeypatch):
    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", False)
    await metrics.record_pacing_decision("join", "gated_unknown_reason")  # no validation
    await metrics.record_duplicate_target("anything")


def test_slo_includes_p4_targets():
    assert metrics.SLO["p4_receipt_latency_p95_seconds"] == 900
    assert metrics.SLO["p4_duplicate_target_rate_limit"] == 0.05
    assert metrics.SLO["p4_ad_outbox_oldest_seconds"] == 1800
    assert metrics.SLO["p4_history_cleanup_failure_limit"] == 0


async def test_aggregate_emits_p4_gauges(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", True)
    cache = FakeCache()
    result = await metrics.aggregate(test_db, cache)
    names = {(gauge["name"], tuple(gauge["labels"])) for gauge in result["gauges"]}
    assert ("vanguard_p4_admin_metrics_enabled", ()) in names
    assert ("vanguard_p4_survival_backlog", ()) in names
    assert ("vanguard_p4_ad_outbox_pending", ()) in names
    assert ("vanguard_p4_history_eligible_operations", ()) in names
    assert result["p4"]["enabled"] is True
    # The cached status exposes the same section for the operations status API.
    cached = await metrics.cached_status(cache)
    assert cached["p4"]["enabled"] is True


async def test_aggregate_emits_per_account_gauges_from_latest_decision(test_db, monkeypatch):
    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", True)
    from app.core.account.models import TelegramAccount

    test_db.add(TelegramAccount(id=5, identifier="m-5", session_name="m-5",
                                risk_level="watch"))
    test_db.add(GrowthPacingDecision(
        stage="ads", mode="pacing", window_started_at=NOW, window_seconds=60,
        candidate_count=1, gated_count=0, scheduled_count=1,
        selected_account_id=5, applied=True,
        gate_reasons_json="{}",
        candidates_json=json.dumps([{
            "account_id": 5, "gate_reason": None, "score": 0.75,
            "budget_headroom": 0.9, "read_recovery_factor": 0.8,
            "fair_share": 1.0, "actual_share": 0.4, "rank": 1,
        }]),
        created_at=NOW,
    ))
    await test_db.flush()
    result = await metrics.aggregate(test_db, FakeCache())
    values = {
        (gauge["name"], tuple(gauge["labels"])): gauge["value"]
        for gauge in result["gauges"]
    }
    assert values[("vanguard_p4_pacing_score", ("5",))] == pytest.approx(0.75)
    assert values[("vanguard_p4_read_recovery_factor", ("5",))] == pytest.approx(0.8)
    assert values[("vanguard_p4_account_risk_level", ("5",))] == pytest.approx(1.0)
    assert result["p4"]["accounts"][0]["risk_level"] == "watch"
    assert result["p4"]["last_decision"]["selected_account_id"] == 5
    await test_db.rollback()


async def test_p4_summary_endpoint_shape(test_db, monkeypatch):
    from app.api.stats import get_p4_summary

    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", False)
    payload = await get_p4_summary(db=test_db)
    assert payload["code"] == 0
    data = payload["data"]
    for key in (
        "flags", "pacing", "ads_queue", "outbox", "survival",
        "reconciliation", "receipts", "history",
    ):
        assert key in data
    assert data["pacing"]["last_decision"] is None
    assert data["pacing"]["accounts"] == []
    assert data["flags"]["ad_pacing_enabled"] is False
    assert data["flags"]["retention_days"]["hot"] == 90
    assert data["outbox"]["states"] == {}
    assert data["history"]["candidates"]["operations"] == 0
    assert data["ads_queue"] == {"state": "unavailable"}
    assert data["receipts"]["latency_p95_seconds"] is None


async def test_p4_summary_endpoint_reports_decision_and_accounts(test_db, monkeypatch):
    from app.api.stats import get_p4_summary

    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", False)
    from app.core.account.models import TelegramAccount

    test_db.add(TelegramAccount(id=9, identifier="s-9", session_name="s-9",
                                risk_level="normal"))
    test_db.add(GrowthPacingDecision(
        stage="ads", mode="shadow", window_started_at=NOW, window_seconds=60,
        candidate_count=1, gated_count=0, scheduled_count=1,
        selected_account_id=9, applied=False,
        gate_reasons_json="{}",
        candidates_json=json.dumps([{
            "account_id": 9, "gate_reason": None, "score": 0.6,
            "fair_share": 1.0, "actual_share": 0.0, "rank": 1,
        }]),
        created_at=NOW,
    ))
    await test_db.flush()
    payload = await get_p4_summary(db=test_db)
    data = payload["data"]
    assert data["pacing"]["last_decision"]["mode"] == "shadow"
    assert data["pacing"]["last_decision"]["selected_account_id"] == 9
    assert data["pacing"]["accounts"][0]["account_id"] == 9
    assert data["pacing"]["accounts"][0]["risk_level"] == "normal"
    assert data["pacing"]["daily_stats"] == []
    await test_db.rollback()
