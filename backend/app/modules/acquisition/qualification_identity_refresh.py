"""Reconcile managed-roster changes without inventing fresh Telegram evidence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, exists, func, select, update

from app.core.account.models import AccountType, TelegramAccount
from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import AdDeliveryScheduleState, GroupQualificationAudit
from app.modules.acquisition.qualification_system_identity import (
    covered_system_ids,
    identity_fingerprint,
    parse_identity_map,
)

IDENTITY_REASON = "qualification_system_identity_unconfirmed"
IDENTITY_REASONS = (IDENTITY_REASON, "unknown:" + IDENTITY_REASON)


async def reusable_identity_context(
    db: Any,
    config: dict,
    account_id: int,
    evidence: dict,
) -> dict | None:
    """Prove unchanged exclusions and the signed roster's strict contraction.

    Legacy snapshots omitted their roster. They can be recovered only when the
    original hash exactly matches all retained mappings. New snapshots retain
    the roster so successive deletions are also verifiable. Changed mappings,
    new accounts, incomplete coverage and unknown hashes require fresh review.
    """
    ids, fingerprint = await covered_system_ids(db, config, account_id=account_id)
    if (
        not fingerprint
        or evidence.get("system_identity_coverage") is not True
        or evidence.get("system_user_ids") != sorted(ids)
    ):
        return None
    managed = set(
        (
            await db.scalars(
                select(TelegramAccount.id).where(
                    TelegramAccount.account_type == AccountType.PROMOTER,
                )
            )
        ).all()
    )
    context = {
        "fingerprint": fingerprint,
        "managed_account_ids": sorted(managed),
        "basis": "current",
    }
    if evidence.get("system_identity_fingerprint") == fingerprint:
        return context
    mapping = parse_identity_map(config.get("system_account_user_ids"))
    prior = evidence.get("system_identity_managed_account_ids", sorted(mapping))
    if (
        not isinstance(prior, list)
        or not prior
        or any(type(value) is not int or value <= 0 for value in prior)
        or len(set(prior)) != len(prior)
        or not managed < set(prior)
        or not set(prior) <= mapping.keys()
        or evidence.get("system_identity_fingerprint")
        != identity_fingerprint(set(prior), mapping, config.get("system_user_ids", []))
    ):
        return None
    return {**context, "basis": "retired_accounts_same_exclusions"}


async def queue_identity_refreshes(
    db: Any,
    config: dict,
    *,
    account_id: int | None = None,
    limit: int = 2,
) -> int:
    """Schedule each changed fingerprint once; retain the reader's backoff."""
    if not config.get("enabled") or limit <= 0:
        return 0
    member = GroupAccountMembership
    audit = GroupQualificationAudit
    latest = (
        select(func.max(audit.id))
        .where(
            audit.membership_id == member.id,
            audit.state != "cancelled",
        )
        .correlate(member)
        .scalar_subquery()
    )
    blocked_ad = exists(
        select(AdDeliveryScheduleState.id).where(
            AdDeliveryScheduleState.account_id == audit.account_id,
            AdDeliveryScheduleState.group_id == audit.group_id,
            AdDeliveryScheduleState.last_reason.in_(IDENTITY_REASONS),
        )
    )
    query = (
        select(audit)
        .join(member, member.id == audit.membership_id)
        .where(
            audit.id == latest,
            audit.state == "completed",
            audit.decision.in_(["allowed", "trial"]),
            audit.policy_version == POLICY_VERSION,
            audit.content_scope == "text_profile",
            audit.membership_joined_at == member.joined_at,
            member.status == "joined",
            member.left_at.is_(None),
            member.review_status == "approved",
            member.ad_status == "active",
        )
        .order_by(case((blocked_ad, 0), else_=1), audit.id)
        .limit(300)
        .with_for_update(
            of=audit,
            skip_locked=True,
        )
        .execution_options(populate_existing=True)
    )
    if account_id is not None:
        query = query.where(audit.account_id == account_id)
    now = datetime.utcnow()
    queued = 0
    for row in (await db.scalars(query)).all():
        if queued >= max(0, limit):
            break
        try:
            evidence = json.loads(row.evidence_json or "{}")
        except (ValueError, TypeError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        ids, fingerprint = await covered_system_ids(db, config, account_id=row.account_id)
        if not fingerprint:
            continue  # Identity registration must establish coverage first.
        if (
            evidence.get("system_identity_fingerprint") == fingerprint
            and evidence.get("system_identity_coverage") is True
            and evidence.get("system_user_ids") == sorted(ids)
        ):
            continue
        if (evidence.get("identity_refresh") or {}).get("fingerprint") == fingerprint:
            continue
        reusable = await reusable_identity_context(db, config, row.account_id, evidence)
        evidence["identity_refresh"] = {
            "fingerprint": fingerprint,
            "requested_at": now.isoformat(),
            "mode": "roster" if reusable else "full",
        }
        row.evidence_json = json.dumps(evidence, ensure_ascii=False)
        row.next_retry_at = now
        if not reusable:
            row.expires_at = now
            # An event grant otherwise ignores expires_at. The old proof must
            # remain revoked until collection establishes the new exclusions.
            evidence["invalidated_at"] = now.isoformat()
            row.evidence_json = json.dumps(evidence, ensure_ascii=False)
        await db.execute(
            update(AdDeliveryScheduleState)
            .where(
                AdDeliveryScheduleState.account_id == row.account_id,
                AdDeliveryScheduleState.group_id == row.group_id,
                AdDeliveryScheduleState.status == "retry",
                AdDeliveryScheduleState.last_reason.in_(IDENTITY_REASONS),
            )
            .values(next_due_at=now + timedelta(minutes=5), updated_at=now)
        )
        queued += 1
    await db.flush()
    return queued


async def identity_retry_at(db: Any, account_id: int, group_id: int, now: datetime) -> datetime:
    retry = await db.scalar(
        select(GroupQualificationAudit.next_retry_at)
        .join(
            GroupAccountMembership,
            GroupAccountMembership.id == GroupQualificationAudit.membership_id,
        )
        .where(
            GroupQualificationAudit.account_id == account_id,
            GroupQualificationAudit.group_id == group_id,
            GroupQualificationAudit.state != "cancelled",
            GroupQualificationAudit.membership_joined_at == GroupAccountMembership.joined_at,
            GroupAccountMembership.status == "joined",
        )
        .order_by(GroupQualificationAudit.id.desc())
        .limit(1)
    )
    return max(now + timedelta(minutes=5), retry or now)


async def wake_identity_schedule(db: Any, account_id: int, group_id: int) -> None:
    now = datetime.utcnow()
    await db.execute(
        update(AdDeliveryScheduleState)
        .where(
            AdDeliveryScheduleState.account_id == account_id,
            AdDeliveryScheduleState.group_id == group_id,
            AdDeliveryScheduleState.status == "retry",
            AdDeliveryScheduleState.last_reason.in_(IDENTITY_REASONS),
        )
        .values(next_due_at=now, last_reason=None, updated_at=now)
    )
