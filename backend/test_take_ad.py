import asyncio
from datetime import datetime
from app.core.redis import get_redis, init_redis
from app.modules.acquisition.simple_limits import take_ad, Deferred

async def run():
    await init_redis()
    redis = await get_redis()
    # Test take_ad for account 33 with a dummy group key 999999999
    try:
        await take_ad(redis, 33, 999999999, cap=100, account_interval=600, group_interval=10800, total_cap=132, durable_used=0, durable_ads=18)
        print('take_ad succeeded for account 33!')
    except Deferred as d:
        print(f'take_ad Deferred: reason={d.reason}, retry_after={d.retry_after_seconds}')

if __name__ == '__main__':
    asyncio.run(run())
