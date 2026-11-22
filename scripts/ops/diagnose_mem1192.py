import asyncio
from datetime import datetime
from sqlalchemy import select, or_, exists
from sqlalchemy.orm import aliased
from app.core import database as db
from app.core.group.models import Group, GroupAccountMembership as Member
from app.modules.acquisition.models import AdCampaign, AccountAdBinding

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        m = await session.get(Member, 1192)
        g = await session.get(Group, m.group_id)
        print(f"Mem 1192: acc={m.account_id}, grp={m.group_id}, status={m.status}, left_at={m.left_at}")
        print(f"  review={m.review_status}, ad_status={m.ad_status}, can_send_text={m.can_send_text}")
        print(f"  v_status={m.verification_status}, next_ad={m.next_ad_allowed_at} (<= now? {m.next_ad_allowed_at <= now if m.next_ad_allowed_at else False})")
        print(f"  leave_requested={m.leave_requested_at}, ad_pause={m.ad_pause_until}")
        
        # Check other member in group 433
        other_mems = (await session.execute(
            select(Member)
            .where(Member.group_id == m.group_id, Member.id != m.id)
        )).scalars().all()
        print(f"  Other members in group {m.group_id}: {len(other_mems)}")
        for om in other_mems:
            print(f"    Other mem {om.id}: acc={om.account_id}, next_ad={om.next_ad_allowed_at} (> now? {om.next_ad_allowed_at > now if om.next_ad_allowed_at else False})")

        # Check bindings
        binding = await session.scalar(
            select(AccountAdBinding).where(AccountAdBinding.account_id == m.account_id, AccountAdBinding.enabled.is_(True))
        )
        print(f"  Binding for acc {m.account_id}: {binding.id if binding else None}")

if __name__ == "__main__":
    asyncio.run(check())
