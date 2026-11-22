import asyncio
from datetime import datetime
from app.core.database import get_db, init_db
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.growth_policy import URL
from app.modules.acquisition.simple_growth import GrowthService, result, campaign_matches
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
        
        now = datetime.utcnow()
        other_member = aliased(Member)
        rows = (await db.execute(select(Member, Group, AdCampaign, AccountAdBinding)
            .join(Group, Group.id == Member.group_id)
            .join(AccountAdBinding, AccountAdBinding.account_id == Member.account_id)
            .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
            .where(Member.account_id == 33, Member.status == 'joined', Member.left_at.is_(None),
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
            .order_by(Member.next_ad_allowed_at, Member.id, AccountAdBinding.priority.desc()).limit(10))).all()
        
        print(f'Checking first candidate for Account 33:')
        member, group, campaign, binding = rows[0]
        key = canonical(member.telegram_group_id)
        print(f'Candidate 0: member={member.id}, grp={group.id}, key={key}')
        
        # Check why it did or didn't send
        # Let's inspect final gate conditions in _send
        from app.core.automation_settings import get_ad_delivery_execution_settings
        exec_settings = await get_ad_delivery_execution_settings(db)
        print(f'exec_settings enabled: {exec_settings["enabled"]}')
        print(f'member.status: {member.status}, review_status: {member.review_status}, ad_status: {member.ad_status}, verification: {member.verification_status}')
        print(f'member.can_send_text: {member.can_send_text}, leave_requested: {member.leave_requested_at}')
        print(f'member.permission_checked_at: {member.permission_checked_at}')
        
        from app.core.group.identity import telegram_group_id_aliases
        shared_due = await db.scalar(select(func.max(Member.next_ad_allowed_at)).where(
            Member.telegram_group_id.in_(telegram_group_id_aliases(key))))
        print(f'shared_due: {shared_due}, now: {now}, shared_due > now: {shared_due and shared_due > now}')
        break

if __name__ == '__main__':
    from sqlalchemy import func
    asyncio.run(run())
