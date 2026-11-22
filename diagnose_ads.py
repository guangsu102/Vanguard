import asyncio
from datetime import UTC, datetime
from sqlalchemy import select, text
from app.core.database import init_db, get_db_session, close_db
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.simple_growth import GrowthService

async def main():
    await init_db(create_tables=False)
    async with get_db_session() as db:
        service = AcquisitionAutomationService(db)
        growth = GrowthService(service)
        
        print("=== CONFIG CHECK ===")
        res = await db.execute(text("""
            SELECT account_id, auto_ads_enabled, quiet_hours_start, quiet_hours_end,
                   max_ads_per_day
            FROM telegram_account_operation_config
            WHERE account_id IN (2, 33, 34, 35)
            ORDER BY account_id
        """))
        for r in res.fetchall():
            print(dict(r._mapping))

        print("\n=== DRY RUN / REASON FOR ADS ===")
        for acc_id in [2, 33, 34, 35]:
            try:
                res_ads = await growth.ads(acc_id)
                print(f"Account {acc_id} ads() result: {res_ads}")
            except Exception as e:
                print(f"Account {acc_id} ads() raised: {e}")

        print("\n=== ELIGIBLE MEMBERSHIP COUNTS ===")
        now = datetime.utcnow()
        for acc_id in [2, 33, 34, 35]:
            q = text("""
                SELECT 
                    COUNT(*) as total_joined,
                    COUNT(CASE WHEN review_status = 'approved' THEN 1 END) as approved_review,
                    COUNT(CASE WHEN ad_status = 'active' THEN 1 END) as active_ad_status,
                    COUNT(CASE WHEN can_send_text = true THEN 1 END) as can_send,
                    COUNT(CASE WHEN verification_status = 'complete' THEN 1 END) as verif_complete,
                    COUNT(CASE WHEN next_ad_allowed_at <= :now THEN 1 END) as next_ad_due,
                    COUNT(CASE WHEN 
                        review_status = 'approved' AND ad_status = 'active' 
                        AND can_send_text = true AND verification_status = 'complete'
                        AND (next_ad_allowed_at IS NULL OR next_ad_allowed_at <= :now)
                    THEN 1 END) as fully_eligible
                FROM group_account_membership
                WHERE account_id = :acc_id AND status = 'joined' AND left_at IS NULL
            """)
            r = await db.execute(q, {"acc_id": acc_id, "now": now})
            print(f"Account {acc_id}:", dict(r.fetchone()._mapping))

    await close_db()

if __name__ == '__main__':
    asyncio.run(main())
