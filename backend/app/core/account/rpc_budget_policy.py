"""Protect advertising headroom without resetting shared account counters."""

SYNC_METHODS = {
    "updates.GetStateRequest",
    "updates.GetDifferenceRequest",
    "updates.GetChannelDifferenceRequest",
}
LISTENER_CONTROL_METHODS = {"users.GetUsersRequest", "updates.GetStateRequest"}
AD_PURPOSES = {"ad_delivery", "ad_result_reconciliation", "ad_qualification_refresh"}
CRITICAL_PURPOSES = {
    "group_qualification",
    "group_qualification_review",
    "auto_join",
    "join_verification",
    "qualification_verification",
    "qualification_identity_registration",
    "group_evaluate",
    "join_candidate_preview",
    "growth_event",
    "auto_join_leave",
    "qualification_exit_reconcile",
    "join_request_reconciliation",
}
BACKGROUND = {
    "group_metadata_sync",
    "auto_join_search",
    "search",
    "resource_search",
    "discover_related",
}
# Base reservations: advertising 35%, survival 15%, joining 25%, sync 20%, flex 5%.
# Critical reads may borrow forecast-idle survival tokens through reserved_reads;
# spent loans stay transferred until the shared window expires.
AD_SHARE_PERCENT = 35
SURVIVAL_SHARE_PERCENT = 15
LANE_SHARES = {"ad": AD_SHARE_PERCENT, "survival": SURVIVAL_SHARE_PERCENT, "sync": 20, "critical": 25, "routine": 5, "background": 5}
LANES = (*LANE_SHARES, "listener")
WINDOWS = (("minute", 60), ("hour", 3600), ("day", 86400)) + tuple(
    (f"{lane}_{window}", duration)
    for lane in ("background", "sync", "critical", "routine", "ad", "non_ad", "survival", "non_survival", "ad_pool", "join_reserved", "sync_reserved", "flex_reserved", "survival_lent", "listener", "critical_join", "critical_review", "sync_lent", "ad_lent")
    for window, duration in (("hour", 3600), ("day", 86400))
)


def read_lane(methods: list[str], purpose: str) -> str:
    if purpose == "ad_survival_check":
        return "survival"
    if purpose in AD_PURPOSES:
        return "ad"
    if purpose in {"growth_listener", "growth_listener_refresh"}:
        return "listener" if not methods or all(m in LISTENER_CONTROL_METHODS for m in methods) else "sync"
    if purpose in CRITICAL_PURPOSES:
        return "critical"
    return "background" if purpose in BACKGROUND else "routine"


def window_plan(limits: dict[str, int], lane: str) -> list[tuple[str, int, int]]:
    return [
        (name, duration, (
            limits["ad_pool_" + name.rsplit("_", 1)[1]] if name in {"ad_hour", "ad_day"}
            else limits["non_ad_" + name.rsplit("_", 1)[1]] if name in {"survival_hour", "survival_day"}
            else limits[name]
        ))
        for name, duration in WINDOWS
        if name in {"minute", "hour", "day"}
        or (name.startswith(lane + "_") and not name.startswith(("survival_lent_", "sync_lent_", "ad_lent_")))
        or (lane != "ad" and name.startswith("non_ad_"))
        or (lane not in {"ad", "survival"} and name.startswith("non_survival_"))
    ]
