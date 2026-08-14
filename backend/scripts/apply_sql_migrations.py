from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "migrations"
DEFAULT_MIGRATIONS = [
    "010_add_telegram_account_session_string.sql",
    "011_acquisition_message_type_varchar.sql",
    "012_group_pool_memberships.sql",
    "013_acquisition_automation_ads.sql",
    "014_account_keyword_replenish_policy.sql",
    "015_growth_guardian_refactor.sql",
    "016_xboard_acquisition_tracking_worker.sql",
    "017_campaign_execution.sql",
    "018_group_search_keyword_usage.sql",
    "019_group_search_keyword_normalized.sql",
    "020_keyword_trigger_review.sql",
    "021_account_proxy_policy.sql",
    "022_ad_warmup_dynamic_state.sql",
    "023_ad_capacity_survival_profile_bio.sql",
    "024_add_account_asset_tier.sql",
    "025_add_account_business_stage.sql",
    "026_add_ad_campaign_target_groups.sql",
    "027_add_qq_official.sql",
    "028_add_group_failover_tasks.sql",
]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _split_sql_statements(content: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    i = 0
    length = len(content)
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    dollar_tag: str | None = None

    while i < length:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < length else ""

        if in_line_comment:
            buffer.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buffer.append(ch)
            if ch == "*" and nxt == "/":
                buffer.append(nxt)
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if dollar_tag is not None:
            if content.startswith(dollar_tag, i):
                buffer.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buffer.append(ch)
                i += 1
            continue

        if not in_single and not in_double:
            if ch == "-" and nxt == "-":
                buffer.extend([ch, nxt])
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                buffer.extend([ch, nxt])
                in_block_comment = True
                i += 2
                continue
            if ch == "$":
                end = content.find("$", i + 1)
                if end != -1:
                    candidate = content[i : end + 1]
                    if all(c.isalnum() or c == "_" or c == "$" for c in candidate):
                        dollar_tag = candidate
                        buffer.append(candidate)
                        i = end + 1
                        continue
            if ch == ";":
                statement = "".join(buffer).strip()
                if statement:
                    statements.append(statement)
                buffer = []
                i += 1
                continue

        if ch == "'" and not in_double:
            if in_single and nxt == "'":
                buffer.extend([ch, nxt])
                i += 2
                continue
            in_single = not in_single
            buffer.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            buffer.append(ch)
            i += 1
            continue

        buffer.append(ch)
        i += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


async def _apply(files: list[str]) -> None:
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)

    history_table_sql = """
    CREATE TABLE IF NOT EXISTS schema_migration_history (
        filename VARCHAR(255) PRIMARY KEY,
        checksum VARCHAR(64) NOT NULL,
        applied_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """

    async with db_module.get_db_session() as session:
        await session.execute(text(history_table_sql))

        for filename in files:
            path = MIGRATIONS_DIR / filename
            if not path.exists():
                raise FileNotFoundError(f"Migration not found: {path}")

            content = path.read_text(encoding="utf-8")
            checksum = _sha256(content)
            existing = await session.execute(
                text("SELECT checksum FROM schema_migration_history WHERE filename = :filename"),
                {"filename": filename},
            )
            row = existing.first()
            if row is not None:
                if row[0] != checksum:
                    raise RuntimeError(f"Migration checksum changed after apply: {filename}")
                print(f"skip {filename} (already applied)")
                continue

            print(f"apply {filename}")
            for statement in _split_sql_statements(content):
                await session.execute(text(statement))

            await session.execute(
                text(
                    """
                    INSERT INTO schema_migration_history (filename, checksum)
                    VALUES (:filename, :checksum)
                    """
                ),
                {"filename": filename, "checksum": checksum},
            )
            await session.commit()

    await db_module.close_db()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply curated PostgreSQL SQL migrations.")
    parser.add_argument(
        "--files",
        nargs="*",
        help="Optional explicit migration filenames. Defaults to curated production patch set.",
    )
    args = parser.parse_args()
    files = args.files or DEFAULT_MIGRATIONS
    asyncio.run(_apply(files))
    print("sql migrations applied successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
