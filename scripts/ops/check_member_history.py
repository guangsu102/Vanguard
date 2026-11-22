import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc in [33, 35]:
            rows = (await session.execute(
                select(Member.id, Member.group_id, Member.status, Member.review_status, Member.permission_reason, Member.permission_checked_at, Member.joined_at)
                .where(Member.account_id == acc)
                .order_by(Member.id.desc())
                .limit(10)
            )).all()
            print(f"\nAccount {acc} latest 10 members:")
            for r in rows:
                print(f"  id={r.id}, grp={r.group_id}, status={r.status}, review={r.review_status}, reason={r.permission_reason}, checked={r.permission_checked_at}, joined={r.joined_at}")

if __name__ == "__main__":
    asyncio.run(main())
