"""Read priorities share one hard account budget; no lane bypasses cooldowns."""

SYNC_METHODS = {
    "updates.GetStateRequest",
    "updates.GetDifferenceRequest",
    "updates.GetChannelDifferenceRequest",
}
CRITICAL_PURPOSES = {
    "ad_delivery",
    "ad_survival_check",
    "ad_result_reconciliation",
    "auto_join_leave",
    "qualification_exit_reconcile",
    "join_request_reconciliation",
}
BACKGROUND = {
    "join_candidate_preview",
    "group_metadata_sync",
    "auto_join_search",
    "search",
    "resource_search",
    "discover_related",
}
WINDOWS = (
    ("minute", 60),
    ("hour", 3600),
    ("day", 86400),
    ("background_hour", 3600),
    ("background_day", 86400),
    ("sync_hour", 3600),
    ("sync_day", 86400),
)


def read_lane(methods: list[str], purpose: str) -> str:
    # Business connection bootstrap may need GetState/GetDifference before a send.
    # The native update loop explicitly selects sync, regardless of lease purpose.
    if purpose in CRITICAL_PURPOSES:
        return "critical"
    if methods and any(method in SYNC_METHODS for method in methods):
        return "sync"
    return "background" if purpose in BACKGROUND else "routine"


def window_plan(limits: dict[str, int], lane: str) -> list[tuple[str, int, int]]:
    """Reserve the final quarter of the shared windows for critical evidence reads."""
    plan = []
    for name, duration in WINDOWS:
        if name in {"minute", "hour", "day"}:
            limit = limits[name] if lane == "critical" else max(1, limits[name] * 3 // 4)
        elif name.startswith(lane + "_"):
            limit = limits[name]
        else:
            continue
        plan.append((name, duration, limit))
    return plan
