import asyncio
import os
import sys

sys.path.append("/app")

from app.core.redis import get_redis, init_redis
from app.core.operating_time import operating_date

async def main():
    await init_redis()
    redis = await get_redis()
    date_str = f"{operating_date():%Y%m%d}"
    print(f"Operating date: {date_str}")
    for acc in [2, 33, 34, 35]:
        key = f"risk:account:{acc}:daily:outbound_message:{date_str}"
        val = await redis.get(key)
        ttl = await redis.ttl(key)
        print(f"Account {acc}: {key} = {val} (ttl={ttl})")

if __name__ == "__main__":
    asyncio.run(main())
