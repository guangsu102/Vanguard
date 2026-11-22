import asyncio
from app.core.redis import init_redis, get_redis

async def check():
    await init_redis()
    client = await get_redis()
    for acc in [2, 33, 34, 35]:
        c = await client.hgetall(f"vanguard:growth:v2:ads:{acc}:count")
        due = await client.get(f"vanguard:growth:v2:ads:{acc}:due")
        print(f"Account {acc}: count={c}, due={due}")

if __name__ == "__main__":
    asyncio.run(check())
