import asyncio
from app.core import database as db
from app.core.account.models import TelegramAccount

async def check():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc_id in [2, 33, 34, 35]:
            a = await session.get(TelegramAccount, acc_id)
            if a:
                print(f"Account {a.id} ({a.phone}): is_active={a.is_active}, status={a.status}, risk_level={a.risk_level}, type={a.account_type}")

if __name__ == "__main__":
    asyncio.run(check())
