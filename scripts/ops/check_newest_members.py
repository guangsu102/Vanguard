import asyncio
import os
import sys
from datetime import datetime

sys.path.append("/app")

from app.core import database as db
from app.core.redis import get_redis, init_redis
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AdCampaign, AccountAdBinding, AdCreative
from app.core.account.session_exec import forward
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        # Check Account 33 newest approved active member
        for acc in [33, 35]:
            q = (
                select(Member, Group)
                .join(Group, Group.id == Member.group_id)
                .where(
                    Member.account_id == acc,
                    Member.status == "joined",
                    Member.review_status == "approved",
                    Member.ad_status == "active",
                    Member.can_send_text.is_(True),
                )
                .order_by(Member.id.desc())
            )
            rows = (await session.execute(q)).all()
            print(f"\nAccount {acc} approved active members (newest first):")
            for m, g in rows[:5]:
                print(f"  member_id={m.id}, group_id={g.id}, tg_group_id={m.telegram_group_id}, title={g.title}")

if __name__ == "__main__":
    asyncio.run(main())
