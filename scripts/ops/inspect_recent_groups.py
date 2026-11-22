import asyncio
from datetime import datetime
from sqlalchemy import select, func, desc
from app.core import database as db
from app.modules.acquisition.models import AdDeliveryLog
from app.core.group.models import GroupAccountMembership, Group

async def inspect():
    await db.init_db()
    async with db.async_session_factory() as session:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        print("=== RECENT AD LOGS TODAY FOR ACC 33 ===")
        logs33 = (await session.execute(
            select(AdDeliveryLog, Group.title, Group.username)
            .outerjoin(Group, Group.id == AdDeliveryLog.group_id)
            .where(AdDeliveryLog.account_id == 33, AdDeliveryLog.created_at >= today_start)
            .order_by(desc(AdDeliveryLog.created_at))
            .limit(10)
        )).all()
        for l, title, username in logs33:
            print(f"Log {l.id} @ {l.created_at}: grp={l.group_id} ({title} @{username}), status={l.status}, err={l.error}")
            
        print("\n=== RECENT AD LOGS TODAY FOR ACC 35 ===")
        logs35 = (await session.execute(
            select(AdDeliveryLog, Group.title, Group.username)
            .outerjoin(Group, Group.id == AdDeliveryLog.group_id)
            .where(AdDeliveryLog.account_id == 35, AdDeliveryLog.created_at >= today_start)
            .order_by(desc(AdDeliveryLog.created_at))
            .limit(10)
        )).all()
        for l, title, username in logs35:
            print(f"Log {l.id} @ {l.created_at}: grp={l.group_id} ({title} @{username}), status={l.status}, err={l.error}")

        print("\n=== CURRENT JOINED GROUPS FOR ACC 33 & 35 ===")
        joined = (await session.execute(
            select(GroupAccountMembership, Group.title, Group.username)
            .outerjoin(Group, Group.id == GroupAccountMembership.group_id)
            .where(GroupAccountMembership.account_id.in_([33, 35]), GroupAccountMembership.status == "joined")
        )).all()
        for m, title, username in joined:
            print(f"Mem {m.id}: acc={m.account_id}, grp={m.group_id} ({title} @{username}), status={m.status}, review={m.review_status}, ad={m.ad_status}, can_send_text={m.can_send_text}, reason={m.permission_reason}, v_status={m.verification_status}, next_ad={m.next_ad_allowed_at}")

        print("\n=== AUTO-JOIN RECENT JOINS FOR ACC 33 & 35 ===")
        recent_joins = (await session.execute(
            select(GroupAccountMembership, Group.title, Group.username)
            .outerjoin(Group, Group.id == GroupAccountMembership.group_id)
            .where(GroupAccountMembership.account_id.in_([33, 35]))
            .order_by(desc(GroupAccountMembership.id))
            .limit(15)
        )).all()
        for m, title, username in recent_joins:
            print(f"Mem {m.id}: acc={m.account_id}, grp={m.group_id} ({title} @{username}), status={m.status}, review={m.review_status}, ad={m.ad_status}, reason={m.permission_reason}, v_status={m.verification_status}, joined_at={m.joined_at}")

if __name__ == "__main__":
    asyncio.run(inspect())
