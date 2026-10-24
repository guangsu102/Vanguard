"""P4 ad fairness pacing: gates, score determinism, ordering and rollout modes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.control_models import AccountBudgetSnapshot
from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.modules.acquisition import ad_fairness_pacing as pacing
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog
from app.modules.acquisition.p4_models import GrowthPacingDecision

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 22, 12, 0, 0)


async def _account(db, account_id, *, risk_score=0.0, risk_pause_until=None,
                   risk_level="normal", is_active=True):
    account = TelegramAccount(
        id=account_id,
        identifier=f"p4-{account_id}",
        session_name=f"p4-{account_id}",
        is_active=is_active,
        status="online",
        risk_level=risk_level,
        risk_score=risk_score,
        risk_pause_until=risk_pause_until,
    )
    db.add(account)
    await db.flush()
    return account


def _snapshot(db, account_id, *, limit=100, used=10, read_factor=1.0):
    db.add(AccountBudgetSnapshot(
        account_id=account_id,
        window="day",
        window_start=NOW - timedelta(hours=12),
        lane="ad",
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        ttl_seconds=86400,
        read_factor=read_factor,
        loan_json="{}",
        snapshot_at=NOW - timedelta(seconds=30),
    ))


async def _seed(db, accounts: dict) -> None:
    for account_id, spec in accounts.items():
        await _account(
            db, account_id,
            **{k: v for k, v in spec.items() if k != "snapshot"},
        )
        if "snapshot" in spec:
            _snapshot(db, account_id, **spec["snapshot"])


async def _delivery(db, account_id, campaign_id, *, status="success",
                    survival_status="not_required", sent_at=NOW - timedelta(hours=1),
                    created=None):
    db.add(AdDeliveryLog(
        account_id=account_id,
        telegram_group_id=-100000 + account_id,
        ad_campaign_id=campaign_id,
        status=status,
        survival_status=survival_status,
        survival_stage="two_minute",
        survival_check_due_at=None,
        sent_at=sent_at if status == "success" else None,
        created_at=created or (NOW - timedelta(hours=1)),
        telegram_message_id=555 if status == "success" else None,
    ))


@pytest.fixture
async def campaign(test_db):
    row = AdCampaign(id=77, name="p4-pacing")
    test_db.add(row)
    await test_db.flush()
    return row


async def test_evaluation_is_deterministic_and_score_order_is_stable(test_db, campaign):
    await _seed(test_db, {
        1: {"snapshot": {"limit": 100, "used": 10}},
        2: {"snapshot": {"limit": 100, "used": 80}},
    })
    first = await pacing.evaluate(test_db, [1, 2], NOW)
    second = await pacing.evaluate(test_db, [2, 1], NOW)
    assert [c.score for c in first.candidates] == [c.score for c in second.candidates]
    assert first.order == second.order
    assert first.order[0] == 1
    by_id = {c.account_id: c for c in first.candidates}
    assert by_id[1].budget_headroom == pytest.approx(0.9)
    assert by_id[2].budget_headroom == pytest.approx(0.2)
    assert by_id[1].score > by_id[2].score
    assert by_id[1].fair_share == pytest.approx(0.5)


async def test_fixed_weights_sum():
    candidate = pacing.PacingCandidate(account_id=1)
    candidate.budget_headroom = 1.0
    candidate.read_recovery_factor = 1.0
    candidate.risk_component = 1.0
    candidate.cooldown_readiness = 1.0
    candidate.survival_health = 1.0
    candidate.recent_success_rate = 1.0
    assert pacing.score_candidate(candidate) == pytest.approx(1.0)


async def test_sort_key_uses_score_fairness_then_recency_then_id():
    def candidate(account_id, *, score, gap=0.0, at=0.0):
        return pacing.PacingCandidate(
            account_id=account_id, score=score, fairness_gap=gap, last_scheduled_at=at
        )

    ranked = sorted(
        [
            candidate(9, score=0.5),
            candidate(2, score=0.9),
            candidate(3, score=0.9, gap=0.2),
            candidate(4, score=0.9, gap=0.2, at=100.0),
            candidate(5, score=0.9, gap=0.2, at=50.0),
            candidate(1, score=0.9, gap=0.3),
        ],
        key=pacing.sort_key,
    )
    assert [c.account_id for c in ranked] == [1, 3, 5, 4, 2, 9]


async def test_hard_gates_block_and_record_reasons(test_db, campaign, monkeypatch):
    await _seed(test_db, {
        1: {"risk_pause_until": NOW + timedelta(minutes=30)},
        2: {"snapshot": {"limit": 100, "used": 100}},
        3: {"risk_level": "quarantined"},
        4: {"is_active": False},
        5: {"snapshot": {"limit": 100, "used": 10}},
    })
    # Two pending survival rows sent two days ago are overdue at every stage.
    for _ in range(2):
        await _delivery(
            test_db, 5, campaign.id, status="success", survival_status="pending",
            sent_at=NOW - timedelta(hours=48),
        )
    monkeypatch.setattr(settings, "P4_SURVIVAL_BACKLOG_LIMIT", 1)
    evaluation = await pacing.evaluate(test_db, [1, 2, 3, 4, 5], NOW)
    reasons = {c.account_id: c.gate_reason for c in evaluation.candidates}
    assert reasons[1] == pacing.GATE_RISK_PAUSE
    assert reasons[2] == pacing.GATE_BUDGET_WAIT
    assert reasons[3] == pacing.GATE_ACCOUNT_STATE
    assert reasons[4] == pacing.GATE_ACCOUNT_STATE
    assert reasons[5] == pacing.GATE_SURVIVAL_BACKLOG
    assert evaluation.order == []
    assert set(evaluation.gate_reason_counts()) == {
        pacing.GATE_RISK_PAUSE, pacing.GATE_BUDGET_WAIT,
        pacing.GATE_ACCOUNT_STATE, pacing.GATE_SURVIVAL_BACKLOG,
    }


async def test_unresolved_backlog_gate(test_db, campaign, monkeypatch):
    await _account(test_db, 6)
    for _ in range(3):
        await _delivery(test_db, 6, campaign.id, status="unknown")
    monkeypatch.setattr(settings, "P4_UNRESOLVED_BACKLOG_LIMIT", 2)
    evaluation = await pacing.evaluate(test_db, [6], NOW)
    assert evaluation.order == []
    assert evaluation.candidates[0].gate_reason == pacing.GATE_UNRESOLVED_BACKLOG


async def test_stale_budget_snapshot_does_not_gate_and_stays_neutral(test_db, campaign):
    await _account(test_db, 7)
    test_db.add(AccountBudgetSnapshot(
        account_id=7, window="day", window_start=NOW - timedelta(hours=12),
        lane="ad", limit=100, used=100, remaining=0, ttl_seconds=86400,
        read_factor=1.0, loan_json="{}", snapshot_at=NOW - timedelta(hours=2),
    ))
    await test_db.flush()
    evaluation = await pacing.evaluate(test_db, [7], NOW)
    assert evaluation.order == [7]
    candidate = evaluation.candidates[0]
    assert candidate.gate_reason is None
    assert candidate.budget_headroom == pytest.approx(pacing.NEUTRAL_COMPONENT)


async def test_no_budget_snapshot_uses_neutral_components(test_db, campaign):
    await _account(test_db, 8)
    evaluation = await pacing.evaluate(test_db, [8], NOW)
    assert evaluation.order == [8]
    assert evaluation.candidates[0].budget_headroom == pacing.NEUTRAL_COMPONENT
    assert evaluation.candidates[0].read_recovery_factor == pacing.NEUTRAL_COMPONENT


async def test_read_recovery_factor_uses_snapshot_read_factor(test_db, campaign):
    await _seed(test_db, {
        9: {"snapshot": {"limit": 100, "used": 0, "read_factor": 0.5}},
    })
    evaluation = await pacing.evaluate(test_db, [9], NOW)
    assert evaluation.candidates[0].read_recovery_factor == pytest.approx(0.5)


async def test_risk_score_component(test_db, campaign):
    await _seed(test_db, {
        10: {"risk_score": 40.0},
    })
    evaluation = await pacing.evaluate(test_db, [10], NOW)
    assert evaluation.candidates[0].risk_component == pytest.approx(0.6)


async def test_low_capacity_account_is_not_starved(test_db, campaign):
    # Headroom is a ratio: an untouched small-budget account keeps 1.0 while
    # a bigger account that already spent keeps less, so the small one wins.
    await _seed(test_db, {
        20: {"snapshot": {"limit": 5, "used": 0}},
        21: {"snapshot": {"limit": 1000, "used": 100}},
    })
    evaluation = await pacing.evaluate(test_db, [20, 21], NOW)
    assert evaluation.order[0] == 20
    # Once both are equal, the account with fewer recent successes has the
    # larger fairness gap and wins the tiebreak.
    await _account(test_db, 22)
    _snapshot(test_db, 22, limit=100, used=0)
    await _delivery(test_db, 20, campaign.id, status="success",
                    sent_at=NOW - timedelta(hours=2))
    evaluation = await pacing.evaluate(test_db, [20, 22], NOW)
    by_id = {c.account_id: c for c in evaluation.candidates}
    assert by_id[22].fairness_gap > by_id[20].fairness_gap
    assert evaluation.order[0] == 22


async def test_recent_success_rate_component(test_db, campaign):
    await _seed(test_db, {
        30: {"snapshot": {"limit": 100, "used": 0}},
        31: {"snapshot": {"limit": 100, "used": 0}},
    })
    for _ in range(4):
        await _delivery(test_db, 30, campaign.id, status="success")
    await _delivery(test_db, 31, campaign.id, status="failed")
    evaluation = await pacing.evaluate(test_db, [30, 31], NOW)
    by_id = {c.account_id: c for c in evaluation.candidates}
    assert by_id[30].recent_success_rate == pytest.approx(1.0)
    assert by_id[31].recent_success_rate == pytest.approx(0.0)
    assert evaluation.order[0] == 30


async def test_shadow_mode_keeps_legacy_order_and_records_decision(test_db, campaign, monkeypatch):
    await _seed(test_db, {
        40: {"snapshot": {"limit": 100, "used": 80}},
        41: {"snapshot": {"limit": 100, "used": 10}},
    })
    monkeypatch.setattr(settings, "P4_AD_PACING", True)
    monkeypatch.setattr(settings, "P4_AD_PACING_SHADOW", True)
    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", False)
    legacy = [40, 41]
    order = await pacing.apply_to_dispatch(
        test_db, legacy, NOW.timestamp(), window_seconds=60,
        last_runs={"ads:40": '{"at": 100.0}', "ads:41": '{"at": 50.0}'},
    )
    assert order == legacy
    decision = (await test_db.scalars(
        select(GrowthPacingDecision).order_by(GrowthPacingDecision.id.desc())
    )).all()[-1]
    assert decision.mode == "shadow"
    assert decision.applied is False
    assert decision.stage == "ads"
    assert decision.candidate_count == 2
    assert decision.selected_account_id == 41  # what pacing would have chosen
    assert decision.scheduled_count == 0

    await pacing.finalize_published_decision(test_db, decision.id, {40})
    await test_db.refresh(decision)
    assert decision.scheduled_count == 1
    published = {row["account_id"]: row["published"] for row in json.loads(decision.candidates_json)}
    assert published[40] is True and published[41] is False


async def test_execute_mode_reorders_and_records_applied_decision(test_db, campaign, monkeypatch):
    await _seed(test_db, {
        50: {"snapshot": {"limit": 100, "used": 80}},
        51: {"snapshot": {"limit": 100, "used": 10}},
    })
    monkeypatch.setattr(settings, "P4_AD_PACING", True)
    monkeypatch.setattr(settings, "P4_AD_PACING_SHADOW", False)
    monkeypatch.setattr(settings, "P4_ADMIN_METRICS", False)
    order = await pacing.apply_to_dispatch(
        test_db, [50, 51], NOW.timestamp(), window_seconds=60,
    )
    assert order == [51, 50]
    decision = (await test_db.scalars(
        select(GrowthPacingDecision).order_by(GrowthPacingDecision.id.desc())
    )).all()[-1]
    assert decision.mode == "pacing"
    assert decision.applied is True
    assert decision.selected_account_id == 51
    assert decision.scheduled_count == 0


async def test_disabled_flag_falls_back_without_decision(test_db, campaign, monkeypatch):
    await _account(test_db, 60)
    monkeypatch.setattr(settings, "P4_AD_PACING", False)
    order = await pacing.apply_to_dispatch(test_db, [60], NOW.timestamp(), window_seconds=60)
    assert order == [60]
    assert (await test_db.scalars(select(GrowthPacingDecision))).first() is None


async def test_canary_order_scopes_reordering():
    evaluation = pacing.PacingEvaluation(stage="ads", candidate_count=4)
    for account_id, rank in ((61, 2), (62, 1), (63, 3), (64, 4)):
        evaluation.candidates.append(
            pacing.PacingCandidate(account_id=account_id, rank=rank)
        )
    evaluation.order = [62, 61, 63, 64]
    legacy = [61, 62, 63, 64]
    # Canary accounts are paced first; everyone else keeps legacy order.
    assert pacing.canary_order(evaluation, legacy, {61, 62}) == [62, 61, 63, 64]
    # Empty canary means a full rollout.
    assert pacing.canary_order(evaluation, legacy, set()) == evaluation.order
    # A gated canary account is held back exactly as a full rollout would.
    evaluation.candidates[0].gate_reason = pacing.GATE_BUDGET_WAIT
    assert pacing.canary_order(evaluation, legacy, {61, 62}) == [62, 63, 64]


async def test_decision_row_records_gate_reasons_and_candidates(test_db, campaign):
    await _seed(test_db, {70: {"snapshot": {"limit": 100, "used": 0}}})
    evaluation = await pacing.evaluate(test_db, [70], NOW)
    payload = pacing.decision_row(
        evaluation, mode="pacing", applied=True, window_started_at=NOW,
        window_seconds=60, selected_account_id=70, scheduled_count=1,
    )
    await pacing.record_decision(test_db, payload)
    await test_db.flush()
    row = (await test_db.scalars(select(GrowthPacingDecision))).all()[-1]
    assert row.candidate_count == 1
    assert row.gated_count == 0
    assert row.selected_account_id == 70
    assert json.loads(row.candidates_json)[0]["account_id"] == 70
    await test_db.rollback()
