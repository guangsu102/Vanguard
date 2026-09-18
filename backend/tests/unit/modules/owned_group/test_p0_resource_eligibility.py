from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.config import settings
from app.core.p0_safety_gate import precheck_owned_group_resources
from app.modules.owned_group.models_extra import OwnedBotProfile


def _valid_string_session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


_VALID_STRING_SESSION = _valid_string_session()


async def _account(
    db,
    identifier: str,
    *,
    account_type: AccountType = AccountType.PROMOTER,
    status: AccountStatus = AccountStatus.ONLINE,
    session_string: str | None = _VALID_STRING_SESSION,
    is_active: bool = True,
    risk_level: str = "normal",
    risk_pause_until: datetime | None = None,
    spam_check_status: str = "unknown",
) -> TelegramAccount:
    account = TelegramAccount(
        identifier=identifier,
        session_name=identifier,
        account_type=account_type,
        status=status,
        session_string=session_string,
        is_active=is_active,
        risk_level=risk_level,
        risk_pause_until=risk_pause_until,
        spam_check_status=spam_check_status,
    )
    db.add(account)
    await db.flush()
    return account


@pytest.mark.asyncio
async def test_precheck_rejects_runtime_unready_user(test_db):
    owner = await _account(test_db, "p0-owner")
    offline = await _account(
        test_db,
        "p0-offline",
        status=AccountStatus.OFFLINE,
        session_string=None,
    )

    decision = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "user", "resource_id": offline.id},
        ],
        owner.id,
    )

    assert decision.allowed is False
    assert decision.reason == "resource_eligibility_failed"
    assert any(
        item["resource_id"] == offline.id and item["reason"] == "account_status_not_ready"
        for item in decision.details["violations"]
    )


@pytest.mark.parametrize(
    ("blocked_status", "spam_check_status", "expected_reason"),
    [
        (AccountStatus.RESTRICTED, "clear", "account_restricted"),
        (AccountStatus.BANNED, "clear", "account_banned"),
        (AccountStatus.ERROR, "clear", "account_error"),
        (AccountStatus.ONLINE, "restricted", "account_spam_restricted"),
    ],
)
@pytest.mark.asyncio
async def test_precheck_rejects_blocked_account_when_runtime_readiness_is_deferred(
    test_db,
    blocked_status,
    spam_check_status,
    expected_reason,
):
    owner = await _account(test_db, "p0-deferred-owner")
    blocked = await _account(
        test_db,
        "p0-deferred-blocked-" + expected_reason,
        status=blocked_status,
        spam_check_status=spam_check_status,
    )

    decision = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "user", "resource_id": blocked.id},
        ],
        owner.id,
        require_runtime_ready=False,
    )

    assert decision.allowed is False
    assert any(
        item["resource_id"] == blocked.id and item["reason"] == expected_reason
        for item in decision.details["violations"]
    )


@pytest.mark.asyncio
async def test_precheck_refreshes_account_state_changed_by_another_session(test_db):
    owner = await _account(test_db, "p0-stale-owner")
    await test_db.commit()

    # Keep the original ONLINE object in this session's identity map while a
    # separate worker/session records a restriction after resource selection.
    assert owner.status == AccountStatus.ONLINE
    session_factory = async_sessionmaker(test_db.bind, expire_on_commit=False)
    async with session_factory() as external_db:
        updated_owner = await external_db.get(TelegramAccount, owner.id)
        assert updated_owner is not None
        updated_owner.status = AccountStatus.RESTRICTED
        await external_db.commit()

    assert owner.status == AccountStatus.ONLINE
    decision = await precheck_owned_group_resources(
        test_db,
        [{"resource_type": "user", "resource_id": owner.id}],
        owner.id,
        require_runtime_ready=False,
    )

    assert decision.allowed is False
    assert owner.status == AccountStatus.RESTRICTED
    assert any(
        item["resource_id"] == owner.id and item["reason"] == "account_restricted"
        for item in decision.details["violations"]
    )


@pytest.mark.asyncio
async def test_precheck_rejects_undecryptable_session_ciphertext(test_db):
    owner = await _account(
        test_db, "p0-invalid-ciphertext", session_string="vgs1:not-a-fernet-token"
    )

    decision = await precheck_owned_group_resources(
        test_db,
        [{"resource_type": "user", "resource_id": owner.id}],
        owner.id,
    )

    assert decision.allowed is False
    assert decision.details["violations"][0]["reason"] == "account_session_missing"


@pytest.mark.asyncio
async def test_precheck_rejects_invalid_plaintext_session(test_db):
    owner = await _account(
        test_db,
        "p0-invalid-plaintext",
        session_string="legacy-session",
    )

    decision = await precheck_owned_group_resources(
        test_db,
        [{"resource_type": "user", "resource_id": owner.id}],
        owner.id,
    )

    assert decision.allowed is False
    assert decision.details["violations"][0]["reason"] == "account_session_missing"


@pytest.mark.asyncio
async def test_precheck_can_validate_references_before_runtime_check(test_db):
    owner = await _account(test_db, "p0-owner-deferred")
    offline = await _account(
        test_db,
        "p0-offline-deferred",
        status=AccountStatus.OFFLINE,
        session_string=None,
    )

    decision = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "user", "resource_id": offline.id},
        ],
        owner.id,
        require_runtime_ready=False,
    )

    assert decision.allowed is True
    assert decision.details["selected_accounts"] == 2


@pytest.mark.asyncio
async def test_precheck_enforces_staged_rollout_limit(test_db, monkeypatch):
    accounts = [await _account(test_db, f"p0-rollout-{index}") for index in range(3)]
    monkeypatch.setattr(settings, "OWNED_GROUP_ROLLOUT_MAX_ACCOUNTS", 2)

    decision = await precheck_owned_group_resources(
        test_db,
        [{"resource_type": "user", "resource_id": account.id} for account in accounts],
        accounts[0].id,
        require_runtime_ready=False,
    )

    assert decision.allowed is False
    assert any(
        item["reason"] == "rollout_account_limit_exceeded"
        for item in decision.details["violations"]
    )


@pytest.mark.asyncio
async def test_precheck_requires_verified_owned_bot_profile(test_db):
    owner = await _account(test_db, "p0-bot-owner")
    bot_account = await _account(
        test_db,
        "p0-bot-account",
        account_type=AccountType.GUARDIAN_BOT,
    )
    profile = OwnedBotProfile(
        owner_account_id=owner.id,
        account_id=bot_account.id,
        token_ciphertext="encrypted-token",
        status="pending_verification",
        enabled=True,
    )
    test_db.add(profile)
    await test_db.flush()

    pending = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "bot", "resource_id": profile.id},
        ],
        owner.id,
    )
    assert pending.allowed is False
    assert any(
        item["reason"] == "bot_profile_not_verified" for item in pending.details["violations"]
    )

    profile.status = "verified"
    await test_db.flush()
    verified = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "bot", "resource_id": profile.id},
        ],
        owner.id,
    )
    assert verified.allowed is True


@pytest.mark.asyncio
async def test_precheck_rejects_bot_profile_bound_to_promoter_account(test_db):
    owner = await _account(test_db, "p0-bot-type-owner")
    # A profile may be accidentally linked to a promoter row because the
    # polymorphic foreign key cannot express account_type at the DB level.
    # The safety gate must reject that before any Bot API adapter is invoked.
    promoter_bot = await _account(test_db, "p0-promoter-as-bot")
    profile = OwnedBotProfile(
        owner_account_id=owner.id,
        account_id=promoter_bot.id,
        token_ciphertext="encrypted-token",
        status="verified",
        enabled=True,
    )
    test_db.add(profile)
    await test_db.flush()

    decision = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "bot", "resource_id": profile.id},
        ],
        owner.id,
    )

    assert decision.allowed is False
    assert any(
        item["reason"] == "bot_account_type_invalid" for item in decision.details["violations"]
    )


@pytest.mark.asyncio
async def test_precheck_blocks_risk_cooldown(test_db):
    owner = await _account(
        test_db,
        "p0-risk-owner",
        risk_pause_until=datetime.utcnow() + timedelta(minutes=5),
    )

    decision = await precheck_owned_group_resources(
        test_db,
        [{"resource_type": "user", "resource_id": owner.id}],
        owner.id,
    )

    assert decision.allowed is False
    assert decision.details["violations"][0]["reason"] == "account_cooldown"


@pytest.mark.asyncio
async def test_bot_precheck_keeps_risk_guard_without_user_session_requirement(test_db):
    owner = await _account(test_db, "p0-bot-risk-owner")
    bot_account = await _account(
        test_db,
        "p0-bot-risk-account",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.OFFLINE,
        session_string=None,
        risk_level="frozen",
    )
    profile = OwnedBotProfile(
        owner_account_id=owner.id,
        account_id=bot_account.id,
        token_ciphertext="encrypted-token",
        status="verified",
        enabled=True,
    )
    test_db.add(profile)
    await test_db.flush()

    decision = await precheck_owned_group_resources(
        test_db,
        [
            {"resource_type": "user", "resource_id": owner.id},
            {"resource_type": "bot", "resource_id": profile.id},
        ],
        owner.id,
        require_runtime_ready=False,
    )

    assert decision.allowed is False
    assert any(
        item["resource_id"] == profile.id and item["reason"] == "account_risk_blocked"
        for item in decision.details["violations"]
    )
