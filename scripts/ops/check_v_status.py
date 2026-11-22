import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member
from sqlalchemy import select, func

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc in [2, 33, 34, 35]:
            q = (
                select(Member.verification_status, func.count(Member.id))
                .where(Member.account_id == acc, Member.status == "joined")
                .group_by(Member.verification_status)
            )
            rows = (await session.execute(q)).all()
            print(f"Account {acc} joined members verification_status: {rows}")

if __name__ == "__main__":
    asyncio.run(main())
