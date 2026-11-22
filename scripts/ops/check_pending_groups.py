import asyncio
from app.core import database as db
from app.core.group.models import Group
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as s:
        groups = (await s.execute(select(Group).where(Group.status == "pending").limit(10))).scalars().all()
        for g in groups:
            print(f"Group {g.id}: title='{g.title}', username={g.username}, group_id={g.group_id}, status={g.status}, created_at={g.created_at}")

if __name__ == "__main__":
    asyncio.run(main())
