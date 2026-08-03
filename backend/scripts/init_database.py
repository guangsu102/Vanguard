from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.database import close_db, init_db


async def main() -> None:
    await init_db(create_tables=not settings.is_production)
    await close_db()
    if settings.is_production:
        print("database connection ready; schema changes are managed by migrations")
    else:
        print("database schema ready")


if __name__ == "__main__":
    asyncio.run(main())
