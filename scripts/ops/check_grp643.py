import asyncio
from sqlalchemy import select
from app.core.database import init_db, get_db
from app.core.group.models import Group, GroupAccountMembership

async def check():
    await init_db()
    async for db in get_db():
        for grp_id, acc_id in [(643, 33), (549, 35)]:
            g = await db.get(Group, grp_id)
            stmt = select(GroupAccountMembership).where(
                GroupAccountMembership.account_id == acc_id,
                GroupAccountMembership.group_id == grp_id
            )
            m = await db.scalar(stmt)
            print(f"Group {grp_id}: title='{g.title}', username={g.username}")
            if m:
                print(f"  Mem for {acc_id}: status={m.status}, review={m.review_status}, ad_status={m.ad_status}, reason={m.permission_reason}, left_at={m.left_at}")
        break

asyncio.run(check())
