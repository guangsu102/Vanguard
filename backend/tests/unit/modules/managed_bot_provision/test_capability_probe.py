from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.modules.managed_bot_provision import service as service_module


@pytest.mark.asyncio
async def test_capability_probe_is_bounded_ordered_and_failure_isolated(
    test_db,
    monkeypatch,
):
    owner = TelegramAccount(
        identifier="+15559000000",
        phone="+15559000000",
        session_name="capability-probe-owner",
        session_string="owner-session",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(owner)

    manager_accounts = [
        TelegramAccount(
            identifier=f"@capability_manager_{index}_bot",
            display_name=f"Capability Manager {index}",
            session_name=f"capability-manager-{index}",
            account_type=AccountType.GUARDIAN_BOT,
            status=AccountStatus.IDLE,
            is_active=True,
        )
        for index in range(10)
    ]
    test_db.add_all(manager_accounts)
    await test_db.flush()

    profiles = [
        GuardianBotProfile(
            account_id=account.id,
            bot_token=f"manager-token-{index}",
            bot_username=f"stored_manager_{index}_bot",
            bot_user_id=880000 + index,
            enabled=True,
        )
        for index, account in enumerate(manager_accounts)
    ]
    test_db.add_all(profiles)
    await test_db.commit()
    expected_profile_ids = [profile.id for profile in profiles]

    class FakeTelegramClient:
        active = 0
        peak = 0
        constructed_tokens: list[str] = []
        closed_tokens: list[str] = []

        def __init__(self, config):
            self.token = str(config.bot_token)
            self.__class__.constructed_tokens.append(self.token)

        async def get_me(self):
            index = int(self.token.rsplit("-", 1)[1])
            self.__class__.active += 1
            self.__class__.peak = max(self.__class__.peak, self.__class__.active)
            try:
                # Different delays make completion order differ from profile order.
                await asyncio.sleep(0.005 + ((9 - index) * 0.002))
                if index == 4:
                    raise RuntimeError("isolated probe failure")
                return SimpleNamespace(
                    user_id=990000 + index,
                    username=f"live_manager_{index}_bot",
                    is_bot=True,
                    can_manage_bots=True,
                )
            finally:
                self.__class__.active -= 1

        async def close(self):
            self.__class__.closed_tokens.append(self.token)

    monkeypatch.setattr(service_module, "TelegramClient", FakeTelegramClient)

    result = await service_module.list_manager_capabilities(test_db)
    rows = result["manager_bot_profiles"]

    assert 1 < FakeTelegramClient.peak <= 8
    assert [row["profile_id"] for row in rows] == expected_profile_ids
    assert [row["can_manage_bots"] for row in rows] == [
        index != 4 for index in range(10)
    ]
    assert result["available"] is True
    assert len(FakeTelegramClient.constructed_tokens) == 10
    assert sorted(FakeTelegramClient.closed_tokens) == sorted(
        FakeTelegramClient.constructed_tokens
    )

