import asyncio
import time
from app.core.redis import init_redis, get_redis
from app.core.operating_time import operating_date

async def reset():
    await init_redis()
    client = await get_redis()
    now = int(time.time())
    today_str = f"{operating_date():%Y%m%d}"
    
    for acc in [33, 35]:
        print(f"=== Resetting false state for Account {acc} ===")
        # Reset ads count used to 0
        await client.hset(f"vanguard:growth:v2:ads:{acc}:count", "used", 0)
        # Delete cooldown
        await client.delete(f"vanguard:growth:v2:ads:{acc}:due")
        # Reset risk outbound
        risk_key = f"risk:account:{acc}:daily:outbound_message:{today_str}"
        await client.delete(risk_key)
        # Set ads:due to now so it dispatches immediately
        await client.zadd("vanguard:growth_dispatch:v2:ads:due", {str(acc): now})
        print(f"Account {acc} reset: count.used=0, cooldown deleted, dispatch queue set to {now}")

if __name__ == "__main__":
    asyncio.run(reset())
