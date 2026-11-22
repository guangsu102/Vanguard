import asyncio
from datetime import datetime
from sqlalchemy import select
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        mems = (await session.execute(
            select(Member, Group.title, Group.username)
            .join(Group, Group.id == Member.group_id)
            .where(
                Member.status == "joined",
                Member.can_send_text.is_(True),
                Member.review_status != "approved"
            )
        )).all()
        print(f"Found {len(mems)} joined memberships with can_send_text=True but review_status != approved:")
        for m, title, uname in mems:
            print(f"Mem {m.id}: acc={m.account_id}, grp={m.group_id} ({title} @{uname}), status={m.status}, review={m.review_status}, ad={m.ad_status}, v_status={m.verification_status}, reason={m.permission_reason}")

if __name__ == "__main__":
    asyncio.run(check())
