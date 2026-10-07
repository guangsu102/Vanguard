"""AI-inconclusive outcomes must observe with bounded retries, not fail closed."""
from datetime import datetime, timedelta

import pytest

from app.modules.acquisition.qualification_retry import rule_retry_fingerprint
from app.modules.acquisition.qualification_service import review_schedule

NOW = datetime(2026, 10, 7, 12, 0, 0)


def snapshot(reason="group_rules_ai_unavailable"):
    return {
        "decision": "observe",
        "reason": reason,
        "policy_version": "pp_ai_20260922",
        "evidence": [
            {"source": "full_about", "message_id": 1, "text": "本群禁止广告",
             "sender_role": "group_metadata", "scope": "group"},
        ],
    }


@pytest.mark.asyncio
async def test_first_inconclusive_ai_outcome_observes_instead_of_rejecting():
    snap = snapshot()
    verdict, reason, state, retry = review_schedule(snap, {}, NOW)
    assert (verdict, state) == ("observe", "completed")
    assert reason == "group_rules_ai_unavailable"
    assert retry == NOW + timedelta(hours=12)
    assert snap.get("ai_final") is not True
    assert snap["unchanged_rejection_count"] == 1
    assert snap["review_trigger"] == "ai_incomplete_observation"


@pytest.mark.asyncio
async def test_second_inconclusive_attempt_extends_observation_window():
    snap = snapshot()
    fingerprint = rule_retry_fingerprint(snap)
    previous = {"rejection_fingerprint": fingerprint, "unchanged_rejection_count": 1}
    verdict, _, state, retry = review_schedule(snap, previous, NOW)
    assert (verdict, state) == ("observe", "completed")
    assert retry == NOW + timedelta(hours=24)
    assert snap["unchanged_rejection_count"] == 2
    assert snap.get("ai_final") is not True


@pytest.mark.asyncio
async def test_third_inconclusive_attempt_fails_closed_as_before():
    snap = snapshot()
    fingerprint = rule_retry_fingerprint(snap)
    previous = {"rejection_fingerprint": fingerprint, "unchanged_rejection_count": 2}
    verdict, reason, state, retry = review_schedule(snap, previous, NOW)
    assert (verdict, state, retry) == ("reject", "completed", None)
    assert snap["ai_final"] is True
    assert snap["ai_decision"] == "fail"
    assert snap["review_trigger"] == "ai_terminal_failure"


@pytest.mark.asyncio
async def test_fresh_evidence_triggers_prompt_new_recognition_attempt():
    snap = snapshot()
    verdict, _, state, retry = review_schedule(
        snap,
        {"rejection_fingerprint": "stale-material-fingerprint", "unchanged_rejection_count": 2},
        NOW,
    )
    assert (verdict, state) == ("observe", "completed")
    assert retry == NOW + timedelta(hours=2)
    assert snap.get("ai_pending") is True
    assert snap["unchanged_rejection_count"] == 1


@pytest.mark.asyncio
async def test_evidence_maturity_shortens_the_observation_wait():
    snap = snapshot()
    snap["evidence"].append({
        "source": "recent_promotional_message", "sender_role": "ordinary",
        "sender_id": 999, "message_id": 7, "accessible": True,
        "text": "低价出售全新手机有意联系我", "date": (NOW - timedelta(hours=23)).isoformat(),
        "observed_at": NOW.isoformat(),
    })
    verdict, _, state, retry = review_schedule(snap, {}, NOW)
    assert (verdict, state) == ("observe", "completed")
    assert retry == NOW + timedelta(hours=1)
