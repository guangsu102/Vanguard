"""Idempotent local business projection; no Telegram calls or chat replay."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.worker_status import TelegramEventInbox


async def enqueue_fact(db: Any, account_id: int, fact: dict) -> None:
    kind = fact["kind"]
    # Definitive deletion facts have one lifetime identity. Cues are coalesced
    # in SQLite, then bucketed to keep repeated updates from producing work.
    identity = [account_id, fact["key"]]
    if kind == "evidence_candidate":
        identity.append(fact["message_id"])
    if kind == "rules":
        identity.append(sorted(fact.get("message_ids", [])))
    if kind == "deletion_candidates":
        identity.append(hashlib.sha256(json.dumps(sorted(fact["ids"])).encode()).hexdigest())
        identity.append(bool(fact.get("overflow")))
    elif kind != "deleted":
        identity.append(int(fact["at"] // 300))
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    await db.execute(
        insert(TelegramEventInbox)
        .values(
            account_id=account_id,
            event_key=key,
            kind="listener_fact",
            chat_id=fact["peer"],
            payload_json=json.dumps(fact),
            state="pending",
            next_attempt_at=datetime.utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["account_id", "event_key"])
    )


async def apply_fact(db: Any, account_id: int, fact: dict) -> None:
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.adaptive_frequency import (
        canonical,
        payload,
        queue_deleted_observation,
    )
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.qualification_identity import identity_aliases, peer_identity

    peer, kind = fact["peer"], fact["kind"]
    if kind in {"deleted", "deletion_candidates", "ad_gap"}:
        if kind == "deleted":
            ids = [fact["message_id"]]
        elif kind == "deletion_candidates" and not fact.get("overflow"):
            ids = fact["ids"]
        else:
            # Lost deletion callbacks are repaired only against our latest
            # successful ad, never by reading or replaying group history.
            logs = (
                await db.scalars(
                    select(AdDeliveryLog)
                    .where(
                        AdDeliveryLog.account_id == account_id,
                        AdDeliveryLog.status == "success",
                        AdDeliveryLog.telegram_group_id.in_(identity_aliases(peer_identity(peer))),
                    )
                    .order_by(AdDeliveryLog.sent_at.desc())
                    .limit(20)
                )
            ).all()
            log = next(
                (
                    log
                    for log in logs
                    if canonical(log.telegram_group_id, payload(log.qualification_context_json))
                    == peer
                ),
                None,
            )
            if log is None:
                return
            ids = [log.telegram_message_id]
        await queue_deleted_observation(
            db, account_id, SimpleNamespace(chat_id=peer, deleted_ids=ids),
            confirmed=(kind == "deleted" or (kind == "deletion_candidates" and not fact.get("overflow"))),
        )
        return
    members = (
        await db.scalars(
            select(GroupAccountMembership)
            .where(
                GroupAccountMembership.account_id == account_id,
                GroupAccountMembership.telegram_group_id.in_(identity_aliases(peer_identity(peer))),
                GroupAccountMembership.status.in_(["joined", "pending"]),
            )
            .with_for_update(of=GroupAccountMembership)
        )
    ).all()
    from app.modules.acquisition.qualification_service import (
        enqueue_membership_review,
        latest_review,
    )

    now = datetime.utcnow()
    occurred = datetime.utcfromtimestamp(fact["at"])
    for member in members:
        if member.joined_at is None or occurred < member.joined_at - timedelta(seconds=30):
            continue
        if fact.get("joined_at") and fact["joined_at"] != member.joined_at.isoformat():
            continue
        if (member.ad_status == "paused"
                or member.review_status in {"owned_group_excluded", "manual_required", "exit_pending"}):
            continue
        if member.status == "pending" and kind != "membership":
            continue  # A rules/gap cue is not proof of an accepted join.
        if kind == "evidence_candidate":
            from app.modules.acquisition.callback_evidence import save_hint
            changed = await save_hint(db, member, fact, now)
            row = await latest_review(db, member.id, include_pending=True)
            if (changed and row is not None and row.state == "completed"
                    and row.decision == "observe" and row.next_retry_at is None
                    and row.membership_joined_at == member.joined_at
                    and payload(row.evidence_json).get("review_trigger") == "evidence_changed"
                    and member.ad_status != "paused"
                    and member.review_status not in {"manual_required", "exit_pending"}):
                row.state = "queued"
                from app.modules.acquisition.evidence_progress import date
                # A fresh promotional message cannot yet prove 24h survival.
                # Keep its hint and wake once it can supply that missing fact.
                observed = date(fact.get("message_date")) or now
                row.next_retry_at = max(now, (row.checked_at or now) + timedelta(hours=2),
                                        observed + timedelta(hours=24))
                evidence = payload(row.evidence_json)
                evidence["collection_needs_refresh"] = "ordinary_ad_hint"
                row.evidence_json = json.dumps(evidence, ensure_ascii=False)
                member.review_next_at = row.next_retry_at
            continue
        if kind == "verification" and (
            occurred < now - timedelta(minutes=15)
            or member.review_status == "approved"
            or member.joined_at < now - timedelta(hours=48)
        ):
            continue
        row = await latest_review(db, member.id, include_pending=True)
        if kind == "gap" and row is not None and row.decision not in {"allowed", "trial"}:
            # A missing update range supplies no new qualification evidence.
            # Keep pending work and its backoff; do not resurrect a rejection
            # or an exhausted observation on every channel synchronization.
            continue
        if kind == "rules" and row is not None:
            from app.modules.acquisition.evidence_progress import date
            collected = date(payload(row.evidence_json).get("collected_at"))
            if collected is not None and occurred <= collected:
                continue  # Already covered by the initial collection, not a new change.
        if kind in {"membership", "rules", "gap"} and row is not None:
            from app.modules.acquisition.qualification_events import request
            source_token = hashlib.sha256(json.dumps(fact, sort_keys=True).encode()).hexdigest()
            # Preserve the grant and explicit operator holds. The event itself
            # blocks the local send boundary until its exact generation settles.
            if row.decision in {"allowed", "trial"}:
                if member.ad_status == "paused" or member.review_status in {"manual_required", "exit_pending"}:
                    continue
                await request(db, member, kind, message_ids=fact.get("message_ids", []), source_token=source_token)
                continue
            await request(db, member, kind, message_ids=fact.get("message_ids", []), source_token=source_token)
        if row is None:
            await enqueue_membership_review(
                db, member, batch_id=f"listener:{member.id}:{member.joined_at.isoformat()}"
            )
        elif row.membership_joined_at == member.joined_at and row.state != "running":
            # A callback is a durable cue, not permission to bypass a read wait.
            due = row.next_retry_at or now
            row.state, row.next_retry_at = "queued", due
        if kind == "membership":
            member.ad_status, member.review_status = "blocked", "initial_pending"
        member.review_next_at = row.next_retry_at if row is not None else now
