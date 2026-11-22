import asyncio
from datetime import datetime
from sqlalchemy import select, func, desc
from app.core import database as db
from app.modules.acquisition.models import AdDeliveryLog
from app.core.group.models import GroupAccountMembership

async def inspect():
    await db.init_db()
    async with db.async_session_factory() as session:
        print("=== RECENT AD LOGS FOR ACC 33 & 35 ===")
        logs = (await session.execute(
            select(AdDeliveryLog)
            .where(AdDeliveryLog.account_id.in_([33, 35]))
            .order_by(desc(AdDeliveryLog.created_at))
            .limit(10)
        )).scalars().all()
        for l in logs:
            print(f"Log {l.id}: acc={l.account_id}, grp={l.group_id}, tg_grp={l.telegram_group_id}, status={l.status}, msg_id={l.telegram_message_id}, error={l.error}, created_at={l.created_at}")

        print("\n=== TODAY AD STATS BY ACCOUNT ===")
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        stats = (await session.execute(
            select(
                AdDeliveryLog.account_id,
                func.count(AdDeliveryLog.id).filter(AdDeliveryLog.telegram_message_id.isnot(None)).label("success"),
                func.count(AdDeliveryLog.id).filter(AdDeliveryLog.telegram_message_id.is_(None)).label("failed"),
                func.count(AdDeliveryLog.id).label("total")
            )
            .where(AdDeliveryLog.created_at >= today_start)
            .group_by(AdDeliveryLog.account_id)
            .order_by(AdDeliveryLog.account_id)
        )).all()
        for r in stats:
            print(f"Account {r.account_id}: success={r.success}, failed={r.failed}, total={r.total}")

        print("\n=== ACC 33 MEMBERSHIP COUNTS ===")
        m_stats_33 = (await session.execute(
            select(GroupAccountMembership.review_status, GroupAccountMembership.ad_status, GroupAccountMembership.status, func.count())
            .where(GroupAccountMembership.account_id == 33)
            .group_by(GroupAccountMembership.review_status, GroupAccountMembership.ad_status, GroupAccountMembership.status)
        )).all()
        for r in m_stats_33:
            print(f"review={r[0]}, ad={r[1]}, status={r[2]}: count={r[3]}")

        print("\n=== ACC 35 MEMBERSHIP COUNTS ===")
        m_stats_35 = (await session.execute(
            select(GroupAccountMembership.review_status, GroupAccountMembership.ad_status, GroupAccountMembership.status, func.count())
            .where(GroupAccountMembership.account_id == 35)
            .group_by(GroupAccountMembership.review_status, GroupAccountMembership.ad_status, GroupAccountMembership.status)
        )).all()
        for r in m_stats_35:
            print(f"review={r[0]}, ad={r[1]}, status={r[2]}: count={r[3]}")

if __name__ == "__main__":
    asyncio.run(inspect())
