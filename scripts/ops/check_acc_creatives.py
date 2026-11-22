import asyncio
from datetime import datetime
from sqlalchemy import select, or_
from app.core import database as db
from app.core.group.models import GroupAccountMembership as Member, Group
from app.modules.acquisition.models import AdCampaign, AccountAdBinding, AdCreative
from app.modules.acquisition.simple_growth import campaign_matches, URL

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        now = datetime.utcnow()
        for acc in [33, 35]:
            print(f"=== ACCOUNT {acc} CAMPAIGN & CREATIVE CHECK ===")
            bindings = (await session.execute(
                select(AccountAdBinding, AdCampaign)
                .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
                .where(AccountAdBinding.account_id == acc, AccountAdBinding.enabled.is_(True))
            )).all()
            print(f"Bindings found: {len(bindings)}")
            for b, c in bindings:
                print(f"  Binding {b.id}: campaign={c.id} ('{c.name}'), enabled={c.enabled}, policy={c.delivery_policy}, target_levels={c.target_group_levels}, target_ids={c.target_group_ids}")
                
                # Check creatives for this binding
                cq = select(AdCreative).where(AdCreative.enabled.is_(True))
                allowed = select(AccountAdBinding.id).where(
                    AccountAdBinding.account_id == acc,
                    AccountAdBinding.ad_campaign_id == c.id,
                    AccountAdBinding.enabled.is_(True),
                    or_(AccountAdBinding.creative_id.is_(None), AccountAdBinding.creative_id == AdCreative.id)
                )
                cq = cq.where(allowed.exists())
                all_creatives = (await session.scalars(cq)).all()
                valid_creatives = [
                    cr for cr in all_creatives
                    if cr.content.strip() and not cr.media_url and not cr.link_url
                    and not URL.search(cr.content) and cr.creative_type == "text"
                ]
                print(f"  Creatives: total={len(all_creatives)}, valid_for_simple_growth={len(valid_creatives)}")
                for cr in all_creatives:
                    has_url = bool(URL.search(cr.content))
                    print(f"    Creative {cr.id}: type={cr.creative_type}, media={bool(cr.media_url)}, link={bool(cr.link_url)}, has_url_in_text={has_url}")

if __name__ == "__main__":
    asyncio.run(check())
