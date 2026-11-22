import asyncio
from datetime import datetime
from sqlalchemy import update
from app.core import database as db
from app.core.account.models import TelegramAccount, AccountStatus

async def set_online():
    await db.init_db()
    async with db.async_session_factory() as session:
        await session.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id == 33)
            .values(status=AccountStatus.ONLINE, is_active=True, last_active_at=datetime.utcnow())
        )
        await session.commit()
        print("Account 33 status set to ONLINE in DB.")

if __name__ == "__main__":
    asyncio.run(set_online())
