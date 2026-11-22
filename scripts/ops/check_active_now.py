import asyncio
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group
from sqlalchemy import select

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc in [33, 35]:
            mems = (await session.execute(
                select(Member, Group.title, Group.username)
                .join(Group, Group.id == Member.group_id)
                .where(Member.account_id == acc, Member.status == "joined", Member.review_status == "approved", Member.ad_status == "active")
            )).all()
            print(f"=== ACCOUNT {acc} ACTIVE APPROVED GROUPS ({len(mems)}) ===")
            for m, title, uname in mems:
                print(f"Mem {m.id}: grp={m.group_id} ({title} @{uname}), next_ad={m.next_ad_allowed_at}, tg_id={m.telegram_group_id}")

if __name__ == "__main__":
    asyncio.run(check())
