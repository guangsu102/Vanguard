import asyncio
import os
import sys
from datetime import datetime
from sqlalchemy import select, or_, exists
from sqlalchemy.orm import aliased, lazyload

sys.path.append("/app")

from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AdCampaign, AccountAdBinding, AdCreative
from app.modules.acquisition.simple_growth import campaign_matches

async def test_acc(session, account_id):
    now = datetime.utcnow()
    other_member = aliased(Member)
    rows = (await session.execute(select(Member, Group, AdCampaign, AccountAdBinding)
        .join(Group, Group.id == Member.group_id)
        .join(AccountAdBinding, AccountAdBinding.account_id == Member.account_id)
        .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
        .options(lazyload("*"))
        .where(Member.account_id == account_id, Member.status == "joined", Member.left_at.is_(None),
            Member.review_status == "approved", Member.ad_status == "active",
            Member.can_send_text.is_(True), Member.verification_status == "complete",
            Member.next_ad_allowed_at <= now, Member.leave_requested_at.is_(None),
            ~exists().where(other_member.group_id == Member.group_id,
                            other_member.next_ad_allowed_at > now),
            or_(Member.ad_pause_until.is_(None), Member.ad_pause_until <= now),
            AccountAdBinding.enabled.is_(True), AdCampaign.enabled.is_(True),
            AdCampaign.delivery_policy != "ad_only",
            or_(AdCampaign.start_at.is_(None), AdCampaign.start_at <= now),
            or_(AdCampaign.end_at.is_(None), AdCampaign.end_at > now))
        .order_by(Member.next_ad_allowed_at, Member.id, AccountAdBinding.priority.desc()).limit(100))).all()
    
    print(f"\nAccount {account_id}: found {len(rows)} matching rows in ads() query")
    for idx, (m, g, c, b) in enumerate(rows[:15]):
        matches = campaign_matches(c, g, now)
        print(f"  [{idx}] mem_id={m.id}, grp_id={g.id}, tg_id={g.group_id}, title={g.title[:20] if g.title else ''}, matches={matches}")

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        await test_acc(session, 33)
        await test_acc(session, 35)

if __name__ == "__main__":
    asyncio.run(main())
