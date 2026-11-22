import asyncio
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        m = await session.get(Member, 1097)
        if m:
            print(f"Mem 1097: acc={m.account_id}, grp={m.group_id}, status={m.status}, review={m.review_status}, ad={m.ad_status}, v_status={m.verification_status}, can_send_text={m.can_send_text}, next_ad={m.next_ad_allowed_at}, left_at={m.left_at}")

if __name__ == "__main__":
    asyncio.run(check())
