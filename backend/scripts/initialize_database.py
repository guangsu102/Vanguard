"""Create the SQLAlchemy base schema for a brand-new production database.

Production startup intentionally disables ``create_all``.  This one-shot
bootstrap is used only before the curated incremental migrations on a fresh
Vanguard database; subsequent deployments leave the schema untouched and run
the migration history normally.
"""

from __future__ import annotations

import asyncio

from app.core.database import close_db, init_db


async def main() -> None:
    await init_db(create_tables=True)
    await close_db()
    print("base schema initialized")


if __name__ == "__main__":
    asyncio.run(main())
