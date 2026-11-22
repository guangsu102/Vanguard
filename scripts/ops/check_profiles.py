import asyncio
import os
import sys

sys.path.append("/app")

from app.core import database as db
from app.core.account.models import TelegramAccount
from sqlalchemy import select

async def main():
    await db.init_db()
    async with db.async_session_factory() as session:
        for acc_id in [2, 33, 34, 35]:
            acc = await session.get(TelegramAccount, acc_id)
            print(f"\nAccount {acc_id}: phone={acc.phone}, status={acc.status}, type={acc.account_type}")

if __name__ == "__main__":
    asyncio.run(main())
