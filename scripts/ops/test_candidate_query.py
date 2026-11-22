import asyncio
from datetime import datetime
from sqlalchemy import select, func
from app.core import database as db
from app.core.group.models import Group
from app.modules.acquisition.join_claims import group_occupied

async def test():
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        # Query 1: current simple_growth query
        q1 = select(func.count(Group.id)).where(
            Group.status.in_(["pending_join", "cooling_down"]),
            Group.username.is_not(None),
            ~group_occupied(now)
        )
        c1 = await session.scalar(q1)
        print(f"Current simple_growth candidate count (pending_join, cooling_down): {c1}")

        # Query 2: with pending added
        q2 = select(func.count(Group.id)).where(
            Group.status.in_(["pending", "pending_join", "cooling_down"]),
            Group.username.is_not(None),
            ~group_occupied(now)
        )
        c2 = await session.scalar(q2)
        print(f"With 'pending' added: {c2}")

if __name__ == "__main__":
    asyncio.run(test())
