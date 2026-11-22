"""Create the SQLAlchemy base schema for a brand-new production database.

Production startup intentionally disables ``create_all``.  This one-shot
bootstrap is used only before the curated incremental migrations on a fresh
Vanguard database; subsequent deployments leave the schema untouched and run
the migration history normally.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.core import database
from app.core.database import close_db, init_db


async def baseline_qq_growth_schema() -> None:
    """Record the historical QQ DDL already created by the current models.

    Migration 065 predates this bootstrap and is intentionally non-idempotent.
    Its checksum must remain unchanged for existing production installations.
    Validate its model-created tables before recording the fresh-schema baseline.
    """
    filename = "065_qq_growth_automation.sql"
    migration = Path(__file__).resolve().parents[1] / "migrations" / filename
    checksum = hashlib.sha256(migration.read_text(encoding="utf-8").encode()).hexdigest()
    table_names = (
        "qq_bot_connection",
        "qq_managed_group",
        "qq_ad_campaign",
        "qq_campaign_target",
        "qq_ad_binding",
        "qq_join_task",
        "qq_ad_schedule",
        "qq_automation_log",
    )
    if database.engine is None:
        raise RuntimeError("Database was not initialized")
    async with database.engine.begin() as connection:

        def validate_schema(sync_connection: Connection) -> None:
            inspector = inspect(sync_connection)
            for name in table_names:
                expected = database.Base.metadata.tables[name]
                actual_columns = {column["name"] for column in inspector.get_columns(name)}
                if not set(expected.columns.keys()).issubset(actual_columns):
                    raise RuntimeError(f"Incomplete fresh QQ schema: {name}")
                actual_indexes = {index["name"] for index in inspector.get_indexes(name)}
                if not {index.name for index in expected.indexes}.issubset(actual_indexes):
                    raise RuntimeError(f"Incomplete fresh QQ indexes: {name}")

        await connection.run_sync(validate_schema)
        await connection.execute(
            text("""
            CREATE TABLE IF NOT EXISTS schema_migration_history (
                filename VARCHAR(255) PRIMARY KEY,
                checksum VARCHAR(64) NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        )
        existing = await connection.scalar(
            text("SELECT checksum FROM schema_migration_history WHERE filename = :filename"),
            {"filename": filename},
        )
        if existing is not None and existing != checksum:
            raise RuntimeError(f"Migration checksum changed after apply: {filename}")
        await connection.execute(
            text("""
                INSERT INTO schema_migration_history (filename, checksum)
                VALUES (:filename, :checksum) ON CONFLICT (filename) DO NOTHING
            """),
            {"filename": filename, "checksum": checksum},
        )
    print(f"baseline {filename} (verified model-created schema)")


async def main() -> None:
    await init_db(create_tables=True)
    try:
        await baseline_qq_growth_schema()
    finally:
        await close_db()
    print("base schema initialized")


if __name__ == "__main__":
    asyncio.run(main())
