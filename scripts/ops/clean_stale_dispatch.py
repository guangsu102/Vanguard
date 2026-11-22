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
    print(f"Current time: {now}")

    for stage, stage_spec in STAGES.items():
        run_key = PREFIX + "running:" + stage_spec.queue
        out_key = PREFIX + stage + ":outstanding"
        
        # Check running set
        running = await redis.zrange(run_key, 0, -1, withscores=True)
        for token, exp in running:
            print(f"Stage {stage} running token {token}, expires in {exp - now:.1f}s")
            # If token is stale, let's clean it
            await redis.zrem(run_key, token)
            print(f"  -> Removed {token} from {run_key}")

        # Check outstanding set
        out = await redis.zrange(out_key, 0, -1, withscores=True)
        for acc, exp in out:
            print(f"Stage {stage} outstanding acc {acc}, expires in {exp - now:.1f}s")
            t_key = f"{PREFIX}{stage}:ticket:{acc}"
            val = await redis.get(t_key)
            print(f"  ticket {t_key} = {val}")
            await redis.delete(t_key)
            await redis.zrem(out_key, acc)
            print(f"  -> Cleared ticket and removed {acc} from {out_key}")

    for acc in [2, 33, 34, 35]:
        r_key = f"{PREFIX}account:{acc}:run"
        p_key = f"{PREFIX}account:{acc}:priority"
        print(f"Account {acc} run={await redis.get(r_key)}, prio={await redis.get(p_key)}")
        await redis.delete(r_key)
        await redis.delete(p_key)
        print(f"  -> Cleared {r_key} and {p_key}")

    print("Cleanup done!")

if __name__ == "__main__":
    asyncio.run(main())
