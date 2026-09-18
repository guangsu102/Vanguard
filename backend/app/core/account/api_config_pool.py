"""Platform-aware api_id/app_hash pooling for Telegram accounts.

All accounts authenticating under one api_id share that api_id's anti-abuse
reputation, so the fleet is spread over several registered api_id/app_hash
pairs. Each config declares the device platform it was registered for
(windows/macos/android/ios, or "any" for the legacy env default) and accounts
are matched to a config by their device fingerprint platform, never exceeding
``MAX_ACCOUNTS_PER_API_CONFIG`` accounts per config.

Accounts without an explicit ``api_config_id`` still resolve their credentials
from the environment at connect time, so they are *effectively* served by
whichever config carries the env ``TELEGRAM_API_ID``/``TELEGRAM_API_HASH``
(usually "default"). Effective load — explicit bindings plus that env-fallback
population — is what counts against the per-config cap, both in the report and
in ``pick_api_config_for_platform``.

An api_config binding must only be created or changed together with a fresh
Telegram login (a new auth key): re-declaring a different api_id on an
existing auth key is itself an anti-abuse signal. Assignment helpers in this
module therefore never rebind accounts that already hold a session.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.manager import AccountManager
from app.core.account.models import AccountType, TelegramAccount, TelegramAPIConfig
from app.core.config import get_settings

MAX_ACCOUNTS_PER_API_CONFIG = 30

Platform = Literal["windows", "macos", "android", "ios", "any"]
AUTO_CONFIG_NAMES = {"", "auto"}


def _promoter_filter():
    """API pooling manages the promoter fleet only; bot accounts live outside it."""
    return TelegramAccount.account_type == AccountType.PROMOTER


async def count_accounts_per_api_config(db: AsyncSession) -> dict[int, int]:
    """Return live explicit promoter-account counts keyed by api_config_id."""
    rows = (
        await db.execute(
            select(TelegramAccount.api_config_id, func.count(TelegramAccount.id))
            .where(
                TelegramAccount.api_config_id.is_not(None),
                _promoter_filter(),
            )
            .group_by(TelegramAccount.api_config_id)
        )
    ).all()
    return {int(config_id): int(count) for config_id, count in rows if config_id is not None}


async def count_unbound_accounts(db: AsyncSession) -> int:
    """Count promoter accounts with no explicit config (they use env credentials)."""
    return int(
        (
            await db.execute(
                select(func.count(TelegramAccount.id)).where(
                    TelegramAccount.api_config_id.is_(None),
                    _promoter_filter(),
                )
            )
        ).scalar()
        or 0
    )


def env_fallback_config_id(
    configs: list[TelegramAPIConfig],
    unbound_count: int,
) -> int | None:
    """Return the config id that env-credential accounts effectively use.

    Any account without an explicit binding connects with the env
    ``TELEGRAM_API_ID``/``TELEGRAM_API_HASH``, so it loads whichever config
    carries exactly those credentials — preferring the one named "default".
    """
    if unbound_count <= 0 or not configs:
        return None
    settings = get_settings()
    env_api_id = str(settings.TELEGRAM_API_ID or "")
    env_api_hash = str(settings.TELEGRAM_API_HASH or "")
    if not env_api_id or not env_api_hash:
        return None
    matches = [
        config
        for config in configs
        if str(config.api_id) == env_api_id and str(config.api_hash) == env_api_hash
    ]
    if not matches:
        return None
    default_match = next((config for config in matches if config.name == "default"), None)
    return int((default_match or matches[0]).id)


async def effective_counts_per_api_config(
    db: AsyncSession,
    configs: list[TelegramAPIConfig],
) -> tuple[dict[int, int], int, int | None]:
    """Return (effective counts per config, env-fallback count, env config id)."""
    explicit = await count_accounts_per_api_config(db)
    unbound = await count_unbound_accounts(db)
    env_config_id = env_fallback_config_id(configs, unbound)
    effective = dict(explicit)
    if env_config_id is not None:
        effective[env_config_id] = effective.get(env_config_id, 0) + unbound
    return effective, (unbound if env_config_id is not None else 0), env_config_id


async def pick_api_config_for_platform(
    db: AsyncSession,
    *,
    os_type: str,
    requested_name: str | None = None,
) -> TelegramAPIConfig | None:
    """Choose the api config whose platform matches the device fingerprint.

    An explicit non-default config name is always honored. Otherwise prefer
    the least-loaded platform-matched config under the cap, then any
    least-loaded platform-agnostic config under the cap, and finally the
    legacy "default" config (which may exceed the cap and may mismatch the
    platform) so logins never hard-fail on pool exhaustion. Load includes the
    env-fallback population, so a default config that already serves the whole
    legacy fleet counts as full and new logins flow to fresh configs.
    """
    manager = AccountManager(db)
    name = (requested_name or "").strip()
    if name and name not in AUTO_CONFIG_NAMES and name != "default":
        config = await manager.get_api_config(name)
        if config is not None:
            return config

    configs = (
        (await db.execute(select(TelegramAPIConfig).order_by(TelegramAPIConfig.id.asc())))
        .scalars()
        .all()
    )
    if not configs:
        return await _get_or_create_default(manager)

    counts, _env_fallback, _env_config_id = await effective_counts_per_api_config(db, configs)

    def under_cap(config: TelegramAPIConfig) -> bool:
        return counts.get(int(config.id), 0) < MAX_ACCOUNTS_PER_API_CONFIG

    platform_matched = [
        config for config in configs if config.platform == os_type and under_cap(config)
    ]
    platform_agnostic = [
        config for config in configs if config.platform == "any" and under_cap(config)
    ]
    for candidates in (platform_matched, platform_agnostic):
        if candidates:
            return min(candidates, key=lambda config: (counts.get(int(config.id), 0), config.id))

    default_config = next((config for config in configs if config.name == "default"), None)
    if default_config is not None:
        return default_config
    return await _get_or_create_default(manager)


async def _get_or_create_default(manager: AccountManager) -> TelegramAPIConfig | None:
    """Recreate the env-backed default config when the table is empty."""
    settings = get_settings()
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        return None
    return await manager.create_api_config(
        name="default",
        api_id=str(settings.TELEGRAM_API_ID),
        api_hash=settings.TELEGRAM_API_HASH,
        description="Default config from TELEGRAM_API_ID/TELEGRAM_API_HASH",
    )


def link_account_api_config(account: TelegramAccount, config: TelegramAPIConfig) -> None:
    """Bind an account to a config by id and name (id drives runtime creds)."""
    account.api_config_id = int(config.id)
    account.api_config_name = config.name


async def api_config_assignment_report(db: AsyncSession) -> dict:
    """Summarize configs, effective load and accounts that cannot be rebound."""
    configs = (
        (await db.execute(select(TelegramAPIConfig).order_by(TelegramAPIConfig.id.asc())))
        .scalars()
        .all()
    )
    explicit_counts = await count_accounts_per_api_config(db)
    effective_counts, env_fallback_total, env_config_id = await effective_counts_per_api_config(
        db, configs
    )
    totals = (
        await db.execute(
            select(
                func.count(TelegramAccount.id),
                func.count(TelegramAccount.api_config_id),
                func.count(func.nullif(TelegramAccount.session_string, "")),
            ).where(_promoter_filter())
        )
    ).one()
    accounts_total, accounts_bound, accounts_with_session = (int(value) for value in totals)
    return {
        "max_accounts_per_config": MAX_ACCOUNTS_PER_API_CONFIG,
        "configs": [
            {
                "id": int(config.id),
                "name": config.name,
                "api_id": config.api_id,
                "platform": config.platform,
                "description": config.description,
                "account_count": effective_counts.get(int(config.id), 0),
                "explicit_account_count": explicit_counts.get(int(config.id), 0),
                "env_fallback_count": (
                    env_fallback_total if int(config.id) == env_config_id else 0
                ),
                "under_cap": effective_counts.get(int(config.id), 0) < MAX_ACCOUNTS_PER_API_CONFIG,
            }
            for config in configs
        ],
        "accounts_total": accounts_total,
        "accounts_bound": accounts_bound,
        "accounts_unbound": accounts_total - accounts_bound,
        "accounts_env_fallback": env_fallback_total,
        "accounts_with_session": accounts_with_session,
    }
