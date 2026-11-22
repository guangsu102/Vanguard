import asyncio
from app.core import database as db
from app.core.account.models import TelegramAccount

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        a = await session.get(TelegramAccount, 33)
        if a:
            for col in a.__table__.columns:
                val = getattr(a, col.name)
                if val is not None:
                    print(f"  {col.name}: {val}")

if __name__ == "__main__":
    asyncio.run(check())
