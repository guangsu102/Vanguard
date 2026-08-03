"""Backfill plaintext Telegram session_string values with encrypted values.

Default mode is dry-run. Use --apply to persist changes.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from app.core.account.models import TelegramAccount
from app.core.account.session_crypto import SessionCryptoError, decrypt_session_string, encrypt_session_string, get_session_crypto_service
from app.core.database import close_db, get_db_session, init_db


@dataclass
class BackfillResult:
    scanned: int = 0
    empty: int = 0
    already_encrypted: int = 0
    encrypted: int = 0
    failed: int = 0


def _masked(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-6:]}"


async def backfill_session_strings(*, apply: bool, limit: int | None = None) -> BackfillResult:
    await init_db(create_tables=False)
    service = get_session_crypto_service()
    result = BackfillResult()
    try:
        async with get_db_session() as db:
            query = select(TelegramAccount).order_by(TelegramAccount.id)
            if limit:
                query = query.limit(limit)
            accounts = (await db.execute(query)).scalars().all()
            for account in accounts:
                result.scanned += 1
                raw = account.session_string
                if not raw:
                    result.empty += 1
                    continue
                if service.is_encrypted(raw):
                    try:
                        decrypt_session_string(raw)
                    except SessionCryptoError:
                        result.failed += 1
                        print(f"FAILED encrypted account_id={account.id} session={account.session_name}: decrypt_error")
                        continue
                    result.already_encrypted += 1
                    continue

                encrypted = encrypt_session_string(raw)
                if not encrypted or encrypted == raw:
                    result.failed += 1
                    print(f"FAILED plaintext account_id={account.id} session={account.session_name}: encrypt_noop")
                    continue

                result.encrypted += 1
                print(
                    f"{'APPLY' if apply else 'DRY'} account_id={account.id} "
                    f"session={account.session_name} value={_masked(raw)}"
                )
                if apply:
                    account.session_string = encrypted
                    db.add(account)
            if not apply:
                await db.rollback()
    finally:
        await close_db()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt plaintext Telegram session_string values.")
    parser.add_argument("--apply", action="store_true", help="Persist encrypted session strings")
    parser.add_argument("--limit", type=int, default=None, help="Optional max accounts to scan")
    args = parser.parse_args()

    result = asyncio.run(backfill_session_strings(apply=args.apply, limit=args.limit))
    print(
        "summary "
        f"scanned={result.scanned} empty={result.empty} "
        f"already_encrypted={result.already_encrypted} encrypted={result.encrypted} failed={result.failed} "
        f"mode={'apply' if args.apply else 'dry-run'}"
    )


if __name__ == "__main__":
    main()