"""Persistent qualification execution and send gate; evidence reads do not send or leave."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, desc, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.core.account.models import TelegramAccount
from app.core.group.identity import is_owned_group_target, telegram_group_id_aliases
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition.group_qualification import (
    LINK_BAN,
    NEGATED_BAN,
    POLICY_VERSION,
    TOPIC_CONDITION,
    EvidenceCollector,
    snapshot_hash,
    trial_evidence,
    trial_proof,
)
from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt, GroupQualificationAudit
from app.modules.acquisition.qualification_identity import (
    PeerIdentity,
    identity_aliases,
    identity_relation,
    peer_identity,
)
from app.modules.acquisition.qualification_system_identity import covered_system_ids
from app.modules.acquisition.send_restriction import (
    confirmed_denial, track_restriction, verified_long_restriction,
)

from app.modules.acquisition.qualification_lifetime import EVIDENCE_TTL, authorization_current, evidence_expiry, renewal_at

SETTING_KEY = "automation.group_qualification"
AI_REVIEW_INCOMPLETE_REASONS = {
    "group_rules_ai_unavailable",
    "group_rules_ai_provider_content_rejected",
    "group_rules_ai_queued",
    "group_rules_ai_destination_unapproved",
    "group_rules_ai_disabled",
    "group_rules_ai_evidence_arguments_incomplete",
    "group_rules_ai_unresolved_conflict",
    "group_rules_ai_consensus_failed",
}

# Continuity-restored trials re-verify cheaply every cycle; after this many
# verifications they graduate to the standard long renewal schedule instead
# of occupying the review lane daily.
CONTINUITY_TRIAL_REASONS = {
    "own_ad_survived_twenty_four_hours",
    "ordinary_member_ads_verified",
}
CONTINUITY_TRIAL_GRADUATION_ATTEMPTS = 3
# Light re-checks that keep re-confirming the same non-converging state
# (retired precedent snapshots, stale reject verdicts) escalate after this
# many attempts instead of looping forever.
STALE_CLAIM_ESCALATION_ATTEMPTS = 3


def stale_claim_action(row: Any, *, now: datetime) -> str | None:
    """Classify a claimed row that keeps re-confirming the same non-converging state.

    Production data showed single rows re-claimed 100+ times: continuity
    trials re-verified daily, retired-precedent snapshots re-confirmed with
    4-read passes, and reject verdicts re-queued without a terminal write.
    Each returns the escape action for the claim loop; None keeps the normal
    review flow.
    """
    from app.modules.acquisition.qualification_continuity import RETIRED_PRECEDENT_REASONS

    attempts = int(getattr(row, "attempts", 0) or 0)
    reason = getattr(row, "reason", None)
    if (
        row.state == "queued" and row.decision == "trial"
        and reason in CONTINUITY_TRIAL_REASONS
        and attempts >= CONTINUITY_TRIAL_GRADUATION_ATTEMPTS
    ):
        return "graduate_trial"
    # Only the queued-reject anomaly finalizes here. Completed reject rows keep
    # their designed retry ladders: the AI-inconclusive observation ladder
    # counts per-material streaks, and maturity-scheduled rejects recover on
    # evidence maturation. Both would be cut short by a lifetime-attempts
    # threshold.
    if (
        row.state == "queued" and row.decision == "reject"
        and attempts >= STALE_CLAIM_ESCALATION_ATTEMPTS
    ):
        return "finalize_reject"
    if reason in RETIRED_PRECEDENT_REASONS and attempts >= STALE_CLAIM_ESCALATION_ATTEMPTS:
        return "escalate_full_review"
    return None


async def policy(db: Any, *, fresh: bool = False) -> dict[str, Any]:
    row = (
        await db.get(SystemSetting, SETTING_KEY, populate_existing=True)
        if fresh
        else await db.get(SystemSetting, SETTING_KEY)
    )
    config = json.loads(row.value) if row and row.value else {"enabled": False, "execute_exits": False}
    if config.get("verification_active_promoters") is True:
        from app.core.account.models import AccountOperationConfig, AccountType
        config["verification_account_ids"] = list((await db.scalars(
            select(AccountOperationConfig.account_id).join(
                TelegramAccount, TelegramAccount.id == AccountOperationConfig.account_id
            ).where(
                AccountOperationConfig.enabled.is_(True),
                AccountOperationConfig.auto_join_enabled.is_(True),
                TelegramAccount.account_type == AccountType.PROMOTER,
            )
        )).all())
    if config.get("promotion_active_promoters") is True:
        from app.core.account.models import AccountOperationConfig, AccountType
        active = list((await db.scalars(select(AccountOperationConfig.account_id).join(
            TelegramAccount, TelegramAccount.id == AccountOperationConfig.account_id,
        ).where(
            AccountOperationConfig.enabled.is_(True),
            AccountOperationConfig.auto_ads_enabled.is_(True),
            TelegramAccount.account_type == AccountType.PROMOTER,
        ))).all())
        config["promotion_account_ids"] = active
        rollout = config.setdefault("rollout_accounts", {})
        for account_id in active:
            # Explicit pauses remain pauses; a newly enabled promoter inherits
            # the global rules without requiring a second static allowlist edit.
            rollout.setdefault(str(account_id), {"phase": "dynamic"})
    return config


def exit_scope_filters(config: dict[str, Any]) -> tuple[set[int] | None, set[str] | None]:
    """Absent scopes retain compatibility; explicit empty scopes authorize nothing."""
    parsed = []
    for key, value_type in (("exit_membership_ids", int), ("exit_reason_allowlist", str)):
        if key not in config:
            parsed.append(None)
            continue
        values = config[key]
        if not isinstance(values, list) or any(
            type(value) is not value_type
            or (value <= 0 if value_type is int else not value or value != value.strip())
            for value in values
        ):
            raise ValueError("qualification_exit_scope_invalid:" + key)
        parsed.append(set(values))
    return parsed[0], parsed[1]


def _payload(row: GroupQualificationAudit | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        value = json.loads(row.evidence_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo:
            from datetime import UTC

            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _ids(values: Any) -> set[int]:
    return {int(value) for value in (values or []) if str(value).lstrip("-").isdigit()}


def verification_account_allowed(config: dict[str, Any], account_id: int) -> bool:
    """Allow a narrower verification scope without narrowing read-only reviews."""
    scope = config.get("verification_account_ids", config.get("account_ids"))
    if scope is None:
        return True
    return isinstance(scope, list) and any(
        type(value) is int and value == account_id for value in scope
    )


def manually_protected(
    config: dict[str, Any], group: Group, membership: GroupAccountMembership | None
) -> bool:
    """Manual protection is separate from permission to advertise."""
    return (
        group.id in _ids(config.get("manual_protected_group_ids"))
        or bool(
            set(telegram_group_id_aliases(group.group_id))
            & _ids(config.get("protected_telegram_group_ids"))
        )
        or (
            membership is not None and membership.id in _ids(config.get("protected_membership_ids"))
        )
    )


def scope_matches(
    row: GroupQualificationAudit, group: Group, member: GroupAccountMembership
) -> bool:
    snapshot = _payload(row)
    return (
        row.account_id == member.account_id
        and row.group_id == member.group_id == group.id
        and row.membership_id == member.id
        and row.membership_joined_at == member.joined_at
        and row.policy_version == POLICY_VERSION
        and row.content_scope == "text_profile"
        and snapshot.get("account_id") == member.account_id
        and snapshot.get("group_id") == group.id
        and snapshot.get("telegram_group_id") in telegram_group_id_aliases(group.group_id)
        and snapshot.get("content_scope") == row.content_scope
        and snapshot.get("policy_version") == POLICY_VERSION
        and snapshot.get("target_topic_id") is None
        and member.telegram_group_id in telegram_group_id_aliases(group.group_id)
    )


def qualification_exit_fact(
    row: GroupQualificationAudit,
    snapshot: dict[str, Any],
    *,
    checked_at: datetime | None = None,
    evidence_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "audit_id": row.id,
        "checked_at": (checked_at or row.checked_at).isoformat()
        if (checked_at or row.checked_at)
        else snapshot.get("checked_at"),
        "reason": snapshot.get("reason") or row.reason,
        "evidence_hash": evidence_hash or row.evidence_hash,
        "policy_version": snapshot.get("policy_version") or row.policy_version,
        "account_id": row.account_id,
        "group_id": row.group_id,
        "telegram_group_id": snapshot.get("telegram_group_id"),
        "exit_confirmed_at": None,
        "snapshot": {
            key: value
            for key, value in snapshot.items()
            if key not in {"qualification_exit_history", "previous_checks"}
        },
    }


def merge_exit_history(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never discard confirmed exit/rejection facts when a mutable audit is refreshed."""
    merged: dict[tuple, dict[str, Any]] = {}
    for item in items:
        key = (item.get("audit_id"), item.get("checked_at"), item.get("evidence_hash"))
        old = merged.get(key)
        if old is None:
            merged[key] = dict(item)
        elif item.get("exit_confirmed_at") and not old.get("exit_confirmed_at"):
            merged[key] = {**old, **item}
    return list(merged.values())


def authoritative_rule_fingerprint(snapshot: dict[str, Any]) -> str | None:
    rules = sorted(
        {
            (item.get("source"), str(item.get("text", "")).strip(), str(item.get("topic_id", "")))
            for item in snapshot.get("evidence", [])
            if item.get("source") in {"full_about", "pinned_message", "admin_rule"}
            and str(item.get("text", "")).strip()
        }
    )
    if not rules:
        return None
    return hashlib.sha256(json.dumps(rules, ensure_ascii=False).encode()).hexdigest()


def confirmed_group_bans(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep confirmed group bans independently of bounded observation history."""
    checks = [snapshot, *snapshot.get("previous_checks", [])]
    checks.extend(
        fact.get("snapshot") or {}
        for fact in snapshot.get("qualification_exit_history", [])
        if isinstance(fact, dict)
    )
    facts = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        facts.extend(
            fact for fact in check.get("confirmed_group_bans", []) if isinstance(fact, dict)
        )
        if check.get("group_level_advertising_ban"):
            facts.append(
                {
                    "rule_fingerprint": authoritative_rule_fingerprint(check),
                    "confirmed_at": check.get("checked_at"),
                    "reason": check.get("reason"),
                    "policy_version": check.get("policy_version"),
                    # A later raw-core review cannot reassign an old ban's namespace.
                    **{
                        key: check[key]
                        for key in ("telegram_group_id", "raw_peer_id", "group_type")
                        if key in check
                    },
                }
            )
    unique: dict[tuple, dict[str, Any]] = {}
    for fact in facts:
        key = (
            str(fact.get("rule_fingerprint") or "unfingerprinted"),
            fact.get("telegram_group_id"),
            fact.get("raw_peer_id"),
            fact.get("group_type"),
        )
        old = unique.get(key)
        # A repeated prohibition invalidates an earlier clearance for those rules.
        if old is None or (_date(fact.get("confirmed_at")) or datetime.min) > (
            _date(old.get("confirmed_at")) or datetime.min
        ):
            unique[key] = fact
    return list(unique.values())


async def qualification_peer_history(
    db: Any, identity: PeerIdentity
) -> list[tuple[Group, GroupQualificationAudit]]:
    """Load candidates; callers must still check each historical fact's namespace.

    Requeued rows without checked_at retain their persisted bans and exit facts.
    History is never limited to the latest review or the current account/core row.
    """
    return list(
        (
            await db.execute(
                select(Group, GroupQualificationAudit)
                .join(GroupQualificationAudit, GroupQualificationAudit.group_id == Group.id)
                .where(Group.group_id.in_(identity_aliases(identity)))
                .order_by(GroupQualificationAudit.id.desc())
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def group_ban_cleared(
    config: dict[str, Any], group: Group, row: GroupQualificationAudit, ban: dict[str, Any]
) -> bool:
    """A newer complete explicit-permission review supersedes historical rules."""
    snapshot = _payload(row)
    confirmed_at = _date(ban.get("confirmed_at"))
    return bool(
        row.decision == "allowed" and row.state == "completed"
        and snapshot.get("reason") == "explicit_permission_verified"
        and authoritative_rule_fingerprint(snapshot)
        and authoritative_rule_fingerprint(snapshot) != ban.get("rule_fingerprint")
        and not snapshot.get("rules_incomplete")
        and not snapshot.get("group_level_advertising_ban")
        and confirmed_at is not None and row.checked_at is not None
        and row.checked_at > confirmed_at
    )


def verified_permanent_send_restriction(snapshot: dict[str, Any]) -> bool:
    permissions = snapshot.get("permissions", {})
    return bool(
        not snapshot.get("technical_errors")
        and permissions.get("member") is True
        and permissions.get("can_send_text") is False
        and permissions.get("permanent_send_restriction_verified") is True
        and permissions.get("verification_required") is not True
        and permissions.get("verification_pending") is not True
        and not any(
            permissions.get(key)
            for key in (
                "temporary_until",
                "newcomer_until",
                "slowmode_until",
            )
        )
    )


def annotate_newcomer_restriction(snapshot: dict, joined_at: datetime | None, now: datetime) -> None:
    """Telegram's indefinite mute can be a pending newcomer challenge.

    Preserve the existing verification lane's 48-hour window before treating a
    never-writable new membership as a confirmed long-term restriction.
    """
    permissions = snapshot.get("permissions") or {}
    # An explicit finite ban beyond three days is not an unspecified newcomer wait.
    if permissions.get("temporary_until") and verified_long_restriction(snapshot, now):
        return
    if permissions.get("can_send_text") is False and joined_at and joined_at > now - timedelta(hours=48):
        permissions["verification_pending"] = True
        permissions["newcomer_until"] = (joined_at + timedelta(hours=48)).isoformat()


def account_block_reason(account: TelegramAccount | None, now: datetime) -> str | None:
    if account is None or not account.is_active:
        return "account_unavailable"
    status = str(getattr(account.status, "value", account.status)).lower()
    if status in {"banned", "restricted", "error"}:
        return "account_status_unavailable"
    if account.spam_check_status in {"restricted", "flagged"}:
        return "account_spam_restricted"
    if account.risk_pause_until and account.risk_pause_until > now:
        return "account_risk_paused"
    if account.risk_level not in {"normal", "watch"}:
        return "account_risk_level_not_allowed"
    return None


def restricted_evidence_read_allowed(
    account: TelegramAccount | None, config: dict[str, Any], now: datetime
) -> bool:
    return bool(
        config.get("allow_restricted_evidence_reads") is True
        and account is not None
        and account.is_active
        and str(getattr(account.status, "value", account.status)).lower() == "restricted"
        and account.risk_level in {"normal", "watch"}
        and not (account.risk_pause_until and account.risk_pause_until > now)
    )


async def latest_review(
    db: Any, membership_id: int, *, include_pending: bool = False
) -> GroupQualificationAudit | None:
    query = select(GroupQualificationAudit).where(
        GroupQualificationAudit.membership_id == membership_id,
        GroupQualificationAudit.state != "cancelled",
    )
    if not include_pending:
        query = query.where(GroupQualificationAudit.checked_at.is_not(None))
    # A newer queued review invalidates older approvals immediately.
    return await db.scalar(query.order_by(desc(GroupQualificationAudit.id)).limit(1))


class AuthorizationResolution(tuple):
    """Three-value compatible result with a fail-closed resolution diagnostic."""

    reason: str | None

    def __new__(cls, row=None, group=None, member=None, *, reason=None):
        result = super().__new__(cls, (row, group, member))
        result.reason = reason
        return result


async def current_authorization(
    db: Any, account_id: int, target: int | str
) -> AuthorizationResolution:
    """Resolve a unique account relationship before choosing its scoped audit."""
    target_aliases = None
    if isinstance(target, int) or str(target).lstrip("-").isdigit():
        target_aliases = telegram_group_id_aliases(int(target))
        clause = Group.group_id.in_(target_aliases)
    else:
        username = (
            str(target)
            .strip()
            .removeprefix("https://t.me/")
            .removeprefix("http://t.me/")
            .lstrip("@")
            .rstrip("/")
        )
        if not username or "/" in username or "?" in username:
            return AuthorizationResolution(reason="qualification_target_invalid")
        clause = func.lower(Group.username) == username.lower()
    # Orphaned aliases and another account's relationship cannot select our group.
    # Keep historical relationships in this query: conflicting duplicates require
    # reconciliation, never an arbitrary newest/active row or an automatic merge.
    matches = (
        await db.execute(
            select(Group, GroupAccountMembership)
            .join(GroupAccountMembership, GroupAccountMembership.group_id == Group.id)
            .where(clause, GroupAccountMembership.account_id == account_id)
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not matches:
        return AuthorizationResolution(reason="qualification_membership_missing")
    if len(matches) != 1:
        return AuthorizationResolution(reason="qualification_membership_ambiguous")
    group, member = matches[0]
    if member.telegram_group_id not in telegram_group_id_aliases(group.group_id) or (
        target_aliases is not None and member.telegram_group_id not in target_aliases
    ):
        return AuthorizationResolution(reason="qualification_membership_identity_changed")
    from app.modules.acquisition.qualification_continuity import effective_review
    row = await effective_review(db, member.id)
    return AuthorizationResolution(
        row, group, member, reason="qualification_review_required" if row is None else None
    )


def automatic_exit_reason(snapshot: dict[str, Any]) -> str | None:
    """The exit policy is A or B or (C and D), with confirmed evidence per arm.

    A: the latest 48 hours are completely read and have no ordinary-member ad.
    B: the verified group member count is below 50.
    C and D: an explicit group-wide ad ban and fewer than two online members.
    """
    if (
        snapshot.get("protected") is True
        or (snapshot.get("permissions") or {}).get("member") is not True
        or (snapshot.get("account_eligibility") or {}).get("can_auto_leave") is False
    ):
        return None
    if verified_permanent_send_restriction(snapshot):
        return "account_permanent_send_restriction"
    observed_at = _date(snapshot.get("collected_at")) or _date(snapshot.get("checked_at")) or datetime.utcnow()
    if verified_long_restriction(snapshot, observed_at):
        return "account_long_send_restriction"
    coverage = snapshot.get("coverage")
    recent_complete = (
        isinstance(coverage, list)
        and len(coverage) >= 2
        and all(
            isinstance(segment, dict) and segment.get("complete") is True
            for segment in coverage[:2]
        )
    )
    ad_count = snapshot.get("ordinary_advertising_last_48h_count")
    history_unknowns = snapshot.get("unknowns") or []
    ad_presence_uncertain = {
        "history_hidden", "history_pagination_stalled", "message_date_unknown",
        "message_id_unknown", "media_without_readable_caption",
        "sender_identity_unverified", "advertisement_no_longer_accessible",
        "advertisement_content_changed",
    }
    no_recent_ad = (
        recent_complete
        and type(ad_count) is int
        and ad_count == 0
        and not any(reason in ad_presence_uncertain for reason in history_unknowns)
    )
    members = snapshot.get("member_count")
    few_members = (
        snapshot.get("member_count_verified") is True
        and type(members) is int
        and 0 <= members < 50
    )
    online = snapshot.get("online_count")
    ban_and_few_online = (
        snapshot.get("rules_incomplete") is False
        and snapshot.get("exit_group_rule_ban") is True
        and type(online) is int
        and 0 <= online < 2
        and snapshot.get("online_count_source")
        in {"full_chat.online_count", "messages.getOnlines"}
    )
    if no_recent_ad:
        return "no_ordinary_member_ad_48h"
    if few_members:
        return "members_below_50"
    if ban_and_few_online:
        return "group_ad_ban_and_online_below_2"
    return None


def qualifies_for_automatic_exit(snapshot: dict[str, Any]) -> bool:
    return automatic_exit_reason(snapshot) is not None


def ai_failure_without_exit_evidence(snapshot: dict[str, Any]) -> bool:
    """A failed recognition cannot manufacture exit or permanent rejoin facts."""
    return bool(
        snapshot.get("ai_final")
        and snapshot.get("ai_decision") == "fail"
        and not snapshot.get("group_level_advertising_ban")
        and not snapshot.get("exit_group_rule_ban")
        and not qualifies_for_automatic_exit(snapshot)
    )


def _base_review_schedule(
    snapshot: dict[str, Any], previous: dict[str, Any], now: datetime
) -> tuple[str, str, str, datetime | None]:
    """Bounded observation starts with valid evidence, never with queue creation."""
    verdict, reason = snapshot["decision"], snapshot["reason"]
    if verdict in {"observe", "wait"}:
        if verified_permanent_send_restriction(snapshot):
            verdict, reason = "reject", "account_permanent_send_restriction"
        elif verified_long_restriction(snapshot, now):
            verdict, reason = "reject", "account_long_send_restriction"
        elif confirmed_denial(snapshot, now):
            # Keep collecting live rights throughout the three-day observation period.
            expiry = _date((snapshot.get("permissions") or {}).get("temporary_until"))
            retry = min(now + timedelta(hours=2), expiry) if expiry else now + timedelta(hours=2)
            snapshot["decision"], snapshot["reason"] = verdict, reason
            return verdict, reason, "completed", retry
    failures = int(previous.get("technical_failures", 0))
    started = _date(previous.get("observation_started_at"))
    state, retry = "completed", None
    snapshot["ai_review_failures"] = (
        int(previous.get("ai_review_failures", 0)) + 1
        if verdict == "observe" and snapshot.get("ai_review_incomplete")
        else 0
    )
    if snapshot.get("ai_pending"):
        # Waiting on the model is not a Telegram failure or a group observation verdict.
        snapshot["technical_failures"] = failures
        snapshot["ai_review_failures"] = int(previous.get("ai_review_failures", 0))
        snapshot["observation_started_at"] = started.isoformat() if started else None
        snapshot["decision"], snapshot["reason"] = "observe", reason
        # A model retry cannot satisfy missing live account or group evidence.
        missing_live_evidence = reason in {
            "ordinary_member_advertising_unverified",
            "no_other_online_member",
            "online_count_unknown",
            "system_account_identity_unconfirmed",
        }
        retry = now + (timedelta(hours=2) if missing_live_evidence else timedelta(minutes=5))
        return "observe", reason, "waiting_ai", retry
    if reason in {"topic_route_requires_review", "qualification_content_scope_unsupported", "group_rules_ai_provider_content_rejected"}:
        snapshot["technical_failures"] = failures
        snapshot["observation_started_at"] = started.isoformat() if started else None
        return "observe", reason, "completed", now + timedelta(hours=2)
    if verdict == "observe" and snapshot.get("ai_review_incomplete"):
        # An AI outage or incomplete adjudication is not evidence that ads are
        # absent/forbidden. Bound retries independently of Telegram read errors.
        started = started or now
        if snapshot["ai_review_failures"] >= 3 or now >= started + timedelta(hours=24):
            state = "manual_required"
        else:
            retry = min(now + timedelta(hours=2), started + timedelta(hours=24))
    elif verdict == "technical_wait":
        lease_busy = reason == "AccountOperationLeaseBusy"
        if lease_busy:
            # A busy account lease is scheduler contention, not a failed evidence
            # read. Retry it without consuming the three-failure technical limit.
            lease_started = _date(previous.get("lease_wait_started_at")) or now
            lease_deadline = lease_started + timedelta(hours=24)
            snapshot["lease_wait_started_at"] = lease_started.isoformat()
            if now >= lease_deadline:
                state = "manual_required"
            else:
                retry = min(now + timedelta(minutes=2), lease_deadline)
        else:
            failures += 1
            if failures >= 3:
                state = "manual_required"
            else:
                retry = now + timedelta(hours=2)
        if retry is not None:
            try:
                seconds = max(0, int(snapshot.get("retry_after_seconds") or 0))
                retry = max(retry, now + timedelta(seconds=seconds))
                if lease_busy:
                    retry = min(retry, lease_deadline)
            except (TypeError, ValueError, OverflowError):
                state, reason, retry = "manual_required", "invalid_technical_retry_deadline", None
    elif verdict not in {"protected", "cancelled"}:
        started = started or now
        if verdict in {"observe", "wait"}:
            final = started + timedelta(hours=24)
            maximum = started + timedelta(hours=48)
            permission = snapshot.get("permissions", {})
            wait_until = max(
                filter(
                    None,
                    (
                        _date(permission.get("temporary_until")),
                        _date(permission.get("slowmode_until")),
                        _date(permission.get("newcomer_until")),
                    ),
                ),
                default=None,
            )
            if verdict == "wait" and wait_until:
                if now >= maximum:
                    state, reason = "manual_required", "known_wait_exceeds_observation_limit"
                else:
                    retry = min(max(now + timedelta(minutes=1), wait_until), maximum)
            elif now >= final:
                # Missing/inaccessible facts are never silently converted to a ban.
                if (
                    snapshot.get("unknowns")
                    or snapshot.get("rules_incomplete")
                    or snapshot.get("quality_status") != "qualified"
                ):
                    state, reason = "manual_required", "qualification_evidence_incomplete"
                else:
                    verdict, reason = (
                        "reject",
                        "advertising_evidence_insufficient_after_observation",
                    )
            else:
                retry = min(started + timedelta(hours=2), final)
                if retry <= now:
                    retry = final
    if verdict in {"allowed", "trial"}:
        retry = renewal_at(_date(snapshot.get("collected_at")) or now, now)
    # Review does not have a human approval lane. Unresolved evidence remains
    # blocked for advertising and is retried automatically.
    if state == "manual_required":
        state = "completed"
        retry = now + timedelta(hours=2)
        if verdict == "technical_wait":
            try:
                seconds = max(0, int(snapshot.get("retry_after_seconds") or 0))
                retry = max(retry, now + timedelta(seconds=seconds))
            except (TypeError, ValueError, OverflowError):
                pass
    snapshot["observation_started_at"] = started.isoformat() if started else None
    snapshot["technical_failures"] = failures
    snapshot["decision"], snapshot["reason"] = verdict, reason
    return verdict, reason, state, retry


def review_schedule(
    snapshot: dict[str, Any], previous: dict[str, Any], now: datetime
) -> tuple[str, str, str, datetime | None]:
    from app.modules.acquisition.evidence_progress import evidence_maturity, progress_retry

    original_reason = snapshot["reason"]
    # AI recognition is single shot for one material set. A provider failure
    # closes that material immediately, while a genuinely changed rule/evidence
    # set is allowed to enter one fresh recognition attempt. ``ai_material_key``
    # is strongest; the rule fingerprint covers legacy rows.
    ai_material = snapshot.get("ai_material_key")
    previous_material = previous.get("ai_material_key")
    if ai_material and previous_material:
        material_changed = ai_material != previous_material
        current_fingerprint = str(ai_material)
    else:
        from app.modules.acquisition.qualification_retry import rule_retry_fingerprint

        current_fingerprint = rule_retry_fingerprint(snapshot)
        previous_audit = previous.get("advertising_audit")
        if not isinstance(previous_audit, dict):
            previous_audit = {}
        previous_attempted = bool(
            previous.get("rejection_fingerprint")
            or previous.get("ai_material_key")
            or previous.get("ai_final")
            or previous.get("ai_review_incomplete")
            or previous.get("ai_pending")
            or previous_audit.get("decision_source")
        )
        previous_fingerprint = str(previous.get("rejection_fingerprint") or "")
        if not previous_fingerprint and previous_attempted:
            previous_fingerprint = rule_retry_fingerprint(previous)
        material_changed = bool(previous_attempted and previous_fingerprint != current_fingerprint)
    # ``ai_pending`` means the first recognition has not started yet and must
    # remain in the bounded waiting lane.  Only an explicit incomplete marker
    # or a terminal AI reason without that pending marker represents an unknown
    # result that must fail closed.
    ai_incomplete = bool(
        snapshot.get("ai_review_incomplete")
        or (
            original_reason in AI_REVIEW_INCOMPLETE_REASONS
            and not snapshot.get("ai_pending")
        )
    )
    # New material gets exactly one new attempt. The next worker pass owns the
    # provider call; this pass only records the changed-material wake-up.
    if ai_incomplete and material_changed and not (
        snapshot.get("ai_final") and snapshot.get("ai_decision") == "fail"
    ):
        snapshot.update(
            ai_pending=True,
            ai_review_incomplete=True,
            ai_final=False,
            ai_decision=None,
            rejection_fingerprint=current_fingerprint,
            unchanged_rejection_count=1,
            review_trigger="evidence_changed",
            decision="observe",
            reason=original_reason or "group_rules_ai_unknown",
        )
        return "observe", snapshot["reason"], "completed", now + timedelta(hours=2)
    # AI is a binary fail-closed gate for a *determinative* verdict, but an
    # inconclusive outcome (provider outage, unresolved evidence conflict) is
    # a measurement failure, not a group-quality verdict. Fail closed only
    # after the same material produced three inconclusive attempts; until
    # then keep the membership under a degraded observation window — ads stay
    # blocked (observe never authorizes sends) while fresh evidence
    # accumulates for a new recognition attempt.
    if ai_incomplete and not (
        snapshot.get("ai_final") and snapshot.get("ai_decision") == "fail"
    ):
        streak = (int(previous.get("unchanged_rejection_count", 0)) + 1
                  if previous.get("rejection_fingerprint") == current_fingerprint else 1)
        if streak >= 3:
            snapshot.update(
                ai_pending=False,
                ai_review_incomplete=False,
                ai_final=True,
                ai_decision="fail",
                decision="reject",
                reason=original_reason or "group_rules_ai_unknown",
                rejection_fingerprint=current_fingerprint,
                unchanged_rejection_count=streak,
                review_trigger="ai_terminal_failure",
            )
            return "reject", snapshot["reason"], "completed", None
        snapshot.update(
            ai_pending=False,
            ai_review_incomplete=False,
            decision="observe",
            reason=original_reason or "group_rules_ai_unknown",
            rejection_fingerprint=current_fingerprint,
            unchanged_rejection_count=streak,
            review_trigger="ai_incomplete_observation",
        )
        retry = now + timedelta(hours=12 * streak)
        maturity = evidence_maturity(snapshot, now)
        if maturity and maturity < retry:
            retry = maturity
        return "observe", snapshot["reason"], "completed", retry
    if snapshot.get("ai_final") and snapshot.get("ai_decision") == "fail":
        snapshot.update(ai_pending=False, ai_review_incomplete=False, review_trigger="evidence_changed")
        snapshot["decision"] = "reject"
        maturity = evidence_maturity(snapshot, now)
        if maturity:
            snapshot.update(next_evidence_maturity_at=maturity.isoformat(), review_trigger="ordinary_ad_maturity")
        # Only real evidence maturation can schedule a read. The AI decision is
        # terminal and its durable material record prevents another provider call.
        return "reject", original_reason, "completed", maturity
    if original_reason in {"telegram_rpc_cooldown", "telegram_read_budget", "telegram_read_slice", "telegram_rpc_guard_unavailable"}:
        snapshot["technical_failures"] = int(previous.get("technical_failures", 0))
        return "technical_wait", original_reason, "completed", now + timedelta(
            seconds=max(60, int(snapshot.get("retry_after_seconds") or 60))
        )
    maturity = evidence_maturity(snapshot, now)
    verdict, reason, state, retry = _base_review_schedule(snapshot, previous, now)
    if (
        maturity
        and reason == "advertising_evidence_insufficient_after_observation"
        and not qualifies_for_automatic_exit(snapshot)
    ):
        verdict, reason, state, retry = "observe", original_reason, "completed", maturity
        snapshot["decision"], snapshot["reason"] = verdict, reason
    if reason == "group_rules_ai_provider_content_rejected":
        from app.modules.acquisition.qualification_retry import rule_retry_fingerprint
        fingerprint = rule_retry_fingerprint(snapshot)
        streak = (int(previous.get("unchanged_rejection_count", 0)) + 1
                  if previous.get("rejection_fingerprint") == fingerprint else 1)
        snapshot["rejection_fingerprint"] = fingerprint
        snapshot["unchanged_rejection_count"] = streak
        retry = now + timedelta(hours=min(24, 2 ** min(streak, 5)))
    if verdict not in {"observe", "wait"}:
        return verdict, reason, state, retry
    progress = progress_retry(snapshot, now)
    if progress:
        kind, deadline = progress
        errors = set((snapshot.get("identity_errors") or {}).values())
        if kind == "identity_budget_resume" or (
            bool(errors)
            and errors
            <= {
                "ChatAdminRequiredError",
                "permission_probe_budget_exhausted",
            }
        ):
            retry = deadline if kind == "identity_permission_backoff" else min(retry or deadline, deadline)
            snapshot["review_trigger"] = kind
    if maturity:
        snapshot["next_evidence_maturity_at"] = maturity.isoformat()
        if retry is None or maturity < retry:
            retry = maturity
            snapshot["review_trigger"] = "ordinary_ad_maturity"
    if retry and (maturity or progress):
        permission = snapshot.get("permissions") or {}
        barriers = [_date(permission.get(name)) for name in ("temporary_until", "slowmode_until", "newcomer_until")]
        retry = max([retry, *[value for value in barriers if value and value > now]])
        try:
            retry = max(retry, now + timedelta(seconds=max(0, int(snapshot.get("retry_after_seconds") or 0))))
        except (ValueError, TypeError, OverflowError):
            pass
    # Repeating unchanged, exhausted evidence does not improve qualification.
    # A new callback, explicit recheck, or a real maturity/progress deadline can
    # resume it; waiting here never creates a grant or a human approval lane.
    from app.modules.acquisition.review_retention import waits_for_new_evidence

    if waits_for_new_evidence(snapshot, now):
        snapshot["review_trigger"] = "evidence_changed"
        retry = None
    return verdict, reason, state, retry



def text_profile_link_scope_verified(
    ad_result: Any, evidence: list[dict[str, Any]], *, min_confidence: int
) -> bool:
    """A URL-only restriction needs one complete review of the fixed text/profile scope.

    The shared AI prompt explicitly audits a text advertisement with a profile CTA
    and no direct URL. Every matching restriction must be cited and explained by
    the reviewer; an empty explanation cannot authorize that interpretation.
    """
    link_indexes = {
        index
        for index, item in enumerate(evidence)
        if item.get("source") in {"full_about", "pinned_message", "admin_rule"}
        and LINK_BAN.search(str(item.get("text") or ""))
    }
    reviews = ad_result.ai_reviews
    return bool(
        ad_result.ad_allowed is True
        and ad_result.policy_mode in {"soft_ad_trial", "soft_ad_allowed"}
        and len(reviews) >= 1
        and all(
            review.get("mode") == ad_result.policy_mode
            and int(review.get("confidence") or 0) >= max(95, min_confidence)
            and review.get("scope_review_complete") is True
            and review.get("evidence_arguments_present") is True
            and review.get("evidence_arguments_valid") is True
            and not review.get("conflict")
            and not review.get("applicable_prohibition")
            and not review.get("requires_admin_approval")
            and bool(review.get("supporting_evidence_indexes"))
            and link_indexes.issubset(set(review.get("opposing_evidence_indexes") or []))
            and bool(str(review.get("opposing_evidence_resolution") or "").strip())
            and bool(str(review.get("rationale") or "").strip())
            for review in reviews
        )
    )


def topic_only_denial(evidence: list[dict[str, Any]]) -> bool:
    """A missing topic route is a capability hold, never a group-wide rejection.

    This only narrows a negative AI result; it cannot authorize advertising. An
    independently applicable prohibition in an authoritative clause keeps the
    AI denial. Ambiguous combined topic clauses remain for manual review.
    """
    from app.modules.acquisition.automation import AD_RULE_DENY_RE

    rules = [
        str(item.get("text") or "")
        for item in evidence
        if item.get("source") in {"full_about", "pinned_message", "admin_rule"}
    ]
    if not any(TOPIC_CONDITION.search(text) for text in rules):
        return False
    route_scope = re.compile(
        r"话题|話題|广告区|廣告區|其他地方|其它地方|其余地方|topic|thread|elsewhere|outside", re.I
    )
    profile_ban = re.compile(
        r"(?:禁止|禁|不得|不许).{0,8}(?:简介|簡介|个人资料|個人資料|主页|主頁).{0,8}(?:引流|推广|推廣)|(?:no|forbid|ban).{0,12}profile.{0,12}(?:traffic|promot|cta)",
        re.I,
    )
    for text in rules:
        for clause in re.split(r"[。；;！!？?\n，,]", text):
            if NEGATED_BAN.search(clause):
                continue
            if profile_ban.search(clause):
                return False
            if not route_scope.search(clause) and AD_RULE_DENY_RE.search(clause):
                return False
    return True


def ai_evidence_reusable(previous: dict, account: Any, now: datetime, change: dict | None = None) -> bool:
    """Local AI may resume only within the same fresh, unrevoked evidence scope."""
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.evidence_progress import evidence_maturity
    from app.modules.acquisition.qualification_events import same_evidence_event

    collected_at = _date(previous.get("collected_at"))
    maturity = evidence_maturity(previous, now)
    return bool(
        not previous.get("ai_final")
        and not (maturity and maturity <= now + timedelta(seconds=1))
        and (previous.get("ai_pending") or previous.get("ai_review_incomplete"))
        and not previous.get("collection_needs_refresh")
        and same_evidence_event(previous.get("collection_event"), change)
        and previous.get("account_id", getattr(account, "id", None)) == getattr(account, "id", None)
        and collected_at and now-timedelta(hours=24) <= collected_at <= now
        and previous.get("policy_version") == POLICY_VERSION
        and previous.get("profile_fingerprint") == profile_fingerprint(account)
        and previous.get("quality_status") == "qualified"
        and not previous.get("invalidated_at")
        and not previous.get("technical_errors")
        and automatic_exit_reason(previous) is None
    )


async def assess(
    service: Any, account_id: int, group: Group, *,
    row: GroupQualificationAudit | None = None, force_refresh: bool = False
) -> Any:
    from app.core.account.read_work import operation
    scope = f"{row.membership_id}:{row.membership_joined_at.isoformat()}" if row is not None and row.membership_joined_at else ""
    async with operation(account_id, group.id, "review", scope=scope) as work:
        result = await _assess(service, account_id, group, row=row, force_refresh=force_refresh)
        if work.outcome == "interrupted":
            work.outcome = "allowed" if result.passed else (result.reason or "technical_wait")
        return result


async def _assess(
    service: Any, account_id: int, group: Group, *,
    row: GroupQualificationAudit | None = None, force_refresh: bool = False,
) -> Any:
    from app.core.automation_settings import get_ad_capacity_settings
    from app.modules.acquisition.automation import GroupAdRulesAuditResult, JoinedGroupAuditResult

    now = datetime.utcnow()
    previous = _payload(row)
    membership = await service.db.scalar(
        select(GroupAccountMembership).where(
            GroupAccountMembership.account_id == account_id,
            GroupAccountMembership.group_id == group.id,
        )
    )
    config = await policy(service.db, fresh=True)
    account = await service.db.get(TelegramAccount, account_id, populate_existing=True)
    restricted_read = restricted_evidence_read_allowed(account, config, now)
    snapshot: dict[str, Any] = {}
    verdict = "unknown"
    reason = "evidence_unavailable"
    ad_result = GroupAdRulesAuditResult()
    if row is not None and (
        membership is None
        or row.account_id != account_id
        or row.group_id != group.id
        or row.membership_id != membership.id
        or row.membership_joined_at != membership.joined_at
        or row.policy_version != POLICY_VERSION
        or row.content_scope != "text_profile"
    ):
        raise ValueError("qualification_membership_or_scope_changed")
    if manually_protected(config, group, membership):
        snapshot = {
            "protected": True,
            "quality_status": "protected",
            "quality_reason": "manual_protection",
        }
        verdict, reason = "protected", "manual_protection"
    elif await is_owned_group_target(
        service.db, core_group_id=group.id, telegram_group_id=group.group_id
    ):
        snapshot = {
            "protected": True,
            "quality_status": "protected",
            "quality_reason": "owned_group",
        }
        reason = "owned_group_protected"
        verdict = "protected"
    elif not restricted_read and (
        account_block_reason(account, now)
        or service._join_review_account_block_reason(account, now)
    ):
        snapshot = {"quality_status": "technical_wait", "technical_errors": ["account_unavailable"]}
        verdict, reason = "technical_wait", "account_unavailable"
    else:
        from app.modules.acquisition.qualification_ai import profile_fingerprint, review_semantics

        previous = _payload(row)
        previous = previous.get("pending_collection") or previous
        from app.modules.acquisition.qualification_events import pending
        collection_event = await pending(service.db, membership) if membership is not None else None
        from app.modules.acquisition.qualification_events import same_evidence_event
        from app.core.account.idempotency import operation_key
        event_version = hashlib.sha256(
            json.dumps(collection_event or {}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        collection_operation_key = operation_key(
            account_id=account_id,
            operation="qualification_read",
            subject_id=f"{membership.id}:{membership.joined_at}:{event_version}",
            version=POLICY_VERSION,
        )
        collection_reuse = bool(
            not force_refresh
            and row is not None
            and row.decision in {"unknown", "technical_wait", "observe"}
            and previous.get("collection_complete") is True
            and previous.get("collection_operation_key") == collection_operation_key
            and same_evidence_event(previous.get("collection_event"), collection_event)
        )
        if (row is not None and row.decision == "reject" and previous.get("ai_final")
                and previous.get("ai_decision") == "fail" and not force_refresh
                and (row.next_retry_at is None or row.next_retry_at > now)
                and previous.get("profile_fingerprint") == profile_fingerprint(account)
                and not previous.get("collection_needs_refresh")
                and same_evidence_event(previous.get("collection_event"), collection_event)):
            return JoinedGroupAuditResult(passed=False, reason=row.reason)
        from app.modules.acquisition.qualification_continuity import OWN_SOURCE, effective_review, own_proof_valid
        if row is not None and row.decision == "unknown" and not force_refresh:
            approved = await effective_review(service.db, membership.id)
            old = _payload(approved)
            if (approved is not None and approved.id != row.id
                    and approved.decision in {"allowed", "trial"}
                    and authorization_current(approved, now)
                    and scope_matches(approved, group, membership)
                    and old.get("authorization_basis") == OWN_SOURCE
                    and old.get("profile_fingerprint") == profile_fingerprint(account)
                    and await own_proof_valid(service.db, old, membership, group, now)):
                row.decision, row.reason = approved.decision, approved.reason
                row.checked_at, row.expires_at = approved.checked_at, approved.expires_at
                row.evidence_json, row.evidence_hash = approved.evidence_json, approved.evidence_hash
                previous = old
        from app.modules.acquisition.review_batch import current_batch
        if row is not None and row.decision in {"allowed", "trial"} and current_batch.get() is not None:
            await current_batch.get().close()
        from app.modules.acquisition.qualification_renewal import try_light_renewal
        renewed = await try_light_renewal(
            service, account, group, membership, row, previous,
            force_refresh=force_refresh or restricted_read or ai_evidence_reusable(previous, account, now, collection_event),
        )
        if renewed is not None:
            from app.core.account.read_work import current
            work = current(account_id)
            if work is not None:
                work.kind = "renewal"
                work.outcome = row.decision if renewed.passed and row is not None else (renewed.reason or "technical_wait")
                if row is not None:
                    payload = _payload(row)
                    payload["read_cost"] = work.summary()
                    row.evidence_json = json.dumps(payload, ensure_ascii=False, default=str)
            return renewed
        reuse = not force_refresh and not restricted_read and ai_evidence_reusable(previous, account, now, collection_event)
        from app.core.account.read_work import current
        from app.modules.acquisition.review_cost import review_shape
        active_work = current(account_id)
        if active_work is not None:
            active_work.phase, active_work.requested_reads = review_shape(
                previous, row, membership, account, collection_event, now,
                force_refresh=force_refresh or restricted_read)
        from app.modules.acquisition.review_batch import current_batch
        batch = current_batch.get()
        wrapper = None
        verdict = "technical_wait"
        try:
            if reuse or collection_reuse:
                snapshot = {
                    key: value
                    for key, value in previous.items()
                    if key
                    not in {"previous_checks", "qualification_exit_history", "pending_collection"}
                }
                if reuse:
                    snapshot["collection_reused_for_ai"] = True
                else:
                    snapshot["collection_reused"] = True
            else:
                await service.account_pool.add_account_from_db(account)
                wrapper = (await batch.acquire(account_id, restricted=restricted_read) if batch else
                           await service.account_pool.acquire_by_id(
                               account_id, purpose="group_qualification", raise_on_lease_failure=True,
                               allow_restricted=restricted_read))
                if wrapper is None or wrapper.client is None:
                    raise RuntimeError("account_unavailable")
                client = wrapper.client
                entity = await client.get_entity(group.username or group.group_id)
                if not service._telegram_entity_matches_group_id(entity, group.group_id):
                    raise RuntimeError("group_identity_mismatch")
                self_user = await client.get_me()
                self_user_id = int(self_user.id)
                own_ids, identity_fingerprint = await covered_system_ids(
                    service.db, config, account_id=account_id, live_user_id=self_user_id
                )
                own_ids.add(self_user_id)
                progress_valid = (
                    previous.get("account_id") == account_id
                    and previous.get("telegram_group_id") == group.group_id
                    and previous.get("policy_version") == POLICY_VERSION
                    and previous.get("system_identity_fingerprint") == identity_fingerprint
                    and not force_refresh
                )
                from app.modules.acquisition.qualification_events import same_evidence_event
                if not same_evidence_event(previous.get("collection_event"), collection_event):
                    # Message hints remain useful, but a new event invalidates
                    # skipped-page cursors and all assumptions about coverage.
                    previous = {**previous, "collection_progress": {
                        **(previous.get("collection_progress") or {}), "history_cursors": [],
                    }}
                # Leave other memberships a chance in the same read window.
                # Unfinished identities retain message IDs for fresh continuation.
                collector = EvidenceCollector(
                    client, own_user_ids=own_ids, identity_limit=12, priority_identities=8,
                )
                from app.modules.acquisition.callback_evidence import hint_ids
                collector.callback_message_ids = await hint_ids(service.db, membership, now)
                collector.stop_on_permanent_mute = bool(membership.joined_at and membership.joined_at <= now - timedelta(hours=48))
                if progress_valid:
                    collector.previous = previous
                snapshot = await collector.collect(entity, now=now)
                annotate_newcomer_restriction(snapshot, membership.joined_at, now)
                snapshot["system_identity_coverage"] = identity_fingerprint is not None
                snapshot["system_identity_fingerprint"] = identity_fingerprint
                snapshot["system_user_ids"] = sorted(own_ids)
                snapshot["collected_at"] = now.isoformat()
                snapshot["profile_fingerprint"] = profile_fingerprint(account)
                snapshot["collection_event"] = collection_event
                snapshot["collection_operation_key"] = collection_operation_key
                snapshot["collection_complete"] = not bool(
                    (snapshot.get("collection_progress") or {}).get("pending_message_ids")
                ) and not snapshot.get("local_rpc_defer_reason")
            reason, verdict = snapshot["quality_reason"], snapshot["quality_status"]
            if snapshot.get("local_rpc_defer_reason"):
                reason, verdict = snapshot["local_rpc_defer_reason"], "technical_wait"
        except Exception as exc:
            snapshot = {
                "quality_status": "technical_wait",
                "technical_errors": [type(exc).__name__],
            }
            if "Flood" in type(exc).__name__ or "SlowModeWait" in type(exc).__name__:
                snapshot["retry_after_seconds"] = max(1, int(getattr(exc, "seconds", 0) or 60))
            verdict, reason = "technical_wait", type(exc).__name__
            from app.core.account.rpc_governor import RpcDeferred
            if isinstance(exc, RpcDeferred):
                reason = exc.reason
                snapshot["retry_after_seconds"] = exc.retry_after_seconds
                if previous.get("collection_progress") or previous.get("evidence"):
                    snapshot["pending_collection"] = previous
        finally:
            if wrapper is not None:
                if batch is None:
                    await service.account_pool.release(wrapper)
                elif verdict == "technical_wait":
                    await batch.close()
        from app.core.account.read_work import current, release
        cost = current(account_id)
        if cost is not None:
            await release(cost)
            cost.outcome = verdict
            snapshot["read_cost"] = cost.summary()
        # No Telegram account lease is held during semantic review or its queue wait.
        previous_logs = list(
            (
                await service.db.scalars(
                    select(AdDeliveryLog)
                    .where(
                        AdDeliveryLog.account_id == account_id,
                        AdDeliveryLog.group_id == group.id,
                        AdDeliveryLog.created_at >= now - timedelta(days=7),
                    )
                    .order_by(AdDeliveryLog.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
        if snapshot.get("collected_at") and not reuse:
            track_restriction(
                snapshot, previous, now,
                last_sent_at=await service.db.scalar(select(func.max(AdDeliveryLog.sent_at)).where(
                    AdDeliveryLog.account_id == account_id,
                    AdDeliveryLog.group_id == group.id,
                    AdDeliveryLog.status == "success",
                )),
            )
        snapshot["delivery_history"] = [
            {
                "status": item.status,
                "survival_status": item.survival_status,
                "at": item.created_at.isoformat(),
                "message_id": item.telegram_message_id,
            }
            for item in previous_logs
        ]
        if verdict == "qualified" and not restricted_read:
            evidence = snapshot["evidence"]
            rules = [
                item
                for item in evidence
                if item["source"] in {"full_about", "pinned_message", "admin_rule"}
            ]
            # Negation, quotations and exceptions require semantic review, not a regex veto.
            ambiguous = any(
                NEGATED_BAN.search(item["text"])
                or any(
                    word in item["text"]
                    for word in ("但", "除外", "例外", "此前", "旧群规", "曾经", "引用")
                )
                for item in rules
            )
            ad_result = (
                GroupAdRulesAuditResult(evidence=evidence)
                if ambiguous
                else service._evaluate_group_ad_rules(evidence)
            )
            ordinary_proof = trial_proof(
                [item for item in evidence if item.get("topic_id") is None]
            )
            qualified_by_precedent = bool(ordinary_proof)
            limits = dict(await get_ad_capacity_settings(service.db))
            limits["ad_policy_ai_require_second_pass"] = False
            limits["require_evidence_arguments"] = True
            limits["ad_policy_ai_min_confidence"] = max(
                95, int(limits.get("ad_policy_ai_min_confidence", 95))
            )
            snapshot.pop("ai_review_incomplete", None)
            snapshot["unknowns"] = [
                value
                for value in snapshot.get("unknowns", [])
                if value not in AI_REVIEW_INCOMPLETE_REASONS
                and value != "link_or_profile_cta_scope_unconfirmed"
            ]
            if row is not None and not qualified_by_precedent:
                pending = {
                    **snapshot,
                    "ai_pending": True,
                    "policy_version": POLICY_VERSION,
                    "account_id": account_id,
                    "group_id": group.id,
                    "telegram_group_id": group.group_id,
                    "content_scope": "text_profile",
                }
                durable = _payload(row)
                durable["pending_collection"] = pending
                row.evidence_json = json.dumps(durable, ensure_ascii=False, default=str)
                row.state, row.next_retry_at = (
                    "reviewing_ai",
                    datetime.utcnow() + timedelta(seconds=300),
                )
                await service.db.commit()
            # Keep the literal group-rule ban as a separate exit fact. A visible
            # ordinary-member ad may permit sending, but cannot erase C in C&D.
            snapshot["exit_group_rule_ban"] = bool(
                ad_result.ad_allowed is False
                and ad_result.policy_mode == "forbidden"
                and not topic_only_denial(evidence)
            )
            if qualified_by_precedent:
                # A verified ordinary member's retained ad takes precedence over
                # a plain group-rule ad prohibition. Other constraints remain.
                snapshot["precedent_overrides_group_ad_ban"] = bool(ad_result.deny_matches)
                ad_result = GroupAdRulesAuditResult(
                    ad_allowed=True,
                    policy_mode="soft_ad_trial",
                    reason="ordinary_member_ads_verified",
                    evidence=evidence,
                    confidence=100,
                    decision_source="verified_ordinary_member_precedent",
                )
                snapshot["ai_pending"] = False
            else:
                if batch is not None:
                    await batch.close()  # AI latency must not hold the Telegram lease.
                ad_result = await review_semantics(service, snapshot, account, ad_result, limits, previous=previous)
                snapshot["exit_group_rule_ban"] = bool(
                    ad_result.ad_allowed is False
                    and ad_result.policy_mode == "forbidden"
                    and not topic_only_denial(evidence)
                )
            mode = ad_result.policy_mode
            if (
                ad_result.ad_allowed is False
                and mode == "forbidden"
                and topic_only_denial(evidence)
            ):
                verdict, reason = "observe", "topic_route_requires_review"
                snapshot.setdefault("unknowns", []).append(reason)
                snapshot["unsupported_capability"] = "topic_route"
            elif ad_result.ad_allowed is False:
                verdict, reason = "reject", ad_result.reason
            elif not qualified_by_precedent and any(TOPIC_CONDITION.search(item["text"]) for item in rules):
                verdict, reason = "observe", "topic_route_requires_review"
                snapshot.setdefault("unknowns", []).append(reason)
            elif not qualified_by_precedent and any(
                LINK_BAN.search(item["text"]) for item in rules
            ) and not text_profile_link_scope_verified(
                ad_result, evidence, min_confidence=limits["ad_policy_ai_min_confidence"]
            ):
                verdict, reason = "observe", "link_or_profile_cta_scope_unconfirmed"
                snapshot.setdefault("unknowns", []).append(reason)
            elif not snapshot["system_identity_coverage"]:
                verdict, reason = "observe", "system_account_identity_unconfirmed"
                snapshot.setdefault("unknowns", []).append(reason)
            elif ordinary_proof and mode == "soft_ad_trial" and ad_result.ad_allowed is True:
                verdict, reason = "trial", "ordinary_member_ads_verified"
            elif (
                mode in {"soft_ad_allowed", "high_volume_ad_allowed"}
                and ad_result.ad_allowed is True
            ):
                verdict, reason = "allowed", "explicit_permission_verified"
            elif not ordinary_proof and trial_evidence(evidence):
                verdict, reason = "observe", "topic_route_requires_review"
                snapshot.setdefault("unknowns", []).append(reason)
            else:
                verdict, reason = (
                    "observe",
                    ad_result.reason or "advertising_evidence_insufficient",
                )
            if verdict == "observe" and (
                ad_result.reason in AI_REVIEW_INCOMPLETE_REASONS
                or reason == "link_or_profile_cta_scope_unconfirmed"
            ):
                snapshot["ai_review_incomplete"] = True
                snapshot.setdefault("unknowns", []).append(
                    ad_result.reason if ad_result.reason in AI_REVIEW_INCOMPLETE_REASONS else reason
                )
            snapshot["group_level_advertising_ban"] = bool(
                ad_result.ad_allowed is False and mode == "forbidden" and verdict == "reject"
            )
            # Prior unexplained disappearance or account punishment needs fresh operator review.
            from app.modules.acquisition.adaptive_frequency import frequency_context
            if verdict != "reject" and any(
                item.survival_status in {"deleted", "check_failed"} and not frequency_context(item)
                for item in previous_logs
            ):
                verdict, reason = "observe", "prior_delivery_failure_requires_review"
    if restricted_read:
        snapshot["evidence_decision"] = verdict
        snapshot["evidence_reason"] = reason
        snapshot["account_eligibility"] = {
            "can_promote": False,
            "can_auto_leave": False,
            "reason": "account_restricted",
            "evidence_read_allowed": True,
        }
        if verdict != "protected":
            verdict, reason = "technical_wait", "account_restricted_read_only"
    # Reuse real Telegram facts independently of the qualification verdict.
    from app.core.group.collection import record_snapshot
    if snapshot.get("collected_at"):
        await record_snapshot(service.db, group, snapshot, source="qualification")
    # Exit facts take priority over ad permission. Evidence used for an exit
    # must come from this collection, not a reused model-review snapshot.
    snapshot["advertising_audit"] = ad_result.details()
    exit_reason = automatic_exit_reason(snapshot)
    collected_at = _date(snapshot.get("collected_at"))
    if (
        exit_reason is not None
        and not restricted_read
        and collected_at is not None
        and collected_at >= now - timedelta(minutes=5)
        and verdict not in {"protected", "cancelled"}
    ):
        if verdict != "reject":
            reason = exit_reason
        verdict = "reject"
        snapshot.pop("ai_pending", None)
        snapshot.pop("ai_review_incomplete", None)
    if verdict == "technical_wait":
        snapshot["first_blocked_at"] = previous.get("first_blocked_at") or now.isoformat()
        snapshot["last_progress_at"] = (now.isoformat() if (snapshot.get("read_cost") or {}).get("reads")
                                        else previous.get("last_progress_at"))
    else:
        snapshot["last_progress_at"] = now.isoformat()
    # Rate-limit waits start after collection ends, not when a long scan began.
    now = datetime.utcnow()
    snapshot.update(
        {
            "account_id": account_id,
            "group_id": group.id,
            "telegram_group_id": group.group_id,
            "policy_version": POLICY_VERSION,
            "content_scope": "text_profile",
            "exit_policy_logic": "permanent_or_over_3d_mute|A|B|(C&D)",
            "decision": verdict,
            "reason": reason,
            "advertising_audit": ad_result.details(),
        }
    )
    if membership is not None:
        # An unsuccessful recheck is a job result, not a revocation of an
        # existing approval. Preserve its timestamp, deadline and evidence.
        if (not restricted_read and row is not None and row.decision in {"allowed", "trial"}
                and (verdict == "technical_wait" or snapshot.get("ai_review_incomplete"))
                and not exit_reason):
            previous = _payload(row)
            from app.core.account.read_work import current
            work = current(account_id)
            if work is not None:
                work.outcome = "technical_wait"
                snapshot["read_cost"] = work.summary()
            previous["last_review_attempt"] = {"at": now.isoformat(), "reason": reason,
                                               "technical_errors": snapshot.get("technical_errors", []),
                                               "read_cost": snapshot.get("read_cost")}
            if snapshot.get("collection_progress"):
                previous["pending_collection"] = snapshot
            row.evidence_json = json.dumps(previous, ensure_ascii=False, default=str)
            row.state = "completed"
            row.next_retry_at = now + timedelta(seconds=max(60, int(snapshot.get("retry_after_seconds") or 60)))
            return JoinedGroupAuditResult(passed=False, reason=reason)
        if row is None:
            row = GroupQualificationAudit(
                batch_id="automatic-" + uuid4().hex,
                membership_id=membership.id,
                account_id=account_id,
                group_id=group.id,
                policy_version=POLICY_VERSION,
                content_scope="text_profile",
                membership_joined_at=membership.joined_at,
                attempts=1,
            )
            service.db.add(row)
        previous = _payload(row)
        exit_history = list(previous.get("qualification_exit_history", []))
        # Rows are mutable; retain rejection facts before overwriting one, and
        # carry them across new manual batches for the same membership.
        historical_rows = list(
            (
                await service.db.scalars(
                    select(GroupQualificationAudit).where(
                        GroupQualificationAudit.membership_id == membership.id,
                        GroupQualificationAudit.checked_at.is_not(None),
                    )
                )
            ).all()
        )
        for historical in historical_rows:
            old_snapshot = _payload(historical)
            exit_history.extend(old_snapshot.get("qualification_exit_history", []))
            if (
                historical.policy_version in {
                    "pp-ai-qualification-v1", "pp-ai-qualification-v2", POLICY_VERSION
                }
                and historical.decision == "reject"
                and not ai_failure_without_exit_evidence(old_snapshot)
            ):
                exit_history.append(qualification_exit_fact(historical, old_snapshot))
        snapshot["checked_at"] = now.isoformat()
        snapshot["confirmed_group_bans"] = confirmed_group_bans(
            {
                **snapshot,
                "confirmed_group_bans": confirmed_group_bans(previous),
            }
        )
        verdict, reason, row.state, row.next_retry_at = review_schedule(snapshot, previous, now)
        if verdict == "reject" and not snapshot.get("ai_final") and not qualifies_for_automatic_exit(snapshot):
            row.next_retry_at = now + timedelta(hours=2)
        # Retain each previous evidence set (bounded by the maximum review lifecycle).
        history = list(previous.get("previous_checks", []))
        if previous.get("checked_at"):
            history.append(
                {
                    key: value
                    for key, value in previous.items()
                    if key
                    not in {"previous_checks", "qualification_exit_history", "pending_collection"}
                }
            )
        snapshot["previous_checks"] = history[-6:]
        snapshot["checked_at"] = now.isoformat()
        if previous.get("requested_account_ids") is not None:
            snapshot["requested_account_ids"] = previous["requested_account_ids"]
        row.decision, row.reason = verdict, reason
        from app.core.account.read_work import current
        work = current(account_id)
        if work is not None:
            work.outcome = verdict
            snapshot["read_cost"] = work.summary()
        row.evidence_hash = snapshot_hash(snapshot, account_id, row.content_scope)
        row.checked_at = now
        if verdict in {"allowed", "trial"}:
            snapshot["authorization_mode"] = "events"
        row.expires_at = evidence_expiry(_date(snapshot.get("collected_at")) or now, now)
        await service.db.flush()  # New automatic rows need a durable ID in the history.
        if (verdict == "reject" and row.policy_version == POLICY_VERSION
                and not ai_failure_without_exit_evidence(snapshot)):
            exit_history.append(
                qualification_exit_fact(
                    row,
                    snapshot,
                    checked_at=now,
                    evidence_hash=row.evidence_hash,
                )
            )
        snapshot["qualification_exit_history"] = merge_exit_history(exit_history)
        row.evidence_json = json.dumps(snapshot, ensure_ascii=False, default=str)
        started = _date(snapshot.get("observation_started_at"))
        if started is not None:
            membership.review_started_at = started
            membership.review_deadline_at = started + timedelta(hours=24)
        await service.db.flush()
        snapshot["audit_id"] = row.id
    technical = verdict == "technical_wait"
    return JoinedGroupAuditResult(
        passed=verdict in {"allowed", "trial"},
        reason=None
        if verdict in {"allowed", "trial"}
        else ("join_audit_failed" if technical else reason),
        can_send_messages=snapshot.get("permissions", {}).get("can_send_text"),
        permission_reason=reason,
        message_count=snapshot.get("valid_messages", 0),
        unique_senders=sum(v == "ordinary" for v in snapshot.get("roles", {}).values()),
        member_count=snapshot.get("member_count"),
        should_leave=verdict == "reject" and qualifies_for_automatic_exit(snapshot),
        ad_allowed=True
        if verdict in {"allowed", "trial"}
        else False
        if verdict == "reject"
        else None,
        ad_rule_reason=reason,
        ad_rule_details={**ad_result.details(), "qualification": snapshot},
        verification_details={"qualification_decision": verdict},
    )


async def enqueue_membership_review(
    db: Any, member: GroupAccountMembership, *, batch_id: str
) -> GroupQualificationAudit:
    """Queue in the caller's membership/Join transaction; never commit or do I/O."""
    await db.flush()
    row = await db.scalar(
        select(GroupQualificationAudit).where(
            GroupQualificationAudit.batch_id == batch_id,
            GroupQualificationAudit.membership_id == member.id,
        )
    )
    if row is not None:
        if (
            row.account_id != member.account_id
            or row.group_id != member.group_id
            or row.membership_joined_at != member.joined_at
            or row.policy_version != POLICY_VERSION
            or row.content_scope != "text_profile"
        ):
            raise ValueError("qualification_idempotency_scope_mismatch")
        return row
    row = GroupQualificationAudit(
        batch_id=batch_id,
        membership_id=member.id,
        account_id=member.account_id,
        group_id=member.group_id,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        membership_joined_at=member.joined_at,
        state="queued",
        next_retry_at=datetime.utcnow(),
        evidence_json=json.dumps({"trigger": "membership_join_confirmed"}),
    )
    db.add(row)
    member.review_status, member.ad_status = "initial_pending", "blocked"
    member.review_started_at = member.review_deadline_at = member.review_next_at = None
    member.review_attempts = 0
    await db.flush()
    return row


async def queue_reviews(db: Any, account_ids: list[int], batch_id: str) -> list[int]:
    requested = sorted(set(account_ids))
    if not requested:
        raise ValueError("qualification_accounts_required")
    accounts = list(
        (
            await db.scalars(select(TelegramAccount.id).where(TelegramAccount.id.in_(requested)))
        ).all()
    )
    if sorted(accounts) != requested:
        raise ValueError("qualification_account_not_found")
    config = await policy(db)
    if config.get("account_ids") and not set(requested).issubset(_ids(config["account_ids"])):
        raise ValueError("qualification_account_outside_configured_scope")
    batch_key = "qualification.batch." + hashlib.sha256(batch_id.encode()).hexdigest()
    marker = await db.get(SystemSetting, batch_key)
    if marker and json.loads(marker.value)["account_ids"] != requested:
        raise ValueError("idempotency_account_scope_mismatch")
    existing = list(
        (
            await db.scalars(
                select(GroupQualificationAudit)
                .where(GroupQualificationAudit.batch_id == batch_id)
                .order_by(GroupQualificationAudit.id)
            )
        ).all()
    )
    if existing:
        original = _payload(existing[0]).get(
            "requested_account_ids", sorted({item.account_id for item in existing})
        )
        if original != requested:
            raise ValueError("idempotency_account_scope_mismatch")
        return [item.id for item in existing]
    if marker:
        return []  # An empty batch is also an idempotent result.
    db.add(SystemSetting(key=batch_key, value=json.dumps({"account_ids": requested})))
    members = list(
        (
            await db.scalars(
                select(GroupAccountMembership)
                .where(
                    GroupAccountMembership.account_id.in_(requested),
                    GroupAccountMembership.status.in_(
                        ["joined", "pending", "rejected", "leave_failed"]
                    ),
                )
                .order_by(GroupAccountMembership.account_id, GroupAccountMembership.id)
            )
        ).all()
    )
    rows = []
    now = datetime.utcnow()
    for member in members:
        from app.modules.acquisition.qualification_continuity import effective_review
        active_authorization = await effective_review(db, member.id)
        keep_approval = bool(
            active_authorization and active_authorization.decision in {"allowed", "trial"}
            and authorization_current(active_authorization, now)
            and active_authorization.membership_joined_at == member.joined_at
            and member.review_status == "approved" and member.ad_status == "active"
        )
        pending = list(
            (
                await db.scalars(
                    select(GroupQualificationAudit).where(
                        GroupQualificationAudit.membership_id == member.id,
                        GroupQualificationAudit.state.in_(
                            ["queued", "running", "waiting_ai", "reviewing_ai"]
                        ),
                    )
                )
            ).all()
        )
        for old in pending:
            if keep_approval and old.id == active_authorization.id:
                continue
            old.state, old.reason, old.next_retry_at = "cancelled", "superseded_by_recheck", None
        row = GroupQualificationAudit(
            batch_id=batch_id,
            membership_id=member.id,
            account_id=member.account_id,
            group_id=member.group_id,
            policy_version=POLICY_VERSION,
            content_scope="text_profile",
            membership_joined_at=member.joined_at,
            next_retry_at=now,
            evidence_json=json.dumps({"requested_account_ids": requested}),
        )
        db.add(row)
        rows.append(row)
        if not keep_approval:
            member.review_status, member.ad_status = "initial_pending", "blocked"
        member.review_started_at = member.review_deadline_at = member.review_next_at = None
        member.review_attempts = 0
    await db.flush()
    return [item.id for item in rows]


async def ensure_membership_reviews(db: Any, config: dict, *, limit: int = 100, account_id: int | None = None) -> list[int]:
    """Recover missing confirmed-membership work; preserve deliberate terminal holds."""
    from app.modules.acquisition.review_retention import retire_obsolete_reviews

    await retire_obsolete_reviews(db, limit=500)
    recovered = []
    manual_query = select(GroupQualificationAudit).join(
        GroupAccountMembership, GroupAccountMembership.id == GroupQualificationAudit.membership_id
    ).where(
        GroupQualificationAudit.state == "manual_required",
        GroupQualificationAudit.policy_version == POLICY_VERSION,
        GroupAccountMembership.status == "joined",
        GroupQualificationAudit.membership_joined_at.is_not_distinct_from(GroupAccountMembership.joined_at),
        GroupQualificationAudit.id.in_(select(func.max(GroupQualificationAudit.id)).where(
            GroupQualificationAudit.state != "cancelled"
        ).group_by(GroupQualificationAudit.membership_id)),
    ).limit(limit)
    if account_id is not None:
        manual_query = manual_query.where(GroupQualificationAudit.account_id == account_id)
    if config.get("exit_all_accounts") is not True and "account_ids" in config:
        manual_query = manual_query.where(GroupQualificationAudit.account_id.in_(_ids(config["account_ids"])))
    for audit in (await db.scalars(manual_query)).all():
        member = await db.get(GroupAccountMembership, audit.membership_id)
        group = await db.get(Group, audit.group_id)
        if group is None or manually_protected(config, group, member) or await is_owned_group_target(
            db, core_group_id=group.id, telegram_group_id=group.group_id
        ):
            continue
        audit.state, audit.next_retry_at = "completed", datetime.utcnow()
        member.review_status, member.ad_status = "review_2h", "blocked"
        member.review_next_at = audit.next_retry_at
        recovered.append(audit.id)
    current = exists(
        select(GroupQualificationAudit.id).where(
            GroupQualificationAudit.membership_id == GroupAccountMembership.id,
            GroupQualificationAudit.account_id == GroupAccountMembership.account_id,
            GroupQualificationAudit.group_id == GroupAccountMembership.group_id,
            GroupQualificationAudit.membership_joined_at.is_not_distinct_from(
                GroupAccountMembership.joined_at
            ),
            GroupQualificationAudit.policy_version == POLICY_VERSION,
            GroupQualificationAudit.content_scope == "text_profile",
        )
    )
    query = select(GroupAccountMembership).where(
        GroupAccountMembership.status.in_(["joined", "leave_failed"]),
        or_(
            GroupAccountMembership.review_status.is_(None),
            GroupAccountMembership.review_status.not_in(
                ["owned_group_excluded", "left"]
            ),
        ),
        ~current,
    )
    if not (config.get("exit_all_accounts") is True) and "account_ids" in config:
        query = query.where(GroupAccountMembership.account_id.in_(_ids(config["account_ids"])))
    if account_id is not None:
        query = query.where(GroupAccountMembership.account_id == account_id)
    members = (
        await db.scalars(
            query.order_by(GroupAccountMembership.id)
            .limit(max(1, min(limit, 100)))
            .with_for_update(of=GroupAccountMembership, skip_locked=True)
        )
    ).all()
    queued = list(recovered)
    for member in members:
        group = await db.get(Group, member.group_id)
        if group is None:
            continue
        if manually_protected(config, group, member) or await is_owned_group_target(
            db, core_group_id=group.id, telegram_group_id=group.group_id
        ):
            member.review_status, member.ad_status = "owned_group_excluded", "blocked"
            member.review_next_at = None
            continue
        epoch = str(member.joined_at or "unknown")
        batch = (
            "recovery-"
            + hashlib.sha256(f"{POLICY_VERSION}:{member.id}:{epoch}".encode()).hexdigest()
        )
        row = await enqueue_membership_review(db, member, batch_id=batch)
        queued.append(row.id)
    await db.commit()
    return queued


async def priority_account_ids(db: Any, config: dict, now: datetime) -> set[int]:
    """Yield Telegram leases to due maintenance; this never performs an external action."""
    accounts = set(
        (
            await db.scalars(
                select(AdDeliveryLog.account_id)
                .where(
                    AdDeliveryLog.status == "success",
                    AdDeliveryLog.survival_status == "pending",
                    AdDeliveryLog.survival_check_due_at <= now,
                )
                .distinct()
            )
        ).all()
    )
    accounts.update(
        (
            await db.scalars(
                select(AutoJoinAttempt.account_id)
                .where(
                    or_(
                        AutoJoinAttempt.request_state == "outcome_unknown",
                        and_(
                            AutoJoinAttempt.request_state == "sent",
                            AutoJoinAttempt.status == "pending",
                        ),
                    ),
                    AutoJoinAttempt.reconciliation_status.not_in(
                        ["resolved", "confirmed"]
                    ),
                    or_(
                        AutoJoinAttempt.reconciliation_next_at.is_(None),
                        AutoJoinAttempt.reconciliation_next_at <= now,
                    ),
                )
                .distinct()
            )
        ).all()
    )
    from app.core.account.models import AccountOperationConfig
    from app.modules.acquisition.models import AdDeliveryScheduleState
    latest_ad_review = aliased(GroupQualificationAudit)
    fresh_ad_review = exists(select(latest_ad_review.id).where(
        latest_ad_review.membership_id == GroupAccountMembership.id,
        latest_ad_review.membership_joined_at == GroupAccountMembership.joined_at,
        latest_ad_review.policy_version == POLICY_VERSION,
        latest_ad_review.state == "completed",
        latest_ad_review.decision.in_(["allowed", "trial"]),
        latest_ad_review.expires_at > now,
        latest_ad_review.checked_at >= now - EVIDENCE_TTL,
        ~exists(select(GroupQualificationAudit.id).where(
            GroupQualificationAudit.membership_id == latest_ad_review.membership_id,
            GroupQualificationAudit.id > latest_ad_review.id,
            GroupQualificationAudit.state != "cancelled",
        )),
    ))
    due_ads = (await db.execute(select(
        AdDeliveryScheduleState.account_id, GroupAccountMembership.telegram_group_id,
    ).join(
        GroupAccountMembership,
        and_(GroupAccountMembership.group_id == AdDeliveryScheduleState.group_id,
             GroupAccountMembership.account_id == AdDeliveryScheduleState.account_id),
    ).join(AccountOperationConfig, AccountOperationConfig.account_id == AdDeliveryScheduleState.account_id).where(
        AdDeliveryScheduleState.status.in_(["idle", "retry"]),
        AdDeliveryScheduleState.next_due_at <= now,
        fresh_ad_review,
        GroupAccountMembership.status == "joined",
        GroupAccountMembership.review_status == "approved",
        GroupAccountMembership.ad_status == "active",
        AccountOperationConfig.enabled.is_(True), AccountOperationConfig.auto_ads_enabled.is_(True),
    ).distinct())).all()
    # A fresh approval is insufficient: ambiguous identity, a full group quota,
    # or an unresolved prior delivery may leave this schedule due indefinitely.
    # Reuse the send path's cached gates before making all reviews yield to it.
    from types import SimpleNamespace
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.capacity_reads import CapacityReads
    from sqlalchemy.ext.asyncio import AsyncSession

    reads = CapacityReads(db) if isinstance(db, AsyncSession) else db
    ads = AcquisitionAutomationService(reads, account_pool=SimpleNamespace())
    for account_id, target in due_ads:
        if account_id in accounts:
            continue
        context, reason = await ads._qualified_ad_context(account_id, target, now)
        if reason or context is None:
            continue
        if await ads._qualification_delivery_quota_reason(context, target, now) is None:
            accounts.add(account_id)
    # Reserve the scheduler boundary for due joins, without starving review in
    # the four minutes between scans or when a target cannot be found.
    if now.minute % 5 == 0:
        accounts.update((await db.scalars(select(AccountOperationConfig.account_id).where(
            AccountOperationConfig.enabled.is_(True), AccountOperationConfig.auto_join_enabled.is_(True),
            or_(AccountOperationConfig.next_join_after.is_(None), AccountOperationConfig.next_join_after <= now),
        ))).all())
    if config.get("execute_exits"):
        accounts.update((await db.scalars(select(GroupAccountMembership.account_id).where(
            GroupAccountMembership.status.in_(["joined", "leave_failed"]),
            GroupAccountMembership.review_status.in_(["exit_pending", "leave_failed"]),
            GroupAccountMembership.review_next_at <= now,
        ).distinct())).all())
    if config.get("execute_verification"):
        from app.modules.acquisition.qualification_verification import action_count
        candidates = (
            await db.scalars(
                select(GroupQualificationAudit)
                .join(
                    GroupAccountMembership,
                    GroupAccountMembership.id == GroupQualificationAudit.membership_id,
                )
                .where(
                    GroupQualificationAudit.state == "completed",
                    GroupQualificationAudit.policy_version == POLICY_VERSION,
                    GroupQualificationAudit.content_scope == "text_profile",
                    GroupQualificationAudit.decision.in_(["observe", "wait"]),
                    GroupQualificationAudit.checked_at >= now - timedelta(minutes=2),
                    GroupAccountMembership.status.in_(["joined", "pending"]),
                    GroupAccountMembership.joined_at >= now - timedelta(hours=48),
                )
                .limit(100)
            )
        ).all()
        for row in candidates:
            if not verification_account_allowed(config, row.account_id):
                continue
            record = await db.get(
                SystemSetting,
                f"qualification.verification.{row.membership_id}",
                populate_existing=True,
            )
            ledger = json.loads(record.value) if record else {}
            if (
                action_count(ledger) < 3
                and (_date(ledger.get("next_check_at")) or datetime.min) <= now
            ):
                accounts.add(row.account_id)
    return accounts


async def run_reviews(service: Any, *, limit: int = 1, account_id: int | None = None) -> dict[str, Any]:
    from app.modules.acquisition.review_batch import ReviewBatch, current_batch
    batch = ReviewBatch(getattr(service, "account_pool", None))
    token = current_batch.set(batch)
    try:
        return await _run_reviews(service, limit=limit, account_id=account_id)
    finally:
        current_batch.reset(token)
        await batch.close()


async def _run_reviews(service: Any, *, limit: int = 1, account_id: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"processed": 0, "results": []}
    config = await policy(service.db, fresh=True)
    if not config.get("enabled"):
        return result
    if config.get("execute_reviews", True) is False:
        return {**result, "paused": True, "reason": "qualification_reviews_paused"}
    if config.get("automatic_identity_registration"):
        from app.modules.acquisition.qualification_system_identity import ensure_system_identities
        await ensure_system_identities(service)
        config = await policy(service.db, fresh=True)
    await ensure_membership_reviews(service.db, config, account_id=account_id)
    # A stored local-budget deadline is a forecast, not a platform cooldown.
    # Reconsider it only after a fresh check proves a useful read slice is now
    # available (for example after a reservation is released or idle loans grow).
    reconsider_read_waits = False
    reconsider_budget_wait_ids: set[int] = set()
    local_read_wait = and_(
        GroupQualificationAudit.state.in_(["queued", "completed"]),
        GroupQualificationAudit.decision.in_(["unknown", "technical_wait"]),
        GroupQualificationAudit.reason == "telegram_read_budget",
    )
    if account_id is not None:
        # ``reason`` predates the durable wait ledger and only covers a subset
        # of old rows.  The ledger is the source of truth for all rows whose
        # future timer was produced by a local read-budget admission failure.
        from app.modules.acquisition.review_waits import budget_wait_ids
        reconsider_budget_wait_ids = await budget_wait_ids(
            service.db, account_id, now=datetime.utcnow(), limit=500
        )
    if account_id is not None and (
        reconsider_budget_wait_ids
        or await service.db.scalar(select(GroupQualificationAudit.id).where(
            local_read_wait, GroupQualificationAudit.account_id == account_id,
            GroupQualificationAudit.next_retry_at > datetime.utcnow(),
        ).limit(1))
    ):
        from app.core.account.rpc_governor import RpcDeferred, check_read_ready
        try:
            # This is only a wake-up probe.  The selected row performs its
            # own exact read-cost check below, so a small available slice can
            # wake a queue without falsely authorizing a larger collection.
            await check_read_ready(service.db, account_id, purpose="group_qualification", minimum_reads=1)
            reconsider_read_waits = True
        except RpcDeferred:
            pass
    deferred_review_ids: set[int] = set()
    reconsider_renewal_ids = []
    if account_id is not None:
        from app.core.account.rpc_governor import RpcDeferred, check_read_ready
        checked_at = datetime.utcnow()
        candidates = (await service.db.scalars(select(GroupQualificationAudit).where(
            GroupQualificationAudit.account_id == account_id,
            GroupQualificationAudit.state.in_(['queued', 'completed']),
            GroupQualificationAudit.decision.in_(['allowed', 'trial']),
            GroupQualificationAudit.next_retry_at > checked_at,
        ).limit(300))).all()
        try:
            ready = (await check_read_ready(service.db, account_id, now=checked_at,
                                           purpose='ad_qualification_refresh', minimum_reads=16)
                     if candidates else {})
            ready = ready if isinstance(ready, dict) else {}
            boundaries = [checked_at + timedelta(seconds=ready['usage'][w]['ttl_seconds'])
                          for w in ('hour', 'day') if ready.get('usage', {}).get(w, {}).get('ttl_seconds', -1) > 0]
            for candidate in candidates:
                saved = _payload(candidate)
                local_wait = saved.get('renewal_wait_reason') == 'telegram_read_budget' or (
                    saved.get('read_cost') or {}).get('outcome') == 'telegram_read_budget'
                # Old readers did not label readiness deferrals. Only their
                # precise current budget boundary is recognized during upgrade.
                legacy_boundary = not saved.get('renewal_wait_reason') and any(
                    0 <= (candidate.next_retry_at - end).total_seconds() <= 4 for end in boundaries)
                if local_wait or legacy_boundary:
                    reconsider_renewal_ids.append(candidate.id)
        except RpcDeferred:
            pass
    claims = 0
    # Readiness deferrals are local scheduling work, not execution quanta.
    # Bound the scan, while letting independent ad renewals/local AI pass rows
    # whose read lane is exhausted. Deferred deadlines persist across passes.
    for _ in range(max(0, limit) * 4):
        if claims >= limit:
            break
        now = datetime.utcnow()
        other = aliased(GroupQualificationAudit)
        busy_account = exists(
            select(other.id).where(
                other.account_id == GroupQualificationAudit.account_id,
                other.id != GroupQualificationAudit.id,
                other.state == "running",
                other.next_retry_at > now,
            )
        )
        due = or_(
            and_(
                GroupQualificationAudit.state == "queued",
                or_(
                    GroupQualificationAudit.next_retry_at.is_(None),
                    GroupQualificationAudit.next_retry_at <= now,
                ),
            ),
            # Recover a worker claim that expired before its durable
            # writeback.  The subsequent path reuses the row's evidence and
            # does not treat a terminal decision as a fresh review.
            and_(
                GroupQualificationAudit.state == "running",
                GroupQualificationAudit.next_retry_at.is_not(None),
                GroupQualificationAudit.next_retry_at <= now,
            ),
            # The bounded waiting-AI lane is due work once its timer expires.
            # Fresh reusable evidence resumes without a read; anything else
            # still has to pass the read gate below.
            and_(
                GroupQualificationAudit.state == "waiting_ai",
                GroupQualificationAudit.next_retry_at.is_not(None),
                GroupQualificationAudit.next_retry_at <= now,
            ),
            # Technical/budget waits are explicitly retryable.  AI and
            # completed semantic decisions are intentionally absent here;
            # they are resumed only through a new evidence/renewal event.
            and_(
                GroupQualificationAudit.state == "completed",
                GroupQualificationAudit.decision.in_(["unknown", "technical_wait"]),
                GroupQualificationAudit.next_retry_at.is_not(None),
                GroupQualificationAudit.next_retry_at <= now,
            ),
            # Send-time invalidation (qualification_actions.invalidate) and
            # unresolved AI verdicts park rows as completed observe/reject
            # decisions that still carry a retry timer.  No other path resumes
            # them: without this branch such a row stays due-past forever while
            # execution_admission keeps counting its membership as a pending
            # first review, which permanently exhausts the join admission
            # capacity (join_wait_execution_capacity deadlock).
            and_(
                GroupQualificationAudit.state == "completed",
                GroupQualificationAudit.decision.in_(["observe", "reject"]),
                GroupQualificationAudit.next_retry_at.is_not(None),
                GroupQualificationAudit.next_retry_at <= now,
            ),
            # Expired approved/trial evidence is a renewal operation.  It is
            # due work too; excluding it made the ad lane look healthy while
            # silently leaving renewals behind older unresolved reviews.
            and_(
                GroupQualificationAudit.state == "completed",
                GroupQualificationAudit.decision.in_(["allowed", "trial"]),
                GroupQualificationAudit.next_retry_at.is_not(None),
                GroupQualificationAudit.next_retry_at <= now,
            ),
        )
        if reconsider_read_waits:
            due = or_(due, local_read_wait, GroupQualificationAudit.id.in_(reconsider_budget_wait_ids))
        if reconsider_renewal_ids:
            due = or_(due, GroupQualificationAudit.id.in_(reconsider_renewal_ids))
        from app.core.account.rpc_governor import PREFIX, parse_date
        from app.core.settings_models import SystemSetting
        states = (await service.db.scalars(select(SystemSetting).where(SystemSetting.key.like(PREFIX + "%")))).all()
        cooling = [int(item.key[len(PREFIX):]) for item in states
                   if (parse_date(json.loads(item.value).get("pause_until")) or datetime.min) > now]
        exiting_membership = exists(select(GroupAccountMembership.id).where(
            GroupAccountMembership.id == GroupQualificationAudit.membership_id,
            GroupAccountMembership.review_status.in_(["exit_pending", "leave_failed", "membership_reconciliation"]),
        ))
        # Exit workers collect fresh evidence themselves. Ordinary reviews must
        # not consume their due rows and clear the membership's exit state.
        query = select(GroupQualificationAudit).where(due, ~busy_account, ~exiting_membership)
        if account_id is not None:
            query = query.where(GroupQualificationAudit.account_id == account_id)
        if deferred_review_ids:
            query = query.where(GroupQualificationAudit.id.not_in(deferred_review_ids))
        if cooling:
            query = query.where(GroupQualificationAudit.account_id.not_in(cooling))
        # Suspended reads remain due; do not consume attempts or model calls.
        paused_accounts = select(TelegramAccount.id).where(or_(
            TelegramAccount.risk_pause_until > now,
            TelegramAccount.risk_level.in_(["quarantined", "frozen"]),
            TelegramAccount.is_active.is_(False),
        ))
        query = query.where(GroupQualificationAudit.account_id.not_in(paused_accounts))
        rollout_paused = [int(key) for key, value in (config.get("rollout_accounts") or {}).items()
                          if value.get("phase") == "paused"]
        if rollout_paused:
            query = query.where(GroupQualificationAudit.account_id.not_in(rollout_paused))
        priority = await priority_account_ids(service.db, config, now) if account_id is None else set()
        # Due work is a scheduling preference, never an indefinite account veto.
        # The operation lease still gives executable ads precedence at the RPC boundary.
        priority_rank = case((GroupQualificationAudit.account_id.in_(priority), 1), else_=0)
        if not (config.get("exit_all_accounts") is True) and config.get("account_ids"):
            query = query.where(GroupQualificationAudit.account_id.in_(_ids(config["account_ids"])))
        joined = exists(select(GroupAccountMembership.id).where(
            GroupAccountMembership.id == GroupQualificationAudit.membership_id,
            GroupAccountMembership.status == "joined",
            GroupAccountMembership.left_at.is_(None),
        ))
        successful = exists(select(AdDeliveryLog.id).where(
            AdDeliveryLog.account_id == GroupQualificationAudit.account_id,
            AdDeliveryLog.group_id == GroupQualificationAudit.group_id,
            AdDeliveryLog.status == "success",
            AdDeliveryLog.telegram_message_id.is_not(None),
            AdDeliveryLog.created_at >= now - timedelta(days=7),
        ))
        renewal_rank = case((and_(joined, or_(
            GroupQualificationAudit.decision.in_(["allowed", "trial"]), successful,
        )), 0), (or_(and_(
            GroupQualificationAudit.checked_at.is_(None),
            GroupQualificationAudit.decision == "unknown",
            # A never-collected row that has been parked for a long period is
            # stale maintenance work, not fresh join evidence.  Keep it in
            # the normal retry family so it cannot displace an active renewal.
            or_(
                GroupQualificationAudit.next_retry_at.is_(None),
                GroupQualificationAudit.next_retry_at >= now - timedelta(hours=6),
            ),
        ), and_(GroupQualificationAudit.batch_id.like("join-attempt:%"),
                 GroupQualificationAudit.decision == "technical_wait")), 2), else_=1)
        from app.modules.acquisition import qualification_queue
        order = (priority_rank, GroupQualificationAudit.next_retry_at, renewal_rank, GroupQualificationAudit.id)
        if account_id is not None:
            family_order = qualification_queue.ordering(
                await qualification_queue.cursor(service.db, account_id), renewal_rank
            )
            # A row parked for many hours is stale maintenance.  Keep it out
            # of the first cursor turn so a currently qualified membership can
            # renew before that historical item, while recent initial work
            # continues to follow the persistent fair-turn cursor.
            stale_rank = case(
                (
                    and_(
                        GroupQualificationAudit.checked_at.is_(None),
                        GroupQualificationAudit.decision == "unknown",
                        GroupQualificationAudit.next_retry_at.is_not(None),
                        GroupQualificationAudit.next_retry_at < now - timedelta(hours=6),
                    ),
                    1,
                ),
                else_=0,
            )
            order = (stale_rank, *family_order)
        selected = (await service.db.execute(
            query.add_columns(renewal_rank).order_by(*order)
            .with_for_update(skip_locked=True)
            .limit(1)
        )).first()
        if selected is not None:
            # If the only competing initial item is an old never-collected
            # row, prefer an expired approval renewal.  This is a local DB
            # ordering decision; it does not trigger an extra Telegram read.
            selected_account_id = selected[0].account_id
            stale_initial = (
                selected[0].checked_at is None
                and selected[0].decision == "unknown"
                and selected[0].next_retry_at is not None
                and selected[0].next_retry_at < now - timedelta(hours=6)
            )
            if stale_initial:
                fresh_initial = await service.db.scalar(select(GroupQualificationAudit.id).where(
                    GroupQualificationAudit.account_id == selected_account_id,
                    GroupQualificationAudit.state == "queued",
                    GroupQualificationAudit.decision == "unknown",
                    GroupQualificationAudit.checked_at.is_(None),
                    GroupQualificationAudit.next_retry_at.is_not(None),
                    GroupQualificationAudit.next_retry_at >= now - timedelta(hours=6),
                ).limit(1))
                if fresh_initial is None:
                    renewal = (await service.db.execute(
                        query.where(
                            GroupQualificationAudit.account_id == selected_account_id,
                            GroupQualificationAudit.state == "completed",
                            GroupQualificationAudit.decision.in_(["allowed", "trial"]),
                            GroupQualificationAudit.next_retry_at.is_not(None),
                            GroupQualificationAudit.next_retry_at <= now,
                        ).add_columns(renewal_rank).order_by(
                            GroupQualificationAudit.next_retry_at,
                            GroupQualificationAudit.id,
                        ).with_for_update(skip_locked=True).limit(1)
                    )).first()
                    if renewal is not None:
                        selected = renewal
        if selected is None:
            break
        row, review_family = selected
        membership = await service.db.get(
            GroupAccountMembership, row.membership_id, populate_existing=True
        )
        group = await service.db.get(Group, row.group_id)
        latest = await latest_review(service.db, row.membership_id, include_pending=True)
        if (
            membership is None
            or group is None
            or latest is None
            or latest.id != row.id
            or membership.joined_at != row.membership_joined_at
            or membership.account_id != row.account_id
            or membership.group_id != row.group_id
            or membership.status not in {"joined", "pending", "rejected", "leave_failed"}
            or row.policy_version != POLICY_VERSION
            or row.content_scope != "text_profile"
        ):
            if (latest is not None and latest.id != row.id
                    and row.decision in {"allowed", "trial"}
                    and membership is not None and scope_matches(row, group, membership)):
                row.state, row.next_retry_at = "completed", None
                await service.db.commit()
                continue
            row.state, row.reason, row.next_retry_at = (
                "cancelled",
                "membership_or_scope_changed",
                None,
            )
            await service.db.commit()
            continue
        # Serialize the claim across different memberships of the same account.
        # The row lock lasts through the durable running-state commit.
        action = stale_claim_action(row, now=now)
        if action == "graduate_trial":
            # A continuity-restored trial has been cheaply re-verified enough
            # times; graduate to a long renewal horizon well inside the 30-day
            # evidence TTL. The send-time rules revalidation remains the
            # real-time guard.
            row.state, row.next_retry_at = "completed", now + timedelta(days=14)
            membership.review_next_at = row.next_retry_at
            await service.db.commit()
            deferred_review_ids.add(row.id)
            continue
        if action == "finalize_reject":
            # A reject that keeps being re-queued without a terminal write is
            # already a concluded verdict; finalize it like the standard
            # reject writeback.  The ai_final marker keeps waiting_for_evidence
            # exempting the row from execution capacity, otherwise a terminal
            # non-AI reject would count as pending review forever.
            saved = _payload(row)
            saved["ai_final"] = True
            saved["ai_decision"] = "fail"
            row.evidence_json = json.dumps(saved, ensure_ascii=False, default=str)
            row.state, row.next_retry_at = "completed", None
            membership.review_next_at = None
            await service.db.commit()
            deferred_review_ids.add(row.id)
            continue
        if action == "escalate_full_review":
            # The retired-precedent light re-check never converges on its own
            # (the precedent message is gone); escalate to a full evidence
            # re-collection.  Strip the prior snapshot's freshness markers so
            # every light-reuse shortcut rejects it, keeping the old evidence
            # only as previous_checks history.
            prior = _payload(row)
            history = prior.get("previous_checks") or []
            history.append({k: v for k, v in prior.items() if k != "previous_checks"})
            row.state, row.decision, row.checked_at = "queued", "unknown", None
            row.reason = "qualification_escalated_full_review"
            row.evidence_json = json.dumps(
                {"decision": "unknown", "reason": "qualification_escalated_full_review",
                 "previous_checks": history},
                ensure_ascii=False, default=str)
            row.next_retry_at = now
            await service.db.commit()
            deferred_review_ids.add(row.id)
            continue
        account_claim = await service.db.scalar(
            select(TelegramAccount.id)
            .where(TelegramAccount.id == row.account_id)
            .with_for_update(skip_locked=True)
        )
        if account_claim is None:
            # Listener/event and pool-sync transactions hold the account row
            # for seconds at a time.  Breaking silently here starves the whole
            # review lane while those transactions dominate the row and leaves
            # every due timer frozen with no scheduling signal.  Defer this row
            # briefly instead so a later quantum claims it once the lock frees.
            if row.state in {"running", "reviewing_ai"}:
                row.state = "completed" if row.decision in {"allowed", "trial"} else "queued"
            row.next_retry_at = now + timedelta(seconds=120)
            from app.modules.acquisition.review_waits import record
            await record(service.db, row.id, "account_row_lock_busy", 0, row.next_retry_at, now)
            deferred_review_ids.add(row.id)
            await service.db.commit()
            continue
        concurrent = await service.db.scalar(
            select(GroupQualificationAudit.id)
            .where(
                GroupQualificationAudit.account_id == row.account_id,
                GroupQualificationAudit.id != row.id,
                GroupQualificationAudit.state == "running",
                GroupQualificationAudit.next_retry_at > now,
            )
            .limit(1)
        )
        if concurrent is not None:
            await service.db.rollback()
            break
        from app.core.account.read_schedule import read_wait
        account = await service.db.get(TelegramAccount, row.account_id, populate_existing=True)
        previous = _payload(row)
        previous = previous.get("pending_collection") or previous
        from app.modules.acquisition.qualification_events import pending, acknowledge, same_evidence_event
        change = await pending(service.db, membership)
        from app.modules.acquisition.review_inventory import waiting_for_evidence
        if waiting_for_evidence(row, now) and same_evidence_event(previous.get("collection_event"), change):
            # Retire only an obsolete timer; new events/manual queues, read
            # failures and real evidence maturity continue through the claim.
            previous["review_trigger"] = "evidence_changed"
            row.evidence_json = json.dumps(previous, ensure_ascii=False, default=str)
            row.next_retry_at = None
            membership.review_next_at = None
            result["waiting_evidence"] = result.get("waiting_evidence", 0) + 1
            await service.db.commit()
            continue
        from app.modules.acquisition.qualification_renewal import REFRESH_PURPOSE, eligible
        light_renewal = (
            not restricted_evidence_read_allowed(account, config, now)
            and await eligible(service.db, account, group, membership, row, previous)
        )
        local_grant = light_renewal and change is None
        local_ai = (not restricted_evidence_read_allowed(account, config, now)
                    and ai_evidence_reusable(previous, account, now, change))
        from app.core.account.read_work import review_required
        from app.modules.acquisition.review_cost import review_shape
        phase, fallback = review_shape(previous, row, membership, account, change, now,
                                      force_refresh=restricted_evidence_read_allowed(account, config, now))
        work_scope = f"{membership.id}:{membership.joined_at.isoformat()}" if membership.joined_at else ""
        required = 16 if light_renewal else await review_required(row.account_id, group.id, work_scope,
                                                                 phase=phase, fallback=fallback)
        pause = None if local_ai or local_grant else await read_wait(
            service.db, row.account_id, now,
            purpose=REFRESH_PURPOSE if light_renewal else "group_qualification",
            requires_bootstrap=True,
            minimum_reads=required,
            work_group_id=group.id,
            work_scope=work_scope,
        )
        if pause:
            # Defer only this network-dependent row. Another membership may
            # already have complete evidence and require only the AI decision.
            # An expired claim is no longer running. Keeping that state while
            # extending its deadline would lock every other review on the account.
            if row.state in {"running", "reviewing_ai"}:
                row.state = "completed" if row.decision in {"allowed", "trial"} else "queued"
            row.next_retry_at = pause[1]
            from app.modules.acquisition.review_waits import record
            await record(service.db, row.id, pause[0], required, pause[1], now)
            if pause[0] == "telegram_read_budget" and row.decision in {"unknown", "technical_wait"}:
                row.reason = pause[0]
            if light_renewal:
                saved = _payload(row)
                saved['renewal_wait_reason'] = pause[0]
                row.evidence_json = json.dumps(saved, ensure_ascii=False)
            deferred_review_ids.add(row.id)
            await service.db.commit()
            continue
        # Account quanta have a six-minute hard deadline. Their abandoned claim
        # becomes eligible after eight minutes, without delaying unrelated accounts.
        claim_seconds = 480 if account_id is not None else 1200
        from app.modules.acquisition.review_waits import clear
        await clear(service.db, row.id)
        row.state, row.next_retry_at = "running", now + timedelta(seconds=claim_seconds)
        row.attempts = (row.attempts or 0) + 1
        claimed_id, claimed_attempt = row.id, row.attempts
        await qualification_queue.claimed(service.db, row.account_id, row.id, review_family)
        await service.db.commit()
        claims += 1
        try:
            audit = await assess(service, row.account_id, group, row=row)
        except Exception as exc:
            # One malformed peer/model response must not leave the entire
            # account locked in "running" until its abandoned-claim deadline.
            await service.db.rollback()
            current = await service.db.get(GroupQualificationAudit, claimed_id, with_for_update=True)
            if (current is not None and current.attempts == claimed_attempt
                    and current.state in {"running", "reviewing_ai"}):
                current.state = "completed" if current.decision in {"allowed", "trial"} else "queued"
                delay = min(900, 30 * 2 ** min(claimed_attempt, 5))
                current.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
                await service.db.commit()
            deferred_review_ids.add(claimed_id)
            result["failed"] = result.get("failed", 0) + 1
            result["results"].append({"audit_id": claimed_id, "reason": "review_exception:" + type(exc).__name__})
            if type(exc).__name__ == "SoftTimeLimitExceeded":
                raise
            continue
        # A concurrent requeue or leave cannot be overwritten by a completed old read.
        # Reacquire the audit row before the membership row so every writeback
        # follows the dispatcher order and uses a fresh ORM instance.
        await service.db.flush()
        row = await service.db.scalar(select(GroupQualificationAudit).where(
            GroupQualificationAudit.id == row.id,
        ).with_for_update(of=GroupQualificationAudit).execution_options(populate_existing=True))
        if row is None:
            await service.db.rollback()
            continue
        membership = await service.db.scalar(select(GroupAccountMembership).where(
            GroupAccountMembership.id == row.membership_id,
        ).with_for_update(of=GroupAccountMembership).execution_options(populate_existing=True))
        if membership is None:
            row.state, row.reason, row.next_retry_at = (
                "cancelled", "membership_or_scope_changed", None
            )
            await service.db.commit()
            continue
        latest = await latest_review(service.db, membership.id, include_pending=True)
        if (
            latest is None
            or latest.id != row.id
            or membership.joined_at != row.membership_joined_at
            or membership.account_id != row.account_id
            or membership.group_id != row.group_id
            or membership.status not in {"joined", "pending", "rejected", "leave_failed"}
        ):
            if (latest is not None and latest.id != row.id
                    and row.decision in {"allowed", "trial"}
                    and scope_matches(row, group, membership)):
                row.state, row.next_retry_at = "completed", None
                await service.db.commit()
                continue
            row.state, row.reason, row.next_retry_at = (
                "cancelled",
                "superseded_during_collection",
                None,
            )
            await service.db.commit()
            continue
        if getattr(audit, "passed", False):
            if (membership.ad_status == "paused" or membership.review_status in {
                "manual_required", "exit_pending", "owned_group_excluded",
            }):
                row.state, row.next_retry_at = "completed", None
                await service.db.commit()
                continue
            if not await acknowledge(service.db, membership, change):
                row.state, row.next_retry_at = "queued", datetime.utcnow()
                membership.review_next_at = row.next_retry_at
                await service.db.commit()
                continue
            from app.modules.acquisition.qualification_continuity import restore_own_authorization
            await restore_own_authorization(service.db, membership.id, apply=True)
        decision = row.decision
        evidence = _payload(row)
        from app.modules.acquisition.qualification_continuity import effective_review, review_only
        if review_only(row):
            approved = await effective_review(service.db, membership.id)
            if (approved is not None and approved.id != row.id
                    and approved.decision in {"allowed", "trial"}
                    and authorization_current(approved, now)
                    and scope_matches(approved, group, membership)
                    and membership.ad_status not in {"paused", "blocked"}):
                membership.review_status = "approved"
                membership.review_next_at = row.next_retry_at
                await service.db.commit()
                result["processed"] += 1
                result["results"].append({"audit_id": row.id, "decision": decision,
                                          "reason": row.reason, "approval_retained": approved.id})
                continue
        if decision in {"allowed", "trial"} and not audit.passed:
            # A deferred lightweight renewal retains its previous approval and
            # ad profile. Only the next review deadline changes.
            membership.review_next_at = row.next_retry_at
            await service.db.commit()
            result["processed"] += 1
            result["results"].append({"account_id": row.account_id, "group_id": row.group_id,
                                      "decision": decision, "reason": audit.reason, "deferred": True})
            continue
        membership.review_started_at = _date(evidence.get("observation_started_at"))
        membership.review_deadline_at = (
            membership.review_started_at + timedelta(hours=24)
            if membership.review_started_at
            else None
        )
        membership.review_attempts = row.attempts
        membership.review_next_at = row.next_retry_at
        membership.last_checked_at = row.checked_at
        permissions = evidence.get("permissions") or {}
        if (
            decision == "technical_wait"
            and row.reason == "membership_not_participant"
            and permissions.get("member") is False
            and permissions.get("membership_evidence")
            in {"telegram_permissions", "telegram_permissions_not_participant"}
        ):
            membership.status = "left"
            membership.left_at = row.checked_at
            membership.review_status, membership.ad_status = "left", "blocked"
            membership.review_next_at = None
            row.state, row.next_retry_at = "completed", None
        elif row.state == "manual_required":
            membership.review_status, membership.ad_status = "manual_required", "blocked"
        elif decision == "protected":
            membership.review_status, membership.ad_status = "owned_group_excluded", "blocked"
        elif decision == "reject" and qualifies_for_automatic_exit(evidence):
            membership.ad_status, membership.review_status = "blocked", "exit_pending"
            membership.leave_requested_at = membership.leave_requested_at or now
            membership.review_next_at = now
        elif decision in {"allowed", "trial"} and membership.status == "joined":
            membership.review_status, membership.ad_status = "approved", "active"
            await service._sync_group_ad_policy_from_audit(group, audit)
        elif decision == "reject":
            membership.ad_status, membership.review_status = "blocked", "review_2h"
            membership.review_next_at = (row.next_retry_at if evidence.get("ai_final") else row.next_retry_at or now + timedelta(hours=2))
        else:
            membership.ad_status = (
                "warming" if membership.status in {"joined", "pending"} else "blocked"
            )
            membership.review_status = (
                "initial_pending" if row.batch_id.startswith("join-attempt:")
                and decision == "technical_wait" else "review_2h"
            )
        await service.db.commit()
        result["processed"] += 1
        result["results"].append(
            {"audit_id": row.id, "decision": row.decision, "reason": row.reason}
        )
    return result


async def authorize_leave(
    db: Any, account_id: int, group: Group, membership: GroupAccountMembership, audit: Any = None
) -> tuple[bool, str]:
    """Database half of exit authorization; caller must recheck live Telegram protection."""
    config = await policy(db, fresh=True)
    if not config.get("enabled") or not config.get("execute_exits"):
        return False, "qualification_exits_paused"
    try:
        membership_ids, reasons = exit_scope_filters(config)
    except ValueError as exc:
        return False, str(exc)
    if membership_ids is not None and membership.id not in membership_ids:
        return False, "qualification_membership_outside_exit_scope"
    if reasons is not None and not reasons:
        return False, "qualification_exit_reason_outside_configured_scope"
    if (
        config.get("exit_all_accounts") is not True
        and config.get("account_ids")
        and account_id not in _ids(config["account_ids"])
    ):
        return False, "qualification_account_outside_configured_scope"
    row, current_group, current_member = await current_authorization(db, account_id, group.group_id)
    if row is not None:
        await db.refresh(row)
    if current_group is None or current_member is None or current_member.id != membership.id:
        return False, "qualification_membership_changed"
    if manually_protected(config, current_group, current_member) or await is_owned_group_target(
        db, core_group_id=current_group.id, telegram_group_id=current_group.group_id
    ):
        return False, "qualification_protected"
    now = datetime.utcnow()
    if account_block_reason(await db.get(TelegramAccount, account_id, populate_existing=True), now):
        return False, "qualification_account_unavailable"
    if (
        current_member.status not in {"joined", "rejected", "leave_failed"}
        or current_member.review_status != "exit_pending"
    ):
        return False, "qualification_exit_not_pending"
    from app.modules.acquisition.adaptive_frequency import FrequencyService
    adaptive_reason = await FrequencyService(db).exit_reason(account_id, current_group, current_member)
    if adaptive_reason:
        if reasons is not None and adaptive_reason not in reasons:
            return False, "qualification_exit_reason_outside_configured_scope"
        return True, adaptive_reason
    if (
        row is None
        or row.account_id != account_id
        or row.group_id != current_group.id
        or row.membership_joined_at != current_member.joined_at
        or row.policy_version != POLICY_VERSION
        or row.content_scope != "text_profile"
        or row.state != "completed"
        or row.decision != "reject"
        or row.checked_at is None
        or row.checked_at < now - timedelta(minutes=5)
        or row.expires_at is None
        or row.expires_at <= now
    ):
        return False, "qualification_exit_review_required"
    if reasons is not None and row.reason not in reasons:
        return False, "qualification_exit_reason_outside_configured_scope"
    if not scope_matches(row, current_group, current_member):
        return False, "qualification_exit_scope_changed"
    snapshot = _payload(row)
    if not qualifies_for_automatic_exit(snapshot):
        return False, "qualification_exit_conditions_not_met"
    collected_at = _date(snapshot.get("collected_at"))
    if collected_at is None or collected_at < now - timedelta(minutes=5):
        return False, "qualification_exit_evidence_stale"
    if audit is not None:
        requested_id = getattr(audit, "id", None)
        if requested_id is not None and requested_id != row.id:
            return False, "qualification_exit_review_superseded"
    fact = automatic_exit_reason(snapshot)
    if fact in {"account_permanent_send_restriction", "account_long_send_restriction"}:
        return True, fact  # The final live guard must see the actual mutable exit condition.
    return True, row.reason or "qualification_rejected"


async def send_gate(
    db: Any,
    account_id: int,
    target: int | str,
    content: str,
    media_url: str | None,
    *,
    reservation_token: str | None = None,
) -> str | None:
    """Fail closed at the actual send boundary; legacy shared profiles cannot authorize."""
    config = await policy(db, fresh=True)
    if not config.get("enabled"):
        return "qualification_disabled"
    execution = await db.get(
        SystemSetting, "automation.ad_delivery_execution", populate_existing=True
    )
    try:
        execution_enabled = (
            json.loads(execution.value).get("enabled") is True if execution else False
        )
    except (TypeError, ValueError, AttributeError):
        execution_enabled = False
    if not execution_enabled:
        return "ad_delivery_paused"
    rollout = (config.get("rollout_accounts") or {}).get(str(account_id), {})
    phase = rollout.get("phase", "paused")
    if phase not in {"pilot", "dynamic"}:
        return "qualification_rollout_paused"
    from app.modules.acquisition.group_qualification import URL

    if (config.get("promotion_active_promoters") or config.get("promotion_account_ids")) and account_id not in _ids(
        config["promotion_account_ids"]
    ):
        return "qualification_account_not_promoter"
    resolution = await current_authorization(db, account_id, target)
    row, group, member = resolution
    if group is None:
        return getattr(resolution, "reason", None) or "qualification_group_missing"
    if member is None or member.status != "joined":
        return "qualification_membership_missing"
    now = datetime.utcnow()
    if account_block_reason(await db.get(TelegramAccount, account_id, populate_existing=True), now):
        return "qualification_account_unavailable"
    if member.review_status != "approved" or member.ad_status in {"blocked", "paused"}:
        return "qualification_review_required"
    if (
        row is None
        or row.policy_version != POLICY_VERSION
        or row.membership_joined_at != member.joined_at
    ):
        return "qualification_review_required"
    if row.decision not in {"allowed", "trial"} or row.state not in {"completed", "running"}:
        return "qualification_not_approved"
    if not authorization_current(row, now):
        return "qualification_expired"
    from app.modules.acquisition.qualification_events import pending
    if await pending(db, member):
        return "qualification_event_pending"
    if not scope_matches(row, group, member):
        return "qualification_scope_changed"
    snapshot = _payload(row)
    from app.modules.acquisition.qualification_ai import profile_fingerprint

    current_account = await db.get(TelegramAccount, account_id, populate_existing=True)
    if snapshot.get("profile_fingerprint") != profile_fingerprint(current_account):
        return "qualification_profile_changed"
    if (
        snapshot.get("protected")
        or snapshot.get("technical_errors")
        or manually_protected(config, group, member)
    ):
        return "qualification_not_approved"
    if row.content_scope != "text_profile" or media_url or URL.search(content):
        return "qualification_content_scope_changed"
    # A verified 24-hour ordinary-member precedent can authorize a trial even
    # when a current or historical group rule prohibits ads. Its original post
    # is rechecked by validate_live_send immediately before the actual send.
    from app.modules.acquisition.qualification_continuity import OWN_SOURCE, own_proof_valid
    own_authorizes = snapshot.get("authorization_basis") == OWN_SOURCE
    if own_authorizes and not await own_proof_valid(db, snapshot, member, group, now):
        return "qualification_own_delivery_unconfirmed"
    precedent_authorizes_trial = own_authorizes or (
        row.decision == "trial"
        and (snapshot.get("advertising_audit") or {}).get("decision_source")
        == "verified_ordinary_member_precedent"
        and bool(
            trial_proof(
                [item for item in snapshot.get("evidence", []) if item.get("topic_id") is None]
            )
        )
    )
    identity = peer_identity(group.group_id, snapshot)
    if identity is None or identity[1] is None:
        return "qualification_group_identity_unknown"
    # A raw legacy membership cannot lend approval to the other marked namespace.
    if peer_identity(member.telegram_group_id, snapshot) != identity:
        return "qualification_group_identity_unknown"
    if isinstance(target, int) or (isinstance(target, str) and target.lstrip("-").isdigit()):
        if peer_identity(target, snapshot) != identity:
            return "qualification_group_identity_unknown"
    history = await qualification_peer_history(db, identity)
    for historical_group, historical_review in history:
        for ban in confirmed_group_bans(_payload(historical_review)):
            if not ban.get("confirmed_at") and historical_review.checked_at:
                ban = {**ban, "confirmed_at": historical_review.checked_at.isoformat()}
            relation = identity_relation(identity, peer_identity(historical_group.group_id, ban))
            if relation == "different":
                continue
            if relation == "unknown":
                return "qualification_group_identity_unknown"
            if not precedent_authorizes_trial and not group_ban_cleared(
                config, group, row, ban
            ):
                return "qualification_group_rule_prohibits"
    log_query = select(AdDeliveryLog).where(
        AdDeliveryLog.telegram_group_id.in_(identity_aliases(identity))
    )
    from app.modules.acquisition.adaptive_frequency import FrequencyService, enabled
    shared_frequency = await FrequencyService(db).state(group.group_id, snapshot)
    if shared_frequency and getattr(shared_frequency, "status", None) in {"exit_pending", "blocked"}:
        return shared_frequency.reason or "frequency_blocked"
    adaptive = phase == "dynamic" and await enabled(db, account_id)
    if adaptive:
        ready, _frequency = await FrequencyService(db).readiness(
            group.group_id, now, context=snapshot, reservation_token=reservation_token
        )
        if ready.reason:
            return ready.reason
        logs = []
    else:
        logs = list((await db.scalars(log_query)).all())
    for item in logs:
        try:
            context = json.loads(getattr(item, "qualification_context_json", None) or "{}")
        except (ValueError, TypeError):
            context = {}
        if item.status == "success" or item.telegram_message_id is not None:
            if item.status != "success":
                return "qualification_delivery_reconciliation_required"
            if (_date(item.sent_at) or _date(item.created_at) or now) >= now - timedelta(hours=24):
                return "qualification_group_daily_cap"
            relation = identity_relation(identity, peer_identity(item.telegram_group_id, context))
            if relation == "unknown":
                return "qualification_group_identity_unknown"
            if relation == "same" and item.survival_status != "survived":
                return "qualification_previous_survival_unresolved"
            continue
        if item.status in {"unknown", "send_unknown", "reconciliation_required"} or (
            item.error
            and any(
                term in item.error.lower()
                for term in ("send_outcome_unknown", "delivery_unknown", "unconfirmed")
            )
        ):
            if str(item.error or "").startswith("manual_evidence_required:"):
                # Terminal reconciliation outcome recorded by the survival
                # lane; no automatic action can resolve it further.
                continue
            return "qualification_delivery_reconciliation_required"
        if item.status in {"pending", "sending"}:
            if reservation_token and item.reservation_token == reservation_token:
                continue
            if item.account_id != account_id or reservation_token:
                return "qualification_group_delivery_in_flight"
            return "qualification_group_delivery_in_flight"
    if phase == "pilot":
        if not reservation_token:
            return "qualification_pilot_reservation_required"
        reserved = await db.scalar(
            select(AdDeliveryLog)
            .where(
                AdDeliveryLog.reservation_token == reservation_token,
                AdDeliveryLog.account_id == account_id,
            )
            .execution_options(populate_existing=True)
        )
        try:
            context = json.loads(getattr(reserved, "qualification_context_json", None) or "{}")
        except (ValueError, TypeError):
            context = {}
        if (
            reserved is None
            or reserved.status not in {"pending", "sending"}
            or reserved.telegram_message_id is not None
            or context.get("rollout_phase") != "pilot"
            or not rollout.get("started_at")
            or context.get("rollout_started_at") != rollout.get("started_at")
            or context.get("pilot_max_groups") != rollout.get("max_groups")
            or context.get("account_id") != account_id
            or context.get("audit_id") != row.id
            or context.get("evidence_hash") != row.evidence_hash
            or context.get("policy_version") != POLICY_VERSION
            or context.get("content_scope") != "text_profile"
            or identity_relation(identity, peer_identity(reserved.telegram_group_id, context))
            != "same"
        ):
            return "qualification_pilot_reservation_mismatch"
    current_system_ids, identity_fingerprint = await covered_system_ids(
        db, config, account_id=account_id
    )
    if (
        not identity_fingerprint
        or snapshot.get("system_identity_fingerprint") != identity_fingerprint
        or snapshot.get("system_user_ids") != sorted(current_system_ids)
    ):
        return "qualification_system_identity_unconfirmed"
    if not snapshot.get("system_identity_coverage"):
        return "qualification_system_identity_unconfirmed"
    if row.decision == "trial" and not own_authorizes and not trial_proof(
        [item for item in snapshot.get("evidence", []) if item.get("topic_id") is None]
    ):
        return "qualification_ad_precedent_unconfirmed"
    return None
