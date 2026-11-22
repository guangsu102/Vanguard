import asyncio
import time
from app.core.redis import init_redis, get_redis

async def check_redis():
    await init_redis()
    client = await get_redis()
    prefix = "vanguard:growth_dispatch:v2:"
    now = int(time.time())
    print(f"Current timestamp: {now}")
    for stage in ["ads", "join", "review_and_verify", "exit"]:
        due_key = prefix + stage + ":due"
        items = await client.zrange(due_key, 0, -1, withscores=True)
        print(f"\nStage {stage} due items:")
        for member, score in items:
            diff = int(score) - now
            print(f"  Account {member}: score={int(score)} (due in {diff}s)")

if __name__ == "__main__":
    asyncio.run(check_redis())
