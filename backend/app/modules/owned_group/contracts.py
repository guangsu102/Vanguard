"""Stable contracts for the self-owned group orchestration module.

This module deliberately contains no database or Telegram client code. It is
the shared state-machine and snapshot contract used by API, workers and tests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any


class ResourceType(StrEnum):
    USER = "user"
    BOT = "bot"


class AssetStatus(StrEnum):
    DRAFT = "draft"
    PRECHECKING = "prechecking"
    CREATING = "creating"
    READY = "ready"
    CREATE_FAILED = "create_failed"
    NEEDS_ATTENTION = "needs_attention"
    ARCHIVED = "archived"


class OperationStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    PARTIAL_COMPLETED = "partial_completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ItemStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    INVITE_SENT = "invite_sent"
    WAITING_APPROVAL = "waiting_approval"
    MEMBER_VERIFIED = "member_verified"
    ADMIN_PROMOTING = "admin_promoting"
    ADMIN_VERIFIED = "admin_verified"
    SKIPPED_ALREADY_MEMBER = "skipped_already_member"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class ReasonCode(StrEnum):
    ALREADY_MEMBER = "already_member"
    ACCOUNT_OFFLINE = "account_offline"
    SESSION_REVOKED = "session_revoked"
    ACCOUNT_COOLDOWN = "account_cooldown"
    PRIVACY_RESTRICTED = "privacy_restricted"
    NOT_MUTUAL_CONTACT = "not_mutual_contact"
    BANNED = "banned"
    NO_ADMIN_PERMISSION = "no_admin_permission"
    FLOOD_WAIT = "flood_wait"
    PEER_FLOOD = "peer_flood"
    NETWORK_TIMEOUT = "network_timeout"
    INVITE_EXPIRED = "invite_expired"
    UNKNOWN_NEEDS_RECONCILE = "unknown_needs_reconcile"


DEFAULT_OPERATION_CONFIG: dict[str, int] = {
    "batch_size": 5,
    "batch_interval_seconds": 600,
    "max_parallelism": 1,
    "max_attempts": 2,
}


_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "asset": {
        AssetStatus.DRAFT.value: {AssetStatus.PRECHECKING.value, AssetStatus.ARCHIVED.value},
        AssetStatus.PRECHECKING.value: {
            AssetStatus.CREATING.value,
            AssetStatus.DRAFT.value,
            AssetStatus.NEEDS_ATTENTION.value,
        },
        AssetStatus.CREATING.value: {
            AssetStatus.READY.value,
            AssetStatus.CREATE_FAILED.value,
            AssetStatus.NEEDS_ATTENTION.value,
        },
        AssetStatus.READY.value: {AssetStatus.NEEDS_ATTENTION.value, AssetStatus.ARCHIVED.value},
        AssetStatus.CREATE_FAILED.value: {
            AssetStatus.PRECHECKING.value,
            AssetStatus.ARCHIVED.value,
        },
        AssetStatus.NEEDS_ATTENTION.value: {
            AssetStatus.PRECHECKING.value,
            AssetStatus.ARCHIVED.value,
        },
        AssetStatus.ARCHIVED.value: set(),
    },
    "operation": {
        OperationStatus.DRAFT.value: {
            OperationStatus.QUEUED.value,
            OperationStatus.STOPPED.value,
        },
        OperationStatus.QUEUED.value: {
            OperationStatus.RUNNING.value,
            OperationStatus.PAUSED.value,
            OperationStatus.STOPPING.value,
            OperationStatus.STOPPED.value,
        },
        OperationStatus.RUNNING.value: {
            OperationStatus.QUEUED.value,
            OperationStatus.PAUSED.value,
            OperationStatus.STOPPING.value,
            OperationStatus.COMPLETED.value,
            OperationStatus.PARTIAL_COMPLETED.value,
            OperationStatus.FAILED.value,
            OperationStatus.UNKNOWN.value,
        },
        OperationStatus.PAUSED.value: {
            OperationStatus.QUEUED.value,
            OperationStatus.STOPPING.value,
            OperationStatus.STOPPED.value,
        },
        OperationStatus.STOPPING.value: {
            OperationStatus.STOPPED.value,
            OperationStatus.UNKNOWN.value,
        },
        OperationStatus.UNKNOWN.value: {
            OperationStatus.QUEUED.value,
            OperationStatus.PAUSED.value,
            OperationStatus.STOPPING.value,
            OperationStatus.STOPPED.value,
            OperationStatus.FAILED.value,
        },
        OperationStatus.STOPPED.value: {OperationStatus.QUEUED.value},
        OperationStatus.COMPLETED.value: set(),
        OperationStatus.PARTIAL_COMPLETED.value: {OperationStatus.QUEUED.value},
        OperationStatus.FAILED.value: {OperationStatus.QUEUED.value},
    },
    "item": {
        ItemStatus.PENDING.value: {
            ItemStatus.IN_PROGRESS.value,
            ItemStatus.CANCELLED.value,
            ItemStatus.SKIPPED_ALREADY_MEMBER.value,
        },
        ItemStatus.IN_PROGRESS.value: {
            ItemStatus.INVITE_SENT.value,
            ItemStatus.WAITING_APPROVAL.value,
            ItemStatus.MEMBER_VERIFIED.value,
            ItemStatus.SKIPPED_ALREADY_MEMBER.value,
            ItemStatus.FAILED_TRANSIENT.value,
            ItemStatus.FAILED_PERMANENT.value,
            ItemStatus.UNKNOWN.value,
            ItemStatus.CANCELLED.value,
        },
        ItemStatus.INVITE_SENT.value: {
            ItemStatus.MEMBER_VERIFIED.value,
            ItemStatus.WAITING_APPROVAL.value,
            ItemStatus.FAILED_TRANSIENT.value,
            ItemStatus.FAILED_PERMANENT.value,
            ItemStatus.UNKNOWN.value,
        },
        ItemStatus.WAITING_APPROVAL.value: {
            ItemStatus.MEMBER_VERIFIED.value,
            ItemStatus.FAILED_TRANSIENT.value,
            ItemStatus.FAILED_PERMANENT.value,
            ItemStatus.UNKNOWN.value,
        },
        ItemStatus.MEMBER_VERIFIED.value: {ItemStatus.ADMIN_PROMOTING.value},
        ItemStatus.ADMIN_PROMOTING.value: {
            ItemStatus.ADMIN_VERIFIED.value,
            ItemStatus.FAILED_TRANSIENT.value,
            ItemStatus.FAILED_PERMANENT.value,
            ItemStatus.UNKNOWN.value,
        },
        ItemStatus.FAILED_TRANSIENT.value: {
            ItemStatus.IN_PROGRESS.value,
            ItemStatus.PENDING.value,
            ItemStatus.CANCELLED.value,
        },
        ItemStatus.UNKNOWN.value: {
            ItemStatus.IN_PROGRESS.value,
            ItemStatus.SKIPPED_ALREADY_MEMBER.value,
            ItemStatus.CANCELLED.value,
            ItemStatus.FAILED_PERMANENT.value,
        },
        ItemStatus.ADMIN_VERIFIED.value: set(),
        ItemStatus.SKIPPED_ALREADY_MEMBER.value: set(),
        ItemStatus.FAILED_PERMANENT.value: {ItemStatus.PENDING.value},
        ItemStatus.CANCELLED.value: {ItemStatus.PENDING.value},
    },
}


def assert_transition(kind: str, current: str, target: str) -> None:
    """Raise ``ValueError`` when a state transition is not allowed."""

    try:
        allowed = _TRANSITIONS[kind][current]
    except KeyError as exc:
        raise ValueError(f"Unknown {kind} state: {current}") from exc
    if target not in allowed:
        raise ValueError(f"Invalid {kind} transition: {current} -> {target}")


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_selection_snapshot(
    resources: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Normalize a resource selection and return it with a stable SHA-256 hash."""

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    valid_types = {item.value for item in ResourceType}
    for resource in resources:
        resource_type = str(resource.get("resource_type", "")).strip().lower()
        if resource_type not in valid_types:
            raise ValueError(f"Invalid resource_type: {resource_type}")
        try:
            resource_id = int(resource["resource_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("resource_id must be an integer") from exc
        if resource_id <= 0:
            raise ValueError("resource_id must be positive")
        key = (resource_type, resource_id)
        if key in seen:
            raise ValueError(f"Duplicate resource: {resource_type}:{resource_id}")
        seen.add(key)
        normalized.append({"resource_type": resource_type, "resource_id": resource_id})

    normalized.sort(key=lambda item: (item["resource_type"], item["resource_id"]))
    return normalized, _canonical_hash(normalized)


def build_config_snapshot(config: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """Apply safe defaults and return an immutable operation configuration snapshot."""

    merged: dict[str, Any] = {**DEFAULT_OPERATION_CONFIG, **dict(config or {})}
    integer_fields = (
        ("batch_size", 1, 200),
        ("batch_interval_seconds", 1, 86400),
        ("max_parallelism", 1, 1),
        ("max_attempts", 1, 5),
    )
    for field, minimum, maximum in integer_fields:
        try:
            value = int(merged[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum}")
        merged[field] = value
    return merged, _canonical_hash(merged)
