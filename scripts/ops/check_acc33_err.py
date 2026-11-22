import asyncio
from app.core import database as db
from app.core.account.models import TelegramAccount

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        a = await session.get(TelegramAccount, 33)
        if a:
            print(f"Account 33: phone={a.phone}, status={a.status}, error={a.error_message}, proxy_id={a.proxy_id}, proxy_mode={a.proxy_mode}")

if __name__ == "__main__":
    asyncio.run(check())
