import asyncio
import os
import sys
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_

sys.path.append("/app")

from app.core import database as db
from app.core.redis import get_redis, init_redis
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt
from app.core.operating_time import operating_day_start

async def main():
    await init_redis()
    await db.init_db()
    redis = await get_redis()
    today_start = operating_day_start()

    print(f"=== VERIFICATION REPORT: {datetime.utcnow().isoformat()}Z ===")

    async with db.async_session_factory() as session:
        # 1. Ad Delivery Stats Today
        print("\n--- 1. TODAY'S AD DELIVERY STATS ---")
        for acc in [2, 33, 34, 35]:
            q_succ = select(func.count(AdDeliveryLog.id)).where(
                AdDeliveryLog.account_id == acc,
                AdDeliveryLog.created_at >= today_start,
                AdDeliveryLog.status == "success"
            )
            q_fail = select(func.count(AdDeliveryLog.id)).where(
                AdDeliveryLog.account_id == acc,
                AdDeliveryLog.created_at >= today_start,
                AdDeliveryLog.status != "success"
            )
            succ = await session.scalar(q_succ)
            fail = await session.scalar(q_fail)
            print(f"Account {acc}: success={succ}, failed/pruned={fail}")

        # 2. Latest 5 ad deliveries across the board
        print("\n--- 2. LATEST 5 AD DELIVERIES ---")
        q_latest = (
            select(AdDeliveryLog)
            .order_by(AdDeliveryLog.id.desc())
            .limit(5)
        )
        latest_logs = (await session.scalars(q_latest)).all()
        for log in latest_logs:
            print(f"Log {log.id}: acc={log.account_id}, grp={log.group_id}, status={log.status}, msg_id={log.telegram_message_id}, err={log.error}, time={log.created_at}")

        # 3. Membership breakdown by account
        print("\n--- 3. MEMBERSHIP BREAKDOWN ---")
        for acc in [2, 33, 34, 35]:
            q_mem = (
                select(Member.status, Member.review_status, Member.ad_status, func.count(Member.id))
                .where(Member.account_id == acc)
                .group_by(Member.status, Member.review_status, Member.ad_status)
            )
            rows = (await session.execute(q_mem)).all()
            print(f"Account {acc}:")
            for st, rev, ad, cnt in rows:
                print(f"  status={st}, review={rev}, ad={ad} -> count={cnt}")

        # 4. Recent AutoJoin Attempts (last 1 hour)
        print("\n--- 4. RECENT AUTO-JOIN ATTEMPTS (Last 1 hr) ---")
        one_hr_ago = datetime.utcnow() - timedelta(hours=1)
        q_join = (
            select(AutoJoinAttempt)
            .where(AutoJoinAttempt.attempted_at >= one_hr_ago)
            .order_by(AutoJoinAttempt.id.desc())
            .limit(10)
        )
        joins = (await session.scalars(q_join)).all()
        for j in joins:
            print(f"Join {j.id}: acc={j.account_id}, title={j.group_title[:25] if j.group_title else ''}, status={j.status}, err={j.error}")

    # 5. Redis Due and Limits
    print("\n--- 5. REDIS LIMITS & TIMERS ---")
    now_ts = int(datetime.utcnow().timestamp())
    for acc in [2, 33, 34, 35]:
        due = await redis.get(f"vanguard:growth:v2:ads:{acc}:due")
        cnt = await redis.hgetall(f"vanguard:growth:v2:ads:{acc}:count")
        due_diff = int(due) - now_ts if due else None
        print(f"Account {acc}: ads:due={due} (in {due_diff}s), ads:count={cnt}")

if __name__ == "__main__":
    asyncio.run(main())
