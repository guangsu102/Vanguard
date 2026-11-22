import asyncio
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        m = await session.get(Member, 1170)
        if m:
            print(f"Mem 1170: status={m.status}, review={m.review_status}, ad={m.ad_status}, can_send_text={m.can_send_text}, v_status={m.verification_status}, reason={m.permission_reason}")

if __name__ == "__main__":
    asyncio.run(check())
