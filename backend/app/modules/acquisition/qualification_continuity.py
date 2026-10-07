"""Keep review jobs separate from authorization and reuse verified own receipts.

Recovery is local database work. It never reads Telegram or invents a new
observation timestamp, and it appends an audit instead of rewriting old evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.models import TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import AdDeliveryLog, GroupAdProfile, GroupQualificationAudit
from app.modules.acquisition.qualification_identity import identity_relation, peer_identity
from app.modules.acquisition.qualification_lifetime import evidence_expiry, renewal_at

OWN_SOURCE = "verified_own_delivery_survived"
REVIEW_ONLY_REASONS = {
    "telegram_read_budget",
    "telegram_rpc_cooldown",
    "telegram_rpc_guard_unavailable",
    "AccountOperationLeaseBusy",
    "AccountOperationLeaseUnavailable",
    "TimeoutError",
    "ConnectionError",
    "OSError",
    "account_unavailable",
    "group_rules_ai_consensus_failed",
    "group_rules_unavailable",
    "group_rules_no_authoritative_evidence",
}
RETIRED_PRECEDENT_REASONS = {
    "advertising_precedent_disappeared_before_send",
    "advertising_precedent_identity_changed_before_send",
    "advertising_precedent_changed_before_send",
}


def payload(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def review_only(row: Any) -> bool:
    """Only an unfinished/technical review may be skipped; never a rejection."""
    if row.state == "manual_required":
        return False
    if row.decision == "technical_wait":
        snapshot = payload(getattr(row, "evidence_json", None))
        return (
            row.reason not in {"membership_not_participant", "group_identity_mismatch"}
            and (snapshot.get("permissions") or {}).get("member") is not False
        )
    if (
        row.state in {"queued", "running", "waiting_ai", "reviewing_ai"}
        and row.decision == "unknown"
    ):
        return True
    return row.decision in {"technical_wait", "observe"} and row.reason in REVIEW_ONLY_REASONS


def choose_authorization(rows: list[Any]) -> Any:
    """Rows are newest first. A substantive negative result stops fallback."""
    for index, row in enumerate(rows):
        if not review_only(row):
            if index and row.decision not in {"allowed", "trial"}:
                return rows[0]  # Never lend a historical rejection to an exit action.
            return row
    return rows[0] if rows else None


async def effective_review(db: Any, membership_id: int) -> Any:
    rows = list(
        (
            await db.scalars(
                select(GroupQualificationAudit)
                .where(
                    GroupQualificationAudit.membership_id == membership_id,
                    GroupQualificationAudit.state != "cancelled",
                )
                .order_by(GroupQualificationAudit.id.desc())
            )
        ).all()
    )
    return choose_authorization(rows)


def verified_observation(log: Any, member: Any, group: Any, now: datetime, *, enduring: bool = False) -> dict | None:
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_service import _date

    context = payload(log.qualification_context_json)
    sent, checked = log.sent_at, log.survived_twenty_four_hour_at
    identity = peer_identity(log.telegram_group_id, context)
    if (
        log.status != "success"
        or not log.telegram_message_id
        or log.survival_status != "survived"
        or log.survival_error
        or sent is None
        or checked is None
        or member.joined_at is None
        or sent < member.joined_at
        or checked < sent + timedelta(hours=24)
        or checked > now
        or (not enduring and evidence_expiry(checked, now) <= now)
        or log.account_id != member.account_id
        or log.group_id != member.group_id
        or context.get("account_id") != member.account_id
        or context.get("policy_version") != POLICY_VERSION
        or context.get("content_scope") != "text_profile"
        or context.get("decision") not in {"allowed", "trial"}
        or _date(context.get("membership_joined_at")) != member.joined_at
        or identity is None
        or identity[1] is None
        or identity_relation(identity, peer_identity(group.group_id, context)) != "same"
        or identity_relation(identity, peer_identity(member.telegram_group_id, context)) != "same"
    ):
        return None
    for observation in context.get("survival_observations", []):
        facts = observation.get("facts") or {}
        if (
            observation.get("stage") == "twenty_four_hour"
            and _date(observation.get("checked_at")) == checked
            and not observation.get("reason")
            and not facts.get("errors")
            and all(
                facts.get(key) is True
                for key in (
                    "exists",
                    "is_own_message",
                    "member",
                    "can_send",
                    "group_accessible",
                    "account_readable",
                )
            )
            and type(facts.get("account_user_id")) is int
        ):
            return {
                "log_id": log.id,
                "message_id": log.telegram_message_id,
                "source_audit_id": context.get("audit_id"),
                "source_evidence_hash": context.get("evidence_hash"),
                "sent_at": sent.isoformat(),
                "checked_at": checked.isoformat(),
                "account_user_id": facts["account_user_id"],
            }
    return None


async def own_proof_valid(
    db: Any, snapshot: dict, member: Any, group: Any, now: datetime | None = None
) -> bool:
    proof = snapshot.get("own_delivery_proof")
    if not isinstance(proof, dict) or type(proof.get("log_id")) is not int:
        return False
    log = await db.get(AdDeliveryLog, proof["log_id"], populate_existing=True)
    return bool(log and verified_observation(log, member, group, now or datetime.utcnow(),
        enduring=snapshot.get("authorization_mode") == "events") == proof)


def recoverable(row: Any) -> bool:
    return (
        row.decision in {"allowed", "trial"}
        or review_only(row)
        or (row.decision == "observe" and row.reason in RETIRED_PRECEDENT_REASONS)
    )


async def restore_own_authorization(
    db: Any, membership_id: int, *, apply: bool = False, log_id: int | None = None
) -> dict:
    """Preview or append one scoped authorization; caller owns commit/rollback."""
    from app.core.group.identity import is_owned_group_target
    from app.modules.acquisition.group_qualification import POLICY_VERSION, snapshot_hash
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_service import (
        _date,
        account_block_reason,
        manually_protected,
        policy,
        scope_matches,
    )
    from app.modules.acquisition.qualification_system_identity import covered_system_ids

    now = datetime.utcnow()
    query = select(GroupAccountMembership).where(GroupAccountMembership.id == membership_id)
    if apply:
        query = query.with_for_update(of=GroupAccountMembership)
    member = await db.scalar(query.execution_options(populate_existing=True))
    result = {"membership_id": membership_id, "restored": False}
    if member is None:
        return {**result, "reason": "membership_missing"}
    result["group_id"] = member.group_id
    from app.modules.acquisition.qualification_events import pending
    if await pending(db, member):
        return {**result, "reason": "qualification_event_pending"}
    group = await db.get(Group, member.group_id)
    account = await db.get(TelegramAccount, member.account_id)
    config = await policy(db, fresh=True)
    if not config.get("enabled") or (config.get("rollout_accounts") or {}).get(
        str(member.account_id), {}
    ).get("phase") not in {"pilot", "dynamic"}:
        return {**result, "reason": "qualification_rollout_paused"}
    if (
        member.status != "joined"
        or member.ad_status in {"paused", "blocked"}
        or member.review_status in {"manual_required", "exit_pending", "owned_group_excluded"}
        or (member.ad_pause_until and member.ad_pause_until > now)
        or account_block_reason(account, now)
        or manually_protected(config, group, member)
        or await is_owned_group_target(db, core_group_id=group.id, telegram_group_id=group.group_id)
    ):
        return {**result, "reason": "membership_or_account_protected"}
    profile = await db.scalar(select(GroupAdProfile).where(GroupAdProfile.group_id == group.id))
    if profile and (
        (profile.paused_until and profile.paused_until > now)
        or (
            profile.ad_policy_source == "manual"
            and profile.ad_policy_mode == "forbidden"
            and (profile.ad_policy_expires_at is None or profile.ad_policy_expires_at > now)
        )
    ):
        return {**result, "reason": "group_manually_paused"}
    rows = list(
        (
            await db.scalars(
                select(GroupQualificationAudit)
                .where(
                    GroupQualificationAudit.membership_id == member.id,
                    GroupQualificationAudit.state != "cancelled",
                )
                .order_by(GroupQualificationAudit.id.desc())
            )
        ).all()
    )
    latest = rows[0] if rows else None
    if latest is None or not scope_matches(latest, group, member) or not recoverable(latest):
        return {**result, "reason": "substantive_review_or_scope_change"}
    if latest.policy_version != POLICY_VERSION or latest.membership_joined_at != member.joined_at:
        return {**result, "reason": "policy_or_membership_changed"}
    current = payload(latest.evidence_json)
    if current.get("authorization_basis") == OWN_SOURCE and await own_proof_valid(
        db, current, member, group, now
    ):
        if log_id is None or current["own_delivery_proof"]["log_id"] == log_id:
            return {**result, "reason": "already_established", "audit_id": latest.id}
    logs_query = (
        select(AdDeliveryLog)
        .where(
            AdDeliveryLog.account_id == member.account_id,
            AdDeliveryLog.group_id == member.group_id,
            AdDeliveryLog.sent_at >= member.joined_at,
            AdDeliveryLog.status == "success",
            AdDeliveryLog.survived_twenty_four_hour_at.is_not(None),
        )
        .order_by(AdDeliveryLog.survived_twenty_four_hour_at.desc())
    )
    if log_id is not None:
        logs_query = logs_query.where(AdDeliveryLog.id == log_id)
    logs = list((await db.scalars(logs_query)).all())
    system_ids, fingerprint = await covered_system_ids(db, config, account_id=member.account_id)
    by_id = {row.id: row for row in rows}
    for log in logs:
        proof = verified_observation(log, member, group, now)
        if (
            proof is None
            or not fingerprint
            or proof["account_user_id"]
            != (config.get("system_account_user_ids") or {}).get(str(member.account_id))
        ):
            continue
        source = by_id.get(proof["source_audit_id"])
        if (
            source is None
            or source.membership_joined_at != member.joined_at
            or not scope_matches(source, group, member)
        ):
            continue
        context = payload(log.qualification_context_json)
        stored = payload(source.evidence_json)
        candidates = [stored] + list(reversed(stored.get("previous_checks", [])))
        base = next(
            (
                p
                for p in candidates
                if (
                    p.get("profile_fingerprint") == profile_fingerprint(account)
                    and p.get("policy_version") == POLICY_VERSION
                    and p.get("account_id") == member.account_id
                    and p.get("group_id") == group.id
                    and p.get("quality_status") == "qualified"
                    and (p.get("advertising_audit") or {}).get("ad_allowed") is True
                    and identity_relation(
                        peer_identity(group.group_id, p),
                        peer_identity(log.telegram_group_id, context),
                    )
                    == "same"
                    and _date(p.get("collected_at")) is not None
                    and _date(p["collected_at"]) <= log.sent_at
                )
            ),
            None,
        )
        if base is None:
            continue
        # Do not revive a later rejection, even if another technical review hides it.
        if any(
            row.checked_at and row.checked_at > log.sent_at and not recoverable(row) for row in rows
        ):
            continue
        if any(
            p.get("decision") in {"reject", "protected"}
            and (_date(p.get("checked_at")) or datetime.min) > log.sent_at
            for row in rows
            for p in [payload(row.evidence_json)]
            + payload(row.evidence_json).get("previous_checks", [])
        ):
            continue
        later_failure = await db.scalar(
            select(AdDeliveryLog.id)
            .where(
                AdDeliveryLog.account_id == member.account_id,
                AdDeliveryLog.group_id == member.group_id,
                AdDeliveryLog.sent_at >= log.sent_at,
                AdDeliveryLog.survival_status.in_(["deleted", "check_failed"]),
            )
            .limit(1)
        )
        if later_failure is not None:
            continue
        observed = log.survived_twenty_four_hour_at
        snapshot = {
            k: v
            for k, v in base.items()
            if k
            not in {
                "previous_checks",
                "pending_collection",
                "invalidated_at",
                "renewal_failure_type",
                "ai_pending",
                "ai_review_incomplete",
                "unknowns",
            }
        }
        snapshot.update(
            decision="trial",
            reason="own_ad_survived_twenty_four_hours",
            authorization_basis=OWN_SOURCE,
            authorization_mode="events",
            own_delivery_proof=proof,
            authorization_recovery={
                "previous_audit_id": latest.id,
                "previous_reason": latest.reason,
                "recovered_at": now.isoformat(),
            },
            full_collected_at=base.get("full_collected_at") or base["collected_at"],
            collected_at=observed.isoformat(),
            checked_at=observed.isoformat(),
            technical_errors=[],
            system_identity_coverage=True,
            system_identity_fingerprint=fingerprint,
            system_user_ids=sorted(system_ids),
            advertising_audit={
                **(base.get("advertising_audit") or {}),
                "ad_allowed": True,
                "policy_mode": "soft_ad_trial",
                "decision_source": OWN_SOURCE,
                "reason": "own_ad_survived_twenty_four_hours",
            },
        )
        result.update(
            reason="own_ad_survived_twenty_four_hours",
            log_id=log.id,
            previous_audit_id=latest.id,
            checked_at=observed.isoformat(),
            expires_at=evidence_expiry(observed, now).isoformat(),
        )
        if not apply:
            return {**result, "eligible": True}
        restored = GroupQualificationAudit(
            batch_id=f"own-delivery:{log.id}",
            membership_id=member.id,
            account_id=member.account_id,
            group_id=group.id,
            policy_version=POLICY_VERSION,
            content_scope="text_profile",
            state="completed",
            decision="trial",
            reason=result["reason"],
            membership_joined_at=member.joined_at,
            checked_at=observed,
            expires_at=evidence_expiry(observed, now),
            next_retry_at=renewal_at(observed, now),
            evidence_json=json.dumps(snapshot, ensure_ascii=False, default=str),
            evidence_hash=snapshot_hash(snapshot, member.account_id, "text_profile"),
        )
        # An old recovery must not be replayed over a later operator decision.
        existing = await db.scalar(
            select(GroupQualificationAudit.id).where(
                GroupQualificationAudit.batch_id == restored.batch_id,
                GroupQualificationAudit.membership_id == member.id,
            )
        )
        if existing is not None:
            return {**result, "reason": "recovery_already_recorded", "audit_id": existing}
        db.add(restored)
        member.review_status, member.ad_status = "approved", "active"
        member.review_next_at = restored.next_retry_at
        await db.flush()
        from app.modules.acquisition.automation import (
            AcquisitionAutomationService,
            JoinedGroupAuditResult,
        )

        profile = await AcquisitionAutomationService(db)._sync_group_ad_policy_from_audit(
            group,
            JoinedGroupAuditResult(
                passed=True,
                ad_allowed=True,
                can_send_messages=True,
                ad_rule_details={**snapshot["advertising_audit"], "qualification": snapshot},
            ),
            commit=False,
        )
        if profile.ad_policy_source != "manual":
            profile.ad_policy_expires_at = restored.expires_at
            profile.ad_policy_verified_at = observed
        return {**result, "restored": True, "audit_id": restored.id}
    return {**result, "reason": "no_scoped_own_survival_evidence"}
