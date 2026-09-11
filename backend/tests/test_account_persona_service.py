from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.account.persona import (
    NEUTRAL_PERSONA,
    NEUTRAL_PERSONA_HASH,
    AccountPersonaError,
    AccountPersonaService,
    PersonaSource,
    hash_persona,
)


def persona_payload(name: str = "技术型群友") -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": name,
        "tone": "自然、克制、简短",
        "interests": ["网络稳定性"],
        "expertise": ["技术排障"],
        "reply_length": "short",
        "preferred_topics": ["使用体验"],
        "forbidden_topics": ["过度营销"],
        "ad_style": "soft_share",
        "catchphrases": [],
        "language_style": "zh_cn",
        "system_prompt": "",
    }


def account(
    *,
    account_type: str = "promoter",
    operation_mode: str | None = "growth",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=17,
        account_type=account_type,
        operation_config=(
            None if operation_mode is None else SimpleNamespace(operation_mode=operation_mode)
        ),
        ai_persona=None,
        ai_persona_revision=0,
        ai_persona_hash=None,
        ai_persona_updated_at=None,
        ai_persona_updated_by=None,
    )


class FakeSession:
    def __init__(self) -> None:
        self.flush_count = 0

    async def flush(self) -> None:
        self.flush_count += 1


@pytest.mark.asyncio
async def test_put_safe_replay_precedes_stale_revision_conflict() -> None:
    db = FakeSession()
    target = account()

    created = await AccountPersonaService.put(
        db,
        target,
        persona_payload(),
        expected_revision=0,
        actor_id=2,
    )
    replayed = await AccountPersonaService.put(
        db,
        target,
        persona_payload(),
        expected_revision=0,
        actor_id=99,
    )

    assert created.changed is True
    assert created.revision == 1
    assert replayed.changed is False
    assert replayed.revision == 1
    assert replayed.persona_hash == hash_persona(persona_payload())
    assert target.ai_persona_updated_by == 2
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_put_different_target_with_stale_revision_conflicts() -> None:
    db = FakeSession()
    target = account()
    await AccountPersonaService.put(
        db,
        target,
        persona_payload(),
        expected_revision=0,
        actor_id=2,
    )

    with pytest.raises(AccountPersonaError) as exc_info:
        await AccountPersonaService.put(
            db,
            target,
            persona_payload("另一个群友"),
            expected_revision=0,
            actor_id=3,
        )
    assert exc_info.value.code == "PERSONA_REVISION_CONFLICT"
    assert exc_info.value.details["current_revision"] == 1
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_reset_keeps_history_revision_and_is_safely_replayable() -> None:
    db = FakeSession()
    target = account()
    await AccountPersonaService.put(
        db,
        target,
        persona_payload(),
        expected_revision=0,
        actor_id=2,
    )
    reset = await AccountPersonaService.reset(
        db,
        target,
        expected_revision=1,
        actor_id=3,
    )
    replay = await AccountPersonaService.reset(
        db,
        target,
        expected_revision=1,
        actor_id=4,
    )

    assert reset.configured is False
    assert reset.revision == 2
    assert reset.changed is True
    assert replay.revision == 2
    assert replay.changed is False
    assert target.ai_persona_updated_by == 3
    assert db.flush_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_type", "operation_mode", "expected_code"),
    [
        ("guardian_bot", "growth", "PERSONA_ACCOUNT_TYPE_UNSUPPORTED"),
        ("promoter", "ad_only", "PERSONA_ACCOUNT_MODE_UNSUPPORTED"),
        ("promoter", None, "PERSONA_OPERATION_CONFIG_MISSING"),
    ],
)
async def test_put_rejects_inapplicable_accounts_without_mutation(
    account_type: str,
    operation_mode: str | None,
    expected_code: str,
) -> None:
    db = FakeSession()
    target = account(account_type=account_type, operation_mode=operation_mode)
    with pytest.raises(AccountPersonaError) as exc_info:
        await AccountPersonaService.put(
            db,
            target,
            persona_payload(),
            expected_revision=0,
            actor_id=2,
        )
    assert exc_info.value.code == expected_code
    assert target.ai_persona is None
    assert db.flush_count == 0


@pytest.mark.asyncio
async def test_reset_can_clean_persona_after_account_role_change() -> None:
    db = FakeSession()
    target = account(account_type="guardian_bot", operation_mode=None)
    target.ai_persona = persona_payload()
    target.ai_persona_revision = 4
    target.ai_persona_hash = hash_persona(persona_payload())

    result = await AccountPersonaService.reset(
        db,
        target,
        expected_revision=4,
        actor_id=2,
    )
    assert result.revision == 5
    assert result.configured is False
    assert db.flush_count == 1


def test_snapshot_sources_and_effective_revisions_are_stable() -> None:
    target = account()
    neutral = AccountPersonaService.snapshot_for_new_execution(
        target,
        feature_enabled=True,
    )
    disabled = AccountPersonaService.snapshot_for_new_execution(
        target,
        feature_enabled=False,
    )
    assert neutral.source is PersonaSource.NEUTRAL_DEFAULT
    assert disabled.source is PersonaSource.FEATURE_DISABLED_DEFAULT
    assert neutral.revision == disabled.revision == 0
    assert neutral.persona == disabled.persona == NEUTRAL_PERSONA
    assert neutral.persona_hash == disabled.persona_hash == NEUTRAL_PERSONA_HASH


@pytest.mark.asyncio
async def test_configured_snapshot_fails_closed_on_hash_mismatch() -> None:
    db = FakeSession()
    target = account()
    await AccountPersonaService.put(
        db,
        target,
        persona_payload(),
        expected_revision=0,
        actor_id=2,
    )
    configured = AccountPersonaService.snapshot_for_new_execution(
        target,
        feature_enabled=True,
    )
    assert configured.source is PersonaSource.CONFIGURED
    assert configured.revision == 1

    target.ai_persona_hash = "0" * 64
    with pytest.raises(AccountPersonaError) as exc_info:
        AccountPersonaService.snapshot_for_new_execution(
            target,
            feature_enabled=True,
        )
    assert exc_info.value.code == "PERSONA_CONFIG_INVALID"
