"""A bounded work plan shared by scheduler forecasts and atomic admission.

These are conservative slice plans, not a promise that a missing proof can be
invented inside the slice. Unknown/full-history branches keep the old forecast.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.modules.acquisition.review_read_flags import measurement_stage, stage_enabled


@dataclass(frozen=True)
class ReadPlan:
    phase: str
    reads: int
    reason: str
    bounded: bool = False
    candidate_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReadStepPlan:
    """One payable step, derived locally before taking an account connection."""

    operation: str
    scope: str
    required_reads: int
    ready_at: datetime | None = None
    deadline: datetime | None = None
    reason: str = "missing_facts"
    version: int = 1
    missing_dependencies: tuple[str, ...] = ()
    candidate_ids: tuple[int, ...] = ()
    wake_conditions: tuple[str, ...] = ()

    @property
    def reads(self) -> int:
        return self.required_reads

    @property
    def phase(self) -> str:
        return self.operation

    @property
    def bounded(self) -> bool:
        return True


def _enabled(account_id: int | None) -> bool:
    from app.modules.acquisition.qualification_engine import enabled_for

    return enabled_for(account_id)


def step_plan(previous: dict, member: Any, now: datetime, *,
              event: dict | None = None, renewal: bool = False, force_refresh: bool = False) -> ReadStepPlan:
    """Waiting obligations have zero SDK cost; no full-recapture fallback."""
    from app.modules.acquisition.evidence_progress import date

    scope = f"membership:{getattr(member, 'id', 0)}:{getattr(member, 'joined_at', None)}"
    version = int(previous.get("target_version") or 1)
    active = previous.get("pending_collection") or previous
    maturity = date(active.get("next_evidence_maturity_at")) or date(active.get("maturity_at"))
    from app.modules.acquisition.qualification_engine import _provider_wait_progress
    provider = None if force_refresh else _provider_wait_progress(previous, event=event, now=now)
    if provider is not None and provider.get("collection_complete"):
        return ReadStepPlan("provider", scope, 0, reason="provider_pending", version=version,
                            missing_dependencies=("provider",), wake_conditions=("provider_ready",))
    if (not force_refresh and not event and maturity is not None and maturity > now
            and not (active.get("ai_pending") or active.get("ai_review_incomplete"))):
        return ReadStepPlan("maturity", scope, 0, maturity, maturity,
                            "candidate_not_mature", version, ("maturity",),
                            wake_conditions=("maturity_due", "relevant_event"))
    progress = active.get("collection_progress") or {}
    ids = tuple(dict.fromkeys(value for value in [
        *progress.get("pending_message_ids", []),
        *[item.get("message_id") for item in active.get("evidence", [])
          if item.get("source") == "recent_promotional_message"
          and item.get("sender_role") == "ordinary" and not item.get("system_account")
          and not item.get("warning_reply_ids") and item.get("accessible") is not False],
    ] if type(value) is int and value > 0))[:1]
    if (not force_refresh and not event and active.get("discovery_exhausted") and not ids
            and not (active.get("ai_pending") or active.get("ai_review_incomplete"))):
        return ReadStepPlan("event", scope, 0, reason="new_evidence_required", version=version,
                            wake_conditions=("new_candidate", "rules_changed", "manual_review"))
    missing = ["current_access", "own_permissions"]
    if ids:
        missing.extend(("candidate_current_content", "author_role", "warning_coverage"))
    elif renewal:
        missing.append("changed_dependencies")
    else:
        missing.extend(("rules", "candidate_discovery_page"))
    # Includes SDK/bootstrap overhead; actual reservations settle unused reads.
    return ReadStepPlan("candidate" if ids else "continuation" if progress else "first",
                        scope, 8, now, reason="target_missing_dependencies", version=version,
                        missing_dependencies=tuple(missing), candidate_ids=ids,
                        wake_conditions=("budget_available", "relevant_event"))


async def for_join(db: Any, account_id: int, group_id: int | None, now: datetime, *,
                   candidate: Any = None) -> ReadStepPlan:
    """Pay only this target's preview/bootstrap/confirmation, never historic debt."""
    if db is not None and group_id is not None:
        from app.core.group.models import Group
        from app.modules.acquisition import candidate_inventory as inventory

        group = await db.get(Group, group_id)
        if group is not None:
            if candidate is None:
                candidate = (await inventory.load(db, account_id, [group_id])).get(group_id)
            if (not isinstance(candidate, dict) or candidate.get("namespace") not in {"chat", "channel"}
                    or not inventory.fresh(candidate, group, account_id, now)):
                candidate = None
        else:
            candidate = None
    # Join RPCs remain governed by the existing join quota. These are read RPCs
    # needed around that mutation, including possible SDK initialization.
    cached = isinstance(candidate, dict) and candidate.get("status") in {
        "metadata_checked", "sampled"} and not candidate.get("preview_exclusion")
    return ReadStepPlan("join", f"join:{account_id}:{group_id}", 6 if cached else 8,
                        now, reason="target_join_steps", missing_dependencies=(
                            ("bootstrap", "member_confirmation") if cached else
                            ("bootstrap", "target_preview", "member_confirmation")),
                        wake_conditions=("budget_available", "target_changed"))


def plan(previous: dict, row: Any, member: Any, account: Any, event: dict | None,
         now: datetime, *, renewal: bool = False, force_refresh: bool = False,
         candidate_available: bool = False) -> ReadPlan:
    from app.modules.acquisition.review_cost import review_shape

    phase, reads = review_shape(previous, row, member, account, event, now, force_refresh=force_refresh)
    original = ReadPlan("renewal" if renewal else phase, 16 if renewal else reads, "original_forecast")
    if (account is None or not stage_enabled(account.id, "minimal_plan") or force_refresh
            or previous.get("read_plan_requires_full")):
        return original
    kinds = set((event or {}).get("kinds") or [])
    if renewal and kinds == {"membership"}:
        # Entity/full-info/self-permissions, SDK bootstrap margin, and an
        # optional current online count when unread rules become relevant.
        return ReadPlan("permissions_only", 6, "current_membership_permissions", True)
    if (not renewal and event is None and candidate_available
            and stage_enabled(account.id, "fast_path")):
        # One candidate batch plus metadata, own/author rights, root and full
        # warning replies. Failure returns to the normal bounded continuation.
        return ReadPlan("candidate", 12, "one_mature_candidate", True)
    if renewal and kinds and kinds <= {"rules", "membership"}:
        ids = set((event or {}).get("message_ids") or [])
        admins = {item.get("message_id") for item in previous.get("evidence", [])
                  if item.get("source") == "admin_rule"}
        if ids and len(ids | admins) <= 2 and previous.get("rules_evaluation_status") != "NOT_EVALUATED":
            # Existing grant: metadata and own rights + one proof root/thread
            # + up to two rule identities + optional online/admin capability.
            return ReadPlan("precise_event", 14, "bounded_rule_messages", True)
    return original


async def for_review(db: Any, previous: dict, row: Any, member: Any, group: Any,
                     account: Any, event: dict | None, now: datetime, *,
                     renewal: bool = False, force_refresh: bool = False) -> ReadPlan:
    """Resolve only local hints before the first network read/reservation."""
    if account is not None and _enabled(account.id):
        value = step_plan(previous, member, now, event=event, renewal=renewal, force_refresh=force_refresh)
        if value.reads and not value.candidate_ids and not event:
            from app.modules.acquisition.adaptive_frequency import canonical
            from app.modules.acquisition.review_read_runtime import candidate_ids
            marked = canonical(group.group_id, previous)
            ids = await candidate_ids(member, now, group_id=marked, mature_only=True) if marked else []
            if ids:
                from dataclasses import replace
                value = replace(value, operation="candidate", candidate_ids=tuple(ids[:1]))
        return value
    value = plan(previous, row, member, account, event, now,
                 renewal=renewal, force_refresh=force_refresh)
    if (renewal or account is None or member is None or force_refresh or event is not None
            or previous.get("read_plan_requires_full")
            or not stage_enabled(account.id, "minimal_plan")
            or not stage_enabled(account.id, "fast_path")):
        return value
    from datetime import timedelta

    from sqlalchemy import select

    from app.modules.acquisition.evidence_progress import date
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.qualification_identity import peer_identity
    from app.modules.acquisition.qualification_service import (
        fast_path_history_requires_full,
        qualification_peer_history,
        scope_matches,
    )
    if row is None or not scope_matches(row, group, member):
        return value
    peer = peer_identity(group.group_id, previous)
    if peer is None or fast_path_history_requires_full(await qualification_peer_history(db, peer)):
        return value
    if await db.scalar(select(AdDeliveryLog.id).where(
            AdDeliveryLog.account_id == account.id,
            AdDeliveryLog.group_id == group.id).limit(1)) is not None:
        return value
    candidates = []
    for item in previous.get("evidence", []):
        published, edited = date(item.get("date")), date(item.get("edited_at"))
        if (item.get("source") == "recent_promotional_message"
                and item.get("sender_role") == "ordinary" and not item.get("system_account")
                and item.get("topic_id") is None and type(item.get("message_id")) is int
                and published is not None and now-timedelta(hours=48) <= published <= now
                and max(published, edited or published)+timedelta(hours=24) <= now):
            candidates.append(item["message_id"])
    from app.modules.acquisition.adaptive_frequency import canonical
    from app.modules.acquisition.review_read_runtime import candidate_ids
    marked = canonical(group.group_id, previous)
    if marked is not None:
        candidates.extend(await candidate_ids(member, now, group_id=marked, mature_only=True))
    if not candidates:
        return value
    value = plan(previous, row, member, account, event, now, candidate_available=True)
    return ReadPlan(value.phase, value.reads, value.reason, value.bounded,
                    tuple(dict.fromkeys(candidates))[:1])


def apply(work: Any, value: ReadPlan, account_id: int, *, renewal: bool = False) -> None:
    if work is None:
        return
    work.phase = value.phase
    work.requested_reads = value.reads
    work.planned_reads = value.reads if value.bounded else None
    from app.core.account.read_metrics import set_work_plan
    phase = "event" if renewal else "maturity" if value.phase == "candidate" else (
        "continuation" if value.phase == "continuation" else "first")
    set_work_plan(measurement_stage(account_id), phase=phase,
                  complexity="simple" if value.bounded else "complex")


def finish_candidate(value: ReadPlan, snapshot: dict) -> None:
    """A deferred or incomplete small plan must not repeat the same small slice."""
    if isinstance(value, ReadStepPlan):
        snapshot.pop("read_plan_requires_full", None)
        return
    if value.phase != "candidate" or snapshot.get("decision_dependencies_complete") is True:
        return
    snapshot["read_plan_requires_full"] = True
    if isinstance(snapshot.get("pending_collection"), dict):
        snapshot["pending_collection"] = {**snapshot["pending_collection"], "read_plan_requires_full": True}
