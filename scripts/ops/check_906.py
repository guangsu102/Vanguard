import asyncio
from sqlalchemy import select
from app.core import database as db
from app.modules.acquisition.models import AdDeliveryLog
from app.core.group.models import GroupAccountMembership

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        # Check grp 843 member
        m = (await session.execute(
            select(GroupAccountMembership).where(GroupAccountMembership.group_id == 843, GroupAccountMembership.account_id == 33)
        )).scalar_one_or_none()
        if m:
            print(f"Group 843 Member: id={m.id}, status={m.status}, review={m.review_status}, ad={m.ad_status}, err={m.leave_error}")

        # Check grp 906 in AdDeliveryLog
        logs_906 = (await session.execute(
            select(AdDeliveryLog).where(AdDeliveryLog.group_id == 906).order_by(AdDeliveryLog.id.desc()).limit(5)
        )).scalars().all()
        print(f"Group 906 Logs ({len(logs_906)}):")
        for l in logs_906:
            print(f"  Log {l.id}: acc={l.account_id}, status={l.status}, error={l.error}, sent_at={l.sent_at}, created_at={l.created_at}")

        # Check grp 913 in AdDeliveryLog (for Acc 35)
        logs_913 = (await session.execute(
            select(AdDeliveryLog).where(AdDeliveryLog.group_id == 913).order_by(AdDeliveryLog.id.desc()).limit(5)
        )).scalars().all()
        print(f"Group 913 Logs ({len(logs_913)}):")
        for l in logs_913:
            print(f"  Log {l.id}: acc={l.account_id}, status={l.status}, error={l.error}, sent_at={l.sent_at}, created_at={l.created_at}")

if __name__ == "__main__":
    asyncio.run(check())
