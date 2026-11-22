import asyncio
from datetime import datetime
from sqlalchemy import select, update
from app.core import database as db
from app.modules.acquisition.models import AdDeliveryLog
from app.core.group.models import GroupAccountMembership

async def clean():
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        # Find all groups where AdDeliveryLog recorded UserBannedInChannelError or ChannelPrivateError
        bad_logs = (await session.execute(
            select(AdDeliveryLog.account_id, AdDeliveryLog.group_id, AdDeliveryLog.error)
            .where(AdDeliveryLog.error.in_(["UserBannedInChannelError", "ChannelPrivateError", "ChatAdminRequiredError"]))
            .distinct()
        )).all()
        print(f"Found {len(bad_logs)} distinct banned account-group combinations in ad logs.")
        
        updated_count = 0
        for acc_id, grp_id, err in bad_logs:
            # Check membership
            mem = (await session.execute(
                select(GroupAccountMembership)
                .where(GroupAccountMembership.account_id == acc_id, GroupAccountMembership.group_id == grp_id)
            )).scalar_one_or_none()
            if mem:
                if mem.status != "left" or mem.review_status != "rejected" or mem.ad_status != "blocked":
                    print(f"Pruning banned membership {mem.id}: acc={acc_id}, grp={grp_id}, old_status=({mem.status}, {mem.review_status}, {mem.ad_status}) -> left/rejected/blocked")
                    mem.status = "left"
                    mem.left_at = now
                    mem.review_status = "rejected"
                    mem.ad_status = "blocked"
                    mem.permission_reason = err
                    mem.leave_requested_at = now
                    mem.leave_error = err
                    updated_count += 1
        await session.commit()
        print(f"Total memberships pruned: {updated_count}")

if __name__ == "__main__":
    asyncio.run(clean())
