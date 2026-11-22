import asyncio
import os
import sys

sys.path.append("/app")

from app.core.redis import init_redis
from app.core.account.session_exec import forward

async def main():
    await init_redis()
    print("Testing forward(33, 'ads')...")
    res33 = await forward(33, "ads")
    print(f"Account 33 ads result: {res33}")

    print("\nTesting forward(35, 'ads')...")
    res35 = await forward(35, "ads")
    print(f"Account 35 ads result: {res35}")

if __name__ == "__main__":
    asyncio.run(main())
