"""Read-only, conservative V5 acceptance calculator. No production operations.

Input is bounded numeric evidence produced by the release/window collector.
Missing evidence produces insufficient_evidence, never a favourable zero.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

COMPLEXITIES = ("simple", "standard", "complex", "unknown")
STAGES = ("baseline", "phase1", "phase2")
RELEASE_KEYS = ("image", "manifest_sha256", "measurement_version", "attribution_version",
                "completion_version", "config_fingerprint")
BUSINESS_NUMBERS = ("healthy_seconds", "backlog_start", "backlog_end", "oldest_age_start",
                    "oldest_age_end", "latency_p95_seconds", "ads_attempted", "ads_succeeded",
                    "ads_unknown", "joins_completed", "failed_jobs", "duplicate_sends",
                    "premature_protection_releases", "storage_bytes")
MAX_INPUT_BYTES = 2 * 1024 * 1024
PHASE1_FLAGS = {"reuse", "resume", "minimal_plan"}
PHASE2_FLAGS = {"demand_identity", "push", "fast_path", "fact_handoff", "dependency_resume", "public_share"}
EXECUTION_ROLES = {"backend", "growth", "celery", "reconciliation", "resource", "guardian"}


def number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _analyse(document: dict, *, now: float | None = None) -> dict:
    instant = time.time() if now is None else now
    issues = []
    safety = []
    rejected = []
    windows = {}
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return {"status": "insufficient_evidence", "reasons": ["schema_version_invalid"], "measured_reduction": None}
    values = document.get("windows")
    pairs = document.get("pairs")
    weights = document.get("baseline_weights")
    frozen = document.get("weights_frozen_at")
    if not isinstance(values, list) or len(values) > 96 or not isinstance(pairs, list) or len(pairs) > 32:
        return {"status": "insufficient_evidence", "reasons": ["bounded_windows_or_pairs_required"], "measured_reduction": None}
    if (not isinstance(weights, dict) or set(weights) != set(COMPLEXITIES)
            or not all(number(value) for value in weights.values())
            or abs(sum(weights.values()) - 1) > 1e-9 or not number(frozen)):
        issues.append("baseline_weights_must_be_frozen_complete_distribution")
    for window in values:
        reasons = []
        if not isinstance(window, dict):
            issues.append("window_not_object")
            continue
        identity = window.get("id")
        if not isinstance(identity, str) or not 0 < len(identity) <= 128 or identity in windows:
            issues.append("window_id_missing_or_duplicate")
            continue
        start, end = window.get("window_start"), window.get("window_end")
        if (not number(start) or not number(end) or end - start != 86400
                or start % 3600 or end % 3600 or end > int(instant // 3600) * 3600):
            reasons.append("complete_24_utc_hours_required")
        account = window.get("account_id")
        if type(account) is not int or account <= 0 or window.get("stage") not in STAGES:
            reasons.append("account_and_stage_required")
        release = window.get("release") or {}
        if not isinstance(release, dict) or any(not isinstance(release.get(key), str) or not release[key] for key in (*RELEASE_KEYS, "flags_fingerprint")):
            reasons.append("release_and_configuration_proof_missing")
        flags = release.get("flags")
        if not isinstance(flags, dict) or set(flags) != PHASE1_FLAGS | PHASE2_FLAGS or any(type(v) is not bool for v in flags.values()):
            reasons.append("effective_account_flags_snapshot_missing")
        else:
            fingerprint = hashlib.sha256(json.dumps(flags, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if fingerprint != release.get("flags_fingerprint"):
                reasons.append("flags_fingerprint_mismatch")
            enabled = {key for key, value in flags.items() if value}
            if ((window.get("stage") == "baseline" and enabled)
                    or (window.get("stage") == "phase1" and enabled != PHASE1_FLAGS)
                    or (window.get("stage") == "phase2" and (not PHASE1_FLAGS <= enabled or not enabled & PHASE2_FLAGS))):
                reasons.append("actual_flags_do_not_match_declared_stage")
        if window.get("execution_coverage_verified") is not True or window.get("unmetered_channels") != 0:
            reasons.append("all_execution_processes_and_read_channels_not_verified")
        metrics = window.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        if metrics.get("available") is not True or metrics.get("complete") is not True:
            reasons.append("raw_metrics_incomplete")
        if metrics.get("current_hour_excluded") is not True or metrics.get("window_start") != start or metrics.get("window_end") != end:
            reasons.append("metrics_window_mismatch")
        for key in ("gap_hours", "missing_roles"):
            if metrics.get(key) != []:
                reasons.append(key + "_or_missing")
        for key in ("overflow_events", "counter_saturation_events"):
            if metrics.get(key) != 0:
                reasons.append(key + "_or_missing")
        if metrics.get("source_capacity_exceeded") is not False or metrics.get("pending_sdk_since") is not None:
            reasons.append("source_capacity_or_pending_entry")
        configured_roles = release.get("expected_roles")
        if (not isinstance(configured_roles, list) or not configured_roles
                or any(not isinstance(role, str) for role in configured_roles)
                or len(configured_roles) != len(set(configured_roles))
                or not {"backend", "growth", "celery"} <= set(configured_roles) <= EXECUTION_ROLES):
            reasons.append("configured_execution_roles_missing_or_invalid")
            configured_roles = []
        if set(metrics.get("expected_roles") or []) != set(configured_roles):
            reasons.append("expected_execution_roles_do_not_match_configuration")
        for role in configured_roles:
            if not number((metrics.get("active_roles") or {}).get(role)) or not metrics["active_roles"][role]:
                reasons.append("execution_role_missing:" + role)
        for key in ("coverage_started_at", "measurement_semantics_started_at", "business_semantics_started_at"):
            if not number(metrics.get(key)) or not number(start) or metrics[key] > start:
                reasons.append("window_before_" + key)
        if not number(metrics.get("sources_confirmed_through")) or not number(end) or metrics["sources_confirmed_through"] < end:
            reasons.append("source_not_confirmed_through_window")
        for key in ("measurement_version", "attribution_version", "completion_version"):
            if metrics.get(key) != release.get(key) or not release.get(key):
                reasons.append("mixed_" + key)
        if metrics.get("measurement_version") != "sdk_entry_v2" or metrics.get("complexity_version") != "business_demand_v1":
            reasons.append("current_sdk_entry_and_demand_complexity_required")
        if release.get("attribution_version") != "purpose_v2_lane_first" or release.get("completion_version") != "business_transition_v2":
            reasons.append("current_attribution_and_business_completion_required")
        rows = metrics.get("by_stage")
        if not isinstance(rows, list):
            rows = []
        selected = [row for row in rows if row.get("stage") == window.get("stage")]
        if len(selected) != 1 or any(row.get("entered_sdk", 0) for row in rows if row.get("stage") != window.get("stage")):
            reasons.append("mixed_measurement_stages")
        row = selected[0] if selected else {}
        if row.get("attribution_overflow") != 0:
            reasons.append("work_attribution_overflow_or_missing")
        all_reads = metrics.get("actual_reads")
        if not number(all_reads) or all_reads != metrics.get("known_entered_sdk") or all_reads != row.get("entered_sdk"):
            reasons.append("all_purpose_sdk_total_unknown_or_not_conserved")
        costs = dict.fromkeys(COMPLEXITIES, 0)
        completes = dict.fromkeys(COMPLEXITIES, 0)
        work_rows = row.get("work")
        if not isinstance(work_rows, list) or {item.get("workload") for item in work_rows} != {"review", "renewal"}:
            reasons.append("both_business_workloads_required")
            work_rows = []
        for work in work_rows:
            for complexity in COMPLEXITIES:
                stratum = (work.get("complexity") or {}).get(complexity) or {}
                if not number(stratum.get("entered_sdk")) or not number(stratum.get("completed_decisions")):
                    reasons.append("business_complexity_unknown:" + complexity)
                    continue
                costs[complexity] += stratum["entered_sdk"]
                completes[complexity] += stratum["completed_decisions"]
        if not sum(completes.values()):
            reasons.append("zero_independent_committed_completions")
        if any(costs[key] and not completes[key] for key in COMPLEXITIES):
            reasons.append("reads_in_zero_completion_stratum")
        if number(all_reads) and sum(costs.values()) > all_reads:
            reasons.append("work_cost_exceeds_all_channel_cost")
        if (row.get("business_completions") or {}).get("decisions") != sum(completes.values()):
            reasons.append("business_denominator_mismatch")
        business = window.get("business") or {}
        if not isinstance(business, dict):
            business = {}
        for key in BUSINESS_NUMBERS:
            if not number(business.get(key)):
                reasons.append("business_missing:" + key)
        arrivals = business.get("arrivals_by_complexity")
        if (business.get("arrivals_known") is not True or not isinstance(arrivals, dict)
                or set(arrivals) != set(COMPLEXITIES) or not all(number(v) for v in arrivals.values()) or not sum(arrivals.values())):
            reasons.append("independent_arrivals_unknown")
        if business.get("healthy_seconds") != 86400:
            reasons.append("unhealthy_or_inactive_window")
        if all(number(business.get(key)) for key in ("backlog_end", "backlog_start", "oldest_age_end", "oldest_age_start")):
            if business["backlog_end"] > business["backlog_start"] or business["oldest_age_end"] > business["oldest_age_start"]:
                reasons.append("backlog_or_oldest_age_increased")
        for key in ("duplicate_sends", "premature_protection_releases"):
            if number(business.get(key)) and business[key] > 0:
                safety.append(identity + ":" + key)
        if number(business.get("storage_bytes")) and business["storage_bytes"] >= 1024**3:
            safety.append(identity + ":physical_storage_limit_exceeded")
        windows[identity] = {**window, "costs": costs, "completes": completes, "all_reads": all_reads,
                             "business": business, "release": release, "reasons": reasons}
        if reasons:
            rejected.append({"window": identity, "reasons": sorted(set(reasons))})
    if len(pairs) < 3:
        issues.append("at_least_three_independent_pairs_required")
    used = set()
    chosen = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict) or any(pair.get(stage) not in windows for stage in STAGES):
            issues.append(f"pair_{index}:three_known_stage_windows_required")
            continue
        triple = [windows[pair[stage]] for stage in STAGES]
        if any(item["id"] in used for item in triple):
            issues.append(f"pair_{index}:window_reused")
        used.update(item["id"] for item in triple)
        if any(item["reasons"] for item in triple):
            continue
        if len({item["account_id"] for item in triple}) != 1 or [item["stage"] for item in triple] != list(STAGES):
            issues.append(f"pair_{index}:same_account_and_exact_stages_required")
        for key in RELEASE_KEYS:
            if len({item["release"][key] for item in triple}) != 1:
                issues.append(f"pair_{index}:mixed_{key}")
        if len({tuple(sorted(item["release"]["expected_roles"])) for item in triple}) != 1:
            issues.append(f"pair_{index}:mixed_execution_role_contract")
        if number(frozen) and frozen > triple[1]["window_start"]:
            issues.append("baseline_weights_frozen_after_optimized_window")
        for before, after in zip(triple, triple[1:], strict=False):
            if before["window_end"] > after["window_start"]:
                issues.append(f"pair_{index}:stage_windows_overlap_or_reversed")
            for complexity in COMPLEXITIES:
                if isinstance(weights, dict) and number(weights.get(complexity)) and weights[complexity] > 0:
                    if not before["completes"][complexity] or not after["completes"][complexity]:
                        issues.append(f"pair_{index}:zero_completed_stratum:{complexity}")
                if after["completes"][complexity] < before["completes"][complexity]:
                    issues.append(f"pair_{index}:business_completion_volume_declined:{complexity}")
                left, right = before["business"]["arrivals_by_complexity"][complexity], after["business"]["arrivals_by_complexity"][complexity]
                if abs(right - left) > max(1, left * 0.05):
                    issues.append(f"pair_{index}:arrival_complexity_unmatched:{complexity}")
            for key in ("ads_succeeded", "joins_completed"):
                if after["business"][key] < before["business"][key]:
                    issues.append(f"pair_{index}:business_volume_declined:{key}")
            for key in ("failed_jobs", "ads_unknown", "latency_p95_seconds"):
                if after["business"][key] > before["business"][key]:
                    issues.append(f"pair_{index}:business_quality_worsened:{key}")
            if after["all_reads"] >= before["all_reads"]:
                issues.append(f"pair_{index}:all_channel_net_reads_not_lower")
        chosen.append(triple)
    # Reject overlaps across pairs, even if a caller supplied different labels.
    active = sorted((windows[key] for key in used if not windows[key]["reasons"]), key=lambda w: (w["account_id"], w["window_start"]))
    for before, after in zip(active, active[1:], strict=False):
        if before["account_id"] == after["account_id"] and before["window_end"] > after["window_start"]:
            issues.append("cross_pair_window_overlap")
    if rejected or issues or safety:
        return {"status": "failed" if safety else "insufficient_evidence",
                "reasons": sorted(set(issues + safety)), "rejected_windows": rejected,
                "measured_reduction": None, "required_pairs": 3, "accepted_pairs": len(chosen)}
    baseline_counts = {key: sum(triple[0]["completes"][key] for triple in chosen) for key in COMPLEXITIES}
    if frozen < max(triple[0]["window_end"] for triple in chosen):
        return {"status": "insufficient_evidence", "reasons": ["weights_frozen_before_baseline_observation_complete"], "measured_reduction": None}
    total_baseline = sum(baseline_counts.values())
    if any(abs(weights[key] - baseline_counts[key] / total_baseline) > 1e-9 for key in COMPLEXITIES):
        return {"status": "insufficient_evidence", "reasons": ["weights_do_not_match_frozen_baseline_workload"], "measured_reduction": None}
    def cost(items: list[dict]) -> float:
        return sum(weights[c] * sum(item["costs"][c] for item in items) / sum(item["completes"][c] for item in items)
                   for c in COMPLEXITIES if weights[c] > 0)
    combined = [cost([triple[index] for triple in chosen]) for index in range(3)]
    if not all(value > 0 for value in combined):
        return {"status": "insufficient_evidence", "reasons": ["positive_comparable_cost_required"], "measured_reduction": None}
    per_pair = []
    for triple in chosen:
        values = [cost([item]) for item in triple]
        if not all(value > 0 for value in values):
            return {"status": "insufficient_evidence", "reasons": ["positive_pair_cost_required"], "measured_reduction": None}
        per_pair.append({"windows": [item["id"] for item in triple], "phase1": 1-values[1]/values[0], "phase2_extra": 1-values[2]/values[1]})
    reductions = {"phase1": 1-combined[1]/combined[0], "phase2_extra": 1-combined[2]/combined[1], "cumulative": 1-combined[2]/combined[0]}
    passed = reductions["phase1"] >= 0.20 - 1e-12 and reductions["phase2_extra"] >= 0.30 - 1e-12 and all(p["phase1"] >= 0 and p["phase2_extra"] >= 0 for p in per_pair)
    return {"status": "passed" if passed else "target_not_met", "reasons": [], "measured_reduction": reductions,
            "normalized_cost": dict(zip(STAGES, combined, strict=True)), "per_pair": per_pair,
            "raw_windows": [{"id": windows[key]["id"], "all_entered_sdk": windows[key]["all_reads"],
                "work_entered_sdk": sum(windows[key]["costs"].values()), "independent_completions": sum(windows[key]["completes"].values())} for key in sorted(used)],
            "scope": "same_account_all_governed_sdk_entries; not_wire_retries", "accepted_pairs": len(chosen)}


def analyse(document: dict, *, now: float | None = None) -> dict:
    try:
        return _analyse(document, now=now)
    except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError, OverflowError, RecursionError):
        return {"status": "insufficient_evidence", "reasons": ["malformed_or_incomplete_numeric_evidence"], "measured_reduction": None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    arguments = parser.parse_args()
    if arguments.input.stat().st_size > MAX_INPUT_BYTES:
        raise SystemExit("input exceeds bounded numeric evidence size")
    try:
        result = analyse(json.loads(arguments.input.read_text(encoding="utf-8")))
    except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError, OverflowError, RecursionError):
        result = {"status": "insufficient_evidence", "reasons": ["malformed_or_incomplete_numeric_evidence"], "measured_reduction": None}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
