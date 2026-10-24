"""Immutable qualification snapshots used by the P2 growth Outbox.

The existing ``GroupQualificationAudit`` table is a mutable review/checkpoint
record.  P2 needs a smaller immutable identity which can be safely referenced
by a join or advertisement operation.  This module keeps that identity and
the canonical hashing rules in one place; it deliberately contains no
Telegram client calls.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

SNAPSHOT_CACHE_TTL_SECONDS = 24 * 60 * 60
UNKNOWN_VERDICTS = frozenset({"unknown", "technical_wait", "pending"})
ELIGIBLE_VERDICTS = frozenset({"allowed", "trial", "qualified"})
_RUNTIME_EVIDENCE_KEYS = frozenset(
    {
        "collected_at",
        "checked_at",
        "created_at",
        "updated_at",
        "account_id",
        "audit_id",
        "read_cost",
        "previous_checks",
        "qualification_exit_history",
        "qualification_snapshot_id",
        "collection_operation_key",
        "checkpoint_cycle",
        "cache_hit",
        "qualification_snapshot_cache_hit",
    }
)


class QualificationSnapshotError(ValueError):
    """Base error for invalid or unusable qualification snapshots."""


class QualificationSnapshotNotEligible(QualificationSnapshotError):
    """The evidence does not authorize a P2 operation."""


class QualificationSnapshotMismatch(QualificationSnapshotError):
    """An operation envelope does not match its immutable snapshot."""


class QualificationFactVersion(Base):
    """Monotonic local fact version used to invalidate read evidence."""

    __tablename__ = "qualification_fact_version"

    target_key: Mapped[str] = mapped_column(String(500), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return deterministic JSON used for evidence and rule fingerprints."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_rule_version(rules: Mapping[str, Any]) -> str:
    """Fingerprint the normalized qualification policy."""

    return sha256_hex(dict(rules))


def normalized_evidence(value: Any) -> Any:
    """Use the same evidence for identity, storage and conflict checks."""
    if isinstance(value, Mapping):
        return {
            str(key): normalized_evidence(item)
            for key, item in value.items()
            if str(key) not in _RUNTIME_EVIDENCE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [normalized_evidence(item) for item in value]
    return value


def derive_evidence_hash(evidence: Mapping[str, Any]) -> str:
    """Fingerprint evidence while excluding runtime-only timestamps."""
    return sha256_hex(normalized_evidence(evidence))


class QualificationSnapshot(Base):
    """Append-only qualification evidence identity.

    ``account_id`` and ``run_id`` intentionally live on the operation
    envelope.  The evidence itself is reusable across accounts and campaign
    runs when its target and versions are identical.
    """

    __tablename__ = "qualification_snapshot"

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    target_key: Mapped[str] = mapped_column(String(500), nullable=False)
    group_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    source_fact_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "target_key",
            "group_version",
            "rule_version",
            "evidence_hash",
            name="uq_qualification_snapshot_identity",
        ),
        Index(
            "ix_qualification_snapshot_target_version",
            "target_key",
            "group_version",
            "rule_version",
        ),
        Index("ix_qualification_snapshot_evidence", "evidence_hash"),
    )


@event.listens_for(QualificationSnapshot, "before_update")
def _reject_snapshot_update(_mapper: Any, _connection: Any, target: QualificationSnapshot) -> None:
    state = inspect(target)
    immutable = (
        "snapshot_id",
        "target_key",
        "group_version",
        "rule_version",
        "evidence_hash",
        "evidence_json",
        "verdict",
        "source_fact_version",
        "created_at",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise ValueError("qualification_snapshot_immutable")


@dataclass(frozen=True, slots=True)
class QualificationSnapshotEnvelope:
    """P2 input envelope carrying immutable evidence plus operation context."""

    snapshot_id: str
    group_version: str
    rule_version: str
    evidence_hash: str
    account_id: int
    target_key: str
    campaign_id: int | None = None
    run_id: str | None = None
    verdict: str = "qualified"


def snapshot_id_for(
    *, target_key: str, group_version: str, rule_version: str, evidence_hash: str
) -> str:
    identity = {
        "target_key": target_key,
        "group_version": group_version,
        "rule_version": rule_version,
        "evidence_hash": evidence_hash,
    }
    return f"qs:{sha256_hex(identity)[:48]}"


async def create_or_get_snapshot(
    db: Any,
    *,
    target_key: str,
    group_version: str,
    rule_version: str,
    evidence: Mapping[str, Any],
    verdict: str,
    source_fact_version: str,
    now: datetime | None = None,
) -> QualificationSnapshot:
    """Persist one immutable snapshot and reuse an exact existing identity.

    Unknown/technical results are intentionally rejected before persistence;
    they must be retried after the missing evidence is collected and must not
    reach the P2 Outbox.
    """

    for name, value in {
        "target_key": target_key,
        "group_version": group_version,
        "rule_version": rule_version,
        "source_fact_version": source_fact_version,
    }.items():
        if not str(value or "").strip():
            raise QualificationSnapshotError(f"qualification_snapshot_{name}_required")
    evidence = normalized_evidence(evidence)
    if not str(evidence_hash := derive_evidence_hash(evidence)):
        raise QualificationSnapshotError("qualification_snapshot_evidence_required")
    if verdict in UNKNOWN_VERDICTS:
        raise QualificationSnapshotNotEligible("qualification_snapshot_unknown_verdict")

    identity_filter = (
        QualificationSnapshot.target_key == target_key,
        QualificationSnapshot.group_version == str(group_version),
        QualificationSnapshot.rule_version == str(rule_version),
        QualificationSnapshot.evidence_hash == evidence_hash,
    )
    existing = await db.scalar(select(QualificationSnapshot).where(*identity_filter))
    if existing is not None:
        if existing.verdict != verdict or canonical_json(normalized_evidence(json.loads(existing.evidence_json))) != canonical_json(evidence):
            raise QualificationSnapshotMismatch("qualification_snapshot_identity_conflict")
        return existing

    from app.core.account.budget_ledger import conflict_insert

    snapshot_values = {
        "snapshot_id": snapshot_id_for(
            target_key=target_key,
            group_version=str(group_version),
            rule_version=str(rule_version),
            evidence_hash=evidence_hash,
        ),
        "target_key": target_key,
        "group_version": str(group_version),
        "rule_version": str(rule_version),
        "evidence_hash": evidence_hash,
        "evidence_json": canonical_json(evidence),
        "verdict": verdict,
        "source_fact_version": str(source_fact_version),
        "created_at": now or datetime.utcnow(),
    }
    await db.execute(
        conflict_insert(db, QualificationSnapshot)
        .values(**snapshot_values)
        .on_conflict_do_nothing()
    )
    snapshot = await db.scalar(select(QualificationSnapshot).where(*identity_filter))
    if snapshot is None or snapshot.verdict != verdict or canonical_json(
        normalized_evidence(json.loads(snapshot.evidence_json))
    ) != canonical_json(evidence):
        raise QualificationSnapshotMismatch("qualification_snapshot_identity_conflict")
    return snapshot


async def current_fact_version(db: Any, target_key: str) -> str:
    row = await db.get(QualificationFactVersion, target_key)
    return str(row.version if row is not None else 0)


async def bump_fact_version(db: Any, target_key: str) -> str:
    """Advance a local fact watermark in the caller's transaction."""

    if not str(target_key or "").strip():
        raise QualificationSnapshotError("qualification_fact_target_required")
    from app.core.account.budget_ledger import conflict_insert

    now = datetime.utcnow()
    statement = conflict_insert(db, QualificationFactVersion).values(
        target_key=target_key, version=1, updated_at=now
    ).on_conflict_do_update(
        index_elements=["target_key"],
        set_={"version": QualificationFactVersion.version + 1, "updated_at": now},
    ).returning(QualificationFactVersion.version)
    version = (await db.execute(statement)).scalar_one()
    # Refresh an identity-map instance that may have been read before the upsert.
    await db.get(QualificationFactVersion, target_key, populate_existing=True)
    return str(version)


def to_envelope(
    snapshot: QualificationSnapshot,
    *,
    account_id: int,
    target_key: str | None = None,
    campaign_id: int | None = None,
    run_id: str | None = None,
) -> QualificationSnapshotEnvelope:
    if snapshot.verdict not in ELIGIBLE_VERDICTS:
        raise QualificationSnapshotNotEligible("qualification_snapshot_ineligible_verdict")
    if target_key is not None and target_key != snapshot.target_key:
        raise QualificationSnapshotMismatch("qualification_snapshot_target_mismatch")
    return QualificationSnapshotEnvelope(
        snapshot_id=snapshot.snapshot_id,
        group_version=snapshot.group_version,
        rule_version=snapshot.rule_version,
        evidence_hash=snapshot.evidence_hash,
        account_id=int(account_id),
        target_key=target_key or snapshot.target_key,
        campaign_id=campaign_id,
        run_id=run_id,
        verdict=snapshot.verdict,
    )


async def validate_envelope(db: Any, envelope: QualificationSnapshotEnvelope) -> QualificationSnapshot:
    """Validate P2's immutable snapshot reference before enqueueing."""

    snapshot = await db.get(QualificationSnapshot, envelope.snapshot_id)
    if snapshot is None:
        raise QualificationSnapshotMismatch("qualification_snapshot_not_found")
    if snapshot.verdict not in ELIGIBLE_VERDICTS:
        raise QualificationSnapshotNotEligible("qualification_snapshot_ineligible_verdict")
    fact = await db.get(QualificationFactVersion, envelope.target_key, populate_existing=True)
    if fact is not None and str(fact.version) != snapshot.source_fact_version:
        raise QualificationSnapshotMismatch("qualification_snapshot_fact_version_stale")
    if snapshot.source_fact_version != snapshot.group_version:
        raise QualificationSnapshotMismatch("qualification_snapshot_source_version_mismatch")
    expected = (
        snapshot.target_key == envelope.target_key,
        snapshot.group_version == envelope.group_version,
        snapshot.rule_version == envelope.rule_version,
        snapshot.evidence_hash == envelope.evidence_hash,
    )
    if not all(expected):
        raise QualificationSnapshotMismatch("qualification_snapshot_envelope_mismatch")
    return snapshot


__all__ = [
    "SNAPSHOT_CACHE_TTL_SECONDS",
    "QualificationFactVersion",
    "QualificationSnapshot",
    "QualificationSnapshotEnvelope",
    "QualificationSnapshotError",
    "QualificationSnapshotMismatch",
    "QualificationSnapshotNotEligible",
    "canonical_json",
    "create_or_get_snapshot",
    "current_fact_version",
    "bump_fact_version",
    "derive_evidence_hash",
    "derive_rule_version",
    "snapshot_id_for",
    "to_envelope",
    "validate_envelope",
]
