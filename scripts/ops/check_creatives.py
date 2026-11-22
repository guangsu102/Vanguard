import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.modules.acquisition.models import AdCampaign, AccountAdBinding, AdCreative
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc in [2, 33, 34, 35]:
            q = (
                select(AccountAdBinding, AdCampaign, AdCreative)
                .join(AdCampaign, AdCampaign.id == AccountAdBinding.ad_campaign_id)
                .outerjoin(AdCreative, AdCreative.id == AccountAdBinding.creative_id)
                .where(AccountAdBinding.account_id == acc)
            )
            rows = (await session.execute(q)).all()
            print(f"\nAccount {acc} Bindings ({len(rows)}):")
            for b, c, cr in rows:
                print(f"  binding_id={b.id}, camp_id={c.id}, camp_name={c.name}, enabled={b.enabled}, creative_id={b.creative_id}")
                if cr:
                    print(f"    creative content: {repr(cr.content[:80])}")

        # Also let's check all enabled creatives
        all_creatives = (await session.scalars(select(AdCreative).where(AdCreative.enabled.is_(True)))).all()
        print(f"\nAll Enabled Creatives ({len(all_creatives)}):")
        for cr in all_creatives:
            print(f"  id={cr.id}, title={cr.title}, type={cr.creative_type}, len={len(cr.content)}")
            print(f"    preview: {repr(cr.content[:100])}")

if __name__ == "__main__":
    asyncio.run(main())
