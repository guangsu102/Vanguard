import asyncio
import time
from datetime import datetime
from sqlalchemy import update
from app.core import database as db
from app.core.redis import init_redis, get_redis
from app.core.group.models import GroupAccountMembership as Member

async def force_due():
    await db.init_db()
    await init_redis()
    client = await get_redis()
    now_dt = datetime.utcnow()
    now_ts = int(time.time())
    
    # 1. Set review_next_at to now in DB
    async with db.async_session_factory() as session:
        await session.execute(
            update(Member)
            .where(
                Member.account_id.in_([33, 35]),
                Member.status == "joined",
                Member.ad_status != "paused"
            )
            .values(review_next_at=now_dt)
        )
        await session.commit()
    print(f"Updated memberships in DB: review_next_at set to {now_dt}")
    
    # 2. Wake Redis queue
    await client.zadd("vanguard:growth_dispatch:v2:review_and_verify:due", {"33": now_ts, "35": now_ts})
    print(f"Woke review_and_verify queue in Redis at {now_ts}")

if __name__ == "__main__":
    asyncio.run(force_due())
