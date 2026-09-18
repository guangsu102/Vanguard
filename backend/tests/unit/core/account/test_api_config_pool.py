"""Platform-aware api_id/app_hash pooling: selection, capping and linking."""

import pytest

from app.core.account.api_config_pool import (
    MAX_ACCOUNTS_PER_API_CONFIG,
    api_config_assignment_report,
    link_account_api_config,
    pick_api_config_for_platform,
)
from app.core.account.manager import AccountManager
from app.core.account.models import (
    AccountType,
    TelegramAccount,
    TelegramAPIConfig,
)


async def _create_config(db, name: str, platform: str = "any") -> TelegramAPIConfig:
    manager = AccountManager(db)
    return await manager.create_api_config(
        name=name,
        api_id="1000",
        api_hash=f"hash-{name}",
        platform=platform,
    )


def _account(session_name: str, api_config_id: int | None = None) -> TelegramAccount:
    return TelegramAccount(
        identifier=session_name,
        session_name=session_name,
        account_type=AccountType.PROMOTER,
        api_config_id=api_config_id,
        status="offline",
    )


@pytest.mark.asyncio
async def test_explicit_config_name_is_honored(test_db):
    await _create_config(test_db, "default", platform="any")
    ios_config = await _create_config(test_db, "ios-main", platform="ios")

    picked = await pick_api_config_for_platform(
        test_db, os_type="ios", requested_name="ios-main"
    )

    assert picked is not None and picked.id == ios_config.id


@pytest.mark.asyncio
async def test_platform_match_prefers_least_loaded_under_cap(test_db):
    await _create_config(test_db, "default", platform="any")
    ios_a = await _create_config(test_db, "ios-a", platform="ios")
    ios_b = await _create_config(test_db, "ios-b", platform="ios")
    test_db.add(_account("ios-a-user", api_config_id=ios_a.id))
    await test_db.commit()

    picked = await pick_api_config_for_platform(test_db, os_type="ios")

    assert picked is not None and picked.id == ios_b.id


@pytest.mark.asyncio
async def test_platform_agnostic_config_used_when_platform_pools_full(test_db):
    any_config = await _create_config(test_db, "default", platform="any")
    ios_config = await _create_config(test_db, "ios-a", platform="ios")
    test_db.add_all(
        _account(f"ios-full-{idx}", api_config_id=ios_config.id)
        for idx in range(MAX_ACCOUNTS_PER_API_CONFIG)
    )
    await test_db.commit()

    picked = await pick_api_config_for_platform(test_db, os_type="ios")

    assert picked is not None and picked.id == any_config.id


@pytest.mark.asyncio
async def test_default_config_is_last_resort_when_all_full(test_db):
    default_config = await _create_config(test_db, "default", platform="any")
    ios_config = await _create_config(test_db, "ios-a", platform="ios")
    test_db.add_all(
        _account(f"ios-full-{idx}", api_config_id=ios_config.id)
        for idx in range(MAX_ACCOUNTS_PER_API_CONFIG)
    )
    test_db.add_all(
        _account(f"any-full-{idx}", api_config_id=default_config.id)
        for idx in range(MAX_ACCOUNTS_PER_API_CONFIG)
    )
    await test_db.commit()

    picked = await pick_api_config_for_platform(test_db, os_type="ios")

    assert picked is not None and picked.id == default_config.id


@pytest.mark.asyncio
async def test_link_account_sets_id_and_name(test_db):
    config = await _create_config(test_db, "android-main", platform="android")
    account = _account("link-me")
    test_db.add(account)
    await test_db.commit()

    link_account_api_config(account, config)

    assert account.api_config_id == config.id
    assert account.api_config_name == "android-main"


@pytest.mark.asyncio
async def test_env_fallback_accounts_count_toward_matching_config(test_db, monkeypatch):
    """Unbound accounts use env credentials, so they load the env-matching config."""
    from types import SimpleNamespace

    import app.core.account.api_config_pool as pool_module

    monkeypatch.setattr(
        pool_module,
        "get_settings",
        lambda: SimpleNamespace(TELEGRAM_API_ID="28451540", TELEGRAM_API_HASH="env-hash"),
    )
    default_config = await _create_config(test_db, "default", platform="any")
    # Make the default config carry exactly the env credentials.
    default_config.api_id = "28451540"
    default_config.api_hash = "env-hash"
    other_config = await _create_config(test_db, "ios-main", platform="ios")
    test_db.add_all(
        _account(f"explicit-{idx}", api_config_id=default_config.id) for idx in range(2)
    )
    test_db.add_all(_account(f"fallback-{idx}") for idx in range(3))
    await test_db.commit()

    report = await pool_module.api_config_assignment_report(test_db)

    entry = next(item for item in report["configs"] if item["name"] == "default")
    assert entry["account_count"] == 5
    assert entry["explicit_account_count"] == 2
    assert entry["env_fallback_count"] == 3
    assert entry["under_cap"] is True
    ios_entry = next(item for item in report["configs"] if item["name"] == "ios-main")
    assert ios_entry["env_fallback_count"] == 0
    assert report["accounts_env_fallback"] == 3
    assert report["accounts_unbound"] == 3

    picked = await pool_module.pick_api_config_for_platform(test_db, os_type="ios")
    assert picked is not None and picked.id == other_config.id


@pytest.mark.asyncio
async def test_env_fallback_full_default_pushes_new_logins_to_other_configs(
    test_db, monkeypatch
):
    """A default config already serving the whole legacy fleet counts as full."""
    from types import SimpleNamespace

    import app.core.account.api_config_pool as pool_module

    monkeypatch.setattr(
        pool_module,
        "get_settings",
        lambda: SimpleNamespace(TELEGRAM_API_ID="28451540", TELEGRAM_API_HASH="env-hash"),
    )
    default_config = await _create_config(test_db, "default", platform="any")
    default_config.api_id = "28451540"
    default_config.api_hash = "env-hash"
    ios_config = await _create_config(test_db, "ios-main", platform="ios")
    test_db.add_all(
        _account(f"legacy-{idx}") for idx in range(MAX_ACCOUNTS_PER_API_CONFIG)
    )
    await test_db.commit()

    picked = await pool_module.pick_api_config_for_platform(test_db, os_type="ios")

    assert picked is not None and picked.id == ios_config.id


@pytest.mark.asyncio
async def test_report_and_counts_exclude_guardian_bot_accounts(test_db):
    """API pooling manages the promoter fleet only; bot accounts stay out."""
    config = await _create_config(test_db, "default", platform="any")
    guardian = TelegramAccount(
        identifier="@some_managed_bot",
        session_name="guardian_managed_1",
        account_type=AccountType.GUARDIAN_BOT,
        api_config_id=config.id,
        status="offline",
    )
    promoter = _account("promoter-1", api_config_id=config.id)
    promoter.session_string = "enc"
    unbound_promoter = _account("promoter-2")
    test_db.add_all([guardian, promoter, unbound_promoter])
    await test_db.commit()

    report = await api_config_assignment_report(test_db)

    assert report["accounts_total"] == 2
    assert report["accounts_bound"] == 1
    assert report["accounts_unbound"] == 1
    assert report["accounts_with_session"] == 1
    entry = next(item for item in report["configs"] if item["name"] == "default")
    assert entry["explicit_account_count"] == 1


@pytest.mark.asyncio
async def test_assignment_report_counts_sessions_and_bindings(test_db):
    config = await _create_config(test_db, "default", platform="any")
    bound = _account("bound", api_config_id=config.id)
    bound.session_string = "enc"
    unbound = _account("unbound")
    test_db.add_all([bound, unbound])
    await test_db.commit()

    report = await api_config_assignment_report(test_db)

    assert report["max_accounts_per_config"] == MAX_ACCOUNTS_PER_API_CONFIG
    assert report["accounts_total"] == 2
    assert report["accounts_bound"] == 1
    assert report["accounts_unbound"] == 1
    assert report["accounts_with_session"] == 1
    entry = next(item for item in report["configs"] if item["name"] == "default")
    assert entry["platform"] == "any"
    assert entry["account_count"] == 1
    assert entry["under_cap"] is True
