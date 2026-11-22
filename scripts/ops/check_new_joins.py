import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        rows = (await session.execute(
            select(Member, Group)
            .join(Group, Group.id == Member.group_id)
            .where(Member.id.in_([1188, 1187, 1186, 1185, 1184, 1180]))
            .order_by(Member.id.desc())
        )).all()
        print("\nDetails of Members 1188-1180:")
        for m, g in rows:
            print(f"  mem={m.id}, acc={m.account_id}, grp={m.group_id}")
            print(f"    title={g.title}")
            print(f"    status={m.status}, rev={m.review_status}, ad={m.ad_status}")
            print(f"    can_send_text={m.can_send_text}, reason={m.permission_reason}")
            print(f"    leave_error={m.leave_error}, left_at={m.left_at}")

if __name__ == "__main__":
    asyncio.run(main())
