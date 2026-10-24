"""Reuse completed semantic reviews without reusing mutable send permissions.

Redis is an optimization pointer to an immutable PostgreSQL snapshot. A cache
hit only supplies the semantic result; the current collection still supplies
membership, permission, evidence-maturity and account eligibility checks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.read_cache import FeatureFlags, TelegramReadCache
from app.core.config import settings
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.qualification_ai import SEMANTIC_VERSION, profile_fingerprint
from app.modules.acquisition.qualification_snapshot import (
    SNAPSHOT_CACHE_TTL_SECONDS,
    QualificationSnapshot,
    canonical_json,
    create_or_get_snapshot,
    current_fact_version,
    derive_evidence_hash,
    derive_rule_version,
    normalized_evidence,
    to_envelope,
)


def effective_rule_version(config: Mapping[str, Any], limits: Mapping[str, Any]) -> str:
    """Include every effective policy setting, not only the code policy label."""
    return derive_rule_version({
        "policy_version": POLICY_VERSION,
        "semantic_version": SEMANTIC_VERSION,
        "content_scope": "text_profile",
        "qualification_policy": dict(config),
        "ad_policy": {key: value for key, value in limits.items()
                      if key.startswith("ad_policy_") or key == "require_evidence_arguments"},
    })


def evidence_is_fresh(snapshot: Mapping[str, Any], now: datetime) -> bool:
    """Uncertain collections must neither authorize reuse nor mint snapshots."""
    try:
        collected = datetime.fromisoformat(str(snapshot.get("collected_at") or ""))
        if collected.tzinfo is not None:
            collected = collected.astimezone(UTC).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return bool(
        now - timedelta(seconds=SNAPSHOT_CACHE_TTL_SECONDS) <= collected <= now
        and snapshot.get("quality_status") == "qualified"
        and snapshot.get("collection_complete") is True
        and snapshot.get("evidence")
        and not snapshot.get("collection_needs_refresh")
        and not snapshot.get("invalidated_at")
        and not snapshot.get("technical_errors")
        and not snapshot.get("local_rpc_defer_reason")
        and snapshot.get("read_status", "fresh") == "fresh"
    )


def review_input_hash(snapshot: Mapping[str, Any], account: Any) -> str:
    """Bind semantic reuse to stable materials and the current account profile."""
    evidence = sorted(
        (normalized_evidence(item) for item in snapshot.get("evidence", [])),
        key=canonical_json,
    )
    return derive_evidence_hash({
        "evidence": evidence,
        "profile_fingerprint": profile_fingerprint(account),
        "raw_peer_id": snapshot.get("raw_peer_id"),
        "group_type": snapshot.get("group_type"),
        "system_identity_fingerprint": snapshot.get("system_identity_fingerprint"),
        "permissions": snapshot.get("permissions"),
    })


def _cache() -> TelegramReadCache:
    return TelegramReadCache(lambda **_: None, flags=FeatureFlags(qualification_snapshot=True))


async def reuse_semantic_review(
    db: Any, snapshot: dict[str, Any], account: Any, *, target_key: str,
    fact_version: str, rule_version: str, force_refresh: bool = False,
    cache: TelegramReadCache | None = None, now: datetime | None = None,
) -> Any | None:
    """Return a reviewed result only for a fresh exact-version Redis/PG pair."""
    now = now or datetime.utcnow()
    if (not settings.P3_QUALIFICATION_SNAPSHOT or force_refresh
            or not evidence_is_fresh(snapshot, now)
            or await current_fact_version(db, target_key) != fact_version):
        return None
    material = review_input_hash(snapshot, account)
    # Evidence hashes include the final verdict; locate recent candidates by
    # the indexed target/version tuple, then compare their pre-review material.
    candidates = (await db.scalars(select(QualificationSnapshot).where(
        QualificationSnapshot.target_key == target_key,
        QualificationSnapshot.group_version == fact_version,
        QualificationSnapshot.source_fact_version == fact_version,
        QualificationSnapshot.rule_version == rule_version,
        QualificationSnapshot.created_at >= now - timedelta(seconds=SNAPSHOT_CACHE_TTL_SECONDS),
        QualificationSnapshot.created_at <= now,
        QualificationSnapshot.verdict.in_(["allowed", "trial", "reject"]),
    ).order_by(QualificationSnapshot.created_at.desc()).limit(32))).all()
    cache = cache or _cache()
    for candidate in candidates:
        try:
            saved = json.loads(candidate.evidence_json)
        except (TypeError, ValueError):
            continue
        if (saved.get("qualification_input_hash") != material
                or saved.get("ai_final") is not True
                or saved.get("ai_decision") not in {"pass", "fail"}
                or saved.get("ai_pending") or saved.get("ai_review_incomplete")):
            continue
        result = await cache.get_qualification(
            target_key, fact_version, rule_version, candidate.evidence_hash,
        )
        identity = result.data if isinstance(result.data, Mapping) else {}
        if not result.usable_for_send or any(identity.get(key) != value for key, value in {
            "snapshot_id": candidate.snapshot_id, "target_key": target_key,
            "group_version": fact_version, "rule_version": rule_version,
            "evidence_hash": candidate.evidence_hash, "verdict": candidate.verdict,
        }.items()):
            continue
        from app.modules.acquisition.automation import GroupAdRulesAuditResult

        saved_result = saved.get("advertising_audit") or {}
        if saved_result.get("ad_allowed") not in (True, False):
            continue
        accepted = {field.name for field in fields(GroupAdRulesAuditResult)} - {"evidence", "cache_hit"}
        reviewed = GroupAdRulesAuditResult(**{
            key: value for key, value in saved_result.items() if key in accepted
        }, evidence=snapshot["evidence"], cache_hit=True)
        # Copy semantic metadata only. Current Telegram facts stay authoritative.
        for key in ("ai_final", "ai_decision", "ai_material_key", "ai_redaction_version"):
            if key in saved:
                snapshot[key] = saved[key]
        snapshot.update(ai_pending=False, qualification_snapshot_cache_hit=True)
        return reviewed
    return None


async def persist_qualification_snapshot(
    db: Any, snapshot: dict[str, Any], *, target_key: str, fact_version: str,
    rule_version: str, verdict: str, cache: TelegramReadCache | None = None,
    now: datetime | None = None,
) -> QualificationSnapshot | None:
    """Persist known fresh evidence; never bless an event racing collection."""
    now = now or datetime.utcnow()
    if (not settings.P3_QUALIFICATION_SNAPSHOT
            or verdict not in {"allowed", "trial", "reject"}
            or not evidence_is_fresh(snapshot, now)
            or snapshot.get("ai_pending") or snapshot.get("ai_review_incomplete")
            or await current_fact_version(db, target_key) != fact_version):
        return None
    immutable = await create_or_get_snapshot(
        db, target_key=target_key, group_version=fact_version,
        rule_version=rule_version, evidence=snapshot, verdict=verdict,
        source_fact_version=fact_version, now=now,
    )
    snapshot["qualification_snapshot_id"] = immutable.snapshot_id
    try:
        await (cache or _cache()).put_qualification(
            target_key, fact_version, rule_version, immutable.evidence_hash,
            {"snapshot_id": immutable.snapshot_id, "target_key": target_key,
             "group_version": fact_version, "rule_version": rule_version,
             "evidence_hash": immutable.evidence_hash, "verdict": verdict,
             "source_version": fact_version},
        )
    except Exception:
        # Redis only publishes a pointer. Consumers still require the committed
        # database row, so a later transaction rollback cannot authorize reuse.
        pass
    return immutable


async def get_valid_qualification_snapshot(
    db: Any, *, target_key: str, account_id: int, campaign_id: int | None = None,
    run_id: str | None = None, rule_version: str | None = None,
    required_scope: str | None = None,
    now: datetime | None = None,
) -> Any | None:
    """Return a current immutable envelope for a P2 write boundary.

    This helper intentionally does not infer eligibility from ``Group`` fields
    or candidate scores. It only returns a persisted, eligible snapshot whose
    fact watermark still equals the current local fact version. Missing,
    expired, unknown or stale evidence returns ``None`` so the caller can
    route the target back to qualification/reconciliation.
    """
    now = now or datetime.utcnow()
    fact_version = await current_fact_version(db, target_key)
    filters = [
        QualificationSnapshot.target_key == target_key,
        QualificationSnapshot.group_version == fact_version,
        QualificationSnapshot.source_fact_version == fact_version,
        QualificationSnapshot.verdict.in_(["allowed", "trial", "qualified"]),
        QualificationSnapshot.created_at >= now - timedelta(seconds=SNAPSHOT_CACHE_TTL_SECONDS),
        QualificationSnapshot.created_at <= now,
    ]
    if rule_version is not None:
        filters.append(QualificationSnapshot.rule_version == str(rule_version))
    candidate = await db.scalar(
        select(QualificationSnapshot).where(*filters).order_by(
            QualificationSnapshot.created_at.desc()
        ).limit(1)
    )
    if candidate is None:
        return None
    if required_scope == "ad":
        try:
            evidence = json.loads(candidate.evidence_json or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if isinstance(evidence, Mapping) and evidence.get("authorization_scope") == "join":
            return None
    try:
        return to_envelope(
            candidate, account_id=int(account_id), target_key=target_key,
            campaign_id=campaign_id, run_id=run_id,
        )
    except (TypeError, ValueError):
        return None
