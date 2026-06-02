import pytest

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group
from app.modules.guardian.models import ManagedGroupBinding, ManagedGroupBindingStatus, ManagedGroupBotRole


async def _create_guardian_bot(test_db, identifier: str = "@guardian_bot") -> TelegramAccount:
    account = TelegramAccount(
        identifier=identifier,
        account_type=AccountType.GUARDIAN_BOT,
        session_name=f"session_{identifier.strip('@')}",
        api_config_name="default",
        country_code="US",
        status=AccountStatus.OFFLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    return account


async def _create_promoter(test_db, identifier: str = "+10000000001") -> TelegramAccount:
    account = TelegramAccount(
        identifier=identifier,
        phone=identifier,
        account_type=AccountType.PROMOTER,
        session_name=f"session_{identifier.replace('+', '')}",
        api_config_name="default",
        country_code="US",
        status=AccountStatus.OFFLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    return account


async def _create_managed_group(test_db, bot_account_id: int, telegram_group_id: int = 10001) -> ManagedGroupBinding:
    group = Group(
        group_id=telegram_group_id,
        title=f"Managed {telegram_group_id}",
        username=f"managed_{telegram_group_id}",
        discovery_source="guardian_binding",
    )
    test_db.add(group)
    await test_db.commit()
    await test_db.refresh(group)

    binding = ManagedGroupBinding(
        group_id=group.id,
        telegram_group_id=telegram_group_id,
        bot_account_id=bot_account_id,
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
    )
    test_db.add(binding)
    await test_db.commit()
    await test_db.refresh(binding)
    return binding


@pytest.mark.asyncio
async def test_group_governance_requires_managed_group(client):
    response = await client.get("/api/group-governance/verification/99999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Managed group binding not found"


@pytest.mark.asyncio
async def test_sensitive_keyword_create_rejects_unmanaged_group(client):
    response = await client.post(
        "/api/moderation-sensitive-keywords",
        json={
            "text": "spam",
            "category": "ads",
            "source": "manual",
            "level": "medium",
            "action": "warn",
            "group_id": 99999,
            "enabled": True,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Managed group binding not found"


@pytest.mark.asyncio
async def test_managed_group_campaign_requires_guardian_bot_and_bound_group(client, test_db):
    guardian_bot = await _create_guardian_bot(test_db)
    await _create_managed_group(test_db, guardian_bot.id, telegram_group_id=10001)

    response = await client.post(
        "/api/campaigns",
        json={
            "name": "Managed Campaign A",
            "campaign_type": "discount",
            "campaign_scope": "managed_group",
            "trigger_timing": "immediate",
            "trigger_event": "user_joined",
            "target_group_ids": [10001, 20002],
            "bot_account_id": guardian_bot.id,
            "enabled": False,
        },
    )

    assert response.status_code == 404
    assert "Managed group binding not found for Telegram group(s): 20002" in response.json()["detail"]


@pytest.mark.asyncio
async def test_managed_group_campaign_rejects_promoter_account(client, test_db):
    promoter = await _create_promoter(test_db)
    guardian_bot = await _create_guardian_bot(test_db, identifier="@guardian_bot_b")
    await _create_managed_group(test_db, guardian_bot.id, telegram_group_id=10001)

    response = await client.post(
        "/api/campaigns",
        json={
            "name": "Managed Campaign B",
            "campaign_type": "discount",
            "campaign_scope": "managed_group",
            "trigger_timing": "immediate",
            "trigger_event": "user_joined",
            "target_group_ids": [10001],
            "bot_account_id": promoter.id,
            "enabled": False,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "bot_account_id must reference a guardian_bot account"


@pytest.mark.asyncio
async def test_managed_group_campaign_normalizes_legacy_trigger_event(client, test_db):
    guardian_bot = await _create_guardian_bot(test_db, identifier="@guardian_bot_c")
    await _create_managed_group(test_db, guardian_bot.id, telegram_group_id=10001)

    response = await client.post(
        "/api/campaigns",
        json={
            "name": "Managed Campaign Legacy",
            "campaign_type": "discount",
            "campaign_scope": "managed_group",
            "trigger_timing": "immediate",
            "trigger_event": "member_join",
            "target_group_ids": [10001],
            "bot_account_id": guardian_bot.id,
            "enabled": False,
        },
    )

    assert response.status_code == 201
    assert response.json()["trigger_event"] == "user_joined"
    assert response.json()["trigger_timing"] == "immediate"


@pytest.mark.asyncio
async def test_managed_group_campaign_rejects_trigger_timing_mismatch(client, test_db):
    guardian_bot = await _create_guardian_bot(test_db, identifier="@guardian_bot_d")
    await _create_managed_group(test_db, guardian_bot.id, telegram_group_id=10001)

    response = await client.post(
        "/api/campaigns",
        json={
            "name": "Managed Campaign Timing Mismatch",
            "campaign_type": "discount",
            "campaign_scope": "managed_group",
            "trigger_timing": "scheduled",
            "trigger_event": "user_joined",
            "target_group_ids": [10001],
            "bot_account_id": guardian_bot.id,
            "enabled": False,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "trigger_timing must be 'immediate' for trigger_event 'user_joined'"
