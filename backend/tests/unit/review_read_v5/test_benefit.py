"""Synthetic, numeric-only acceptance evidence; no live or Telegram access."""
import copy
import hashlib
import json

import pytest
from ._benefit_validator import COMPLEXITIES, PHASE1_FLAGS, PHASE2_FLAGS, analyse

START = 1_799_712_000


def fixture():
    windows, pairs = [], []
    for account in (2, 3, 4):
        pair = {}
        for index, (stage, reads, all_reads) in enumerate((
            ("baseline", 1000, 1200), ("phase1", 800, 1000), ("phase2", 560, 700)
        )):
            start = START + index * 86400
            enabled = set() if index == 0 else PHASE1_FLAGS | (PHASE2_FLAGS if index == 2 else set())
            flags = {key: key in enabled for key in PHASE1_FLAGS | PHASE2_FLAGS}
            release = {"image": "offline-image", "manifest_sha256": "offline-manifest",
                "expected_roles": ["backend", "growth", "celery"],
                "measurement_version": "sdk_entry_v2", "attribution_version": "purpose_v2_lane_first",
                "completion_version": "business_transition_v2", "config_fingerprint": "same-budget-and-admission",
                "flags": flags, "flags_fingerprint": hashlib.sha256(json.dumps(flags, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()}
            workloads = []
            for name, count, cost in (("review", 8, reads), ("renewal", 2, 0)):
                complexity = {key: {"entered_sdk": cost if key == "complex" else 0,
                    "completed_decisions": count if key == "complex" else 0} for key in COMPLEXITIES}
                workloads.append({"workload": name, "complexity": complexity})
            metrics = {"available": True, "complete": True, "current_hour_excluded": True,
                "window_start": start, "window_end": start + 86400, "gap_hours": [], "missing_roles": [],
                "overflow_events": 0, "counter_saturation_events": 0, "source_capacity_exceeded": False,
                "pending_sdk_since": None, "expected_roles": ["backend", "growth", "celery"],
                "active_roles": {"backend": 1, "growth": 1, "celery": 1}, "coverage_started_at": START,
                "measurement_semantics_started_at": START, "business_semantics_started_at": START,
                "sources_confirmed_through": start + 86400, "actual_reads": all_reads,
                "known_entered_sdk": all_reads, "measurement_version": release["measurement_version"],
                "attribution_version": release["attribution_version"], "completion_version": release["completion_version"],
                "complexity_version": "business_demand_v1",
                "by_stage": [{"stage": stage, "entered_sdk": all_reads, "work": workloads, "attribution_overflow": 0,
                    "business_completions": {"decisions": 10}}]}
            business = {"healthy_seconds": 86400, "arrivals_known": True,
                "arrivals_by_complexity": {key: 10 if key == "complex" else 0 for key in COMPLEXITIES},
                "backlog_start": 1, "backlog_end": 1, "oldest_age_start": 2, "oldest_age_end": 2,
                "latency_p95_seconds": 20, "ads_attempted": 10, "ads_succeeded": 10,
                "ads_unknown": 0, "joins_completed": 5, "failed_jobs": 0, "duplicate_sends": 0,
                "premature_protection_releases": 0, "storage_bytes": 16_261_120}
            identity = f"{account}-{stage}"
            windows.append({"id": identity, "account_id": account, "stage": stage,
                "window_start": start, "window_end": start + 86400, "release": release,
                "metrics": metrics, "business": business, "execution_coverage_verified": True,
                "unmetered_channels": 0})
            pair[stage] = identity
        pairs.append(pair)
    return {"schema_version": 1, "baseline_weights": {key: 1 if key == "complex" else 0 for key in COMPLEXITIES},
        "weights_frozen_at": START + 86400, "windows": windows, "pairs": pairs}


def result(document):
    return analyse(document, now=START + 4 * 86400)


def reasons(value):
    return value["reasons"] + [reason for item in value.get("rejected_windows", []) for reason in item["reasons"]]


def test_three_independent_accounts_hit_twenty_then_extra_thirty():
    value = result(fixture())
    assert value["status"] == "passed"
    assert value["accepted_pairs"] == 3
    assert value["measured_reduction"] == pytest.approx({"phase1": .20, "phase2_extra": .30, "cumulative": .44})
    assert value["normalized_cost"] == {"baseline": 100, "phase1": 80, "phase2": 56}
    assert len(value["raw_windows"]) == 9


def test_six_execution_roles_follow_configuration_and_require_each_live_source():
    document = fixture()
    roles = ["backend", "growth", "celery", "reconciliation", "resource", "guardian"]
    for window in document["windows"]:
        window["release"]["expected_roles"] = roles
        window["metrics"]["expected_roles"] = roles
        window["metrics"]["active_roles"] = dict.fromkeys(roles, 1)
    assert result(document)["status"] == "passed"
    document["windows"][1]["metrics"]["active_roles"]["guardian"] = 0
    value = result(document)
    assert value["status"] == "insufficient_evidence"
    assert "execution_role_missing:guardian" in reasons(value)


def test_declared_coverage_cannot_silently_ignore_configured_extra_roles():
    document = fixture()
    document["windows"][1]["release"]["expected_roles"].append("reconciliation")
    value = result(document)
    assert value["status"] == "insufficient_evidence"
    assert "expected_execution_roles_do_not_match_configuration" in reasons(value)


def test_comparison_rejects_changed_execution_role_contract():
    document = fixture()
    window = document["windows"][1]
    roles = ["backend", "growth", "celery", "reconciliation", "resource", "guardian"]
    window["release"]["expected_roles"] = roles
    window["metrics"]["expected_roles"] = roles
    window["metrics"]["active_roles"] = dict.fromkeys(roles, 1)
    value = result(document)
    assert value["status"] == "insufficient_evidence"
    assert "pair_0:mixed_execution_role_contract" in reasons(value)


@pytest.mark.parametrize("mutation,reason", [
    (lambda w: w["metrics"].update(missing_roles=["celery"]), "missing_roles_or_missing"),
    (lambda w: w["metrics"].update(actual_reads=None), "all_purpose_sdk_total_unknown_or_not_conserved"),
    (lambda w: w["metrics"].update(gap_hours=[START]), "gap_hours_or_missing"),
    (lambda w: w["metrics"].update(counter_saturation_events=1), "counter_saturation_events_or_missing"),
    (lambda w: w["metrics"].update(pending_sdk_since=START), "source_capacity_or_pending_entry"),
    (lambda w: w["metrics"].update(business_semantics_started_at=START + 5 * 86400), "window_before_business_semantics_started_at"),
    (lambda w: w["metrics"].update(sources_confirmed_through=START), "source_not_confirmed_through_window"),
    (lambda w: w["metrics"].update(completion_version="old"), "mixed_completion_version"),
    (lambda w: w["metrics"].update(complexity_version="old"), "current_sdk_entry_and_demand_complexity_required"),
    (lambda w: w["metrics"]["by_stage"][0].update(attribution_overflow=1), "work_attribution_overflow_or_missing"),
    (lambda w: w["release"].update(flags_fingerprint="wrong"), "flags_fingerprint_mismatch"),
    (lambda w: w.update(execution_coverage_verified=False), "all_execution_processes_and_read_channels_not_verified"),
    (lambda w: w["business"].update(backlog_end=2), "backlog_or_oldest_age_increased"),
    (lambda w: w["business"].update(oldest_age_end=3), "backlog_or_oldest_age_increased"),
    (lambda w: w["business"].update(arrivals_known=False), "independent_arrivals_unknown"),
    (lambda w: w["business"].update(latency_p95_seconds=None), "business_missing:latency_p95_seconds"),
    (lambda w: w["business"].update(healthy_seconds=86399), "unhealthy_or_inactive_window"),
])
def test_missing_or_untrustworthy_evidence_rejects(mutation, reason):
    document = fixture()
    mutation(document["windows"][1])
    value = result(document)
    assert value["status"] == "insufficient_evidence"
    assert value["measured_reduction"] is None
    assert reason in reasons(value)


@pytest.mark.parametrize("key,value,reason", [
    ("ads_succeeded", 9, "business_volume_declined:ads_succeeded"),
    ("joins_completed", 4, "business_volume_declined:joins_completed"),
    ("failed_jobs", 1, "business_quality_worsened:failed_jobs"),
    ("ads_unknown", 1, "business_quality_worsened:ads_unknown"),
    ("latency_p95_seconds", 21, "business_quality_worsened:latency_p95_seconds"),
])
def test_volume_or_quality_decline_cannot_claim_read_reduction(key, value, reason):
    document = fixture()
    document["windows"][1]["business"][key] = value
    output = result(document)
    assert output["status"] == "insufficient_evidence"
    assert any(reason in entry for entry in reasons(output))


def test_zero_committed_completions_and_unknown_cost_stratum_are_rejected():
    document = fixture()
    row = document["windows"][1]["metrics"]["by_stage"][0]
    for work in row["work"]:
        work["complexity"]["complex"]["completed_decisions"] = 0
    row["business_completions"]["decisions"] = 0
    assert "zero_independent_committed_completions" in reasons(result(document))
    document = fixture()
    document["windows"][1]["metrics"]["by_stage"][0]["work"][0]["complexity"]["unknown"]["entered_sdk"] = 1
    assert "reads_in_zero_completion_stratum" in reasons(result(document))


def test_transfer_to_other_purposes_does_not_pass():
    document = fixture()
    metrics = document["windows"][1]["metrics"]
    metrics["actual_reads"] = metrics["known_entered_sdk"] = 1200
    metrics["by_stage"][0]["entered_sdk"] = 1200
    assert any("all_channel_net_reads_not_lower" in item for item in reasons(result(document)))


def test_business_denominator_is_independent_of_slice_counts():
    document = fixture()
    document["windows"][1]["metrics"]["by_stage"][0]["business_completions"]["decisions"] = 100
    assert "business_denominator_mismatch" in reasons(result(document))


@pytest.mark.parametrize("key", ["duplicate_sends", "premature_protection_releases", "storage_bytes"])
def test_safety_regression_is_failed_not_missing_evidence(key):
    document = fixture()
    document["windows"][1]["business"][key] = 1024**3 if key == "storage_bytes" else 1
    assert result(document)["status"] == "failed"


def test_window_reuse_partial_hour_future_and_mixed_release_reject():
    document = fixture()
    document["pairs"][2] = copy.deepcopy(document["pairs"][1])
    assert any("window_reused" in item for item in reasons(result(document)))
    document = fixture()
    document["windows"][1]["window_end"] -= 1
    assert "complete_24_utc_hours_required" in reasons(result(document))
    document = fixture()
    document["windows"][1]["release"]["image"] = "different-image"
    assert any("mixed_image" in item for item in reasons(result(document)))
    assert analyse(fixture(), now=START)["status"] == "insufficient_evidence"


def test_baseline_weight_selection_and_insufficient_replicates_reject():
    document = fixture()
    document["baseline_weights"] = {key: .5 if key in {"simple", "complex"} else 0 for key in COMPLEXITIES}
    assert result(document)["status"] == "insufficient_evidence"
    document = fixture()
    document["pairs"].pop()
    assert "at_least_three_independent_pairs_required" in reasons(result(document))
    document = fixture()
    document["weights_frozen_at"] = START
    assert "weights_frozen_before_baseline_observation_complete" in reasons(result(document))


def test_valid_but_insufficient_reduction_is_target_not_met():
    document = fixture()
    for window in document["windows"]:
        if window["stage"] == "phase2":
            window["metrics"]["by_stage"][0]["work"][0]["complexity"]["complex"]["entered_sdk"] = 600
    value = result(document)
    assert value["status"] == "target_not_met"
    assert value["measured_reduction"]["phase2_extra"] == pytest.approx(.25)


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": 1},
    {"schema_version": 1, "windows": [None], "pairs": [None], "baseline_weights": {}, "weights_frozen_at": 0}])
def test_malformed_evidence_never_raises_or_passes(value):
    assert result(value)["status"] == "insufficient_evidence"
