"""Durable observations of existing due ad obligations, never execution authority.

SystemSetting holds measurement rows only. Existing schedule, budget, receipts,
authorization and outbox remain authoritative. Retrying a due obligation cannot
create another denominator entry, and only a linked successful message closes it.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import lazyload

from app.core.settings_models import SystemSetting

PREFIX = "ad.opportunity.v1."
COVERAGE_PREFIX = "ad.opportunity.coverage.v1."
MAX_SCAN_GAP_SECONDS = 120
RETENTION_DAYS = 7


def _data(value: str | None) -> dict:
    try:
        item = json.loads(value or "{}")
        return item if isinstance(item, dict) else {}
    except (ValueError, TypeError):
        return {}


def _date(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def _save(row: SystemSetting, value: dict) -> None:
    row.value = json.dumps(value, sort_keys=True, separators=(",", ":"))


def _account_prefix(account_id: int) -> str:
    return PREFIX + str(account_id) + "."


async def _rows(db: Any, account_id: int, *, lock: bool = False, pending_only: bool = False,
                now: datetime | None = None) -> list:
    pending = SystemSetting.value.contains('"state":"pending"')
    query = select(SystemSetting).where(SystemSetting.key.startswith(_account_prefix(account_id)),
        pending if pending_only else or_(pending, SystemSetting.updated_at >= (now or datetime.utcnow())-timedelta(days=RETENTION_DAYS)))
    if lock:
        query = query.with_for_update()
    return list((await db.scalars(query)).all())


def _gap(coverage: dict, start: datetime, end: datetime, reason: str) -> None:
    gaps = [item for item in coverage.get("gaps", []) if (_date(item.get("end")) or datetime.max) >= end-timedelta(days=2)]
    gaps.append({"start": start.isoformat(), "end": end.isoformat(), "reason": reason})
    # Overflow is conservatively unknown for another full observation window.
    if len(gaps) > 1024:
        coverage["unknown_until"] = (end+timedelta(days=1)).isoformat()
    coverage["gaps"] = gaps[-1024:]


async def collect_due(db: Any, now: datetime, *, account_id: int | None = None) -> dict:
    """Observe due grants before scheduler, budget, owner and software gates.

    One local batch validates grants; no Telegram reads or quota reservations.
    A previous pending obligation survives all schedule retry/backoff changes.
    """
    from app.core.account.models import TelegramAccount
    from app.core.group.models import Group, GroupAccountMembership as Member
    from app.modules.acquisition import qualification_engine as engine
    from app.modules.acquisition.models import AccountAdBinding, AdCampaign, AdDeliveryScheduleState

    accounts = engine.enabled_account_ids()
    if account_id is not None:
        accounts &= {account_id}
    result = {"accounts": len(accounts), "new": 0}
    if not accounts:
        return result
    for identity in sorted(accounts):
        # This measurement-only lock never contends with the caller's business
        # account row. It also serializes absent-marker creation after restart.
        if db.bind.dialect.name == "postgresql":
            await db.execute(select(func.pg_advisory_xact_lock(572909, identity)))
        if await db.scalar(select(TelegramAccount.id).where(TelegramAccount.id == identity)) is None:
            continue
        marker = await db.get(SystemSetting, COVERAGE_PREFIX+str(identity), with_for_update=True)
        coverage = _data(marker.value) if marker else {"started_at": now.isoformat(), "gaps": []}
        previous = _date(coverage.get("last_scan_at"))
        if previous and (now-previous).total_seconds() > MAX_SCAN_GAP_SECONDS:
            _gap(coverage, previous, now, "dispatcher_scan_gap")
        retired = select(SystemSetting.key).where(
            SystemSetting.key.startswith(_account_prefix(identity)),
            SystemSetting.value.contains('"state":"completed"'),
            SystemSetting.updated_at < now-timedelta(days=RETENTION_DAYS),
        ).order_by(SystemSetting.updated_at).limit(1000)
        await db.execute(delete(SystemSetting).where(SystemSetting.key.in_(retired)))
        rows = await _rows(db, identity, lock=True, now=now)
        obligations = {row.key: (row, _data(row.value)) for row in rows}
        pending_by_tuple, completed_by_tuple, pending_by_member = {}, {}, defaultdict(list)
        for row, value in obligations.values():
            tuple_key = (value.get("membership_id"), value.get("campaign_id"), value.get("incarnation"))
            if value.get("state") == "pending":
                pending_by_tuple[tuple_key] = (row, value)
                pending_by_member[tuple_key[0]].append((row, value))
            elif value.get("state") == "completed":
                completed_by_tuple[tuple_key] = max(completed_by_tuple.get(tuple_key, datetime.min),
                    _date(value.get("completed_at")) or datetime.min)
        members_and_groups = (await db.execute(select(Member, Group).join(Group, Group.id == Member.group_id).where(
            Member.account_id == identity, Member.status == "joined", Member.left_at.is_(None),
        ).options(lazyload("*")))).all()
        runtime = await engine.runtime_for_memberships(db, [member for member, _ in members_and_groups])
        bindings = (await db.execute(select(AccountAdBinding, AdCampaign).join(
            AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id,
        ).where(AccountAdBinding.account_id == identity, AccountAdBinding.enabled.is_(True),
                AdCampaign.enabled.is_(True), AdCampaign.delivery_policy == "growth").options(lazyload("*")))).all()
        states = {(state.campaign_id, state.group_id): state for state in (await db.scalars(
            select(AdDeliveryScheduleState).where(AdDeliveryScheduleState.account_id == identity).options(lazyload("*"))
        )).all()}
        for member, group in members_and_groups:
            if runtime.get(member.id, {}).get("authorization", {}).get("state") != "active":
                for row, value in pending_by_member.get(member.id, []):
                    value.update(last_reason="authorization_invalidated_after_due", last_observed_at=now.isoformat())
                    _save(row, value)
                continue
            for _, campaign in bindings:
                if (campaign.start_at and campaign.start_at > now) or (campaign.end_at and campaign.end_at < now):
                    continue
                targets = campaign.get_target_group_ids()
                if (targets and member.group_id not in targets) or group.ad_delivery_account_id is not None:
                    continue
                incarnation = member.joined_at.isoformat() if member.joined_at else None
                tuple_key = (member.id, campaign.id, incarnation)
                existing = pending_by_tuple.get(tuple_key)
                state = states.get((campaign.id, member.group_id))
                if existing:
                    row, value = existing
                    if state and state.last_reason:
                        value.update(last_reason=state.last_reason, last_observed_at=now.isoformat())
                        _save(row, value)
                    continue
                # The schedule owns its due time. Later retries cannot change an
                # already captured obligation's due_at or stable identifier.
                due = state.next_due_at if state else max(
                    member.joined_at or now,
                    (member.joined_at or now)+timedelta(minutes=campaign.min_wait_after_join_minutes or 0),
                    member.first_ad_allowed_at or datetime.min,
                )
                if due > now:
                    continue
                # A successful schedule may be observed briefly before its new
                # deadline commits. Do not manufacture a second obligation.
                if state and state.last_success_at and due <= state.last_success_at:
                    _gap(coverage, previous or now, now, "schedule_success_deadline_unsettled")
                    continue
                anchor = completed_by_tuple.get(tuple_key)
                if anchor and (not state or due <= anchor):
                    _gap(coverage, previous or now, now, "schedule_receipt_deadline_unsettled")
                    continue
                seed = [identity, campaign.id, member.id, incarnation, state.last_success_at.isoformat() if state and state.last_success_at else None]
                key = _account_prefix(identity)+hashlib.sha256(json.dumps(seed).encode()).hexdigest()
                if key in obligations:
                    continue
                value = {"id": key, "account_id": identity, "campaign_id": campaign.id,
                    "membership_id": member.id, "group_id": member.group_id, "incarnation": incarnation,
                    "telegram_group_id": member.telegram_group_id,
                    "due_authorization": dict(runtime[member.id]["authorization"]),
                    "due_at": due.isoformat(), "observed_at": now.isoformat(), "last_observed_at": now.isoformat(),
                    "state": "pending", "last_reason": state.last_reason if state and state.last_reason else "software_dispatch_wait"}
                row = SystemSetting(key=key, value="{}")
                _save(row, value)
                db.add(row)
                obligations[key] = (row, value)
                pending_by_tuple[tuple_key] = (row, value)
                result["new"] += 1
        coverage.update(last_scan_at=now.isoformat(), scan_interval_limit_seconds=MAX_SCAN_GAP_SECONDS)
        if marker is None:
            marker = SystemSetting(key=COVERAGE_PREFIX+str(identity), value="{}")
            db.add(marker)
        _save(marker, coverage)
    await db.flush()
    return result


async def observe_log(db: Any, log: Any) -> None:
    """Bind an existing business log, then close only on its successful receipt."""
    from app.modules.acquisition.review_read_flags import growth_enabled_for
    if not growth_enabled_for(log.account_id):
        return
    context = _data(log.qualification_context_json)
    incarnation = context.get("membership_joined_at")
    from app.core.group.identity import telegram_group_id_aliases
    for row in await _rows(db, log.account_id, lock=True, pending_only=True):
        value = _data(row.value)
        if value.get("campaign_id") != log.ad_campaign_id or value.get("group_id") != log.group_id:
            continue
        if value.get("state") != "pending" or value.get("incarnation") != incarnation:
            continue
        aliases = telegram_group_id_aliases(value.get("telegram_group_id"))
        if (log.telegram_group_id not in aliases or context.get("telegram_group_id") not in aliases
                or context.get("account_id") != log.account_id or context.get("membership_id") != value.get("membership_id")):
            continue
        linked = context.get("ad_opportunity_id")
        if linked and linked != row.key:
            continue
        context["ad_opportunity_id"] = row.key
        log.qualification_context_json = json.dumps(context, sort_keys=True)
        if log.status == "success" and type(log.telegram_message_id) is int and log.telegram_message_id > 0 and log.sent_at:
            if log.sent_at < (_date(value.get("due_at")) or datetime.max):
                continue
            if type(context.get("audit_id")) is not int or not context.get("policy_version"):
                value["last_reason"] = "receipt_authorization_scope_unrecorded"
                _save(row, value)
                break
            value.update(state="completed", completed_at=log.sent_at.isoformat(),
                receipt_log_id=log.id, message_id=log.telegram_message_id, last_reason=None,
                receipt_authorization={key: context.get(key) for key in ("audit_id", "policy_version", "profile_fingerprint", "telegram_group_id", "membership_id")},
                authorization_changed_since_due=context.get("audit_id") != value.get("due_authorization", {}).get("grant_audit_id"))
        elif log.status in {"unknown", "sending", "reconciliation_required"}:
            value["last_reason"] = "external_outcome_unknown"
        elif log.error:
            value["last_reason"] = str(log.error)[:255]
        _save(row, value)
        break


async def note_result(db: Any, account_id: int, result: dict, now: datetime) -> None:
    """Record actual software/budget waits without excluding the obligation."""
    from app.modules.acquisition.review_read_flags import growth_enabled_for
    if not growth_enabled_for(account_id):
        return
    details = [item for item in result.get("details", []) if isinstance(item, dict)]
    for row in await _rows(db, account_id, lock=True, pending_only=True):
        value = _data(row.value)
        if value.get("state") != "pending":
            continue
        reason = next((item.get("reason") for item in details if item.get("reason")
            and item.get("group_id") in {None, value.get("group_id")}
            and item.get("campaign_id") in {None, value.get("campaign_id")}), result.get("reason"))
        if reason:
            value.update(last_reason=str(reason)[:255], last_observed_at=now.isoformat())
            _save(row, value)
    await db.flush()


async def capture_due(db: Any, now: datetime, *, account_id: int | None = None) -> dict:
    """A measurement failure must preserve growth and make its window unknown."""
    from app.modules.acquisition.qualification_engine import enabled_account_ids
    accounts = enabled_account_ids()
    if account_id is not None:
        accounts &= {account_id}
    if not accounts:
        return {"accounts": 0, "new": 0}
    from app.core.database import get_db_session
    # Never commit or roll back the caller's pending business modifications.
    # The dedicated local session commits before any later admission gate.
    try:
        async with get_db_session() as measurement_db:
            try:
                async with measurement_db.begin_nested():
                    return await collect_due(measurement_db, now, account_id=account_id)
            except Exception:
                for identity in accounts:
                    marker = await measurement_db.get(SystemSetting, COVERAGE_PREFIX+str(identity))
                    value = _data(marker.value) if marker else {"started_at": now.isoformat()}
                    _gap(value, _date(value.get("last_scan_at")) or now-timedelta(seconds=1), now, "capture_failed")
                    if marker is None:
                        marker = SystemSetting(key=COVERAGE_PREFIX+str(identity), value="{}")
                        measurement_db.add(marker)
                    _save(marker, value)
    except Exception:
        # A database outage cannot record its own gap. The next scan detects
        # the missing heartbeat, and reads reject stale coverage immediately.
        pass
    return {"accounts": len(accounts), "new": 0, "incomplete": True}


async def observe_log_safely(db: Any, log: Any) -> None:
    """Keep the original receipt transaction even if measuring it fails."""
    from app.modules.acquisition.review_read_flags import growth_enabled_for
    if not growth_enabled_for(log.account_id):
        return
    try:
        async with db.begin_nested():
            await db.flush()
            await observe_log(db, log)
    except Exception:
        now = datetime.utcnow()
        marker = await db.get(SystemSetting, COVERAGE_PREFIX+str(log.account_id))
        if marker:
            value = _data(marker.value)
            _gap(value, _date(value.get("last_scan_at")) or now-timedelta(seconds=1), now, "receipt_link_failed")
            _save(marker, value)


async def summary(db: Any, account_id: int, now: datetime, *, hours: int = 24) -> dict:
    """Read-only due-cohort completion; an incomplete window has no percentage."""
    start = now-timedelta(hours=hours)
    marker = await db.get(SystemSetting, COVERAGE_PREFIX+str(account_id))
    coverage = _data(marker.value) if marker else {}
    first, last = _date(coverage.get("started_at")), _date(coverage.get("last_scan_at"))
    reasons = []
    if not first or first > start:
        reasons.append("observation_window_not_covered")
    if not last or (now-last).total_seconds() > MAX_SCAN_GAP_SECONDS:
        reasons.append("dispatcher_scan_stale")
    if (_date(coverage.get("unknown_until")) or datetime.min) >= start:
        reasons.append("coverage_history_truncated")
    if any((_date(gap.get("start")) or datetime.min) < now and (_date(gap.get("end")) or datetime.max) > start
           for gap in coverage.get("gaps", [])):
        reasons.append("dispatcher_scan_gap")
    rows = [_data(row.value) for row in await _rows(db, account_id, now=now)]
    def verified_completion(row: dict) -> bool:
        return (row.get("state") == "completed" and type(row.get("message_id")) is int and row["message_id"] > 0
            and type(row.get("receipt_log_id")) is int and row["receipt_log_id"] > 0
            and (_date(row.get("completed_at")) or datetime.max) <= now)
    if any(not _date(row.get("due_at")) or row.get("state") not in {"pending", "completed"}
            or (row.get("state") == "completed" and not verified_completion(row)) for row in rows):
        reasons.append("measurement_row_invalid")
    due = [row for row in rows if (_date(row.get("due_at")) or datetime.max) <= now
        and ((_date(row.get("due_at")) or datetime.min) >= start or row.get("state") == "pending"
             or (_date(row.get("completed_at")) or datetime.min) >= start)]
    completed = sum(verified_completion(row) for row in due)
    complete = not reasons
    return {"available": bool(marker), "complete": complete, "incomplete_reasons": sorted(set(reasons)),
        "window_start": start, "window_end": now, "due": len(due), "completed": completed,
        "pending": len(due)-completed, "completion_rate": completed/len(due) if complete and due else None,
        "pending_before_window": sum(row.get("state") == "pending" and (_date(row.get("due_at")) or now) < start for row in rows),
        "pending_by_reason": dict(Counter(row.get("last_reason") or "software_dispatch_wait" for row in due if row.get("state") == "pending")),
        "denominator": "authorized_due_in_window_or_carried_pending_at_window_start;budget_and_software_waits_included",
        "sampling_interval_limit_seconds": MAX_SCAN_GAP_SECONDS,
        "source": "persistent_schedule_observations_and_linked_ad_delivery_log_receipts"}


async def observe_committed_receipts(receipts: dict[int, int]) -> None:
    """Measure recovered business receipts only after their commit, on the shared pool."""
    import asyncio

    from app.core.database import get_db_session
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.review_read_flags import growth_enabled_for
    from sqlalchemy import text

    receipts = {identity: account for identity, account in receipts.items() if growth_enabled_for(account)}
    if not receipts:
        return
    accounts = set(receipts.values())

    async def limits(db: Any) -> None:
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET LOCAL statement_timeout='300ms'"))
            await db.execute(text("SET LOCAL lock_timeout='300ms'"))

    try:
        async with asyncio.timeout(.5):
            async with get_db_session() as db:
                await limits(db)
                logs = list((await db.scalars(select(AdDeliveryLog).where(
                    AdDeliveryLog.id.in_(receipts),
                ).options(lazyload("*")))).all())
                if {log.id: log.account_id for log in logs} != receipts:
                    raise RuntimeError("committed_receipt_identity_missing")
                for log in logs:
                    await observe_log_safely(db, log)
    except (Exception, asyncio.CancelledError) as exc:
        # Cancellation/commit failure may roll back the safe hook's own gap.
        # Preserve that uncertainty in a fresh, bounded shared-pool transaction.
        gap_recorded = False
        try:
            async with asyncio.timeout(.3):
                async with get_db_session() as db:
                    await limits(db)
                    now = datetime.utcnow()
                    for account in sorted(accounts):
                        key = COVERAGE_PREFIX + str(account)
                        marker = await db.get(SystemSetting, key, with_for_update=True)
                        value = _data(marker.value) if marker else {"started_at": now.isoformat()}
                        _gap(value, _date(value.get("last_scan_at")) or now-timedelta(seconds=1),
                            now, "receipt_link_failed")
                        if marker is None:
                            marker = SystemSetting(key=key, value="{}")
                            db.add(marker)
                        _save(marker, value)
            gap_recorded = True
        except (Exception, asyncio.CancelledError):
            pass
        import structlog

        structlog.get_logger(__name__).warning("ad_recovered_receipt_measurement_failed",
            receipt_log_ids=sorted(receipts), account_ids=sorted(accounts),
            error=type(exc).__name__, gap_recorded=gap_recorded)
        if isinstance(exc, asyncio.CancelledError):
            raise
