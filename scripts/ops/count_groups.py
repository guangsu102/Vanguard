import asyncio
from app.core import database as db
from app.core.group.models import Group
from sqlalchemy import select, func

async def main():
    await db.init_db()
    async with db.async_session_factory() as s:
        print("Total groups:", await s.scalar(select(func.count(Group.id))))
        res = (await s.execute(select(Group.status, func.count()).group_by(Group.status))).all()
        print("Groups by status:", res)

if __name__ == "__main__":
    asyncio.run(main())
