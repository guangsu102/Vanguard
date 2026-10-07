"""Guarantee join and review progress within the existing critical reservation."""
from __future__ import annotations

JOIN_PURPOSES = {
    "auto_join", "join_candidate_preview", "join_verification",
    "qualification_verification", "auto_join_leave", "join_request_reconciliation",
}


def workload(purpose: str) -> str:
    return "join" if purpose in JOIN_PURPOSES else "review"


def other_hold(base: int, total_used: int, join_used: int, review_used: int, kind: str, join_hold: int = -1) -> int:
    """Each gets a quarter minimum; half plus borrowed capacity remains shared.

    Old, unattributed critical spend is charged across both groups. Introducing
    counters does not erase old usage or resurrect exhausted parent windows.
    """
    join_used = min(total_used, max(0, join_used))
    review_used = min(total_used - join_used, max(0, review_used))
    legacy = max(0, total_used - join_used - review_used)
    join_used += legacy // 2
    review_used += legacy - legacy // 2
    other = review_used if kind == "join" else join_used
    hold = max(0, base // 4 - other)
    return min(hold, join_hold) if kind == 'review' and join_hold >= 0 else hold


def add_workload_budgets(budget: dict, limits: dict) -> None:
    from app.core.account.reserved_reads import (
        allocations,
        available,
        lending_caps,
        transferred_caps,
    )

    usage = budget["usage"]
    groups = {}
    for kind in ("join", "review"):
        rooms, waits = [usage["minute"]["remaining"]], [budget["retry_after_seconds"]]
        parent = budget.get("lanes", {}).get("critical", {})
        if "remaining" in parent:
            rooms.append(parent["remaining"])
        waits.append(parent.get("retry_after_seconds", 0))
        for window, duration in (("hour", 3600), ("day", 86400)):
            used = [usage[f"{lane}_{window}"]["used"] for lane in ("ad", "survival", "critical", "sync", "routine")]
            base = allocations(limits[window])
            caps = transferred_caps(base, usage[f"survival_lent_{window}"]["used"], usage.get(f"sync_lent_{window}", {}).get("used", 0), usage.get(f"ad_lent_{window}", {}).get("used", 0))
            caps = lending_caps(caps, used, "critical", limits.get(f"survival_hold_{window}", -1), limits.get(f"sync_hold_{window}", -1), limits.get(f"ad_demand_hold_{window}", -1))
            hold = other_hold(base[2], used[2], usage[f"critical_join_{window}"]["used"],
                              usage[f"critical_review_{window}"]["used"], kind,
                              limits.get(f"join_idle_hold_{window}", -1))
            room = max(0, available(limits[window], used, caps, "critical") - hold)
            rooms.append(room)
            if room == 0:
                ttl = usage[window]["ttl_seconds"]
                waits.append(max(1, ttl) if ttl >= 0 else duration)
        groups[kind] = {"remaining": min(rooms), "retry_after_seconds": max(waits)}
    budget["critical_workloads"] = groups


def purpose_budget(snapshot: dict, purpose: str) -> dict:
    from app.core.account.rpc_budget_policy import read_lane

    lane = read_lane([], purpose)
    if purpose == "ad_qualification_refresh":
        return snapshot.get("lanes", {}).get("ad_refresh", snapshot.get("lanes", {}).get("ad", {}))
    if lane == "critical":
        specific = snapshot.get("critical_workloads", {}).get(workload(purpose))
        if specific is not None:
            return specific
    return snapshot.get("lanes", {}).get(lane, {})
