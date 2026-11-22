"""Account-scoped, persistent qualification work and immutable final decisions.

The membership lock always precedes work/grant locks. Network calls never hold
these locks. A claim captures its event revision and cannot consume a later fact.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, String, and_, case, cast, false, func, or_, select

from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.modules.acquisition import qualification_sla_runtime as sla_measure
from app.modules.acquisition import qualification_ready_origin as ready_origin
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import (
    AutoJoinAttempt, GroupQualificationAudit, QualificationActiveGrant, QualificationWork,
)

ENGINE_VERSION = "efficiency_v1"
FAMILY = "qualification"
RUNNABLE_STATES = ("READY", "WAIT_BUDGET", "WAIT_PROVIDER", "WAIT_MATURITY")
HELD_REVIEW_STATES = {"manual_required", "exit_pending", "leave_failed", "membership_reconciliation", "owned_group_excluded"}
_WAKE_KEY = "qualification.efficiency_wakes"


def _stage_wake(db: Any, account_id: int, *, stages: tuple[str, ...] = ("review",), ready_at: datetime | None = None) -> None:
    session = getattr(db, "sync_session", None)
    if session is None or not enabled_for(account_id):
        return
    wakes = session.info.setdefault(_WAKE_KEY, {})
    previous = wakes.get(account_id)
    if previous is not None:
        stages = tuple(sorted(set(previous["stages"]) | set(stages)))
        ready_at = min(previous["ready_at"], ready_at) if previous["ready_at"] and ready_at else None
    wakes[account_id] = {"stages": stages, "ready_at": ready_at}


async def _publish_wakes(wakes: dict) -> None:
    from app.core.scheduler.growth_dispatch import wake_account
    for account_id, options in wakes.items():
        try:
            await wake_account(account_id, reason="qualification_target_ready", **options)
        except Exception:
            # Durable Work remains due; the bounded periodic scan is fallback.
            pass


def _after_commit(session: Any) -> None:
    if session.in_nested_transaction():
        return
    wakes = session.info.pop(_WAKE_KEY, {})
    if not wakes:
        return
    from sqlalchemy.util.concurrency import await_only, in_greenlet
    if in_greenlet():
        try:
            await_only(_publish_wakes(wakes))
        except Exception:
            pass


def _after_rollback(session: Any, *args: Any) -> None:
    session.info.pop(_WAKE_KEY, None)


def enabled_account_ids() -> set[int]:
    if not settings.GROWTH_EFFICIENCY_ENABLED:
        return set()
    raw = str(settings.GROWTH_EFFICIENCY_ACCOUNT_IDS or "").strip()
    # Reject malformed scope in full; a typo must never widen the rollout.
    if not raw or not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", raw):
        return set()
    return {int(value.strip()) for value in raw.split(",") if int(value.strip()) > 0}


def enabled_for(account_id: int | None) -> bool:
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        return account_id is not None and account_id > 0
    return account_id in enabled_account_ids()


def policy_scope_allows(config: dict, account_id: int) -> bool:
    """Use the same all-account override as dispatch and maintenance."""
    return (config.get("exit_all_accounts") is True
            or not config.get("account_ids")
            or account_id in config["account_ids"])


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _object(value: str | None, fallback: Any = None) -> Any:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {} if fallback is None else fallback


def event_version(event: dict | None) -> str:
    return hashlib.sha256(_json({key: value for key, value in (event or {}).items() if key != "queued"}).encode()).hexdigest()


def event_blocks_authorization(snapshot: dict, event: dict | None) -> bool:
    """Only proved observational/maintenance cues are independent of permission.

    A rule message may contain a warning, even if its ID differs from the ad;
    unknown event kinds therefore remain relevant until evidence proves otherwise.
    """
    if not event:
        return False
    kinds = set(event.get("kinds") or ())
    return not kinds or not kinds.issubset({
        "evidence_candidate", "business_value", "maintenance", "metadata",
        "online_count", "member_count", "ordinary_message",
    })


def _scope_key(member: Any, policy_settings: dict, profile: str) -> str:
    from app.core.group.identity import telegram_group_id_aliases
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_service import _ids
    # Only changes to the meaning of an authorization retire its evidence.
    # Scheduling, account rollout and business-maintenance switches are read
    # by their own runners; pausing them must not revoke a valid send grant.
    # Protecting an unrelated target cannot change this authorization. Keep
    # only membership-local facts; aliases are equivalent forms of one peer.
    semantic = {
        "manual_protected_group_ids": member.group_id in _ids(policy_settings.get("manual_protected_group_ids")),
        "protected_telegram_group_ids": bool(
            telegram_group_id_aliases(getattr(member, "telegram_group_id", None))
            & _ids(policy_settings.get("protected_telegram_group_ids"))
        ),
        "protected_membership_ids": member.id in _ids(policy_settings.get("protected_membership_ids")),
        "system_user_ids": policy_settings.get("system_user_ids", []),
    }
    for key, value in semantic.items():
        if isinstance(value, list):
            semantic[key] = sorted(value, key=_json)
    semantic["system_account_user_ids"] = policy_settings.get("system_account_user_ids", {})
    return hashlib.sha256(_json({
        "account": member.account_id, "membership": member.id, "group": member.group_id,
        "joined_at": member.joined_at, "policy": POLICY_VERSION, "profile": profile,
        "content_scope": "text_profile", "family": FAMILY,
        "policy_settings": semantic,
    }).encode()).hexdigest()


async def _scope(db: Any, member: Any) -> tuple[str, str, str]:
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_service import policy
    account = await db.get(TelegramAccount, member.account_id, populate_existing=True)
    profile = profile_fingerprint(account)
    scope = _scope_key(member, await policy(db, fresh=True), profile)
    return scope, POLICY_VERSION, profile


async def _member_lock(db: Any, membership_id: int) -> Any:
    return await db.scalar(select(GroupAccountMembership).where(
        GroupAccountMembership.id == membership_id,
    ).with_for_update(of=GroupAccountMembership).execution_options(populate_existing=True))


async def initial_slots(db: Any, account_id: int) -> int:
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        return 0
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_service import policy
    account = await db.get(TelegramAccount, account_id, populate_existing=True)
    if account is None:
        return 0
    profile, config = profile_fingerprint(account), await policy(db, fresh=True)
    rows = (await db.execute(select(QualificationWork, GroupAccountMembership).join(
        GroupAccountMembership, GroupAccountMembership.id == QualificationWork.membership_id,
    ).where(
        QualificationWork.account_id == account_id,
        QualificationWork.engine_version == ENGINE_VERSION,
        QualificationWork.family == FAMILY,
        QualificationWork.initial_collection_pending.is_(True),
        QualificationWork.state.in_(("READY", "RUNNING", "WAIT_BUDGET")),
        GroupAccountMembership.status == "joined",
        GroupAccountMembership.left_at.is_(None),
        or_(GroupAccountMembership.ad_status.is_(None), GroupAccountMembership.ad_status != "paused"),
        or_(GroupAccountMembership.review_status.is_(None), GroupAccountMembership.review_status.not_in(HELD_REVIEW_STATES)),
        QualificationWork.membership_joined_at.is_not_distinct_from(GroupAccountMembership.joined_at),
    ))).all()
    now = datetime.utcnow()
    return sum(work.scope_key == _scope_key(member, config, profile)
               and _initial_join_slot_active(work, _initial_join_source(work, member), now)
               for work, member in rows)


async def enqueue(db: Any, member: Any, *, reason: str = "initial", now: datetime | None = None,
                  initial_collection_pending: bool = False, event: dict | None = None,
                  join_attempt_id: int | None = None) -> QualificationWork:
    """Merge all triggers into the current scope; caller owns the transaction."""
    from app.modules.acquisition.qualification_events import pending
    now = now or datetime.utcnow()
    await db.flush()
    member = await _member_lock(db, member.id)
    if member is None:
        raise ValueError("qualification_membership_missing")
    scope, policy, profile = await _scope(db, member)
    join_source = ({"attempt_id": join_attempt_id, "membership_id": member.id,
                    "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                    "scope_key": scope, "confirmed": reason == "joined"}
                   if type(join_attempt_id) is int and join_attempt_id > 0
                   and reason in {"joined", "approval"} else None)
    obsolete = (await db.scalars(select(QualificationWork).where(
        QualificationWork.membership_id == member.id, QualificationWork.family == FAMILY,
        QualificationWork.membership_joined_at.is_not_distinct_from(member.joined_at),
        QualificationWork.scope_key != scope, QualificationWork.state != "STALE",
    ).with_for_update(of=QualificationWork))).all()
    for previous in obsolete:
        previous.state, previous.reason, previous.ready_at = "STALE", "scope_changed", None
        previous.initial_collection_pending = False
        previous.claim_token, previous.claim_until, previous.updated_at = None, None, now
    event = await pending(db, member) if event is None else event
    version = event_version(event)
    if event_blocks_authorization({}, event):
        grant = await db.get(QualificationActiveGrant, member.id, with_for_update=True, populate_existing=True)
        if grant is not None and grant.invalidated_at is None:
            await invalidate(db, member, "event:"+",".join((event or {}).get("kinds") or ["unknown"]), now=now)
    work = await db.scalar(select(QualificationWork).where(
        QualificationWork.scope_key == scope,
    ).with_for_update(of=QualificationWork).execution_options(populate_existing=True))
    if work is None:
        work = QualificationWork(
            membership_id=member.id, account_id=member.account_id, group_id=member.group_id,
            membership_joined_at=member.joined_at, scope_key=scope, policy_version=policy,
            profile_fingerprint=profile, engine_version=ENGINE_VERSION, family=FAMILY,
            kind="event" if event else reason, state="READY", reason=reason,
            event_version=version, event_json=_json(event or {}), ready_at=now,
            progress_json=_json({"join_initial_budget": join_source}) if join_source else "{}", revision=1,
            initial_collection_pending=bool(initial_collection_pending and member.status == "joined"),
        )
        db.add(work)
    elif work.event_version != version:
        work.revision += 1
        work.event_version, work.event_json = version, _json(event or {})
        work.state, work.kind, work.reason, work.ready_at = "READY", "event", reason, now
        work.claim_token, work.claim_until = None, None
        # Collected material is retained; its scoped event determines reuse.
    elif reason == "manual" or (reason == "scope_changed" and work.state in {"DONE", "WAIT_EVENT", "STALE"}):
        work.revision += 1
        work.state, work.kind, work.reason, work.ready_at = "READY", reason, reason, now
        work.claim_token, work.claim_until = None, None
    if join_source and join_source["confirmed"]:
        old_source = _object(work.progress_json).get("join_initial_budget") or {}
        old_source = old_source if isinstance(old_source, dict) else {}
        if (old_source.get("attempt_id") == join_attempt_id and old_source.get("confirmed") is False
                and work.state in {"WAIT_EVENT", "READY", "WAIT_BUDGET"}):
            # Confirming the original approval advances only its own first work.
            # Replaying a completed join never grants a new protected collection.
            work.revision += 1
            work.kind, work.state, work.reason, work.ready_at = "joined", "READY", "joined", now
            work.initial_collection_pending = True
            work.claim_token, work.claim_until = None, None
            work.progress_json = _json({**_object(work.progress_json), "join_initial_budget": join_source})
    if _object(work.progress_json).get("engine_technical_retry_revision") != work.revision:
        work.progress_json = _json(_reset_technical_retry(_object(work.progress_json), work.revision))
    work.updated_at = now
    member.review_next_at = work.ready_at if work.state not in {"DONE", "WAIT_EVENT", "STALE"} else None
    sla_measure.stage_ready(db, work)
    await db.flush()
    if work.state in RUNNABLE_STATES:
        _stage_wake(db, member.account_id, ready_at=work.ready_at)
    return work


async def invalidate(db: Any, member: Any, reason: str, *, now: datetime | None = None) -> None:
    """Revoke a target after a concrete fact, without editing its final audit."""
    now = now or datetime.utcnow()
    member = await _member_lock(db, member.id)
    if member is None:
        return
    grant = await db.get(QualificationActiveGrant, member.id, with_for_update=True, populate_existing=True)
    if grant is not None:
        grant.version += 1
        grant.invalidated_at, grant.invalidated_reason, grant.updated_at = now, reason[:160], now


async def active_authorization(db: Any, member: Any, *, now: datetime | None = None,
                               group: Any = None) -> GroupQualificationAudit | None:
    """Sole authority for enabled accounts; missing pointers never use history."""
    from app.modules.acquisition.qualification_events import pending
    from app.modules.acquisition.qualification_lifetime import authorization_current
    now = now or datetime.utcnow()
    if (member.status != "joined" or member.left_at is not None or member.ad_status == "paused"
            or member.review_status in HELD_REVIEW_STATES):
        return None
    grant = await db.get(QualificationActiveGrant, member.id, populate_existing=True)
    if grant is None or grant.invalidated_at or not grant.audit_id:
        return None
    scope, _, _ = await _scope(db, member)
    if grant.scope_key != scope:
        return None
    audit = await db.get(GroupQualificationAudit, grant.audit_id, populate_existing=True)
    if (audit is None or audit.account_id != member.account_id or audit.group_id != member.group_id
            or audit.membership_id != member.id or audit.membership_joined_at != member.joined_at
            or audit.policy_version != grant.policy_version or audit.content_scope != "text_profile"
            or not authorization_current(audit, now)):
        return None
    if event_blocks_authorization(_object(audit.evidence_json), await pending(db, member)):
        return None
    from app.modules.acquisition.qualification_service import policy, scope_matches
    from app.modules.acquisition.qualification_system_identity import covered_system_ids
    group = group if group is not None else await db.get(Group, member.group_id, populate_existing=True)
    if group is None or not scope_matches(audit, group, member):
        return None
    _, identity = await covered_system_ids(db, await policy(db, fresh=True), account_id=member.account_id)
    snapshot = _object(audit.evidence_json)
    if (not identity or snapshot.get("system_identity_coverage") is not True
            or snapshot.get("system_identity_fingerprint") != identity):
        return None
    return audit


@dataclass(frozen=True)
class Claim:
    work_id: int
    membership_id: int
    token: str
    revision: int
    scope_key: str
    event_version: str
    event: dict | None
    sla_episode: dict | None = None


async def _claimed(db: Any, claim: Claim) -> tuple[Any, Any] | tuple[None, None]:
    from app.modules.acquisition.qualification_events import pending
    member = await _member_lock(db, claim.membership_id)
    work = await db.get(QualificationWork, claim.work_id, with_for_update=True, populate_existing=True)
    if member is None or work is None:
        return None, None
    scope, _, _ = await _scope(db, member)
    if (work.claim_token != claim.token or work.revision != claim.revision
            or work.state != "RUNNING" or scope != claim.scope_key
            or work.scope_key != scope or work.event_version != claim.event_version
            or event_version(await pending(db, member)) != claim.event_version
            or member.status not in {"joined", "pending"} or member.left_at is not None
            or member.ad_status == "paused" or member.review_status in HELD_REVIEW_STATES):
        return None, None
    return work, member


def _initial_join_source(work: Any, member: Any) -> dict:
    progress = _object(work.progress_json)
    source = progress.get("join_initial_budget") or {}
    if not isinstance(source, dict):
        return {}
    if (not work.initial_collection_pending or work.account_id != member.account_id
            or work.group_id != member.group_id
            or (progress.get("pending_collection") or progress).get("collection_complete")
            or source.get("confirmed") is not True or type(source.get("attempt_id")) is not int
            or source.get("attempt_id", 0) <= 0 or source.get("membership_id") != member.id
            or source.get("scope_key") != work.scope_key
            or source.get("joined_at") != (member.joined_at.isoformat() if member.joined_at else None)):
        return {}
    return source


def _initial_join_slot_active(work: Any, source: dict, now: datetime) -> bool:
    if not source:
        return False
    if not source.get("first_read_token") or source.get("zero_sdk_released") is True:
        return True
    return bool(work.state == "RUNNING" and work.claim_token == source.get("first_claim_token")
                and work.claim_until is not None and work.claim_until > now)


async def initial_join_claim_current(db: Any, claim: Claim | None, account_id: int, group_id: int) -> bool:
    if not enabled_for(account_id) or claim is None:
        return False
    work, member = await _claimed(db, claim)
    return bool(work is not None and work.account_id == account_id and work.group_id == group_id
                and member.account_id == account_id and member.group_id == group_id
                and member.status == "joined" and work.claim_until is not None
                and work.claim_until > datetime.utcnow())


async def initial_join_budget_current(db: Any, claim: Claim | None, account_id: int, group_id: int,
                                      *, work_token: str | None = None) -> bool:
    """Authorize the new join's unfinished first collection under its live fence.

    The source is written with the original join outcome, never inferred from
    historical initial_pending labels. The governor rechecks it in its short
    DB phase before every SDK read; no transaction spans network I/O.
    """
    if settings.TELEGRAM_RPC_PACING_ENABLED or not settings.BUDGET_LEDGER_ENABLED or not enabled_for(account_id) or claim is None:
        return False
    work, member = await _claimed(db, claim)
    now = datetime.utcnow()
    if (work is None or work.account_id != account_id or work.group_id != group_id
            or member.account_id != account_id or member.group_id != group_id
            or member.status != "joined" or not work.initial_collection_pending
            or work.claim_until is None or work.claim_until <= now):
        return False
    source = _initial_join_source(work, member)
    if not source:
        return False
    prior_token = source.get("first_read_token")
    if prior_token and prior_token != work_token:
        # Only a completed, explicitly zero-SDK release may retry the first
        # slice. A missing journal, cancellation or crash never recreates it.
        if source.get("zero_sdk_released") is not True:
            return False
        from app.core.account.control_models import AccountBudgetReservation
        prior = await db.get(AccountBudgetReservation, "read:"+prior_token, populate_existing=True)
        if (prior is None or prior.account_id != account_id or prior.workload != "review"
                or prior.lane != "critical" or prior.state != "settled" or prior.consumed_reads != 0
                or prior.reason != "work_complete" or not 1 <= prior.reserved_reads <= 8):
            return False
    from app.modules.acquisition.models import AutoJoinAttempt
    attempt = await db.get(AutoJoinAttempt, source["attempt_id"], populate_existing=True)
    return bool(attempt is not None and attempt.account_id == account_id and attempt.group_id == group_id
                and attempt.status == "success" and attempt.reconciliation_status == "confirmed"
                and attempt.telegram_action_attempted and attempt.request_state == "sent")


async def bind_initial_join_read_work(db: Any, claim: Claim, account_id: int, group_id: int,
                                      work_token: str, limit: int) -> bool:
    """Bind one existing ledger reservation before its first SDK call."""
    if type(limit) is not int or not 1 <= limit <= 8 or not work_token:
        return False
    if not await initial_join_budget_current(db, claim, account_id, group_id, work_token=work_token):
        return False
    from app.core.account.control_models import AccountBudgetReservation
    row = await db.get(AccountBudgetReservation, "read:"+work_token, populate_existing=True)
    if (row is None or row.account_id != account_id or row.workload != "review" or row.lane != "critical"
            or row.state != "reserved" or row.reserved_reads != limit or row.consumed_reads != 0
            or row.lease_expires_at <= datetime.utcnow()):
        return False
    work, member = await _claimed(db, claim)
    source = _initial_join_source(work, member) if work is not None else {}
    if not source:
        return False
    source = {**source, "first_read_token": work_token, "first_read_limit": limit,
              "first_claim_token": claim.token, "zero_sdk_released": False}
    work.progress_json = _json({**_object(work.progress_json), "join_initial_budget": source})
    await db.commit()
    # A commit acknowledgement or owner change cannot authorize a stale read.
    return await initial_join_budget_current(db, claim, account_id, group_id, work_token=work_token)


async def finish_initial_join_read_work(read_work: Any, *, failed: bool) -> None:
    """End the single protected slice; certify zero SDK use only with its journal."""
    if (read_work.kind != "review" or read_work.budget_workload != "join"
            or read_work.budget_claim is None or not read_work.closed):
        return
    from app.core.account.budget_ledger import CONSUMED_PREFIX
    from app.core.account.control_models import AccountBudgetReservation
    from app.core.database import get_db_session
    from app.core.redis import get_redis
    try:
        zero_candidate = (not failed and not getattr(read_work, "budget_bind_failed", False)
                          and read_work.closed and read_work.reads == 0 and read_work.entered_reads == 0)
        journal_zero = False
        if zero_candidate:
            # Observe only a closed token, before taking any PG lock. Failure
            # or timeout is unknown; the short PG phase can never span Redis.
            import asyncio
            try:
                async with asyncio.timeout(0.5):
                    cache = await get_redis()
                    journal_zero = await cache.get(CONSUMED_PREFIX+"read:"+read_work.token) in {"0", b"0"}
            except Exception:
                journal_zero = False
        async with get_db_session() as db:
            work, member = await _claimed(db, read_work.budget_claim)
            source = _initial_join_source(work, member) if work is not None else {}
            if (not source or source.get("first_read_token") != read_work.token
                    or work.claim_until is None or work.claim_until <= datetime.utcnow()
                    or work.account_id != read_work.account_id or work.group_id != read_work.group_id):
                return
            zero = False
            if zero_candidate and journal_zero:
                row = await db.get(AccountBudgetReservation, "read:"+read_work.token, populate_existing=True)
                if (row is not None and row.account_id == read_work.account_id and row.workload == "review"
                        and row.lane == "critical" and row.state == "settled" and row.consumed_reads == 0
                        and row.reason == "work_complete" and 1 <= row.reserved_reads <= 8):
                    zero = True
            source = {**source, "zero_sdk_released": zero}
            work.progress_json = _json({**_object(work.progress_json), "join_initial_budget": source})
            if not zero:
                work.initial_collection_pending = False
                _stage_wake(db, work.account_id, stages=("join",))
            await db.commit()
    except Exception:
        # The durable first token stays consumed/unknown. It is never refunded.
        return


def _keep_initial_join_source(work: Any, snapshot: dict) -> dict:
    source = _object(work.progress_json).get("join_initial_budget") or {}
    source = source if isinstance(source, dict) else {}
    result = {key: value for key, value in snapshot.items() if key != "join_initial_budget"}
    if source:
        result["join_initial_budget"] = source
    return result


async def checkpoint_draft(db: Any, draft: Any) -> bool:
    """Persist completed stages independently of a final Audit, under its fence."""
    claim = getattr(draft, "_efficiency_claim", None)
    if claim is None:
        return False
    work, _ = await _claimed(db, claim)
    if work is None:
        raise ValueError("qualification_work_claim_changed")
    ready_origin.capture_admission(work, sla_measure.current_admission())
    work.progress_json = _json(_keep_initial_join_source(work, _object(draft.evidence_json)))
    draft.evidence_json = work.progress_json
    work.checkpoint_json = ready_origin.preserve_checkpoint(work.checkpoint_json, draft.checkpoint_json)
    work.checkpoint_stage = draft.checkpoint_stage
    work.checkpoint_revision = draft.checkpoint_revision
    work.updated_at = datetime.utcnow()
    snapshot = _object(work.progress_json)
    pending = snapshot.get("pending_collection") or snapshot
    if pending.get("collection_complete"):
        work.initial_collection_pending = False
    await db.flush()
    return True


async def on_decision(db: Any, member: Any, audit: GroupQualificationAudit, *,
                      claim: Claim | None = None, now: datetime | None = None) -> bool:
    """Publish a final business conclusion and its pointer atomically."""
    from app.modules.acquisition.qualification_events import acknowledge, pending
    from app.modules.acquisition.qualification_lifetime import authorization_current
    now = now or datetime.utcnow()
    work = None
    if claim is not None:
        work, member = await _claimed(db, claim)
        if work is None:
            return False
    else:
        member = await _member_lock(db, member.id)
        if member is None or await pending(db, member):
            return False
    scope, policy, profile = await _scope(db, member)
    evidence = _object(audit.evidence_json)
    if (audit.membership_id != member.id or audit.account_id != member.account_id
            or audit.group_id != member.group_id or audit.membership_joined_at != member.joined_at
            or audit.policy_version != policy or audit.content_scope != "text_profile"
            or evidence.get("profile_fingerprint") != profile
            or audit.decision not in {"allowed", "trial", "observe", "reject", "protected"}
            or audit.checked_at is None):
        return False
    if audit.decision in {"allowed", "trial"}:
        if claim is not None and event_version(evidence.get("collection_event")) != claim.event_version:
            return False
        from app.modules.acquisition.qualification_service import policy as current_policy, scope_matches
        from app.modules.acquisition.qualification_system_identity import covered_system_ids
        group = await db.get(Group, member.group_id, populate_existing=True)
        _, fingerprint = await covered_system_ids(db, await current_policy(db, fresh=True), account_id=member.account_id)
        if (group is None or not scope_matches(audit, group, member) or not fingerprint
                or evidence.get("system_identity_coverage") is not True
                or evidence.get("system_identity_fingerprint") != fingerprint
                or evidence.get("invalidated_at") or evidence.get("protected")):
            return False
    if claim is not None:
        # An observation is not proof that a critical change was reconciled.
        # Preserve that cue and its suspended pointer until a real conclusion.
        consume = audit.decision != "observe" or not event_blocks_authorization(evidence, claim.event)
        if consume and not await acknowledge(db, member, claim.event):
            return False
    if work is not None:
        ready_origin.capture_admission(work, sla_measure.current_admission())
    sla_measure.stage_local_step(db, plan_name="qualified_decision_commit")
    sla_measure.stage_finished(db, outcome=audit.decision, reason=audit.reason or "completed")
    db.add(audit)
    from sqlalchemy import inspect
    if inspect(audit).pending:
        from app.core.account.review_completions import stage_completion
        from app.core.account.read_work import current
        await stage_completion(db, audit, current(member.account_id), kind="review")
    await db.flush()
    grant = await db.get(QualificationActiveGrant, member.id, with_for_update=True, populate_existing=True)
    if grant is None and audit.decision != "observe":
        grant = QualificationActiveGrant(
            membership_id=member.id, account_id=member.account_id, group_id=member.group_id,
            membership_joined_at=member.joined_at, scope_key=scope, policy_version=policy,
            profile_fingerprint=profile, version=0,
        )
        db.add(grant)
    if audit.decision != "observe":
        grant.version += 1
        grant.scope_key, grant.policy_version, grant.profile_fingerprint = scope, policy, profile
        grant.membership_joined_at, grant.account_id, grant.group_id = member.joined_at, member.account_id, member.group_id
        grant.updated_at = now
        if audit.decision in {"allowed", "trial"} and authorization_current(audit, now):
            grant.audit_id, grant.invalidated_at, grant.invalidated_reason = audit.id, None, None
            member.review_status, member.ad_status = "approved", "active"
        else:
            grant.audit_id, grant.invalidated_at, grant.invalidated_reason = None, now, (audit.reason or audit.decision)[:160]
    if work is not None:
        work.claim_token, work.claim_until = None, None
        work.initial_collection_pending = False
        work.progress_json = _json({**evidence, "decision": audit.decision, "reason": audit.reason,
            "checked_at": audit.checked_at.isoformat(),
            "expires_at": audit.expires_at.isoformat() if audit.expires_at else None})
        if consume:
            # The captured event is retained in immutable Audit evidence, but
            # the work now tracks the acknowledged, empty pending-event state.
            work.event_version, work.event_json = event_version(None), "{}"
        work.state, work.reason, work.ready_at = "DONE", audit.reason, None
        work.updated_at = now
    if audit.decision in {"allowed", "trial"}:
        await enqueue_business_maintenance(db, member, now=now)
        _stage_wake(db, member.account_id, stages=("ads", "join"))
    return True


async def enqueue_business_maintenance(db: Any, member: Any, *, now: datetime | None = None,
                                        refresh: bool = False) -> QualificationWork:
    """Independent, low priority obligations cannot occupy qualification slots."""
    now = now or datetime.utcnow()
    scope, policy, profile = await _scope(db, member)
    maintenance_scope = hashlib.sha256(("business_maintenance:"+scope).encode()).hexdigest()
    work = await db.scalar(select(QualificationWork).where(QualificationWork.scope_key == maintenance_scope))
    if work is None:
        work = QualificationWork(
            membership_id=member.id, account_id=member.account_id, group_id=member.group_id,
            membership_joined_at=member.joined_at, engine_version=ENGINE_VERSION,
            family="business_maintenance", kind="business_maintenance", scope_key=maintenance_scope,
            policy_version=policy, profile_fingerprint=profile, state="READY", ready_at=now,
            initial_collection_pending=False, event_version=event_version(None),
        )
        db.add(work)
        await db.flush()
    elif refresh:
        work.state, work.ready_at = "READY", now
    _stage_wake(db, member.account_id, stages=("exit",), ready_at=work.ready_at)
    return work


async def business_decision(db: Any, member: Any, *, now: datetime | None = None,
                           require_fresh: bool = True) -> GroupQualificationAudit | None:
    """Exit conclusions are resolved independently of the positive send pointer."""
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_service import _date, qualifies_for_automatic_exit
    now = now or datetime.utcnow()
    scope, _, _ = await _scope(db, member)
    rows = (await db.scalars(select(GroupQualificationAudit).where(
        GroupQualificationAudit.membership_id == member.id,
        GroupQualificationAudit.account_id == member.account_id,
        GroupQualificationAudit.group_id == member.group_id,
        GroupQualificationAudit.membership_joined_at.is_not_distinct_from(member.joined_at),
        GroupQualificationAudit.policy_version == POLICY_VERSION,
        GroupQualificationAudit.state == "completed", GroupQualificationAudit.decision == "reject",
    ).order_by(GroupQualificationAudit.id.desc()).limit(10))).all()
    for audit in rows:
        snapshot = _object(audit.evidence_json)
        observed = _date(snapshot.get("collected_at"))
        if (snapshot.get("engine_scope_key") == scope and qualifies_for_automatic_exit(snapshot)
                and audit.checked_at and audit.checked_at <= now and observed and observed <= now
                and (not require_fresh or (audit.checked_at >= now-timedelta(minutes=5) and observed >= now-timedelta(minutes=5)))):
            return audit
    return None


async def run_business_maintenance(service: Any, *, account_id: int | None = None,
                                   limit: int = 1, now: datetime | None = None) -> dict:
    """Consume verified existing facts, and queue exits only under old switches.

    Missing 48h coverage is never manufactured by a new Telegram scan. The
    metadata collector and listener can wake this independent obligation later.
    """
    from app.core.group.collection import collection_for_groups
    from app.modules.acquisition.qualification_service import (
        _date, automatic_exit_reason, exit_scope_filters, manually_protected, policy,
    )
    from app.core.group.identity import is_owned_group_target
    now = now or datetime.utcnow()
    output = {"processed": 0, "results": []}
    accounts = enabled_account_ids()
    if settings.TELEGRAM_RPC_PACING_ENABLED:
        from app.core.account.models import AccountOperationConfig, AccountType
        accounts = set((await service.db.scalars(select(TelegramAccount.id).join(
            AccountOperationConfig, AccountOperationConfig.account_id == TelegramAccount.id).where(
                TelegramAccount.account_type == AccountType.PROMOTER,
                AccountOperationConfig.enabled.is_(True)))).all())
    if account_id is not None:
        accounts &= {account_id}
    if not accounts:
        return output
    config = await policy(service.db, fresh=True)
    query = select(QualificationWork).where(
        QualificationWork.account_id.in_(accounts), QualificationWork.family == "business_maintenance",
        QualificationWork.state.in_(("READY", "WAIT_EVENT")),
        or_(QualificationWork.ready_at.is_(None), QualificationWork.ready_at <= now),
    ).order_by(QualificationWork.ready_at, QualificationWork.id).limit(max(0, limit))
    for candidate in (await service.db.scalars(query)).all():
        member = await _member_lock(service.db, candidate.membership_id)
        work = await service.db.get(QualificationWork, candidate.id, with_for_update=True, populate_existing=True)
        if member is None or member.status != "joined" or member.left_at or member.ad_status == "paused" or member.review_status in HELD_REVIEW_STATES:
            work.state, work.ready_at = "STALE", None
            continue
        scope, policy_version, profile = await _scope(service.db, member)
        if work.scope_key != hashlib.sha256(("business_maintenance:"+scope).encode()).hexdigest():
            work.state, work.ready_at = "STALE", None
            continue
        group = await service.db.get(Group, member.group_id)
        if group is None or manually_protected(config, group, member) or await is_owned_group_target(
            service.db, core_group_id=member.group_id, telegram_group_id=member.telegram_group_id,
        ):
            work.state, work.reason, work.ready_at = "DONE", "protected", None
            continue
        grant = await service.db.get(QualificationActiveGrant, member.id)
        audit = await service.db.get(GroupQualificationAudit, grant.audit_id) if grant and grant.audit_id else None
        snapshot = dict(_object(audit.evidence_json) if audit else {})
        source_time = _date(snapshot.get("collected_at"))
        if not source_time or not now-timedelta(minutes=5) <= source_time <= now:
            snapshot["member_count_verified"] = False
            snapshot["online_count_source"] = "unknown"
        facts = (await collection_for_groups(service.db, [group.group_id])).get(group.group_id, {})
        for name in ("member_count", "online_count"):
            stamp = _date(facts.get(name+"_checked_at"))
            if stamp and now-timedelta(minutes=5) <= stamp <= now and type(facts.get(name)) is int:
                snapshot[name] = facts[name]
                snapshot[name+"_verified"] = True
                snapshot[name+"_source"] = str(facts.get(name+"_source") or "existing_group_collection").split(":", 1)[-1]
        # Each business arm requires its own fresh proof. Old full history
        # cannot acquire a new observation time merely from fresh metadata.
        if not source_time or not now-timedelta(minutes=5) <= source_time <= now:
            snapshot.pop("coverage", None)
            snapshot.pop("ordinary_advertising_last_48h_count", None)
            snapshot["exit_group_rule_ban"] = None
        snapshot["permissions"] = {**snapshot.get("permissions", {}), "member": True}
        reason = automatic_exit_reason(snapshot)
        # Canonical reasons are owned by automatic_exit_reason; exclude only
        # account permission denials, which use the qualification risk lane.
        confirmed = reason is not None and reason not in {"account_permanent_send_restriction", "account_long_send_restriction"}
        work.updated_at = now
        if not confirmed:
            work.state, work.reason, work.ready_at = "WAIT_EVENT", "business_evidence_missing", now+timedelta(hours=6)
            work.missing_dependencies_json = _json(["fresh_business_facts_or_complete_48h_coverage"])
        else:
            work.progress_json = _json({"reason": reason, "source_audit_id": audit.id if audit else None, "metadata_key": "group.collection."+str(group.group_id), "observed_at": now})
            work.state, work.reason, work.ready_at = "DONE", reason, None
            try:
                membership_ids, reasons = exit_scope_filters(config)
                scoped = ((membership_ids is None or member.id in membership_ids) and (reasons is None or reason in reasons))
                if not policy_scope_allows(config, member.account_id):
                    scoped = False
            except ValueError:
                scoped = False
            if config.get("enabled") and config.get("execute_exits") and scoped:
                observed = source_time
                if reason == "members_below_50":
                    observed = _date(facts.get("member_count_checked_at")) or source_time
                elif reason == "group_ad_ban_and_online_below_2":
                    observed = min(source_time, _date(facts.get("online_count_checked_at")) or source_time)
                if observed is None:
                    continue
                evidence = {**snapshot, "decision": "reject", "reason": reason, "collected_at": observed.isoformat(),
                    "checked_at": now.isoformat(), "engine_scope_key": scope, "qualification_work_family": "business_maintenance",
                    "account_id": member.account_id, "group_id": member.group_id, "telegram_group_id": group.group_id,
                    "policy_version": policy_version, "content_scope": "text_profile", "profile_fingerprint": profile,
                    "business_source_audit_id": audit.id if audit else None}
                final = GroupQualificationAudit(
                    batch_id=f"business:{work.id}:{uuid4().hex}", membership_id=member.id, account_id=member.account_id,
                    group_id=member.group_id, membership_joined_at=member.joined_at, policy_version=policy_version,
                    content_scope="text_profile", state="completed", decision="reject", reason=reason,
                    checked_at=now, expires_at=now+timedelta(minutes=5), evidence_json=_json(evidence),
                )
                service.db.add(final)
                await invalidate(service.db, member, "business:"+reason, now=now)
                member.review_status, member.ad_status, member.review_next_at = "exit_pending", "blocked", now
                member.leave_requested_at = member.leave_requested_at or now
                _stage_wake(service.db, member.account_id, stages=("exit",))
        output["processed"] += 1
        output["results"].append({"work_id": work.id, "family": work.family, "state": work.state, "reason": work.reason})
    await service.db.commit()
    return output


TECHNICAL_RETRY_SECONDS = (60, 300, 900)
TECHNICAL_RETRY_FIELDS = frozenset({
    "engine_technical_retry_revision", "engine_technical_failures",
    "technical_failures", "technical_recovery_started_at", "technical_recovery_deadline_at",
    "technical_recovery_exhausted", "technical_recovery_event_key", "technical_errors",
    "lease_wait_started_at",
})
TECHNICAL_WAIT_REASONS = frozenset({
    "telegram_read_budget", "telegram_read_slice", "read_step_not_ready", "telegram_rpc_cooldown",
    "telegram_rpc_guard_unavailable", "account_unavailable", "AccountOperationLeaseBusy",
    "session_owner_required", "session_owner_unavailable", "session_owner_request_expired",
    "session_owner_stage_timeout", "session_owner_shutdown", "FloodWaitError", "SlowModeWaitError",
    "qualification_candidate_full_required", "qualification_history_refresh_required",
})


def _reset_technical_retry(snapshot: dict, revision: int) -> dict:
    # A new event changes scheduling state, never collected facts or cursors.
    value = {key: item for key, item in snapshot.items() if key not in TECHNICAL_RETRY_FIELDS}
    if value.get("reason") == "group_identity_mismatch":
        value.pop("reason", None)
    if isinstance(value.get("pending_collection"), dict):
        value["pending_collection"] = _reset_technical_retry(value["pending_collection"], revision)
    value.update(engine_technical_retry_revision=revision, engine_technical_failures=0)
    return value


def _provider_wait_progress(snapshot: dict, reason: str | None = None, *, event: Any = ..., now: datetime | None = None) -> dict | None:
    """Project only the still-current semantic work; never flatten durable facts."""
    from app.modules.acquisition.qualification_events import same_evidence_event
    from app.modules.acquisition.evidence_progress import date, evidence_maturity
    now = now or datetime.utcnow()
    pending = snapshot.get("pending_collection")
    progress = pending if isinstance(pending, dict) else snapshot
    if (reason == "group_identity_mismatch" or reason in TECHNICAL_WAIT_REASONS
            or snapshot.get("local_rpc_defer_reason")
            or any(value.get(key) for value in (snapshot, progress)
                   for key in ("scope_collection_refresh", "collection_needs_refresh", "invalidated_at"))
            or not (progress.get("ai_pending") or progress.get("ai_review_incomplete"))):
        return None
    if isinstance(pending, dict) and progress.get("collection_complete") is not True:
        return None
    collected = date(progress.get("collected_at"))
    maturity = date(progress.get("next_evidence_maturity_at")) or date(progress.get("maturity_at"))
    inferred_maturity = evidence_maturity(progress, now)
    if (collected is None or not now-timedelta(hours=24) <= collected <= now
            or (maturity is not None and maturity <= now)
            or (inferred_maturity is not None and inferred_maturity <= now+timedelta(seconds=1))):
        # Without a proved checkpoint, expired/due materials need a payable live step.
        return None
    if event is not ... and not same_evidence_event(progress.get("collection_event"), event):
        return None
    for key in ("account_id", "group_id", "telegram_group_id", "policy_version", "content_scope"):
        if snapshot.get(key) is not None and progress.get(key) is not None and snapshot[key] != progress[key]:
            return None
    return progress


def _technical_wait_only(snapshot: dict, reason: str | None, *, event: Any = ..., now: datetime | None = None) -> bool:
    errors = snapshot.get("technical_errors") or ()
    return bool(
        reason in TECHNICAL_WAIT_REASONS or snapshot.get("local_rpc_defer_reason")
        or _provider_wait_progress(snapshot, reason, event=event, now=now) is not None
        or str(reason or "").startswith("group_rules_ai_")
        or any(isinstance(error, str) and ("FloodWait" in error or "SlowModeWait" in error
                or error in {"RpcDeferred", "AccountOperationLeaseBusy", "account_unavailable"}) for error in errors)
    )


def _technical_retry_snapshot(snapshot: dict, previous: dict, revision: int,
                              reason: str | None, now: datetime, *, event: Any = ...) -> dict:
    value = dict(snapshot)
    same_revision = previous.get("engine_technical_retry_revision") == revision
    failures = max(0, int(previous.get("engine_technical_failures") or 0)) if same_revision else 0
    value.update(engine_technical_retry_revision=revision, engine_technical_failures=failures)
    failed = bool(value.get("technical_errors") or str(reason or "").startswith("review_exception:"))
    if failed or _technical_wait_only(value, reason, event=event, now=now):
        # Failed I/O cannot discard already collected same-scope facts.
        for key in ("collection_progress", "renewal_progress", "evidence", "pending_collection"):
            if (key == "pending_collection" and value.get("collection_complete") is True
                    and (value.get("ai_pending") or value.get("ai_review_incomplete"))):
                # A returned current semantic snapshot supersedes its older draft envelope.
                continue
            if key not in value and key in previous:
                value[key] = previous[key]
    if reason == "group_identity_mismatch":
        value["technical_recovery_exhausted"] = True
        return value
    if failed and not _technical_wait_only(value, reason, event=event, now=now):
        failures += 1
        exhausted = failures > len(TECHNICAL_RETRY_SECONDS)
        value.update(engine_technical_failures=failures, technical_recovery_exhausted=exhausted)
        value["review_trigger"] = "new_evidence_required" if exhausted else "technical_retry"
        if not exhausted:
            value["retry_after_seconds"] = TECHNICAL_RETRY_SECONDS[failures - 1]
    return value


def _wait(snapshot: dict, reason: str | None, now: datetime, *, event: Any = ...) -> tuple[str, datetime | None]:
    from app.modules.acquisition.qualification_service import _date
    if reason == "group_identity_mismatch":
        return "WAIT_EVENT", None
    if reason in {"telegram_read_budget", "telegram_read_slice", "read_step_not_ready"}:
        return "WAIT_BUDGET", now + timedelta(seconds=max(1, int(snapshot.get("retry_after_seconds") or 60)))
    provider = _provider_wait_progress(snapshot, reason, event=event, now=now)
    active = snapshot.get("pending_collection") or snapshot
    from app.modules.acquisition.evidence_progress import evidence_maturity
    active_maturity = _date(active.get("next_evidence_maturity_at")) or _date(active.get("maturity_at"))
    inferred_maturity = evidence_maturity(active, now)
    if (provider is None and reason not in TECHNICAL_WAIT_REASONS
            and not snapshot.get("local_rpc_defer_reason")
            and not snapshot.get("technical_errors")
            and not str(reason or "").startswith("review_exception:")
            and not snapshot.get("technical_recovery_exhausted")
            and (active.get("ai_pending") or active.get("ai_review_incomplete"))
            and (active.get("collection_complete") is True
                 or (active_maturity is not None and active_maturity <= now)
                 or (inferred_maturity is not None and inferred_maturity <= now+timedelta(seconds=1)))):
        return "READY", now
    maturity_source = provider if provider is not None else snapshot
    maturity = _date(maturity_source.get("maturity_at") or maturity_source.get("next_evidence_maturity_at"))
    if provider is not None and provider.get("ai_pending") and provider.get("ai_request_started"):
        return "WAIT_PROVIDER", now+timedelta(seconds=30)
    if maturity and maturity > now:
        return "WAIT_MATURITY", maturity
    if provider is None and snapshot.get("ai_final") is True and snapshot.get("ai_decision") == "fail" and not snapshot.get("collection_needs_refresh"):
        return "WAIT_EVENT", None
    if provider is not None or str(reason or "").startswith("group_rules_ai_"):
        # Identical semantic material already has a durable once-only decision.
        # Provider retries are scheduled locally; Telegram collection stays intact.
        terminal = reason in {"group_rules_ai_provider_content_rejected", "group_rules_ai_destination_unapproved", "group_rules_ai_disabled"}
        return ("WAIT_EVENT", None) if terminal else ("WAIT_PROVIDER", now + timedelta(minutes=5))
    progress = snapshot.get("collection_progress") or {}
    if snapshot.get("discovery_exhausted") or snapshot.get("history_exhausted") or progress.get("terminal") or snapshot.get("wait_for_evidence_event"):
        return "WAIT_EVENT", None
    if _technical_wait_only(snapshot, reason, event=event, now=now):
        return "WAIT_BUDGET", now + timedelta(seconds=max(60, int(snapshot.get("retry_after_seconds") or 60)))
    if snapshot.get("technical_recovery_exhausted"):
        return "WAIT_EVENT", None
    if snapshot.get("technical_errors") or str(reason or "").startswith("review_exception:"):
        # READY can have a future ready_at; software retries are not budget waits.
        return "READY", now + timedelta(seconds=max(60, int(snapshot.get("retry_after_seconds") or 60)))
    if snapshot.get("efficiency_v1") and not snapshot.get("discovery_exhausted"):
        return "READY", now
    return "WAIT_EVENT", None


async def defer(db: Any, claim: Claim, *, snapshot: dict, reason: str | None,
                state: str | None = None, ready_at: datetime | None = None,
                draft: Any = None, now: datetime | None = None) -> bool:
    """A wait never changes or revokes an earlier final business conclusion."""
    now = now or datetime.utcnow()
    work, member = await _claimed(db, claim)
    if work is None:
        return False
    if reason == "group_identity_mismatch":
        # The live target identity proof failed; only this membership loses its grant.
        await invalidate(db, member, "group_identity_mismatch", now=now)
    ready_origin.capture_admission(work, sla_measure.current_admission())
    snapshot = _keep_initial_join_source(work, snapshot)
    snapshot = _technical_retry_snapshot(snapshot, _object(work.progress_json), claim.revision, reason, now, event=claim.event)
    if state is None:
        state, ready_at = _wait(snapshot, reason, now, event=claim.event)
    sla_provider_ready = False
    provider = _provider_wait_progress(snapshot, reason, event=claim.event, now=now)
    if state == "WAIT_PROVIDER" and provider is not None and provider.get("ai_material_key"):
        from app.core.settings_models import SystemSetting
        cached = await db.get(SystemSetting, "qualification.ai.once."+provider["ai_material_key"], populate_existing=True)
        if cached is not None and _object(cached.value).get("phase") == "completed":
            # A fast provider can publish while this Work still says RUNNING.
            # Its durable completed cache is the wake cue for this commit.
            state, ready_at, reason = "READY", now, "provider_completed"
            sla_provider_ready = True
    work.state, work.reason, work.ready_at = state, (reason or "evidence_required")[:160], ready_at
    work.progress_json = _json(snapshot)
    if draft is not None:
        work.checkpoint_json, work.checkpoint_stage, work.checkpoint_revision = ready_origin.preserve_checkpoint(work.checkpoint_json, draft.checkpoint_json), draft.checkpoint_stage, draft.checkpoint_revision
    work.claim_token, work.claim_until, work.updated_at = None, None, now
    active = snapshot.get("pending_collection") or snapshot
    local_plan = None
    if provider is None and (active.get("ai_pending") or active.get("ai_review_incomplete")):
        from app.modules.acquisition.review_read_plan import step_plan
        candidate_plan = step_plan(snapshot, member, now, event=claim.event)
        if candidate_plan.required_reads or reason not in TECHNICAL_WAIT_REASONS:
            local_plan = candidate_plan
    work.next_step_reads = (local_plan.required_reads if local_plan is not None else
        0 if provider is not None and state in {"WAIT_PROVIDER", "READY", "WAIT_EVENT"} else
        max(0, int((snapshot.get("read_plan") or {}).get("required_reads", 0))))
    from app.modules.acquisition.qualification_service import _date
    work.deadline_at = _date((snapshot.get("read_plan") or {}).get("deadline"))
    work.missing_dependencies_json = _json(list(local_plan.missing_dependencies) if local_plan is not None else ["provider"] if provider is not None and state in {"WAIT_PROVIDER", "READY", "WAIT_EVENT"} else snapshot.get("missing_dependencies") or (snapshot.get("read_plan") or {}).get("missing_dependencies") or [])
    if state in {"WAIT_PROVIDER", "WAIT_MATURITY", "WAIT_EVENT", "DONE", "STALE"} or snapshot.get("collection_complete"):
        work.initial_collection_pending = False
    member.review_next_at = ready_at
    sla_measure.stage_ready(db, work, dependency_ready_verified=("provider_completed" if sla_provider_ready else None))
    if state in RUNNABLE_STATES:
        _stage_wake(db, member.account_id, ready_at=ready_at)
    if not work.initial_collection_pending:
        _stage_wake(db, member.account_id, stages=("join",))
    return True


async def bootstrap_member(db: Any, member: Any, *, now: datetime | None = None) -> QualificationWork:
    """Adopt locally verified old evidence once, preserving its original times."""
    from app.modules.acquisition.qualification_events import pending
    from app.modules.acquisition.qualification_lifetime import authorization_current
    from app.modules.acquisition.qualification_service import _payload, scope_matches
    now = now or datetime.utcnow()
    member = await _member_lock(db, member.id)
    scope, _, profile = await _scope(db, member)
    existing = await db.scalar(select(QualificationWork).where(QualificationWork.scope_key == scope))
    if existing is not None:
        event = await pending(db, member)
        return await enqueue(db, member, reason="event", event=event, now=now) if event and event_version(event) != existing.event_version else existing
    work = await enqueue(db, member, reason="migration", now=now)
    group = await db.get(Group, member.group_id)
    historical = list((await db.scalars(select(GroupQualificationAudit).where(
        GroupQualificationAudit.membership_id == member.id,
        GroupQualificationAudit.state != "cancelled",
    ).order_by(GroupQualificationAudit.id.desc()).limit(50))).all())
    from app.modules.acquisition.qualification_continuity import choose_authorization
    latest = choose_authorization(historical)
    if latest is not None and group is not None and scope_matches(latest, group, member):
        snapshot = _payload(latest)
        work.progress_json = latest.evidence_json
        work.checkpoint_json, work.checkpoint_stage, work.checkpoint_revision = latest.checkpoint_json, latest.checkpoint_stage, latest.checkpoint_revision
        if (latest.decision in {"allowed", "trial"}
                and authorization_current(latest, now) and snapshot.get("profile_fingerprint") == profile
                and member.review_status == "approved" and member.ad_status == "active"
                and not await pending(db, member)):
            conclusion = latest
            if latest.state != "completed":
                # The legacy queue overwrote job state on valid conclusions.
                # Adopt their original proven facts into a final append only.
                snapshot = {**snapshot, "adopted_from_audit_id": latest.id, "adopted_at": now.isoformat()}
                conclusion = GroupQualificationAudit(
                    batch_id=f"efficiency-adopt:{scope}:{latest.id}",
                    membership_id=member.id, account_id=member.account_id, group_id=member.group_id,
                    membership_joined_at=member.joined_at, policy_version=latest.policy_version,
                    content_scope=latest.content_scope, state="completed", decision=latest.decision,
                    reason=latest.reason, checked_at=latest.checked_at, expires_at=latest.expires_at,
                    evidence_hash=latest.evidence_hash, evidence_json=_json(snapshot), attempts=latest.attempts,
                )
            if await on_decision(db, member, conclusion, now=now):
                work.state, work.ready_at = "DONE", None
                work.progress_json = conclusion.evidence_json
        elif latest.decision in {"reject", "protected"} and snapshot.get("ai_final"):
            work.state, work.ready_at = _wait(snapshot, latest.reason, now)
        elif latest.decision == "observe":
            work.state, work.ready_at = _wait(snapshot, latest.reason, now)
    sla_measure.stage_ready(db, work)
    return work


async def _bootstrap_targets(db: Any, account_id: int, *, config: dict | None = None,
                             events_only: bool = False, limit: int = 500) -> list[tuple[Any, dict | None]]:
    """Find missing work/new facts with four bounded queries, not per-member I/O."""
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_events import account_key, key, pending_value
    from app.modules.acquisition.qualification_service import policy
    from sqlalchemy.orm import lazyload
    if limit <= 0:
        return []
    account = (await db.execute(select(TelegramAccount.id, TelegramAccount.profile_bio).where(
        TelegramAccount.id == account_id,
    ))).one_or_none()
    if account is None:
        return []
    config = await policy(db, fresh=True) if config is None else config
    profile = profile_fingerprint(account)
    members = (await db.scalars(select(GroupAccountMembership).where(
        GroupAccountMembership.account_id == account_id, GroupAccountMembership.status == "joined",
        GroupAccountMembership.left_at.is_(None),
        or_(GroupAccountMembership.ad_status.is_(None), GroupAccountMembership.ad_status != "paused"),
        or_(GroupAccountMembership.review_status.is_(None), GroupAccountMembership.review_status.not_in(HELD_REVIEW_STATES)),
    ).options(lazyload(GroupAccountMembership.account)).order_by(GroupAccountMembership.id))).all()
    if not members:
        return []
    scopes = {member.id: _scope_key(member, config, profile) for member in members}
    works = {work.scope_key: work for work in (await db.scalars(select(QualificationWork).where(
        QualificationWork.account_id == account_id, QualificationWork.engine_version == ENGINE_VERSION,
        QualificationWork.family == FAMILY, QualificationWork.scope_key.in_(scopes.values()),
    ))).all()}
    keys = [account_key(account_id), *[key(member.id) for member in members]]
    cues = {cue.key: cue.value for cue in (await db.scalars(select(SystemSetting).where(SystemSetting.key.in_(keys)))).all()}
    targets = []
    for member in members:
        work = works.get(scopes[member.id])
        event = pending_value(cues.get(key(member.id)), member.joined_at, cues.get(account_key(account_id)))
        changed = event is not None and (work is None or event_version(event) != work.event_version)
        if changed or (not events_only and work is None):
            targets.append((member, event))
            if len(targets) >= limit:
                break
    return targets


async def bootstrap_account(db: Any, account_id: int, *, config: dict | None = None, limit: int = 500) -> int:
    targets = await _bootstrap_targets(db, account_id, config=config, limit=limit)
    for member, _ in targets:
        await bootstrap_member(db, member)
    return len(targets)


def _first_join_priority(dialect: str) -> Any:
    """Prioritize only a proved new join that has not started its first step.

    This is a queue hint, never budget or send authorization. A budget denial
    creates no read token/checkpoint and retains priority when its wait is due.
    A token with a certified zero-SDK release retains the unfinished first
    step; consumed/unknown slices and completed local checkpoints use FIFO.
    Invalid historic JSON is neutral and cannot break account dispatch.
    """
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB
        doc = cast(case(
            (func.pg_input_is_valid(QualificationWork.progress_json, "jsonb"), QualificationWork.progress_json),
            else_="{}",
        ), JSONB)

        def field(*path: str) -> tuple[Any, Any]:
            value = doc
            for part in path:
                value = value[part]
            return value.astext, func.jsonb_typeof(value)

        def positive_id(name: str) -> Any:
            value, kind = field("join_initial_budget", name)
            return case((and_(kind == "number", value.op("~")(r"^[1-9][0-9]{0,17}$")), cast(value, BigInteger)))

        true_type, string_type = "boolean", "string"
        joined_at = func.to_char(GroupAccountMembership.joined_at, 'YYYY-MM-DD"T"HH24:MI:SS.US')
    elif dialect == "sqlite":
        doc = case((func.json_valid(QualificationWork.progress_json) == 1, QualificationWork.progress_json), else_="{}")

        def field(*path: str) -> tuple[Any, Any]:
            key = "$." + ".".join(path)
            return func.json_extract(doc, key), func.json_type(doc, key)

        def positive_id(name: str) -> Any:
            value, kind = field("join_initial_budget", name)
            return case((and_(kind == "integer", value > 0, value <= 999999999999999999), value))

        true_type, string_type = "true", "text"
        joined_at = func.replace(cast(GroupAccountMembership.joined_at, String), " ", "T")
    else:
        return false()

    confirmed, confirmed_type = field("join_initial_budget", "confirmed")
    source_scope, scope_type = field("join_initial_budget", "scope_key")
    source_joined, joined_type = field("join_initial_budget", "joined_at")
    first_token, token_type = field("join_initial_budget", "first_read_token")
    zero_released, zero_type = field("join_initial_budget", "zero_sdk_released")
    normalized_joined = case((func.length(source_joined) == 19, cast(source_joined, String) + ".000000"), else_=source_joined)
    # json_extract uses numeric 1 for a true boolean; JSONB uses its text.
    confirmed_true = confirmed == ("true" if dialect == "postgresql" else 1)
    first_step_unfinished = or_(
        token_type.is_(None), token_type == "null",
        and_(token_type == string_type, first_token == ""),
        and_(token_type == string_type, first_token != "", zero_type == true_type,
             zero_released == ("true" if dialect == "postgresql" else 1)),
    )
    incomplete = []
    for path in (("collection_complete",), ("pending_collection", "collection_complete")):
        value, kind = field(*path)
        incomplete.append(or_(kind.is_(None), kind == "null", and_(
            kind == ("boolean" if dialect == "postgresql" else "false"),
            value == ("false" if dialect == "postgresql" else 0),
        )))
    attempt_id = positive_id("attempt_id")
    source_attempt = select(AutoJoinAttempt.id).where(
        AutoJoinAttempt.id == attempt_id,
        AutoJoinAttempt.account_id == QualificationWork.account_id,
        AutoJoinAttempt.group_id == QualificationWork.group_id,
        AutoJoinAttempt.status == "success",
        AutoJoinAttempt.reconciliation_status == "confirmed",
        AutoJoinAttempt.telegram_action_attempted.is_(True),
        AutoJoinAttempt.request_state == "sent",
    ).correlate(QualificationWork, GroupAccountMembership).exists()
    return and_(
        QualificationWork.initial_collection_pending.is_(True),
        confirmed_type == true_type, confirmed_true,
        positive_id("membership_id") == GroupAccountMembership.id,
        scope_type == string_type, source_scope == QualificationWork.scope_key,
        joined_type == string_type, GroupAccountMembership.joined_at.is_not(None),
        normalized_joined == joined_at,
        first_step_unfinished,
        or_(QualificationWork.checkpoint_stage.is_(None), QualificationWork.checkpoint_stage == ""),
        *incomplete, source_attempt,
    )


async def _normalize_provider_wait(db: Any, work: Any, event: dict | None, now: datetime) -> bool:
    """Repair a pre-upgrade cancellation locally before spending a claim/SDK slot."""
    snapshot = _object(work.progress_json)
    provider = _provider_wait_progress(snapshot, work.reason, event=event, now=now)
    active = snapshot.get("pending_collection") or snapshot
    if provider is None and (active.get("ai_pending") or active.get("ai_review_incomplete")):
        from app.modules.acquisition.review_read_plan import step_plan
        plan = step_plan(snapshot, None, now, event=event)
        if plan.required_reads or work.reason not in TECHNICAL_WAIT_REASONS:
            work.next_step_reads = plan.required_reads
            work.missing_dependencies_json = _json(list(plan.missing_dependencies))
    if (work.state != "READY" or not str(work.reason or "").startswith("review_exception:")
            or provider is None):
        return False
    state, ready_at = _wait(snapshot, work.reason, now, event=event)
    if state != "WAIT_PROVIDER":
        return False
    work.next_step_reads, work.initial_collection_pending = 0, False
    work.missing_dependencies_json = _json(["provider"])
    if provider.get("ai_material_key"):
        from app.core.settings_models import SystemSetting
        cached = await db.get(SystemSetting, "qualification.ai.once."+provider["ai_material_key"], populate_existing=True)
        if cached is not None and _object(cached.value).get("phase") == "completed":
            work.reason = "provider_completed"
            return False
    work.state, work.ready_at, work.updated_at = state, ready_at, now
    work.claim_token, work.claim_until = None, None
    return True


async def claim_next(db: Any, account_id: int, *, now: datetime | None = None) -> Claim | None:
    from app.modules.acquisition.qualification_events import pending
    from app.modules.acquisition.qualification_service import account_block_reason
    now = now or datetime.utcnow()
    account = await db.get(TelegramAccount, account_id, with_for_update={"of": TelegramAccount}, populate_existing=True)
    if account_block_reason(account, now):
        return None
    busy = await db.scalar(select(QualificationWork.id).where(
        QualificationWork.account_id == account_id, QualificationWork.state == "RUNNING",
        QualificationWork.claim_until > now,
    ).limit(1))
    if busy:
        return None
    query = select(QualificationWork.id).join(
        GroupAccountMembership, GroupAccountMembership.id == QualificationWork.membership_id,
    ).where(
        QualificationWork.account_id == account_id, QualificationWork.engine_version == ENGINE_VERSION,
        QualificationWork.family == FAMILY,
        GroupAccountMembership.account_id == account_id,
        GroupAccountMembership.group_id == QualificationWork.group_id,
        GroupAccountMembership.status == "joined", GroupAccountMembership.left_at.is_(None),
        or_(GroupAccountMembership.ad_status.is_(None), GroupAccountMembership.ad_status != "paused"),
        or_(GroupAccountMembership.review_status.is_(None), GroupAccountMembership.review_status.not_in(HELD_REVIEW_STATES)),
        QualificationWork.membership_joined_at.is_not_distinct_from(GroupAccountMembership.joined_at),
        or_(QualificationWork.state.in_(RUNNABLE_STATES),
            (QualificationWork.state == "RUNNING") & (QualificationWork.claim_until <= now)),
        or_(QualificationWork.ready_at.is_(None), QualificationWork.ready_at <= now),
    ).order_by(
        case((_first_join_priority(db.get_bind().dialect.name), 0), else_=1),
        QualificationWork.ready_at, QualificationWork.id,
    )
    scanned: set[int] = set()
    while True:
        ids = (await db.scalars(query.where(QualificationWork.id.not_in(scanned)).limit(32))).all()
        if not ids:
            return None
        for work_id in ids:
            scanned.add(work_id)
            candidate = await db.get(QualificationWork, work_id)
            if candidate is None:
                continue
            member = await _member_lock(db, candidate.membership_id)
            work = await db.get(QualificationWork, work_id, with_for_update=True, populate_existing=True)
            if work is None:
                continue
            if member is None or member.status != "joined" or member.left_at is not None or member.joined_at != work.membership_joined_at:
                work.state, work.reason, work.ready_at, work.initial_collection_pending = "STALE", "membership_changed", None, False
                work.claim_token, work.claim_until = None, None
                continue
            if member.ad_status == "paused" or member.review_status in HELD_REVIEW_STATES:
                work.state, work.reason, work.ready_at, work.initial_collection_pending = "WAIT_EVENT", "membership_held", None, False
                work.claim_token, work.claim_until = None, None
                continue
            scope, _, _ = await _scope(db, member)
            if scope != work.scope_key:
                work.state, work.reason, work.ready_at, work.initial_collection_pending = "STALE", "scope_changed", None, False
                work.claim_token, work.claim_until = None, None
                continue
            event = await pending(db, member)
            if event_version(event) != work.event_version:
                work = await enqueue(db, member, reason="event", event=event, now=now)
            if await _normalize_provider_wait(db, work, event, now):
                member.review_next_at = work.ready_at
                sla_measure.stage_ready(db, work)
                _stage_wake(db, account_id, ready_at=work.ready_at)
                _stage_wake(db, account_id, stages=("join",))
                continue
            sla_episode = sla_measure.reference(work)
            work.state, work.claim_token = "RUNNING", uuid4().hex
            work.claim_until, work.updated_at = now + timedelta(seconds=360), now
            work.attempts += 1
            ready_origin.capture_claim(work, sla_episode, datetime.utcnow(), health=not bool(account_block_reason(account, now)))
            sla_episode = sla_measure.reference(work)
            await db.flush()
            return Claim(work.id, member.id, work.claim_token, work.revision, work.scope_key, work.event_version, event, sla_episode)


async def _draft(db: Any, claim: Claim) -> GroupQualificationAudit:
    from app.modules.acquisition.qualification_service import _date
    work = await db.get(QualificationWork, claim.work_id)
    grant = await db.get(QualificationActiveGrant, work.membership_id)
    prior = await db.get(GroupQualificationAudit, grant.audit_id) if grant and grant.audit_id else None
    progress = _object(work.progress_json)
    if progress.get("engine_technical_retry_revision") != claim.revision:
        progress = _reset_technical_retry(progress, claim.revision)
    pending = progress.get("pending_collection")
    if isinstance(pending, dict):
        pending = dict(pending)
        for key in ("scope_collection_refresh", "collection_needs_refresh", "invalidated_at"):
            if progress.get(key):
                pending[key] = progress[key]
        if pending.get("invalidated_at"):
            pending["collection_needs_refresh"] = True
        progress["pending_collection"] = pending
    in_progress = progress.get("pending_collection") or progress
    decision = prior.decision if prior else progress.get("decision", "unknown")
    if in_progress.get("ai_pending") or in_progress.get("ai_review_incomplete"):
        decision = "technical_wait"
    draft = GroupQualificationAudit(
        batch_id=f"efficiency:{work.id}:{claim.revision}:{claim.token}",
        membership_id=work.membership_id, account_id=work.account_id, group_id=work.group_id,
        membership_joined_at=work.membership_joined_at, policy_version=work.policy_version,
        content_scope="text_profile", state="running", decision=decision,
        reason=prior.reason if prior else progress.get("reason") or work.reason, attempts=work.attempts,
        evidence_json=_json(progress), checked_at=prior.checked_at if prior else _date(progress.get("checked_at")),
        expires_at=prior.expires_at if prior else _date(progress.get("expires_at")), next_retry_at=work.ready_at,
        checkpoint_json=work.checkpoint_json, checkpoint_stage=work.checkpoint_stage,
        checkpoint_revision=work.checkpoint_revision, created_at=work.created_at,
    )
    draft._efficiency_claim = claim
    draft._efficiency_cycle = f"efficiency:{work.id}:{claim.revision}"
    return draft


async def queue_pending_events(db: Any, account_id: int, *, limit: int = 2) -> int:
    targets = await _bootstrap_targets(db, account_id, events_only=True, limit=limit)
    for member, event in targets:
        await enqueue(db, member, reason="event", event=event)
    return len(targets)


def _candidate_evidence(snapshot: dict) -> list[dict]:
    pending = snapshot.get("pending_collection")
    return [item for source in (snapshot, pending if isinstance(pending, dict) else {})
            for item in source.get("evidence", []) if isinstance(item, dict)]


def _candidate_wait(previous: dict | None, before: list[dict], current: list[dict],
                    changed_ids: set[int], now: datetime) -> tuple[str, datetime | None]:
    """A new hint changes candidate obligations, never another execution wait."""
    from app.modules.acquisition.evidence_progress import date, evidence_maturity
    previous = previous or {}
    state, ready_at = previous.get("state"), previous.get("ready_at")
    if state in {"WAIT_BUDGET", "WAIT_PROVIDER", "READY"}:
        return state, ready_at
    if state == "RUNNING" or previous.get("initial_collection_pending"):
        # The new event invalidates the old claim, but cannot suppress its
        # runnable continuation or pretend that first collection completed.
        return "READY", now
    snapshot = previous.get("progress") or {}
    evidence = _candidate_evidence(snapshot)
    due = [date(item["date"]) + timedelta(hours=24) for item in current]
    surviving = [item for item in evidence if item.get("message_id") not in changed_ids]
    maturity = evidence_maturity({**snapshot, "evidence": surviving}, now)
    if maturity is not None:
        due.append(now if maturity <= now + timedelta(seconds=1) else maturity)
    known_ids = {item["id"] for item in before} | {item.get("message_id") for item in evidence}
    if (state == "WAIT_MATURITY" and ready_at is not None
            and not changed_ids.intersection(known_ids)
            and ready_at >= now - timedelta(hours=48)):
        # An unrelated candidate cannot retire an existing history-derived
        # maturity obligation, including ones not present in the hint cache.
        due.append(ready_at)
    if not due or min(due) <= now:
        return "READY", now
    return "WAIT_MATURITY", min(due)


async def project_fact(db: Any, member: Any, fact: dict, *, now: datetime | None = None) -> None:
    """Listener adapter: hints wake missing evidence; completed Audits stay fixed."""
    from app.modules.acquisition.qualification_events import request
    from app.modules.acquisition.qualification_service import _date
    now = now or datetime.utcnow()
    kind = fact["kind"]
    if kind in {"evidence_candidate", "evidence_candidate_removed"}:
        from app.modules.acquisition.callback_evidence import hints, remove_hints, save_hint
        # Serialize hints with enqueue and completion. request() itself calls
        # enqueue, so capture the existing wait before it resets the revision.
        member = await _member_lock(db, member.id)
        if member is None or member.joined_at is None or fact.get("joined_at") != member.joined_at.isoformat():
            return
        scope, _, _ = await _scope(db, member)
        old = await db.scalar(select(QualificationWork).where(
            QualificationWork.scope_key == scope,
        ).with_for_update(of=QualificationWork).execution_options(populate_existing=True))
        previous = ({"state": old.state, "ready_at": old.ready_at, "reason": old.reason,
                     "initial_collection_pending": old.initial_collection_pending,
                     "progress": _object(old.progress_json)} if old else None)
        before = await hints(db, member, now)
        removed = kind == "evidence_candidate_removed"
        ids = ({value for value in fact.get("message_ids", ()) if type(value) is int and value > 0}
               if removed else {fact["message_id"]})
        changed = (await remove_hints(db, member, ids, now) if removed
                   else await save_hint(db, member, fact, now))
        if removed and not changed:
            grant = await db.get(QualificationActiveGrant, member.id)
            audit = await db.get(GroupQualificationAudit, grant.audit_id) if grant and grant.audit_id else None
            referenced = _candidate_evidence(previous["progress"] if previous else {})
            referenced += _candidate_evidence(_object(audit.evidence_json)) if audit else []
            changed = bool(ids.intersection(item.get("message_id") for item in referenced))
        if not changed or (not removed and await active_authorization(db, member, now=now)):
            return
        await request(db, member, "rules" if removed else kind, message_ids=sorted(ids),
                      source_token=hashlib.sha256(_json(fact).encode()).hexdigest())
        work = await enqueue(db, member, reason="new_candidate", now=now)
        work.state, work.ready_at = _candidate_wait(previous, before, await hints(db, member, now), ids, now)
        if removed and work.state == "WAIT_MATURITY":
            # A changed dependency needs a directed live check before its
            # replacement age can support a conclusion.
            work.state, work.ready_at = "READY", now
        if previous and work.state in {"WAIT_BUDGET", "WAIT_PROVIDER", "READY"}:
            work.reason = previous["reason"] if previous["state"] == work.state else work.reason
        member.review_next_at = work.ready_at
        sla_measure.stage_ready(db, work)
        if work.state in RUNNABLE_STATES:
            _stage_wake(db, member.account_id, ready_at=work.ready_at)
        return
    if kind == "verification" and (
        datetime.utcfromtimestamp(fact["at"]) < now - timedelta(minutes=15)
        or member.review_status == "approved" or member.joined_at < now - timedelta(hours=48)
    ):
        return
    if kind == "rules":
        grant = await db.get(QualificationActiveGrant, member.id)
        audit = await db.get(GroupQualificationAudit, grant.audit_id) if grant and grant.audit_id else None
        collected = _date(_object(audit.evidence_json).get("collected_at")) if audit else None
        if collected is not None and datetime.utcfromtimestamp(fact["at"]) <= collected:
            return
    await request(db, member, kind, message_ids=fact.get("message_ids", ()),
                  source_token=hashlib.sha256(_json(fact).encode()).hexdigest(),
                  evidence_refs=fact.get("evidence_refs"))


async def try_light_renewal(service: Any, account: Any, group: Any, member: Any,
                            draft: Any, previous: dict, *, force_refresh: bool) -> Any:
    """Use the existing narrow proof checker against an unpersisted draft."""
    from app.core.account.rpc_governor import RpcDeferred
    from app.modules.acquisition.automation import JoinedGroupAuditResult
    from app.modules.acquisition.qualification_actions import _detached_refresh_row, _read_live_authorization
    from app.modules.acquisition.qualification_events import pending
    from app.modules.acquisition.qualification_renewal import eligible, REFRESH_PURPOSE
    from app.modules.acquisition.renewal_progress import resume
    from app.modules.acquisition.review_read_plan import apply, for_review
    from app.core.account.read_work import current
    if force_refresh or not await eligible(service.db, account, group, member, draft, previous):
        return None
    event = await pending(service.db, member)
    if not event:
        return None
    work = current(account.id)
    plan = await for_review(service.db, previous, draft, member, group, account, event, datetime.utcnow(), renewal=True)
    if work is not None:
        work.kind = "renewal"
        apply(work, plan, account.id, renewal=True)
    progress = resume(previous, event, member, datetime.utcnow())
    detached = (draft, _detached_refresh_row(group), _detached_refresh_row(member))
    wrapper = None
    from app.modules.acquisition.review_fact_owner import create_context, publish_context
    context = None
    failure = None
    effects: dict[str, Any] = {}
    await service.db.commit()
    try:
        await service.account_pool.add_account_from_db(account)
        wrapper = await service.account_pool.acquire_by_id(account.id, purpose=REFRESH_PURPOSE, raise_on_lease_failure=True)
        if wrapper is None or wrapper.client is None:
            raise RpcDeferred("telegram_rpc_guard_unavailable", 60)
        context = create_context(account.id, wrapper)
        await service.db.commit()  # account loading must not lend a DB connection to Telegram
        await _read_live_authorization(
            service.db, wrapper.client, account.id, group.group_id,
            permissions_only=set(event.get("kinds") or ()) <= {"membership"},
            message_ids=event.get("message_ids"),
            recheck_rules=bool(set(event.get("kinds") or ()) & {"rules", "gap", "warning"}),
            progress=progress, shared_read_context=context, authorization=detached, effects=effects,
        )
    except Exception as exc:
        failure = exc
    finally:
        if context is not None:
            await publish_context(context, _object(draft.evidence_json))
        if wrapper is not None:
            await service.account_pool.release(wrapper)
    snapshot = _object(draft.evidence_json)
    snapshot["collection_event"] = event
    snapshot["renewal_progress"] = progress
    if failure is not None:
        if snapshot.get("invalidated_at"):
            # Changed material requires the normal bounded missing-facts path.
            # This draft's changed verdict is not a final business conclusion.
            draft.decision, draft.reason = "unknown", None
            return None
        reason = getattr(failure, "reason", str(failure)) or type(failure).__name__
        snapshot.update({"technical_errors": [type(failure).__name__], "reason": reason,
                         "retry_after_seconds": max(60, int(getattr(failure, "retry_after_seconds", 0) or 60))})
        if effects.get("live_mute"):
            snapshot["live_mute"] = effects["live_mute"]
        draft.decision, draft.reason, draft.state = "technical_wait", reason, "completed"
        draft.evidence_json = _json(snapshot)
        return JoinedGroupAuditResult(passed=False, reason=reason)
    snapshot.pop("renewal_progress", None)
    snapshot.pop("technical_errors", None)
    snapshot["event_reconciled_at"] = datetime.utcnow().isoformat()
    snapshot["authorization_mode"] = "events"
    draft.state, draft.next_retry_at = "completed", None
    draft.checked_at = datetime.utcnow()
    draft.evidence_json = _json(snapshot)
    return JoinedGroupAuditResult(passed=True, can_send_messages=True, ad_allowed=True,
        ad_rule_details={**snapshot.get("advertising_audit", {}), "qualification": snapshot})


async def run_reviews(service: Any, *, account_id: int, limit: int = 1) -> dict:
    """Bounded work runner; no technical Audit rows and no periodic full recheck."""
    from app.core.account.session_exec import current_actor_account
    from app.modules.acquisition.qualification_service import assess, policy, qualifies_for_automatic_exit
    result = {"processed": 0, "results": [], "engine_version": ENGINE_VERSION}
    if enabled_for(account_id) and current_actor_account() != account_id:
        # A Celery task closes its asyncio loop on return. Only the persistent
        # owner may start background recognition; no claim/attempt is spent.
        return {**result, "reason": "session_owner_required", "retryable": True}
    config = await policy(service.db, fresh=True)
    if not enabled_for(account_id) or not config.get("enabled") or config.get("execute_reviews", True) is False:
        return result
    if not policy_scope_allows(config, account_id):
        return result
    await bootstrap_account(service.db, account_id, config=config)
    await service.db.commit()
    for _ in range(max(0, limit)):
        claim = await claim_next(service.db, account_id)
        if claim is None:
            await service.db.commit()
            break
        draft = await _draft(service.db, claim)
        member = await service.db.get(GroupAccountMembership, claim.membership_id)
        group = await service.db.get(Group, draft.group_id)
        await service.db.commit()  # release claim/membership/account locks before I/O
        sla_binding = sla_measure.bind_claim(claim, account_id)
        try:
            audit_result = await assess(service, account_id, group, row=draft)
            evidence = _object(draft.evidence_json)
            decision = draft.decision
            reason = draft.reason or audit_result.reason
            if decision in {"allowed", "trial", "observe", "reject", "protected"} and not evidence.get("ai_pending") and not evidence.get("ai_review_incomplete"):
                # Technical invalidations and incomplete collection remain work.
                if (decision in {"allowed", "trial"} and audit_result.passed) or decision in {"reject", "protected"} or (decision == "observe" and evidence.get("next_evidence_maturity_at")):
                    published = await on_decision(service.db, member, draft, claim=claim)
                    if published:
                        work = await service.db.get(QualificationWork, claim.work_id)
                        if decision in {"allowed", "trial"}:
                            await service._sync_group_ad_policy_from_audit(group, audit_result)
                            group.status = "active"
                        elif decision == "observe":
                            work.state, work.ready_at = _wait(evidence, reason, datetime.utcnow())
                            if not await active_authorization(service.db, member):
                                member.review_status, member.ad_status = "review_2h", "warming"
                            member.review_next_at = work.ready_at
                            sla_measure.stage_ready(service.db, work)
                        elif decision == "protected":
                            member.review_status, member.ad_status = "owned_group_excluded", "blocked"
                        elif decision == "reject":
                            member.review_status, member.ad_status = ("exit_pending" if qualifies_for_automatic_exit(evidence) else "review_2h"), "blocked"
                            work.state, work.ready_at = _wait(evidence, reason, datetime.utcnow())
                            member.review_next_at = work.ready_at
                            sla_measure.stage_ready(service.db, work)
                    else:
                        await service.db.rollback()
                        result["results"].append({"work_id": claim.work_id, "reason": "stale_claim"})
                        continue
                else:
                    await defer(service.db, claim, snapshot=evidence, reason=reason, draft=draft)
            else:
                await defer(service.db, claim, snapshot=evidence, reason=reason, draft=draft)
            await service.db.commit()
            result["processed"] += 1
            result["results"].append({"work_id": claim.work_id, "decision": decision, "reason": reason})
        except BaseException as exc:
            import asyncio
            await service.db.rollback()
            snapshot = _object(draft.evidence_json)
            snapshot.setdefault("technical_errors", []).append(type(exc).__name__)
            await defer(service.db, claim, snapshot=snapshot, reason="review_exception:" + type(exc).__name__, draft=draft)
            await service.db.commit()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            result["results"].append({"work_id": claim.work_id, "reason": "review_exception:" + type(exc).__name__})
        finally:
            sla_measure.reset_binding(sla_binding)
    return result


from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _SyncSession

_sa_event.listen(_SyncSession, "after_commit", _after_commit)
_sa_event.listen(_SyncSession, "after_rollback", _after_rollback)


def _runtime_next_step(work: Any, member: Any, event: dict | None,
                       now: datetime) -> tuple[Any, Any]:
    """Project the canonical local demand without repairing or waking old work."""
    reads, missing = work.next_step_reads, _object(work.missing_dependencies_json, [])
    progress = _object(work.progress_json)
    active = progress.get("pending_collection") or progress
    if (work.state in RUNNABLE_STATES
            and (active.get("ai_pending") or active.get("ai_review_incomplete"))):
        from app.modules.acquisition.review_read_plan import step_plan
        plan = step_plan(progress, member, now, event=event)
        if work.state != "WAIT_BUDGET" or plan.required_reads:
            return plan.required_reads, list(plan.missing_dependencies)
    if work.state != "WAIT_BUDGET" or work.reason not in {
        "telegram_read_budget", "telegram_read_slice", "read_step_not_ready",
    }:
        return reads, missing
    progress = _object(work.progress_json)
    saved = progress.get("read_plan")
    if isinstance(saved, dict):
        return saved.get("required_reads", reads), saved.get("missing_dependencies", missing)
    if "read_plan" not in progress:
        from app.modules.acquisition.review_read_plan import step_plan
        previous = progress.get("pending_collection") or progress
        plan = step_plan(previous, member, now, event=event)
        return plan.required_reads, list(plan.missing_dependencies)
    return reads, missing


async def runtime_for_memberships(db: Any, rows: Any) -> dict[int, dict]:
    """Read-only operational view; never creates or wakes work."""
    ids = [int(getattr(row, "membership_id", getattr(row, "id", row))) for row in rows]
    if not ids:
        return {}
    grants = {item.membership_id: item for item in (await db.scalars(select(QualificationActiveGrant).where(QualificationActiveGrant.membership_id.in_(ids)))).all()}
    members = {item.id: item for item in (await db.scalars(select(GroupAccountMembership).where(GroupAccountMembership.id.in_(ids)))).all()}
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_events import account_key, key, pending_value
    from app.modules.acquisition.qualification_lifetime import authorization_current
    from app.modules.acquisition.qualification_service import policy, scope_matches
    from app.modules.acquisition.qualification_system_identity import identity_fingerprint, parse_identity_map
    from sqlalchemy.orm import lazyload
    config = await policy(db, fresh=True)
    account_ids = {member.account_id for member in members.values()}
    accounts = {item.id: item for item in (await db.scalars(select(TelegramAccount).where(TelegramAccount.id.in_(account_ids)))).all()}
    group_ids = {member.group_id for member in members.values()}
    groups = {item.id: item for item in (await db.scalars(select(Group).where(Group.id.in_(group_ids)).options(
        lazyload(Group.account_memberships),
    ))).all()}
    scopes = {member.id: _scope_key(member, config, profile_fingerprint(accounts.get(member.account_id))) for member in members.values()}
    from app.core.account.models import AccountType
    managed = set((await db.scalars(select(TelegramAccount.id).where(TelegramAccount.account_type == AccountType.PROMOTER))).all())
    mapping, extras = parse_identity_map(config.get("system_account_user_ids")), config.get("system_user_ids", [])
    coverage = (identity_fingerprint(managed, mapping, extras) if mapping and managed and managed <= mapping.keys()
        and isinstance(extras, list) and all(type(value) is int and value > 0 for value in extras) else None)
    audit_ids = {grant.audit_id for grant in grants.values() if grant.audit_id}
    audits = {item.id: item for item in (await db.scalars(select(GroupQualificationAudit).where(GroupQualificationAudit.id.in_(audit_ids)))).all()}
    setting_keys = [*[key(member_id) for member_id in ids], *[account_key(account_id) for account_id in account_ids]]
    cues = {item.key: item.value for item in (await db.scalars(select(SystemSetting).where(SystemSetting.key.in_(setting_keys)))).all()}
    works = (await db.scalars(select(QualificationWork).where(
        QualificationWork.scope_key.in_(scopes.values()), QualificationWork.family == FAMILY,
        QualificationWork.engine_version == ENGINE_VERSION,
    ))).all()
    current = {item.membership_id: item for item in works}
    result = {}
    for membership_id in ids:
        grant, work = grants.get(membership_id), current.get(membership_id)
        member = members.get(membership_id)
        scope = scopes.get(membership_id)
        group = groups.get(member.group_id) if member is not None else None
        authorization = audits.get(grant.audit_id) if grant else None
        identity_snapshot = _object(authorization.evidence_json) if authorization else {}
        change = pending_value(cues.get(key(membership_id)), member.joined_at, cues.get(account_key(member.account_id))) if member else None
        if (member is None or grant is None or grant.invalidated_at or grant.scope_key != scope
                or member.status != "joined" or member.left_at is not None or member.ad_status == "paused"
                or member.review_status in HELD_REVIEW_STATES or authorization is None
                or authorization.account_id != member.account_id or authorization.group_id != member.group_id
                or authorization.membership_id != member.id or authorization.membership_joined_at != member.joined_at
                or authorization.policy_version != grant.policy_version or authorization.content_scope != "text_profile"
                or group is None or not scope_matches(authorization, group, member)
                or not coverage or member.account_id not in managed
                or identity_snapshot.get("system_identity_coverage") is not True
                or identity_snapshot.get("system_identity_fingerprint") != coverage
                or not authorization_current(authorization, datetime.utcnow())
                or event_blocks_authorization(_object(authorization.evidence_json), change)):
            authorization = None
        invalidated_reason = grant.invalidated_reason if grant else None
        if grant and authorization is None and not invalidated_reason:
            invalidated_reason = "qualification_scope_or_event_changed"
        work_state = work.state if work and work.scope_key == scope else "STALE"
        required_reads, missing_dependencies = (work.next_step_reads, _object(work.missing_dependencies_json, [])) if work else (None, [])
        if work is not None and work.scope_key == scope and member is not None:
            required_reads, missing_dependencies = _runtime_next_step(work, member, change, datetime.utcnow())
        result[membership_id] = {
            "authorization": {"state": "active" if authorization is not None else "invalidated" if grant else "missing",
                "grant_audit_id": grant.audit_id if grant else None, "version": grant.version if grant else 0,
                "scope_key": grant.scope_key if grant else None,
                "policy_version": grant.policy_version if grant else None,
                "profile_fingerprint": grant.profile_fingerprint if grant else None,
                "invalidated_reason": invalidated_reason},
            "job": ({"id": work.id, "state": work_state, "kind": work.kind, "ready_at": work.ready_at,
                "next_step_reads": required_reads, "missing_dependencies": missing_dependencies} if work else None),
            "blocker": ({"code": work.reason, "required_reads": required_reads, "available_reads": None,
                "retry_at": work.ready_at, "observed_at": work.updated_at} if work and work_state.startswith("WAIT_") else None),
        }
    return result
