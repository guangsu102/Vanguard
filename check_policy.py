import asyncio
from app.core.database import init_db, get_db_session, close_db
from app.modules.acquisition.qualification_service import policy
from app.core.automation_settings import get_ad_delivery_execution_settings

async def main():
    await init_db(create_tables=False)
    async with get_db_session() as db:
        pol = await policy(db, fresh=True)
        print("=== QUALIFICATION POLICY ===")
        print(pol)

        ad_settings = await get_ad_delivery_execution_settings(db)
        print("\n=== AD DELIVERY EXECUTION SETTINGS ===")
        print(ad_settings)
    await close_db()

if __name__ == '__main__':
    asyncio.run(main())
