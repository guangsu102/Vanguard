from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.modules.guardian.models import (
    GroupModerationPolicy,
    ModerationRule,
    ModerationSensitiveKeyword,
    RuleType,
    SensitiveKeywordSource,
    Violation,
    ViolationAction,
    ViolationLevel,
)
from app.modules.owned_group.messaging_outbound_governance import (
    GovernanceProjectionService,
    OutboundGovernanceError,
    evaluate_outbound_content,
)


async def _seed_policy(test_db, group_id: int = 128) -> None:
    test_db.add(
        GroupModerationPolicy(
            group_id=group_id,
            message_interval_seconds=10,
            max_messages_per_minute=5,
            max_links_per_hour=3,
            new_member_silent_minutes=5,
            first_speak_delay_seconds=30,
            media_policy='{"mode":"safe"}',
            link_policy='{"mode":"strict"}',
        )
    )
    await test_db.flush()


@pytest.mark.asyncio
async def test_persona_forbidden_topic_and_catchphrase_are_deterministic(test_db) -> None:
    await _seed_policy(test_db)
    forbidden = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="这里不要做过度营销。",
        content_category="community",
        allowed_promotion_url=None,
        persona_forbidden_topics=("过度营销",),
        persona_catchphrases=(),
    )
    overused = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="简单说，保持稳定。简单说，先观察。",
        content_category="community",
        allowed_promotion_url=None,
        persona_forbidden_topics=(),
        persona_catchphrases=("简单说",),
    )

    assert forbidden.allowed is False
    assert forbidden.reason_code == "PERSONA_FORBIDDEN_TOPIC_MATCHED"
    assert forbidden.matched_term_hashes
    assert overused.allowed is False
    assert overused.reason_code == "PERSONA_CATCHPHRASE_OVERUSED"


@pytest.mark.asyncio
async def test_outbound_rules_include_global_group_and_sensitive_without_side_effects(test_db) -> None:
    await _seed_policy(test_db)
    now = datetime.utcnow()
    test_db.add_all(
        [
            ModerationRule(
                rule_type=RuleType.KEYWORD,
                pattern="全局禁词",
                level=ViolationLevel.MEDIUM,
                action=ViolationAction.WARN,
                group_id=None,
                enabled=True,
                created_at=now,
                updated_at=now,
            ),
            ModerationRule(
                rule_type=RuleType.KEYWORD,
                pattern="本群禁词",
                level=ViolationLevel.HIGH,
                action=ViolationAction.BAN,
                group_id=128,
                enabled=True,
                created_at=now,
                updated_at=now,
            ),
            ModerationSensitiveKeyword(
                text="敏感短语",
                normalized_text="敏感短语",
                category="sensitive",
                source=SensitiveKeywordSource.MANUAL,
                level=ViolationLevel.MEDIUM,
                action=ViolationAction.WARN,
                group_id=128,
                enabled=True,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await test_db.flush()
    before = await test_db.scalar(select(func.count()).select_from(Violation))

    decision = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="这条消息包含本群禁词和敏感短语。",
        content_category="community",
        allowed_promotion_url=None,
        persona_forbidden_topics=(),
        persona_catchphrases=(),
    )
    after = await test_db.scalar(select(func.count()).select_from(Violation))

    assert decision.allowed is False
    assert decision.reason_code == "CONTENT_POLICY_BLOCKED"
    assert len(decision.matched_rule_ids) == 2
    assert decision.matched_term_hashes
    assert before == after == 0
    assert not test_db.new and not test_db.dirty and not test_db.deleted


@pytest.mark.asyncio
async def test_community_blocks_urls_and_promotion_locks_exact_url(test_db) -> None:
    await _seed_policy(test_db)
    community = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="详情请看 https://example.com",
        content_category="community",
        allowed_promotion_url=None,
        persona_forbidden_topics=(),
        persona_catchphrases=(),
    )
    promotion = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="了解详情 https://example.com https://evil.example",
        content_category="promotion",
        allowed_promotion_url="https://example.com",
        persona_forbidden_topics=(),
        persona_catchphrases=(),
    )
    allowed = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="保持稳定，了解详情 https://example.com",
        content_category="promotion",
        allowed_promotion_url="https://example.com",
        persona_forbidden_topics=(),
        persona_catchphrases=(),
    )

    assert community.allowed is False
    assert promotion.allowed is False
    assert allowed.allowed is True


@pytest.mark.asyncio
async def test_projection_is_bounded_but_full_rule_set_still_blocks(test_db) -> None:
    await _seed_policy(test_db)
    now = datetime.utcnow()
    test_db.add_all(
        [
            ModerationRule(
                rule_type=RuleType.KEYWORD,
                pattern=f"禁词{index:03d}",
                level=ViolationLevel.MEDIUM,
                action=ViolationAction.WARN,
                group_id=128,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            for index in range(201)
        ]
    )
    await test_db.flush()

    projection = await GovernanceProjectionService(test_db).project(core_group_id=128)
    decision = await evaluate_outbound_content(
        db=test_db,
        core_group_id=128,
        text="这条消息包含禁词200。",
        content_category="community",
        allowed_promotion_url=None,
        persona_forbidden_topics=(),
        persona_catchphrases=(),
    )

    assert len(projection.forbidden_terms) == 200
    assert "禁词200" not in projection.forbidden_terms
    assert decision.allowed is False
    assert decision.reason_code == "CONTENT_POLICY_BLOCKED"


@pytest.mark.asyncio
async def test_missing_group_policy_fails_closed(test_db) -> None:
    with pytest.raises(OutboundGovernanceError) as error:
        await evaluate_outbound_content(
            db=test_db,
            core_group_id=128,
            text="普通中文消息",
            content_category="community",
            allowed_promotion_url=None,
            persona_forbidden_topics=(),
            persona_catchphrases=(),
        )
    assert error.value.code == "GOVERNANCE_CONTEXT_UNAVAILABLE"
