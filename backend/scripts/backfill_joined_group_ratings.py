"""
Backfill automatic ratings for groups that already have joined memberships.

Run inside the backend container or any environment with the production
DATABASE_URL configured.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.database import close_db, get_db_session, init_db
from app.core.group.auto_rating import apply_joined_group_auto_rating
from app.core.group.manager import GroupManager
from app.core.group.models import Group, GroupAccountMembership


async def main() -> None:
    await init_db(create_tables=False)
    try:
        async with get_db_session() as db:
            manager = GroupManager(db)
            result = await db.execute(
                select(Group)
                .join(GroupAccountMembership)
                .where(GroupAccountMembership.status == "joined")
                .distinct()
                .order_by(Group.id)
            )
            groups = list(result.scalars().all())

            rated = 0
            skipped = 0
            for group in groups:
                if await apply_joined_group_auto_rating(
                    group,
                    manager.scorer,
                    recompute_existing_auto_rating=True,
                ):
                    rated += 1
                else:
                    skipped += 1

            await db.commit()
            print(f"scanned={len(groups)} rated={rated} skipped={skipped}")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
