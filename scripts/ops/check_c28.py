import asyncio
from app.core import database as db
from app.modules.acquisition.models import AdCreative

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        c = await session.get(AdCreative, 28)
        if c:
            print(f"Creative 28 content:\n{c.content}")

if __name__ == "__main__":
    asyncio.run(check())
