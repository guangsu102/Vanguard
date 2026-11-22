"""Operator summaries report measured bounded work, never inferred savings."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.api import group_qualification as api
from app.core.account.control_models import AccountBudgetReservation
from app.core.config import settings

NOW = datetime(2026, 11, 3, 1, 0)


def audit(**changes):
    values = {"state": "completed", "decision": "observe", "next_retry_at": None,
              "checked_at": NOW - timedelta(hours=1)}
    return Obj(**(values | changes))


def test_budget_wait_requires_current_timer_provenance():
    retry = NOW + timedelta(minutes=10)
    row = audit(next_retry_at=retry)
    wait = {"reason": "telegram_read_budget", "retry_at": retry.isoformat(), "required_reads": 24}
    result = api.review_status(row, {"read_cost": {"reads": 3, "entered_sdk_reads": 2, "reserved_reads": 24}}, wait, now=NOW)
    assert result["stage"] == "budget_wait"
    assert result["actual_reads"] == 2 and result["budget_attempted_reads"] == 3 and result["reserved_reads"] == 24
    assert api.review_status(row, {"read_cost": {"reads": 3}}, wait, now=NOW)["actual_reads"] is None
    wait["retry_at"] = (retry - timedelta(minutes=1)).isoformat()
    assert api.review_status(row, {}, wait, now=NOW)["stage"] == "evidence_wait"


@pytest.mark.parametrize(("row", "evidence", "event", "expected"), [
    (audit(state="waiting_ai"), {}, None, "ai_wait"),
    (audit(), {"collection_progress": {"pending_message_ids": [1, 2]}}, None, "collection_continuation"),
    (audit(), {}, {"message_ids": [1]}, "event_recheck"),
    (audit(decision="technical_wait"), {}, None, "technical_wait"),
    (audit(state="queued", checked_at=None), {}, None, "initial_review"),
    (audit(), {"collection_needs_refresh": True,
               "evidence_handoffs": [{"outcome": "unknown"}]}, None, "storage_fallback"),
])
def test_operational_stage_preserves_verdict(row, evidence, event, expected):
    decision = row.decision
    assert api.review_status(row, evidence, {}, event, now=NOW)["stage"] == expected
    assert row.decision == decision


def test_maturity_uses_edit_anchor_and_keeps_not_evaluated_explicit():
    evidence = {"rules_evaluation_status": "NOT_EVALUATED", "evidence": [{
        "source": "recent_promotional_message", "sender_role": "ordinary",
        "accessible": True, "text": "机场优惠订阅促销，欢迎点击 https://example.com 购买",
        "date": (NOW - timedelta(hours=23)).isoformat(),
        "edited_at": (NOW - timedelta(hours=1)).isoformat(),
    }]}
    result = api.review_status(audit(), evidence, {}, now=NOW)
    assert result["stage"] == "maturity_wait"
    assert result["mature_at"] == NOW + timedelta(hours=23)
    assert result["rules_evaluation_status"] == "NOT_EVALUATED"


@pytest.mark.asyncio
async def test_samples_are_exact_key_bounded_partial_and_utc(monkeypatch):
    at = NOW.replace(tzinfo=UTC).timestamp()
    review = [json.dumps({"reads": 3, "reserved_reads": 24, "at": at,
                         "methods": {"GetHistoryRequest": 3}}),
              json.dumps({"reads": 500, "at": at - 8 * 86400}),
              json.dumps({"reads": 500, "at": at + 1}), "[]", "invalid"]
    renewal = [json.dumps({"reads": 2, "at": at, "methods": {"GetMessagesRequest": 2}})]
    cache = Obj(lrange=AsyncMock(side_effect=[review, renewal]))
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=cache))
    result = await api.sample_summary(34, NOW)
    assert result["actual_reads"] is None and result["work_units"] == 2
    assert result["budget_charged_reads"] == 5 and result["sdk_unknown_work_units"] == 2
    assert result["partial"] is True
    assert result["per_workload_limit"] == 200
    assert result["methods"] == [{"method": "GetHistoryRequest", "reads": 3},
                                 {"method": "GetMessagesRequest", "reads": 2}]
    assert cache.lrange.await_args_list[0].args == ("vanguard:rpc:work_cost:34:review", 0, 199)
    assert cache.lrange.await_args_list[1].args == ("vanguard:rpc:work_cost:34:renewal", 0, 199)
    assert "savings" not in result


@pytest.mark.asyncio
async def test_samples_without_account_never_scan_redis(monkeypatch):
    get_redis = AsyncMock(side_effect=AssertionError("No global scan"))
    monkeypatch.setattr("app.core.redis.get_redis", get_redis)
    result = await api.sample_summary(None, NOW)
    assert result["available"] is False and result["reason"] == "select_account"
    get_redis.assert_not_called()


def ledger(identity, *, state="settled", reads=3, account_id=34, reason=None, age=1):
    now = datetime.utcnow()
    return AccountBudgetReservation(
        reservation_id=identity, account_id=account_id, workload="review", lane="critical",
        window_start=now - timedelta(hours=1), reserved_reads=100, consumed_reads=reads,
        state=state, lease_expires_at=now + timedelta(minutes=2),
        created_at=now - timedelta(hours=age), reason=reason,
    )


@pytest.mark.asyncio
async def test_runtime_counts_known_actual_consumption_only(test_db, monkeypatch):
    test_db.add_all([
        ledger("settled"), ledger("inflight", state="reserved", reads=9),
        ledger("unknown", reason="consumption_evidence_missing", reads=80),
        ledger("other-account", account_id=2, reads=50), ledger("old", reads=50, age=25),
    ])
    await test_db.flush()
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr("app.modules.acquisition.review_read_runtime.evidence_status", AsyncMock(return_value={"enabled": False}))
    monkeypatch.setattr(api, "sample_summary", AsyncMock(return_value={"available": False}))
    monkeypatch.setattr("app.core.account.rpc_governor.snapshot", AsyncMock(return_value={"state": "ready"}))
    result = (await api.read_runtime(account_id=34, db=test_db))["data"]
    metrics = result["metrics"]
    assert metrics["actual_reads"] is None
    assert metrics["known_budget_charged_reads"] == 3
    assert metrics["budget_charged_reads"] is None
    assert metrics["coverage"] == "partial"
    assert metrics["settled_work_units"] == 2
    assert metrics["inflight_work_units"] == 1
    assert metrics["unknown_consumption_work_units"] == 1
    assert metrics["row_limit"] == 1000 and metrics["truncated"] is False


@pytest.mark.asyncio
async def test_disabled_ledger_does_not_require_new_tables(monkeypatch):
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", False)
    db = Obj(execute=AsyncMock(side_effect=AssertionError("Ledger disabled")))
    monkeypatch.setattr("app.modules.acquisition.review_read_runtime.evidence_status", AsyncMock(return_value={"enabled": False}))
    result = (await api.read_runtime(account_id=None, db=db))["data"]
    assert result["metrics"]["actual_reads"] is None
    assert result["samples"]["reason"] == "select_account"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ledger_aggregation_is_bounded_and_marks_truncation(test_db, monkeypatch):
    test_db.add_all([ledger(f"bounded-{index}", reads=1) for index in range(1002)])
    await test_db.flush()
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr("app.modules.acquisition.review_read_runtime.evidence_status", AsyncMock(return_value={"enabled": False}))
    result = (await api.read_runtime(account_id=None, db=test_db))["data"]
    assert result["metrics"]["truncated"] is True
    assert result["metrics"]["actual_reads"] is None
    assert result["metrics"]["known_budget_charged_reads"] == 1000


@pytest.mark.asyncio
async def test_empty_enabled_ledger_is_unavailable_not_zero(test_db, monkeypatch):
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    monkeypatch.setattr("app.modules.acquisition.review_read_runtime.evidence_status", AsyncMock(return_value={"enabled": False}))
    result = (await api.read_runtime(account_id=None, db=test_db))["data"]
    assert result["metrics"]["actual_reads"] is None
    assert result["metrics"]["budget_charged_reads"] is None
    assert result["metrics"]["reason"] == "no_matching_ledger_records"
