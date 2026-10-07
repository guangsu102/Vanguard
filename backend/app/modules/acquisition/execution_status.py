"""Business readiness derived from the same purpose budgets as RPC admission."""
from __future__ import annotations

from datetime import datetime

from app.core.account.critical_fairness import purpose_budget


def read_status(rpc: dict, purpose: str) -> dict:
    view = purpose_budget(rpc, purpose)
    if rpc.get("state") in {"cooldown", "unavailable", "budget_wait"}:
        resume = max(filter(None, (rpc.get("resume_at"), view.get("resume_at"))), default=None)
        return {"state": rpc["state"], "reason": rpc.get("reason"), "resume_at": resume}
    waiting = bool(view.get("retry_after_seconds"))
    return {"state": "budget_wait" if waiting else "ready", "reason": "telegram_read_budget" if waiting else None,
            "resume_at": view.get("resume_at") if waiting else None}


def business_status(rpc: dict, *, now: datetime, ads_enabled: bool, join_enabled: bool,
                    executable_ad: int, executable_join: int, ad_due: datetime | None,
                    join_due: datetime | None, join_blocker: str | None) -> dict:
    result = {name: read_status(rpc, purpose) for name, purpose in {
        "ad": "ad_delivery", "join": "auto_join", "preview": "join_candidate_preview",
        "review": "group_qualification", "listener_control": "growth_listener",
        "history_sync": "growth_listener_refresh",
    }.items() if name != "history_sync"}
    sync = rpc.get("lanes", {}).get("sync", {})
    result["history_sync"] = read_status({**rpc, "lanes": {"routine": sync}}, "history_sync")
    for name, enabled, executable, due in (("ad", ads_enabled, executable_ad, ad_due),
                                            ("join", join_enabled, executable_join, join_due)):
        if not enabled:
            result[name] = {"state": "paused", "reason": "account_auto_" + ("ads" if name == "ad" else "join") + "_disabled", "resume_at": None}
        elif result[name]["state"] == "ready" and not executable:
            result[name] = {"state": "scheduled" if due and due > now else "waiting_conditions",
                            "reason": (join_blocker or "join_candidate_preview_due") if name == "join" else "ad_target_not_ready",
                            "resume_at": due.isoformat() if due and due > now else None}
    return result


def summary_status(business: dict, *, quarantined: bool, risk_reason: str | None) -> dict:
    if quarantined:
        return {"state": "quarantined", "reason": risk_reason, "resume_at": None}
    active = [business[name] for name in ("ad", "join") if business[name]["state"] != "paused"]
    if not active:
        return {"state": "paused", "reason": "account_automation_disabled", "resume_at": None}
    if any(item["state"] == "ready" for item in active):
        return {"state": "scheduled", "reason": None, "resume_at": None}
    blocked = [item for item in active if item["state"] in {"cooldown", "unavailable", "budget_wait"}]
    selected = blocked if len(blocked) == len(active) else active
    resumes = [item["resume_at"] for item in selected if item.get("resume_at")]
    item = selected[0]
    return {"state": item["state"], "reason": item.get("reason"), "resume_at": min(resumes, default=None)}
