"""Idempotent maturity adoption from an existing, scoped own-delivery authorization."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig
from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.adaptive_frequency import FrequencyService, payload, rule_quota
from app.modules.acquisition.mature_survival import LOCAL_WAITS
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequencyEvent
from app.modules.acquisition.qualification_continuity import OWN_SOURCE, own_proof_valid
from app.modules.acquisition.qualification_identity import identity_relation, peer_identity
from app.modules.acquisition.qualification_service import current_authorization, policy, send_gate

FREQUENCY_WAITS = {
    "frequency_group_daily_cap",
    "frequency_group_interval",
    "frequency_survival_unresolved",
    "frequency_survival_due",
    "frequency_daily_review_due",
    "frequency_daily_review_unknown",
    "frequency_first_checkpoint_required",
    "qualification_previous_survival_unresolved",
}


async def adopt_established(db: Any, membership_id: int, *, apply: bool = False) -> dict:
    """Caller owns the transaction. No Telegram calls, rewrites of receipts or quota refunds."""
    now = datetime.utcnow()
    member = await db.get(GroupAccountMembership, membership_id)
    result = {"membership_id": membership_id, "applied": False}
    if member is None:
        return {**result, "reason": "membership_missing"}
    result.update(account_id=member.account_id, group_id=member.group_id)
    config = await db.scalar(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == member.account_id)
    )
    if not (
        config
        and config.enabled
        and config.auto_ads_enabled
        and config.dynamic_capacity_enabled
        and config.adaptive_ads_enabled
    ):
        return {**result, "reason": "account_adaptive_disabled"}
    audit, group, current_member = await current_authorization(
        db, member.account_id, member.telegram_group_id
    )
    context = payload(audit.evidence_json) if audit else {}
    if (
        current_member is None
        or current_member.id != member.id
        or context.get("authorization_basis") != OWN_SOURCE
        or not await own_proof_valid(db, context, member, group, now)
    ):
        return {**result, "reason": "scoped_own_evidence_required"}
    proof = context["own_delivery_proof"]
    conf = await policy(db, fresh=True)
    from app.modules.acquisition.qualification_system_identity import covered_system_ids

    ids, fingerprint = await covered_system_ids(db, conf, account_id=member.account_id)
    if (
        not fingerprint
        or context.get("system_identity_fingerprint") != fingerprint
        or context.get("system_user_ids") != sorted(ids)
        or not context.get("system_identity_coverage")
        or proof["account_user_id"]
        != conf.get("system_account_user_ids", {}).get(str(member.account_id))
    ):
        return {**result, "reason": "system_identity_unconfirmed"}
    gate = await send_gate(db, member.account_id, member.telegram_group_id, "查看简介", None)
    if gate is not None and gate not in FREQUENCY_WAITS:
        return {**result, "reason": gate}
    service = FrequencyService(db)
    # Lock receipts before the group, matching daily-finish and reconciliation.
    history = await service.logs(member.telegram_group_id, context)
    if apply and history:
        await db.execute(
            select(AdDeliveryLog.id)
            .where(AdDeliveryLog.id.in_([log.id for log in history]))
            .order_by(AdDeliveryLog.id)
            .with_for_update(of=AdDeliveryLog)
        )
        history = await service.logs(member.telegram_group_id, context)
    identity = peer_identity(member.telegram_group_id, context)
    for log in history:
        if log.status in {"pending", "sending", "unknown", "reconciliation_required"}:
            return {**result, "reason": "delivery_inflight_or_unknown"}
        if log.status == "success" or log.telegram_message_id is not None:
            if (
                log.status != "success"
                or not log.telegram_message_id
                or not log.sent_at
                or identity_relation(
                    identity,
                    peer_identity(log.telegram_group_id, payload(log.qualification_context_json)),
                )
                != "same"
            ):
                return {**result, "reason": "receipt_identity_or_outcome_unknown"}
            if log.survival_status in {"deleted", "check_failed"}:
                return {**result, "reason": "negative_survival_history"}
            if (
                log.survival_error
                and log.survival_error not in LOCAL_WAITS
                and not log.survival_error.startswith("survival_read_unknown:")
            ):
                return {**result, "reason": "survival_observation_unresolved"}
    state = await service.state(
        member.telegram_group_id, context, lock=apply, create=apply, now=now
    )
    if state and state.mature:
        return {**result, "reason": "already_mature"}
    if await db.scalar(
        select(GroupAdFrequencyEvent.id)
        .where(
            GroupAdFrequencyEvent.log_id == proof["log_id"],
            GroupAdFrequencyEvent.kind == "established_survived",
        )
        .limit(1)
    ):
        return {**result, "reason": "proof_already_adopted"}
    if state and (state.status != "active" or (state.pause_until and state.pause_until > now)):
        return {**result, "reason": "frequency_protected"}
    if (
        state
        and state.daily_review_token
        and state.daily_review_expires_at
        and state.daily_review_expires_at > now
    ):
        return {**result, "reason": "daily_review_inflight"}
    quota = min(2, rule_quota(context))
    if quota < 1:
        return {**result, "reason": "group_rule_limit"}
    result.update(
        eligible=True,
        proof_log_id=proof["log_id"],
        quota=quota,
        evidence_checked_at=proof["checked_at"],
        reason="verified_own_survival",
    )
    if not apply:
        return result
    old_quota, epoch = state.quota, state.epoch
    state.quota, state.mature = quota, True
    state.epoch += 1
    state.epoch_started_at = now
    state.daily_review_due_at = state.promote_after = now + timedelta(days=1)
    state.daily_review_checked_at = datetime.fromisoformat(proof["checked_at"])
    state.daily_review_log_id = proof["log_id"]
    state.daily_review_error = state.daily_review_retry_at = state.daily_review_token = (
        state.daily_review_expires_at
    ) = None
    state.reason, state.updated_at = None, now
    db.add(
        GroupAdFrequencyEvent(
            telegram_group_id=state.telegram_group_id,
            log_id=proof["log_id"],
            kind="established_survived",
            epoch=epoch,
            old_quota=old_quota,
            new_quota=quota,
            created_at=now,
        )
    )
    await db.flush()
    return {**result, "applied": True}
