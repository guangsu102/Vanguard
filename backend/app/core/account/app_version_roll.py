"""Gradual roll of declared Telegram app versions onto the current pool.

A stale declared app_version (an app nobody runs anymore) is itself an
anti-spam signal, so accounts whose stored version fell out of the current
pool are moved onto a fresh one a few at a time. Only the declared
``app_version`` changes — apps update in place on real devices, so this does
not require a new auth key. The account is invalidated in all pools so its
next connect picks up the new value.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import TelegramAccount
from app.core.account.pool import (
    get_account_pool,
    invalidate_account_in_all_pools,
)
from app.core.network.fingerprint import FingerprintManager


def _fingerprint_key(account: TelegramAccount) -> str:
    return (
        account.fingerprint_id
        or account.phone
        or account.identifier
        or account.session_name
        or str(account.id)
    )


def _rolled_app_version(account: TelegramAccount) -> str | None:
    """Return the app version the current pool assigns to this account key."""
    profile = FingerprintManager().generate_telegram_device_profile(
        _fingerprint_key(account),
        device_model=account.device_model,
        system_version=account.system_version,
        app_version=None,
    )
    return profile.get("app_version") or None


async def roll_outdated_app_versions(
    db: AsyncSession,
    *,
    batch_size: int = 3,
) -> dict[str, object]:
    """Move up to ``batch_size`` outdated accounts onto the current pool.

    Accounts that are mid-operation in the local pool are skipped; cross-process
    workers observe the change on their next account reload.
    """
    accounts = (
        (await db.execute(select(TelegramAccount).order_by(TelegramAccount.id.asc())))
        .scalars()
        .all()
    )
    pool = get_account_pool()
    rolled: list[dict[str, object]] = []
    skipped_busy = 0
    for account in accounts:
        if len(rolled) >= max(1, int(batch_size)):
            break
        current = (account.app_version or "").strip()
        target = _rolled_app_version(account)
        if not target or current == target:
            continue
        wrapper = await pool.get_account_by_id(account.id)
        if wrapper is not None and getattr(wrapper, "status", None) is not None:
            status_value = getattr(wrapper.status, "value", wrapper.status)
            if status_value == "working":
                skipped_busy += 1
                continue
        account.app_version = target
        rolled.append(
            {
                "account_id": account.id,
                "from": current or None,
                "to": target,
            }
        )
    if rolled:
        await db.commit()
        for entry in rolled:
            await invalidate_account_in_all_pools(
                int(entry["account_id"]),
                reason="app_version_rolled",
            )
    # Rolled accounts already carry the new version in this session, so the
    # recount below reflects the post-roll state.
    outdated_total = 0
    for account in accounts:
        target = _rolled_app_version(account)
        if target and (account.app_version or "").strip() != target:
            outdated_total += 1
    return {
        "scanned": len(accounts),
        "rolled": rolled,
        "rolled_count": len(rolled),
        "skipped_busy": skipped_busy,
        "outdated_remaining": outdated_total,
    }
