import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select, update
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member

async def unblock():
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        # 1. Update memberships that have can_send_text == True and are stuck in verification_pending
        q_writable = (
            select(Member)
            .where(
                Member.status == "joined",
                Member.can_send_text.is_(True),
                Member.review_status != "approved"
            )
        )
        writable_mems = (await session.execute(q_writable)).scalars().all()
        print(f"Found {len(writable_mems)} writable stuck memberships to approve:")
        for m in writable_mems:
            print(f"  Approving Mem {m.id}: acc={m.account_id}, grp={m.group_id}")
            m.verification_status = "complete"
            m.review_status = "approved"
            m.ad_status = "active"
            m.permission_reason = "text_allowed"
            m.warmup_status = "ad_eligible"
            m.first_ad_allowed_at = now
            m.next_ad_allowed_at = now
            m.review_next_at = now + timedelta(days=1)

        # 2. Reset review_next_at for any joined memberships that need review
        q_pending_review = (
            select(Member)
            .where(
                Member.status == "joined",
                Member.review_status == "pending"
            )
        )
        pending_mems = (await session.execute(q_pending_review)).scalars().all()
        print(f"Found {len(pending_mems)} pending review memberships to schedule immediately:")
        for m in pending_mems:
            print(f"  Scheduling review for Mem {m.id}: acc={m.account_id}, grp={m.group_id}")
            m.review_next_at = now

        await session.commit()
        print("Database commit successful.")

if __name__ == "__main__":
    asyncio.run(unblock())
