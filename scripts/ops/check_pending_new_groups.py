import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc in [33, 35]:
            q = (
                select(Member, Group)
                .join(Group, Group.id == Member.group_id)
                .where(
                    Member.account_id == acc,
                    Member.status == "joined",
                    Member.review_status == "pending"
                )
            )
            rows = (await session.execute(q)).all()
            print(f"\nAccount {acc} pending review groups ({len(rows)}):")
            for m, g in rows:
                print(f"  member_id={m.id}, grp_id={g.id}, title={g.title}, review_next_at={m.review_next_at}, perm={m.permission_reason}")

if __name__ == "__main__":
    asyncio.run(main())
