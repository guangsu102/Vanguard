"""Account-Actor adapter for the durable P2 growth Outbox.

The Outbox service owns leases, attempts and the state machine.  This module
is deliberately a thin owner-side adapter: it resolves the durable payload,
uses the already connected account Actor, and returns a serializable
``ActorResult``.  No worker process may call Telegram through this module;
``execute_owner_outbox`` is only registered by ``session_exec`` while the
account owner context is active.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, select

from app.core.account.session_exec import current_actor_account
from app.core.account.session_lease import OWNER_PREFIX, PROCESS_ID
from app.core.account.telegram_execution import (
    TelegramJoinRequestPendingError,
    TelegramSendDeferredError,
    TelegramSendPreflightError,
)
from app.core.config import settings
from app.core.redis import get_redis
from app.modules.acquisition.p2_outbox import (
    ActorResult,
    PermanentOutboxError,
    RetryableOutboxError,
    UnknownExternalOutcome,
    dispatch_outbox_once,
)
from app.modules.acquisition.search.group_finder import DiscoveredGroup


async def produce_join_outbox(
    db: Any,
    account_id: int,
    *,
    account_pool: Any = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Materialize pre-join candidates into the durable P2 Outbox.

    The preview facts are persisted by the existing account Actor read lane.
    They are used only when they contain a strong, explicit qualification
    signal.  A generic candidate score or an incomplete preview never mints a
    snapshot.  The join budget reservation created here is the same durable
    reservation passed to ``enqueue_operation``; no second reservation is
    created for the Outbox operation.
    """

    if not settings.p2_outbox_enabled_for("join_group", int(account_id)):
        return {"enqueued": 0, "skipped": 0, "reason": "p2_outbox_disabled"}
    if not settings.P3_QUALIFICATION_SNAPSHOT:
        return {"enqueued": 0, "skipped": 0, "reason": "p3_snapshot_disabled"}
    # P2 requires the P0 durable budget ledger.  A local join reservation
    # without its ledger row cannot satisfy enqueue_operation's invariant.
    if not settings.BUDGET_LEDGER_ENABLED:
        return {"enqueued": 0, "skipped": 0, "reason": "budget_ledger_disabled"}

    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition.automation import (
        AUTO_JOIN_RETRYABLE_GROUP_STATUSES,
        AcquisitionAutomationService,
    )
    from app.modules.acquisition.candidate_inventory import fresh, load, ready
    from app.modules.acquisition.join_budget import JoinBudgetBlocked
    from app.modules.acquisition.p2_models import GrowthOperation
    from app.modules.acquisition.p2_outbox import (
        ACTIVE_OPERATION_STATES,
        P2TargetBlocked,
        derive_idempotency_key,
        enqueue_operation,
    )
    from app.modules.acquisition.qualification_cache import (
        effective_rule_version,
        persist_qualification_snapshot,
    )
    from app.modules.acquisition.qualification_snapshot import current_fact_version

    now = datetime.utcnow()
    maximum = max(1, int(limit or settings.P2_OUTBOX_BATCH_SIZE))
    service = AcquisitionAutomationService(db, account_pool=account_pool)
    groups = list(
        (
            await db.scalars(
                select(Group)
                .where(
                    Group.status.in_(AUTO_JOIN_RETRYABLE_GROUP_STATUSES),
                    Group.username.isnot(None),
                )
                .order_by(
                    Group.level_score.desc(),
                    Group.convert_score.desc(),
                    Group.activity_score.desc(),
                    Group.id.asc(),
                )
                .limit(maximum * 8)
            )
        ).all()
    )
    if not groups:
        return {"enqueued": 0, "skipped": 0}
    facts = await load(db, int(account_id), [int(group.id) for group in groups])
    from app.modules.acquisition.qualification_service import policy as qualification_policy

    policy = await qualification_policy(db, fresh=True)
    if not isinstance(policy, Mapping):
        policy = {}
    try:
        from app.core.automation_settings import get_ad_capacity_settings

        limits = await get_ad_capacity_settings(db)
    except Exception:
        limits = {}
    rule_version = effective_rule_version(policy, limits)
    stats = {"enqueued": 0, "skipped": 0}

    for group in groups:
        if stats["enqueued"] >= maximum:
            break
        target_key = str(int(group.group_id))
        fact = facts.get(int(group.id)) or {}
        if not fresh(fact, group, int(account_id), now) or not ready(fact):
            stats["skipped"] += 1
            continue
        # Only explicit policy evidence can authorize a pre-join operation.
        # ``metadata_checked`` alone is an ordering hint, not a qualification.
        signal = str(fact.get("rule_signal") or "")
        ordinary = int(fact.get("ordinary_advertisers") or 0)
        if signal == "explicit_allow":
            verdict = "allowed"
        elif ordinary > 0:
            verdict = "trial"
        else:
            stats["skipped"] += 1
            continue
        evidence = {
            "source": "prejoin_candidate_preview",
            "authorization_scope": "join",
            "quality_status": "qualified",
            "collection_complete": True,
            "read_status": "fresh",
            "collected_at": fact.get("checked_at") or now.isoformat(),
            "telegram_group_id": int(group.group_id),
            "username": group.username,
            "preview": dict(fact),
            "evidence": [
                {
                    "source": "prejoin_candidate_preview",
                    "rule_signal": signal,
                    "ordinary_advertisers": ordinary,
                    "member_count": fact.get("member_count"),
                    "namespace": fact.get("namespace"),
                }
            ],
        }
        fact_version = await current_fact_version(db, target_key)
        # Legacy preview rows without a watermark must be refreshed before
        # they can authorize a P2 write.
        if str(fact.get("source_fact_version") or "") != str(fact_version):
            stats["skipped"] += 1
            continue
        snapshot = await persist_qualification_snapshot(
            db,
            evidence,
            target_key=target_key,
            fact_version=fact_version,
            rule_version=rule_version,
            verdict=verdict,
            now=now,
        )
        if snapshot is None:
            stats["skipped"] += 1
            continue

        # Re-check local membership and durable operation identity before
        # consuming the scarce join reservation.
        joined = await db.scalar(
            select(GroupAccountMembership.id).where(
                GroupAccountMembership.group_id == int(group.id),
                GroupAccountMembership.account_id == int(account_id),
                GroupAccountMembership.status.in_(("joined", "pending")),
            )
        )
        if joined is not None:
            stats["skipped"] += 1
            continue
        idem = derive_idempotency_key(
            kind="join_group",
            account_id=int(account_id),
            target_key=target_key,
            run_id=f"group:{int(group.id)}",
            campaign_id=None,
            snapshot_id=snapshot.snapshot_id,
            group_version=snapshot.group_version,
            rule_version=snapshot.rule_version,
            evidence_hash=snapshot.evidence_hash,
        )
        existing = await db.scalar(
            select(GrowthOperation.id).where(GrowthOperation.idempotency_key == idem)
        )
        if existing is not None:
            stats["skipped"] += 1
            continue
        active = await db.scalar(
            select(GrowthOperation.id).where(
                GrowthOperation.account_id == int(account_id),
                GrowthOperation.target_key == target_key,
                or_(
                    GrowthOperation.state.in_(ACTIVE_OPERATION_STATES),
                    GrowthOperation.reconciliation_required.is_(True),
                ),
            ).limit(1)
        )
        if active is not None:
            stats["skipped"] += 1
            continue

        attempt = None
        try:
            attempt = await service.join_budget.reserve(
                int(account_id),
                group_id=int(group.id),
                telegram_group_id=int(group.group_id),
                group_username=str(group.username).lstrip("@"),
                group_title=group.title,
                source_keyword=group.source_keyword,
                source="p2_outbox",
                require_auto_join_enabled=True,
                commit=False,
                now=now,
            )
            reservation_id = f"join:{int(attempt.id)}"
            operation, _outbox, reused = await enqueue_operation(
                db,
                kind="join_group",
                account_id=int(account_id),
                target_key=target_key,
                snapshot_id=snapshot.snapshot_id,
                group_version=snapshot.group_version,
                rule_version=snapshot.rule_version,
                evidence_hash=snapshot.evidence_hash,
                payload={
                    "target": str(group.username).lstrip("@"),
                    "username": str(group.username).lstrip("@"),
                    "title": group.title or str(group.username),
                    "telegram_group_id": int(group.group_id),
                    "group_db_id": int(group.id),
                    "source_keyword": group.source_keyword,
                    # The risk/audit callback uses the attempt's opaque key;
                    # the ledger identity remains ``join:{attempt.id}``.
                    "join_reservation_key": str(attempt.reservation_key),
                    "join_attempt_id": int(attempt.id),
                },
                group_id=int(group.id),
                budget_reservation_id=reservation_id,
                idempotency_key=idem,
                priority=10,
                run_id=f"group:{int(group.id)}",
                now=now,
            )
            if reused:
                await service.join_budget.release(
                    attempt, reason="p2_operation_reused", now=now, commit=False
                )
            await db.commit()
        except JoinBudgetBlocked:
            stats["skipped"] += 1
            continue
        except P2TargetBlocked:
            await db.rollback()
            try:
                await service.join_budget.release(attempt, reason="p2_target_blocked", now=now)
            except Exception:
                pass
            stats["skipped"] += 1
            continue
        except Exception:
            await db.rollback()
            try:
                if attempt is not None:
                    await service.join_budget.release(attempt, reason="p2_enqueue_failed", now=now)
            except Exception:
                pass
            raise
        stats["enqueued"] += 1
    return stats


async def produce_ad_outbox(
    db: Any,
    account_id: int,
    *,
    account_pool: Any = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Materialize eligible ad candidates into the durable P2 Outbox.

    This is deliberately a producer only: it performs no Telegram reads or
    writes.  A candidate is accepted only when P3 has persisted a current,
    eligible immutable qualification snapshot.  The actor consumer remains the
    sole component allowed to execute the eventual send.
    """

    if not settings.p2_outbox_enabled_for("send_ad", int(account_id)):
        return {"enqueued": 0, "skipped": 0, "reason": "p2_outbox_disabled"}
    if not settings.P3_QUALIFICATION_SNAPSHOT:
        return {"enqueued": 0, "skipped": 0, "reason": "p3_snapshot_disabled"}

    from app.core.account.budget_ledger import reserve
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.models import (
        AdDeliveryLog,
        AdSurvivalStatus,
        DeliveryStatus,
    )
    from app.modules.acquisition.p2_models import GrowthOperation
    from app.modules.acquisition.p2_outbox import (
        ACTIVE_OPERATION_STATES,
        P2TargetBlocked,
        derive_idempotency_key,
        enqueue_operation,
    )
    from app.modules.acquisition.qualification_cache import (
        get_valid_qualification_snapshot,
    )

    now = datetime.utcnow()
    maximum = max(1, int(limit or settings.P2_OUTBOX_BATCH_SIZE))
    service = AcquisitionAutomationService(db, account_pool=account_pool)
    bindings = await service._list_enabled_ad_bindings(account_id=int(account_id))
    stats = {"enqueued": 0, "skipped": 0}

    for binding in bindings:
        if stats["enqueued"] >= maximum:
            break
        campaign = binding.campaign
        if campaign is None or not service._campaign_is_active(campaign):
            stats["skipped"] += 1
            continue
        policy = str(getattr(campaign, "delivery_policy", None) or "growth")
        memberships = await service._list_joined_groups_for_account(
            int(account_id), campaign_id=campaign.id
        )
        for membership in memberships:
            if stats["enqueued"] >= maximum:
                break
            group = membership.group
            if group is None:
                stats["skipped"] += 1
                continue
            # Qualification snapshots are keyed by the canonical Group
            # telegram id (the same value used by qualification_service),
            # while the membership may carry a marked peer alias.
            target_key = str(int(group.group_id))

            # Avoid doing any work for an already active or unresolved target.
            active = await db.scalar(
                select(GrowthOperation.id).where(
                    GrowthOperation.account_id == int(account_id),
                    GrowthOperation.target_key == target_key,
                    or_(
                        GrowthOperation.state.in_(ACTIVE_OPERATION_STATES),
                        GrowthOperation.reconciliation_required.is_(True),
                    ),
                ).limit(1)
            )
            if active is not None:
                stats["skipped"] += 1
                continue

            snapshot = await get_valid_qualification_snapshot(
                db,
                target_key=target_key,
                account_id=int(account_id),
                campaign_id=int(campaign.id),
                required_scope="ad",
            )
            if snapshot is None:
                # Missing/stale/unknown evidence must go back through P3
                # qualification and may never be converted into an Outbox row.
                stats["skipped"] += 1
                continue

            # The producer must be cheap and deterministic.  It may use an
            # already bound/sendable creative (or an existing pool entry),
            # but must never invoke the legacy AI generation side effect while
            # merely materializing an Outbox row.
            creative = getattr(binding, "creative", None)
            if creative is None or not service._creative_is_sendable(creative):
                pool = await service._creative_pool_for_binding(binding)
                creative = next(
                    (item for item in pool if service._creative_is_sendable(item)),
                    None,
                )
            if creative is None:
                stats["skipped"] += 1
                continue

            # Keep the existing local policy and pacing gates.  ``read_only``
            # prevents this producer from entering the legacy send path.
            skip_reason = await service._ad_skip_reason(
                binding,
                campaign,
                creative,
                membership,
                dry_run=True,
                read_only=True,
                now=now,
            )
            if skip_reason:
                stats["skipped"] += 1
                continue

            reservation_id = (
                f"p2-ad:{int(account_id)}:{target_key}:{int(campaign.id)}:"
                f"{snapshot.snapshot_id}"
            )[:160]
            idem = derive_idempotency_key(
                kind="send_ad",
                account_id=int(account_id),
                target_key=target_key,
                run_id=None,
                campaign_id=int(campaign.id),
                snapshot_id=snapshot.snapshot_id,
                group_version=snapshot.group_version,
                rule_version=snapshot.rule_version,
                evidence_hash=snapshot.evidence_hash,
            )

            # Recheck the exact idempotency row before creating a legacy log;
            # this keeps repeated producer passes free of orphan pending logs.
            existing = await db.scalar(
                select(GrowthOperation.id).where(GrowthOperation.idempotency_key == idem)
            )
            if existing is not None:
                stats["skipped"] += 1
                continue

            reservation_token = reservation_id
            log = AdDeliveryLog(
                account_id=int(account_id),
                group_id=int(group.id),
                telegram_group_id=int(membership.telegram_group_id),
                ad_campaign_id=int(campaign.id),
                creative_id=int(creative.id),
                status=DeliveryStatus.PENDING.value,
                survival_status=AdSurvivalStatus.NOT_REQUIRED.value,
                survival_stage="two_minute",
                reservation_token=reservation_token,
            )
            db.add(log)
            await db.flush()
            try:
                await reserve(
                    db,
                    reservation_id,
                    account_id=int(account_id),
                    workload="growth_ad",
                    lane="ad",
                    reserved_reads=1,
                    lease_expires_at=now + timedelta(seconds=settings.P2_OUTBOX_LEASE_SECONDS),
                    now=now,
                )
                await enqueue_operation(
                    db,
                    kind="send_ad",
                    account_id=int(account_id),
                    target_key=target_key,
                    snapshot_id=snapshot.snapshot_id,
                    group_version=snapshot.group_version,
                    rule_version=snapshot.rule_version,
                    evidence_hash=snapshot.evidence_hash,
                    payload={
                        "telegram_group_id": int(membership.telegram_group_id),
                        "group_db_id": int(group.id),
                        "campaign_id": int(campaign.id),
                        "creative_id": int(creative.id),
                        "delivery_policy": policy,
                        "reservation_token": reservation_token,
                        "delivery_log_id": int(log.id),
                    },
                    campaign_id=int(campaign.id),
                    budget_reservation_id=reservation_id,
                    idempotency_key=idem,
                    priority=70,
                    now=now,
                )
                await db.commit()
            except P2TargetBlocked:
                await db.rollback()
                stats["skipped"] += 1
                continue
            except Exception:
                await db.rollback()
                raise
            stats["enqueued"] += 1

    return stats


def _decode(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value) if value is not None else None


def _payload_target(operation: Any, payload: Mapping[str, Any]) -> str:
    target = payload.get("target") or payload.get("target_key") or operation.target_key
    target = str(target or "").strip()
    if not target:
        raise PermanentOutboxError("p2_target_required")
    return target


def _creative_from_payload(payload: Mapping[str, Any]) -> Any:
    """Build an immutable creative view when the payload embeds its content.

    P2 callers should normally include ``creative_id`` and the consumer loads
    the canonical row.  Embedded content is accepted for replay-safe payloads
    from migration/backfill jobs, while mutable campaign fields are ignored.
    """

    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise PermanentOutboxError("p2_ad_content_required")
    return SimpleNamespace(
        content=content,
        creative_type=str(payload.get("creative_type") or "text"),
        media_url=payload.get("media_url"),
        link_url=payload.get("link_url"),
    )


async def _legacy_join_projection(db: Any, operation: Any, payload: Mapping[str, Any]) -> None:
    """Project a confirmed join into the existing membership read model.

    The projection is best effort for old rows which do not carry a local
    group id.  It never manufactures a qualification snapshot or a target.
    """

    group_id = payload.get("group_db_id") or operation.group_id
    attempt_id = payload.get("join_attempt_id")
    if attempt_id is not None:
        # The join budget row is the legacy accounting projection for the
        # reservation used by this Outbox operation.  Keep it aligned with a
        # confirmed Actor result so a successful P2 join cannot look reserved
        # forever to the legacy scheduler.
        from app.modules.acquisition.models import AutoJoinAttempt

        attempt = await db.get(AutoJoinAttempt, int(attempt_id), populate_existing=True)
        if attempt is not None and attempt.account_id == int(operation.account_id):
            now = datetime.utcnow()
            attempt.request_state = "sent"
            attempt.telegram_action_attempted = True
            attempt.request_sent_at = attempt.request_sent_at or now
            attempt.reservation_expires_at = None
            attempt.status = "success"
            attempt.reconciliation_status = "confirmed"
            attempt.reconciliation_checked_at = now
            attempt.reconciliation_next_at = None
            attempt.joined_at = attempt.joined_at or now
            attempt.reason = "p2_outbox_confirmed"
    if group_id is None:
        return
    from app.core.group.models import Group, GroupAccountMembership

    row = await db.scalar(
        select(GroupAccountMembership).where(
            GroupAccountMembership.group_id == int(group_id),
            GroupAccountMembership.account_id == int(operation.account_id),
        )
    )
    if row is not None:
        row.status = "joined"
        row.joined_at = row.joined_at or datetime.utcnow()
        row.left_at = None
        row.last_checked_at = datetime.utcnow()
    else:
        # A P2 join has no legacy membership row yet.  Materialize the same
        # read-model projection the old join path created so subsequent ad
        # candidate scans can observe the confirmed membership.
        raw_telegram_group_id = payload.get("telegram_group_id")
        if raw_telegram_group_id is None:
            group = await db.get(Group, int(group_id))
            raw_telegram_group_id = getattr(group, "group_id", None)
        if raw_telegram_group_id is None:
            return
        db.add(
            GroupAccountMembership(
                group_id=int(group_id),
                telegram_group_id=int(raw_telegram_group_id),
                account_id=int(operation.account_id),
                status="joined",
                join_method="auto_keyword_search",
                source_keyword=payload.get("source_keyword"),
                joined_at=datetime.utcnow(),
                last_checked_at=datetime.utcnow(),
            )
        )


async def _legacy_ad_projection(db: Any, operation: Any, payload: Mapping[str, Any], message_id: int) -> None:
    """Project a confirmed ad into an existing delivery log when supplied."""

    log_id = payload.get("delivery_log_id")
    if log_id is None:
        return
    from app.modules.acquisition.models import AdDeliveryLog, AdSurvivalStatus, DeliveryStatus

    row = await db.get(AdDeliveryLog, int(log_id), populate_existing=True)
    if row is None or int(row.account_id) != int(operation.account_id):
        return
    row.status = DeliveryStatus.SUCCESS.value
    row.telegram_message_id = int(message_id)
    row.sent_at = datetime.utcnow()
    row.survival_status = AdSurvivalStatus.PENDING.value
    row.survival_check_due_at = datetime.utcnow() + timedelta(minutes=2)


async def _submit_actor(
    db: Any,
    account_pool: Any,
    operation: Any,
    payload: Mapping[str, Any],
    fencing_token: str,
) -> ActorResult:
    """Execute one durable operation using the connected owner session."""

    if current_actor_account() != int(operation.account_id):
        raise UnknownExternalOutcome("p2_actor_context_required")
    account = await account_pool.acquire_by_id(
        int(operation.account_id),
        purpose=f"p2_outbox:{operation.kind}",
        allow_restricted=True,
        raise_on_lease_failure=True,
    )
    if account is None:
        raise RetryableOutboxError("p2_actor_account_unavailable")
    try:
        guard = getattr(account, "session_guard", None)
        if guard is None:
            raise UnknownExternalOutcome("p2_actor_session_guard_missing")
        await guard.assert_owned(expected_token=fencing_token)
        from app.modules.acquisition.automation import AcquisitionAutomationService

        service = AcquisitionAutomationService(db, account_pool=account_pool)
        if operation.kind == "join_group":
            target = _payload_target(operation, payload)
            username = str(payload.get("username") or target).strip().lstrip("@")
            if not username or username.startswith("-") or "://" in username:
                raise PermanentOutboxError("p2_join_public_username_required")
            group = DiscoveredGroup(
                group_id=int(payload.get("telegram_group_id") or payload.get("group_id") or 0),
                title=str(payload.get("title") or target),
                username=username,
                member_count=int(payload.get("member_count") or 0),
                is_private=False,
                source_keyword=payload.get("source_keyword"),
            )
            try:
                await service.telegram_execution.join_group(
                    account,
                    group,
                    source="p2_outbox",
                    join_reservation_key=str(
                        payload.get("join_reservation_key") or operation.budget_reservation_id
                    ),
                )
            except TelegramJoinRequestPendingError as exc:
                raise UnknownExternalOutcome(str(exc)) from exc
            except TelegramSendPreflightError as exc:
                # A preflight rejection proves the provider write was not
                # attempted. Replaying it cannot make the immutable snapshot
                # valid, so require a new qualification/operation instead of
                # burning Outbox attempts.
                raise PermanentOutboxError(str(exc)) from exc
            await guard.assert_owned(expected_token=fencing_token)
            await _legacy_join_projection(db, operation, payload)
            return ActorResult(
                "confirmed",
                provider_request_id=f"p2:{operation.id}:join",
                receipt={"membership": True, "target_key": target},
                actor_generation=fencing_token,
            )

        if operation.kind == "send_ad":
            raw_group_id = payload.get("telegram_group_id") or payload.get("group_id")
            if raw_group_id is None:
                raise PermanentOutboxError("p2_ad_telegram_group_id_required")
            group_id = int(raw_group_id)
            creative = None
            if payload.get("creative_id") is not None:
                from app.modules.acquisition.models import AdCreative

                stored = await db.get(AdCreative, int(payload["creative_id"]))
                if stored is None or not stored.enabled:
                    raise PermanentOutboxError("p2_ad_creative_unavailable")
                creative = stored
            if creative is None:
                creative = _creative_from_payload(payload)
            reservation_token = str(
                payload.get("reservation_token") or operation.budget_reservation_id
            )
            try:
                message_id = await service.telegram_execution.send_ad(
                    account,
                    await service._ad_send_target(group_id),
                    service._render_ad_content(creative),
                    media_url=getattr(creative, "media_url", None),
                    source="p2_outbox",
                    delivery_policy=str(payload.get("delivery_policy") or "growth"),
                    reservation_token=reservation_token,
                )
            except TelegramSendDeferredError as exc:
                raise RetryableOutboxError(str(exc)) from exc
            except TelegramSendPreflightError as exc:
                raise PermanentOutboxError(str(exc)) from exc
            if message_id is None:
                raise UnknownExternalOutcome("p2_ad_message_id_missing")
            await guard.assert_owned(expected_token=fencing_token)
            await _legacy_ad_projection(db, operation, payload, int(message_id))
            return ActorResult(
                "confirmed",
                provider_request_id=(
                    str(payload.get("provider_request_id") or "")
                    or f"p2:{operation.id}:ad:{int(message_id)}"
                ),
                receipt={"message_id": int(message_id), "telegram_group_id": group_id},
                survival_due_at=None,
                actor_generation=fencing_token,
            )
        raise PermanentOutboxError("p2_operation_kind_invalid")
    finally:
        await account_pool.release(account)


async def execute_owner_outbox(
    db: Any,
    account_id: int,
    kind: str,
    *,
    account_pool: Any,
) -> tuple[dict[str, Any], int]:
    """Consume one P2 Outbox row while running inside the account Actor.

    The default shadow flag is fail-closed: it leaves durable rows pending so
    the legacy path remains the only Telegram side effect.  Active mode still
    requires the feature flag and the live Redis session generation.
    """

    if kind not in {"join_group", "send_ad"}:
        raise ValueError("p2_outbox_kind_invalid")
    if current_actor_account() != int(account_id):
        return {"processed": 0, "reason": "p2_actor_context_required"}, 30
    if not settings.p2_outbox_enabled_for(kind, int(account_id)):
        return {"processed": 0, "reason": "p2_outbox_disabled"}, 60
    if settings.P2_SHADOW_MODE:
        return {"processed": 0, "reason": "p2_shadow_mode"}, 60
    if account_pool is None or not account_pool.peek_connected(int(account_id)):
        return {"processed": 0, "reason": "p2_actor_not_connected"}, 30

    redis = await get_redis()
    token = _decode(await redis.get(f"vanguard:telegram:lease:{int(account_id)}"))
    owner = _decode(await redis.get(f"{OWNER_PREFIX}{int(account_id)}"))
    if not token or owner != PROCESS_ID:
        return {"processed": 0, "reason": "p2_actor_lease_missing"}, 30
    if kind == "send_ad":
        # The owner Actor first materializes the current DB candidates.  This
        # keeps the scheduler's P2 path useful even when no legacy worker has
        # pre-created rows, while still guaranteeing that only the Actor can
        # consume the resulting Outbox.
        produced = await produce_ad_outbox(
            db,
            int(account_id),
            account_pool=account_pool,
            limit=settings.P2_OUTBOX_BATCH_SIZE,
        )
    else:
        produced = await produce_join_outbox(
            db,
            int(account_id),
            account_pool=account_pool,
            limit=settings.P2_OUTBOX_BATCH_SIZE,
        )
    actor_owner = f"p2-actor:{PROCESS_ID}:{int(account_id)}"
    row = await dispatch_outbox_once(
        db,
        owner=actor_owner,
        fencing_token=token,
        account_id=int(account_id),
        actor_submitter=lambda operation, payload, fence: _submit_actor(
            db, account_pool, operation, payload, fence
        ),
    )
    if row is None:
        return {
            "processed": 0,
            "reason": "p2_outbox_empty",
            "produced": produced,
        }, 60
    return {
        "processed": 1,
        "outbox_id": int(row.id),
        "state": row.state,
        "produced": produced,
    }, 30


__all__ = ["execute_owner_outbox", "produce_ad_outbox", "produce_join_outbox"]
