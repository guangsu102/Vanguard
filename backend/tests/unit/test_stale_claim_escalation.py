"""Non-converging review claims must escape the loop after bounded attempts."""
from datetime import datetime, timedelta

from app.modules.acquisition.qualification_service import stale_claim_action

NOW = datetime(2026, 10, 8, 12, 0, 0)


class Row:
    def __init__(self, *, state="queued", decision="observe", reason="x", attempts=0):
        self.state = state
        self.decision = decision
        self.reason = reason
        self.attempts = attempts


def test_continuity_trial_graduates_after_three_verifications():
    row = Row(decision="trial", reason="ordinary_member_ads_verified", attempts=3)
    assert stale_claim_action(row, now=NOW) == "graduate_trial"
    row2 = Row(decision="trial", reason="own_ad_survived_twenty_four_hours", attempts=131)
    assert stale_claim_action(row2, now=NOW) == "graduate_trial"


def test_continuity_trial_below_threshold_keeps_normal_flow():
    row = Row(decision="trial", reason="ordinary_member_ads_verified", attempts=2)
    assert stale_claim_action(row, now=NOW) is None


def test_stale_reject_finalizes_after_three_attempts():
    row = Row(state="queued", decision="reject", reason="group_rules_no_authoritative_evidence", attempts=101)
    assert stale_claim_action(row, now=NOW) == "finalize_reject"
    row2 = Row(state="queued", decision="reject", reason="no_ordinary_member_ad_48h", attempts=1)
    assert stale_claim_action(row2, now=NOW) is None


def test_completed_reject_keeps_designed_retry_ladders():
    # Completed reject rows are excluded even at high attempt counts: the
    # AI-inconclusive observation ladder counts per-material streaks and
    # maturity-scheduled rejects recover on evidence maturation. A lifetime
    # threshold here would cut those ladders short.
    for reason in ("group_rules_ai_consensus_failed", "group_rules_ai_unresolved_conflict",
                   "no_ordinary_member_ad_48h"):
        row = Row(state="completed", decision="reject", reason=reason, attempts=97)
        assert stale_claim_action(row, now=NOW) is None, reason


def test_retired_precedent_escalates_to_full_review():
    row = Row(reason="advertising_precedent_disappeared_before_send", attempts=184)
    assert stale_claim_action(row, now=NOW) == "escalate_full_review"
    row2 = Row(reason="advertising_precedent_changed_before_send", attempts=3)
    assert stale_claim_action(row2, now=NOW) == "escalate_full_review"


def test_retired_precedent_below_threshold_keeps_light_check():
    row = Row(reason="advertising_precedent_disappeared_before_send", attempts=2)
    assert stale_claim_action(row, now=NOW) is None


def test_normal_reviews_never_escalate():
    cases = [
        Row(state="queued", decision="unknown", reason="qualification_evidence_incomplete", attempts=1),
        Row(state="queued", decision="observe", reason="rules_coverage_incomplete", attempts=2),
        Row(state="completed", decision="trial", reason="ordinary_member_ads_verified", attempts=50),
        Row(state="waiting_ai", decision="observe", reason="group_rules_ai_unavailable", attempts=9),
        Row(state="queued", decision="trial", reason="group_rules_ai_unresolved_conflict", attempts=9),
        Row(state="running", decision="trial", reason="ordinary_member_ads_verified", attempts=9),
    ]
    for row in cases:
        assert stale_claim_action(row, now=NOW) is None, row.reason
