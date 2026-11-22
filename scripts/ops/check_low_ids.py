import asyncio
from datetime import datetime
from sqlalchemy import select
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group

async def check_lower():
    await db.init_db()
    async with db.async_session_factory() as session:
        # Check Acc 33
        rows_33 = (await session.execute(
            select(Member, Group)
            .join(Group, Group.id == Member.group_id)
            .where(
                Member.account_id == 33,
                Member.status == "joined",
                Member.review_status == "approved",
                Member.ad_status == "active",
                Member.id < 717
            )
            .order_by(Member.id)
        )).all()
        print(f"Account 33 has {len(rows_33)} approved active groups with id < 717:")
        for m, g in rows_33:
            print(f"  Mem {m.id}: grp={g.id} ({g.title})")

        # Check Acc 35
        rows_35 = (await session.execute(
            select(Member, Group)
            .join(Group, Group.id == Member.group_id)
            .where(
                Member.account_id == 35,
                Member.status == "joined",
                Member.review_status == "approved",
                Member.ad_status == "active",
                Member.id < 719
            )
            .order_by(Member.id)
        )).all()
        print(f"\nAccount 35 has {len(rows_35)} approved active groups with id < 719:")
        for m, g in rows_35:
            print(f"  Mem {m.id}: grp={g.id} ({g.title})")

if __name__ == "__main__":
    asyncio.run(check_lower())
