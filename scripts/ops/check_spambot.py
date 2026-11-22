import asyncio
from app.core.database import init_db, get_db
from app.core.account.models import TelegramAccount
from sqlalchemy import select

async def check():
    await init_db()
    async for db in get_db():
        stmt = select(TelegramAccount).where(TelegramAccount.id.in_([2, 33, 34, 35]))
        accs = (await db.execute(stmt)).scalars().all()
        for a in accs:
            print(f"Account {a.id} ({a.phone}):")
            print(f"  spam_status={a.spam_check_status}, checked_at={a.spam_checked_at}")
            print(f"  summary={a.spam_check_summary}")
            print(f"  restriction_reason={a.restriction_reason}, detected_at={a.restriction_detected_at}")
        break

asyncio.run(check())
