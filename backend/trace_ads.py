import asyncio
from datetime import datetime, timedelta
from app.core.database import get_db, init_db
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.growth_policy import URL
from app.modules.acquisition.simple_growth import GrowthService, campaign_matches
from sqlalchemy import select, or_, exists
from sqlalchemy.orm import aliased
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AccountAdBinding, AdCampaign, AdCreative
from app.modules.acquisition.adaptive_frequency import canonical

async def run():
    await init_db()
    async for db in get_db():
        service = AcquisitionAutomationService(db)
        growth = GrowthService(service)
        
        for acc in [33, 35]:
            print(f'=== TESTING ADS FOR ACCOUNT {acc} ===')
            now = datetime.utcnow()
            other_member = aliased(Member)
            rows = (await db.execute(select(Member, Group, AdCampaign, AccountAdBinding)
                .join(Group, Group.id == Member.group_id)
                .join(AccountAdBinding, AccountAdBinding.account_id == Member.account_id)
                .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
                .where(Member.account_id == acc, Member.status == 'joined', Member.left_at.is_(None),
                    Member.review_status == 'approved', Member.ad_status == 'active',
                    Member.can_send_text.is_(True), Member.verification_status == 'complete',
                    Member.next_ad_allowed_at <= now, Member.leave_requested_at.is_(None),
                    ~exists().where(other_member.group_id == Member.group_id,
                                    other_member.next_ad_allowed_at > now),
                    or_(Member.ad_pause_until.is_(None), Member.ad_pause_until <= now),
                    AccountAdBinding.enabled.is_(True), AdCampaign.enabled.is_(True),
                    AdCampaign.delivery_policy != 'ad_only',
                    or_(AdCampaign.start_at.is_(None), AdCampaign.start_at <= now),
                    or_(AdCampaign.end_at.is_(None), AdCampaign.end_at > now))
                .order_by(Member.next_ad_allowed_at, Member.id, AccountAdBinding.priority.desc()).limit(100))).all()
            print(f'Total candidates: {len(rows)}')
            
            for member, group, campaign, binding in rows[:5]:
                print(f'  Candidate: member={member.id}, grp={group.id} ({group.group_id}), next_ad={member.next_ad_allowed_at}, checked_at={member.permission_checked_at}')
                cm = campaign_matches(campaign, group, now)
                prot = await growth.protected(member, group)
                print(f'    campaign_matches={cm}, protected={prot}, group.ad_delivery_account_id={group.ad_delivery_account_id}')
                
                creative_query = select(AdCreative).where(AdCreative.enabled.is_(True))
                allowed_binding = select(AccountAdBinding.id).where(
                    AccountAdBinding.account_id == acc,
                    AccountAdBinding.ad_campaign_id == campaign.id,
                    AccountAdBinding.enabled.is_(True),
                    or_(AccountAdBinding.creative_id.is_(None), AccountAdBinding.creative_id == AdCreative.id))
                creative_query = creative_query.where(allowed_binding.exists())
                creatives = list((await db.scalars(creative_query)).all())
                creatives = [c for c in creatives if c.content.strip() and not c.media_url and not c.link_url
                             and not URL.search(c.content) and c.creative_type == 'text']
                print(f'    available creatives count: {len(creatives)}')
                
                if member.permission_checked_at is None or member.permission_checked_at < now - timedelta(days=1):
                    print(f'    STALE PERMISSION: permission_checked_at is {member.permission_checked_at}')
        break

if __name__ == '__main__':
    asyncio.run(run())
