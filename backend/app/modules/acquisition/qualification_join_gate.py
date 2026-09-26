"""Read-only, group-wide rejoin guard at the Telegram join RPC boundary.

``rejoin_clearances`` are operator records tied to a rejected audit/checked time/
evidence hash and a fresh review audit/hash. They authorize a new membership only;
all advertising still requires the separate account-scoped send gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.core.group.models import Group
from app.core.settings_models import SystemSetting
from app.modules.acquisition.group_qualification import (
    POLICY_VERSION,
    snapshot_hash,
    trial_evidence,
)
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_identity import (
    entity_identity,
    identity_relation,
    peer_identity,
)
from app.modules.acquisition.qualification_service import (
    SETTING_KEY,
    _date,
    _payload,
    authoritative_rule_fingerprint,
    confirmed_group_bans,
    group_ban_cleared,
    qualification_peer_history,
)


def rejection_facts(row: GroupQualificationAudit) -> list[dict[str, Any]]:
    """Only this qualification policy creates rejoin blocks, not manual leaves."""
    if row.policy_version not in {"pp-ai-qualification-v1", "pp-ai-qualification-v2", POLICY_VERSION}:
        return []
    payload = _payload(row)
    facts = [
        item for item in payload.get("qualification_exit_history", []) if isinstance(item, dict)
    ]
    for index, item in enumerate([payload, *payload.get("previous_checks", [])]):
        if not isinstance(item, dict):
            continue
        decision = row.decision if index == 0 else item.get("decision")
        if decision != "reject":
            continue
        checked_at = row.checked_at if index == 0 else _date(item.get("checked_at"))
        if checked_at is None:
            continue
        snapshot = {
            key: value
            for key, value in item.items()
            if key not in {"previous_checks", "qualification_exit_history"}
        }
        facts.append(
            {
                "audit_id": row.id,
                "checked_at": checked_at.isoformat(),
                "evidence_hash": (
                    row.evidence_hash
                    if index == 0
                    else item.get("evidence_hash")
                    or snapshot_hash(item, row.account_id, row.content_scope)
                ),
                "reason": row.reason if index == 0 else item.get("reason"),
                "policy_version": row.policy_version,
                "snapshot": snapshot,
            }
        )
    # A single rejection may appear both in the exit record and the review history.
    unique = {}
    for fact in facts:
        version = fact.get("policy_version", fact.get("snapshot", {}).get("policy_version"))
        if version not in {None, "pp-ai-qualification-v1", "pp-ai-qualification-v2", POLICY_VERSION}:
            continue
        key = (fact.get("audit_id"), str(fact.get("checked_at")), fact.get("evidence_hash"))
        unique[key] = fact
    return list(unique.values())


def _fresh_review(row: GroupQualificationAudit, group: Group, now: datetime) -> bool:
    snapshot = _payload(row)
    return bool(
        row.policy_version == POLICY_VERSION
        and row.state in {"completed", "manual_required"}
        and row.checked_at is not None
        and now - timedelta(hours=24) <= row.checked_at <= now
        and row.expires_at is not None
        and row.expires_at > now
        and row.evidence_hash
        and snapshot.get("policy_version") == POLICY_VERSION
        and snapshot.get("group_id") == group.id
        and snapshot.get("telegram_group_id") is not None
        and peer_identity(group.group_id, snapshot) is not None
        and not snapshot.get("technical_errors")
    )


def _condition_changed(fact: dict[str, Any], review: GroupQualificationAudit) -> bool:
    """A timestamp/hash change alone never proves that a rejected condition improved."""
    old = fact.get("snapshot") or {}
    current = _payload(review)
    reason = fact.get("reason")
    if (
        reason in {"members_below_50", "messages_below_5_in_72h"}
        and fact.get("policy_version") in {"pp-ai-qualification-v1", "pp-ai-qualification-v2"}
        and review.policy_version == POLICY_VERSION
    ):
        return bool(
            review.decision in {"allowed", "trial"}
            and isinstance(current.get("online_count"), int)
            and current["online_count"] >= 2
            and trial_evidence(current.get("evidence", []))
        )
    if reason == "members_below_50":
        count = current.get("member_count")
        return bool(
            current.get("member_count_verified") is True and isinstance(count, int) and count >= 50
        )
    if reason == "messages_below_5_in_72h":
        count = current.get("valid_messages")
        return bool(isinstance(count, int) and count >= 5)
    if reason == "account_permanent_send_restriction":
        permission = current.get("permissions", {})
        return bool(
            current.get("account_id") == old.get("account_id")
            and permission.get("member") is True
            and permission.get("can_send_text") is True
            and not permission.get("permanent_send_restriction_verified")
        )
    old_rules = authoritative_rule_fingerprint(old)
    new_rules = authoritative_rule_fingerprint(current)
    if new_rules and new_rules != old_rules and not current.get("rules_incomplete"):
        return True
    if reason == "advertising_evidence_insufficient_after_observation":
        return trial_evidence(current.get("evidence", [])) and not trial_evidence(
            old.get("evidence", [])
        )
    return False


def _rejoin_cleared(
    config: dict[str, Any], group: Group, fact: dict[str, Any], review: GroupQualificationAudit
) -> bool:
    rejected_at = _date(fact.get("checked_at"))
    if (
        rejected_at is None
        or review.checked_at <= rejected_at
        or not fact.get("evidence_hash")
        or not _condition_changed(fact, review)
    ):
        return False
    for clearance in config.get("rejoin_clearances", []):
        if not isinstance(clearance, dict):
            continue
        reviewed_at = _date(clearance.get("reviewed_at"))
        if (
            clearance.get("group_id") == group.id
            and clearance.get("rejection_audit_id") == fact.get("audit_id")
            and _date(clearance.get("rejection_checked_at")) == rejected_at
            and clearance.get("rejection_evidence_hash") == fact.get("evidence_hash")
            and clearance.get("review_audit_id") == review.id
            and clearance.get("review_evidence_hash") == review.evidence_hash
            and type(clearance.get("operator_id")) is int
            and clearance["operator_id"] > 0
            and reviewed_at is not None
            and review.checked_at <= reviewed_at <= datetime.utcnow()
        ):
            return True
    return False


async def qualification_join_gate(db: Any, entity: Any) -> str | None:
    """Return a blocking reason; never join, send, mutate membership or clear history."""
    setting = await db.get(SystemSetting, SETTING_KEY, populate_existing=True)
    if setting is None or not setting.value:
        return "qualification_disabled"
    config = json.loads(setting.value)
    if not config.get("enabled"):
        return "qualification_disabled"
    identity = entity_identity(entity)
    if identity is None:
        return "qualification_join_identity_unknown"
    from app.modules.acquisition.adaptive_frequency import FrequencyService
    marked = -identity[0] - (1_000_000_000_000 if identity[1] == "channel" else 0)
    frequency = await FrequencyService(db).state(marked)
    if frequency and frequency.status in {"exit_pending", "blocked"}:
        return "frequency_rejoin_blocked"
    history = await qualification_peer_history(db, identity)
    latest = {}
    for group, row in history:
        latest.setdefault(row.membership_id, (group, row))
    candidates = [
        (group, row)
        for group, row in latest.values()
        if _fresh_review(row, group, datetime.utcnow())
        and identity_relation(identity, peer_identity(group.group_id, _payload(row))) == "same"
    ]
    for group, row in history:
        facts = rejection_facts(row)
        for ban in confirmed_group_bans(_payload(row)):
            relation = identity_relation(identity, peer_identity(group.group_id, ban))
            if relation == "different":
                continue
            if relation == "unknown":
                return "qualification_join_identity_unknown"
            if not any(
                group_ban_cleared(config, review_group, review, ban)
                for review_group, review in candidates
            ):
                return "qualification_group_rule_prohibits_rejoin"
        for fact in facts:
            evidence = fact.get("snapshot") or {}
            relation = identity_relation(identity, peer_identity(group.group_id, evidence))
            if relation == "different":
                continue
            if relation == "unknown":
                return "qualification_join_identity_unknown"
            # Rule bans use the stricter clearance contract above.
            if evidence.get("group_level_advertising_ban"):
                continue
            if not any(
                _rejoin_cleared(config, review_group, fact, review)
                for review_group, review in candidates
            ):
                return "qualification_rejoin_requires_changed_evidence_clearance"
    return None
