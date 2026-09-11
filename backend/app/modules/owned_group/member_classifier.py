"""Pure member classification and presence merge for owned groups.

This module intentionally has no SQLAlchemy, FastAPI, Redis, Telegram or LLM
imports.  It consumes already-sanitised candidates and produces the single
deduplicated read representation used by both stage-five GET endpoints.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

_SOURCE_ORDER = {
    "asset_owner": 0,
    "owned_group_membership": 1,
    "guardian_binding": 2,
    "group_account_membership": 3,
    "guardian_observation": 4,
    "user_profile": 5,
}
_PRESENCE_SOURCE_ORDER = {
    "guardian_observation": 3,
    "owned_group_membership": 2,
    "group_account_membership": 1,
}
_OWNED_PRESENT = {"member_verified", "admin_verified", "skipped_already_member"}
_OWNED_PENDING = {
    "pending",
    "in_progress",
    "invite_sent",
    "waiting_approval",
    "admin_promoting",
}
_OWNED_FAILED = {"failed_transient", "failed_permanent", "cancelled"}
_GROUP_LEFT = {"left", "banned", "rejected"}
_GROUP_PENDING = {"pending", "joining"}


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _get(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _positive_int(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


def _clean_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized[:limit] or None


def _username(value: Any) -> str | None:
    normalized = _clean_text(value, limit=121)
    if normalized:
        normalized = normalized.lstrip("@")[:120]
    return normalized or None


def _datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _max_datetime(values: Iterable[datetime | None]) -> datetime | None:
    candidates = [value for value in values if value is not None]
    return max(candidates, key=_timestamp) if candidates else None


@dataclass(slots=True, frozen=True)
class PresenceFact:
    source: str
    raw_status: str | None
    observed_at: datetime | None
    joined_at: datetime | None = None
    left_at: datetime | None = None
    verified_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class PresenceResult:
    presence_status: str = "unknown"
    presence_confidence: str = "unknown"
    raw_presence_status: str | None = None
    joined_at: datetime | None = None
    left_at: datetime | None = None
    last_verified_at: datetime | None = None
    last_observed_at: datetime | None = None
    quality_codes: tuple[str, ...] = ()


@dataclass(slots=True)
class MemberCandidate:
    """One sanitised source record participating in classification.

    ``relation`` is semantic (for example ``owned_membership_bot``), whereas
    ``source`` is the fixed public source name.  Sensitive ORM fields are not
    represented, so a classifier call cannot accidentally return them.
    """

    relation: str
    source: str
    source_id: int
    source_time: datetime | None = None
    account_id: int | None = None
    telegram_user_id: int | None = None
    is_bot: bool | None = None
    account_type: str | None = None
    account_display_name: str | None = None
    account_status: str | None = None
    account_risk_level: str | None = None
    risk_pause_until: datetime | None = None
    operation_mode: str | None = None
    persona_configured: bool | None = None
    persona_revision: int | None = None
    owned_bot_profile_id: int | None = None
    owned_bot_user_id: int | None = None
    owned_bot_username: str | None = None
    owned_bot_display_name: str | None = None
    parent_account_id: int | None = None
    guardian_bot_profile_id: int | None = None
    guardian_bot_user_id: int | None = None
    guardian_bot_username: str | None = None
    bot_health_status: str | None = None
    bot_sync_status: str | None = None
    bot_role: str | None = None
    display_name_snapshot: str | None = None
    username_snapshot: str | None = None
    raw_presence_status: str | None = None
    joined_at: datetime | None = None
    left_at: datetime | None = None
    last_verified_at: datetime | None = None
    last_observed_at: datetime | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None
    is_admin: bool = False
    admin_title: str | None = None
    user_id: int | None = None
    user_state: str | None = None
    warning_count: int | None = None
    muted_until: datetime | None = None
    user_profile_source_id: int | None = None
    user_profile_source_time: datetime | None = None
    quality_codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class ClassificationResult:
    members: tuple[dict[str, Any], ...]

    @property
    def unresolved_count(self) -> int:
        return sum(item["classification_status"] == "unresolved" for item in self.members)

    @property
    def conflict_count(self) -> int:
        return sum(item["classification_status"] == "conflict" for item in self.members)


def _presence_from_fact(fact: PresenceFact) -> tuple[str, str]:
    raw = str(_value(fact.raw_status) or "").strip().lower()
    if fact.source == "guardian_observation":
        if raw in {"present", "left"}:
            return raw, "observed"
        return "unknown", "unknown"
    if fact.source == "owned_group_membership":
        if raw in _OWNED_PRESENT or fact.verified_at is not None:
            return "present", "verified"
        if (fact.joined_at is not None) and raw not in _OWNED_FAILED:
            return "present", "verified"
        if raw in _OWNED_PENDING:
            return "pending", "stored"
        if raw in _OWNED_FAILED:
            return "failed", "stored"
        return "unknown", "unknown"
    if fact.source == "group_account_membership":
        if fact.left_at is not None or raw in _GROUP_LEFT:
            return "left", "stored"
        if raw == "joined":
            return "present", "stored"
        if raw in _GROUP_PENDING:
            return "pending", "stored"
        return "unknown", "unknown"
    return "unknown", "unknown"


def merge_presence(facts: Iterable[PresenceFact | Mapping[str, Any] | object]) -> PresenceResult:
    """Merge source facts by fact time, then by the fixed source precedence."""

    normalized: list[PresenceFact] = []
    for item in facts:
        if isinstance(item, PresenceFact):
            fact = item
        else:
            fact = PresenceFact(
                source=str(_get(item, "source", "")),
                raw_status=_get(item, "raw_status", _get(item, "raw_presence_status")),
                observed_at=_datetime(_get(item, "observed_at", _get(item, "source_time"))),
                joined_at=_datetime(_get(item, "joined_at")),
                left_at=_datetime(_get(item, "left_at")),
                verified_at=_datetime(_get(item, "verified_at", _get(item, "last_verified_at"))),
            )
        if fact.source in _PRESENCE_SOURCE_ORDER:
            normalized.append(fact)

    joined_at = _max_datetime(fact.joined_at for fact in normalized)
    left_at = _max_datetime(fact.left_at for fact in normalized)
    last_verified_at = _max_datetime(
        fact.verified_at for fact in normalized if fact.source == "owned_group_membership"
    )
    last_observed_at = _max_datetime(
        fact.observed_at for fact in normalized if fact.source == "guardian_observation"
    )
    if not normalized:
        return PresenceResult(
            joined_at=joined_at,
            left_at=left_at,
            last_verified_at=last_verified_at,
            last_observed_at=last_observed_at,
        )

    ranked = sorted(
        normalized,
        key=lambda fact: (
            _timestamp(fact.observed_at),
            _PRESENCE_SOURCE_ORDER[fact.source],
        ),
        reverse=True,
    )
    winner = ranked[0]
    status, confidence = _presence_from_fact(winner)
    top_time = _timestamp(winner.observed_at)
    tied_statuses = {
        _presence_from_fact(fact)[0] for fact in ranked if _timestamp(fact.observed_at) == top_time
    }
    quality_codes = ("presence_source_conflict",) if len(tied_statuses) > 1 else ()
    return PresenceResult(
        presence_status=status,
        presence_confidence=confidence,
        raw_presence_status=str(_value(winner.raw_status))
        if winner.raw_status is not None
        else None,
        joined_at=joined_at,
        left_at=left_at,
        last_verified_at=last_verified_at,
        last_observed_at=last_observed_at,
        quality_codes=quality_codes,
    )


def build_member_key(candidate: Mapping[str, Any] | object) -> str:
    """Return the stable public key for a classified member-like value."""

    status = str(_get(candidate, "classification_status", ""))
    member_kind = _get(candidate, "member_kind")
    account_id = _positive_int(_get(candidate, "account_id"))
    telegram_user_id = _positive_int(_get(candidate, "telegram_user_id"))
    if status == "conflict" and account_id and not telegram_user_id:
        return f"conflict:system_bot:account:{account_id}"
    if status == "conflict" and telegram_user_id:
        return f"conflict:telegram:{telegram_user_id}"
    if member_kind in {"system_ad_account", "system_bot"} and account_id:
        return f"{member_kind}:account:{account_id}"
    if member_kind == "real_user" and telegram_user_id:
        return f"real_user:telegram:{telegram_user_id}"
    source = str(_get(candidate, "source", "observation"))
    source = {
        "owned_group_membership": "owned_membership",
        "guardian_binding": "guardian_binding",
        "group_account_membership": "group_membership",
        "guardian_observation": "observation",
    }.get(source, source)
    if source not in {"owned_membership", "guardian_binding", "group_membership", "observation"}:
        source = "observation"
    source_id = _positive_int(_get(candidate, "source_id", _get(candidate, "record_id"))) or 1
    return f"unresolved:{source}:{source_id}"


def _source_entries(
    candidates: Iterable[MemberCandidate], *, include_user_profile: bool = False
) -> list[dict[str, Any]]:
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for candidate in candidates:
        source_id = _positive_int(candidate.source_id)
        if candidate.source not in _SOURCE_ORDER or source_id is None:
            continue
        key = (candidate.source, source_id)
        unique[key] = {
            "source": candidate.source,
            "record_id": source_id,
            "observed_at": candidate.source_time,
        }
        profile_id = _positive_int(candidate.user_profile_source_id)
        if include_user_profile and profile_id is not None:
            unique[("user_profile", profile_id)] = {
                "source": "user_profile",
                "record_id": profile_id,
                "observed_at": candidate.user_profile_source_time,
            }
    return sorted(
        unique.values(),
        key=lambda item: (_SOURCE_ORDER[item["source"]], item["record_id"]),
    )


def _candidate_presence(candidates: Iterable[MemberCandidate]) -> PresenceResult:
    facts: list[PresenceFact] = []
    for candidate in candidates:
        if candidate.source not in _PRESENCE_SOURCE_ORDER:
            continue
        facts.append(
            PresenceFact(
                source=candidate.source,
                raw_status=candidate.raw_presence_status,
                observed_at=candidate.source_time,
                joined_at=candidate.joined_at,
                left_at=candidate.left_at,
                verified_at=candidate.last_verified_at,
            )
        )
    return merge_presence(facts)


def _first(candidates: Iterable[MemberCandidate], name: str) -> Any:
    for candidate in candidates:
        value = getattr(candidate, name)
        if value is not None and value != "":
            return _value(value)
    return None


def _quality(status: str, codes: Iterable[str]) -> tuple[str, list[str]]:
    ordered = list(dict.fromkeys(code for code in codes if code))
    if status == "conflict":
        return "conflict", ordered
    if status == "unresolved" or ordered:
        return "partial", ordered
    return "reliable", ordered


def _finalize(
    candidates: list[MemberCandidate],
    *,
    asset: Mapping[str, Any] | object,
    member_kind: str | None,
    status: str,
    reason: str,
    member_key: str,
    telegram_user_id: int | None,
    account_id: int | None,
    extra_codes: Iterable[str] = (),
) -> dict[str, Any]:
    presence = _candidate_presence(candidates)
    sources = _source_entries(candidates, include_user_profile=member_kind == "real_user")
    codes = [code for candidate in candidates for code in candidate.quality_codes]
    codes.extend(presence.quality_codes)
    codes.extend(extra_codes)
    data_quality, codes = _quality(status, codes)

    observation_display = _first(
        (candidate for candidate in candidates if candidate.source == "guardian_observation"),
        "display_name_snapshot",
    )
    observation_username = _first(
        (candidate for candidate in candidates if candidate.source == "guardian_observation"),
        "username_snapshot",
    )
    if member_kind == "system_bot":
        display_name = (
            _first(candidates, "owned_bot_display_name")
            or _first(candidates, "account_display_name")
            or observation_display
            or (f"Bot #{account_id}" if account_id else "待修复 Bot")
        )
        username = (
            _first(candidates, "owned_bot_username")
            or _first(candidates, "guardian_bot_username")
            or observation_username
        )
    elif member_kind == "system_ad_account":
        display_name = (
            _first(candidates, "account_display_name")
            or observation_display
            or (f"账号 #{account_id}" if account_id else "待修复账号")
        )
        username = observation_username
    elif member_kind == "real_user":
        display_name = observation_display or (
            f"Telegram 用户 {telegram_user_id}" if telegram_user_id else "已观察用户"
        )
        username = observation_username
    else:
        display_name = observation_display or (
            f"身份冲突 {telegram_user_id}"
            if status == "conflict" and telegram_user_id
            else "待修复成员"
        )
        username = observation_username

    owner_account_id = _positive_int(_get(asset, "owner_account_id"))
    bot_roles = {
        str(_value(candidate.bot_role)).lower()
        for candidate in candidates
        if candidate.bot_role is not None
    }
    owned_admin = next((candidate for candidate in candidates if candidate.is_admin), None)
    if account_id is not None and account_id == owner_account_id:
        telegram_role, is_admin, admin_title = "owner", True, "Owner"
    elif "owner" in bot_roles:
        telegram_role, is_admin, admin_title = "owner", True, "Owner"
    elif "admin" in bot_roles or "administrator" in bot_roles:
        telegram_role, is_admin, admin_title = "administrator", True, "Administrator"
    elif owned_admin is not None:
        telegram_role = "administrator"
        is_admin = True
        admin_title = _clean_text(owned_admin.admin_title, limit=64) or "Administrator"
    elif presence.presence_status == "present":
        telegram_role, is_admin, admin_title = "member", False, None
    else:
        telegram_role, is_admin, admin_title = "unknown", False, None

    operation_mode = _first(candidates, "operation_mode")
    if operation_mode not in {"growth", "ad_only"} or member_kind != "system_ad_account":
        operation_mode = None
    persona_revision = _first(candidates, "persona_revision")
    if type(persona_revision) is not int or persona_revision < 0:
        persona_revision = None

    real_user = member_kind == "real_user"
    system_ad = member_kind == "system_ad_account"
    system_bot = member_kind == "system_bot"
    internal_bot_conflict = bool(
        status == "conflict"
        and account_id is not None
        and member_key == f"conflict:system_bot:account:{account_id}"
    )
    return {
        "member_key": member_key,
        "member_kind": member_kind,
        "classification_status": status,
        "classification_reason": reason,
        "telegram_user_id": telegram_user_id,
        "display_name": _clean_text(display_name, limit=200) or "待修复成员",
        "username": _username(username),
        "primary_source": sources[0]["source"] if sources else "guardian_observation",
        "sources": sources,
        # An internal Bot identity conflict still has one unambiguous system
        # account; retaining that ID aids repair without choosing a Telegram ID.
        # Cross-resource conflicts keep account_id null.
        "account_id": account_id if (system_ad or system_bot or internal_bot_conflict) else None,
        "owned_bot_profile_id": _positive_int(_first(candidates, "owned_bot_profile_id"))
        if system_bot
        else None,
        "guardian_bot_profile_id": _positive_int(_first(candidates, "guardian_bot_profile_id"))
        if system_bot
        else None,
        "parent_account_id": _positive_int(_first(candidates, "parent_account_id"))
        if system_bot
        else None,
        "user_id": _positive_int(_first(candidates, "user_id")) if real_user else None,
        "operation_mode": operation_mode,
        "account_status": str(_first(candidates, "account_status"))
        if (system_ad or system_bot) and _first(candidates, "account_status") is not None
        else None,
        "account_risk_level": str(_first(candidates, "account_risk_level"))
        if system_ad and _first(candidates, "account_risk_level") is not None
        else None,
        "risk_pause_until": _datetime(_first(candidates, "risk_pause_until"))
        if system_ad
        else None,
        "risk_scope": "account_global" if system_ad else ("user_global" if real_user else None),
        "persona_configured": bool(_first(candidates, "persona_configured"))
        if system_ad and _first(candidates, "persona_configured") is not None
        else None,
        "persona_revision": persona_revision if system_ad else None,
        "bot_health_status": str(_first(candidates, "bot_health_status"))
        if system_bot and _first(candidates, "bot_health_status") is not None
        else None,
        "bot_sync_status": str(_first(candidates, "bot_sync_status"))
        if system_bot and _first(candidates, "bot_sync_status") is not None
        else None,
        "telegram_role": telegram_role,
        "is_admin": is_admin,
        "admin_title": admin_title,
        "presence_status": presence.presence_status,
        "presence_confidence": presence.presence_confidence,
        "raw_presence_status": _clean_text(presence.raw_presence_status, limit=64),
        "user_state": str(_first(candidates, "user_state"))
        if real_user and _first(candidates, "user_state") is not None
        else None,
        "warning_count": _first(candidates, "warning_count") if real_user else None,
        "muted_until": _datetime(_first(candidates, "muted_until")) if real_user else None,
        "joined_at": presence.joined_at,
        "left_at": presence.left_at,
        "last_verified_at": presence.last_verified_at,
        "last_observed_at": presence.last_observed_at,
        "data_quality": data_quality,
        "quality_codes": codes,
        # Internal sort evidence.  The read-model removes this field before
        # Pydantic/wire validation; it is never part of the public contract.
        "_sort_created_at": _max_datetime(candidate.created_at for candidate in candidates),
    }


def _coerce_candidate(item: MemberCandidate | Mapping[str, Any] | object) -> MemberCandidate:
    if isinstance(item, MemberCandidate):
        return item
    values: dict[str, Any] = {}
    for name in MemberCandidate.__dataclass_fields__:
        value = _get(item, name)
        if value is not None:
            values[name] = value
    return MemberCandidate(**values)


def classify_member_candidates(
    candidates: Iterable[MemberCandidate | Mapping[str, Any] | object],
    asset: Mapping[str, Any] | object,
) -> ClassificationResult:
    """Classify, de-duplicate and merge an asset's sanitised source records."""

    items = [_coerce_candidate(candidate) for candidate in candidates]
    observations = [item for item in items if item.relation == "observation"]
    system = [item for item in items if item.relation != "observation"]
    by_account: dict[int, list[MemberCandidate]] = defaultdict(list)
    unresolved_sources: list[MemberCandidate] = []
    for candidate in system:
        account_id = _positive_int(candidate.account_id)
        if account_id is None:
            unresolved_sources.append(candidate)
        else:
            by_account[account_id].append(candidate)

    rows: list[tuple[dict[str, Any], list[MemberCandidate]]] = []
    conflicting_system_ids: set[int] = set()
    for account_id, related in sorted(by_account.items()):
        account_types = {str(_value(item.account_type)) for item in related if item.account_type}
        is_bot_relation = (
            any(item.relation in {"owned_membership_bot", "guardian_binding"} for item in related)
            or "guardian_bot" in account_types
        )
        if is_bot_relation:
            telegram_ids = {
                value
                for item in related
                for value in (
                    _positive_int(item.telegram_user_id),
                    _positive_int(item.owned_bot_user_id),
                    _positive_int(item.guardian_bot_user_id),
                )
                if value is not None
            }
            reason = (
                "owned_membership_bot"
                if any(item.relation == "owned_membership_bot" for item in related)
                else "guardian_binding_bot"
            )
            if len(telegram_ids) > 1:
                conflicting_system_ids.update(telegram_ids)
                row = _finalize(
                    related,
                    asset=asset,
                    member_kind=None,
                    status="conflict",
                    reason="system_identity_conflict",
                    member_key=f"conflict:system_bot:account:{account_id}",
                    telegram_user_id=None,
                    account_id=account_id,
                    extra_codes=("telegram_identity_conflict",),
                )
            else:
                telegram_user_id = next(iter(telegram_ids), None)
                row = _finalize(
                    related,
                    asset=asset,
                    member_kind="system_bot",
                    status="resolved",
                    reason=reason,
                    member_key=f"system_bot:account:{account_id}",
                    telegram_user_id=telegram_user_id,
                    account_id=account_id,
                    extra_codes=("identity_incomplete",) if telegram_user_id is None else (),
                )
            rows.append((row, related))
            continue

        if "promoter" in account_types:
            telegram_ids = {
                value
                for item in related
                if (value := _positive_int(item.telegram_user_id)) is not None
            }
            if len(telegram_ids) > 1:
                conflicting_system_ids.update(telegram_ids)
                for telegram_user_id in sorted(telegram_ids):
                    rows.append(
                        (
                            _finalize(
                                related,
                                asset=asset,
                                member_kind=None,
                                status="conflict",
                                reason="system_identity_conflict",
                                member_key=f"conflict:telegram:{telegram_user_id}",
                                telegram_user_id=telegram_user_id,
                                account_id=None,
                                extra_codes=("telegram_identity_conflict",),
                            ),
                            related,
                        )
                    )
                continue
            else:
                telegram_user_id = next(iter(telegram_ids), None)
                reason = (
                    "asset_owner_promoter"
                    if any(item.relation == "asset_owner" for item in related)
                    else (
                        "owned_membership_promoter"
                        if any(item.relation == "owned_membership_user" for item in related)
                        else "group_membership_promoter"
                    )
                )
                row = _finalize(
                    related,
                    asset=asset,
                    member_kind="system_ad_account",
                    status="resolved",
                    reason=reason,
                    member_key=f"system_ad_account:account:{account_id}",
                    telegram_user_id=telegram_user_id,
                    account_id=account_id,
                    extra_codes=("identity_incomplete",) if telegram_user_id is None else (),
                )
            rows.append((row, related))
            continue

        unresolved_sources.extend(related)

    # A Telegram identity shared by two different system resources is one
    # explicit conflict row, never an arbitrary minimum-id winner.
    identity_rows: dict[int, list[int]] = defaultdict(list)
    for index, (row, _related) in enumerate(rows):
        identity = _positive_int(row["telegram_user_id"])
        if identity is not None:
            identity_rows[identity].append(index)
    replaced: set[int] = set()
    conflict_rows: list[tuple[dict[str, Any], list[MemberCandidate]]] = []
    for telegram_user_id, indexes in identity_rows.items():
        if len(indexes) < 2:
            continue
        combined = [candidate for index in indexes for candidate in rows[index][1]]
        conflict_rows.append(
            (
                _finalize(
                    combined,
                    asset=asset,
                    member_kind=None,
                    status="conflict",
                    reason="system_identity_conflict",
                    member_key=f"conflict:telegram:{telegram_user_id}",
                    telegram_user_id=telegram_user_id,
                    account_id=None,
                    extra_codes=("system_identity_conflict",),
                ),
                combined,
            )
        )
        replaced.update(indexes)
    rows = [entry for index, entry in enumerate(rows) if index not in replaced] + conflict_rows

    observations_by_id: dict[int, list[MemberCandidate]] = defaultdict(list)
    for observation in observations:
        identity = _positive_int(observation.telegram_user_id)
        if identity is None:
            unresolved_sources.append(observation)
        else:
            observations_by_id[identity].append(observation)

    system_by_identity: dict[int, int] = {}
    internal_conflict_by_identity: dict[int, int] = {}
    for index, (row, _related) in enumerate(rows):
        identity = _positive_int(row["telegram_user_id"])
        if identity is not None:
            system_by_identity[identity] = index
        elif row["classification_status"] == "conflict":
            for candidate in _related:
                for candidate_identity in (
                    _positive_int(candidate.telegram_user_id),
                    _positive_int(candidate.owned_bot_user_id),
                    _positive_int(candidate.guardian_bot_user_id),
                ):
                    if candidate_identity is not None:
                        internal_conflict_by_identity[candidate_identity] = index

    consumed_observations: set[int] = set()
    for identity, observations_for_id in observations_by_id.items():
        row_index = system_by_identity.get(identity)
        if row_index is None:
            row_index = internal_conflict_by_identity.get(identity)
        if row_index is None:
            continue
        row, related = rows[row_index]
        combined = related + observations_for_id
        if identity in conflicting_system_ids and row["classification_status"] == "conflict":
            rows[row_index] = (
                _finalize(
                    combined,
                    asset=asset,
                    member_kind=None,
                    status="conflict",
                    reason=row["classification_reason"],
                    member_key=row["member_key"],
                    telegram_user_id=row["telegram_user_id"],
                    account_id=row["account_id"],
                    extra_codes=row["quality_codes"],
                ),
                combined,
            )
            consumed_observations.add(identity)
            continue
        # A valid system relation is authoritative for classification.  The
        # observation is absorbed for public profile/presence evidence even if
        # its historical is_bot snapshot disagrees; the fixed precedence is
        # system_bot > system_ad_account > real_user.
        row = _finalize(
            combined,
            asset=asset,
            member_kind=row["member_kind"],
            status=row["classification_status"],
            reason=row["classification_reason"],
            member_key=row["member_key"],
            telegram_user_id=identity,
            account_id=row["account_id"],
            extra_codes=row["quality_codes"],
        )
        rows[row_index] = (row, combined)
        consumed_observations.add(identity)

    for identity, observed in sorted(observations_by_id.items()):
        if identity in consumed_observations:
            continue
        bool_values = {item.is_bot for item in observed if type(item.is_bot) is bool}
        if len(bool_values) > 1:
            row = _finalize(
                observed,
                asset=asset,
                member_kind=None,
                status="conflict",
                reason="system_identity_conflict",
                member_key=f"conflict:telegram:{identity}",
                telegram_user_id=identity,
                account_id=None,
                extra_codes=("telegram_identity_conflict",),
            )
        elif not bool_values:
            row = _finalize(
                observed,
                asset=asset,
                member_kind=None,
                status="unresolved",
                reason="identity_incomplete",
                member_key=f"unresolved:observation:{observed[0].source_id}",
                telegram_user_id=identity,
                account_id=None,
                extra_codes=("identity_incomplete",),
            )
        elif True in bool_values:
            row = _finalize(
                observed,
                asset=asset,
                member_kind=None,
                status="unresolved",
                reason="third_party_bot_unmanaged",
                member_key=f"unresolved:observation:{observed[0].source_id}",
                telegram_user_id=identity,
                account_id=None,
                extra_codes=("third_party_bot_unmanaged",),
            )
        else:
            row = _finalize(
                observed,
                asset=asset,
                member_kind="real_user",
                status="resolved",
                reason="observed_real_user",
                member_key=f"real_user:telegram:{identity}",
                telegram_user_id=identity,
                account_id=None,
            )
        rows.append((row, observed))

    for candidate in unresolved_sources:
        source_key = {
            "owned_group_membership": "owned_membership",
            "guardian_binding": "guardian_binding",
            "group_account_membership": "group_membership",
            "guardian_observation": "observation",
        }.get(candidate.source, "observation")
        rows.append(
            (
                _finalize(
                    [candidate],
                    asset=asset,
                    member_kind=None,
                    status="unresolved",
                    reason="identity_incomplete",
                    member_key=f"unresolved:{source_key}:{candidate.source_id}",
                    telegram_user_id=_positive_int(candidate.telegram_user_id),
                    account_id=None,
                    extra_codes=(
                        "identity_incomplete"
                        if candidate.relation == "observation"
                        else "source_record_missing",
                    ),
                ),
                [candidate],
            )
        )

    # The read model owns user-facing sorting.  A deterministic key here keeps
    # classification stable even when a database returns rows in a new order.
    members = tuple(sorted((row for row, _ in rows), key=lambda row: row["member_key"]))
    return ClassificationResult(members=members)


__all__ = [
    "ClassificationResult",
    "MemberCandidate",
    "PresenceFact",
    "PresenceResult",
    "build_member_key",
    "classify_member_candidates",
    "merge_presence",
]
