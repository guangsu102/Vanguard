import asyncio
import os
import sys
import time

sys.path.append("/app")

from app.core.redis import get_redis, init_redis
from app.core.scheduler.growth_dispatch import PREFIX, STAGES

async def main():
    await init_redis()
    redis = await get_redis()
    now = time.time()
    print(f"DISPATCH PREFIX: {PREFIX}")
    print(f"Current time: {now}")

    for stage in STAGES:
        due_key = PREFIX + stage + ":due"
        wake_key = PREFIX + stage + ":wake_due"
        out_key = PREFIX + stage + ":outstanding"
        run_key = PREFIX + "running:" + STAGES[stage].queue
        cursor_key = PREFIX + stage + ":cursor"

        due_members = await redis.zrange(due_key, 0, -1, withscores=True)
        wake_members = await redis.zrange(wake_key, 0, -1, withscores=True)
        out_members = await redis.zrange(out_key, 0, -1, withscores=True)
        run_members = await redis.zrange(run_key, 0, -1, withscores=True)
        cursor = await redis.get(cursor_key)

        print(f"\n--- Stage: {stage} ---")
        print(f"  due: {[(str(m), round(s - now, 1)) for m, s in due_members]}")
        print(f"  wake: {[(str(m), round(s - now, 1)) for m, s in wake_members]}")
        print(f"  outstanding: {[str(m) for m, s in out_members]}")
        print(f"  running: {[str(m) for m, s in run_members]}")
        print(f"  cursor: {cursor}")

        for acc in [2, 33, 34, 35]:
            ticket = await redis.get(f"{PREFIX}{stage}:ticket:{acc}")
            if ticket:
                print(f"  ticket for acc {acc}: {ticket}")

    for acc in [2, 33, 34, 35]:
        run_key = f"{PREFIX}account:{acc}:run"
        prio_key = f"{PREFIX}account:{acc}:priority"
        run_acc = await redis.get(run_key)
        run_ttl = await redis.ttl(run_key)
        prio_acc = await redis.get(prio_key)
        prio_ttl = await redis.ttl(prio_key)
        print(f"\nAccount {acc} locks: run={run_acc} (ttl={run_ttl}), priority={prio_acc} (ttl={prio_ttl})")

if __name__ == "__main__":
    asyncio.run(main())
