"""Project Guardian updates into the partial owned-group member fact table.

The projector is deliberately fail-open for Guardian.  It owns a short database
session per Telegram update, commits before returning, and never receives or
persists message bodies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.modules.owned_group.models_extra import OwnedGroupMemberObservation

logger = structlog.get_logger().bind(module="owned_group_member_observation")

UpdateKind = Literal["message", "edited_message"]
ObservationEventType = Literal[
    "message", "new_chat_member", "left_chat_member"
]
PresenceStatus = Literal["present", "left"]
ObservationWriteStatus = Literal[
    "inserted",
    "updated",
    "ignored_stale",
    "source_bot_mismatch",
    "identity_type_conflict",
    "write_conflict",
]

_BIGINT_MAX = 9_223_372_036_854_775_807
_MEDIA_FIELDS = frozenset(
    {
        "animation",
        "audio",
        "contact",
        "dice",
        "document",
        "game",
        "giveaway",
        "invoice",
        "location",
        "paid_media",
        "photo",
        "poll",
        "sticker",
        "story",
        "venue",
        "video",
        "video_note",
        "voice",
    }
)
_SERVICE_MESSAGE_FIELDS = frozenset(
    {
        "boost_added",
        "channel_chat_created",
        "chat_background_set",
        "chat_shared",
        "connected_website",
        "delete_chat_photo",
        "forum_topic_closed",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_reopened",
        "general_forum_topic_hidden",
        "general_forum_topic_unhidden",
        "giveaway_completed",
        "giveaway_created",
        "giveaway_winners",
        "group_chat_created",
        "left_chat_member",
        "message_auto_delete_timer_changed",
        "migrate_from_chat_id",
        "migrate_to_chat_id",
        "new_chat_members",
        "new_chat_photo",
        "new_chat_title",
        "passport_data",
        "pinned_message",
        "proximity_alert_triggered",
        "refunded_payment",
        "successful_payment",
        "supergroup_chat_created",
        "users_shared",
        "video_chat_ended",
        "video_chat_participants_invited",
        "video_chat_scheduled",
        "video_chat_started",
        "web_app_data",
        "write_access_allowed",
    }
)


@dataclass(frozen=True, slots=True)
class MemberObservationFact:
    group_asset_id: int
    telegram_user_id: int
    is_bot: bool
    username_snapshot: str | None
    display_name_snapshot: str | None
    presence_status: PresenceStatus
    event_type: ObservationEventType
    source_bot_account_id: int
    update_id: int
    event_time: datetime


@dataclass(frozen=True, slots=True)
class ObservationExtraction:
    facts: tuple[MemberObservationFact, ...]
    skipped_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservationWriteResult:
    status: ObservationWriteStatus
    event_type: ObservationEventType
    telegram_user_id: int


@dataclass(frozen=True, slots=True)
class ObservationProjectionSummary:
    candidate_count: int
    results: tuple[ObservationWriteResult, ...] = ()
    skipped_reasons: tuple[str, ...] = ()
    failed: bool = False
    failure_reason: str | None = None


def _bounded_positive_id(value: Any) -> int | None:
    if type(value) is not int or not 0 < value <= _BIGINT_MAX:
        return None
    return value


def _bounded_update_id(value: Any) -> int | None:
    if type(value) is not int or not 0 <= value <= _BIGINT_MAX:
        return None
    return value


def _event_time(value: Any) -> datetime | None:
    if type(value) is not int:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _snapshot_text(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized[:max_length] or None


def _public_snapshots(identity: dict[str, Any]) -> tuple[str | None, str | None]:
    username = _snapshot_text(identity.get("username"), 121)
    if username is not None:
        username = username.lstrip("@")[:120] or None
    name_parts = [
        part
        for part in (
            _snapshot_text(identity.get("first_name"), 200),
            _snapshot_text(identity.get("last_name"), 200),
        )
        if part
    ]
    display_name = " ".join(name_parts)[:200] or None
    return username, display_name


def _fact_from_identity(
    identity: Any,
    *,
    group_asset_id: int,
    source_bot_account_id: int,
    update_id: int,
    event_time: datetime,
    event_type: ObservationEventType,
) -> tuple[MemberObservationFact | None, str | None]:
    if not isinstance(identity, dict):
        return None, "telegram_identity_invalid"
    telegram_user_id = _bounded_positive_id(identity.get("id"))
    if telegram_user_id is None:
        return None, "telegram_user_id_invalid"
    is_bot = identity.get("is_bot")
    if type(is_bot) is not bool:
        return None, "identity_type_invalid"
    username, display_name = _public_snapshots(identity)
    is_left = event_type == "left_chat_member"
    return (
        MemberObservationFact(
            group_asset_id=group_asset_id,
            telegram_user_id=telegram_user_id,
            is_bot=is_bot,
            username_snapshot=username,
            display_name_snapshot=display_name,
            presence_status="left" if is_left else "present",
            event_type=event_type,
            source_bot_account_id=source_bot_account_id,
            update_id=update_id,
            event_time=event_time,
        ),
        None,
    )


def extract_member_observation_facts(
    message: Any,
    *,
    group_asset_id: Any,
    source_bot_account_id: Any,
    update_id: Any,
    update_kind: Any,
) -> ObservationExtraction:
    """Validate and extract facts without persisting message content."""

    if update_kind == "edited_message":
        return ObservationExtraction((), ("edited_message",))
    if update_kind != "message" or not isinstance(message, dict):
        return ObservationExtraction((), ("update_kind_invalid",))

    asset_id = _bounded_positive_id(group_asset_id)
    if asset_id is None:
        return ObservationExtraction((), ("asset_id_invalid",))
    bot_account_id = _bounded_positive_id(source_bot_account_id)
    if bot_account_id is None:
        return ObservationExtraction((), ("source_bot_account_id_invalid",))
    accepted_update_id = _bounded_update_id(update_id)
    if accepted_update_id is None:
        return ObservationExtraction((), ("update_id_invalid",))
    observed_at = _event_time(message.get("date"))
    if observed_at is None:
        return ObservationExtraction((), ("event_time_invalid",))

    facts: list[MemberObservationFact] = []
    skipped: list[str] = []
    service_fact_seen = False

    new_members = message.get("new_chat_members")
    if isinstance(new_members, list):
        service_fact_seen = True
        for member in new_members:
            fact, reason = _fact_from_identity(
                member,
                group_asset_id=asset_id,
                source_bot_account_id=bot_account_id,
                update_id=accepted_update_id,
                event_time=observed_at,
                event_type="new_chat_member",
            )
            if fact is not None:
                facts.append(fact)
            elif reason is not None:
                skipped.append(reason)

    if "left_chat_member" in message:
        service_fact_seen = True
        fact, reason = _fact_from_identity(
            message.get("left_chat_member"),
            group_asset_id=asset_id,
            source_bot_account_id=bot_account_id,
            update_id=accepted_update_id,
            event_time=observed_at,
            event_type="left_chat_member",
        )
        if fact is not None:
            facts.append(fact)
        elif reason is not None:
            skipped.append(reason)

    is_service_message = service_fact_seen or any(
        field in message for field in _SERVICE_MESSAGE_FIELDS
    )
    has_body = any(
        isinstance(message.get(field), str) and bool(message[field])
        for field in ("text", "caption")
    )
    has_media = any(bool(message.get(field)) for field in _MEDIA_FIELDS)
    if not is_service_message and (has_body or has_media):
        sender = message.get("from")
        if sender is None and message.get("sender_chat") is not None:
            skipped.append("anonymous_sender")
        else:
            fact, reason = _fact_from_identity(
                sender,
                group_asset_id=asset_id,
                source_bot_account_id=bot_account_id,
                update_id=accepted_update_id,
                event_time=observed_at,
                event_type="message",
            )
            if fact is not None:
                facts.append(fact)
            elif reason is not None:
                skipped.append(reason)

    if not facts and not skipped:
        skipped.append("no_member_fact")
    return ObservationExtraction(tuple(facts), tuple(skipped))


def _insert_statement(fact: MemberObservationFact, dialect_name: str) -> Any:
    values = {
        "group_asset_id": fact.group_asset_id,
        "telegram_user_id": fact.telegram_user_id,
        "is_bot": fact.is_bot,
        "username_snapshot": fact.username_snapshot,
        "display_name_snapshot": fact.display_name_snapshot,
        "presence_status": fact.presence_status,
        "last_event_type": fact.event_type,
        "source_bot_account_id": fact.source_bot_account_id,
        "last_update_id": fact.update_id,
        "first_observed_at": fact.event_time,
        "last_observed_at": fact.event_time,
        "joined_at": fact.event_time if fact.event_type == "new_chat_member" else None,
        "left_at": fact.event_time if fact.event_type == "left_chat_member" else None,
    }
    if dialect_name == "postgresql":
        statement = postgresql_insert(OwnedGroupMemberObservation).values(**values)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(OwnedGroupMemberObservation).values(**values)
    else:
        raise RuntimeError("member observation requires PostgreSQL or SQLite")
    return statement.on_conflict_do_nothing(
        index_elements=["group_asset_id", "telegram_user_id"]
    )


def _conditional_update_statement(fact: MemberObservationFact) -> Any:
    values: dict[str, Any] = {
        "username_snapshot": fact.username_snapshot,
        "display_name_snapshot": fact.display_name_snapshot,
        "presence_status": fact.presence_status,
        "last_event_type": fact.event_type,
        "last_update_id": fact.update_id,
        "last_observed_at": fact.event_time,
        "left_at": fact.event_time if fact.event_type == "left_chat_member" else None,
        "updated_at": func.now(),
    }
    if fact.event_type == "new_chat_member":
        values["joined_at"] = fact.event_time
    return (
        update(OwnedGroupMemberObservation)
        .where(
            OwnedGroupMemberObservation.group_asset_id == fact.group_asset_id,
            OwnedGroupMemberObservation.telegram_user_id == fact.telegram_user_id,
            OwnedGroupMemberObservation.source_bot_account_id
            == fact.source_bot_account_id,
            OwnedGroupMemberObservation.is_bot == fact.is_bot,
            OwnedGroupMemberObservation.last_update_id < fact.update_id,
        )
        .values(**values)
    )


async def upsert_member_observation(
    db: AsyncSession,
    fact: MemberObservationFact,
) -> ObservationWriteResult:
    """Atomically insert or compare-and-swap one observation fact."""

    dialect_name = db.get_bind().dialect.name
    inserted = await db.execute(_insert_statement(fact, dialect_name))
    if int(inserted.rowcount or 0) > 0:
        return ObservationWriteResult("inserted", fact.event_type, fact.telegram_user_id)

    changed = await db.execute(_conditional_update_statement(fact))
    if int(changed.rowcount or 0) > 0:
        return ObservationWriteResult("updated", fact.event_type, fact.telegram_user_id)

    current = (
        await db.execute(
            select(
                OwnedGroupMemberObservation.source_bot_account_id,
                OwnedGroupMemberObservation.is_bot,
                OwnedGroupMemberObservation.last_update_id,
            ).where(
                OwnedGroupMemberObservation.group_asset_id == fact.group_asset_id,
                OwnedGroupMemberObservation.telegram_user_id == fact.telegram_user_id,
            )
        )
    ).one_or_none()
    if current is None:
        status: ObservationWriteStatus = "write_conflict"
    elif current.source_bot_account_id != fact.source_bot_account_id:
        status = "source_bot_mismatch"
    elif current.is_bot is not fact.is_bot:
        status = "identity_type_conflict"
    elif int(current.last_update_id) >= fact.update_id:
        status = "ignored_stale"
    else:
        status = "write_conflict"
    return ObservationWriteResult(status, fact.event_type, fact.telegram_user_id)


def _safe_log(level: str, event: str, **fields: Any) -> None:
    try:
        getattr(logger, level)(
            event,
            **{key: value for key, value in fields.items() if value is not None},
        )
    except Exception:
        return


def _record_skipped(
    *,
    asset_id: Any,
    core_group_id: int | None,
    bot_account_id: Any,
    update_id: Any,
    reason_code: str,
    correlation_id: str,
) -> None:
    fields = {
        "asset_id": asset_id if type(asset_id) is int else None,
        "core_group_id": core_group_id,
        "bot_account_id": bot_account_id if type(bot_account_id) is int else None,
        "update_id": update_id if type(update_id) is int else None,
        "event_type": "unknown",
        "result": "skipped",
        "reason_code": reason_code,
        "duration_ms": 0,
        "candidate_count": 0,
        "correlation_id": correlation_id,
    }
    _safe_log("info", "owned_group_member_observation_skipped", **fields)
    _safe_log(
        "info",
        "owned_group_member_observation_total",
        value=1,
        event_type="unknown",
        result="skipped",
    )


def _record_write_result(
    result: ObservationWriteResult,
    *,
    asset_id: int,
    core_group_id: int | None,
    bot_account_id: int,
    update_id: int,
    duration_ms: int,
    candidate_count: int,
    correlation_id: str,
) -> None:
    if result.status in {"inserted", "updated"}:
        event = "owned_group_member_observation_upserted"
        metric_result = result.status
        reason_code = None
    elif result.status in {
        "ignored_stale",
        "source_bot_mismatch",
        "identity_type_conflict",
    }:
        event = (
            "owned_group_member_observation_stale_ignored"
            if result.status == "ignored_stale"
            else "owned_group_member_observation_skipped"
        )
        metric_result = "ignored" if result.status == "ignored_stale" else "skipped"
        reason_code = result.status
        _safe_log(
            "info",
            "owned_group_member_observation_stale_total",
            value=1,
            reason=result.status,
        )
    elif result.status == "write_conflict":
        event = "owned_group_member_observation_failed"
        metric_result = "failed"
        reason_code = result.status
        _safe_log(
            "info",
            "owned_group_member_observation_failures_total",
            value=1,
            reason=result.status,
        )
    else:
        event = "owned_group_member_observation_skipped"
        metric_result = "skipped"
        reason_code = result.status
    _safe_log(
        "info",
        event,
        asset_id=asset_id,
        core_group_id=core_group_id,
        bot_account_id=bot_account_id,
        update_id=update_id,
        event_type=result.event_type,
        result=metric_result,
        reason_code=reason_code,
        duration_ms=max(0, duration_ms),
        candidate_count=candidate_count,
        correlation_id=correlation_id,
    )
    _safe_log(
        "info",
        "owned_group_member_observation_total",
        value=1,
        event_type=result.event_type,
        result=metric_result,
    )


def _record_failure(
    facts: tuple[MemberObservationFact, ...],
    *,
    asset_id: Any,
    core_group_id: int | None,
    bot_account_id: Any,
    update_id: Any,
    duration_ms: int,
    reason_code: str,
    correlation_id: str,
) -> None:
    _safe_log(
        "warning",
        "owned_group_member_observation_failed",
        asset_id=asset_id if type(asset_id) is int else None,
        core_group_id=core_group_id,
        bot_account_id=bot_account_id if type(bot_account_id) is int else None,
        update_id=update_id if type(update_id) is int else None,
        event_type=facts[0].event_type if len(facts) == 1 else "multiple",
        result="failed",
        reason_code=reason_code,
        duration_ms=max(0, duration_ms),
        candidate_count=len(facts),
        correlation_id=correlation_id,
    )
    _safe_log(
        "info",
        "owned_group_member_observation_failures_total",
        value=1,
        reason=reason_code,
    )


async def project_owned_group_member_observations(
    message: Any,
    *,
    group_asset_id: Any,
    core_group_id: int | None,
    source_bot_account_id: Any,
    update_id: Any,
    update_kind: Any,
) -> ObservationProjectionSummary:
    """Project one Telegram update in its own committed, fail-open transaction."""

    correlation_id = (
        f"guardian-observation:{source_bot_account_id}:{update_id}"
    )
    extraction = extract_member_observation_facts(
        message,
        group_asset_id=group_asset_id,
        source_bot_account_id=source_bot_account_id,
        update_id=update_id,
        update_kind=update_kind,
    )
    for reason in extraction.skipped_reasons:
        _record_skipped(
            asset_id=group_asset_id,
            core_group_id=core_group_id,
            bot_account_id=source_bot_account_id,
            update_id=update_id,
            reason_code=reason,
            correlation_id=correlation_id,
        )
    if not extraction.facts:
        return ObservationProjectionSummary(
            candidate_count=0,
            skipped_reasons=extraction.skipped_reasons,
        )

    started = time.perf_counter()
    results: list[ObservationWriteResult] = []
    for attempt in range(2):
        try:
            async with get_db_session() as db:
                results = [
                    await upsert_member_observation(db, fact)
                    for fact in extraction.facts
                ]
            break
        except OperationalError:
            if attempt == 0:
                continue
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_failure(
                extraction.facts,
                asset_id=group_asset_id,
                core_group_id=core_group_id,
                bot_account_id=source_bot_account_id,
                update_id=update_id,
                duration_ms=duration_ms,
                reason_code="database_transient_error",
                correlation_id=correlation_id,
            )
            return ObservationProjectionSummary(
                candidate_count=len(extraction.facts),
                skipped_reasons=extraction.skipped_reasons,
                failed=True,
                failure_reason="database_transient_error",
            )
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _record_failure(
                extraction.facts,
                asset_id=group_asset_id,
                core_group_id=core_group_id,
                bot_account_id=source_bot_account_id,
                update_id=update_id,
                duration_ms=duration_ms,
                reason_code="database_error",
                correlation_id=correlation_id,
            )
            return ObservationProjectionSummary(
                candidate_count=len(extraction.facts),
                skipped_reasons=extraction.skipped_reasons,
                failed=True,
                failure_reason="database_error",
            )

    duration_ms = int((time.perf_counter() - started) * 1000)
    for result in results:
        _record_write_result(
            result,
            asset_id=int(group_asset_id),
            core_group_id=core_group_id,
            bot_account_id=int(source_bot_account_id),
            update_id=int(update_id),
            duration_ms=duration_ms,
            candidate_count=len(extraction.facts),
            correlation_id=correlation_id,
        )
    return ObservationProjectionSummary(
        candidate_count=len(extraction.facts),
        results=tuple(results),
        skipped_reasons=extraction.skipped_reasons,
    )


__all__ = [
    "MemberObservationFact",
    "ObservationExtraction",
    "ObservationProjectionSummary",
    "ObservationWriteResult",
    "extract_member_observation_facts",
    "project_owned_group_member_observations",
    "upsert_member_observation",
]
