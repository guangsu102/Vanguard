import asyncio
import os
import sys
from datetime import datetime

sys.path.append("/app")

from app.core import database as db
from app.core.redis import get_redis, init_redis
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AdDeliveryLog
from sqlalchemy import select

async def main():
    await init_redis()
    await db.init_db()
    redis = await get_redis()
    print("=== REDIS KEYS FOR SIMPLE GROWTH ===")
    now_ts = int(datetime.utcnow().timestamp())
    print(f"Current UTC timestamp: {now_ts}")
    for acc in [2, 33, 34, 35]:
        due = await redis.get(f"vanguard:growth:v2:ads:{acc}:due")
        cnt = await redis.hgetall(f"vanguard:growth:v2:ads:{acc}:count")
        due_diff = int(due) - now_ts if due else None
        print(f"Account {acc}: ads:due={due} (in {due_diff}s), ads:count={cnt}")

    async with db.async_session_factory() as session:
        for acc in [33, 35]:
            q = (
                select(Member.id, Member.group_id, Member.next_ad_allowed_at, Group.title, Group.group_id)
                .join(Group, Group.id == Member.group_id)
                .where(
                    Member.account_id == acc,
                    Member.status == "joined",
                    Member.review_status == "approved",
                    Member.ad_status == "active",
                )
                .order_by(Member.next_ad_allowed_at.asc())
            )
            res = (await session.execute(q)).all()
            print(f"\nAccount {acc} Active Candidates ({len(res)} total):")
            for r in res[:10]:
                print(f"  member_id={r.id}, group_id={r.group_id}, tg_group_id={r.group_id}, next_ad={r.next_ad_allowed_at}, title={r.title[:25] if r.title else ''}")

if __name__ == "__main__":
    asyncio.run(main())
