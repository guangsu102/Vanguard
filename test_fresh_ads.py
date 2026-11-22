import asyncio
from sqlalchemy import select, text
from app.core.database import init_db, get_db_session, close_db
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.simple_growth import GrowthService

async def check_account(acc_id: int):
    async with get_db_session() as db:
        service = AcquisitionAutomationService(db)
        growth = GrowthService(service)
        try:
            res_ads = await growth.ads(acc_id)
            print(f"Account {acc_id} ads() result: {res_ads}")
        except Exception as e:
            import traceback
            print(f"Account {acc_id} ads() raised {type(e).__name__}: {e}")
            traceback.print_exc()

async def main():
    await init_db(create_tables=False)
    for acc_id in [2, 33, 34, 35]:
        await check_account(acc_id)
    await close_db()

if __name__ == '__main__':
    asyncio.run(main())
