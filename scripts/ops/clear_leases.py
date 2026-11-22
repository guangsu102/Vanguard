import asyncio
from app.core.redis import init_redis, get_redis

async def clear_leases():
    await init_redis()
    client = await get_redis()
    for acc in [2, 33, 34, 35]:
        await client.delete(f"vanguard:telegram:lease:{acc}")
        await client.delete(f"vanguard:telegram:owner:{acc}")
        print(f"Cleared session lease & owner for account {acc}")

if __name__ == "__main__":
    asyncio.run(clear_leases())
