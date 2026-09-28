"""Protect advertising headroom without resetting shared account counters."""

SYNC_METHODS = {
    "updates.GetStateRequest",
    "updates.GetDifferenceRequest",
    "updates.GetChannelDifferenceRequest",
}
AD_PURPOSES = {"ad_delivery", "ad_result_reconciliation"}
CRITICAL_PURPOSES = {
    "growth_event",
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
# Advertising owns 35%; survival checks own 15%; other lanes share 50%.
# Their existing ceilings are retained, not additional guarantees. Keeping the
# sync ceiling also preserves its sustained pacing during listener catch-up.
AD_SHARE_PERCENT = 35
SURVIVAL_SHARE_PERCENT = 15
LANE_SHARES = {"ad": AD_SHARE_PERCENT, "survival": SURVIVAL_SHARE_PERCENT, "sync": 50, "critical": 25, "routine": 10, "background": 15}
LANES = tuple(LANE_SHARES)
WINDOWS = (("minute", 60), ("hour", 3600), ("day", 86400)) + tuple(
    (f"{lane}_{window}", duration)
    for lane in ("background", "sync", "critical", "routine", "ad", "non_ad", "survival", "non_survival")
    for window, duration in (("hour", 3600), ("day", 86400))
)


def read_lane(methods: list[str], purpose: str) -> str:
    if purpose == "ad_survival_check":
        return "survival"
    if purpose in AD_PURPOSES:
        return "ad"
    if purpose in {"growth_listener", "growth_listener_refresh"}:
        return "sync"
    if purpose in CRITICAL_PURPOSES:
        return "critical"
    return "background" if purpose in BACKGROUND else "routine"


def window_plan(limits: dict[str, int], lane: str) -> list[tuple[str, int, int]]:
    return [
        (name, duration, limits[name])
        for name, duration in WINDOWS
        if name in {"minute", "hour", "day"}
        or name.startswith(lane + "_")
        or (lane != "ad" and name.startswith("non_ad_"))
        or (lane not in {"ad", "survival"} and name.startswith("non_survival_"))
    ]
