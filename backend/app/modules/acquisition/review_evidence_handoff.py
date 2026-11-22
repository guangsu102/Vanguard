"""Transfer bounded-cache observations to the original durable review workflow.

No Telegram calls or business verdicts originate here. A protected slot is
released only after an independent database transaction commits its recovery cue.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass
from time import monotonic

import json
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import lazyload

from app.core.database import get_db_session

_last_run: dict[tuple[str, int], float] = {}
_last_sweep: dict[str, float] = {}
_running: set[str] = set()
MAINTENANCE_SECONDS = 300
logger = structlog.get_logger(__name__)


def _date(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _payload(row: Any) -> dict:
    try:
        value = json.loads(row.evidence_json or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _matching_proof(row: Any, control: dict) -> bool:
    from app.modules.acquisition.group_qualification import trial_proof
    from app.modules.acquisition.review_read_runtime import content_version

    evidence = _payload(row).get("evidence", [])
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if (not isinstance(item, dict) or item.get("message_id") != control["message_id"]
                or item.get("topic_id") is not None or not trial_proof([item])):
            continue
        if content_version(item.get("text", ""), sender_id=item.get("sender_id"),
                           date=item.get("date"), edit_date=item.get("edited_at")) == control["version"]:
            return True
    return False


def _anchor(row: Any, control: dict, outcome: str, now: datetime) -> None:
    """At most two minimal locations per existing audit; never persist a body."""
    snapshot = _payload(row)
    prior = snapshot.get("evidence_handoffs", [])
    prior = prior if isinstance(prior, list) else []
    item = {
        "message_id": control["message_id"], "version": control["version"],
        "account_id": control["account_id"], "peer": control["group_id"],
        "joined_at": _date(control["joined_at"]).isoformat(),
        "outcome": outcome, "recorded_at": now.isoformat(),
        "facts": sorted(control.get("facts", {})),
    }
    snapshot["evidence_handoffs"] = [
        *[old for old in prior if isinstance(old, dict) and old.get("message_id") != item["message_id"]][-1:],
        item,
    ]
    row.evidence_json = json.dumps(snapshot, ensure_ascii=False)


async def _can_wake(db: Any, config: dict, member: Any, group: Any, now: datetime) -> bool:
    from app.core.account.models import TelegramAccount
    from app.modules.acquisition.qualification_service import (
        account_block_reason,
        is_owned_group_target,
        manually_protected,
    )

    if (not config.get("enabled") or not config.get("execute_reviews", True)
            or member.status != "joined" or member.left_at is not None
            or member.ad_status == "paused"
            or member.review_status in {"manual_required", "owned_group_excluded", "exit_pending",
                                        "leave_failed", "membership_reconciliation"}
            or (member.ad_pause_until and member.ad_pause_until > now)
            or manually_protected(config, group, member)):
        return False
    rollout = (config.get("rollout_accounts") or {}).get(str(member.account_id), {})
    if rollout.get("phase") == "paused":
        return False
    scope = config.get("account_ids")
    if isinstance(scope, list) and scope and member.account_id not in scope:
        return False
    account = await db.get(TelegramAccount, member.account_id, populate_existing=True)
    return not account_block_reason(account, now) and not await is_owned_group_target(
        db, core_group_id=group.id, telegram_group_id=group.group_id
    )


async def _transfer(store: Any, handle: str, now: datetime) -> str:
    from app.core.group.identity import telegram_group_id_aliases
    from app.core.group.models import Group
    from app.core.group.models import GroupAccountMembership as Member
    from app.modules.acquisition.callback_evidence import save_hint
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_service import (
        enqueue_membership_review,
        latest_review,
        policy,
        scope_matches,
    )

    result = await store.get_observation(handle)
    if not result.ok:
        return "deferred"
    control = (result.data or {}).get("control", {})
    joined = _date(control.get("joined_at"))
    if joined is None:
        return "deferred"
    token = None
    outcome = "deferred"
    # This context commits on exit. The caller's review session cannot flush
    # then roll back evidence that this module has already released.
    async with get_db_session() as db:
        member_id = (control.get("metadata") or {}).get("membership_id")
        query = select(Member).options(lazyload(Member.account))
        if type(member_id) is int and member_id > 0:
            query = query.where(Member.id == member_id)
        else:
            query = query.where(Member.account_id == control["account_id"],
                                Member.telegram_group_id.in_(telegram_group_id_aliases(int(control["group_id"]))),
                                Member.joined_at == joined)
        member = await db.scalar(query)
        if member is not None:
            member_id = member.id
        # Follow the normal writer's audit -> membership lock order. A skipped
        # lock is a deferred attempt, never proof that the scope disappeared.
        audit = None
        if member_id is not None:
            audit_query = select(Audit).where(
                Audit.membership_id == member_id, Audit.account_id == control["account_id"],
                Audit.membership_joined_at == joined, Audit.state != "cancelled",
            ).order_by(desc(Audit.id)).limit(1)
            audit = await db.scalar(audit_query)
            if audit is not None:
                audit = await db.scalar(select(Audit).where(Audit.id == audit.id)
                                        .with_for_update(of=Audit, skip_locked=True)
                                        .execution_options(populate_existing=True))
                if audit is None:
                    return "deferred"
        if member is not None:
            member = await db.scalar(select(Member).options(lazyload(Member.account))
                                     .where(Member.id == member.id)
                                     .with_for_update(of=Member, skip_locked=True)
                                     .execution_options(populate_existing=True))
            if member is None:
                return "deferred"
        ended = (member is None or member.account_id != control["account_id"]
                 or int(control["group_id"]) not in telegram_group_id_aliases(member.telegram_group_id)
                 or member.joined_at != joined or member.left_at is not None
                 or member.status in {"left", "banned"})
        if ended:
            recorded = await store.record_control(handle, "not_applicable")
            if not recorded.ok:
                return "deferred"
            prepared = await store.prepare_handoff(handle, "not_applicable", now=now)
            if not prepared.ok:
                return "deferred"
            control = recorded.data
            token, outcome = prepared.data["token"], "not_applicable"
            if audit is not None:
                _anchor(audit, control, outcome, now)
        else:
            group = await db.get(Group, member.group_id, populate_existing=True)
            if group is None or member.telegram_group_id not in telegram_group_id_aliases(group.group_id):
                return "deferred"
            current = await latest_review(db, member.id, include_pending=True)
            if current is not None and (audit is None or current.id != audit.id):
                return "deferred"
            from app.modules.acquisition.group_qualification import POLICY_VERSION

            if audit is not None and (
                audit.group_id != group.id or audit.policy_version != POLICY_VERSION
                or audit.content_scope != "text_profile"
            ):
                return "deferred"
            scoped = audit is not None and scope_matches(audit, group, member)
            same_trial = scoped and audit.decision == "trial" and _matching_proof(audit, control)
            collected = _date(_payload(audit).get("collected_at")) if audit is not None else None
            if (same_trial and audit.state == "completed" and not control.get("facts")
                    and collected is not None and collected >= _date(control["ready_at"])
                    and not control.get("handoff")):
                await store.prepare_handoff(handle, "verified", now=now)
            elif not control.get("handoff"):
                facts = control.get("facts", {})
                kind = "deleted" if "deleted" in facts else "edited" if "edited" in facts else "unknown"
                # A gap/warning is not an early-release fact. Keep observing
                # until maturity instead of repeatedly persisting a rejected
                # UNKNOWN preparation. Concrete edit/delete facts stay prompt.
                if facts and (kind != "unknown" or now >= _date(control["ready_at"])):
                    await store.prepare_handoff(handle, kind, now=now)
            latest = await store.get_observation(handle)
            control = (latest.data or {}).get("control", {}) if latest.ok else {}
            pending = control.get("handoff")
            if not pending:
                if (control.get("facts") or control.get("maturity_scheduled_at") is not None
                        or now < _date(control["ready_at"])):
                    return "observing"
                config = await policy(db, fresh=True)
                if not await _can_wake(db, config, member, group, now):
                    return "observing"
                if audit is None:
                    audit = await enqueue_membership_review(
                        db, member, batch_id=f"evidence-v5:{member.id}:{joined.isoformat()}")
                # Do not preempt an unrelated grant, in-flight work or provider
                # retry. The original collector will consume its scoped hint.
                if audit.decision in {"allowed", "trial"} or audit.state not in {"queued", "completed"}:
                    return "observing"
                saved = _payload(audit)
                cues = saved.get("evidence_maturity_cues", [])
                cues = cues if isinstance(cues, list) else []
                prior = next((cue for cue in cues if isinstance(cue, dict)
                              and cue.get("version") == control["version"]
                              and cue.get("message_id") == control["message_id"]), {})
                from app.modules.acquisition.review_waits import event_backoff

                waiting = await event_backoff(db, audit, now)
                scheduled = bool(prior.get("scheduled_at"))
                cue = {"message_id": control["message_id"], "version": control["version"],
                       "ready_at": _date(control["ready_at"]).isoformat(),
                       "scheduled_at": prior.get("scheduled_at") or (None if waiting else now.isoformat())}
                saved["evidence_maturity_cues"] = [
                    *[old for old in cues if isinstance(old, dict)
                      and old.get("message_id") != control["message_id"]][-1:], cue]
                audit.evidence_json = json.dumps(saved, ensure_ascii=False)
                await save_hint(db, member, {
                    "joined_at": joined.isoformat(), "message_id": control["message_id"],
                    "message_date": _date(control["date"]).isoformat(),
                }, now)
                if not waiting and not scheduled:
                    audit.state, audit.next_retry_at = "queued", now
                    member.review_next_at = now
                outcome = "maturity_scheduled" if scheduled or not waiting else "maturity_waiting"
            else:
                token, outcome = pending["token"], pending["outcome"]
                if outcome == "verified" and not (
                    same_trial and audit.state == "completed" and not control.get("facts")
                    and collected is not None and collected >= _date(control["ready_at"])
                ):
                    return "deferred"
                # A cache timeout is only an UNKNOWN recovery cue. It never grants
                # or rejects permission, and unrelated durable grants stay intact.
                config = await policy(db, fresh=True)
                wake = await _can_wake(db, config, member, group, now)
                if audit is None:
                    if not wake:
                        return "deferred"
                    audit = await enqueue_membership_review(
                        db, member, batch_id=f"evidence-v5:{member.id}:{joined.isoformat()}"
                    )
                _anchor(audit, control, outcome, now)
                await save_hint(db, member, {
                    "joined_at": joined.isoformat(), "message_id": control["message_id"],
                    "message_date": _date(control["date"]).isoformat(),
                }, now)
                if wake and outcome != "verified":
                    if same_trial and control.get("facts"):
                        from app.modules.acquisition.qualification_events import request

                        await request(db, member, "gap", message_ids=[control["message_id"]],
                                      source_token=f"evidence-v5:{token}")
                    elif audit.decision not in {"allowed", "trial"} and audit.state in {"queued", "completed"}:
                        from app.modules.acquisition.review_waits import event_backoff

                        if not await event_backoff(db, audit, now):
                            audit.state, audit.next_retry_at = "queued", now
                            member.review_next_at = now
    # A later control fact changes the token and keeps the slot protected.
    if token and (await store.ack_handoff(handle, token)).ok:
        return outcome
    if outcome == "maturity_scheduled":
        recorded = await store.mark_maturity_scheduled(handle, now=now)
        return outcome if recorded.ok else "deferred"
    if outcome == "maturity_waiting":
        return outcome
    return "deferred"


async def maintain_evidence_store(*, account_id: int | None = None, force: bool = False) -> dict:
    """Sweep locally at most every five minutes per enabled account and path."""
    from app.modules.acquisition.review_read_runtime import maintenance_store

    stats = {"enabled": False, "throttled": 0, "swept": 0, "observing": 0,
             "verified": 0, "unknown": 0, "edited": 0, "deleted": 0,
             "not_applicable": 0, "deferred": 0, "failed": 0,
             "maturity_scheduled": 0, "maturity_waiting": 0,
             "maintenance_waiting": 0}
    store = maintenance_store(account_id)
    if store is None:
        return stats
    stats["enabled"] = True
    path = str(store.path)
    if path in _running:
        stats["throttled"] += 1
        return stats
    _running.add(path)
    lease_token = None
    try:
        listed = await store.list_observations(account_id=account_id)
        if not listed.ok:
            stats["failed"] += 1
            return stats
        rows = (listed.data or {}).get("observations", [])
        # Discover retained accounts from the store, even after grey rollout
        # is removed or admission is switched off. Maintenance cannot strand
        # old reservations simply because they are no longer new candidates.
        accounts = ([account_id] if account_id is not None else
                    sorted({item["control"]["account_id"] for item in rows}) or [0])
        retained_accounts = {item["control"]["account_id"] for item in rows}
        instant = time.monotonic()
        active = []
        for candidate in accounts:
            key = path, candidate
            # Protected rows use the durable state/lease below. A local account
            # timer must not hide a fresh control fact or a ready/deadline edge.
            if (candidate not in retained_accounts and not force
                    and instant - _last_run.get(key, -MAINTENANCE_SECONDS) < MAINTENANCE_SECONDS):
                stats["throttled"] += 1
            else:
                active.append(candidate)
                _last_run[key] = instant
        deadline_due = any(
            item["control"]["account_id"] in active
            and not item["control"].get("handoff")
            and datetime.utcnow() >= _date(item["control"]["deadline_at"])
            for item in rows
        )
        if active and (force or deadline_due
                       or instant - _last_sweep.get(path, -MAINTENANCE_SECONDS) >= MAINTENANCE_SECONDS):
            result = await store.sweep()
            if not result.ok:
                stats["failed"] += 1
                return stats
            stats["swept"] += 1
            _last_sweep[path] = instant
        claimed = await store.claim_maintenance(account_ids=active, force=force)
        if not claimed.ok:
            stats["failed"] += 1
            return stats
        lease_token = claimed.data["token"]
        stats["maintenance_waiting"] += claimed.data["waiting"]
        for item in claimed.data["observations"]:
            candidate = item["control"]["account_id"]
            try:
                outcome = await _transfer(store, item["handle"], datetime.utcnow())
                stats[outcome] += 1
            except Exception as exc:
                # A failed commit must leave the body/slot intact. The
                # original workflow can retry without any Telegram work.
                stats["failed"] += 1
                logger.warning("review_evidence_handoff_failed", account_id=candidate,
                               handle=item["handle"], error=type(exc).__name__)
    except Exception as exc:
        stats["failed"] += 1
        logger.warning("review_evidence_maintenance_failed", account_id=account_id,
                       error=type(exc).__name__)
    finally:
        try:
            if lease_token:
                finished = await store.finish_maintenance(lease_token)
                if not finished.ok:
                    stats["failed"] += 1
        finally:
            _running.discard(path)
    return stats


# Account-33 owner requests are optional maintenance, never serialized business.
OWNER_ACCOUNT_ID = 33


@dataclass
class _Maintenance:
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task[None] | None = None
    pending: bool = False
    next_at: float = 0


_state: _Maintenance | None = None
_stopping = False


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _drain(state: _Maintenance) -> None:
    try:
        while state.pending and not _stopping:
            delay = max(0, state.next_at - monotonic())
            if delay:
                await _pause(delay)
            # No await separates consuming the cue and starting maintenance.
            # A later request remains pending for the next bounded pass.
            state.pending = False
            state.next_at = monotonic() + MAINTENANCE_SECONDS
            try:
                # _transfer inside the existing maintainer opens get_db_session
                # for its own transaction; no caller session enters this task.
                await maintain_evidence_store(account_id=OWNER_ACCOUNT_ID)
            except asyncio.CancelledError:
                state.pending = True
                raise
            except Exception as exc:
                state.pending = True
                logger.warning("review_evidence_background_failed", error_type=type(exc).__name__)
                # Retry on a later owner request or the existing periodic loop.
                # A failure must not create an independent retry stream.
                return
    finally:
        if state.task is asyncio.current_task():
            state.task = None


def request_owner_maintenance(account_id: int | None) -> bool:
    """Record one coalesced cue; never wait for file I/O or a DB connection."""
    from app.core.account.session_exec import current_actor_account
    from app.modules.acquisition.qualification_engine import enabled_for

    if (_stopping or account_id != OWNER_ACCOUNT_ID or not enabled_for(account_id)
            or current_actor_account() != account_id):
        return False
    global _state
    loop = asyncio.get_running_loop()
    if _state is None or _state.loop is not loop:
        if _state is not None and _state.task is not None and not _state.task.done():
            return False
        _state = _Maintenance(loop)
    _state.pending = True
    if _state.task is None or _state.task.done():
        _state.task = loop.create_task(
            _drain(_state), name="review-evidence-maintenance-33", context=Context(),
        )
    return True


def resume_owner_maintenance() -> None:
    """Allow owner requests after the existing ingest lifecycle starts."""
    global _stopping
    _stopping = False


async def stop_owner_maintenance() -> None:
    """Cancel the single task without acknowledging any protected evidence."""
    global _stopping
    _stopping = True
    task = _state.task if _state is not None else None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
