"""Read-only outbound governance for stage-three owned-group AI messages."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.guardian.models import (
    GroupModerationPolicy,
    ModerationRule,
    ModerationSensitiveKeyword,
)
from app.modules.owned_group.messaging_content_safety import find_url_like_tokens
from app.modules.owned_group.messaging_prompt_builder import GovernanceProjection

_CONTENT_CATEGORIES = frozenset({"community", "promotion"})
_RULE_TYPES = frozenset({"keyword", "domain", "frequency", "image"})
_UNSAFE_TEXT_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]"
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_AI_DISCLOSURE_RE = re.compile(
    r"(?:我是|作为|由|本账号是).{0,8}(?:AI|人工智能|机器人|模型|自动化)|"
    r"(?:AI|人工智能|机器人|模型|系统提示词|system\s+prompt).{0,8}"
    r"(?:生成|助手|回复|身份)",
    re.IGNORECASE,
)
_COMMUNITY_PROMOTION_RE = re.compile(
    r"(注册|邀请链接|邀请码|价格|报价|折扣|优惠|购买|下单|立即体验|"
    r"私聊(?:我)?|加我|联系客服|免费(?:试用|体验)|"
    r"\b(?:buy|purchase|order|discount|coupon|pricing)\b)",
    re.IGNORECASE,
)
_IDENTITY_OR_PROMISE_RE = re.compile(
    r"(?:我是|本人是|作为|代表).{0,10}(?:官方|客服|管理员|群主|真实用户)|"
    r"(?:保证|承诺).{0,24}(?:到账|退款|完成|成功|解决)|"
    r"(?:退款|支付|交易|订单).{0,12}(?:已经|已).{0,8}(?:成功|到账|完成)|"
    r"(?:官方|平台|本群|管理员).{0,8}(?:公告|通知|声明)\s*[:：]",
    re.IGNORECASE,
)
_TELEGRAM_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9_]{5,32}\b")


class OutboundGovernanceError(RuntimeError):
    """Stable failure raised when governance cannot be evaluated safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class OutboundGovernanceDecision:
    allowed: bool
    reason_code: str | None
    matched_rule_ids: tuple[int, ...] = ()
    matched_term_hashes: tuple[str, ...] = ()
    governance_rules_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _GovernanceContext:
    rules: tuple[ModerationRule, ...]
    sensitive_keywords: tuple[ModerationSensitiveKeyword, ...]
    policy: GroupModerationPolicy
    projection: GovernanceProjection


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _UNSAFE_TEXT_RE.sub("", text).strip()


def _match_text(value: Any) -> str:
    return _normalize_text(value).casefold()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256_text(canonical)


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise OutboundGovernanceError(
            "GOVERNANCE_CONTEXT_UNAVAILABLE",
            "governance rule timestamp is invalid",
        )
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _group_scope(group_id: int | None) -> str:
    return "global" if group_id is None else f"group:{int(group_id)}"


def _stable_rule_key(value: Any, source_type: str) -> tuple[str, int, int]:
    group_id = getattr(value, "group_id", None)
    return (source_type, 0 if group_id is None else 1, int(value.id))


def _policy_hash(policy: GroupModerationPolicy) -> str:
    material = {
        "group_id": int(policy.group_id),
        "message_interval_seconds": int(policy.message_interval_seconds),
        "max_messages_per_minute": int(policy.max_messages_per_minute),
        "max_links_per_hour": int(policy.max_links_per_hour),
        "new_member_silent_minutes": int(policy.new_member_silent_minutes),
        "first_speak_delay_seconds": int(policy.first_speak_delay_seconds),
        "media_policy_sha256": _sha256_text(str(policy.media_policy or "")),
        "link_policy_sha256": _sha256_text(str(policy.link_policy or "")),
    }
    return _canonical_sha256(material)


def _build_projection(
    *,
    core_group_id: int,
    rules: tuple[ModerationRule, ...],
    keywords: tuple[ModerationSensitiveKeyword, ...],
    policy: GroupModerationPolicy,
) -> GovernanceProjection:
    policy_hash = _policy_hash(policy)
    canonical_rules: list[dict[str, Any]] = []
    projected_terms: list[str] = []
    seen_projected: set[str] = set()
    categories: set[str] = set()
    domain_count = 0
    frequency_count = 0
    image_count = 0

    combined: list[tuple[str, Any]] = [
        *(("moderation_rule", item) for item in rules),
        *(("sensitive_keyword", item) for item in keywords),
    ]
    combined.sort(key=lambda item: _stable_rule_key(item[1], item[0]))
    for source_type, item in combined:
        if source_type == "moderation_rule":
            rule_type = _enum_value(item.rule_type)
            if rule_type not in _RULE_TYPES:
                raise OutboundGovernanceError(
                    "GOVERNANCE_CONTEXT_UNAVAILABLE",
                    "governance rule type is invalid",
                )
            normalized_term = _normalize_text(item.pattern)
            canonical_rules.append(
                {
                    "source_type": source_type,
                    "id": int(item.id),
                    "rule_type": rule_type,
                    "group_scope": _group_scope(item.group_id),
                    "pattern_or_text": normalized_term,
                    "level": _enum_value(item.level),
                    "action": _enum_value(item.action),
                    "updated_at": _utc_timestamp(item.updated_at),
                }
            )
            if rule_type == "keyword":
                key = normalized_term.casefold()
                if (
                    normalized_term
                    and len(normalized_term) <= 64
                    and key not in seen_projected
                    and len(projected_terms) < 200
                ):
                    projected_terms.append(normalized_term)
                    seen_projected.add(key)
            elif rule_type == "domain":
                domain_count += 1
            elif rule_type == "frequency":
                frequency_count += 1
            elif rule_type == "image":
                image_count += 1
            continue

        normalized_term = _normalize_text(item.normalized_text or item.text)
        category = _normalize_text(item.category)
        if category:
            categories.add(category)
        canonical_rules.append(
            {
                "source_type": source_type,
                "id": int(item.id),
                "category": category,
                "group_scope": _group_scope(item.group_id),
                "pattern_or_text": normalized_term,
                "level": _enum_value(item.level),
                "action": _enum_value(item.action),
                "updated_at": _utc_timestamp(item.updated_at),
            }
        )
        key = normalized_term.casefold()
        if (
            normalized_term
            and len(normalized_term) <= 64
            and key not in seen_projected
            and len(projected_terms) < 200
        ):
            projected_terms.append(normalized_term)
            seen_projected.add(key)

    rules_revision = _canonical_sha256(
        {
            "rules": canonical_rules,
            "moderation_policy_hash": policy_hash,
        }
    )
    return GovernanceProjection(
        core_group_id=core_group_id,
        blocked_keyword_categories=tuple(sorted(categories, key=str.casefold)),
        forbidden_terms=tuple(projected_terms),
        active_domain_rule_count=domain_count,
        active_frequency_rule_count=frequency_count,
        active_image_rule_count=image_count,
        moderation_policy_hash=policy_hash,
        rules_revision=rules_revision,
    )


async def _load_governance_context(
    *,
    db: AsyncSession,
    core_group_id: int,
) -> _GovernanceContext:
    if not isinstance(core_group_id, int) or isinstance(core_group_id, bool) or core_group_id <= 0:
        raise OutboundGovernanceError(
            "GOVERNANCE_CONTEXT_UNAVAILABLE",
            "core group identifier is invalid",
        )
    try:
        with db.no_autoflush:
            rules_result = await db.execute(
                select(ModerationRule)
                .where(
                    ModerationRule.enabled.is_(True),
                    or_(
                        ModerationRule.group_id.is_(None),
                        ModerationRule.group_id == core_group_id,
                    ),
                )
                .order_by(
                    ModerationRule.group_id.asc().nullsfirst(),
                    ModerationRule.id.asc(),
                )
            )
            keyword_result = await db.execute(
                select(ModerationSensitiveKeyword)
                .where(
                    ModerationSensitiveKeyword.enabled.is_(True),
                    or_(
                        ModerationSensitiveKeyword.group_id.is_(None),
                        ModerationSensitiveKeyword.group_id == core_group_id,
                    ),
                )
                .order_by(
                    ModerationSensitiveKeyword.group_id.asc().nullsfirst(),
                    ModerationSensitiveKeyword.id.asc(),
                )
            )
            policy_result = await db.execute(
                select(GroupModerationPolicy).where(
                    GroupModerationPolicy.group_id == core_group_id
                )
            )
        rules = tuple(rules_result.scalars().all())
        keywords = tuple(keyword_result.scalars().all())
        policies = tuple(policy_result.scalars().all())
    except OutboundGovernanceError:
        raise
    except Exception as exc:
        raise OutboundGovernanceError(
            "GOVERNANCE_CONTEXT_UNAVAILABLE",
            "governance context could not be loaded",
        ) from exc
    if len(policies) != 1:
        raise OutboundGovernanceError(
            "GOVERNANCE_CONTEXT_UNAVAILABLE",
            "exactly one group governance policy is required",
        )
    try:
        projection = _build_projection(
            core_group_id=core_group_id,
            rules=rules,
            keywords=keywords,
            policy=policies[0],
        )
    except OutboundGovernanceError:
        raise
    except Exception as exc:
        raise OutboundGovernanceError(
            "GOVERNANCE_CONTEXT_UNAVAILABLE",
            "governance context could not be canonicalized",
        ) from exc
    return _GovernanceContext(
        rules=rules,
        sensitive_keywords=keywords,
        policy=policies[0],
        projection=projection,
    )


class GovernanceProjectionService:
    """Read and hash current governance without mutating moderation state."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def project(self, *, core_group_id: int) -> GovernanceProjection:
        context = await _load_governance_context(
            db=self.db,
            core_group_id=core_group_id,
        )
        return context.projection

    async def load(self, core_group_id: int) -> GovernanceProjection:
        """Compatibility alias for callers that name the read operation load."""

        return await self.project(core_group_id=core_group_id)


async def load_governance_projection(
    *,
    db: AsyncSession,
    core_group_id: int,
) -> GovernanceProjection:
    return await GovernanceProjectionService(db).project(core_group_id=core_group_id)


def _decision(
    *,
    allowed: bool,
    reason_code: str | None,
    context: _GovernanceContext,
    rule_ids: set[int] | None = None,
    term_hashes: set[str] | None = None,
) -> OutboundGovernanceDecision:
    return OutboundGovernanceDecision(
        allowed=allowed,
        reason_code=reason_code,
        matched_rule_ids=tuple(sorted(rule_ids or set())),
        matched_term_hashes=tuple(sorted(term_hashes or set())),
        governance_rules_hash=context.projection.rules_revision,
    )


async def evaluate_outbound_content(
    *,
    db: AsyncSession,
    core_group_id: int,
    text: str,
    content_category: Literal["community", "promotion"],
    allowed_promotion_url: str | None,
    persona_forbidden_topics: tuple[str, ...],
    persona_catchphrases: tuple[str, ...],
) -> OutboundGovernanceDecision:
    """Evaluate final text with no whitelist bypass and no Guardian side effects."""

    context = await _load_governance_context(
        db=db,
        core_group_id=core_group_id,
    )
    if content_category not in _CONTENT_CATEGORIES:
        return _decision(
            allowed=False,
            reason_code="CONTENT_POLICY_BLOCKED",
            context=context,
        )
    normalized = _normalize_text(text)
    shadow = normalized.casefold()
    if not normalized or len(normalized) > 4096 or not _CJK_RE.search(normalized):
        return _decision(
            allowed=False,
            reason_code="CONTENT_POLICY_BLOCKED",
            context=context,
        )

    persona_matches = {
        _sha256_text(term)
        for raw_term in persona_forbidden_topics
        if (term := _match_text(raw_term)) and term in shadow
    }
    if persona_matches:
        return _decision(
            allowed=False,
            reason_code="PERSONA_FORBIDDEN_TOPIC_MATCHED",
            context=context,
            term_hashes=persona_matches,
        )

    matched_catchphrases: set[str] = set()
    catchphrase_match_count = 0
    seen_catchphrases: set[str] = set()
    for raw_phrase in persona_catchphrases:
        phrase = _match_text(raw_phrase)
        if not phrase or phrase in seen_catchphrases:
            continue
        seen_catchphrases.add(phrase)
        count = shadow.count(phrase)
        if count:
            matched_catchphrases.add(_sha256_text(phrase))
            catchphrase_match_count += count
    if len(matched_catchphrases) > 1 or catchphrase_match_count > 1:
        return _decision(
            allowed=False,
            reason_code="PERSONA_CATCHPHRASE_OVERUSED",
            context=context,
            term_hashes=matched_catchphrases,
        )

    urls = tuple(find_url_like_tokens(normalized))
    if content_category == "community":
        if urls or _COMMUNITY_PROMOTION_RE.search(normalized):
            return _decision(
                allowed=False,
                reason_code="CONTENT_POLICY_BLOCKED",
                context=context,
            )
    else:
        allowed_url = _normalize_text(allowed_promotion_url)
        if (
            any(url != allowed_url for url in urls)
            or (urls and not allowed_url)
            or (allowed_url and urls.count(allowed_url) > 1)
            or _TELEGRAM_HANDLE_RE.search(normalized)
        ):
            return _decision(
                allowed=False,
                reason_code="CONTENT_POLICY_BLOCKED",
                context=context,
            )

    if _AI_DISCLOSURE_RE.search(normalized) or _IDENTITY_OR_PROMISE_RE.search(normalized):
        return _decision(
            allowed=False,
            reason_code="CONTENT_POLICY_BLOCKED",
            context=context,
        )

    matched_rule_ids: set[int] = set()
    matched_term_hashes: set[str] = set()
    for rule in context.rules:
        rule_type = _enum_value(rule.rule_type)
        rule_pattern = _normalize_text(rule.pattern)
        if not rule_pattern:
            continue
        if rule_type == "keyword" and rule_pattern.casefold() in shadow:
            matched_rule_ids.add(int(rule.id))
            matched_term_hashes.add(_sha256_text(rule_pattern.casefold()))
        elif rule_type == "domain":
            try:
                pattern = re.compile(rule_pattern, re.IGNORECASE)
            except re.error as exc:
                raise OutboundGovernanceError(
                    "GOVERNANCE_CONTEXT_UNAVAILABLE",
                    "governance domain rule is invalid",
                ) from exc
            if any(pattern.search(url) for url in urls):
                matched_rule_ids.add(int(rule.id))
                matched_term_hashes.add(_sha256_text(rule_pattern.casefold()))
    for keyword in context.sensitive_keywords:
        term = _match_text(keyword.normalized_text or keyword.text)
        if term and term in shadow:
            matched_rule_ids.add(int(keyword.id))
            matched_term_hashes.add(_sha256_text(term))
    if matched_rule_ids:
        return _decision(
            allowed=False,
            reason_code="CONTENT_POLICY_BLOCKED",
            context=context,
            rule_ids=matched_rule_ids,
            term_hashes=matched_term_hashes,
        )
    return _decision(allowed=True, reason_code=None, context=context)


__all__ = [
    "GovernanceProjectionService",
    "OutboundGovernanceDecision",
    "OutboundGovernanceError",
    "evaluate_outbound_content",
    "load_governance_projection",
]
