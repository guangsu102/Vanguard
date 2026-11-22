import asyncio
import time
from app.core.redis import init_redis, get_redis

async def check():
    await init_redis()
    client = await get_redis()
    now = time.time()
    for s in ["ads", "join", "review_and_verify", "exit"]:
        k = f"vanguard:growth_dispatch:v2:{s}:running"
        items = await client.zrange(k, 0, -1, withscores=True)
        print(f"{s} running items (total {len(items)}):")
        for m, sc in items:
            print(f"  {m}: score={sc}, remaining={sc - now:.1f}s")

if __name__ == "__main__":
    asyncio.run(check())
