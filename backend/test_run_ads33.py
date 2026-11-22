import asyncio
from app.core.database import get_db, init_db
from app.core.redis import init_redis
from app.core.account.pool import AccountPool
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.simple_growth import GrowthService

async def run():
    await init_db()
    await init_redis()
    async for db in get_db():
        pool = AccountPool(db)
        service = AcquisitionAutomationService(db, account_pool=pool)
        growth = GrowthService(service)
        print('Executing growth.execute("ads", 33)...')
        res = await growth.execute("ads", 33)
        print('Result for 33:', res)
        break

if __name__ == '__main__':
    asyncio.run(run())
