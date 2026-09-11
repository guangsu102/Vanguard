"""Stage-three outbound-governance bridge for frozen message executions.

This module deliberately consumes only execution snapshots.  It never reads the
current account Persona, never invokes Guardian's inbound action engine, and
never mutates moderation state.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.persona import PersonaV1
from app.core.persona_observability import record_outbound_evaluation
from app.modules.owned_group.messaging_contracts import (
    MessageContentCategory,
    MessageMode,
    OwnedGroupMessagingError,
)
from app.modules.owned_group.messaging_outbound_governance import (
    OutboundGovernanceDecision,
    OutboundGovernanceError,
    evaluate_outbound_content,
)

STRICT_PERSONA_SOURCES = frozenset({"configured", "neutral_default"})
COMPATIBILITY_PERSONA_SOURCES = frozenset(
    {"feature_disabled_default", "legacy_default"}
)
_LOWER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def uses_stage3_outbound_governance(execution: Any) -> bool:
    """Return true only for strict stage-three AI execution snapshots."""

    return (
        str(_value(execution, "mode_snapshot") or "") == MessageMode.AI.value
        and str(_value(execution, "persona_source_snapshot") or "")
        in STRICT_PERSONA_SOURCES
    )


def _persona_terms(execution: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = _value(execution, "persona_snapshot")
    if not isinstance(raw, Mapping):
        raise OwnedGroupMessagingError(
            "PERSONA_CONFIG_INVALID",
            "消息执行缺少有效的 Persona 快照",
        )
    try:
        persona = PersonaV1.model_validate(dict(raw))
    except (TypeError, ValueError, ValidationError) as exc:
        raise OwnedGroupMessagingError(
            "PERSONA_CONFIG_INVALID",
            "消息执行的 Persona 快照无效",
        ) from exc
    return tuple(persona.forbidden_topics), tuple(persona.catchphrases)


def _category(execution: Any) -> Literal["community", "promotion"]:
    category = str(_value(execution, "content_category") or "")
    if category not in {
        MessageContentCategory.COMMUNITY.value,
        MessageContentCategory.PROMOTION.value,
    }:
        raise OwnedGroupMessagingError(
            "POLICY_SNAPSHOT_INVALID",
            "消息执行的内容类别快照无效",
        )
    return cast(Literal["community", "promotion"], category)


def _promotion_url(execution: Any) -> str | None:
    snapshot = _value(execution, "promotion_config_snapshot")
    if snapshot is None:
        return None
    if not isinstance(snapshot, Mapping):
        raise OwnedGroupMessagingError(
            "POLICY_SNAPSHOT_INVALID",
            "消息执行的推广配置快照无效",
        )
    return str(snapshot.get("destination_url") or "").strip() or None


def governance_decision_details(
    decision: OutboundGovernanceDecision,
) -> dict[str, Any]:
    """Return the safe, content-free portion of a governance decision."""

    return {
        "governance_reason_code": decision.reason_code,
        "governance_rules_hash": decision.governance_rules_hash,
        "matched_rule_ids": list(decision.matched_rule_ids),
        "matched_term_hashes": list(decision.matched_term_hashes),
    }


async def _evaluate(
    *,
    db: AsyncSession,
    execution: Any,
    text: str,
    body_only: bool,
    stage: Literal["generated_body", "generated_final", "review", "send"],
) -> OutboundGovernanceDecision:
    started = time.perf_counter()
    try:
        forbidden_topics, catchphrases = _persona_terms(execution)
        category = _category(execution)
        decision = await evaluate_outbound_content(
            db=db,
            core_group_id=int(_value(execution, "core_group_id")),
            text=text,
            content_category=category,
            # LLM-authored promotion bodies may not contain any URL.  Only the
            # deterministic final composition receives the frozen allowed URL.
            allowed_promotion_url=(
                None if body_only else _promotion_url(execution)
            ),
            persona_forbidden_topics=forbidden_topics,
            persona_catchphrases=catchphrases,
        )
    except OwnedGroupMessagingError as exc:
        record_outbound_evaluation(
            execution_id=int(_value(execution, "id", 0) or 0),
            account_id=int(_value(execution, "account_id", 0) or 0),
            asset_id=int(_value(execution, "owned_group_asset_id", 0) or 0),
            core_group_id=int(_value(execution, "core_group_id", 0) or 0),
            content_category=str(_value(execution, "content_category") or ""),
            persona_source=str(_value(execution, "persona_source_snapshot") or "unknown"),
            stage=stage,
            allowed=False,
            reason_code=exc.code,
            governance_hash=str(_value(execution, "governance_rules_hash") or "") or None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            request_id=str(_value(execution, "correlation_id") or "") or None,
        )
        raise
    except OutboundGovernanceError as exc:
        record_outbound_evaluation(
            execution_id=int(_value(execution, "id", 0) or 0),
            account_id=int(_value(execution, "account_id", 0) or 0),
            asset_id=int(_value(execution, "owned_group_asset_id", 0) or 0),
            core_group_id=int(_value(execution, "core_group_id", 0) or 0),
            content_category=str(_value(execution, "content_category") or ""),
            persona_source=str(_value(execution, "persona_source_snapshot") or "unknown"),
            stage=stage,
            allowed=False,
            reason_code=exc.code,
            governance_hash=str(_value(execution, "governance_rules_hash") or "") or None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            request_id=str(_value(execution, "correlation_id") or "") or None,
        )
        raise OwnedGroupMessagingError(
            exc.code,
            "群治理上下文不可用",
        ) from exc
    reason_code = decision.reason_code
    if not decision.allowed:
        reason_code = "CONTENT_POLICY_BLOCKED"
        if (
            stage == "send"
            and decision.governance_rules_hash
            != str(_value(execution, "governance_rules_hash") or "")
        ):
            reason_code = "CONTENT_POLICY_CHANGED"
    record_outbound_evaluation(
        execution_id=int(_value(execution, "id", 0) or 0),
        account_id=int(_value(execution, "account_id", 0) or 0),
        asset_id=int(_value(execution, "owned_group_asset_id", 0) or 0),
        core_group_id=int(_value(execution, "core_group_id", 0) or 0),
        content_category=category,
        persona_source=str(_value(execution, "persona_source_snapshot") or "unknown"),
        stage=stage,
        allowed=decision.allowed,
        reason_code=reason_code,
        governance_hash=decision.governance_rules_hash,
        duration_ms=int((time.perf_counter() - started) * 1000),
        request_id=str(_value(execution, "correlation_id") or "") or None,
    )
    return decision


async def govern_generated_body(
    *,
    db: AsyncSession,
    execution: Any,
    text: str,
) -> OutboundGovernanceDecision | None:
    """Apply the first check before deterministic promotion composition."""

    if not uses_stage3_outbound_governance(execution):
        return None
    decision = await _evaluate(
        db=db,
        execution=execution,
        text=text,
        body_only=True,
        stage="generated_body",
    )
    if not decision.allowed:
        raise OwnedGroupMessagingError(
            "CONTENT_POLICY_BLOCKED",
            "AI 正文未通过出站内容治理",
            details=governance_decision_details(decision),
        )
    return decision


async def govern_generated_final(
    *,
    db: AsyncSession,
    execution: Any,
    text: str,
) -> OutboundGovernanceDecision | None:
    """Apply the final-composition check and return its generation hash."""

    if not uses_stage3_outbound_governance(execution):
        return None
    decision = await _evaluate(
        db=db,
        execution=execution,
        text=text,
        body_only=False,
        stage="generated_final",
    )
    if not decision.allowed:
        raise OwnedGroupMessagingError(
            "CONTENT_POLICY_BLOCKED",
            "AI 消息未通过出站内容治理",
            details=governance_decision_details(decision),
        )
    return decision


async def govern_review_override(
    *,
    db: AsyncSession,
    execution: Any,
    text: str,
) -> OutboundGovernanceDecision | None:
    """Recheck an operator-edited final message without changing generation hash."""

    if not uses_stage3_outbound_governance(execution):
        return None
    decision = await _evaluate(
        db=db,
        execution=execution,
        text=text,
        body_only=False,
        stage="review",
    )
    if not decision.allowed:
        raise OwnedGroupMessagingError(
            "CONTENT_POLICY_BLOCKED",
            "AI 消息未通过出站内容治理",
            details=governance_decision_details(decision),
        )
    return decision


async def govern_before_send(
    *,
    db: AsyncSession,
    execution: Any,
    text: str,
) -> OutboundGovernanceDecision | None:
    """Read current rules and apply the final, no-whitelist send check."""

    if not uses_stage3_outbound_governance(execution):
        return None
    generation_hash = str(_value(execution, "governance_rules_hash") or "")
    if not _LOWER_SHA256_RE.fullmatch(generation_hash):
        raise OwnedGroupMessagingError(
            "POLICY_SNAPSHOT_INVALID",
            "消息执行缺少有效的生成治理指纹",
        )
    decision = await _evaluate(
        db=db,
        execution=execution,
        text=text,
        body_only=False,
        stage="send",
    )
    if not decision.allowed:
        changed = decision.governance_rules_hash != generation_hash
        details = governance_decision_details(decision)
        details.update(
            {
                "generation_governance_rules_hash": generation_hash,
                "send_governance_rules_hash": decision.governance_rules_hash,
            }
        )
        raise OwnedGroupMessagingError(
            "CONTENT_POLICY_CHANGED" if changed else "CONTENT_POLICY_BLOCKED",
            (
                "群治理规则变化后阻止发送"
                if changed
                else "消息在发送前未通过出站内容治理"
            ),
            details=details,
        )
    return decision


__all__ = [
    "COMPATIBILITY_PERSONA_SOURCES",
    "STRICT_PERSONA_SOURCES",
    "govern_before_send",
    "govern_generated_body",
    "govern_generated_final",
    "govern_review_override",
    "governance_decision_details",
    "uses_stage3_outbound_governance",
]
