import asyncio
import time
from app.core.redis import init_redis, get_redis

async def run():
    await init_redis()
    redis = await get_redis()
    now = time.time()
    keys = await redis.keys('vanguard:growth:v2:group:*:due')
    print(f'Total group due keys: {len(keys)}')
    over_3h = []
    for k in keys:
        val = await redis.get(k)
        if val:
            due = float(val)
            rem = due - now
            if rem > 10800:
                over_3h.append((k, rem))
    print(f'Keys with remaining cooldown > 3 hours: {len(over_3h)}')
    for k, rem in over_3h[:10]:
        print(f'  {k}: {rem/3600:.1f} hours remaining')

if __name__ == '__main__':
    asyncio.run(run())
