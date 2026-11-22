import asyncio
from datetime import datetime
from sqlalchemy import select, or_, exists
from sqlalchemy.orm import aliased
from app.core import database as db
from app.core.group.models import Group, GroupAccountMembership as Member
from app.modules.acquisition.models import AdCampaign, AccountAdBinding

async def check_candidates(account_id: int):
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        other_member = aliased(Member)
        rows = (await session.execute(
            select(Member, Group, AdCampaign)
            .join(Group, Group.id == Member.group_id)
            .join(AccountAdBinding, AccountAdBinding.account_id == Member.account_id)
            .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
            .where(
                Member.account_id == account_id,
                Member.status == "joined",
                Member.left_at.is_(None),
                Member.review_status == "approved",
                Member.ad_status == "active",
                Member.can_send_text.is_(True),
                Member.verification_status == "complete",
                Member.next_ad_allowed_at <= now,
                Member.leave_requested_at.is_(None),
                ~exists().where(other_member.group_id == Member.group_id, other_member.next_ad_allowed_at > now),
                or_(Member.ad_pause_until.is_(None), Member.ad_pause_until <= now),
                AccountAdBinding.enabled.is_(True),
                AdCampaign.enabled.is_(True),
                AdCampaign.delivery_policy != "ad_only",
                or_(AdCampaign.start_at.is_(None), AdCampaign.start_at <= now),
                or_(AdCampaign.end_at.is_(None), AdCampaign.end_at > now)
            )
            .order_by(Member.next_ad_allowed_at, Member.id)
            .limit(20)
        )).all()
        print(f"=== CANDIDATES FOR ACCOUNT {account_id} (found {len(rows)}) ===")
        for m, g, c in rows:
            print(f"Member {m.id}: grp={g.id} ({g.title}), tg_grp={m.telegram_group_id}, next_ad={m.next_ad_allowed_at}")

async def main():
    await check_candidates(33)
    print()
    await check_candidates(35)

if __name__ == "__main__":
    asyncio.run(main())
